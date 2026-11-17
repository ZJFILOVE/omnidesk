"""OmniDesk v2 `plugin-annas-archive` (`book.search.v1`) hybrid book search & safe download plugin."""

from __future__ import annotations

import hashlib
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

from annas_html import page_state
from annas_auth import AuthMaterial, resolve_auth_material  # noqa: E402
from annas_http import HttpFetchError, download_to_target, fetch_with_chrome_fallback  # noqa: E402
from annas_parser import (  # noqa: E402
    clean_text,
    extract_slow_download_link,
    join_base_url,
    normalize_format,
    parse_annas_detail_response,
    parse_annas_item_id,
    parse_search_page, AnnasPageError,
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

_SUPPORTED_ACTIONS = ("mirrors", "configure_mirror", "search", "detail", "download", "open_browser", "open_in_babeldoc", "open_in_ebook", "send_to_nas", "status")
_MAX_CACHE = 256
MIRRORS = [{'url': 'https://annas-archive.gl', 'label': 'annas-archive.gl'}, {'url': 'https://annas-archive.pk', 'label': 'annas-archive.pk'}, {'url': 'https://annas-archive.gd', 'label': 'annas-archive.gd'}, {'url': 'https://annas-archive.is', 'label': 'annas-archive.is · 未核实归属'}]


class AnnasArchivePlugin(BasePlugin):
    """Anna's Archive search, MD5 detail inspection, verified download, and workbench handoff."""

    def __init__(self) -> None:
        self._search_cache: dict[str, dict[str, Any]] = {}
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._download_records: dict[str, dict[str, Any]] = {}
        self._last_results: list[dict[str, Any]] = []
        self._last_search_url = ""

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
                    parsed = urllib.parse.urlsplit(raw)
                    if parsed.query or parsed.fragment:
                        raise BookSearchSecurityError("unsafe_url", "Anna 入口地址不能包含查询参数或片段")
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
        if isinstance(exc, HttpFetchError) and exc.status_code == 403:
            return CapabilityResult.fail("Anna 拒绝了当前请求；可在 Chrome 打开当前镜像完成验证后重试", error_code="verification_required")
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
                    self._last_search_url = ""
                except BookSearchSecurityError as exc:
                    return self._fail_from_exc(exc)
            endpoint, _ = self._resolve_endpoint(required=False)
            return CapabilityResult.ok({"mirror_url": endpoint, "mirrors": MIRRORS})
        if action == "open_browser":
            if self.ctx is None or not self.ctx.capabilities.has("web.embed.v1"):
                return CapabilityResult.fail("请启用 Chrome 插件以完成站点验证", error_code="capability_unavailable")
            endpoint, origins = self._resolve_endpoint()
            md5 = parse_annas_item_id(clean_text(payload.get("item_id")))
            url = self._last_search_url or endpoint
            if md5:
                url = join_base_url(endpoint, f"/md5/{md5}")
                token = clean_text(payload.get("download_token"))
                options = self._detail_cache.get(md5, {}).get("download_options", [])
                if token and any(option["download_token"] == token for option in options):
                    url = join_base_url(endpoint, token)
            try:
                validate_url(url, configured_origins=origins, resolve_dns=False)
            except BookSearchSecurityError as exc:
                return self._fail_from_exc(exc)
            return await self.ctx.capabilities.call("web.embed.v1", {"action": "open", "url": url})
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
        if fmt and fmt not in {"pdf", "epub", "mobi", "azw3", "txt", "djvu", "fb2", "cbr", "cbz"}:
            return CapabilityResult.fail(f"不支持的格式过滤: {fmt!r}", error_code="invalid_argument")
        raw_limit = payload.get("limit", 20)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit <= 0:
            return CapabilityResult.fail("limit 必须为正整数", error_code="invalid_argument")
        limit = min(100, raw_limit)
        cursor = payload.get("cursor")
        if cursor is not None and not isinstance(cursor, Mapping):
            return CapabilityResult.fail("无效的分页游标", error_code="invalid_argument")
        raw_page = cursor.get("page", 1) if cursor is not None else payload.get("page", 1)
        offset = cursor.get("offset", 0) if cursor is not None else 0
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000:
            return CapabilityResult.fail("无效的分页偏移", error_code="invalid_argument")
        if isinstance(raw_page, bool) or not isinstance(raw_page, int) or not 1 <= raw_page <= 10000:
            return CapabilityResult.fail("page 须为 1–10000 的整数", error_code="invalid_argument")
        page = raw_page

        try:
            endpoint, origins = self._resolve_endpoint()
        except BookSearchSecurityError as exc:
            return self._fail_from_exc(exc)

        if parse_annas_item_id(query):
            detail = await self._handle_detail({"item_id": parse_annas_item_id(query)})
            if not detail.ok:
                return detail
            self._last_results = [detail.data]
            return CapabilityResult.ok({"results": self._last_results, "count": 1, "page": 1, "has_more": False, "source": "plugin-annas-archive"})
        auth = await resolve_auth_material(self.ctx, endpoint)
        q_pairs: list[tuple[str, str]] = [("q", query), ("page", str(page)), ("display", ""), ("index", "")]
        if fmt:
            q_pairs.append(("ext", fmt))
        for key in ("lang", "content", "sort"):
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > 80:
                return CapabilityResult.fail(f"{key} 格式无效", error_code="invalid_argument")
            if value:
                q_pairs.append((key, value))
        search_path = endpoint if urllib.parse.urlsplit(endpoint).path.lower().endswith("/search") else f"{endpoint}/search"
        search_url = f"{search_path}?{urllib.parse.urlencode(q_pairs)}"

        self._last_search_url = search_url
        try:
            resp = await fetch_with_chrome_fallback(
                self.ctx, search_url, auth=auth, timeout_sec=self._timeout_sec(),
                max_bytes=8 * 1024 * 1024, configured_origins=origins,
                prefer_chrome_fetch=bool(self._cfg("prefer_chrome_fetch", False)),
            )
        except (HttpFetchError, BookSearchSecurityError) as exc:
            return self._fail_from_exc(exc, auth)

        try:
            page_data = parse_search_page(resp.body, resp.content_type, endpoint, page=page)
        except AnnasPageError as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.code, browser_url=search_url)
        results: list[dict[str, Any]] = []
        for item in page_data["results"]:
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
        eligible = results
        results = eligible[offset:offset + limit]
        next_cursor = ({"page": page, "offset": offset + limit} if offset + limit < len(eligible)
                       else {"page": page_data["next_page"], "offset": 0} if page_data["has_more"] else None)
        self._last_results = list(results)
        return CapabilityResult.ok({
            "results": results, "count": len(results), "query": query, "format": fmt,
            "limit": limit, "page": page, "has_more": next_cursor is not None, "next_page": page_data["next_page"], "next_cursor": next_cursor,
            "endpoint": scrub_url_secrets(endpoint), "source": "plugin-annas-archive",
        })

    async def _handle_detail(self, payload: Mapping[str, Any]) -> CapabilityResult:
        item_id = parse_annas_item_id(clean_text(payload.get("item_id") or payload.get("id") or payload.get("md5")))
        if not item_id:
            return CapabilityResult.fail("无效或缺失的 item_id", error_code="invalid_argument")

        try:
            endpoint, origins = self._resolve_endpoint()
            # Canonical record URL, never an arbitrary URL supplied with a valid MD5.
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

        state = page_state(resp.body)
        if state["state"] != "ready":
            return CapabilityResult.fail(state["message"], error_code=state["state"], browser_url=detail_url)
        detail_obj = parse_annas_detail_response(
            resp.body, resp.content_type, endpoint, requested_id=item_id, detail_url=scrub_url_secrets(detail_url)
        )
        if detail_obj is None:
            return CapabilityResult.fail("无法识别 Anna 文件详情页面，请检查入口或完成浏览器验证", error_code="unsupported_response", item_id=item_id)
        detail_obj["member_api_available"] = bool(auth.api_key)
        detail_obj["download_supported"] = detail_obj.get("format") in SUPPORTED_BOOK_FORMATS
        valid_options = []
        for option in detail_obj.get("download_options", []):
            try:
                validate_url(join_base_url(endpoint, option["download_token"]), configured_origins=origins, resolve_dns=False)
                if option["kind"] in {"fast", "slow"} and parse_origin(join_base_url(endpoint, option["download_token"])) != parse_origin(endpoint):
                    option = {**option, "kind": "external"}
                valid_options.append(option)
            except BookSearchSecurityError:
                continue
        detail_obj["download_options"] = valid_options
        self._cache_put(self._detail_cache, item_id, detail_obj)
        return CapabilityResult.ok(detail_obj)

    async def _resolve_fast_api_download(
        self, endpoint: str, item_id: str, *, auth: AuthMaterial, origins: frozenset[Origin], option: Mapping[str, Any] | None = None
    ) -> str | None:
        if not auth.api_key or not item_id:
            return None
        params = {"md5": item_id, "key": auth.api_key}
        if option:
            params.update({key: option[key] for key in ("path_index", "domain_index") if key in option})
        fast_url = f"{endpoint}/dyn/api/fast_download.json?{urllib.parse.urlencode(params)}"
        try:
            resp = await fetch_with_chrome_fallback(
                self.ctx, fast_url, auth=auth, timeout_sec=self._timeout_sec(),
                max_bytes=256 * 1024, configured_origins=origins, accept="application/json",
                allow_json_errors=True, allow_chrome=False,
            )
        except HttpFetchError as exc:
            if exc.error_code == "not_found":
                return None
            raise
        data = json.loads(resp.body.decode("utf-8"))
        if isinstance(data, Mapping):
            err = clean_text(data.get("error"))
            if err:
                code = "quota_exceeded" if any(word in err.lower() for word in ("quota", "left", "limit")) else "auth_required"
                raise HttpFetchError(code, redact_text(err, auth.extra_secrets()))
            dl_url = clean_text(data.get("download_url"))
            if dl_url:
                return validate_url(join_base_url(endpoint, dl_url), configured_origins=origins, resolve_dns=False)
        return None

    async def _handle_download(self, payload: Mapping[str, Any]) -> CapabilityResult:
        raw_id = clean_text(payload.get("item_id") or payload.get("id") or payload.get("md5"))
        req_token = clean_text(payload.get("download_token"))
        if not raw_id:
            return CapabilityResult.fail("Anna 下载需要文件 MD5", error_code="invalid_argument")
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

        # Always obtain the file's real source choices; search cards carry no fabricated URL.
        meta = self._detail_cache.get(effective_id)
        if item_id and meta is None:
            detail_res = await self._handle_detail({"item_id": item_id})
            if not detail_res.ok:
                return detail_res
            meta = detail_res.data
        meta = meta or {}
        file_fmt = normalize_format(meta.get("format"))
        if req_fmt and file_fmt and req_fmt != file_fmt:
            return CapabilityResult.fail("一个 MD5 对应一个文件；请选择所需格式的另一条搜索结果", error_code="format_mismatch")
        auth = await resolve_auth_material(self.ctx, endpoint)
        options = meta.get("download_options", [])
        option = next((o for o in options if o["download_token"] == req_token), None) if req_token else None
        if req_token and option is None:
            return CapabilityResult.fail("下载来源不在该文件详情中，请刷新详情后选择", error_code="invalid_argument")
        if option is None and not auth.api_key:
            return CapabilityResult.fail("请选择此文件的下载来源", error_code="selection_required", download_options=options)
        if option and option["kind"] == "external":
            return CapabilityResult.fail("该来源需要在原站点完成下载", error_code="browser_required", browser_url=join_base_url(endpoint, req_token))
        if option and option["kind"] == "fast" and not auth.api_key:
            return CapabilityResult.fail("快速下载需要会员 Secret Key，或在浏览器登录会员账号", error_code="membership_required")
        matched_fmt = file_fmt or req_fmt
        if not matched_fmt:
            return CapabilityResult.fail("文件格式未知，请先查看详情", error_code="unsupported_format")
        dest_raw = payload.get("destination")
        default_name = sanitize_filename(clean_text(meta.get("title")) or effective_id, ext=matched_fmt)
        tmp_path = None
        try:
            target_path = resolve_download_target(download_root, str(dest_raw) if dest_raw is not None else None, default_filename=default_name)
            if option is None or option["kind"] == "fast":
                dl_url = await self._resolve_fast_api_download(endpoint, item_id, auth=auth, origins=origins, option=option)
                if not dl_url:
                    return CapabilityResult.fail("会员 API 未返回下载地址", error_code="invalid_response")
            else:
                dl_url = validate_url(join_base_url(endpoint, req_token), configured_origins=origins, resolve_dns=False)
                if option["kind"] == "slow":
                    response = await fetch_with_chrome_fallback(self.ctx, dl_url, auth=auth,
                        timeout_sec=self._timeout_sec(), max_bytes=512 * 1024, configured_origins=origins)
                    state = page_state(response.body)
                    if state["state"] != "ready":
                        return CapabilityResult.fail(state["message"], error_code=state["state"],
                            wait_seconds=state.get("wait_seconds", 0), browser_url=dl_url)
                    direct = extract_slow_download_link(response.body)
                    if not direct:
                        return CapabilityResult.fail("慢速下载页尚未提供文件链接，请在浏览器完成验证或等待后重试", error_code="browser_required", browser_url=dl_url)
                    dl_url = validate_url(join_base_url(endpoint, direct), configured_origins=origins, resolve_dns=False)
            # Anna cookies belong only to Anna, never the partner download host.
            file_auth = auth if parse_origin(dl_url) == parse_origin(endpoint) else AuthMaterial()
            tmp_path, sha256_hex, size_bytes, final_url, content_type = await download_to_target(
                self.ctx, dl_url, target_path, auth=file_auth, timeout_sec=self._timeout_sec(), max_bytes=max_bytes, configured_origins=origins)
            if "html" in content_type.lower():
                state = page_state(tmp_path.read_bytes()[:512 * 1024])
                return CapabilityResult.fail(state.get("message", "下载站点返回网页，需要在浏览器继续"),
                    error_code=state["state"] if state["state"] != "ready" else "browser_required", browser_url=final_url)
            # MD5 is Anna's file identity; reject an incorrect file before publishing it.
            digest = hashlib.md5(usedforsecurity=False)
            with tmp_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if item_id and digest.hexdigest() != item_id:
                return CapabilityResult.fail("下载文件的 MD5 与 Anna 记录不一致，文件未导入", error_code="checksum_mismatch")
            final_fmt = finalize_downloaded_file(tmp_path, target_path, matched_fmt, max_bytes)
        except (BookSearchSecurityError, HttpFetchError) as exc:
            return self._fail_from_exc(exc, auth)
        except (ValueError, OSError):
            return CapabilityResult.fail("Anna 下载响应或文件校验失败", error_code="invalid_response")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

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
        if action_id in {"search", "open_browser"}:
            res = await self.handle_book_search({**state_map, "action": action_id})
            return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}
        return {"ok": False, "error": f"Unsupported action: {action_id}", "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `AnnasArchivePlugin` instance."""
    return AnnasArchivePlugin()
