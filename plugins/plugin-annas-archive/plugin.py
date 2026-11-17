"""OmniDesk v2 `plugin-annas-archive` (`book.search.v1`) hybrid book search & safe download plugin."""

from __future__ import annotations

import json
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from annas_auth import AuthMaterial, resolve_auth_material  # noqa: E402
from annas_http import HttpFetchError, download_to_target, fetch_with_chrome_fallback  # noqa: E402
from annas_parser import (  # noqa: E402
    clean_text,
    extract_slow_download_link,
    join_base_url,
    normalize_format,
    parse_annas_detail_response,
    parse_annas_item_id,
    parse_annas_search_response,
    resolve_token_and_format,
)
from annas_security import (  # noqa: E402
    SUPPORTED_BOOK_FORMATS,
    BookSearchSecurityError,
    Origin,
    finalize_downloaded_file,
    parse_origin,
    redact_text,
    resolve_download_target,
    sanitize_filename,
    scrub_url_secrets,
    validate_url,
)
from annas_ui import build_annas_ui_schema, forward_book_handoff  # noqa: E402

_SUPPORTED_ACTIONS = ("mirrors", "configure_mirror", "search", "detail", "download", "open_in_babeldoc", "open_in_ebook", "send_to_nas", "status")
_MAX_CACHE = 256
MIRRORS = [{'url': 'https://annas-archive.gl', 'label': 'annas-archive.gl'}, {'url': 'https://annas-archive.pk', 'label': 'annas-archive.pk'}, {'url': 'https://annas-archive.gd', 'label': 'annas-archive.gd'}, {'url': 'https://annas-archive.is', 'label': 'annas-archive.is · 未核实归属'}]


