"""Edge-case tests: sidecar variants, PDF metadata decoding, glossary CLI errors, and PluginManager unload."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for path_str in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from omnidesk_core import OmnideskPaths, PluginManager  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402
from plugin import create_plugin  # noqa: E402


def _write_fake_babeldoc(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(stat.S_IRWXU)
    return path


from pdf_fixture import write_pdf as _write_minimal_pdf



async def _wait_job(ctx: PluginContext, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "job_status", "job_id": job_id})
        if res.ok and res.data["status"] in {"succeeded", "failed", "cancelled"}:
            return res.data
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {job_id}")


@pytest.mark.asyncio
async def test_sidecar_variants_side_by_side_placeholder_and_utf16_metadata(tmp_path: Path) -> None:
    plugin = create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    lib_dir.mkdir()
    out_dir.mkdir()

    # PDF with UTF-16BE hex title and escaped literal author
    title_hex = ("FEFF" + "双栏论文".encode("utf-16-be").hex()).upper()
    pdf_utf16 = lib_dir / "utf16_paper.pdf"
    pdf_utf16.write_bytes(
        (
            f"%PDF-1.4\n1 0 obj\n<< /Type /Pages /Count 2 >>\nendobj\n"
            f"2 0 obj\n<< /Title <{title_hex}> /Author (Alice \\(Lab\\)) >>\nendobj\n%%EOF\n"
        ).encode("utf-8")
    )
    (out_dir / "utf16_paper.page_map.json").write_text(
        json.dumps(
            {
                "pages": {
                    "1": {"chapter_title": "Intro", "blocks": [{"kind": "heading", "original": "Title", "translated": "标题"}]},
                    "2": {"original": "Second page", "translated": "第二页"},
                }
            }
        ),
        encoding="utf-8",
    )

    # Side-by-side N==N dual PDF without sidecar -> placeholder with side_by_side pairing
    _write_minimal_pdf(lib_dir / "sbs.pdf", pages=2)
    _write_minimal_pdf(out_dir / "sbs.zh.dual.pdf", pages=2)

    # page_map list sidecar variant
    _write_minimal_pdf(lib_dir / "pmap.pdf", pages=2)
    (out_dir / "pmap.pages.json").write_text(
        json.dumps({"page_map": [{"source_page": 1, "dual_page": 2, "original": "Left", "translated": "右"}]}),
        encoding="utf-8",
    )

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"pdf_library_roots": str(lib_dir), "output_root": str(out_dir), "target_language": "zh"})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    src = ctx.domains.book_sources()[0]
    books = {Path(b.file_path).name: b for b in await src.list_books()}
    assert books["utf16_paper.pdf"].title == "双栏论文"
    assert books["utf16_paper.pdf"].author == "Alice (Lab)"

    s_dict = await src.fetch_spread(books["utf16_paper.pdf"].id, page_number=1)
    assert s_dict.blocks[0].kind == "heading"
    assert s_dict.blocks[0].translated == "标题"

    s_sbs = await src.fetch_spread(books["sbs.pdf"].id, page_number=2)
    assert s_sbs.extra["pairing_mode"] == "side_by_side"
    assert s_sbs.extra["left_page"] == 2 and s_sbs.extra["right_page"] == 2
    assert s_sbs.blocks[0].image_url.startswith("data:image/png;base64,")
    assert not s_sbs.blocks[0].translated

    s_pmap = await src.fetch_spread(books["pmap.pdf"].id, page_number=1)
    assert s_pmap.extra["pairing_mode"] == "alternating_pages"
    assert s_pmap.blocks[0].original == "Left" and s_pmap.blocks[0].translated == "右"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_glossary_csv_runtime_unsupported_and_permission_errors(tmp_path: Path) -> None:
    plugin = create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    lib_dir.mkdir()
    out_dir.mkdir()
    src_pdf = _write_minimal_pdf(lib_dir / "doc.pdf")

    csv_glossary = tmp_path / "terms.csv"
    csv_glossary.write_text("source,target\nattention,注意力\n", encoding="utf-8")
    yaml_glossary = tmp_path / "terms.yaml"
    yaml_glossary.write_text("attention: 注意力\n", encoding="utf-8")

    # Fake binary that fails at runtime on --glossary-files without implementing --help
    runtime_reject_bin = _write_fake_babeldoc(
        tmp_path / "fake-runtime-reject",
        """
