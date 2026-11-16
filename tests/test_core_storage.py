"""Tests for OmniDesk v2 core storage: ConfigStore, SecretVault, and SearchIndex."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[1] / "core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from omnidesk_core.bus import EntityBus  # noqa: E402
from omnidesk_core.config import ConfigError as StoreConfigError  # noqa: E402
from omnidesk_core.config import ConfigStore, ensure_private_dir, resolve_xdg_app_dir, split_secrets, strip_secrets  # noqa: E402
from omnidesk_core.errors import ErrorCode, InvalidArgumentError, OmniDeskError, PathSafetyError, SecretVaultError, redact_secrets  # noqa: E402
from omnidesk_core.paths import OmnideskPaths, validate_plugin_id  # noqa: E402
from omnidesk_core.search import SearchDocument, SearchIndex  # noqa: E402
from omnidesk_core.vault import SecretRedactingFilter, SecretVault  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_xdg_paths_and_traversal_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_home = tmp_path / "xdg-cfg"
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    paths = OmnideskPaths.from_env()
    assert paths.vault_path == cfg_home / "omnidesk" / "vault.json"
    assert paths.search_db_path == data_home / "omnidesk" / "search.db"

    vault = SecretVault()
    assert vault.path == paths.vault_path
    with SearchIndex() as idx:
        assert idx.db_path == paths.search_db_path

    store = ConfigStore()
    assert store.config_dir == cfg_home / "omnidesk"

    for bad_id in ("", "../escape", "bad/id", "bad\\id", "x\x00y", ".hidden"):
        with pytest.raises(PathSafetyError):
            vault.set(bad_id, "api_key", "secret-val")
        with pytest.raises(StoreConfigError):
            store.write(bad_id, {"k": "v"})


def test_config_and_vault_enforce_0600_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    store = ConfigStore()
    store.write("plugin-demo", {"host": "127.0.0.1", "port": 3425})
    cfg_file = store.path_for("plugin-demo")
    assert _mode(store.config_dir) == 0o700
    assert _mode(store.plugins_dir) == 0o700
    assert _mode(cfg_file) == 0o600

    vault = SecretVault()
    vault.set("plugin-demo", "api_token", "tok-super-secret-001")
    assert _mode(vault.path.parent) == 0o700
    assert _mode(vault.path) == 0o600

    with SearchIndex() as idx:
        idx.upsert("doc-1", "Title", "Body")
        assert _mode(Path(idx.db_path).parent) == 0o700
        assert _mode(Path(idx.db_path)) == 0o600


def test_vault_crud_plugin_isolation_and_flat_keys(tmp_path: Path) -> None:
    vault_path = tmp_path / "cfg" / "vault.json"
    vault = SecretVault(vault_path)

    assert vault.list_keys() == []
    assert vault.list_keys("plugin-a") == []
    assert vault.get("plugin-a", "cookie_header") is None
    assert vault.get("plugin-a", "cookie_header", default="fallback") == "fallback"

    vault.set("plugin-a", "cookie_header", "cookie-A-12345")
    vault.set("plugin-b", "cookie_header", "cookie-B-67890")
    vault.set("plugin-b", "api_token", "token-B-999")
    vault.set("global_gateway", "gw-secret-val")

    assert vault.get("plugin-a", "cookie_header") == "cookie-A-12345"
    assert vault.get("plugin-a:cookie_header") == "cookie-A-12345"
    assert vault.get("plugin-b", "cookie_header") == "cookie-B-67890"
    assert vault.get("global_gateway") == "gw-secret-val"
    assert vault.has("plugin-b", "api_token") is True
    assert vault.list_keys("plugin-a") == ["cookie_header"]
    assert vault.list_keys("plugin-b") == ["api_token", "cookie_header"]
    assert vault.list_plugins() == ["plugin-a", "plugin-b"]
    assert vault.list_keys() == [
        "global_gateway",
        "plugin-a:cookie_header",
        "plugin-b:api_token",
        "plugin-b:cookie_header",
    ]

    assert vault.delete("plugin-a", "cookie_header") is True
    assert vault.delete("plugin-a", "cookie_header") is False
    assert vault.get("plugin-a", "cookie_header") is None
    assert vault.get("plugin-b", "cookie_header") == "cookie-B-67890"

    assert vault.delete_plugin("plugin-b") == 2
    assert vault.delete_plugin("plugin-b") == 0
    assert vault.list_keys("plugin-b") == []
    assert vault.get("global_gateway") == "gw-secret-val"

    stored = vault.set_many("plugin-c", {"k2": "v2", "k1": "v1"})
    assert stored == ["k1", "k2"]
    assert vault.get("plugin-c", "k1") == "v1"
    assert vault.get("plugin-c", "k2") == "v2"


def test_vault_security_permissions_symlinks_and_corrupt_json(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.json"
    vault = SecretVault(vault_path)
    vault.set("plugin-sec", "token", "secret-top-xyz")

    # Non-strict mode automatically tightens 0644 -> 0600 on read.
    os.chmod(vault_path, 0o644)
    assert vault.get("plugin-sec", "token") == "secret-top-xyz"
    assert _mode(vault_path) == 0o600

    # Strict mode rejects permissive 0644 files with PERMISSION_DENIED.
    os.chmod(vault_path, 0o644)
    strict_vault = SecretVault(vault_path, strict_permissions=True)
    with pytest.raises(SecretVaultError) as exc_info:
        strict_vault.get("plugin-sec", "token")
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    # Symlinked vault path is always refused.
    real_target = tmp_path / "real.json"
    real_target.write_text('{"version": 1, "secrets": {}}', encoding="utf-8")
    link_path = tmp_path / "link_vault.json"
    link_path.symlink_to(real_target)
    sym_vault = SecretVault(link_path)
    with pytest.raises(SecretVaultError) as sym_exc:
        sym_vault.get("plugin-sec", "token")
    assert sym_exc.value.code == ErrorCode.PATH_SAFETY

    # Corrupted JSON never leaks raw vault contents in error text.
    bad_path = tmp_path / "corrupt_vault.json"
    bad_path.write_text('{"secrets": {"p": {"token": "SUPER_SECRET_LEAK"', encoding="utf-8")
    os.chmod(bad_path, 0o600)
    corrupt_vault = SecretVault(bad_path)
    with pytest.raises(SecretVaultError) as corrupt_exc:
        corrupt_vault.get("p", "token")
    assert "SUPER_SECRET_LEAK" not in str(corrupt_exc.value)


def test_secret_isolation_between_config_vault_logs_and_bus(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cfg_store = ConfigStore(tmp_path / "cfg")
    vault = SecretVault(tmp_path / "cfg" / "vault.json")

    schema = [
        {"key": "mount_root", "type": "string", "default": "/mnt/nas"},
        {"key": "nas_pin", "type": "secret"},
    ]
    payload = {
        "mount_root": "/mnt/feiniu-smb",
        "nas_pin": "pin-987654321",
        "api_token": "tok-isolated-secret-value",
        "nested": {"cookie_header": "sid=secret-cookie-abc", "region": "cn"},
    }

    plain, extracted = split_secrets(payload, config_schema=schema)
    assert extracted == {"nas_pin": "pin-987654321", "api_token": "tok-isolated-secret-value"}
    cfg_store.register_schema("plugin-fnos-nas", schema)
    written = cfg_store.write("plugin-fnos-nas", payload, config_schema=schema)
    vault.set_many("plugin-fnos-nas", extracted)

    raw_cfg_text = cfg_store.path_for("plugin-fnos-nas").read_text(encoding="utf-8")
    for secret in ("pin-987654321", "tok-isolated-secret-value", "secret-cookie-abc"):
        assert secret not in raw_cfg_text
        assert secret not in json.dumps(written)
        assert secret not in json.dumps(cfg_store.read("plugin-fnos-nas"))
        assert secret not in json.dumps(plain)

    assert cfg_store.read("plugin-fnos-nas") == {"mount_root": "/mnt/feiniu-smb", "nested": {"region": "cn"}}
    assert vault.get("plugin-fnos-nas", "nas_pin") == "pin-987654321"
    assert vault.get("plugin-fnos-nas", "api_token") == "tok-isolated-secret-value"

    # Vault operations, repr, and summary must never leak secrets to logs.
    _ = repr(vault)
    _ = vault.to_safe_summary()
    _ = vault.list_keys()
    _ = vault.get("plugin-fnos-nas", "api_token")
    with pytest.raises(InvalidArgumentError):
        vault.set("plugin-fnos-nas", "bad", None)
    for secret in ("pin-987654321", "tok-isolated-secret-value"):
        assert secret not in caplog.text
        assert secret not in repr(vault)
        assert secret not in json.dumps(vault.to_safe_summary())

    # Logging filter + EntityBus integration redact vaulted secrets.
    filtered_logger = logging.getLogger("omnidesk.test.redaction")
    filt = SecretRedactingFilter(vault)
    filtered_logger.addFilter(filt)
    try:
        filtered_logger.warning("auth failed with Bearer %s and pin %s", "tok-isolated-secret-value", "pin-987654321")
    finally:
        filtered_logger.removeFilter(filt)
    assert "tok-isolated-secret-value" not in caplog.text
    assert "pin-987654321" not in caplog.text
    assert "[REDACTED]" in caplog.text

    bus = EntityBus(secret_provider=vault.secret_values)
    faults: list[dict] = []

    def bad_handler(_evt) -> None:
        raise RuntimeError("upstream failed token=tok-isolated-secret-value pin=pin-987654321")

    bus.subscribe("entity.test", bad_handler, plugin_id="plugin-fnos-nas")
    bus.subscribe("plugin.fault", lambda evt: faults.append(evt.payload), plugin_id="core")
    asyncio.run(bus.publish("entity.test", {"ok": True}))
    assert len(faults) == 1
    assert "tok-isolated-secret-value" not in faults[0]["error"]
    assert "pin-987654321" not in faults[0]["error"]
    assert "[REDACTED]" in faults[0]["error"]


def test_search_index_fts5_crud_ranking_and_metadata_stripping(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "search.db"
    with SearchIndex(db_path) as idx:
        assert idx.fts_enabled is True
        idx.upsert(
            "doc-body",
            title="Weekly Notes",
            body="Exploring SQLite FTS5 indexing performance in OmniDesk.",
            domain="text",
            source_id="plugin-blog",
            kind="article",
            tags=["notes"],
            metadata={"url": "https://example.com/1", "api_key": "sk-must-be-stripped", "cookie_header": "secret"},
            updated_at=100.0,
        )
        idx.upsert(
            SearchDocument(
                doc_id="doc-title",
                title="SQLite Architecture Deep Dive",
                body="Overview of local storage.",
                domain="text",
                source_id="plugin-journals",
                kind="paper",
                tags=("sqlite", "fts5"),
                metadata={"doi": "10.1038/s41586-024"},
                updated_at=200.0,
            )
        )
        idx.upsert({"id": "doc-media", "title": "Rust Compiler Talk", "body": "Systems language", "domain": "media", "source_id": "plugin-youtube"})

        # Secret keys in metadata must never be stored in search.db.
        stored_body = idx.get("doc-body")
        assert stored_body is not None
        assert stored_body.metadata == {"url": "https://example.com/1"}

        # FTS5 ranking: title match ranks above body match.
        hits = idx.search("SQLite")
        assert idx.last_backend == "fts5"
        assert [h.doc_id for h in hits] == ["doc-title", "doc-body"]
        assert all(h.backend == "fts5" for h in hits)
        assert hits[0].score >= hits[1].score > 0
        assert "<mark>" in hits[0].snippet

        # Prefix search, filters, and special FTS syntax characters never fail.
        assert [h.doc_id for h in idx.search("arch*")] == ["doc-title"]
        assert [h.doc_id for h in idx.search("SQLite", source_id="plugin-blog")] == ["doc-body"]
        assert idx.search("To Be OR Not") == []
        assert idx.search("10.1038/s41586-024") == []  # indexed in metadata, not body, and doesn't crash

        # Word-token semantics in FTS5 ("rust" matches "Rust", not "trust").
        idx.upsert("doc-trust", "We trust this design", "Reliable storage")
        assert [h.doc_id for h in idx.search("rust")] == ["doc-media"]

        # Upsert replaces old FTS5 tokens cleanly.
        idx.upsert("doc-body", title="Renamed Entry", body="Completely new topic on Wayland", domain="text", source_id="plugin-blog")
        assert [h.doc_id for h in idx.search("SQLite")] == ["doc-title"]
        assert [h.doc_id for h in idx.search("Wayland")] == ["doc-body"]

        # Delete and delete_by_source synchronize FTS5 index.
        assert idx.delete("doc-title") is True
        assert idx.delete("doc-title") is False
        assert idx.search("SQLite") == []
        assert idx.delete_by_source("plugin-blog") == 1
        assert idx.search("Wayland") == []
        assert idx.count() == 2


def test_search_index_like_fallback_cjk_and_authorizer_degradation(tmp_path: Path) -> None:
    # 1. Explicit LIKE fallback mode + wildcard escaping + rebuild on FTS reopen.
    db_path = tmp_path / "fallback.db"
    with SearchIndex(db_path, enable_fts=False) as idx:
        assert idx.fts_enabled is False
        idx.upsert_many(
            [
                {"doc_id": "zh-1", "title": "飞牛NAS原盘影库", "body": "支持100%无损双语字幕", "domain": "media", "source_id": "plugin-fnos-nas"},
                {"doc_id": "zh-2", "title": "万象桌面阅读器", "body": "EPUB与PDF双栏对照", "domain": "book", "source_id": "plugin-ebook"},
            ]
        )
        hits = idx.search("100%")
        assert idx.last_backend == "like"
        assert [h.doc_id for h in hits] == ["zh-1"]
        assert "<mark>100%</mark>" in hits[0].snippet

        cjk_hits = idx.search("万象")
        assert [h.doc_id for h in cjk_hits] == ["zh-2"]

    # Reopening with FTS5 enabled auto-rebuilds FTS index and supports both FTS5 and CJK LIKE fallback.
    with SearchIndex(db_path, enable_fts=True) as idx:
        assert idx.fts_enabled is True
        assert [h.doc_id for h in idx.search("EPUB")] == ["zh-2"]
        assert idx.last_backend == "fts5"
        assert [h.doc_id for h in idx.search("原盘")] == ["zh-1"]
        assert idx.last_backend == "like"
        # Explicit use_fts=False forces LIKE query on FTS-enabled index.
        assert [h.doc_id for h in idx.search("EPUB", use_fts=False)] == ["zh-2"]
        assert idx.last_backend == "like"

    # 2. Simulated missing/blocked FTS5 via SQLite authorizer + mid-session table drop.
    orig_connect = sqlite3.connect

    def connect_no_fts(*args, **kwargs):
        conn = orig_connect(*args, **kwargs)
        conn.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_VTABLE else sqlite3.SQLITE_OK
        )
        return conn

    sqlite_patch = pytest.MonkeyPatch()
    sqlite_patch.setattr(sqlite3, "connect", connect_no_fts)
    try:
        with SearchIndex(tmp_path / "no_fts.db") as no_fts_idx:
            assert no_fts_idx.fts_enabled is False
            no_fts_idx.upsert("d1", "Offline Paper", "Graceful degradation works")
            res = no_fts_idx.search("degradation")
            assert no_fts_idx.last_backend == "like"
            assert [h.doc_id for h in res] == ["d1"]
    finally:
        sqlite_patch.undo()

    # Mid-session FTS failure degrades cleanly to LIKE.
    with SearchIndex(":memory:") as mem_idx:
        mem_idx.upsert("m1", "Memory Doc", "Fallback resilience")
        mem_idx._conn.execute("DROP TABLE documents_fts")
        res = mem_idx.search("resilience")
        assert mem_idx.last_backend == "like"
        assert [h.doc_id for h in res] == ["m1"]


def test_search_index_pagination_match_any_clear_and_safety(tmp_path: Path) -> None:
    db_file = tmp_path / "db" / "search.db"
    with SearchIndex(db_file) as idx:
        idx.upsert_many(
            [
                {"doc_id": "d1", "title": "Alpha Guide", "body": "python", "domain": "text", "updated_at": 10.0},
                {"doc_id": "d2", "title": "Beta Guide", "body": "rust", "domain": "text", "updated_at": 20.0},
                {"doc_id": "d3", "title": "Gamma Guide", "body": "typescript", "domain": "book", "updated_at": 30.0},
            ]
        )
        assert idx.count(domain="text") == 2
        listed = idx.list_documents(limit=2, offset=0)
        assert [d.doc_id for d in listed] == ["d3", "d2"]
        assert [h.doc_id for h in idx.search("python rust", match_any=True)] == ["d2", "d1"]
        assert [h.doc_id for h in idx.search("Guide", limit=1, offset=1)] == ["d2"]
        assert idx.clear() == 3
        assert idx.count() == 0
        assert idx.search("Guide") == []

    with pytest.raises(Exception):
        idx.count()

    real_db = tmp_path / "real.db"
    real_db.write_bytes(b"")
    sym_db = tmp_path / "sym.db"
    sym_db.symlink_to(real_db)
    with pytest.raises(PathSafetyError):
        SearchIndex(sym_db)


def test_plugin_id_newline_and_path_symlink_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Trailing newline must not slip past regex `$` anchors.
    with pytest.raises(PathSafetyError):
        validate_plugin_id("plugin-a\n")
    store = ConfigStore(tmp_path / "cfg")
    with pytest.raises(StoreConfigError):
        store.write("plugin-a\n", {"ok": True})

    # Relative XDG paths are ignored per XDG Base Directory specification.
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/cfg")
    assert resolve_xdg_app_dir("XDG_CONFIG_HOME", ".config") == home_dir / ".config" / "omnidesk"
    assert OmnideskPaths.from_env({"HOME": str(home_dir), "XDG_CONFIG_HOME": "relative/cfg"}).config_dir == home_dir / ".config" / "omnidesk"
    with pytest.raises(PathSafetyError):
        OmnideskPaths.from_env({"HOME": str(home_dir)}, app_name="../escape")

    # Symlinked private directories and targets are rejected across Paths, ConfigStore, SecretVault, and SearchIndex.
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    sym_dir = tmp_path / "sym_dir"
    sym_dir.symlink_to(real_dir)

    bad_paths = OmnideskPaths.from_env({"XDG_CONFIG_HOME": str(tmp_path / "xdg-cfg"), "XDG_DATA_HOME": str(tmp_path / "xdg-data")})
    bad_paths.config_dir.parent.mkdir(parents=True, exist_ok=True)
    bad_paths.config_dir.symlink_to(real_dir)
    with pytest.raises(PathSafetyError):
        bad_paths.ensure_dirs()

    with pytest.raises(OSError):
        ensure_private_dir(sym_dir)

    owned_dir = tmp_path / "foreign_owned"
    owned_dir.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(PermissionError):
        ensure_private_dir(owned_dir)
    monkeypatch.undo()

    # ConfigStore rejects symlinked plugins dir and symlinked config files on read & write.
    cfg_root = tmp_path / "cfg_sym"
    cfg_root.mkdir()
    (cfg_root / "plugins").symlink_to(real_dir)
    sym_store = ConfigStore(cfg_root)
    with pytest.raises(StoreConfigError):
        sym_store.write("plugin-a", {"k": "v"})
    with pytest.raises(StoreConfigError):
        sym_store.read("plugin-a")

    normal_store = ConfigStore(tmp_path / "cfg_file_sym")
    normal_store.write("plugin-a", {"k": "v"})
    target_json = normal_store.path_for("plugin-a")
    target_json.unlink()
    outside_json = tmp_path / "outside.json"
    outside_json.write_text('{"k": "evil"}', encoding="utf-8")
    target_json.symlink_to(outside_json)
    with pytest.raises(StoreConfigError):
        normal_store.read("plugin-a")
    with pytest.raises(StoreConfigError):
        normal_store.write("plugin-a", {"k": "overwrite"})

    # SecretVault rejects symlinked parent directory and symlinked dir passed as vault_path.
    with pytest.raises(SecretVaultError) as dir_sym_exc:
        SecretVault(sym_dir).set("plugin-a", "tok", "val")
    assert dir_sym_exc.value.code == ErrorCode.PATH_SAFETY
    with pytest.raises(SecretVaultError) as parent_sym_exc:
        SecretVault(sym_dir / "vault.json").get("plugin-a", "tok")
    assert parent_sym_exc.value.code == ErrorCode.PATH_SAFETY

    # SearchIndex rejects symlinked parent directory and symlinked WAL sidecar.
    with pytest.raises(PathSafetyError):
        SearchIndex(sym_dir / "search.db")
    wal_db = tmp_path / "wal_guard" / "search.db"
    wal_db.parent.mkdir()
    Path(f"{wal_db}-wal").symlink_to(tmp_path / "outside.wal")
    with pytest.raises(PathSafetyError):
        SearchIndex(wal_db)


def test_config_secret_stripping_tuples_case_variants_and_generators(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "cfg")
    schema_gen = (f for f in [{"key": "host", "type": "string", "default": "localhost"}, {"key": "custom_key", "type": "secret"}])
    store.register_schema("plugin-gen", schema_gen)
    written = store.write(
        "plugin-gen",
        {
            "custom_key": "sec-1",
            "API_KEY": "sec-2",
            "nested": ({"Password": "sec-3", "label": "ok"},),
        },
    )
    assert written == {"nested": ({"label": "ok"},)}
    read_back = store.read(
        "plugin-gen",
        config_schema=(f for f in [{"key": "host", "type": "string", "default": "localhost"}]),
        apply_defaults=True,
    )
    assert read_back == {"host": "localhost", "nested": [{"label": "ok"}]}

    # Promoting a previously plain field to secret must strip it, isolated per plugin.
    store.register_schema("plugin-a", [{"key": "api_key", "type": "string"}])
    assert store.write("plugin-a", {"api_key": "allowed-public"}) == {"api_key": "allowed-public"}
    assert store.write("plugin-b", {"api_key": "must-strip-for-b"}) == {}
    store.register_schema("plugin-a", [{"key": "api_key", "type": "secret"}])
    assert store.write("plugin-a", {"api_key": "now-secret"}) == {}

    plain, extracted = split_secrets({"API_TOKEN": "tok-upper", "items": ({"Auth_Token": "nested-tok", "n": 1},)})
    assert extracted == {"API_TOKEN": "tok-upper"}
    assert plain == {"items": ({"n": 1},)}
    assert strip_secrets({"tuple_box": ({"cookie_header": "c=1", "ok": True},)}) == {"tuple_box": ({"ok": True},)}

    with SearchIndex(":memory:") as idx:
        idx.upsert("d1", "Title", "Body", metadata={"items": ({"API_KEY": "leak", "safe": 42},)})
        stored = idx.get("d1")
        assert stored is not None
        assert stored.metadata == {"items": [{"safe": 42}]}


def test_redaction_edge_cases_and_logging_filter(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Unsorted prefix secrets must redact longest match first.
    assert redact_secrets("token=abcd1234", ["abcd", "abcd1234"]) == "token=[REDACTED]"
    # Compound keys, quoted values, JSON pairs, Basic auth, and modern token prefixes.
    msg = (
        'api_token=tok1234 access_token="my secret" cookie_header=\'a=1; b=2\' '
        '{"client_secret": "cs-val", "host": "127.0.0.1"} Authorization: Basic dXNlcjpwYXNz '
        "ghp_1234567890abcdef github_pat_1234567890abcdef"
    )
    scrubbed = redact_secrets(msg)
    for leaked in ("tok1234", "my secret", "a=1; b=2", "cs-val", "dXNlcjpwYXNz", "ghp_1234567890abcdef", "github_pat_1234567890abcdef"):
        assert leaked not in scrubbed
    assert '"host": "127.0.0.1"' in scrubbed

    err = OmniDeskError("failed", details={"nested": {"api_key": "secret-val"}})
    assert "secret-val" not in json.dumps(err.to_dict())

    # Logging filter handles format placeholders (`api_key=%s`), Exception messages, tracebacks, and corrupt vaults.
    vault = SecretVault(tmp_path / "vault.json")
    vault.set("plugin-a", "api_token", "vault-secret-value-777")
    log = logging.getLogger("omnidesk.test.redaction.edge")
    filt = SecretRedactingFilter(vault)
    log.addFilter(filt)
    caplog.set_level(logging.WARNING, logger="omnidesk.test.redaction.edge")
    try:
        log.warning("api_key=%s user=%s", "inline-raw-secret", "alice")
        log.error(RuntimeError("crash vault-secret-value-777"))
        try:
            raise ValueError("trace token=vault-secret-value-777")
        except ValueError:
            log.exception("handler failed")
    finally:
        log.removeFilter(filt)

    assert "inline-raw-secret" not in caplog.text
    assert "vault-secret-value-777" not in caplog.text
    assert "user=alice" in caplog.text

    vault.path.write_text("{broken", encoding="utf-8")
    log.addFilter(filt)
    try:
        log.warning("still logs safely api_token=raw99999")
    finally:
        log.removeFilter(filt)
    assert "raw99999" not in caplog.text


def test_search_cjk_recall_stale_fts_rebuild_unicode_like_and_isolation(tmp_path: Path) -> None:
    db_path = tmp_path / "search.db"
    # 1. CJK search must return both exact-token and compound-token matches without dropping partial matches.
    with SearchIndex(db_path) as idx:
        idx.upsert("cjk-exact", title="NAS原盘", body="资源", source_id="plugin-a", updated_at=100.0)
        idx.upsert("cjk-compound", title="飞牛NAS原盘影库", body="无损双语字幕", source_id="plugin-b", updated_at=200.0)
        idx.upsert("cjk-mixed", title="Python指南", body="CAFÉ教程", source_id="plugin-a", updated_at=300.0)

        hits = idx.search("原盘")
        assert idx.last_backend == "like"
        assert [h.doc_id for h in hits] == ["cjk-compound", "cjk-exact"]

        any_hits = idx.search("Python 原盘", match_any=True)
        assert {h.doc_id for h in any_hits} == {"cjk-mixed", "cjk-compound", "cjk-exact"}

        # Mixed CJK+ASCII doc still matches pure ASCII query via FTS5.
        assert [h.doc_id for h in idx.search("Python")] == ["cjk-mixed"]
        assert idx.last_backend == "fts5"

        # Unicode case-insensitive LIKE search + wildcard/FTS syntax safety.
        assert [h.doc_id for h in idx.search("café", use_fts=False)] == ["cjk-mixed"]
        assert idx.search("100%_\\*") == []
        assert idx.search('"quoted" OR AND NOT NEAR title:foo') == []

        # Cross-plugin isolation across search, count, list_documents, and conflict validation.
        assert idx.count(plugin_id="plugin-a") == 2
        assert [d.doc_id for d in idx.list_documents(plugin_id="plugin-a")] == ["cjk-mixed", "cjk-exact"]
        assert [h.doc_id for h in idx.search("原盘", plugin_id="plugin-a")] == ["cjk-exact"]
        with pytest.raises(InvalidArgumentError):
            idx.search("原盘", source_id="plugin-a", plugin_id="plugin-b")

    # 2. Mutating an existing document under enable_fts=False (keeping same row count) auto-rebuilds FTS5 on reopen.
    with SearchIndex(db_path, enable_fts=False) as idx:
        idx.upsert("cjk-mixed", title="Rust手册", body="Updated offline", source_id="plugin-a", updated_at=400.0)
    with SearchIndex(db_path, enable_fts=True) as idx:
        assert [h.doc_id for h in idx.search("Rust")] == ["cjk-mixed"]
        assert idx.last_backend == "fts5"
        assert idx.search("Python") == []