class AnnasArchivePlugin(BasePlugin):
    """Anna's Archive search, MD5 detail inspection, verified download, and workbench handoff."""

    def __init__(self) -> None:
        self._search_cache: dict[str, dict[str, Any]] = {}
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._download_records: dict[str, dict[str, Any]] = {}
        self._last_results: list[dict[str, Any]] = []

    async def on_load(self, ctx: PluginContext) -> None:
        """Register `book.search.v1` and declarative BookWorkbench tab without blocking I/O."""
        await super().on_load(ctx)
        ctx.register_capability("book.search.v1", self.handle_book_search)
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        """Release transient in-memory search and download caches."""
        self._search_cache.clear()
        self._detail_cache.clear()
        self._download_records.clear()
        self._last_results.clear()

    async def on_health_check(self) -> dict[str, Any]:
        """Report local readiness without touching network or exposing secrets."""
        endpoint, _ = self._resolve_endpoint(required=False)
        download_root = str(self._cfg("download_root", "") or "").strip()
        caps = self.ctx.capabilities if self.ctx is not None else None
        return {
            "ok": True,
            "plugin_id": self.ctx.plugin_id if self.ctx is not None else "plugin-annas-archive",
            "capabilities": ["book.search.v1"],
            "endpoint_configured": bool(endpoint),
            "endpoint": scrub_url_secrets(endpoint),
            "download_root_configured": bool(download_root),
            "has_api_key": bool(self.ctx and self.ctx.get_secret("api_key")),
            "has_cookie": bool(self.ctx and (self.ctx.get_secret("cookie_header") or self.ctx.get_secret("cookie"))),
            "optional_capabilities": {
                "web_cookies": bool(caps and caps.has("web.cookies.v1")),
                "web_fetch": bool(caps and caps.has("web.fetch.v1")),
                "babeldoc": bool(caps and caps.has("book.bilingual_pdf.v1")),
                "ebook": bool(caps and any(caps.has(c) for c in ("book.ebook.v1", "book.source.v1", "book.open.v1"))),
                "nas": bool(caps and any(caps.has(c) for c in ("book.sync.v1", "nas.connection.v1"))),
            },
        }

    def _cfg(self, key: str, default: Any = None) -> Any:
        return default if self.ctx is None else self.ctx.get_config(key, default)

    def _resolve_endpoint(self, *, required: bool = True) -> tuple[str, frozenset[Origin]]:
        for key in ("mirror_url", "mirror_endpoint", "base_url"):
            raw = str(self._cfg(key, "") or "").strip()
            if raw:
                try:
                    return raw.rstrip("/"), frozenset({parse_origin(raw)})
                except BookSearchSecurityError:
                    if required:
                        raise
                    return raw, frozenset()
        if required:
            raise BookSearchSecurityError("not_configured", "尚未配置 Anna's Archive base_url 或 mirror_url")
        return "", frozenset()

    def _timeout_sec(self) -> float:
        try:
            val = float(self._cfg("request_timeout_sec", 15) or 15)
        except (TypeError, ValueError):
            val = 15.0
        return max(0.05, min(300.0, val))

    def _max_bytes(self, req: Mapping[str, Any] | None = None) -> int:
        raw_mb = self._cfg("max_download_mb", 100)
        if isinstance(raw_mb, bool) or not isinstance(raw_mb, (int, float)) or raw_mb <= 0:
            raise BookSearchSecurityError("invalid_argument", "max_download_mb 必须为正整数")
        cap = int(raw_mb * 1024 * 1024)
        if req and req.get("max_bytes") is not None:
            req_cap = req.get("max_bytes")
            if isinstance(req_cap, bool) or not isinstance(req_cap, int) or req_cap <= 0:
                raise BookSearchSecurityError("invalid_argument", "max_bytes 必须为正整数")
            cap = min(cap, req_cap)
        return cap

    def _cache_put(self, store: dict[str, dict[str, Any]], key: str, value: dict[str, Any]) -> None:
        if len(store) >= _MAX_CACHE and key not in store:
            store.pop(next(iter(store)))
        store[key] = value

    def _fail_from_exc(self, exc: BookSearchSecurityError | HttpFetchError, auth: AuthMaterial | None = None) -> CapabilityResult:
        secrets = auth.extra_secrets() if auth is not None else ()
        return CapabilityResult.fail(redact_text(str(exc), secrets), error_code=exc.error_code)

    async def handle_book_search(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `book.search.v1` actions for Anna's Archive."""
        payload = dict(req) if req is not None else {}
        action = clean_text(payload.get("action"))
        if action in {"mirrors", "configure_mirror"}:
            if self.ctx is None:
                return CapabilityResult.fail("服务未加载", error_code="service_offline")
            if action == "configure_mirror":
                raw = payload.get("mirror_url")
                if not isinstance(raw, str) or not raw.strip():
                    return CapabilityResult.fail("请填写镜像地址", error_code="invalid_argument")
                try:
                    url = raw.strip().rstrip("/")
                    parsed = urllib.parse.urlsplit(url)
                    if parsed.query or parsed.fragment:
                        return CapabilityResult.fail("镜像地址不能包含查询参数或片段", error_code="invalid_argument")
                    validate_url(url, configured_origins=frozenset({parse_origin(url)}), resolve_dns=False)
                    old_url, _ = self._resolve_endpoint(required=False)
                    patch = {"mirror_url": url}
                    # Vault credentials belong to the old origin; Chrome cookies are domain scoped.
                    if old_url and not self._cfg("vault_auth_origin", ""):
                        old = parse_origin(old_url)
                        patch["vault_auth_origin"] = f"{old.scheme}://{old.host}:{old.port}"
                    self.ctx.set_config(patch)
                    self._search_cache.clear()
                    self._detail_cache.clear()
                    self._last_results = []
                except BookSearchSecurityError as exc:
                    return self._fail_from_exc(exc)
            endpoint, _ = self._resolve_endpoint(required=False)
            return CapabilityResult.ok({"mirror_url": endpoint, "mirrors": MIRRORS})
        if action == "search":
            return await self._handle_search(payload)
        if action == "detail":
            return await self._handle_detail(payload)
        if action == "download":
            return await self._handle_download(payload)
        if action == "open_in_babeldoc":
            return await forward_book_handoff(
                self.ctx, payload, ("book.bilingual_pdf.v1",), "BabelDOC 双栏翻译",
                default_action="translate", download_records=self._download_records, detail_cache=self._detail_cache,
            )
        if action == "open_in_ebook":
            return await forward_book_handoff(
                self.ctx, payload, ("book.ebook.v1", "book.source.v1", "book.open.v1"), "EPUB 书架阅读",
                default_action="open", download_records=self._download_records, detail_cache=self._detail_cache,
            )
        if action == "send_to_nas":
            return await forward_book_handoff(
                self.ctx, payload, ("book.sync.v1", "nas.connection.v1"), "飞牛 NAS 同步",
                default_action="sync_book", download_records=self._download_records, detail_cache=self._detail_cache,
            )
        if action == "status":
            return CapabilityResult.ok(await self.on_health_check())
        return CapabilityResult.fail(
            f"不支持的 book.search.v1 动作: {action!r}",
            data={"supported_actions": list(_SUPPORTED_ACTIONS)},
            error_code="invalid_argument",
        )

    async def _handle_search(self, payload: Mapping[str, Any]) -> CapabilityResult:
        query = clean_text(payload.get("query"))
        if not query:
            return CapabilityResult.fail("search 缺少必填参数 query", error_code="invalid_argument")
        fmt = normalize_format(payload.get("format"))
        if fmt and fmt not in SUPPORTED_BOOK_FORMATS:
            return CapabilityResult.fail(f"不支持的格式过滤: {fmt!r}", error_code="invalid_argument")
        raw_limit = payload.get("limit", 20)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit <= 0:
            return CapabilityResult.fail("limit 必须为正整数", error_code="invalid_argument")
        limit = min(100, raw_limit)
        raw_page = payload.get("page", 1)
        page = raw_page if isinstance(raw_page, int) and not isinstance(raw_page, bool) and raw_page >= 1 else 1

        try:
            endpoint, origins = self._resolve_endpoint()
        except BookSearchSecurityError as exc:
            return self._fail_from_exc(exc)

        auth = await resolve_auth_material(self.ctx, endpoint)
        q_pairs: list[tuple[str, str]] = [("q", query), ("page", str(page))]
        if fmt:
            q_pairs.append(("ext", fmt))
        search_path = endpoint if urllib.parse.urlsplit(endpoint).path.lower().endswith("/search") else f"{endpoint}/search"
        search_url = f"{search_path}?{urllib.parse.urlencode(q_pairs)}"

        try:
            resp = await fetch_with_chrome_fallback(
                self.ctx, search_url, auth=auth, timeout_sec=self._timeout_sec(),
                max_bytes=8 * 1024 * 1024, configured_origins=origins,
                prefer_chrome_fetch=bool(self._cfg("prefer_chrome_fetch", False)),
            )
        except (HttpFetchError, BookSearchSecurityError) as exc:
            return self._fail_from_exc(exc, auth)

        results: list[dict[str, Any]] = []
        for item in parse_annas_search_response(resp.body, resp.content_type, endpoint):
            if fmt and item.get("format") and item["format"] != fmt:
                continue
            try:
                if item.get("detail_url"):
                    item["detail_url"] = scrub_url_secrets(
                        validate_url(item["detail_url"], configured_origins=origins, resolve_dns=False)
                    )
            except BookSearchSecurityError:
                item["detail_url"] = ""
            results.append(item)
            self._cache_put(self._search_cache, item["item_id"], item)
            if len(results) >= limit:
                break

        self._last_results = list(results)
        return CapabilityResult.ok({
            "results": results, "count": len(results), "query": query, "format": fmt,
            "limit": limit, "endpoint": scrub_url_secrets(endpoint), "source": "plugin-annas-archive",
        })

    async def _handle_detail(self, payload: Mapping[str, Any]) -> CapabilityResult:
        item_id = parse_annas_item_id(clean_text(payload.get("item_id") or payload.get("id") or payload.get("md5")))
        if not item_id:
            return CapabilityResult.fail("无效或缺失的 item_id", error_code="invalid_argument")

        try:
            endpoint, origins = self._resolve_endpoint()
            explicit_detail = clean_text(payload.get("detail_url"))
            if explicit_detail:
                detail_url = validate_url(explicit_detail, configured_origins=origins, resolve_dns=False)
            elif item_id in self._search_cache and self._search_cache[item_id].get("detail_url"):
                detail_url = validate_url(self._search_cache[item_id]["detail_url"], configured_origins=origins, resolve_dns=False)
            else:
                detail_url = validate_url(join_base_url(endpoint, f"/md5/{item_id}"), configured_origins=origins, resolve_dns=False)
        except BookSearchSecurityError as exc:
            return self._fail_from_exc(exc)

        auth = await resolve_auth_material(self.ctx, endpoint)
        try:
            resp = await fetch_with_chrome_fallback(
                self.ctx, detail_url, auth=auth, timeout_sec=self._timeout_sec(),
                max_bytes=4 * 1024 * 1024, configured_origins=origins,
                prefer_chrome_fetch=bool(self._cfg("prefer_chrome_fetch", False)),
            )
        except (HttpFetchError, BookSearchSecurityError) as exc:
            return self._fail_from_exc(exc, auth)

        detail_obj = parse_annas_detail_response(
            resp.body, resp.content_type, endpoint, requested_id=item_id, detail_url=scrub_url_secrets(detail_url)
        )
        if detail_obj is None:
            return CapabilityResult.fail(f"未找到图书 {item_id}", error_code="not_found", item_id=item_id)
        self._cache_put(self._detail_cache, item_id, detail_obj)
        return CapabilityResult.ok(detail_obj)

    async def _resolve_fast_api_download(
        self, endpoint: str, item_id: str, *, auth: AuthMaterial, origins: frozenset[Origin]
    ) -> str | None:
        if not auth.api_key or not item_id:
            return None
        fast_url = f"{endpoint}/dyn/api/fast_download.json?{urllib.parse.urlencode({'md5': item_id, 'key': auth.api_key})}"
        try:
            resp = await fetch_with_chrome_fallback(
                self.ctx, fast_url, auth=auth, timeout_sec=self._timeout_sec(),
                max_bytes=256 * 1024, configured_origins=origins, accept="application/json",
            )
        except HttpFetchError as exc:
            if exc.error_code == "not_found":
                return None
            raise
        data = json.loads(resp.body.decode("utf-8"))
        if isinstance(data, Mapping):
            err = clean_text(data.get("error"))
            if err:
                code = "quota_exceeded" if "quota" in err.lower() or "left" in err.lower() else "auth_required"
                raise HttpFetchError(code, redact_text(err, auth.extra_secrets()))
            dl_url = clean_text(data.get("download_url"))
            if dl_url:
                return validate_url(join_base_url(endpoint, dl_url), configured_origins=origins, resolve_dns=False)
        return None

    async def _handle_download(self, payload: Mapping[str, Any]) -> CapabilityResult:
        raw_id = clean_text(payload.get("item_id") or payload.get("id") or payload.get("md5"))
        req_token = clean_text(payload.get("download_token"))
        if not raw_id and not req_token:
            return CapabilityResult.fail("download 需要 item_id 或 download_token", error_code="invalid_argument")
        item_id = parse_annas_item_id(raw_id) if raw_id else ""
        if raw_id and not item_id:
            return CapabilityResult.fail("无效的 item_id", error_code="invalid_argument")
        effective_id = item_id or "download"
        req_fmt = normalize_format(payload.get("format"))
        if req_fmt and req_fmt not in SUPPORTED_BOOK_FORMATS:
            return CapabilityResult.fail(f"不支持的下载格式: {req_fmt!r}", error_code="invalid_argument")

        try:
            endpoint, origins = self._resolve_endpoint()
            max_bytes = self._max_bytes(payload)
            download_root = str(self._cfg("download_root", "") or "").strip()
            if not download_root:
                raise BookSearchSecurityError("not_configured", "尚未配置 download_root，无法执行下载")
        except BookSearchSecurityError as exc:
            return self._fail_from_exc(exc)

        meta = self._detail_cache.get(effective_id) or self._search_cache.get(effective_id)
        if not req_token and meta is None and item_id:
            detail_res = await self._handle_detail({"item_id": item_id})
            if not detail_res.ok:
                return detail_res
            meta = detail_res.data

        auth = await resolve_auth_material(self.ctx, endpoint)
        dest_raw = payload.get("destination")
        dl_token, matched_fmt = resolve_token_and_format(meta, req_token, req_fmt, dest_raw, SUPPORTED_BOOK_FORMATS)
        default_name = sanitize_filename(clean_text((meta or {}).get("title")) or effective_id, ext=matched_fmt or "epub")

        try:
            target_path = resolve_download_target(download_root, str(dest_raw) if dest_raw is not None else None, default_filename=default_name)
            dl_url = ""
            if auth.api_key and item_id and (not req_token or req_token.startswith("fast:")):
                dl_url = await self._resolve_fast_api_download(endpoint, item_id, auth=auth, origins=origins) or ""
            if not dl_url:
                if not dl_token:
                    return CapabilityResult.fail("缺少有效的 download_token", error_code="not_found", item_id=effective_id)
                dl_url = validate_url(join_base_url(endpoint, dl_token), configured_origins=origins, resolve_dns=False)
            tmp_path, sha256_hex, size_bytes, final_url, content_type = await download_to_target(
                self.ctx, dl_url, target_path, auth=auth, timeout_sec=self._timeout_sec(), max_bytes=max_bytes, configured_origins=origins
            )
        except (BookSearchSecurityError, HttpFetchError, json.JSONDecodeError) as exc:
            err_obj = exc if isinstance(exc, (BookSearchSecurityError, HttpFetchError)) else HttpFetchError("service_offline", "fast_download 响应解析失败")
            return self._fail_from_exc(err_obj, auth)

        try:
            if "html" in content_type.lower() and "/slow_download/" in final_url and size_bytes < 512 * 1024:
                direct_href = extract_slow_download_link(tmp_path.read_bytes())
                if direct_href:
                    tmp_path.unlink(missing_ok=True)
                    next_url = validate_url(join_base_url(endpoint, direct_href), configured_origins=origins, resolve_dns=False)
                    tmp_path, sha256_hex, size_bytes, _, _ = await download_to_target(
                        self.ctx, next_url, target_path, auth=auth, timeout_sec=self._timeout_sec(), max_bytes=max_bytes, configured_origins=origins
                    )
            final_fmt = finalize_downloaded_file(tmp_path, target_path, matched_fmt or None, max_bytes)
        except (BookSearchSecurityError, HttpFetchError) as exc:
            tmp_path.unlink(missing_ok=True)
            return self._fail_from_exc(exc, auth)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            return CapabilityResult.fail(redact_text(f"下载校验失败: {exc}", auth.extra_secrets()), error_code="invalid_magic")

        record = {
            "item_id": effective_id, "format": final_fmt, "file_path": str(target_path), "local_path": str(target_path),
            "sha256": sha256_hex, "size_bytes": size_bytes, "source": "plugin-annas-archive",
        }
        self._download_records[effective_id] = record
        return CapabilityResult.ok(record)

    def _build_ui_schema(self) -> dict[str, Any]:
        endpoint, _ = self._resolve_endpoint(required=False)
        return build_annas_ui_schema(
            endpoint=scrub_url_secrets(endpoint),
            download_root=str(self._cfg("download_root", "") or ""),
            last_results=self._last_results,
        )

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="annas-archive-search", title="Anna's Archive", domain="book", badge="ANNA",
            ui_schema=self._build_ui_schema(), render=self._build_ui_schema, on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        state_map = dict(state) if state is not None else {}
        if action_id == "search":
            res = await self.handle_book_search({"action": "search", **state_map})
            return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}
        return {"ok": False, "error": f"Unsupported action: {action_id}", "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `AnnasArchivePlugin` instance."""
    return AnnasArchivePlugin()
