"""Offline TXT encoding, MOBI/AZW3 fail-closed, and security boundary tests."""

from __future__ import annotations

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import PLUGIN_DIR, build_minimal_epub, load_plugin_module  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _write_fake_calibre(tmp_path: Path, *, mode: str = "epub") -> Path:
    script = tmp_path / "fake-ebook-convert"
    if mode == "epub":
        body = (
            "import sys, zipfile\n"
            "out = sys.argv[2]\n"
            "with zipfile.ZipFile(out, 'w') as zf:\n"
            "    zf.writestr('META-INF/container.xml', '<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\"><rootfiles><rootfile full-path=\"content.opf\"/></rootfiles></container>')\n"
            "    zf.writestr('content.opf', '<package xmlns=\"http://www.idpf.org/2007/opf\"><metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\"><dc:title>Converted MOBI Title</dc:title><dc:creator>Calibre Author</dc:creator></metadata><manifest><item id=\"c1\" href=\"ch1.xhtml\" media-type=\"application/xhtml+xml\"/></manifest><spine><itemref idref=\"c1\"/></spine></package>')\n"
            "    zf.writestr('ch1.xhtml', '<html><body><h1>Converted Chapter</h1><p>Cleanly converted paragraph.</p></body></html>')\n"
        )
    else:
        body = "import sys\nsys.stderr.write('conversion error\\n')\nsys.exit(2)\n"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script


