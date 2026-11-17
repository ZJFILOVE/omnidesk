"""Offline manifest, lifecycle, BookSource, and EPUB parsing tests for plugin-ebook."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from conftest import PLUGIN_DIR, REPO_ROOT, build_minimal_epub, load_plugin_module  # noqa: E402
from omnidesk_core.manifest import load_manifest  # noqa: E402
from omnidesk_core.paths import OmnideskPaths  # noqa: E402
from omnidesk_core.plugins import PluginManager, PluginState  # noqa: E402
from omnidesk_sdk import (  # noqa: E402
    BasePlugin,
    BookItem,
    BookSource,
    BookSpread,
    CapabilityResult,
    PluginContext,
    invoke_lifecycle_hook,
)


def test_manifest_matches_v2_schema() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    parsed = load_manifest(PLUGIN_DIR)
    assert parsed.schema_version == 2
    assert parsed.id == "plugin-ebook"
    assert parsed.kind == "hybrid"
    assert parsed.domain == "book"
    assert parsed.provides == ["book.source.v1", "vocab.sm2.v1", "book.ebook.v1"]

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest["provides"])

    fields = {f["key"]: f for f in manifest["config_schema"]}
    assert {"library_roots", "calibre_bin", "target_language", "sync_enabled"}.issubset(fields.keys())
    assert fields["library_roots"]["default"] == ""
    assert fields["calibre_bin"]["default"] == ""
    assert fields["sync_enabled"]["type"] == "bool"


@pytest.mark.asyncio
async def test_plugin_manager_lifecycle_reload_and_vocab_path(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "pm-cfg"),
            "XDG_DATA_HOME": str(tmp_path / "pm-data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "pm-run"),
        }
    )
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    rec = await mgr.load_plugin(PLUGIN_DIR)
    assert rec.state == PluginState.LOADED
    assert mgr.capabilities.has("book.source.v1")
    assert mgr.capabilities.has("vocab.sm2.v1")
    assert len(mgr.domains.list_book_sources(plugin_id="plugin-ebook")) == 1

    add_res = await mgr.capabilities.call("vocab.sm2.v1", {"action": "add", "word": "Lifecycle"})
    assert add_res.ok is True
    expected_vocab = tmp_path / "pm-data" / "omnidesk" / "vocab" / "plugin-ebook.json"
    assert expected_vocab.is_file()
    assert stat.S_IMODE(expected_vocab.stat().st_mode) == 0o600

    health = await mgr.health_check("plugin-ebook")
    assert health["ok"] is True

    reloaded = await mgr.reload_plugin("plugin-ebook")
    assert reloaded.state == PluginState.LOADED
    assert reloaded.reload_count == 1

    await mgr.unload_plugin("plugin-ebook")
    assert not mgr.capabilities.has("book.source.v1")
    assert not mgr.capabilities.has("vocab.sm2.v1")
    assert mgr.domains.list_book_sources(plugin_id="plugin-ebook") == []


@pytest.mark.asyncio
async def test_empty_library_no_fake_books_and_not_found(tmp_path: Path) -> None:
    mod = load_plugin_module()
    plugin = mod.create_plugin()
    assert isinstance(plugin, BasePlugin)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "pdata",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    source = ctx.domains.book_sources()[0]
    assert isinstance(source, BookSource)

    assert await source.list_books() == []
    res_list = await ctx.capabilities.call("book.source.v1", {"action": "list_books"})
    assert res_list.ok is True
    assert res_list.data["books"] == []
    assert res_list.data["count"] == 0

    with pytest.raises(LookupError, match="not_found|not found"):
        await source.fetch_spread("ebook-nonexistent", 1)

    res_missing = await ctx.capabilities.call(
        "book.source.v1", {"action": "fetch_spread", "book_id": "ebook-nonexistent", "page_number": 1}
    )
    assert res_missing.ok is False
    assert res_missing.data["error_code"] == "not_found"
    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_multi_root_delimited_scan_filter_query_and_progress_sidecar(tmp_path: Path) -> None:
    root_a = tmp_path / "books_a"
    root_b = tmp_path / "books_b"
    sub_a = root_a / "nested"
    build_minimal_epub(root_a / "alpha.epub", title="Alpha Architecture", author="Alice")
    sub_a.mkdir(parents=True, exist_ok=True)
    txt_file = sub_a / "notes.txt"
    txt_file.write_text("Chapter 1\n\nParagraph one.\n\nParagraph two.\n\n" + ("Word " * 120), encoding="utf-8")
    (sub_a / "notes.txt.progress.json").write_text(json.dumps({"current_page": 2}), encoding="utf-8")

    root_b.mkdir(parents=True, exist_ok=True)
    build_minimal_epub(root_b / "beta.epub", title="Beta Systems", author="Bob")

    mod = load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "pdata",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    # Delimited string config with overlapping sub_a root to verify deduplication & stable book_id
    ctx.set_config(
        {
            "library_roots": f"{root_a}{os.pathsep}{sub_a},{root_b}",
            "page_char_budget": 300,
        }
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    source = ctx.domains.book_sources()[0]

    all_books = await source.list_books()
    assert len(all_books) == 3
    assert all(isinstance(b, BookItem) for b in all_books)
    ids_first = {Path(b.file_path).name: b.id for b in all_books}

    notes_book = next(b for b in all_books if b.format == "txt")
    assert notes_book.total_pages >= 2
    assert notes_book.current_page == 2
    assert notes_book.progress_percent > 0.0

    epubs = await source.list_books(format_filter="EPUB")
    assert len(epubs) == 2
    queried = await source.list_books(query="Bob")
    assert [b.title for b in queried] == ["Beta Systems"]

    alpha_id = ids_first["alpha.epub"]
    spread = await source.fetch_spread(alpha_id, 2)
    assert spread.page_number == 2
    relisted = await source.list_books()
    ids_second = {Path(b.file_path).name: b.id for b in relisted}
    assert ids_first == ids_second
    alpha_after = next(b for b in relisted if b.id == alpha_id)
    assert alpha_after.current_page == 2
    assert alpha_after.progress_percent == 100.0
    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_epub_nav_ncx_images_pagination_and_optional_translation(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    long_p1 = "First paragraph sentence one. " * 8
    long_p2 = "Second paragraph with ephemeral terminology. " * 8
    epub_nav = build_minimal_epub(
        lib / "nav_book.epub",
        title="NAV Book",
        author="Grace Hopper",
        chapters=[
            {
                "filename": "sec1.xhtml",
                "title": "Section Alpha (NAV)",
                "body": f"<h1>Intro</h1><p>{long_p1.strip()}</p><p>{long_p2.strip()}</p><img src='fig1.png' alt='Flowchart'/>",
            }
        ],
        use_nav=True,
        use_ncx=False,
    )
    epub_ncx = build_minimal_epub(
        lib / "ncx_book.epub",
        title="NCX Book",
        author="Alan Turing",
        chapters=[
            {
                "filename": "part1.xhtml",
                "title": "NCX Chapter One",
                "body": "<p>Entity &amp; typography check &mdash; clean text.</p><p class='omnidesk-translation'>实体与排版检查。</p>",
            }
        ],
        use_nav=False,
        use_ncx=True,
    )

    mod = load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "pdata",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"library_roots": str(lib), "page_char_budget": 280})
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    await ctx.capabilities.call("vocab.sm2.v1", {"action": "add", "word": "Ephemeral", "meaning": "短暂的"})

    books = await ctx.domains.book_sources()[0].list_books()
    nav_item = next(b for b in books if Path(b.file_path) == epub_nav.resolve())
    assert nav_item.total_pages >= 2
    assert nav_item.extra["toc"][0]["title"] == "Section Alpha (NAV)"

    p1 = await ctx.domains.book_sources()[0].fetch_spread(nav_item.id, 1)
    assert isinstance(p1, BookSpread)
    assert p1.chapter_title == "Section Alpha (NAV)"
    assert any(long_p1.strip() == blk.original for blk in p1.blocks)

    all_blocks = []
    for pn in range(1, nav_item.total_pages + 1):
        sp = await ctx.domains.book_sources()[0].fetch_spread(nav_item.id, pn)
        all_blocks.extend(sp.blocks)
    assert any(blk.kind == "image" and "Flowchart" in blk.original for blk in all_blocks)
    assert any("ephemeral" in blk.highlighted_words for blk in all_blocks)

    # Optional llm.translate.v1 integration on fetch_spread(..., translate=True)
    ctx.capabilities.register(
        "llm.translate.v1",
        lambda req: CapabilityResult.ok({"translation": f"译:{req['text'][:12]}"}),
        plugin_id="plugin-magpie-agent",
    )
    translated_spread = await ctx.capabilities.call(
        "book.source.v1",
        {"action": "fetch_spread", "book_id": nav_item.id, "page_number": 1, "translate": True},
    )
    assert translated_spread.ok is True
    assert translated_spread.data["blocks"][0]["translated"].startswith("译:")

    ncx_item = next(b for b in books if Path(b.file_path) == epub_ncx.resolve())
    assert ncx_item.extra["toc"][0]["title"] == "NCX Chapter One"
    ncx_spread = await ctx.domains.book_sources()[0].fetch_spread(ncx_item.id, 1)
    assert ncx_spread.chapter_title == "NCX Chapter One"
    assert "Entity & typography check — clean text." in ncx_spread.blocks[0].original
    assert ncx_spread.blocks[0].translated == "实体与排版检查。"
    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()

@pytest.mark.asyncio
async def test_download_handoff_opens_configured_book_and_rejects_outside_file(tmp_path: Path) -> None:
    root = tmp_path / 'library'
    root.mkdir()
    path = root / 'download.txt'
    path.write_text('Downloaded text book.\n\nPage content.', encoding='utf-8')
    outside = tmp_path / 'outside.txt'
    outside.write_text('Not in library', encoding='utf-8')
    plugin = load_plugin_module().create_plugin()
    manifest = json.loads((PLUGIN_DIR / 'plugin.json').read_text())
    ctx = PluginContext('plugin-ebook', data_dir=tmp_path / 'data', config_schema=manifest['config_schema'])
    ctx.set_config('library_roots', str(root))
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    try:
        result = await ctx.capabilities.call('book.ebook.v1', {'action': 'open', 'file_path': str(path)})
        assert result.ok and result.data['book_id'].startswith('ebook-')
        spread = await ctx.capabilities.call('book.source.v1', {'action': 'fetch_spread', 'book_id': result.data['book_id']})
        assert spread.ok and 'Downloaded text book' in str(spread.data)
        denied = await ctx.capabilities.call('book.ebook.v1', {'action': 'open', 'file_path': str(outside)})
        assert not denied.ok and denied.data['error_code'] == 'path_safety'
    finally:
        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