import sys
if "--help" in sys.argv:
    sys.exit(2)
if "--glossary-files" in sys.argv:
    sys.stderr.write("error: unrecognized arguments: --glossary-files\\n")
    sys.exit(2)
""",
    )

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"babeldoc_bin": str(runtime_reject_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Unsupported .yaml glossary rejected synchronously
    bad_ext = await ctx.capabilities.call(
        "book.bilingual_pdf.v1", {"action": "submit", "source_path": str(src_pdf), "glossary": str(yaml_glossary)}
    )
    assert bad_ext.ok is False and bad_ext.data["error_code"] == "unsupported_option"

    # CSV glossary passed to runtime_reject_bin -> job fails with unsupported_option
    sub = await ctx.capabilities.call(
        "book.bilingual_pdf.v1", {"action": "submit", "source_path": str(src_pdf), "glossary": str(csv_glossary)}
    )
    assert sub.ok is True
    job = await _wait_job(ctx, sub.data["job_id"])
    assert job["status"] == "failed" and job["error_code"] == "unsupported_option"

    # Cancel already-terminal job returns already_terminal=True; missing job returns not_found
    c_term = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "cancel", "job_id": sub.data["job_id"]})
    assert c_term.ok is True and c_term.data["already_terminal"] is True
    c_miss = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "job_status", "job_id": "missing-id"})
    assert c_miss.ok is False and c_miss.data["error_code"] == "not_found"

    # Non-writable output subdir -> permission_denied (when running non-root)
    if os.geteuid() != 0:
        ro_out = out_dir / "readonly"
        ro_out.mkdir()
        ro_out.chmod(0o555)
        try:
            perm_res = await ctx.capabilities.call(
                "book.bilingual_pdf.v1", {"action": "submit", "source_path": str(src_pdf), "output_dir": str(ro_out)}
            )
            assert perm_res.ok is False and perm_res.data["error_code"] == "permission_denied"
        finally:
            ro_out.chmod(0o755)

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_plugin_manager_load_unload_revokes_resources_and_subprocess(tmp_path: Path) -> None:
    paths = OmnideskPaths.from_env(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    lib_dir.mkdir()
    out_dir.mkdir()
    src_pdf = _write_minimal_pdf(lib_dir / "doc.pdf")
    pid_file = tmp_path / "bg.pid"

    slow_bin = _write_fake_babeldoc(
        tmp_path / "fake-bg",
        f"import os, pathlib, time\npathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )

    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    mgr.config_store.write(
        "plugin-pdf-babeldoc",
        {"babeldoc_bin": str(slow_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)},
    )
    await mgr.load_plugin(PLUGIN_DIR)
    assert mgr.capabilities.has("book.source.v1")
    assert mgr.capabilities.has("book.bilingual_pdf.v1")
    assert len(mgr.domains.list_book_sources(plugin_id="plugin-pdf-babeldoc")) == 1

    sub = await mgr.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": str(src_pdf)})
    assert sub.ok is True
    deadline = time.monotonic() + 3.0
    while not pid_file.is_file() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert pid_file.is_file()
    bg_pid = int(pid_file.read_text(encoding="utf-8"))

    await mgr.unload_plugin("plugin-pdf-babeldoc")
    assert not mgr.capabilities.has("book.source.v1")
    assert not mgr.capabilities.has("book.bilingual_pdf.v1")
    assert mgr.domains.list_book_sources(plugin_id="plugin-pdf-babeldoc") == []
    await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(bg_pid, 0)
