"""Offline manifest, BookSource, status, and spread parsing tests for plugin-pdf-babeldoc."""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import BasePlugin, BookItem, BookSpread, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_pdf_babeldoc_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_fake_babeldoc(path: Path, body: str, *, executable: bool = True) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(stat.S_IRWXU if executable else (stat.S_IRUSR | stat.S_IWUSR))
    return path


def _write_minimal_pdf(
    path: Path,
    *,
    pages: int = 1,
    title: str = "",
    author: str = "",
) -> Path:
    info_parts = []
    if title:
        info_parts.append(f"/Title ({title})")
    if author:
        info_parts.append(f"/Author ({author})")
    info_obj = f"4 0 obj\n<< {' '.join(info_parts)} >>\nendobj\n" if info_parts else ""
    kids = " ".join(f"{10 + i} 0 R" for i in range(pages))
    leaf_objs = "".join(f"{10 + i} 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" for i in range(pages))
    content = (
        f"%PDF-1.4\n"
        f"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {pages} >>\nendobj\n"
        f"{leaf_objs}"
        f"{info_obj}"
        f"%%EOF\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return path


def test_manifest_matches_v2_schema_and_config_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-pdf-babeldoc"
    assert manifest["kind"] == "hybrid"
    assert manifest["domain"] == "book"
    assert manifest["entry"] == "plugin:create_plugin"
    assert set(manifest["provides"]) == {"book.source.v1", "book.bilingual_pdf.v1"}

    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(cap) for cap in manifest["provides"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    assert {"babeldoc_bin", "pdf_library_roots", "output_root", "target_language", "timeout_sec"}.issubset(fields.keys())
    assert fields["babeldoc_bin"]["default"] == ""
    assert fields["pdf_library_roots"]["default"] == ""
    assert fields["output_root"]["default"] == ""
    assert fields["timeout_sec"]["type"] == "int"


@pytest.mark.asyncio
async def test_status_fails_closed_when_unconfigured_or_missing_and_detects_version(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
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
    assert health["babeldoc_bin_found"] is False
    assert ctx.capabilities.has("book.source.v1")
    assert ctx.capabilities.has("book.bilingual_pdf.v1")
    assert len(ctx.domains.book_sources()) == 1

    # 1. Default unconfigured -> not_configured
    res_empty = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "status"})
    assert res_empty.ok is False
    assert res_empty.data["error_code"] == "not_configured"

    # 2. Non-existent binary -> not_configured
    ctx.set_config("babeldoc_bin", str(tmp_path / "does-not-exist"))
    res_missing = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "status"})
    assert res_missing.ok is False
    assert res_missing.data["error_code"] == "not_configured"

    # 3. Non-executable file -> not_configured
    non_exec = _write_fake_babeldoc(tmp_path / "non-exec-babeldoc", "print('babeldoc 0.6.4')", executable=False)
    ctx.set_config("babeldoc_bin", str(non_exec))
    res_noexec = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "status"})
    assert res_noexec.ok is False
    assert res_noexec.data["error_code"] == "not_configured"

    # 4. Working fake executable -> ok + parsed version
    fake_bin = _write_fake_babeldoc(
        tmp_path / "fake-babeldoc",
        "import sys\nif '--version' in sys.argv:\n    print('babeldoc 0.6.4')\n",
    )
    lib_dir = tmp_path / "library"
    out_dir = tmp_path / "output"
    lib_dir.mkdir()
    out_dir.mkdir()
    ctx.set_config({"babeldoc_bin": str(fake_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)})

    res_ok = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "status"})
    assert res_ok.ok is True
    assert res_ok.data["babeldoc_found"] is True
    assert res_ok.data["version"] == "0.6.4"
    assert res_ok.data["ready"] is True

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
    assert not ctx.capabilities.has("book.bilingual_pdf.v1")
    assert ctx.domains.book_sources() == []


