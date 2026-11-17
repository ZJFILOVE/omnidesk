//! XDG Base Directory resolution and strict control.sock path/permission validation.

use std::collections::HashMap;
use std::fs;
use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};

#[cfg(unix)]
use std::ffi::{CStr, OsStr};
#[cfg(unix)]
use std::os::unix::ffi::OsStrExt;
#[cfg(unix)]
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};

use tokio::net::UnixStream;

use crate::protocol::RpcError;

/// Resolved XDG directories for OmniDesk control socket and plugin discovery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OmnideskPaths {
    pub home_dir: PathBuf,
    pub config_dir: PathBuf,
    pub data_dir: PathBuf,
    pub state_dir: PathBuf,
    pub runtime_dir: Option<PathBuf>,
}

/// Result of checking a candidate control socket path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SocketCheck {
    Ready(PathBuf),
    Missing,
}

fn resolve_xdg_abs(env: &HashMap<String, String>, key: &str) -> Option<PathBuf> {
    let raw = env.get(key)?.trim();
    if raw.is_empty() || raw.contains('\0') {
        return None;
    }
    let path = PathBuf::from(raw);
    if path.is_absolute() && !has_parent_or_nul(&path) {
        Some(path)
    } else {
        None
    }
}

impl OmnideskPaths {
    /// Construct paths from the current process environment.
    pub fn from_env() -> Self {
        let env: HashMap<String, String> = std::env::vars().collect();
        Self::from_env_map(&env)
    }

    /// Construct paths from an explicit environment map (matching core `OmnideskPaths.from_env`).
    pub fn from_env_map(env: &HashMap<String, String>) -> Self {
        let home_dir = resolve_xdg_abs(env, "HOME").unwrap_or_else(default_home_dir);
        let config_base = resolve_xdg_abs(env, "XDG_CONFIG_HOME").unwrap_or_else(|| home_dir.join(".config"));
        let data_base = resolve_xdg_abs(env, "XDG_DATA_HOME").unwrap_or_else(|| home_dir.join(".local").join("share"));
        let state_base = resolve_xdg_abs(env, "XDG_STATE_HOME").unwrap_or_else(|| home_dir.join(".local").join("state"));
        let runtime_base = resolve_xdg_abs(env, "XDG_RUNTIME_DIR");

        Self {
            home_dir,
            config_dir: config_base.join("omnidesk"),
            data_dir: data_base.join("omnidesk"),
            state_dir: state_base.join("omnidesk"),
            runtime_dir: runtime_base.map(|p| p.join("omnidesk")),
        }
    }

    /// Construct paths explicitly for isolated tests.
    pub fn for_dirs(runtime_dir: Option<PathBuf>, data_dir: PathBuf, config_dir: PathBuf) -> Self {
        let home_dir = data_dir.parent().unwrap_or(Path::new("/")).to_path_buf();
        let state_dir = data_dir.join("state");
        Self {
            home_dir,
            config_dir,
            data_dir,
            state_dir,
            runtime_dir,
        }
    }

    pub fn daemon_log_path(&self) -> PathBuf {
        self.state_dir.join("logs").join("omnidesk-core.log")
    }

    pub fn mpv_socket_path(&self) -> PathBuf {
        self.runtime_dir
            .as_ref()
            .cloned()
            .unwrap_or_else(|| self.data_dir.join("run"))
            .join("mpv.sock")
    }

    pub fn data_socket_path(&self) -> PathBuf {
        self.data_dir.join("run").join("control.sock")
    }

    pub fn primary_socket_path(&self) -> PathBuf {
        self.runtime_dir
            .as_ref()
            .map(|dir| dir.join("control.sock"))
            .unwrap_or_else(|| self.data_socket_path())
    }

    /// Return candidate control socket paths ordered by preference (`XDG_RUNTIME_DIR` then `XDG_DATA_HOME`).
    pub fn socket_candidates(&self) -> Vec<PathBuf> {
        let mut out = Vec::with_capacity(2);
        let primary = self.primary_socket_path();
        out.push(primary.clone());
        let fallback = self.data_socket_path();
        if fallback != primary {
            out.push(fallback);
        }
        out
    }

    /// Return true iff `path` is strictly one of the XDG runtime/data `control.sock` paths with no traversal.
    pub fn is_allowed_socket_path(&self, path: &Path) -> bool {
        if !path.is_absolute() || has_parent_or_nul(path) {
            return false;
        }
        self.socket_candidates().iter().any(|allowed| allowed == path)
    }

