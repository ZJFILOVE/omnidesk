"""OmniDesk Protocol v2 hybrid plugin for local PDF libraries and BabelDOC dual-column generation."""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

if __package__:
    from . import job_store, pdf_utils, runner, mineru_reader
    from .sources import BabelDocBookSource
else:
    _PLUGIN_ROOT = str(Path(__file__).resolve().parent)
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    import job_store  # type: ignore[import-not-found,no-redef]
    import pdf_utils  # type: ignore[import-not-found,no-redef]
    import runner  # type: ignore[import-not-found,no-redef]
    import mineru_reader
    from sources import BabelDocBookSource  # type: ignore[import-not-found,no-redef]

_SUPPORTED_BILINGUAL_ACTIONS = ("status", "submit", "job_status", "cancel", "list_jobs", "parse_page", "mineru_settings", "configure_mineru")
_SUPPORTED_SOURCE_ACTIONS = ("status", "list_books", "fetch_spread", "list_vocab")
_SEMVER_EXTRACT_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+[^\s]*)?)\b")


class PdfBabeldocPlugin(BasePlugin):
    """Provides `book.source.v1` and `book.bilingual_pdf.v1` using local BabelDOC CLI."""

    def __init__(self) -> None:
        self._book_source: BabelDocBookSource | None = None
        self._active_tokens: dict[str, runner.JobCancelToken] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._help_cache: dict[tuple[str, int, int], str] = {}

    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self._book_source = BabelDocBookSource(self._read_config, self._get_job_store)
        self._book_source.mineru = mineru_reader.MineruReader(ctx.data_dir, self._read_config, lambda: ctx.get_secret('mineru_api_token') or '')
        ctx.register_book_source(self._book_source)
        ctx.register_capability("book.source.v1", self.handle_book_source)
        ctx.register_capability("book.bilingual_pdf.v1", self.handle_bilingual_pdf)

    async def on_unload(self) -> None:
        for token in list(self._active_tokens.values()):
            token.cancel()
        self._active_tokens.clear()
        self._active_tasks.clear()
        self._help_cache.clear()
        self._book_source = None

    async def on_health_check(self) -> dict[str, Any]:
        cfg = self._read_config()
        raw_bin = str(cfg.get("babeldoc_bin") or "").strip()
        roots = pdf_utils.parse_roots_config(cfg.get("pdf_library_roots"))
        try:
            output_root = pdf_utils.parse_single_root(cfg.get("output_root"))
        except pdf_utils.PathContainmentError:
            output_root = None
        return {
            "ok": True,
            "plugin_id": self.ctx.plugin_id if self.ctx is not None else "plugin-pdf-babeldoc",
            "capabilities": ["book.source.v1", "book.bilingual_pdf.v1"],
            "babeldoc_bin_configured": bool(raw_bin),
            "babeldoc_bin_found": bool(pdf_utils.resolve_babeldoc_bin(raw_bin)),
            "library_roots_count": len(roots),
            "output_root_configured": output_root is not None,
            "active_jobs": len(self._active_tasks),
        }

    def _read_config(self) -> dict[str, Any]:
        return {} if self.ctx is None else dict(self.ctx.get_config())

    def _extra_secrets(self) -> tuple[str, ...]:
        vault = getattr(self.ctx, "_secret_vault", None) if self.ctx is not None else None
        getter = getattr(vault, "secret_values", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                return tuple(getter())
        return ()

    def _redact(self, text: str) -> str:
        return pdf_utils.redact_diagnostic(text, self._extra_secrets())

    def _get_job_store(self, output_root: Path | None = None) -> job_store.JobStore:
        assert self.ctx is not None
        if output_root is None:
            with contextlib.suppress(pdf_utils.PathContainmentError):
                output_root = pdf_utils.parse_single_root(self._read_config().get("output_root"))
        return job_store.JobStore(job_store.resolve_jobs_dir(self.ctx, output_root))

    async def _probe_help_text(self, resolved_bin: str) -> str:
        assert self.ctx is not None
        st = Path(resolved_bin).stat()
        key = (resolved_bin, st.st_mtime_ns, st.st_size)
        if key in self._help_cache:
            return self._help_cache[key]
        try:
            res = await self.ctx.exec_subprocess([resolved_bin, "--help"], timeout_sec=5.0)
        except (OSError, FileNotFoundError, PermissionError):
            return ""
        text = (res.stdout + "\n" + res.stderr).strip() if res.ok else ""
        if text:
            self._help_cache[key] = text
        return text

    async def handle_book_source(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Dispatch `book.source.v1` RPC actions (`list_books`, `fetch_spread`, `list_vocab`, `status`)."""
        if self.ctx is None or self._book_source is None:
            return CapabilityResult.fail("插件尚未加载", error_code="service_offline")
        payload = dict(req) if req is not None else {}
        default_action = "fetch_spread" if "book_id" in payload else "list_books"
        action = str(payload.get("action") or default_action).strip()
        roots = pdf_utils.parse_roots_config(self._read_config().get("pdf_library_roots"))

        if action == "status":
            if not roots:
                return CapabilityResult.fail("尚未配置 PDF 书库根目录（pdf_library_roots）", data={"configured": False, "roots": []}, error_code="not_configured")
            return CapabilityResult.ok({"configured": True, "roots": [str(r) for r in roots]})

        if action == "list_books":
            if not roots:
                return CapabilityResult.fail("尚未配置 PDF 书库根目录（pdf_library_roots）", data={"books": [], "count": 0}, error_code="not_configured")
            books = await self._book_source.list_books(
                format_filter=str(payload.get("format_filter") or ""),
                query=str(payload.get("query") or ""),
            )
            serialized = [{**b.model_dump(), "file_size": b.extra.get("file_size", 0)} for b in books]
            return CapabilityResult.ok({"books": serialized, "count": len(serialized)})

        if action == "fetch_spread":
            if not roots:
                return CapabilityResult.fail("尚未配置 PDF 书库根目录（pdf_library_roots）", error_code="not_configured")
            try:
                book_id = pdf_utils.validate_safe_identifier(payload.get("book_id"), "book_id")
            except ValueError as exc:
                return CapabilityResult.fail(str(exc), error_code="invalid_argument")
            raw_page = payload.get("page_number", payload.get("page", 1))
            if isinstance(raw_page, bool) or not isinstance(raw_page, int) or raw_page < 1:
                return CapabilityResult.fail("page_number 必须是 >= 1 的整数", error_code="invalid_argument")
            try:
                spread = await self._book_source.fetch_spread(book_id, page_number=raw_page)
            except FileNotFoundError as exc:
                return CapabilityResult.fail(self._redact(str(exc)), error_code="not_found")
            except (RuntimeError, OSError) as exc:
                return CapabilityResult.fail(self._redact(str(exc)), error_code="render_failed")
            except ValueError as exc:
                return CapabilityResult.fail(self._redact(str(exc)), error_code="invalid_argument")
            if spread.extra.get("needs_bilingual_pdf"):
                return CapabilityResult.fail(
                    "尚未生成 BabelDOC 双栏对照 PDF",
                    data={"status": "needs_bilingual_pdf", "needs_bilingual_pdf": True, "book_id": book_id, "page_number": raw_page, "spread": spread.model_dump()},
                    error_code="needs_bilingual_pdf",
                )
            return CapabilityResult.ok({"status": spread.extra.get("status", "ready"), "book_id": book_id, "page_number": raw_page, "spread": spread.model_dump()})

        if action == "list_vocab":
            cards = await self._book_source.list_vocab()
            return CapabilityResult.ok({"vocab": [c.model_dump() for c in cards], "count": len(cards)})

        return CapabilityResult.fail(
            f"不支持的 book.source.v1 动作: {action!r}",
            data={"supported_actions": list(_SUPPORTED_SOURCE_ACTIONS)},
            error_code="invalid_argument",
        )

    async def handle_bilingual_pdf(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Dispatch `book.bilingual_pdf.v1` capability actions."""
        if self.ctx is None:
            return CapabilityResult.fail("插件尚未加载", error_code="service_offline")
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        if action in {'mineru_settings', 'configure_mineru'}:
            if action == 'configure_mineru':
                patch = {}
                for key in ('mineru_wrapper', 'mineru_python', 'mineru_token_file'):
                    if key in payload:
                        value = payload[key]
                        if not isinstance(value, str) or len(value) > 4096 or '\x00' in value or (value.strip() and not Path(value.strip()).expanduser().is_absolute()):
                            return CapabilityResult.fail('路径须为绝对路径或留空', error_code='invalid_argument')
                        patch[key] = value.strip()
                token = payload.get('mineru_api_token')
                if token is not None and (not isinstance(token, str) or len(token) > 16384 or any(c.isspace() for c in token)):
                    return CapabilityResult.fail('MinerU Token 格式无效', error_code='invalid_argument')
                if patch:
                    self.ctx.set_config(patch)
                if token:
                    self.ctx.set_secret('mineru_api_token', token)
                elif payload.get('clear_token') is True:
                    self.ctx.set_secret('mineru_api_token', None)
            cfg = self._read_config()
            wrapper = cfg.get('mineru_wrapper') or str(Path.home() / 'Documents/GLM-OCR/.agent/skills/mineru-document-parser/scripts/mineru_api.py')
            python = cfg.get('mineru_python') or str(Path.home() / 'Projects/qwen-translator/runtime/pdf-venv/bin/python')
            token_file = cfg.get('mineru_token_file') or str(Path.home() / 'Documents/GLM-OCR/.mineru/token')
            return CapabilityResult.ok({**{key: cfg.get(key, '') for key in ('mineru_wrapper', 'mineru_python', 'mineru_token_file')},
                'token_configured': bool(self.ctx.get_secret('mineru_api_token')),
                'token_file_available': Path(token_file).expanduser().is_file(),
                'wrapper_available': Path(wrapper).expanduser().is_file(),
                'python_available': Path(python).expanduser().is_file(),
                'provider': 'mineru.net', 'upload_scope': 'current_page'})
        if action == "parse_page":
            page = payload.get('page_number', 1)
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                return CapabilityResult.fail('页码必须是正整数', error_code='invalid_argument')
            assert self._book_source is not None
            matched = next(((p, m) for p, m in self._book_source._scan_library()
                            if pdf_utils.make_book_id(p) == payload.get('book_id')), None)
            if matched is None:
                return CapabilityResult.fail('未找到书库中的 PDF', error_code='not_found')
            source, meta = matched
            if page > meta.total_pages:
                return CapabilityResult.fail('页码超出 PDF 范围', error_code='invalid_argument')
            try:
                parsed = await self._book_source.mineru.parse(source, page, upload=payload.get('upload_current_page') is True)
                return CapabilityResult.ok({'page': page, 'parser': 'mineru', 'format': parsed['format'],
                    'block_count': len(parsed['blocks']), 'image_count': sum(bool(b.image_url) for b in parsed['blocks'])})
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                return CapabilityResult.fail(self._redact(str(exc)) or 'MinerU 解析超时，稍后重试可继续已有任务', error_code='parse_failed')
        if action == "status":
            return await self._handle_status()
        if action == "submit":
            return await self._handle_submit(payload)
        if action == "job_status":
            return await self._handle_job_status(payload)
        if action == "cancel":
            return await self._handle_cancel(payload)
        if action == "list_jobs":
            return await self._handle_list_jobs(payload)
        return CapabilityResult.fail(
            f"不支持的 book.bilingual_pdf.v1 动作: {action!r}",
            data={"supported_actions": list(_SUPPORTED_BILINGUAL_ACTIONS)},
            error_code="invalid_argument",
        )

    async def _handle_status(self) -> CapabilityResult:
        assert self.ctx is not None
        cfg = self._read_config()
        raw_bin = str(cfg.get("babeldoc_bin") or "").strip()
        resolved_bin = pdf_utils.resolve_babeldoc_bin(raw_bin)
        roots = pdf_utils.parse_roots_config(cfg.get("pdf_library_roots"))
        try:
            output_root = pdf_utils.parse_single_root(cfg.get("output_root"))
        except pdf_utils.PathContainmentError as exc:
            return CapabilityResult.fail(self._redact(str(exc)), error_code="not_configured")

        base = {
            "configured": False,
            "babeldoc_found": False,
            "babeldoc_bin": raw_bin,
            "version": "",
            "pdf_library_roots": [str(r) for r in roots],
            "output_root": str(output_root) if output_root else "",
            "target_language": str(cfg.get("target_language") or "zh").strip() or "zh",
            "timeout_sec": cfg.get("timeout_sec", 600),
            "roots_configured": bool(roots),
            "output_root_configured": output_root is not None,
        }
        if not raw_bin or not resolved_bin:
            return CapabilityResult.fail("尚未配置可用的 BabelDOC 可执行文件（babeldoc_bin）", data=base, error_code="not_configured")
        try:
            res = await self.ctx.exec_subprocess([resolved_bin, "--version"], timeout_sec=5.0)
        except (OSError, FileNotFoundError, PermissionError) as exc:
            return CapabilityResult.fail(self._redact(f"无法执行 BabelDOC: {exc}"), data=base, error_code="not_configured")
        if not res.ok:
            diag = self._redact((res.stderr or res.stdout).strip())
            return CapabilityResult.fail(f"BabelDOC 版本探测失败: {diag or f'rc={res.returncode}'}", data=base, error_code="not_configured")

        raw_ver = (res.stdout.strip() or res.stderr.strip()).splitlines()[0].strip() if (res.stdout or res.stderr).strip() else "unknown"
        m_ver = _SEMVER_EXTRACT_RE.search(raw_ver)
        missing = [k for k, ok in (("pdf_library_roots", bool(roots)), ("output_root", output_root is not None)) if not ok]
        return CapabilityResult.ok({
            **base,
            "configured": True,
            "babeldoc_found": True,
            "babeldoc_bin": resolved_bin,
            "version": m_ver.group(1) if m_ver else raw_ver,
            "version_raw": raw_ver,
            "ready": not missing,
            "missing_config": missing,
        })

    async def _handle_submit(self, req: Mapping[str, Any]) -> CapabilityResult:
        assert self.ctx is not None
        book_lookup: dict[str, str] = {}
        if not req.get("source_path") and req.get("book_id") and self._book_source is not None:
            book_lookup = {b.id: b.file_path for b in await self._book_source.list_books()}

        plan, err = await runner.build_submission_plan(
            req=req,
            cfg=self._read_config(),
            book_lookup=book_lookup,
            probe_help_fn=self._probe_help_text,
            get_store_fn=self._get_job_store,
            redact_fn=self._redact,
            extra_secrets=self._extra_secrets(),
        )
        if err is not None or plan is None:
            return err or CapabilityResult.fail("参数校验失败", error_code="invalid_argument")

        store: job_store.JobStore = plan["store"]
        job_id: str = plan["job_id"]
        op_id: str | None = plan["operation_id"]
        glossary_csv: Path | None = plan["glossary_csv"]
        try:
            await store.reserve_and_create(plan["initial_record"], operation_id=op_id)
        except job_store.DuplicateOperationError as exc:
            if glossary_csv is not None:
                glossary_csv.unlink(missing_ok=True)
            return CapabilityResult.fail(
                f"重复提交 operation_id: {op_id}",
                data={"duplicate": True, "operation_id": op_id, "existing_job_id": str(exc.existing_job.get("job_id") or ""), "job": exc.existing_job},
                error_code="conflict",
            )

        token = runner.JobCancelToken()
        self._active_tokens[job_id] = token
        self._active_tasks[job_id] = self.ctx.spawn_task(
            runner.run_babeldoc_job(
                ctx=self.ctx,
                job_id=job_id,
                book_id=plan["book_id"],
                store=store,
                args=plan["args"],
                source_path=plan["resolved_source"],
                output_dir=plan["resolved_output"],
                target_language=plan["target_lang"],
                timeout_sec=plan["timeout_sec"],
                token=token,
                redact_fn=self._redact,
                on_finish=self._on_job_finish,
            ),
            name=f"babeldoc:{job_id}",
        )
        return CapabilityResult.ok({
            "job_id": job_id,
            "operation_id": op_id or "",
            "book_id": plan["book_id"],
            "status": "queued",
            "progress": 0.0,
            "source_path": str(plan["resolved_source"]),
            "output_dir": str(plan["resolved_output"]),
            "target_language": plan["target_lang"],
            "glossary_path": str(glossary_csv) if glossary_csv else "",
        })

    def _on_job_finish(self, job_id: str) -> None:
        self._active_tokens.pop(job_id, None)
        self._active_tasks.pop(job_id, None)

    def _lookup_job(self, store: job_store.JobStore, req: Mapping[str, Any]) -> tuple[dict[str, Any] | None, CapabilityResult | None]:
        raw_job_id = str(req.get("job_id") or "").strip()
        raw_op_id = str(req.get("operation_id") or "").strip()
        if raw_job_id:
            try:
                return store.read_job(pdf_utils.validate_safe_identifier(raw_job_id, "job_id")), None
            except (ValueError, pdf_utils.PathContainmentError) as exc:
                return None, CapabilityResult.fail(self._redact(str(exc)), error_code="invalid_argument")
        if raw_op_id:
            try:
                return store.find_by_operation_id(pdf_utils.validate_safe_identifier(raw_op_id, "operation_id")), None
            except (ValueError, pdf_utils.PathContainmentError) as exc:
                return None, CapabilityResult.fail(self._redact(str(exc)), error_code="invalid_argument")
        return None, CapabilityResult.fail("缺少必填参数 job_id 或 operation_id", error_code="invalid_argument")

    async def _handle_job_status(self, req: Mapping[str, Any]) -> CapabilityResult:
        record, err = self._lookup_job(self._get_job_store(), req)
        if err is not None:
            return err
        if record is None:
            return CapabilityResult.fail("未找到指定的 BabelDOC 任务", error_code="not_found")
        return CapabilityResult.ok(record)

    async def _handle_cancel(self, req: Mapping[str, Any]) -> CapabilityResult:
        store = self._get_job_store()
        record, err = self._lookup_job(store, req)
        if err is not None:
            return err
        if record is None:
            return CapabilityResult.fail("未找到指定的 BabelDOC 任务", error_code="not_found")
        job_id = str(record["job_id"])
        if record.get("status") in job_store.TERMINAL_STATES:
            return CapabilityResult.ok({**record, "already_terminal": True})

        if (token := self._active_tokens.get(job_id)) is not None:
            token.cancel()
        if (task := self._active_tasks.get(job_id)) is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        latest = store.read_job(job_id)
        if latest is None or latest.get("status") not in job_store.TERMINAL_STATES:
            latest = store.update_job(
                job_id, status="cancelled", stage="cancelled", error_code="cancelled", error="任务已取消", finished_at=time.time()
            )
        return CapabilityResult.ok(latest or record)

    async def _handle_list_jobs(self, req: Mapping[str, Any]) -> CapabilityResult:
        status_filter = str(req.get("status") or "").strip()
        raw_limit = req.get("limit")
        limit: int | None = None
        if raw_limit is not None:
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 0:
                return CapabilityResult.fail("limit 必须是非负整数", error_code="invalid_argument")
            limit = raw_limit
        try:
            jobs = self._get_job_store().list_jobs(status=status_filter, limit=limit)
        except ValueError as exc:
            return CapabilityResult.fail(str(exc), error_code="invalid_argument")
        return CapabilityResult.ok({"jobs": jobs, "count": len(jobs)})


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `PdfBabeldocPlugin` instance."""
    return PdfBabeldocPlugin()
