"""Same-origin browser fetch validation, JS wrapper builder, and outcome parser."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult

if __package__:
    from .security import ValidationProblem, redact_sensitive, same_origin
else:  # pragma: no cover - direct module import fallback
    from security import ValidationProblem, redact_sensitive, same_origin

_ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ALLOWED_CREDENTIALS = frozenset({"include", "same-origin", "omit"})
_FORBIDDEN_FETCH_HEADERS = frozenset({"host", "connection", "content-length", "cookie"})
_MAX_FETCH_LIMIT_BYTES = 16 * 1024 * 1024


def validate_fetch_params(
    payload: Mapping[str, Any], cfg: Mapping[str, Any]
) -> tuple[str, dict[str, str], str | None, str, int]:
    """Validate HTTP method, headers, body, credentials, and byte cap for `web.fetch.v1`."""
    method = str(payload.get("method") or "GET").strip().upper()
    if method not in _ALLOWED_HTTP_METHODS:
        raise ValidationProblem(f"不支持的 HTTP 方法: {method!r}")

    raw_headers = payload.get("headers")
    clean_headers: dict[str, str] = {}
    if raw_headers is not None:
        if not isinstance(raw_headers, Mapping):
            raise ValidationProblem("headers 必须为键值对象")
        for k, v in raw_headers.items():
            ks, vs = str(k).strip(), str(v)
            if not ks or any(c in ks or c in vs for c in ("\r", "\n", "\x00")):
                raise ValidationProblem("headers 包含非法控制字符")
            if ks.lower() in _FORBIDDEN_FETCH_HEADERS:
                raise ValidationProblem(f"安全限制: 禁止覆盖受保护请求头 {ks!r}", error_code="permission_denied")
            clean_headers[ks] = vs

    raw_body = payload.get("body")
    body_str: str | None = None
    if raw_body is not None:
        if isinstance(raw_body, Mapping):
            body_str = json.dumps(dict(raw_body), ensure_ascii=False)
            if not any(k.lower() == "content-type" for k in clean_headers):
                clean_headers["Content-Type"] = "application/json"
        elif isinstance(raw_body, str):
            body_str = raw_body
        else:
            raise ValidationProblem("body 必须为字符串或 JSON 对象")
    if method in {"GET", "HEAD"}:
        if body_str:
            raise ValidationProblem(f"{method} 请求不允许携带非空 body")
        body_str = None

    credentials = str(payload.get("credentials") or "include").strip()
    if credentials not in _ALLOWED_CREDENTIALS:
        raise ValidationProblem(f"无效的 credentials 模式: {credentials!r}")

    cfg_max = cfg.get("max_fetch_bytes", 8 * 1024 * 1024)
    if isinstance(cfg_max, bool) or not isinstance(cfg_max, int) or cfg_max <= 0:
        cfg_max = 8 * 1024 * 1024
    cfg_cap = min(cfg_max, _MAX_FETCH_LIMIT_BYTES)
    req_max = payload.get("max_bytes")
    if req_max is not None:
        if isinstance(req_max, bool) or not isinstance(req_max, int) or req_max <= 0:
            raise ValidationProblem("max_bytes 必须为正整数")
        max_bytes = min(req_max, cfg_cap)
    else:
        max_bytes = cfg_cap
    return method, clean_headers, body_str, credentials, max_bytes


def build_fetch_js(
    *,
    url: str,
    expected_origin: str,
    method: str,
    headers: Mapping[str, str],
    body: str | None,
    credentials: str,
    max_bytes: int,
    timeout_ms: int,
) -> str:
    """Build async JS expression for CDP `Runtime.evaluate` (`awaitPromise=True`)."""
    body_clause = f"init.body = {json.dumps(body)};" if body is not None else ""
    return (
        "(async () => {"
        f"const reqUrl = {json.dumps(url)};"
        f"const expectedOrigin = {json.dumps(expected_origin)};"
        f"const maxBytes = {int(max_bytes)};"
        "const resolved = new URL(reqUrl, window.location.href);"
        'if (resolved.protocol !== "http:" && resolved.protocol !== "https:") {'
        'return { ok: false, error_code: "invalid_argument", error: "Only http/https allowed" };'
        "}"
        "if (window.location.origin !== expectedOrigin || resolved.origin !== window.location.origin) {"
        'return { ok: false, error_code: "permission_denied", error: "Cross-origin fetch forbidden" };'
        "}"
        "const controller = new AbortController();"
        f"const timer = setTimeout(() => controller.abort(), {int(timeout_ms)});"
        "try {"
        "const init = {"
        f"method: {json.dumps(method)}, "
        f"headers: {json.dumps(dict(headers))}, "
        f"credentials: {json.dumps(credentials)}, "
        'redirect: "follow", signal: controller.signal'
        "};"
        f"{body_clause}"
        "const resp = await fetch(resolved.href, init);"
        "if (resp.url) {"
        "const finalUrl = new URL(resp.url);"
        "if (finalUrl.origin !== window.location.origin) {"
        'return { ok: false, error_code: "permission_denied", error: "Cross-origin redirect forbidden" };'
        "}"
        "}"
        'const cl = Number(resp.headers.get("content-length") || "0");'
        "if (Number.isFinite(cl) && cl > maxBytes) {"
        'return { ok: false, error_code: "validation_error", error: "Response exceeds max_bytes limit" };'
        "}"
        "const buf = await resp.arrayBuffer();"
        "if (buf.byteLength > maxBytes) {"
        'return { ok: false, error_code: "validation_error", error: "Response exceeds max_bytes limit" };'
        "}"
        'const text = new TextDecoder("utf-8", { fatal: false }).decode(buf);'
        "const respHeaders = {};"
        "resp.headers.forEach((v, k) => { respHeaders[k] = v; });"
        "return { ok: true, status: resp.status, url: resp.url || resolved.href, headers: respHeaders, body: text };"
        "} catch (err) {"
        "const name = err && err.name ? String(err.name) : '';"
        "return { ok: false, error_code: name === 'AbortError' ? 'timeout' : 'service_offline', "
        "error: err && err.message ? String(err.message) : 'Fetch failed' };"
        "} finally { clearTimeout(timer); }"
        "})()"
    )


def parse_fetch_outcome(raw_val: Any, *, expected_url: str, max_bytes: int) -> CapabilityResult:
    """Validate CDP `fetch()` response envelope and enforce size/origin limits fail-closed."""
    if isinstance(raw_val, str):
        try:
            raw_val = json.loads(raw_val)
        except json.JSONDecodeError:
            return CapabilityResult.fail("CDP fetch 返回格式无效", error_code="service_offline")
    if not isinstance(raw_val, Mapping):
        return CapabilityResult.fail("CDP fetch 未返回有效响应对象", error_code="service_offline")
    if raw_val.get("ok") is False:
        err_code = str(raw_val.get("error_code") or "service_offline")
        err_msg = redact_sensitive(str(raw_val.get("error") or "浏览器上下文 fetch 失败"))
        return CapabilityResult.fail(err_msg, error_code=err_code)

    status = raw_val.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not (100 <= status <= 599):
        return CapabilityResult.fail("CDP fetch 返回无效 HTTP 状态码", error_code="service_offline")

    resp_url = str(raw_val.get("url") or expected_url).strip() or expected_url
    try:
        if not same_origin(resp_url, expected_url):
            return CapabilityResult.fail("安全限制: 禁止跨源重定向或跨源响应", error_code="permission_denied")
    except ValidationProblem as exc:
        return CapabilityResult.fail(str(exc), error_code=exc.error_code)

    raw_body = raw_val.get("body")
    body = "" if raw_body is None else str(raw_body)
    byte_len = len(body.encode("utf-8"))
    if byte_len > max_bytes:
        return CapabilityResult.fail(
            f"响应体大小 ({byte_len} 字节) 超出上限 ({max_bytes} 字节)",
            data={"byte_length": byte_len, "max_bytes": max_bytes, "reason": "response_too_large"},
            error_code="validation_error",
        )

    raw_headers = raw_val.get("headers")
    headers = {str(k): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, Mapping) else {}
    return CapabilityResult.ok(
        {"status": status, "url": resp_url, "headers": headers, "body": body, "byte_length": byte_len}
    )
