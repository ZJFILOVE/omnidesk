"""Tests for the idempotent v1 -> v2 plugin configuration import."""

from __future__ import annotations

import json
from pathlib import Path

from omnidesk_core.config import ConfigStore
from omnidesk_core.legacy_config import import_v1_config
from omnidesk_core.paths import OmnideskPaths
from omnidesk_core.vault import SecretVault


def _paths(tmp_path: Path) -> OmnideskPaths:
    return OmnideskPaths.from_env({"HOME": str(tmp_path)}, home=tmp_path)


def test_import_maps_keys_composes_bilibili_cookie_and_is_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = ConfigStore(paths.config_dir)
    vault = SecretVault(paths=paths)

    store.write(
        "plugin-bilibili",
        {
            "default_quality": "0",
            "preferred_codec": "12",
            "web_dede_userid": "424242",
        },
        strip_known_secrets=False,
    )
    store.write(
        "plugin-ai-curator",
        {"auto_pull_remote": False, "auto_rehydrate_on_load": True},
        strip_known_secrets=False,
    )
    store.write(
        "plugin-annas-archive",
        {"download_dir": "/home/zhu/Books", "mirror": "https://annas-archive.org/path"},
        strip_known_secrets=False,
    )
    vault.set("plugin-bilibili", "web_sessdata", "sess-secret")
    vault.set("plugin-bilibili", "web_bili_jct", "jct-secret")

    report = import_v1_config(store, vault, paths=paths)
    assert report["changed_count"] == 3
    assert report["backup_dir"]

    bili = store.read("plugin-bilibili", strip_known_secrets=False)
    assert bili["quality"] == "auto"
    assert bili["preferred_codec"] == "hevc"
    assert vault.get("plugin-bilibili", "cookie_header") == (
        "SESSDATA=sess-secret; bili_jct=jct-secret; DedeUserID=424242"
    )
    assert "sess-secret" not in json.dumps(bili)

    curator = store.read("plugin-ai-curator", strip_known_secrets=False)
    assert curator["sync_remote"] is True

    annas = store.read("plugin-annas-archive", strip_known_secrets=False)
    assert annas["download_root"] == "/home/zhu/Books"
    assert annas["base_url"] == "https://annas-archive.org"

    second = import_v1_config(store, vault, paths=paths)
    assert second["changed_count"] == 0


def test_import_moves_zlib_credentials_to_vault(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = ConfigStore(paths.config_dir)
    vault = SecretVault(paths=paths)

    store.write(
        "plugin-zlib",
        {
            "domain": "https://z-lib.gd",
            "download_dir": "/home/zhu/Books",
            "remix_userid": "31916055",
        },
        strip_known_secrets=False,
    )
    vault.set("plugin-zlib", "remix_userkey", "remix-key-secret")
    vault.set("plugin-zlib", "cookie_header", "remix_userid=31916055; remix_userkey=abc")

    report = import_v1_config(store, vault, paths=paths)
    assert report["changed_count"] == 1

    cfg = store.read("plugin-zlib", strip_known_secrets=False)
    assert cfg["base_url"] == "https://z-lib.gd"
    assert cfg["download_root"] == "/home/zhu/Books"
    assert "remix_userid" not in cfg
    assert vault.get("plugin-zlib", "remix_userid") == "31916055"
    assert vault.get("plugin-zlib", "api_key") == "remix-key-secret"


def test_import_derives_pdf_and_asr_paths(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = ConfigStore(paths.config_dir)
    vault = SecretVault(paths=paths)

    store.write(
        "plugin-pdf-babeldoc",
        {
            "library_dirs": "/home/zhu/Books",
            "lang_out": "zh",
        },
        strip_known_secrets=False,
    )
    store.write(
        "plugin-asr-funasr",
        {
            "runtime_bin": "/opt/funasr",
            "model_path": "/models/funasr/sensevoice.gguf",
            "backend": "vulkan",
        },
        strip_known_secrets=False,
    )

    import_v1_config(store, vault, paths=paths)

    pdf = store.read("plugin-pdf-babeldoc", strip_known_secrets=False)
    assert pdf["pdf_library_roots"] == "/home/zhu/Books"
    assert pdf["target_language"] == "zh"
    assert pdf["output_root"].endswith("/plugin-data/plugin-pdf-babeldoc/output")

    asr = store.read("plugin-asr-funasr", strip_known_secrets=False)
    assert asr["funasr_bin"] == "/opt/funasr"
    assert asr["model_dir"] == "/models/funasr"
    assert asr["device"] == "vulkan"