    /// Validate and resolve the active control socket path, failing closed on security violations.
    pub fn resolve_active_socket(&self) -> Result<PathBuf, RpcError> {
        let candidates = self.socket_candidates();
        for candidate in &candidates {
            match self.check_socket_path(candidate)? {
                SocketCheck::Ready(ready) => return Ok(ready),
                SocketCheck::Missing => continue,
            }
        }
        Err(RpcError::service_offline(format!(
            "Control socket not found at {}",
            candidates[0].display()
        )))
    }

    /// Inspect a single candidate socket path for XDG containment, non-symlink, ownership, and 0600 permissions.
    pub fn check_socket_path(&self, path: &Path) -> Result<SocketCheck, RpcError> {
        if !self.is_allowed_socket_path(path) {
            return Err(RpcError::path_safety(
                "Refusing control socket outside XDG runtime/data directories",
            ));
        }
        if path == self.data_socket_path() {
            let run_exists = fs::symlink_metadata(self.data_dir.join("run")).is_ok();
            let forbidden_mask = if run_exists { 0o077 } else { 0o022 };
            if matches!(
                validate_private_dir(&self.data_dir, "OmniDesk data directory", forbidden_mask)?,
                DirCheck::Missing
            ) {
                return Ok(SocketCheck::Missing);
            }
        }
        validate_socket_metadata(path)
    }

    /// Return true iff `path` is a valid allowed control socket with an actively listening same-UID peer.
    pub fn is_socket_listening(&self, path: &Path) -> Result<bool, RpcError> {
        match self.check_socket_path(path)? {
            SocketCheck::Missing => Ok(false),
            SocketCheck::Ready(ready) => match std::os::unix::net::UnixStream::connect(&ready) {
                Ok(stream) => {
                    verify_std_peer_uid(&stream, &ready)?;
                    Ok(true)
                }
                Err(err)
                    if matches!(
                        err.kind(),
                        ErrorKind::ConnectionRefused | ErrorKind::NotFound
                    ) =>
                {
                    Ok(false)
                }
                Err(err) if err.kind() == ErrorKind::PermissionDenied => {
                    Err(RpcError::permission_denied(format!(
                        "Permission denied probing control socket {}: {err}",
                        ready.display()
                    )))
                }
                Err(err) => Err(RpcError::service_offline(format!(
                    "Failed probing control socket {}: {err}",
                    ready.display()
                ))),
            },
        }
    }

    /// Probe all allowed control socket paths and return the first actively listening socket, if any.
    pub fn resolve_listening_socket(&self) -> Result<Option<PathBuf>, RpcError> {
        for candidate in self.socket_candidates() {
            if self.is_socket_listening(&candidate)? {
                return Ok(Some(candidate));
            }
        }
        Ok(None)
    }

    /// Resolve candidate plugin directories matching `omnidesk-pm list` search order.
    pub fn plugin_search_dirs(&self) -> Vec<PathBuf> {
        let mut dirs = Vec::new();
        if let Ok(raw) = std::env::var("OMNIDESK_PLUGINS_DIR") {
            for entry in std::env::split_paths(&raw) {
                if !entry.as_os_str().is_empty() && entry.is_dir() && !dirs.contains(&entry) {
                    dirs.push(entry);
                }
            }
            if !dirs.is_empty() {
                return dirs;
            }
        }
        if let Ok(cwd) = std::env::current_dir() {
            let cwd_plugins = cwd.join("plugins");
            if cwd_plugins.is_dir() {
                dirs.push(cwd_plugins);
            } else if let Some(parent_plugins) = cwd.parent().map(|p| p.join("plugins")) {
                if parent_plugins.is_dir() {
                    dirs.push(parent_plugins);
                }
            }
        }
        let xdg_plugins = self.data_dir.join("plugins");
        if !dirs.contains(&xdg_plugins) {
            dirs.push(xdg_plugins);
        }
        dirs
    }
}

pub(crate) fn has_parent_or_nul(path: &Path) -> bool {
    if path.to_string_lossy().contains('\0') {
        return true;
    }
    path.components().any(|c| matches!(c, Component::ParentDir))
}

#[cfg(unix)]
fn current_euid() -> u32 {
    // SAFETY: `geteuid` is thread-safe and has no preconditions.
    unsafe { libc::geteuid() }
}

