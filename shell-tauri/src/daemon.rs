//! Managed Python Core daemon supervisor (`omnidesk_core serve`) for the Tauri shell.

use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
#[cfg(unix)]
use std::os::unix::process::CommandExt;

use serde::Serialize;
use tokio::sync::Notify;

use crate::events::EventBridge;
use crate::paths::{ensure_private_dir, has_parent_or_nul, OmnideskPaths};
use crate::protocol::RpcError;
use crate::redact::redact_text;

const DEFAULT_STARTUP_TIMEOUT_MS: u64 = 6_000;
const DEFAULT_SHUTDOWN_GRACE_MS: u64 = 2_000;
const DEFAULT_POLL_INTERVAL_MS: u64 = 40;
const MAX_LOG_TAIL_BYTES: usize = 2048;

const ENV_ALLOWLIST: &[&str] = &[
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "VIRTUAL_ENV",
    "UV_CACHE_DIR",
    "UV_PYTHON",
    "UV_PROJECT_ENVIRONMENT",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
];

#[derive(Debug, Clone)]
pub struct DaemonConfig {
    pub repo_root: Option<PathBuf>,
    pub plugins_dir: Option<PathBuf>,
    pub uv_bin: Option<PathBuf>,
    pub python_bin: Option<PathBuf>,
    pub startup_timeout: Duration,
    pub shutdown_grace: Duration,
    pub poll_interval: Duration,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            repo_root: env_abs_path("OMNIDESK_REPO_ROOT"),
            plugins_dir: None,
            uv_bin: None,
            python_bin: None,
            startup_timeout: Duration::from_millis(DEFAULT_STARTUP_TIMEOUT_MS),
            shutdown_grace: Duration::from_millis(DEFAULT_SHUTDOWN_GRACE_MS),
            poll_interval: Duration::from_millis(DEFAULT_POLL_INTERVAL_MS),
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct DaemonSnapshot {
    pub status: String,
    pub owned_by_shell: bool,
    pub pid: Option<u32>,
    pub launcher: Option<String>,
    pub socket_path: Option<String>,
    pub log_path: Option<String>,
    pub last_error: Option<String>,
}

impl Default for DaemonSnapshot {
    fn default() -> Self {
        Self {
            status: "unstarted".to_string(),
            owned_by_shell: false,
            pid: None,
            launcher: None,
            socket_path: None,
            log_path: None,
            last_error: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonCommandSpec {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: BTreeMap<String, String>,
    pub launcher: String,
}

struct ManagedChild {
    child: Child,
    pid: u32,
}

pub struct DaemonManager {
    paths: OmnideskPaths,
    config: DaemonConfig,
    host_env: HashMap<String, String>,
    lifecycle_mu: Mutex<()>,
    owned_child: Mutex<Option<ManagedChild>>,
    snapshot: RwLock<DaemonSnapshot>,
    startup_finished: (Mutex<bool>, Condvar),
    startup_notify: Notify,
    shutdown_requested: AtomicBool,
}

impl DaemonManager {
    pub fn new(paths: OmnideskPaths, config: DaemonConfig) -> Arc<Self> {
        let host_env: HashMap<String, String> = std::env::vars().collect();
        Self::with_env(paths, config, host_env)
    }

    pub fn with_env(
        paths: OmnideskPaths,
        config: DaemonConfig,
        host_env: HashMap<String, String>,
    ) -> Arc<Self> {
        let log_path = paths.daemon_log_path().display().to_string();
        Arc::new(Self {
            paths,
            config,
            host_env,
            lifecycle_mu: Mutex::new(()),
            owned_child: Mutex::new(None),
            snapshot: RwLock::new(DaemonSnapshot {
                log_path: Some(log_path),
                ..DaemonSnapshot::default()
            }),
            startup_finished: (Mutex::new(false), Condvar::new()),
            startup_notify: Notify::new(),
            shutdown_requested: AtomicBool::new(false),
        })
    }

    pub fn snapshot(&self) -> DaemonSnapshot {
        self.snapshot
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    pub fn start_in_background(self: &Arc<Self>, bridge: Arc<EventBridge>) {
        self.set_starting_state();
        let manager = Arc::clone(self);
        let _ = std::thread::Builder::new()
            .name("omnidesk-core-supervisor".to_string())
            .spawn(move || {
                manager.start_or_attach(Some(bridge.as_ref()));
            });
    }

    fn set_starting_state(&self) {
        if let Ok(mut guard) = self.snapshot.write() {
            if matches!(guard.status.as_str(), "unstarted" | "offline" | "stopped") {
                guard.status = "starting".to_string();
            }
        }
        if let Ok(mut done) = self.startup_finished.0.lock() {
            *done = false;
        }
    }

    fn mark_startup_settled(&self) {
        if let Ok(mut done) = self.startup_finished.0.lock() {
            *done = true;
            self.startup_finished.1.notify_all();
        }
        self.startup_notify.notify_waiters();
    }

    /// Wait asynchronously if startup is currently in progress (bounded by `max_wait`).
    pub async fn wait_if_starting(&self, max_wait: Duration) {
        let deadline = tokio::time::Instant::now() + max_wait;
        loop {
            let status = self.snapshot().status;
            if status != "starting" || self.shutdown_requested.load(Ordering::Acquire) {
                return;
            }
            let now = tokio::time::Instant::now();
            if now >= deadline {
                return;
            }
            let remaining = deadline.saturating_duration_since(now);
            let notified = self.startup_notify.notified();
            let _ = tokio::time::timeout(remaining.min(Duration::from_millis(100)), notified).await;
        }
    }

    /// Probe `control.sock`, attach if an external daemon is alive, or launch & supervise Core.
    pub fn start_or_attach(&self, bridge: Option<&EventBridge>) -> DaemonSnapshot {
        let _guard = self.lifecycle_mu.lock().unwrap_or_else(|e| e.into_inner());
        if self.shutdown_requested.load(Ordering::Acquire) {
            self.mark_startup_settled();
            return self.snapshot();
        }

        // 1. Check if we already own a living child process.
        if let Some(existing_pid) = self.check_owned_child_alive() {
            if let Ok(Some(sock)) = self.paths.resolve_listening_socket() {
                self.update_snapshot(|s| {
                    s.status = "running".to_string();
                    s.owned_by_shell = true;
                    s.pid = Some(existing_pid);
                    s.socket_path = Some(sock.display().to_string());
                    s.last_error = None;
                });
                self.mark_startup_settled();
                return self.snapshot();
            }
        }

        // 2. Probe control.sock first; never duplicate an active external daemon.
        match self.paths.resolve_listening_socket() {
            Ok(Some(sock)) => {
                self.update_snapshot(|s| {
                    s.status = "external".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.launcher = None;
                    s.socket_path = Some(sock.display().to_string());
                    s.last_error = None;
                });
                if let Some(b) = bridge {
                    b.notify_wakeup();
                }
                self.mark_startup_settled();
                return self.snapshot();
            }
            Ok(None) => {}
            Err(err) => {
                self.update_snapshot(|s| {
                    s.status = "offline".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.last_error = Some(err.message.clone());
                });
                self.mark_startup_settled();
                return self.snapshot();
            }
        }

        // 3. Build validated argv/env command spec (zero shell concatenation).
        let spec = match self.resolve_command_spec() {
            Ok(spec) => spec,
            Err(err) => {
                self.update_snapshot(|s| {
                    s.status = "offline".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.last_error = Some(err.message.clone());
                });
                self.mark_startup_settled();
                return self.snapshot();
            }
        };

        // 4. Open private 0600 log file for stdout/stderr.
        let log_path = self.paths.daemon_log_path();
        let mut log_file = match open_private_log_file(&log_path) {
            Ok(f) => f,
            Err(err) => {
                self.update_snapshot(|s| {
                    s.status = "offline".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.last_error = Some(err.message.clone());
                });
                self.mark_startup_settled();
                return self.snapshot();
            }
        };
        let ts_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let _ = writeln!(
            log_file,
            "\n[omnidesk-shell ts={ts_ms}] launching {} {:?} (cwd={})",
            spec.program.display(),
            spec.args,
            spec.cwd.display()
        );
        let _ = log_file.flush();

        let stderr_file = match log_file.try_clone() {
            Ok(f) => f,
            Err(err) => {
                self.update_snapshot(|s| {
                    s.status = "offline".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.last_error = Some(redact_text(&format!("Failed cloning log handle: {err}")));
                });
                self.mark_startup_settled();
                return self.snapshot();
            }
        };

        // 5. Spawn child process in its own process group with explicit argv + env.
        let mut cmd = Command::new(&spec.program);
        cmd.args(&spec.args)
            .current_dir(&spec.cwd)
            .env_clear()
            .envs(&spec.env)
            .stdin(Stdio::null())
            .stdout(Stdio::from(log_file))
            .stderr(Stdio::from(stderr_file));

        #[cfg(unix)]
        {
            // SAFETY: `setpgid(0, 0)` is async-signal-safe and isolates child group for cleanup.
            unsafe {
                cmd.pre_exec(|| {
                    let _ = libc::setpgid(0, 0);
                    Ok(())
                });
            }
        }

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(err) => {
                self.update_snapshot(|s| {
                    s.status = "offline".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.launcher = Some(spec.launcher.clone());
                    s.last_error = Some(redact_text(&format!(
                        "Failed spawning Core daemon ({}): {err}",
                        spec.program.display()
                    )));
                });
                self.mark_startup_settled();
                return self.snapshot();
            }
        };

        let pid = child.id();
        self.update_snapshot(|s| {
            s.status = "starting".to_string();
            s.owned_by_shell = true;
            s.pid = Some(pid);
            s.launcher = Some(spec.launcher.clone());
            s.log_path = Some(log_path.display().to_string());
            s.last_error = None;
        });

        let mut managed = ManagedChild { child, pid };

        // 6. Bounded wait for control.sock readiness (or early exit / shutdown).
        let deadline = Instant::now() + self.config.startup_timeout;
        let poll = self.config.poll_interval.max(Duration::from_millis(10));

        loop {
            if self.shutdown_requested.load(Ordering::Acquire) {
                terminate_managed_child(&mut managed, self.config.shutdown_grace);
                self.update_snapshot(|s| {
                    s.status = "stopped".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                });
                self.mark_startup_settled();
                return self.snapshot();
            }

            match managed.child.try_wait() {
                Ok(Some(exit_status)) => {
                    let tail = read_log_tail(&log_path, MAX_LOG_TAIL_BYTES);
                    let detail = if tail.is_empty() {
                        format!("Core daemon exited prematurely ({exit_status})")
                    } else {
                        format!("Core daemon exited prematurely ({exit_status}): {tail}")
                    };
                    self.update_snapshot(|s| {
                        s.status = "offline".to_string();
                        s.owned_by_shell = false;
                        s.pid = None;
                        s.socket_path = None;
                        s.last_error = Some(redact_text(&detail));
                    });
                    self.mark_startup_settled();
                    return self.snapshot();
                }
                Ok(None) => {}
                Err(err) => {
                    terminate_managed_child(&mut managed, self.config.shutdown_grace);
                    self.update_snapshot(|s| {
                        s.status = "offline".to_string();
                        s.owned_by_shell = false;
                        s.pid = None;
                        s.last_error = Some(redact_text(&format!(
                            "Failed polling Core daemon status: {err}"
                        )));
                    });
                    self.mark_startup_settled();
                    return self.snapshot();
                }
            }

            match self.paths.resolve_listening_socket() {
                Ok(Some(sock)) => {
                    if let Ok(mut guard) = self.owned_child.lock() {
                        *guard = Some(managed);
                    }
                    self.update_snapshot(|s| {
                        s.status = "running".to_string();
                        s.owned_by_shell = true;
                        s.pid = Some(pid);
                        s.socket_path = Some(sock.display().to_string());
                        s.last_error = None;
                    });
                    if let Some(b) = bridge {
                        b.notify_wakeup();
                    }
                    self.mark_startup_settled();
                    return self.snapshot();
                }
                Ok(None) => {}
                Err(err) => {
                    terminate_managed_child(&mut managed, self.config.shutdown_grace);
                    self.update_snapshot(|s| {
                        s.status = "offline".to_string();
                        s.owned_by_shell = false;
                        s.pid = None;
                        s.last_error = Some(err.message.clone());
                    });
                    self.mark_startup_settled();
                    return self.snapshot();
                }
            }

            if Instant::now() >= deadline {
                terminate_managed_child(&mut managed, self.config.shutdown_grace);
                let tail = read_log_tail(&log_path, MAX_LOG_TAIL_BYTES);
                let mut msg = format!(
                    "Timed out after {} ms waiting for control.sock at {}",
                    self.config.startup_timeout.as_millis(),
                    self.paths.primary_socket_path().display()
                );
                if !tail.is_empty() {
                    msg.push_str(": ");
                    msg.push_str(&tail);
                }
                self.update_snapshot(|s| {
                    s.status = "offline".to_string();
                    s.owned_by_shell = false;
                    s.pid = None;
                    s.socket_path = None;
                    s.last_error = Some(redact_text(&msg));
                });
                self.mark_startup_settled();
                return self.snapshot();
            }

            std::thread::sleep(poll);
        }
    }

    /// Stop ONLY the daemon child spawned by this shell (never external daemons): SIGTERM -> SIGKILL.
    pub fn stop(&self) {
        self.shutdown_requested.store(true, Ordering::Release);
        self.mark_startup_settled();

        let _guard = self.lifecycle_mu.lock().unwrap_or_else(|e| e.into_inner());
        let child_opt = self
            .owned_child
            .lock()
            .ok()
            .and_then(|mut guard| guard.take());
        if let Some(mut managed) = child_opt {
            terminate_managed_child(&mut managed, self.config.shutdown_grace);
            self.update_snapshot(|s| {
                s.status = "stopped".to_string();
                s.owned_by_shell = false;
                s.pid = None;
            });
        }
    }

    fn check_owned_child_alive(&self) -> Option<u32> {
        let mut guard = self.owned_child.lock().ok()?;
        let managed = guard.as_mut()?;
        match managed.child.try_wait() {
            Ok(None) => Some(managed.pid),
            _ => {
                *guard = None;
                None
            }
        }
    }

    fn resolve_command_spec(&self) -> Result<DaemonCommandSpec, RpcError> {
        let cwd = std::env::current_dir().ok();
        let exe = std::env::current_exe().ok();
        let repo_root = resolve_repo_root(
            self.config.repo_root.as_deref(),
            cwd.as_deref(),
            exe.as_deref(),
        )
        .ok_or_else(|| {
            RpcError::service_offline(
                "Could not locate OmniDesk repository root containing core/omnidesk_core and plugins",
            )
        })?;

        let plugins_dir = self
            .config
            .plugins_dir
            .clone()
            .unwrap_or_else(|| repo_root.join("plugins"));

        build_daemon_command_spec(
            &repo_root,
            &plugins_dir,
            &self.paths,
            &self.host_env,
            self.config.uv_bin.as_deref(),
            self.config.python_bin.as_deref(),
        )
    }

    fn update_snapshot(&self, update: impl FnOnce(&mut DaemonSnapshot)) {
        if let Ok(mut guard) = self.snapshot.write() {
            update(&mut guard);
        }
    }
}

impl Drop for DaemonManager {
    fn drop(&mut self) {
        self.shutdown_requested.store(true, Ordering::Release);
        let child_opt = self
            .owned_child
            .lock()
            .ok()
            .and_then(|mut guard| guard.take());
        if let Some(mut managed) = child_opt {
            terminate_managed_child(&mut managed, self.config.shutdown_grace);
        }
    }
}

/// Locate the OmniDesk repository root (`plugins/` + `core/omnidesk_core` or `pyproject.toml`).
pub fn resolve_repo_root(
    explicit: Option<&Path>,
    cwd: Option<&Path>,
    exe: Option<&Path>,
) -> Option<PathBuf> {
    if let Some(dir) = explicit {
        if is_valid_repo_root(dir) {
            return Some(dir.to_path_buf());
        }
    }

    for start in [cwd, exe.and_then(Path::parent)].into_iter().flatten() {
        for ancestor in start.ancestors() {
            if is_valid_repo_root(ancestor) {
                return Some(ancestor.to_path_buf());
            }
        }
    }

    if let Some(manifest_dir) = option_env!("CARGO_MANIFEST_DIR") {
        if let Some(repo) = Path::new(manifest_dir).parent() {
            if is_valid_repo_root(repo) {
                return Some(repo.to_path_buf());
            }
        }
    }
    None
}

fn is_valid_repo_root(dir: &Path) -> bool {
    if !dir.is_absolute() || has_parent_or_nul(dir) || !dir.is_dir() {
        return false;
    }
    dir.join("plugins").is_dir()
        && (dir.join("core").join("omnidesk_core").is_dir() || dir.join("pyproject.toml").is_file())
}

/// Build an argument-array-only, shell-free `DaemonCommandSpec` with explicit environment variables.
pub fn build_daemon_command_spec(
    repo_root: &Path,
    plugins_dir: &Path,
    paths: &OmnideskPaths,
    host_env: &HashMap<String, String>,
    uv_override: Option<&Path>,
    python_override: Option<&Path>,
) -> Result<DaemonCommandSpec, RpcError> {
    validate_clean_abs_dir(repo_root, "repo_root")?;
    validate_clean_abs_dir(plugins_dir, "plugins_dir")?;

    let path_env = host_env.get("PATH").map(String::as_str);
    let uv_path = uv_override
        .filter(|p| is_executable_file(p))
        .map(Path::to_path_buf)
        .or_else(|| {
            if uv_override.is_some() {
                None
            } else {
                find_executable_in_path("uv", path_env)
            }
        });

    let (program, args, launcher) = if let Some(uv) = uv_path {
        (
            uv,
            vec![
                "run".to_string(),
                "python3".to_string(),
                "-m".to_string(),
                "omnidesk_core".to_string(),
                "serve".to_string(),
                "--plugins-dir".to_string(),
                plugins_dir.to_string_lossy().into_owned(),
            ],
            "uv".to_string(),
        )
    } else {
        let venv_py = repo_root.join(".venv").join("bin").join("python3");
        let py = python_override
            .filter(|p| is_executable_file(p))
            .map(Path::to_path_buf)
            .or_else(|| {
                if python_override.is_some() {
                    None
                } else {
                    find_executable_in_path("python3", path_env)
                }
            })
            .or_else(|| {
                if python_override.is_none() && is_executable_file(&venv_py) {
                    Some(venv_py)
                } else {
                    None
                }
            })
            .ok_or_else(|| {
                RpcError::service_offline(
                    "Neither 'uv' nor 'python3' executable found to start omnidesk_core",
                )
            })?;
        (
            py,
            vec![
                "-m".to_string(),
                "omnidesk_core".to_string(),
                "serve".to_string(),
                "--plugins-dir".to_string(),
                plugins_dir.to_string_lossy().into_owned(),
            ],
            "python3".to_string(),
        )
    };

    let mut env = BTreeMap::new();
    for key in ENV_ALLOWLIST {
        if let Some(val) = host_env.get(*key) {
            if !val.contains('\0') {
                env.insert((*key).to_string(), val.clone());
            }
        }
    }
    env.entry("PATH".to_string())
        .or_insert_with(|| "/usr/local/bin:/usr/bin:/bin".to_string());
    env.entry("LANG".to_string())
        .or_insert_with(|| "C.UTF-8".to_string());
    env.insert(
        "HOME".to_string(),
        paths.home_dir.to_string_lossy().into_owned(),
    );
    if let Some(cfg_home) = paths.config_dir.parent() {
        env.insert(
            "XDG_CONFIG_HOME".to_string(),
            cfg_home.to_string_lossy().into_owned(),
        );
    }
    if let Some(data_home) = paths.data_dir.parent() {
        env.insert(
            "XDG_DATA_HOME".to_string(),
            data_home.to_string_lossy().into_owned(),
        );
    }
    if let Some(state_home) = paths.state_dir.parent() {
        env.insert(
            "XDG_STATE_HOME".to_string(),
            state_home.to_string_lossy().into_owned(),
        );
    }
    if let Some(rt_home) = paths.runtime_dir.as_deref().and_then(Path::parent) {
        env.insert(
            "XDG_RUNTIME_DIR".to_string(),
            rt_home.to_string_lossy().into_owned(),
        );
    }
    if let Some(cache_home) = host_env.get("XDG_CACHE_HOME").filter(|v| {
        let p = Path::new(v.as_str());
        p.is_absolute() && !has_parent_or_nul(p)
    }) {
        env.insert("XDG_CACHE_HOME".to_string(), cache_home.clone());
    } else {
        env.insert(
            "XDG_CACHE_HOME".to_string(),
            paths.home_dir.join(".cache").to_string_lossy().into_owned(),
        );
    }

    env.insert("PYTHONUNBUFFERED".to_string(), "1".to_string());
    env.insert(
        "OMNIDESK_PLUGINS_DIR".to_string(),
        plugins_dir.to_string_lossy().into_owned(),
    );

    if launcher == "python3" {
        let mut py_paths = vec![repo_root.join("core"), repo_root.to_path_buf()];
        if let Some(existing) = host_env.get("PYTHONPATH") {
            for p in std::env::split_paths(existing) {
                if !p.as_os_str().is_empty() && !py_paths.contains(&p) {
                    py_paths.push(p);
                }
            }
        }
        if let Ok(joined) = std::env::join_paths(py_paths) {
            env.insert(
                "PYTHONPATH".to_string(),
                joined.to_string_lossy().into_owned(),
            );
        }
    }

    Ok(DaemonCommandSpec {
        program,
        args,
        cwd: repo_root.to_path_buf(),
        env,
        launcher,
    })
}

fn validate_clean_abs_dir(path: &Path, label: &str) -> Result<(), RpcError> {
    if !path.is_absolute() || has_parent_or_nul(path) {
        return Err(RpcError::path_safety(format!(
            "Unsafe {label} path: {}",
            path.display()
        )));
    }
    if !path.is_dir() {
        return Err(RpcError::invalid_argument(format!(
            "{label} directory does not exist: {}",
            path.display()
        )));
    }
    Ok(())
}

pub(crate) fn find_executable_in_path(name: &str, path_env: Option<&str>) -> Option<PathBuf> {
    let raw = path_env?;
    for dir in std::env::split_paths(raw) {
        if dir.as_os_str().is_empty() || !dir.is_absolute() || has_parent_or_nul(&dir) {
            continue;
        }
        let cand = dir.join(name);
        if is_executable_file(&cand) {
            return Some(cand);
        }
    }
    None
}

pub(crate) fn is_executable_file(path: &Path) -> bool {
    if has_parent_or_nul(path) {
        return false;
    }
    let Ok(meta) = fs::metadata(path) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        (meta.permissions().mode() & 0o111) != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn env_abs_path(key: &str) -> Option<PathBuf> {
    let raw = std::env::var(key).ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let path = PathBuf::from(trimmed);
    if path.is_absolute() && !has_parent_or_nul(&path) {
        Some(path)
    } else {
        None
    }
}

fn open_private_log_file(log_path: &Path) -> Result<File, RpcError> {
    let parent = log_path
        .parent()
        .ok_or_else(|| RpcError::path_safety("Daemon log path has no parent directory"))?;
    ensure_private_dir(&self_or_state_parent(parent), "daemon state directory")?;
    ensure_private_dir(parent, "daemon log directory")?;

    if let Ok(meta) = fs::symlink_metadata(log_path) {
        if meta.file_type().is_symlink() {
            return Err(RpcError::path_safety(format!(
                "Refusing symlinked daemon log file: {}",
                log_path.display()
            )));
        }
    }

    let mut opts = OpenOptions::new();
    opts.create(true).append(true);
    #[cfg(unix)]
    opts.mode(0o600);
    let file = opts.open(log_path).map_err(|err| {
        RpcError::service_offline(format!(
            "Failed opening daemon log file {}: {err}",
            log_path.display()
        ))
    })?;
    #[cfg(unix)]
    {
        let _ = fs::set_permissions(log_path, fs::Permissions::from_mode(0o600));
    }
    Ok(file)
}

fn self_or_state_parent(log_dir: &Path) -> PathBuf {
    log_dir.parent().unwrap_or(log_dir).to_path_buf()
}

fn read_log_tail(log_path: &Path, max_bytes: usize) -> String {
    let Ok(mut file) = File::open(log_path) else {
        return String::new();
    };
    let Ok(len) = file.metadata().map(|m| m.len()) else {
        return String::new();
    };
    let start = len.saturating_sub(max_bytes as u64);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut buf = Vec::new();
    if file.read_to_end(&mut buf).is_err() {
        return String::new();
    }
    let text = String::from_utf8_lossy(&buf);
    text.lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .trim()
        .to_string()
}

pub(crate) fn terminate_child_process_group(child: &mut Child, pid: u32, grace: Duration) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }

    #[cfg(unix)]
    let signal_target = |target_pid: u32| -> libc::pid_t {
        let p = target_pid as libc::pid_t;
        // SAFETY: `getpgid` is thread-safe and inspects whether child leads its own process group.
        let pgid = unsafe { libc::getpgid(p) };
        if pgid == p && p > 0 { -p } else { p }
    };

    #[cfg(unix)]
    {
        let target = signal_target(pid);
        // SAFETY: `target` is either `-pgid` of our own isolated child group or `pid` of our child.
        unsafe {
            libc::kill(target, libc::SIGTERM);
        }
    }
    #[cfg(not(unix))]
    let _ = (pid, child.kill());

    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(25));
    }

    #[cfg(unix)]
    {
        let target = signal_target(pid);
        // SAFETY: `target` is our still-running child or its process group after SIGTERM grace expired.
        unsafe {
            libc::kill(target, libc::SIGKILL);
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn terminate_managed_child(managed: &mut ManagedChild, grace: Duration) {
    terminate_child_process_group(&mut managed.child, managed.pid, grace);
}
