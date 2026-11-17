"""Background BabelDOC subprocess execution, cancellation token, and submission helpers."""

from __future__ import annotations

import asyncio
import math
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from . import artifacts, job_store, pdf_utils
else:
    import artifacts  # type: ignore[import-not-found,no-redef]
    import job_store  # type: ignore[import-not-found,no-redef]
    import pdf_utils  # type: ignore[import-not-found,no-redef]

_SUPPORTED_LAYOUTS = frozenset({"", "dual_side_by_side", "alternating"})


class JobCancelToken:
    """Async cancellation token compatible with `PluginContext.exec_subprocess`."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


def validate_submit_inputs(
    req: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    book_lookup: dict[str, str],
) -> tuple[dict[str, Any] | None, CapabilityResult | None]:
    """Validate tool config, allowed roots, PDF magic, output writability, and options."""
    resolved_bin = pdf_utils.resolve_babeldoc_bin(cfg.get("babeldoc_bin"))
    if not resolved_bin:
        return None, CapabilityResult.fail("尚未配置可用的 BabelDOC 可执行文件（babeldoc_bin）", error_code="not_configured")
    roots = pdf_utils.parse_roots_config(cfg.get("pdf_library_roots"))
    try:
        output_root = pdf_utils.parse_single_root(cfg.get("output_root"))
    except pdf_utils.PathContainmentError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(str(exc)), error_code="path_safety")
    if not roots or output_root is None:
        return None, CapabilityResult.fail("必须先配置允许的 pdf_library_roots 与 output_root", error_code="not_configured")

    layout = str(req.get("layout") or "").strip()
    if layout not in _SUPPORTED_LAYOUTS:
        return None, CapabilityResult.fail(f"不支持的排版模式: {layout!r}", error_code="unsupported_option")
    use_alternating = bool(req.get("use_alternating_pages", False) or layout == "alternating")

    raw_source = str(req.get("source_path") or "").strip()
    if not raw_source and req.get("book_id"):
        raw_source = book_lookup.get(str(req["book_id"]).strip(), "")
    if not raw_source:
        return None, CapabilityResult.fail("缺少必填参数 source_path", error_code="invalid_argument")

    try:
        resolved_source = pdf_utils.resolve_contained_path(raw_source, roots, allow_relative=True)
        pdf_utils.verify_pdf_magic(resolved_source)
    except pdf_utils.PathContainmentError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(str(exc)), error_code="path_safety")
    except FileNotFoundError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(str(exc)), error_code="not_found")
    except ValueError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(str(exc)), error_code="invalid_argument")

    raw_out = str(req.get("output_dir") or "").strip()
    try:
        resolved_output = (
            pdf_utils.resolve_contained_path(raw_out, [output_root], allow_relative=True) if raw_out else output_root
        )
    except pdf_utils.PathContainmentError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(str(exc)), error_code="path_safety")

    if resolved_output.exists() and not resolved_output.is_dir():
        return None, CapabilityResult.fail("output_dir 不是目录", error_code="invalid_argument")
    try:
        resolved_output.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=resolved_output, prefix=".omnidesk-probe-", delete=True):
            pass
    except PermissionError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(f"输出目录不可写: {exc}"), error_code="permission_denied")
    except OSError as exc:
        return None, CapabilityResult.fail(pdf_utils.redact_diagnostic(f"输出目录校验失败: {exc}"), error_code="invalid_argument")

    try:
        target_lang = pdf_utils.validate_language_code(req.get("target_language"), default=str(cfg.get("target_language") or "zh"))
        source_lang = pdf_utils.validate_language_code(req.get("source_language"), default="") if req.get("source_language") else ""
    except ValueError as exc:
        return None, CapabilityResult.fail(str(exc), error_code="invalid_argument")

    raw_timeout = req.get("timeout_sec", cfg.get("timeout_sec", 600))
    if (
        isinstance(raw_timeout, bool)
        or not isinstance(raw_timeout, (int, float))
        or not math.isfinite(float(raw_timeout))
        or float(raw_timeout) <= 0
    ):
        return None, CapabilityResult.fail("timeout_sec 必须是大于 0 的数字", error_code="invalid_argument")

    operation_id: str | None = None
    if req.get("operation_id") is not None and str(req.get("operation_id")).strip():
        try:
            operation_id = pdf_utils.validate_safe_identifier(str(req["operation_id"]), "operation_id")
        except ValueError as exc:
            return None, CapabilityResult.fail(str(exc), error_code="invalid_argument")

    return {
        "resolved_bin": resolved_bin,
        "output_root": output_root,
        "resolved_source": resolved_source,
        "resolved_output": resolved_output,
        "target_lang": target_lang,
        "source_lang": source_lang,
        "timeout_sec": float(raw_timeout),
        "operation_id": operation_id,
        "use_alternating": use_alternating,
    }, None


async def build_submission_plan(
    *,
    req: Mapping[str, Any],
    cfg: Mapping[str, Any],
    book_lookup: dict[str, str],
    probe_help_fn: Callable[[str], Awaitable[str]],
    get_store_fn: Callable[[Path | None], job_store.JobStore],
    redact_fn: Callable[[str], str],
    extra_secrets: tuple[str, ...],
) -> tuple[dict[str, Any] | None, CapabilityResult | None]:
    """Validate submit request, check CLI glossary support, prepare args & initial job record."""
    params, err = validate_submit_inputs(req, cfg, book_lookup=book_lookup)
    if err is not None or params is None:
        return None, err or CapabilityResult.fail("参数校验失败", error_code="invalid_argument")

    glossary_val = req.get("glossary")
    has_glossary = glossary_val not in (None, "", [], {})
    help_text = await probe_help_fn(params["resolved_bin"]) if (has_glossary or params["use_alternating"]) else ""
    has_cli_flags = ("--" in help_text) and ("usage:" in help_text.lower() or "options:" in help_text.lower())

    glossary_flag = "--glossary-files"
    if has_glossary and has_cli_flags:
        if "--glossary-files" in help_text:
            glossary_flag = "--glossary-files"
        elif "--glossary" in help_text:
            glossary_flag = "--glossary"
        else:
            return None, CapabilityResult.fail("当前 BabelDOC CLI 不支持 glossary 术语锁定参数", error_code="unsupported_option")
    if params["use_alternating"] and has_cli_flags and "--use-alternating-pages-dual" not in help_text:
        return None, CapabilityResult.fail("当前 BabelDOC CLI 不支持 alternating 页码模式", error_code="unsupported_option")

    store = get_store_fn(params["output_root"])
    job_id = f"bdoc-{uuid.uuid4().hex[:16]}"
    try:
        glossary_csv = artifacts.prepare_glossary_csv(
            glossary_val, inputs_dir=store.inputs_dir, job_id=job_id, target_language=params["target_lang"]
        )
    except artifacts.UnsupportedOptionError as exc:
        return None, CapabilityResult.fail(redact_fn(str(exc)), error_code="unsupported_option")
    except pdf_utils.PathContainmentError as exc:
        return None, CapabilityResult.fail(redact_fn(str(exc)), error_code="path_safety")
    except FileNotFoundError as exc:
        return None, CapabilityResult.fail(redact_fn(str(exc)), error_code="not_found")
    except (ValueError, OSError) as exc:
        return None, CapabilityResult.fail(redact_fn(str(exc)), error_code="invalid_argument")

    args: list[str] = [
        params["resolved_bin"],
        "--files",
        str(params["resolved_source"]),
        "--output",
        str(params["resolved_output"]),
        "--lang-out",
        params["target_lang"],
    ]
    if params["source_lang"]:
        args.extend(["--lang-in", params["source_lang"]])
    if params["use_alternating"]:
        args.append("--use-alternating-pages-dual")
    if glossary_csv is not None:
        args.extend([glossary_flag, str(glossary_csv)])

    book_id = pdf_utils.make_book_id(params["resolved_source"])
    now = time.time()
    op_id = params["operation_id"]
    initial_record: dict[str, Any] = {
        "job_id": job_id,
        "operation_id": op_id or "",
        "book_id": book_id,
        "status": "queued",
        "progress": 0.0,
        "stage": "queued",
        "message": "任务已入队",
        "source_path": str(params["resolved_source"]),
        "output_dir": str(params["resolved_output"]),
        "target_language": params["target_lang"],
        "glossary_path": str(glossary_csv) if glossary_csv else "",
        "dual_pdf_path": "",
        "page_map_path": "",
        "error": "",
        "error_code": "",
        "command": pdf_utils.redact_command(args, extra_secrets),
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    return {
        **params,
        "job_id": job_id,
        "book_id": book_id,
        "store": store,
        "args": args,
        "glossary_csv": glossary_csv,
        "initial_record": initial_record,
    }, None


async def run_babeldoc_job(
    *,
    ctx: PluginContext,
    job_id: str,
    book_id: str,
    store: job_store.JobStore,
    args: list[str],
    source_path: Path,
    output_dir: Path,
    target_language: str,
    timeout_sec: float,
    token: JobCancelToken,
    redact_fn: Callable[[str], str],
    on_finish: Callable[[str], None],
) -> None:
    """Execute BabelDOC subprocess in background, validate outputs, and update 0600 job file."""
    try:
        if token.is_cancelled:
            store.update_job(
                job_id, status="cancelled", stage="cancelled", error_code="cancelled", error="任务在启动前已取消", finished_at=time.time()
            )
            return

        store.update_job(
            job_id, status="running", stage="running", progress=0.1, started_at=time.time(), message="正在调用 BabelDOC 生成双栏对照 PDF"
        )
        before_snapshot = artifacts.snapshot_directory_mtimes(output_dir)
        res = await ctx.exec_subprocess(args, timeout_sec=timeout_sec, cancel_token=token)

        if res.cancelled or token.is_cancelled:
            store.update_job(
                job_id, status="cancelled", stage="cancelled", error_code="cancelled", error="任务已取消并终止进程组", finished_at=time.time()
            )
            return

        if res.timed_out:
            store.update_job(
                job_id, status="failed", stage="failed", error_code="timeout", error=redact_fn(f"BabelDOC 执行超时（>{timeout_sec}s）"), finished_at=time.time()
            )
            return

        if not res.ok:
            raw_diag = "\n".join(part for part in (res.stderr.strip(), res.stdout.strip()) if part).strip()
            diag = redact_fn(raw_diag[-1200:] if raw_diag else f"BabelDOC 退出码 {res.returncode}")
            lower = diag.lower()
            err_code = (
                "unsupported_option"
                if ("glossary" in lower and any(tok in lower for tok in ("unrecognized", "unknown option", "no such option")))
                else "subprocess_failed"
            )
            store.update_job(
                job_id, status="failed", stage="failed", error_code=err_code, error=diag, returncode=res.returncode, finished_at=time.time()
            )
            return

        dual_pdf, page_map = artifacts.discover_babeldoc_outputs(
            output_dir, source_path, target_language, before_snapshot=before_snapshot
        )
        if dual_pdf is None:
            store.update_job(
                job_id,
                status="failed",
                stage="failed",
                error_code="missing_output",
                error="BabelDOC 返回成功，但未在输出目录检测到有效的双栏 PDF 产物",
                finished_at=time.time(),
            )
            return

        store.update_job(
            job_id,
            status="succeeded",
            stage="done",
            progress=1.0,
            dual_pdf_path=str(dual_pdf),
            page_map_path=str(page_map) if page_map else "",
            message="BabelDOC 双栏 PDF 生成完成",
            finished_at=time.time(),
        )
        await ctx.emit(
            "entity.book.open",
            {
                "book_id": book_id,
                "file_path": str(dual_pdf),
                "format": "pdf",
                "page": 1,
                "job_id": job_id,
                "source_path": str(source_path),
                "page_map_path": str(page_map) if page_map else "",
            },
        )
    except asyncio.CancelledError:
        cur = store.read_job(job_id)
        if cur is not None and cur.get("status") not in job_store.TERMINAL_STATES:
            store.update_job(
                job_id, status="cancelled", stage="cancelled", error_code="cancelled", error="任务已取消", finished_at=time.time()
            )
        raise
    except Exception as exc:
        store.update_job(
            job_id,
            status="failed",
            stage="failed",
            error_code="plugin_fault",
            error=redact_fn(str(exc) or type(exc).__name__),
            finished_at=time.time(),
        )
    finally:
        on_finish(job_id)
