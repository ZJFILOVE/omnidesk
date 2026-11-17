"""Offline L1 mount, MediaSource, path containment, symlink, and atomic subtitle tests for plugin-fnos-nas."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "core"))

from omnidesk_sdk import BasePlugin, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_fnos_nas_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manifest_v2_schema_and_zero_hardcoded_secrets(tmp_path: Path) -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-fnos-nas"
    assert manifest["kind"] == "hybrid"
    assert manifest["domain"] == "media"
    assert manifest["entry"] == "plugin:create_plugin"
    assert set(manifest["provides"]) == {
        "nas.connection.v1",
        "media.source.v1",
        "media.local_match.v1",
        "media.clip_jump.v1",
        "book.sync.v1",
    }
    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest["provides"])

    fields = {f["key"]: f for f in manifest["config_schema"]}
    for key in ("mount_root", "media_service_url", "companion_sync_url", "radarr_url", "sonarr_url", "target_language"):
        assert key in fields
    for sec_key in ("media_service_token", "companion_sync_token", "radarr_api_key", "sonarr_api_key"):
        assert fields[sec_key]["type"] == "secret"
        assert fields[sec_key]["default"] == ""

    assert fields["mount_root"]["default"] == ""
    assert fields["media_service_url"]["default"] == ""
    assert fields["companion_sync_url"]["default"] == ""
    assert fields["radarr_url"]["default"] == ""
    assert fields["sonarr_url"]["default"] == ""


@pytest.mark.asyncio
async def test_unconfigured_degradation_and_unload_cleanup(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    assert isinstance(plugin, BasePlugin)

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["configured"] is False

    for action in ("status", "list_roots", "test_mount"):
        res = await ctx.capabilities.call("nas.connection.v1", {"action": action})
        assert res.ok is False
        assert res.data["error_code"] == "not_configured"

    src = ctx.domains.media_sources()[0]
    assert src.categories() == []
    assert await src.fetch_items() == []

    ms_res = await ctx.capabilities.call("media.source.v1", {"action": "fetch_items"})
    assert ms_res.ok is True
    assert ms_res.data["items"] == []
    assert ms_res.data["categories"] == []

    lm_res = await ctx.capabilities.call("media.local_match.v1", {"title": "Severance"})
    assert lm_res.ok is False
    assert lm_res.data["error_code"] == "not_configured"

    cj_res = await ctx.capabilities.call("media.clip_jump.v1", {"query": "ephemeral"})
    assert cj_res.ok is False
    assert cj_res.data["error_code"] == "not_configured"

    bs_res = await ctx.capabilities.call("book.sync.v1", {"action": "sync", "book_id": "b1", "current_page": 3})
    assert bs_res.ok is False
    assert bs_res.data["error_code"] == "not_configured"
    assert bs_res.data["local_saved"] is True
    assert bs_res.data["remote_synced"] is False

    await invoke_lifecycle_hook(plugin.on_unload)
    summary = await ctx.acleanup()
    assert summary["capabilities"] == 5
    assert summary["media_sources"] == 1
    assert not ctx.capabilities.has("nas.connection.v1")
    assert ctx.domains.media_sources() == []


@pytest.mark.asyncio
async def test_mount_validation_traversal_nonexistent_and_permissions(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    nas = tmp_path / "nas"
    nas.mkdir()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Traversal with '..' rejected even if path would resolve safely.
    trav = f"{nas}/../{nas.name}"
    ctx.set_config("mount_root", trav)
    res_trav = await ctx.capabilities.call("nas.connection.v1", {"action": "test_mount"})
    assert res_trav.ok is False
    assert res_trav.data["error_code"] == "path_safety"

    # Nonexistent mount_root returns empty MediaSource and not_found on test_mount.
    ctx.set_config("mount_root", str(tmp_path / "missing_mount"))
    res_miss = await ctx.capabilities.call("nas.connection.v1", {"action": "test_mount"})
    assert res_miss.ok is False
    assert res_miss.data["error_code"] == "not_found"
    src = ctx.domains.media_sources()[0]
    assert src.categories() == []
    assert await src.fetch_items() == []

    # File instead of directory rejected.
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("x", encoding="utf-8")
    ctx.set_config("mount_root", str(file_path))
    res_file = await ctx.capabilities.call("nas.connection.v1", {"action": "test_mount"})
    assert res_file.ok is False
    assert res_file.data["error_code"] == "invalid_argument"

    # Unreadable directory rejected (when non-root).
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        locked = tmp_path / "locked_nas"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            ctx.set_config("mount_root", str(locked))
            res_perm = await ctx.capabilities.call("nas.connection.v1", {"action": "test_mount"})
            assert res_perm.ok is False
            assert res_perm.data["error_code"] == "permission_denied"
        finally:
            locked.chmod(0o755)

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_media_source_real_scan_categories_filters_and_symlink_escape(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_mkv = outside / "Secret.2025.mkv"
    outside_mkv.write_bytes(b"SECRET")
    outside_dir = outside / "secret_dir"
    outside_dir.mkdir()
    (outside_dir / "Secret2.mkv").write_bytes(b"SECRET2")

    nas = tmp_path / "nas"
    (nas / "Movies" / "Inception (2010)").mkdir(parents=True)
    movie_file = nas / "Movies" / "Inception (2010)" / "Inception.2010.2160p.REMUX.mkv"
    movie_file.write_bytes(b"M" * 4096)
    (movie_file.with_suffix(".meta.json")).write_text(json.dumps({"duration_sec": 8880}), encoding="utf-8")
    (nas / "Movies" / "escape_file.mkv").symlink_to(outside_mkv)
    (nas / "Movies" / "escape_dir").symlink_to(outside_dir)

    (nas / "TV" / "Severance" / "Season 02").mkdir(parents=True)
    ep_file = nas / "TV" / "Severance" / "Season 02" / "Severance.S02E03.1080p.mp4"
    ep_file.write_bytes(b"T" * 2048)
    (ep_file.with_suffix(".zh-en.srt")).write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n你好\n", encoding="utf-8")

    (nas / "Music" / "Bill Evans").mkdir(parents=True)
    music_file = nas / "Music" / "Bill Evans" / "Nardis.flac"
    music_file.write_bytes(b"A" * 1024)
    (music_file.with_suffix(".lrc")).write_text("[00:10.00]Modal opening\n[00:10.00]调式开篇\n", encoding="utf-8")

    (nas / "Books").mkdir()
    book_file = nas / "Books" / "Dune.epub"
    book_file.write_bytes(b"B" * 512)

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config("mount_root", str(nas))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    src = ctx.domains.media_sources()[0]
    assert src.categories() == ["电影", "剧集", "音乐", "书籍"]

    all_items = await src.fetch_items()
    assert len(all_items) == 4
    titles = {it.title for it in all_items}
    assert not any("Secret" in t for t in titles)

    tv_items = await src.fetch_items(category="剧集", format="mp4")
    assert len(tv_items) == 1
    tv = tv_items[0]
    assert tv.play_url == ep_file.resolve().as_uri()
    assert tv.extra["size_bytes"] == 2048
    assert tv.extra["season"] == 2
    assert tv.extra["episode"] == 3
    assert tv.extra["episode_code"] == "S02E03"
    assert tv.has_bilingual_sub is True

    movie_items = await src.fetch_items(category="电影", query="inception")
    assert len(movie_items) == 1
    assert movie_items[0].duration_sec == 8880.0

    music_items = await src.fetch_items(category="音乐")
    assert len(music_items) == 1
    lyrics = await src.fetch_lyrics(music_items[0].id)
    assert len(lyrics) == 1 and lyrics[0].original == "Modal opening" and lyrics[0].translation == "调式开篇"

    # Pagination
    assert len(await src.fetch_items(page=1, page_size=2)) == 2
    assert len(await src.fetch_items(page=3, page_size=2)) == 0

    # Categories reflect only what actually exists (remove Music and Books in fresh mount).
    partial_nas = tmp_path / "partial_nas"
    (partial_nas / "Movies").mkdir(parents=True)
    (partial_nas / "Movies" / "Solo.mkv").write_bytes(b"X")
    ctx.set_config("mount_root", str(partial_nas))
    assert src.categories() == ["电影"]

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_write_subtitle_atomic_manual_protection_and_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sub = outside / "outside.srt"
    outside_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nOutside\n", encoding="utf-8")

    nas = tmp_path / "nas"
    (nas / "TV" / "Show").mkdir(parents=True)
    manual = nas / "TV" / "Show" / "Manual.srt"
    manual_text = "1\n00:00:01,000 --> 00:00:03,000\nHuman curated line\n人工精校字幕\n"
    manual.write_text(manual_text, encoding="utf-8")

    (nas / "TV" / "sym_dir").symlink_to(outside)
    (nas / "TV" / "Show" / "sym_file.srt").symlink_to(outside_sub)

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config("mount_root", str(nas))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Spy os.replace to verify .<name>.omnidesk.tmp atomic rename.
    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy_replace(src_p: Any, dst_p: Any) -> None:
        replace_calls.append((Path(src_p).name, str(dst_p)))
        real_replace(src_p, dst_p)

    monkeypatch.setattr(os, "replace", _spy_replace)

    srt_v1 = "1\n00:00:01,000 --> 00:00:03,000\nNew line\n新字幕\n"
    w1 = await ctx.capabilities.call(
        "nas.connection.v1",
        {"action": "write_subtitle", "target_path": "TV/Show/Auto.zh-en.srt", "content": srt_v1},
    )
    assert w1.ok is True
    assert w1.data["atomic"] is True
    assert w1.data["tmp_filename"] == ".Auto.zh-en.srt.omnidesk.tmp"
    assert any(tmp_n == ".Auto.zh-en.srt.omnidesk.tmp" for tmp_n, _ in replace_calls)
    auto_file = nas / "TV" / "Show" / "Auto.zh-en.srt"
    assert auto_file.read_text(encoding="utf-8") == srt_v1
    assert not (auto_file.parent / ".Auto.zh-en.srt.omnidesk.tmp").exists()

    # Updating own managed subtitle succeeds.
    srt_v2 = "1\n00:00:01,000 --> 00:00:03,000\nUpdated line\n更新字幕\n"
    w2 = await ctx.capabilities.call(
        "nas.connection.v1",
        {"action": "write_subtitle", "target_path": "TV/Show/Auto.zh-en.srt", "content": srt_v2},
    )
    assert w2.ok is True
    assert auto_file.read_text(encoding="utf-8") == srt_v2

    # If a human edits that file afterwards, OmniDesk refuses to overwrite it.
    auto_file.write_text(srt_v2 + "\n2\n00:00:04,000 --> 00:00:05,000\nHuman edit\n", encoding="utf-8")
    w_edited = await ctx.capabilities.call(
        "nas.connection.v1",
        {"action": "write_subtitle", "target_path": "TV/Show/Auto.zh-en.srt", "content": srt_v1},
    )
    assert w_edited.ok is False
    assert w_edited.data["error_code"] == "conflict"

    # Existing manual subtitle is strictly protected by default.
    w_manual = await ctx.capabilities.call(
        "nas.connection.v1",
        {"action": "write_subtitle", "target_path": "TV/Show/Manual.srt", "content": srt_v1},
    )
    assert w_manual.ok is False
    assert w_manual.data["error_code"] == "conflict"
    assert manual.read_text(encoding="utf-8") == manual_text

    # Path traversal and symlink escapes must be rejected.
    for bad_target in (
        "../outside/escape.srt",
        "TV/../../outside/escape.srt",
        str(outside / "abs_escape.srt"),
        "TV/sym_dir/evil.srt",
        "TV/Show/sym_file.srt",
        ".omnidesk/hack.srt",
    ):
        bad_res = await ctx.capabilities.call(
            "nas.connection.v1",
            {"action": "write_subtitle", "target_path": bad_target, "content": srt_v1},
        )
        assert bad_res.ok is False
        assert bad_res.data["error_code"] == "path_safety"

    # Managed subtitle directory whitelist check.
    ctx.set_config("managed_subtitle_dirs", "Subtitles")
    outside_managed = await ctx.capabilities.call(
        "nas.connection.v1",
        {"action": "write_subtitle", "target_path": "TV/Show/Other.srt", "content": srt_v1},
    )
    assert outside_managed.ok is False
    assert outside_managed.data["error_code"] == "path_safety"

    inside_managed = await ctx.capabilities.call(
        "nas.connection.v1",
        {"action": "write_subtitle", "target_path": "Subtitles/Allowed.srt", "content": srt_v1},
    )
    assert inside_managed.ok is True

    # Background task spawn & unload cancellation check.
    ctx.set_config("refresh_interval_sec", 60)
    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
    plugin2 = mod.create_plugin()
    ctx2 = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data2", config_dir=tmp_path / "cfg2", config_schema=manifest["config_schema"])
    ctx2.set_config({"mount_root": str(nas), "refresh_interval_sec": 60})
    await invoke_lifecycle_hook(plugin2.on_load, ctx2)
    assert plugin2._bg_task is not None and not plugin2._bg_task.done()
    await invoke_lifecycle_hook(plugin2.on_unload)
    assert plugin2._bg_task is None
    await ctx2.acleanup()
