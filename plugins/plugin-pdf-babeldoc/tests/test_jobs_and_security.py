"""Offline job lifecycle, security containment, lock, cancellation, and glossary tests."""

from __future__ import annotations

import asyncio
import importlib.util
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_pdf_babeldoc_jobs_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_fake_babeldoc(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(stat.S_IRWXU)
    return path


def _write_minimal_pdf(path: Path, *, pages: int = 2) -> Path:
    kids = " ".join(f"{10 + i} 0 R" for i in range(pages))
    leaf_objs = "".join(f"{10 + i} 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" for i in range(pages))
    raw = (
        f"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {pages} >>\nendobj\n{leaf_objs}%%EOF\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw.encode("utf-8"))
    return path


async def _wait_for_terminal_job(ctx: PluginContext, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "job_status", "job_id": job_id})
        assert res.ok is True
        if res.data["status"] in {"succeeded", "failed", "cancelled"}:
            return res.data
        await asyncio.sleep(0.03)
    raise AssertionError(f"Job {job_id} did not reach terminal state within {timeout}s")


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_submit_security_path_containment_and_pdf_magic(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    outside_dir = tmp_path / "outside"
    lib_dir.mkdir()
    out_dir.mkdir()
    outside_dir.mkdir()

    fake_bin = _write_fake_babeldoc(tmp_path / "fake-babeldoc", "import sys\nsys.exit(0)\n")
    valid_pdf = _write_minimal_pdf(lib_dir / "paper.pdf")
    outside_pdf = _write_minimal_pdf(outside_dir / "secret.pdf")
    bad_magic_pdf = lib_dir / "fake.pdf"
    bad_magic_pdf.write_bytes(b"HTML_NOT_PDF")
    (lib_dir / "link_escape.pdf").symlink_to(outside_pdf)

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"babeldoc_bin": str(fake_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Path traversal and escapes must fail closed with path_safety
    for bad_src in [str(outside_pdf), str(lib_dir / ".." / "outside" / "secret.pdf"), str(lib_dir / "link_escape.pdf")]:
        res = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": bad_src})
        assert res.ok is False
        assert res.data["error_code"] == "path_safety"

    for bad_out in [str(outside_dir), str(out_dir / ".." / "outside")]:
        res = await ctx.capabilities.call(
            "book.bilingual_pdf.v1", {"action": "submit", "source_path": str(valid_pdf), "output_dir": bad_out}
        )
        assert res.ok is False
        assert res.data["error_code"] == "path_safety"

    # Symlinked output_dir inside output_root escaping outside
    (out_dir / "sym_out").symlink_to(outside_dir)
    res_sym_out = await ctx.capabilities.call(
        "book.bilingual_pdf.v1",
        {"action": "submit", "source_path": str(valid_pdf), "output_dir": str(out_dir / "sym_out")},
    )
    assert res_sym_out.ok is False
    assert res_sym_out.data["error_code"] == "path_safety"

    # Invalid PDF magic
    res_magic = await ctx.capabilities.call(
        "book.bilingual_pdf.v1", {"action": "submit", "source_path": str(bad_magic_pdf)}
    )
    assert res_magic.ok is False
    assert res_magic.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_submit_success_glossary_sidecar_bus_event_and_0600_job(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    lib_dir.mkdir()
    out_dir.mkdir()
    source_pdf = _write_minimal_pdf(lib_dir / "paper.pdf", pages=2)
    captured_args_path = tmp_path / "captured_args.json"

    fake_bin = _write_fake_babeldoc(
        tmp_path / "fake-babeldoc",
        f"""
import argparse, json, pathlib, sys
if "--version" in sys.argv:
    print("babeldoc 0.6.4")
    sys.exit(0)
if "--help" in sys.argv:
    print("usage: babeldoc [--files FILES] [--output OUTPUT] [--lang-out LANG_OUT] [--glossary-files GLOSSARY_FILES]")
    sys.exit(0)
parser = argparse.ArgumentParser()
parser.add_argument("--files", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--lang-out", required=True)
parser.add_argument("--glossary-files", default="")
ns = parser.parse_args()
glossary_text = pathlib.Path(ns.glossary_files).read_text(encoding="utf-8") if ns.glossary_files else ""
pathlib.Path({str(captured_args_path)!r}).write_text(
    json.dumps({{"argv": sys.argv[1:], "glossary_csv": glossary_text}}), encoding="utf-8"
)
out = pathlib.Path(ns.output)
stem = pathlib.Path(ns.files).stem
dual_pdf = out / f"{{stem}}.{{ns.lang_out}}.dual.pdf"
dual_pdf.write_bytes(b"%PDF-1.4\\n1 0 obj\\n<< /Type /Pages /Count 2 >>\\nendobj\\n%%EOF\\n")
sidecar = out / f"{{stem}}.page_map.json"
sidecar.write_text(
    json.dumps({{"pages": [{{"page": 1, "original": "Ephemeral state", "translated": "瞬时状态"}}]}}),
    encoding="utf-8",
)
""",
    )

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"babeldoc_bin": str(fake_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)})
    bus_events: list[dict[str, Any]] = []
    ctx.subscribe("entity.book.open", lambda evt: bus_events.append(evt.payload))
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    submit_res = await ctx.capabilities.call(
        "book.bilingual_pdf.v1",
        {
            "action": "submit",
            "source_path": str(source_pdf),
            "target_language": "zh",
            "glossary": {"ephemeral": "瞬时"},
            "operation_id": "op-success-1",
        },
    )
    assert submit_res.ok is True
    assert submit_res.data["status"] == "queued"
    job_id = submit_res.data["job_id"]

    # Duplicate operation_id (sequential + concurrent check)
    dup_res = await ctx.capabilities.call(
        "book.bilingual_pdf.v1",
        {
            "action": "submit",
            "source_path": str(source_pdf),
            "target_language": "zh",
            "operation_id": "op-success-1",
        },
    )
    assert dup_res.ok is False
    assert dup_res.data["error_code"] == "conflict"
    assert dup_res.data["existing_job_id"] == job_id

    final = await _wait_for_terminal_job(ctx, job_id)
    assert final["status"] == "succeeded"
    assert final["progress"] == 1.0
    assert Path(final["dual_pdf_path"]).is_file()
    assert Path(final["page_map_path"]).is_file()

    # Verify 0600 job file permissions
    job_file = (tmp_path / "data" / "jobs" / f"{job_id}.json")
    assert stat.S_IMODE(job_file.stat().st_mode) == 0o600

    # Verify glossary CSV was passed to BabelDOC
    captured = json.loads(captured_args_path.read_text(encoding="utf-8"))
    assert "--glossary-files" in captured["argv"]
    assert "ephemeral,瞬时,zh" in captured["glossary_csv"]

    # Verify entity.book.open emitted
    assert len(bus_events) == 1
    assert bus_events[0]["file_path"] == final["dual_pdf_path"]
    assert bus_events[0]["format"] == "pdf"
    assert bus_events[0]["page"] == 1

    # Verify list_jobs and BookSource integration
    listed = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "list_jobs", "status": "succeeded"})
    assert listed.ok is True and listed.data["count"] == 1

    spread = await ctx.domains.book_sources()[0].fetch_spread(final["book_id"], page_number=1)
    assert spread.blocks[0].original == "Ephemeral state"
    assert spread.blocks[0].translated == "瞬时状态"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_concurrent_duplicate_operation_id_and_unsupported_glossary(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    lib_dir.mkdir()
    out_dir.mkdir()
    source_pdf = _write_minimal_pdf(lib_dir / "paper.pdf")

    # Fake babeldoc whose --help does NOT expose --glossary-files
    fake_bin = _write_fake_babeldoc(
        tmp_path / "fake-babeldoc-nogloss",
        """
import pathlib, sys
if "--help" in sys.argv:
    print("usage: babeldoc [--files FILES] [--output OUTPUT] [--lang-out LANG_OUT]")
    sys.exit(0)
out = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
stem = pathlib.Path(sys.argv[sys.argv.index("--files") + 1]).stem
(out / f"{stem}.zh.dual.pdf").write_bytes(b"%PDF-1.4\\n%%EOF\\n")
""",
    )
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"babeldoc_bin": str(fake_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Unsupported glossary -> unsupported_option (never silently ignored)
    unsup = await ctx.capabilities.call(
        "book.bilingual_pdf.v1",
        {"action": "submit", "source_path": str(source_pdf), "glossary": {"k": "v"}},
    )
    assert unsup.ok is False
    assert unsup.data["error_code"] == "unsupported_option"

    # Concurrent duplicate operation_id race test
    res_a, res_b = await asyncio.gather(
        ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": str(source_pdf), "operation_id": "op-race"}),
        ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": str(source_pdf), "operation_id": "op-race"}),
    )
    assert sorted([res_a.ok, res_b.ok]) == [False, True]
    winner = res_a if res_a.ok else res_b
    loser = res_b if res_a.ok else res_a
    assert loser.data["error_code"] == "conflict"
    assert loser.data["existing_job_id"] == winner.data["job_id"]
    await _wait_for_terminal_job(ctx, winner.data["job_id"])

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_failure_missing_output_timeout_and_cancel_process_group(tmp_path: Path) -> None:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    lib_dir = tmp_path / "lib"
    out_dir = tmp_path / "out"
    lib_dir.mkdir()
    out_dir.mkdir()
    source_pdf = _write_minimal_pdf(lib_dir / "paper.pdf")
    pid_file = tmp_path / "child.pid"

    # 1. Failing babeldoc with secret in stderr -> redacts secret, writes no fake output
    fail_bin = _write_fake_babeldoc(
        tmp_path / "fake-fail",
        "import sys\nsys.stderr.write('Auth failed Bearer supersecret9999 token=abcd1234\\n')\nsys.exit(2)\n",
    )
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"babeldoc_bin": str(fail_bin), "pdf_library_roots": str(lib_dir), "output_root": str(out_dir)})
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    r_fail = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": str(source_pdf)})
    assert r_fail.ok is True
    j_fail = await _wait_for_terminal_job(ctx, r_fail.data["job_id"])
    assert j_fail["status"] == "failed"
    assert "supersecret9999" not in j_fail["error"]
    assert "abcd1234" not in j_fail["error"]
    assert j_fail["dual_pdf_path"] == ""

    # 2. Exit 0 but corrupt/missing dual PDF -> fail closed with missing_output
    empty_bin = _write_fake_babeldoc(
        tmp_path / "fake-no-pdf",
        "import pathlib, sys\nout = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n(out / 'paper.zh.dual.pdf').write_bytes(b'BAD')\n",
    )
    ctx.set_config("babeldoc_bin", str(empty_bin))
    r_bad_out = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": str(source_pdf)})
    j_bad_out = await _wait_for_terminal_job(ctx, r_bad_out.data["job_id"])
    assert j_bad_out["status"] == "failed"
    assert j_bad_out["error_code"] == "missing_output"

    # 3. Timeout kills subprocess cleanly
    slow_bin = _write_fake_babeldoc(tmp_path / "fake-slow", "import time\ntime.sleep(30)\n")
    ctx.set_config("babeldoc_bin", str(slow_bin))
    r_to = await ctx.capabilities.call(
        "book.bilingual_pdf.v1", {"action": "submit", "source_path": str(source_pdf), "timeout_sec": 0.25}
    )
    j_to = await _wait_for_terminal_job(ctx, r_to.data["job_id"], timeout=5.0)
    assert j_to["status"] == "failed"
    assert j_to["error_code"] == "timeout"

    # 4. Cancel terminates entire process group (parent + spawned child process)
    pg_bin = _write_fake_babeldoc(
        tmp_path / "fake-pg",
        f"""
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path({str(pid_file)!r}).write_text(str(child.pid), encoding="utf-8")
time.sleep(60)
""",
    )
    ctx.set_config("babeldoc_bin", str(pg_bin))
    r_cancel = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "submit", "source_path": str(source_pdf)})
    cancel_jid = r_cancel.data["job_id"]
    deadline = time.monotonic() + 3.0
    while not pid_file.is_file() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert pid_file.is_file()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert _is_pid_alive(child_pid)

    c_res = await ctx.capabilities.call("book.bilingual_pdf.v1", {"action": "cancel", "job_id": cancel_jid})
    assert c_res.ok is True
    assert c_res.data["status"] == "cancelled"
    await asyncio.sleep(0.05)
    assert not _is_pid_alive(child_pid)

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