#[cfg(unix)]
fn unix_home_dir() -> Option<PathBuf> {
    let uid = current_euid();
    let initial = unsafe { libc::sysconf(libc::_SC_GETPW_R_SIZE_MAX) };
    let mut buf_len = if initial > 0 { initial as usize } else { 1024 };
    let mut pwd = std::mem::MaybeUninit::<libc::passwd>::uninit();
    for _ in 0..6 {
        let mut buf = vec![0u8; buf_len];
        let mut result: *mut libc::passwd = std::ptr::null_mut();
        // SAFETY: `pwd` and `buf` are valid writable buffers for `getpwuid_r`.
        let rc = unsafe {
            libc::getpwuid_r(
                uid,
                pwd.as_mut_ptr(),
                buf.as_mut_ptr() as *mut libc::c_char,
                buf.len(),
                &mut result,
            )
        };
        if rc == 0 {
            if result.is_null() {
                return None;
            }
            // SAFETY: `result` is non-null and initialized when `rc == 0`.
            let pw_dir = unsafe { (*result).pw_dir };
            if pw_dir.is_null() {
                return None;
            }
            let cstr = unsafe { CStr::from_ptr(pw_dir) };
            let path = PathBuf::from(OsStr::from_bytes(cstr.to_bytes()));
            if path.is_absolute() && !has_parent_or_nul(&path) {
                return Some(path);
            }
            return None;
        }
        if rc != libc::ERANGE {
            return None;
        }
        buf_len = buf_len.saturating_mul(2);
    }
    None
}

fn default_home_dir() -> PathBuf {
    #[cfg(unix)]
    if let Some(dir) = unix_home_dir() {
        return dir;
    }
    PathBuf::from("/")
}

/// Verify connected Unix stream peer effective UID matches `geteuid()` (`SO_PEERCRED`).
pub(crate) fn verify_peer_uid(stream: &UnixStream, sock_path: &Path) -> Result<(), RpcError> {
    #[cfg(unix)]
    {
        let cred = stream.peer_cred().map_err(|err| {
            RpcError::permission_denied(format!(
                "Failed to read peer credentials from {}: {err}",
                sock_path.display()
            ))
        })?;
        let expected = current_euid();
        if cred.uid() != expected {
            return Err(RpcError::permission_denied(format!(
                "Refusing control.sock peer UID {} (expected {expected}) at {}",
                cred.uid(),
                sock_path.display()
            )));
        }
    }
    #[cfg(not(unix))]
    let _ = (stream, sock_path);
    Ok(())
}

/// Verify blocking Unix stream peer effective UID matches `geteuid()` (`SO_PEERCRED`).
pub(crate) fn verify_std_peer_uid(
    stream: &std::os::unix::net::UnixStream,
    sock_path: &Path,
) -> Result<(), RpcError> {
    #[cfg(unix)]
    {
        use std::os::unix::io::AsRawFd;
        let mut ucred = libc::ucred {
            pid: 0,
            uid: 0,
            gid: 0,
        };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        // SAFETY: `ucred` and `len` are valid initialized stack storage matching SO_PEERCRED.
        let rc = unsafe {
            libc::getsockopt(
                stream.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                (&mut ucred as *mut libc::ucred).cast::<libc::c_void>(),
                &mut len,
            )
        };
        if rc != 0 {
            let err = std::io::Error::last_os_error();
            return Err(RpcError::permission_denied(format!(
                "Failed to read peer credentials from {}: {err}",
                sock_path.display()
            )));
        }
        let expected = current_euid();
        if ucred.uid != expected {
            return Err(RpcError::permission_denied(format!(
                "Refusing socket peer UID {} (expected {expected}) at {}",
                ucred.uid,
                sock_path.display()
            )));
        }
    }
    #[cfg(not(unix))]
    let _ = (stream, sock_path);
    Ok(())
}

