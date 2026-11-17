"""Tests for transactional PluginInstaller: install, update, 5-snapshot rollback, lockfile, concurrency, and security."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from omnidesk_core.errors import ErrorCode, OmniDeskError, PathSafetyError, PluginFaultError, PluginManifestError, PluginNotFoundError, ValidationError
from omnidesk_core.installer import MAX_SNAPSHOTS, PluginInstaller, compute_tree_hash
from omnidesk_core.paths import OmnideskPaths


def _make_paths(tmp_path: Path) -> OmnideskPaths:
    return OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            "OMNIDESK_STAGING_DIR": str(tmp_path / "staging"),
        }
    )


def _write_plugin_tree(
    root: Path,
    *,
    plugin_id: str = "plugin-sample",
    version: str = "0.1.0",
    schema_version: Any = 2,
    test_body: str = "def test_smoke():\n    assert True\n",
    on_load_body: str = '        ctx.register_capability("sample.echo.v1", lambda req: CapabilityResult.ok({"version": "' + "{ver}" + '", **req}))\n',
    extra_manifest: dict[str, Any] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "id": plugin_id,
        "name": f"Sample {plugin_id}",
        "version": version,
        "runtime": "python",
        "entry": "plugin:create_plugin",
        "kind": "provider",
        "domain": "core",
        "provides": ["sample.echo.v1"],
        "verify_cmd": "uv run pytest -q",
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    (root / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    load_impl = on_load_body.replace("{ver}", version)
    (root / "plugin.py").write_text(
        "from __future__ import annotations\n"
        "from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext\n\n"
        "class SamplePlugin(BasePlugin):\n"
        "    async def on_load(self, ctx: PluginContext) -> None:\n"
        f"{load_impl}"
        "    async def on_health_check(self) -> dict:\n"
        f'        return {{"ok": True, "version": "{version}"}}\n\n'
        "def create_plugin() -> BasePlugin:\n"
        "    return SamplePlugin()\n",
        encoding="utf-8",
    )
    if test_body is not None:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_plugin.py").write_text(test_body, encoding="utf-8")
    return root


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _init_git_plugin_repo(repo: Path, *, plugin_id: str = "plugin-git", version: str = "0.1.0") -> str:
    _write_plugin_tree(repo, plugin_id=plugin_id, version=version)
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, text=True, check=True)
    _git(repo, "config", "user.email", "test@omnidesk.local")
    _git(repo, "config", "user.name", "OmniDesk Tester")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"release {version}")
    _git(repo, "tag", f"v{version}")
    return _git(repo, "rev-parse", "HEAD")


def test_install_from_local_path_writes_lockfile_and_cleans_staging(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    src = _write_plugin_tree(tmp_path / "src" / "plugin-local", plugin_id="plugin-local", version="0.1.0")

    installer = PluginInstaller(paths=paths)
    res = installer.install(src, plugin_id="plugin-local")

    assert res.ok is True
    assert res.action == "install"
    assert res.plugin_id == "plugin-local"
    assert res.version == "0.1.0"
    assert res.source_type == "path"
    assert res.plugin_dir == paths.plugins_dir / "plugin-local"
    assert (res.plugin_dir / "plugin.json").is_file()
    assert res.content_hash == compute_tree_hash(res.plugin_dir)
    assert list(paths.staging_dir.iterdir()) == []

    lock_file = paths.upstreams_lock_path
    assert lock_file.is_file()
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
    entry = lock_data["locks"]["plugin-local"]
    assert entry["url"] == str(src.resolve())
    assert entry["content_hash"] == res.content_hash
    assert entry["version"] == "0.1.0"


def test_git_install_ref_update_five_snapshots_and_rollback(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    repo = tmp_path / "upstream-repo"
    commit_v1 = _init_git_plugin_repo(repo, plugin_id="plugin-git", version="0.1.0")

    installer = PluginInstaller(paths=paths)
    res1 = installer.install(f"file://{repo}", ref="v0.1.0")
    assert res1.ok is True
    assert res1.source_type == "git"
    assert res1.ref == "v0.1.0"
    assert res1.commit == commit_v1
    assert list(paths.staging_dir.iterdir()) == []

    # 6 updates -> keep only latest 5 snapshots (0.1.0 pruned; 0.2.0..0.6.0 kept)
    versions = ["0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0"]
    commits: dict[str, str] = {"0.1.0": commit_v1}
    hashes: dict[str, str] = {"0.1.0": res1.content_hash}
    for ver in versions:
        _write_plugin_tree(repo, plugin_id="plugin-git", version=ver)
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"release {ver}")
        commits[ver] = _git(repo, "rev-parse", "HEAD")
        upd = installer.update("plugin-git", ref="main")
        assert upd.ok is True
        assert upd.version == ver
        assert upd.commit == commits[ver]
        hashes[ver] = upd.content_hash

    snaps = installer.list_snapshots("plugin-git")
    assert len(snaps) == MAX_SNAPSHOTS
    assert [s.version for s in snaps] == ["0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0"]
    meta_file = paths.plugin_backup_dir("plugin-git") / "snapshots.json"
    assert stat.S_IMODE(meta_file.stat().st_mode) == 0o600

    # Rollback without --snapshot restores latest snapshot (0.6.0)
    rb_latest = installer.rollback("plugin-git")
    assert rb_latest.ok is True
    assert rb_latest.version == "0.6.0"
    assert rb_latest.content_hash == hashes["0.6.0"]
    assert json.loads((paths.plugins_dir / "plugin-git" / "plugin.json").read_text(encoding="utf-8"))["version"] == "0.6.0"

    lock_after_rb = installer.read_upstreams_lock()["locks"]["plugin-git"]
    assert lock_after_rb["commit"] == commits["0.6.0"]
    assert lock_after_rb["content_hash"] == hashes["0.6.0"]

    # Rollback to explicit snapshot (0.3.0)
    target_snap = next(s for s in installer.list_snapshots("plugin-git") if s.version == "0.3.0")
    rb_exact = installer.rollback("plugin-git", snapshot=target_snap.snapshot_id)
    assert rb_exact.ok is True
    assert rb_exact.version == "0.3.0"
    assert rb_exact.content_hash == hashes["0.3.0"]
    assert [s.version for s in installer.list_snapshots("plugin-git")] == ["0.2.0", "0.4.0", "0.5.0"]
    assert list(paths.staging_dir.iterdir()) == []


def test_verification_failure_saves_diagnostic_cleans_staging_and_preserves_active(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    installer = PluginInstaller(paths=paths)

    # 1. Invalid schema_version=1
    bad_schema = _write_plugin_tree(tmp_path / "bad-schema", plugin_id="plugin-bad-schema", schema_version=1)
    with pytest.raises(PluginManifestError) as exc1:
        installer.install(bad_schema)
    assert exc1.value.code == ErrorCode.VALIDATION_ERROR
    diag1 = Path(exc1.value.details["diagnostic_path"])
    assert diag1.is_file()
    assert stat.S_IMODE(diag1.stat().st_mode) == 0o600
    assert not (paths.plugins_dir / "plugin-bad-schema").exists()
    assert list(paths.staging_dir.iterdir()) == []

    # 2. Failing pytest gate + secret redaction in diagnostic
    from omnidesk_core.vault import SecretVault

    SecretVault(paths=paths).set("plugin-failing-tests", "api_token", "super-secret-xyz-999")
    bad_tests = _write_plugin_tree(
        tmp_path / "bad-tests",
        plugin_id="plugin-failing-tests",
        test_body='def test_fail():\n    raise AssertionError("leaked super-secret-xyz-999")\n',
        extra_manifest={"verify_cmd": "uv run pytest -q -W ignore::DeprecationWarning"},
    )
    with pytest.raises(ValidationError) as exc2:
        installer.install(bad_tests)
    assert exc2.value.code == ErrorCode.VALIDATION_ERROR
    diag2_text = Path(exc2.value.details["diagnostic_path"]).read_text(encoding="utf-8")
    assert "super-secret-xyz-999" not in diag2_text
    assert "[REDACTED]" in diag2_text
    assert list(paths.staging_dir.iterdir()) == []

    # 3. Failing lifecycle on_load also scrubs secrets from diagnostic
    SecretVault(paths=paths).set("plugin-bad-lc", "api_token", "lc-secret-token-777")
    bad_lc = _write_plugin_tree(
        tmp_path / "bad-lc",
        plugin_id="plugin-bad-lc",
        on_load_body='        raise RuntimeError("boom on_load lc-secret-token-777")\n',
    )
    with pytest.raises(PluginFaultError) as exc3:
        installer.install(bad_lc)
    assert exc3.value.code == ErrorCode.PLUGIN_FAULT
    diag3_text = Path(exc3.value.details["diagnostic_path"]).read_text(encoding="utf-8")
    assert "lc-secret-token-777" not in diag3_text
    assert "[REDACTED]" in diag3_text
    assert list(paths.staging_dir.iterdir()) == []

    # 4. Update failure leaves active plugin intact and creates no stray snapshots
    repo = tmp_path / "update-fail-repo"
    _init_git_plugin_repo(repo, plugin_id="plugin-keep-good", version="0.1.0")
    installer.install(f"file://{repo}", ref="main")
    _write_plugin_tree(
        repo,
        plugin_id="plugin-keep-good",
        version="0.2.0",
        test_body="def test_broken():\n    assert False\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "broken 0.2.0")
    with pytest.raises(ValidationError):
        installer.update("plugin-keep-good")
    active_mf = json.loads((paths.plugins_dir / "plugin-keep-good" / "plugin.json").read_text(encoding="utf-8"))
    assert active_mf["version"] == "0.1.0"
    assert installer.list_snapshots("plugin-keep-good") == []
    assert list(paths.staging_dir.iterdir()) == []


def test_update_rolls_back_active_dir_if_lockfile_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _make_paths(tmp_path)
    repo = tmp_path / "atom-repo"
    _init_git_plugin_repo(repo, plugin_id="plugin-atomic", version="0.1.0")
    installer = PluginInstaller(paths=paths)
    res1 = installer.install(f"file://{repo}", ref="main")

    _write_plugin_tree(repo, plugin_id="plugin-atomic", version="0.2.0")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v0.2.0")

    def _boom(_data: Any) -> None:
        raise OSError("simulated disk full during lock write")

    monkeypatch.setattr(installer, "_write_upstreams_lock", _boom)
    with pytest.raises(OmniDeskError):
        installer.update("plugin-atomic")

    active_mf = json.loads((paths.plugins_dir / "plugin-atomic" / "plugin.json").read_text(encoding="utf-8"))
    assert active_mf["version"] == "0.1.0"
    assert compute_tree_hash(paths.plugins_dir / "plugin-atomic") == res1.content_hash
    assert installer.list_snapshots("plugin-atomic") == []


def test_rejects_duplicate_id_path_traversal_and_symlinks(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    installer = PluginInstaller(paths=paths)

    src1 = _write_plugin_tree(tmp_path / "src1", plugin_id="plugin-dup", version="0.1.0")
    installer.install(src1)

    # Duplicate ID via manifest or --id or mismatched folder name
    src2 = _write_plugin_tree(tmp_path / "src2", plugin_id="plugin-dup", version="0.2.0")
    with pytest.raises(OmniDeskError) as dup_exc:
        installer.install(src2)
    assert dup_exc.value.code == ErrorCode.CONFLICT

    other_dir = _write_plugin_tree(paths.plugins_dir / "renamed-folder", plugin_id="plugin-alias", version="0.1.0")
    assert other_dir.is_dir()
    src_alias = _write_plugin_tree(tmp_path / "src-alias", plugin_id="plugin-alias", version="0.2.0")
    with pytest.raises(OmniDeskError) as alias_exc:
        installer.install(src_alias)
    assert alias_exc.value.code == ErrorCode.CONFLICT

    # Path traversal in --id, plugin.json id, entry, snapshot, and staging env
    with pytest.raises(PathSafetyError):
        installer.install(src2, plugin_id="../escape")
    bad_mf_id = _write_plugin_tree(tmp_path / "bad-mf-id", plugin_id="../escape")
    with pytest.raises(PluginManifestError):
        installer.install(bad_mf_id)
    bad_entry = _write_plugin_tree(tmp_path / "bad-entry", plugin_id="plugin-bad-entry", extra_manifest={"entry": "../../evil:run"})
    with pytest.raises(PluginManifestError):
        installer.install(bad_entry)
    with pytest.raises(PathSafetyError):
        installer.rollback("plugin-dup", snapshot="../../escape")
    with pytest.raises(PathSafetyError):
        OmnideskPaths.from_env({"OMNIDESK_STAGING_DIR": "../relative"})

    # Symlink source dir & symlink inside source tree & symlinked lockfile
    sym_src = tmp_path / "sym-src"
    sym_src.symlink_to(src2)
    with pytest.raises(PathSafetyError):
        installer.install(sym_src)

    sneaky_src = _write_plugin_tree(tmp_path / "sneaky-src", plugin_id="plugin-sneaky")
    outside = tmp_path / "secret.txt"
    outside.write_text("top-secret", encoding="utf-8")
    (sneaky_src / "leak.txt").symlink_to(outside)
    with pytest.raises(PathSafetyError):
        installer.install(sneaky_src)

    # Git repo containing committed symlink must also be rejected
    git_sym_repo = tmp_path / "git-sym"
    _write_plugin_tree(git_sym_repo, plugin_id="plugin-git-sym")
    subprocess.run(["git", "init", "-b", "main", str(git_sym_repo)], capture_output=True, check=True)
    _git(git_sym_repo, "config", "user.email", "t@omnidesk.local")
    _git(git_sym_repo, "config", "user.name", "T")
    (git_sym_repo / "escape.txt").symlink_to("/etc/passwd")
    _git(git_sym_repo, "add", ".")
    _git(git_sym_repo, "commit", "-m", "add symlink")
    with pytest.raises(PathSafetyError):
        installer.install(f"file://{git_sym_repo}")
    assert list(paths.staging_dir.iterdir()) == []

    # Symlinked upstreams.lock.json / .installer.lock / backups_dir are rejected
    sym_target = tmp_path / "outside-lock.json"
    sym_target.write_text("{}", encoding="utf-8")
    paths.upstreams_lock_path.unlink()
    paths.upstreams_lock_path.symlink_to(sym_target)
    with pytest.raises(PathSafetyError):
        installer.read_upstreams_lock()
    paths.upstreams_lock_path.unlink()

    sym_paths = _make_paths(tmp_path / "sym-env")
    sym_paths.data_dir.mkdir(parents=True, exist_ok=True)
    sym_paths.install_lock_path.symlink_to(sym_target)
    with pytest.raises(PathSafetyError):
        PluginInstaller(paths=sym_paths).install(src2, run_tests=False)


def test_concurrent_install_file_lock_serializes_and_detects_busy_lock(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    src_a = _write_plugin_tree(tmp_path / "src-a", plugin_id="plugin-conc-a")
    src_b = _write_plugin_tree(tmp_path / "src-b", plugin_id="plugin-conc-b")

    # 1. External process holding .installer.lock causes timeout CONFLICT
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            os.sys.executable,
            "-c",
            (
                "import fcntl, sys, time; "
                "fd = open(sys.argv[1], 'a+'); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "time.sleep(2.0)"
            ),
            str(paths.install_lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        installer = PluginInstaller(paths=paths, lock_timeout=0.15)
        with pytest.raises(OmniDeskError) as busy_exc:
            installer.install(src_a, run_tests=False)
        assert busy_exc.value.code == ErrorCode.CONFLICT
    finally:
        holder.terminate()
        holder.wait(timeout=5.0)

    # 2. Two concurrent threads installing distinct plugins serialize safely via flock without losing lock entries
    results: list[tuple[str, Any]] = []
    barrier = threading.Barrier(2)

    def _worker(src: Path, name: str) -> None:
        inst = PluginInstaller(paths=paths, lock_timeout=10.0)
        barrier.wait(timeout=5.0)
        try:
            r = inst.install(src, run_tests=False)
            results.append((name, r.plugin_id))
        except BaseException as exc:  # pragma: no cover
            results.append((name, exc))

    t1 = threading.Thread(target=_worker, args=(src_a, "a"))
    t2 = threading.Thread(target=_worker, args=(src_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)

    assert sorted(results) == [("a", "plugin-conc-a"), ("b", "plugin-conc-b")]
    locks = PluginInstaller(paths=paths).read_upstreams_lock()["locks"]
    assert set(locks) == {"plugin-conc-a", "plugin-conc-b"}

    # 3. Two concurrent threads installing the same plugin ID: one succeeds, one fails with CONFLICT
    dup_src_1 = _write_plugin_tree(tmp_path / "dup-1", plugin_id="plugin-race")
    dup_src_2 = _write_plugin_tree(tmp_path / "dup-2", plugin_id="plugin-race")
    race_outcomes: list[Any] = []
    race_barrier = threading.Barrier(2)

    def _race(src: Path) -> None:
        inst = PluginInstaller(paths=paths, lock_timeout=10.0)
        race_barrier.wait(timeout=5.0)
        try:
            race_outcomes.append(("ok", inst.install(src, run_tests=False).plugin_id))
        except OmniDeskError as exc:
            race_outcomes.append(("err", exc.code))

    r1 = threading.Thread(target=_race, args=(dup_src_1,))
    r2 = threading.Thread(target=_race, args=(dup_src_2,))
    r1.start()
    r2.start()
    r1.join(timeout=15.0)
    r2.join(timeout=15.0)

    assert sorted(race_outcomes) == [("err", ErrorCode.CONFLICT), ("ok", "plugin-race")]