@pytest.mark.asyncio
async def test_book_source_scans_real_pdfs_and_spread_modes(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    lib_a = tmp_path / "lib_a"
    lib_b = tmp_path / "lib_b"
    out_dir = tmp_path / "out"
    outside_dir = tmp_path / "outside"
    lib_a.mkdir()
    lib_b.mkdir()
    out_dir.mkdir()
    outside_dir.mkdir()

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Unconfigured library fails closed on capability and returns [] on BookSource
    unconf = await ctx.capabilities.call("book.source.v1", {"action": "list_books"})
    assert unconf.ok is False
    assert unconf.data["error_code"] == "not_configured"
    source = ctx.domains.book_sources()[0]
    assert await source.list_books() == []

    # Populate library with real PDFs, one invalid non-PDF, and one symlink escape
    pdf_untranslated = _write_minimal_pdf(
        lib_a / "attention.pdf", pages=3, title="Attention Is All You Need", author="Vaswani et al."
    )
    pdf_with_sidecar = _write_minimal_pdf(
        lib_b / "sub" / "scaling.pdf", pages=2, title="Scaling Laws", author="Kaplan et al."
    )
    pdf_no_sidecar = _write_minimal_pdf(
        lib_a / "alternating.pdf", pages=2, title="Alternating Dual", author="Author B"
    )
    (lib_a / "corrupt.pdf").write_bytes(b"NOT A REAL PDF FILE")
    outside_pdf = _write_minimal_pdf(outside_dir / "secret.pdf", pages=1, title="Outside")
    (lib_a / "escape.pdf").symlink_to(outside_pdf)

    # Pre-existing sidecar for scaling.pdf and alternating dual PDF without sidecar for alternating.pdf
    (out_dir / "scaling.zh.dual.pdf").write_bytes(_write_minimal_pdf(out_dir / "tmp_s.pdf", pages=2).read_bytes())
    (out_dir / "tmp_s.pdf").unlink()
    (out_dir / "scaling.page_map.json").write_text(
        json.dumps(
            {
                "layout": "dual_side_by_side",
                "total_pages": 2,
                "pages": [
                    {
                        "page_number": 1,
                        "chapter_title": "Section 1",
                        "blocks": [
                            {
                                "id": "b1",
                                "kind": "paragraph",
                                "original": "Empirical performance follows power laws.",
                                "translated": "经验性能遵循幂律分布。",
                                "highlighted_words": ["empirical"],
                            }
                        ],
                    },
                    {
                        "page_number": 2,
                        "chapter_title": "Section 2",
                        "original": "Compute scales smoothly.",
                        "translated": "算力平滑扩展。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 4-page alternating dual PDF for 2-page alternating.pdf (no sidecar!)
    _write_minimal_pdf(out_dir / "alternating.zh.dual.pdf", pages=4)

    ctx.set_config({"pdf_library_roots": f"{lib_a}:{lib_b}", "output_root": str(out_dir), "target_language": "zh"})

    books = await source.list_books()
    assert len(books) == 3
    assert all(isinstance(b, BookItem) for b in books)
    by_path = {b.file_path: b for b in books}
    assert str(pdf_untranslated.resolve()) in by_path
    assert str(pdf_with_sidecar.resolve()) in by_path
    assert str(pdf_no_sidecar.resolve()) in by_path

    b_untrans = by_path[str(pdf_untranslated.resolve())]
    assert b_untrans.title == "Attention Is All You Need"
    assert b_untrans.author == "Vaswani et al."
    assert b_untrans.total_pages == 3
    assert b_untrans.extra["file_size"] == pdf_untranslated.stat().st_size
    assert b_untrans.bilingual_ready is False

    b_sidecar = by_path[str(pdf_with_sidecar.resolve())]
    assert b_sidecar.bilingual_ready is True

    # Untranslated -> needs_bilingual_pdf with empty blocks (never fabricated)
    spread_untrans = await source.fetch_spread(b_untrans.id, page_number=1)
    assert isinstance(spread_untrans, BookSpread)
    assert spread_untrans.blocks == []
    assert spread_untrans.extra["status"] == "needs_bilingual_pdf"
    assert spread_untrans.extra["needs_bilingual_pdf"] is True

    cap_untrans = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": b_untrans.id, "page_number": 1})
    assert cap_untrans.ok is False
    assert cap_untrans.data["error_code"] == "needs_bilingual_pdf"

    # Sidecar -> exact original + translated blocks
    spread_p1 = await source.fetch_spread(b_sidecar.id, page_number=1)
    assert spread_p1.blocks[0].original == "Empirical performance follows power laws."
    assert spread_p1.blocks[0].translated == "经验性能遵循幂律分布。"
    assert spread_p1.blocks[0].highlighted_words == ["empirical"]
    spread_p2 = await source.fetch_spread(b_sidecar.id, page_number=2)
    assert spread_p2.blocks[0].original == "Compute scales smoothly."
    assert spread_p2.blocks[0].translated == "算力平滑扩展。"

    # Missing sidecar with 2*N dual PDF -> alternating page pairing placeholder, NO fabricated translation
    b_alt = by_path[str(pdf_no_sidecar.resolve())]
    spread_alt = await source.fetch_spread(b_alt.id, page_number=2)
    assert spread_alt.extra["has_sidecar"] is False
    assert spread_alt.extra["placeholder"] is True
    assert spread_alt.extra["pairing_mode"] == "alternating_pages"
    assert spread_alt.extra["left_page"] == 3
    assert spread_alt.extra["right_page"] == 4
    assert "占位" in spread_alt.blocks[0].original
    assert "占位" in spread_alt.blocks[0].translated

    # Query filter and format filter
    assert len(await source.list_books(format_filter="epub")) == 0
    assert len(await source.list_books(query="Vaswani")) == 1

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