/// Ensure a private directory exists with `0700` permissions, owned by current UID, non-symlink.
pub(crate) fn ensure_private_dir(dir: &Path, label: &str) -> Result<(), RpcError> {
    if !dir.is_absolute() || has_parent_or_nul(dir) {
        return Err(RpcError::path_safety(format!(
            "Refusing unsafe {label} path: {}",
            dir.display()
        )));
    }
    match validate_private_dir(dir, label, 0o077)? {
        DirCheck::Present => Ok(()),
        DirCheck::Missing => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                let mut builder = fs::DirBuilder::new();
                builder.recursive(true).mode(0o700);
                builder.create(dir).map_err(|err| {
                    RpcError::service_offline(format!(
                        "Failed creating {label} at {}: {err}",
                        dir.display()
                    ))
                })?;
                fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).map_err(|err| {
                    RpcError::permission_denied(format!(
                        "Failed setting permissions on {label} at {}: {err}",
                        dir.display()
                    ))
                })?;
            }
            #[cfg(not(unix))]
            fs::create_dir_all(dir).map_err(|err| {
                RpcError::service_offline(format!(
                    "Failed creating {label} at {}: {err}",
                    dir.display()
                ))
            })?;
            match validate_private_dir(dir, label, 0o077)? {
                DirCheck::Present => Ok(()),
                DirCheck::Missing => Err(RpcError::service_offline(format!(
                    "{label} missing after creation: {}",
                    dir.display()
                ))),
            }
        }
    }
}

enum DirCheck {
    Present,
    Missing,
}

fn validate_private_dir(dir: &Path, label: &str, forbidden_mode_bits: u32) -> Result<DirCheck, RpcError> {
    match fs::symlink_metadata(dir) {
        Ok(meta) => {
            if meta.file_type().is_symlink() {
                return Err(RpcError::path_safety(format!(
                    "Refusing symlinked {label}: {}",
                    dir.display()
                )));
            }
            if !meta.is_dir() {
                return Err(RpcError::path_safety(format!(
                    "{label} is not a directory: {}",
                    dir.display()
                )));
            }
            #[cfg(unix)]
            {
                if meta.uid() != current_euid() {
                    return Err(RpcError::permission_denied(format!(
                        "{label} is not owned by current user: {}",
                        dir.display()
                    )));
                }
                let mode = meta.permissions().mode() & 0o777;
                if (mode & forbidden_mode_bits) != 0 {
                    return Err(RpcError::permission_denied(format!(
                        "Insecure {label} permissions {mode:04o}; expected no group/other access"
                    )));
                }
            }
            #[cfg(not(unix))]
            let _ = forbidden_mode_bits;
            Ok(DirCheck::Present)
        }
        Err(err) if err.kind() == ErrorKind::NotFound => Ok(DirCheck::Missing),
        Err(err) if err.kind() == ErrorKind::PermissionDenied => Err(RpcError::permission_denied(
            format!("Permission denied inspecting {label}: {err}"),
        )),
        Err(err) => Err(RpcError::service_offline(format!(
            "Cannot inspect {label}: {err}"
        ))),
    }
}

/// Validate socket parent directory (`0700`, same UID, non-symlink) and socket file (`0600`).
pub fn validate_socket_metadata(path: &Path) -> Result<SocketCheck, RpcError> {
    let parent = path
        .parent()
        .ok_or_else(|| RpcError::path_safety("Control socket has no parent directory"))?;

    if matches!(
        validate_private_dir(parent, "control socket directory", 0o077)?,
        DirCheck::Missing
    ) {
        return Ok(SocketCheck::Missing);
    }

    let sock_meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(SocketCheck::Missing),
        Err(err) if err.kind() == ErrorKind::PermissionDenied => {
            return Err(RpcError::permission_denied(format!(
                "Permission denied inspecting control socket: {err}"
            )));
        }
        Err(err) => {
            return Err(RpcError::service_offline(format!(
                "Cannot inspect control socket: {err}"
            )));
        }
    };

    if sock_meta.file_type().is_symlink() {
        return Err(RpcError::path_safety(format!(
            "Refusing symlinked control socket: {}",
            path.display()
        )));
    }

    #[cfg(unix)]
    {
        if !sock_meta.file_type().is_socket() {
            return Err(RpcError::path_safety(format!(
                "Existing path is not a Unix domain socket: {}",
                path.display()
            )));
        }
        if sock_meta.uid() != current_euid() {
            return Err(RpcError::permission_denied(format!(
                "Control socket is not owned by current user: {}",
                path.display()
            )));
        }
        let mode = sock_meta.permissions().mode() & 0o777;
        if mode != 0o600 {
            return Err(RpcError::permission_denied(format!(
                "Insecure control socket permissions {mode:04o}; expected 0600"
            )));
        }
    }

    Ok(SocketCheck::Ready(path.to_path_buf()))
}