@pytest.mark.asyncio
async def test_txt_utf8_gb18030_and_format_probe(tmp_path: Path) -> None:
    lib = tmp_path / "txt_lib"
    lib.mkdir()
    utf8_file = lib / "utf8_demo.txt"
    utf8_file.write_bytes(b"\xef\xbb\xbfChapter 1 Start\n\nFirst UTF-8 paragraph.\n\nSecond paragraph.")

    gb_file = lib / "gb18030_demo.txt"
    gb_text = "第一章 初见\n\n万象桌面支持简体中文与特殊字𠮷。\n\n第二段内容测试。"
    gb_file.write_bytes(gb_text.encode("gb18030"))

    bad_bin = lib / "fake_text.txt"
    bad_bin.write_bytes(b"\x00\xff\xfe\x81\x00binary")

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
    ctx.set_config({"library_roots": str(lib)})
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    source = ctx.domains.book_sources()[0]

    books = await source.list_books(format_filter="txt")
    utf_item = next(b for b in books if Path(b.file_path) == utf8_file.resolve())
    gb_item = next(b for b in books if Path(b.file_path) == gb_file.resolve())
    bad_item = next(b for b in books if Path(b.file_path) == bad_bin.resolve())

    assert utf_item.extra["encoding"] == "utf-8"
    assert gb_item.extra["encoding"] == "gb18030"

    gb_spread = await source.fetch_spread(gb_item.id, 1)
    joined = " ".join(blk.original for blk in gb_spread.blocks)
    assert "万象桌面支持简体中文与特殊字𠮷" in joined

    probe_bin = await ctx.capabilities.call("book.source.v1", {"action": "probe_format", "book_id": bad_item.id})
    assert probe_bin.ok is True
    assert probe_bin.data["format"] == "binary"

    bad_res = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": bad_item.id})
    assert bad_res.ok is False
    assert bad_res.data["error_code"] == "parse_error"
    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_mobi_azw3_fail_closed_and_calibre_conversion(tmp_path: Path) -> None:
    lib = tmp_path / "mobi_lib"
    lib.mkdir()
    mobi_path = lib / "sample.mobi"
    azw3_path = lib / "sample.azw3"
    mobi_path.write_bytes(b"SampleTitle".ljust(60, b"\x00") + b"BOOKMOBI" + b"\x00\x01\xff\xfeRAW_BINARY_GARBAGE")
    azw3_path.write_bytes(b"KindleAZW3".ljust(60, b"\x00") + b"BOOKMOBI" + b"\xff\x00\xab\xcdBINARY")

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
    ctx.set_config({"library_roots": str(lib), "calibre_bin": ""})
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    source = ctx.domains.book_sources()[0]

    books = await source.list_books()
    assert len(books) == 2
    mobi_item = next(b for b in books if b.format == "mobi")
    azw3_item = next(b for b in books if b.format == "azw3")
    assert mobi_item.extra["unavailable_reason"] == "format_unavailable"
    assert "RAW_BINARY_GARBAGE" not in mobi_item.title

    probe = await ctx.capabilities.call("book.source.v1", {"action": "probe_format", "book_id": mobi_item.id})
    assert probe.ok is True and probe.data["is_mobi_magic"] is True

    res_mobi = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": mobi_item.id})
    res_azw3 = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": azw3_item.id})
    assert res_mobi.ok is False and res_mobi.data["error_code"] == "format_unavailable"
    assert res_azw3.ok is False and res_azw3.data["error_code"] == "format_unavailable"

    bad_calibre = _write_fake_calibre(tmp_path, mode="fail")
    ctx.set_config("calibre_bin", str(bad_calibre))
    res_fail = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": mobi_item.id})
    assert res_fail.ok is False and res_fail.data["error_code"] == "format_unavailable"

    good_calibre = _write_fake_calibre(tmp_path, mode="epub")
    ctx.set_config("calibre_bin", str(good_calibre))
    res_ok = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": mobi_item.id})
    assert res_ok.ok is True
    assert res_ok.data["blocks"][1]["original"] == "Cleanly converted paragraph."
    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_security_traversal_symlink_zip_slip_zip_bomb_and_xml_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside_secret = tmp_path / "outside.txt"
    outside_secret.write_text("TOP_SECRET", encoding="utf-8")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "hidden.txt").write_text("HIDDEN", encoding="utf-8")

    lib = tmp_path / "secure_lib"
    lib.mkdir()
    os.symlink(outside_secret, lib / "symlink_book.txt")
    os.symlink(outside_dir, lib / "symlink_dir")

    zip_slip_epub = lib / "zip_slip.epub"
    with zipfile.ZipFile(zip_slip_epub, "w") as zf:
        zf.writestr("../../evil.txt", "escape")

    zip_bomb_epub = lib / "zip_bomb.epub"
    build_minimal_epub(
        zip_bomb_epub,
        chapters=[{"filename": "ch1.xhtml", "title": "Bomb", "body": "<p>" + ("A" * (17 * 1024 * 1024)) + "</p>"}],
    )

    xml_bomb_epub = lib / "xml_bomb.epub"
    with zipfile.ZipFile(xml_bomb_epub, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><container/>',
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

    ctx.set_config("library_roots", str(lib / ".." / "outside_dir"))
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    res_trav_root = await ctx.capabilities.call("book.source.v1", {"action": "list_books"})
    assert res_trav_root.ok is False and res_trav_root.data["error_code"] == "path_safety"

    ctx.set_config("library_roots", str(lib))
    listed = await ctx.capabilities.call("book.source.v1", {"action": "list_books"})
    assert listed.ok is True
    listed_paths = {Path(b["file_path"]).name for b in listed.data["books"]}
    assert "symlink_book.txt" not in listed_paths
    assert "hidden.txt" not in listed_paths

    res_trav_id = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": "../outside"})
    assert res_trav_id.ok is False and res_trav_id.data["error_code"] == "path_safety"

    res_probe_sym = await ctx.capabilities.call(
        "book.source.v1", {"action": "probe_format", "file_path": "symlink_book.txt"}
    )
    assert res_probe_sym.ok is False and res_probe_sym.data["error_code"] == "path_safety"

    by_name = {Path(b["file_path"]).name: b["id"] for b in listed.data["books"]}
    for bad_name, expected_code in (("zip_slip.epub", "path_safety"), ("zip_bomb.epub", "parse_error"), ("xml_bomb.epub", "parse_error")):
        res = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": by_name[bad_name]})
        assert res.ok is False
        assert res.data["error_code"] == expected_code

    tiny_txt = lib / "oversized.txt"
    tiny_txt.write_text("x" * 256, encoding="utf-8")
    from ebook_lib import parsers as ebook_parsers  # noqa: E402

    monkeypatch.setattr(ebook_parsers, "MAX_TXT_FILE_BYTES", 64)
    listed2 = await ctx.capabilities.call("book.source.v1", {"action": "list_books"})
    oversized_id = next(b["id"] for b in listed2.data["books"] if Path(b["file_path"]).name == "oversized.txt")
    res_size = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": oversized_id})
    assert res_size.ok is False and res_size.data["error_code"] == "parse_error"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
