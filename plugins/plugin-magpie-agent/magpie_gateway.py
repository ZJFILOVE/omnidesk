"""Loopback-only HTTP client for Magpie gateway quotas and OpenAI-compatible chat completions."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import queue
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult

try:
    from .magpie_common import fail_result, is_loopback_url, redact_secret
except ImportError:
    from magpie_common import fail_result, is_loopback_url, redact_secret  # type: ignore[no-redef]


def extract_json_error(body: str) -> tuple[str, str]:
    """Return `(error_code, error_message)` extracted from an OpenAI/Magpie error JSON body."""
    if not body:
        return "", ""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return "", body.strip()[:300]
    if not isinstance(parsed, Mapping):
        return "", ""
    err = parsed.get("error")
    if isinstance(err, str):
        return "", err
    if isinstance(err, Mapping):
        return str(err.get("code") or err.get("type") or ""), str(err.get("message") or "")
    return str(parsed.get("code") or ""), str(parsed.get("message") or "")


def classify_http_error(status: int, body: str, secret: str | None) -> tuple[str, str]:
    """Map HTTP status and body onto canonical error codes with secret redaction."""
    api_code, api_msg = extract_json_error(body)
    raw_msg = f"{api_code}: {api_msg}" if (api_code and api_msg) else (api_msg or api_code or f"HTTP {status}")
    safe_msg = redact_secret(raw_msg, secret)
    hay = f"{api_code} {api_msg}".lower()
    is_quota = any(
        tok in hay
        for tok in (
            "insufficient_quota", "quota_exceeded", "quota", "credit",
            "balance", "billing", "allowance", "额度", "配额",
        )
    )
    if status == 402 or (status in {403, 429} and is_quota):
        return "quota_exceeded", safe_msg
    if status == 429:
        return "rate_limited", safe_msg
    if status in {401, 403}:
        return "unauthorized", f"网关鉴权失败: {safe_msg}"
    if status == 504:
        return "timeout", f"Magpie 网关上游超时: {safe_msg}"
    if status in {502, 503} and any(tok in hay for tok in ("offline", "unavailable", "no member", "not served", "stopped")):
        return "service_offline", f"Magpie 网关当前无可用服务: {safe_msg}"
    return "upstream_error", f"Magpie HTTP {status}: {safe_msg}"


def _extract_choice_content(choice: Mapping[str, Any]) -> str:
    msg = choice.get("message")
    if not isinstance(msg, Mapping):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item["text"]) for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str))
    return ""


def _origin_of(url: str) -> tuple[str, str, int]:
    """Return `(scheme, host, port)` for credential-scope comparisons."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow bounded redirects but never forward credentials across origins."""

    max_redirections = 5

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        if _origin_of(req.full_url) != _origin_of(newurl):
            for header in ("Authorization", "Proxy-Authorization", "Cookie"):
                new_req.remove_header(header)
                new_req.unredirected_hdrs.pop(header, None)
        return new_req


def _parse_chat_json_response(raw_body: str, route_used: str, secret: str | None) -> CapabilityResult:
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return fail_result("Magpie /v1/chat/completions 返回了无效 JSON", error_code="invalid_response", secret=secret)
    if not isinstance(parsed, Mapping):
        return fail_result("Magpie /v1/chat/completions 返回格式无效", error_code="invalid_response", secret=secret)
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        _, err_msg = extract_json_error(raw_body)
        return fail_result(err_msg or "Magpie 响应缺少 choices 字段", error_code="invalid_response", secret=secret)
    choice0 = choices[0] if isinstance(choices[0], Mapping) else {}
    reply = _extract_choice_content(choice0)
    served_model = str(parsed.get("model") or route_used)
    usage = dict(parsed.get("usage")) if isinstance(parsed.get("usage"), Mapping) else {}
    return CapabilityResult.ok({
        "reply": reply,
        "model": served_model,
        "served_model": served_model,
        "route_group": route_used,
        "route_used": route_used,
        "finish_reason": str(choice0.get("finish_reason") or ""),
        "usage": usage,
        "message": {"role": "assistant", "content": reply},
        "streaming": False,
    })


class MagpieGatewayClient:
    """Loopback-enforced, proxy-free HTTP client supporting sync JSON and SSE streaming."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_conns: set[http.client.HTTPConnection] = set()
        self._shutting_down = False

    def reset(self) -> None:
        with self._lock:
            self._shutting_down = False

    def close_all(self) -> None:
        """Abort any active HTTP connections on plugin unload."""
        with self._lock:
            self._shutting_down = True
            conns = list(self._active_conns)
            self._active_conns.clear()
        for conn in conns:
            self._close_conn(conn, abort=True)

    @staticmethod
    def _close_conn(conn: http.client.HTTPConnection | None, *, abort: bool = False) -> None:
        if conn is None:
            return
        if abort:
            with contextlib.suppress(Exception):
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            conn.close()

    def _open_conn(self, base_url: str, timeout_sec: float) -> tuple[http.client.HTTPConnection, str]:
        parsed = urllib.parse.urlparse(base_url.strip())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_path = parsed.path.rstrip("/")
        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeout_sec, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout_sec)
        with self._lock:
            if self._shutting_down:
                raise ConnectionAbortedError("plugin unloaded")
            self._active_conns.add(conn)
        return conn, base_path

    def _release_conn(self, conn: http.client.HTTPConnection | None, *, abort: bool = False) -> None:
        if conn is None:
            return
        with self._lock:
            self._active_conns.discard(conn)
        self._close_conn(conn, abort=abort)

    async def fetch_quotas(self, gateway_url: str, *, secret: str | None, timeout_sec: float = 2.0) -> Any | None:
        """GET `/v1/magpie/quotas` directly over loopback bypassing system proxies."""
        if not is_loopback_url(gateway_url):
            return None
        target = gateway_url.rstrip("/") + "/v1/magpie/quotas"

        def _do_get() -> Any:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _SafeRedirectHandler(),
            )
            req = urllib.request.Request(target, method="GET")
            if secret:
                req.add_header("Authorization", f"Bearer {secret}")
            with opener.open(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8")) if resp.status == 200 else None

        try:
            return await asyncio.to_thread(_do_get)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return None

    async def chat_completions(
        self,
        gateway_url: str,
        body: Mapping[str, Any],
        *,
        route_used: str,
        secret: str | None,
        timeout_sec: float = 60.0,
        stream_sink: Any = None,
    ) -> CapabilityResult:
        """POST `/v1/chat/completions` supporting both JSON and incremental SSE streaming."""
        if not gateway_url:
            return fail_result("Magpie 网关地址未配置", error_code="not_configured", secret=secret)
        if not is_loopback_url(gateway_url):
            return fail_result(
                f"出于安全隔离要求，Magpie 网关仅允许 loopback 地址（收到: {gateway_url!r}）",
                error_code="invalid_argument",
                secret=secret,
            )
        if self._shutting_down:
            return fail_result("plugin-magpie-agent 已卸载", error_code="unloaded", secret=secret)

        want_stream = bool(body.get("stream", False))
        payload_bytes = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json" if want_stream else "application/json",
            "Connection": "close",
        }
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        if not want_stream:
            res = await self._post_sync(gateway_url, payload_bytes, headers, route_used, secret, timeout_sec)
            if res.ok and stream_sink is not None and res.data.get("reply"):
                await stream_sink.emit_delta(str(res.data["reply"]))
            return res
        return await self._post_sse(gateway_url, payload_bytes, headers, route_used, secret, timeout_sec, stream_sink)

    async def _post_sync(
        self,
        gateway_url: str,
        payload_bytes: bytes,
        headers: dict[str, str],
        route_used: str,
        secret: str | None,
        timeout_sec: float,
    ) -> CapabilityResult:
        conn_holder: list[http.client.HTTPConnection] = []

        def _run() -> tuple[int, str]:
            conn, base_path = self._open_conn(gateway_url, timeout_sec)
            conn_holder.append(conn)
            try:
                conn.request("POST", f"{base_path}/v1/chat/completions", body=payload_bytes, headers=headers)
                resp = conn.getresponse()
                return resp.status, resp.read().decode("utf-8", errors="replace")
            finally:
                self._release_conn(conn)

        try:
            status, raw = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_sec + 0.5)
        except asyncio.CancelledError:
            for conn in conn_holder:
                self._release_conn(conn, abort=True)
            raise
        except (TimeoutError, socket.timeout):
            for conn in conn_holder:
                self._release_conn(conn, abort=True)
            return fail_result(f"Magpie /v1/chat/completions 请求超时（{timeout_sec:.1f}s）", error_code="timeout", secret=secret)
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError, http.client.HTTPException) as exc:
            for conn in conn_holder:
                self._release_conn(conn, abort=True)
            return fail_result(f"Magpie 网关不可达 ({gateway_url}): {exc}", error_code="service_offline", secret=secret)

        if 200 <= status < 300:
            return _parse_chat_json_response(raw, route_used, secret)
        err_code, err_msg = classify_http_error(status, raw, secret)
        return fail_result(err_msg, error_code=err_code, secret=secret, data={"http_status": status})

    async def _post_sse(
        self,
        gateway_url: str,
        payload_bytes: bytes,
        headers: dict[str, str],
        route_used: str,
        secret: str | None,
        timeout_sec: float,
        stream_sink: Any,
    ) -> CapabilityResult:
        q: queue.Queue[tuple[str, Any]] = queue.Queue()
        conn_holder: list[http.client.HTTPConnection] = []
        stop_flag = threading.Event()

        def _worker() -> None:
            conn: http.client.HTTPConnection | None = None
            try:
                conn, base_path = self._open_conn(gateway_url, timeout_sec)
                conn_holder.append(conn)
                conn.request("POST", f"{base_path}/v1/chat/completions", body=payload_bytes, headers=headers)
                resp = conn.getresponse()
                ctype = (resp.getheader("Content-Type") or "").lower()
                if not (200 <= resp.status < 300):
                    q.put(("http_err", (resp.status, resp.read().decode("utf-8", errors="replace"))))
                    return
                if "application/json" in ctype and "text/event-stream" not in ctype:
                    q.put(("json_ok", resp.read().decode("utf-8", errors="replace")))
                    return
                buf: list[str] = []
                while not stop_flag.is_set() and not self._shutting_down:
                    raw_line = resp.readline()
                    if not raw_line:
                        if buf:
                            q.put(("sse_event", "\n".join(buf)))
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if buf:
                            q.put(("sse_event", "\n".join(buf)))
                            buf.clear()
                    elif line.startswith("data:"):
                        buf.append(line[5:].lstrip(" "))
                q.put(("eof", None))
            except (TimeoutError, socket.timeout):
                q.put(("timeout", None))
            except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError, http.client.HTTPException) as exc:
                q.put(("offline", str(exc)))
            finally:
                self._release_conn(conn)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        acc: list[str] = []
        served_model, finish_reason, usage = route_used, "", {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        try:
            while True:
                if self._shutting_down or (stream_sink is not None and getattr(stream_sink, "is_cancelled", False)):
                    return fail_result("聊天流已取消", error_code="cancelled", secret=secret)
                rem = deadline - loop.time()
                if rem <= 0:
                    return fail_result(f"Magpie SSE 流超时（{timeout_sec:.1f}s）", error_code="timeout", secret=secret)
                try:
                    kind, val = await asyncio.to_thread(q.get, True, min(rem, 0.25))
                except queue.Empty:
                    continue
                if kind == "eof":
                    break
                if kind == "timeout":
                    return fail_result(f"Magpie SSE 流超时（{timeout_sec:.1f}s）", error_code="timeout", secret=secret)
                if kind == "offline":
                    return fail_result(f"Magpie 网关不可达 ({gateway_url}): {val}", error_code="service_offline", secret=secret)
                if kind == "http_err":
                    err_code, err_msg = classify_http_error(val[0], val[1], secret)
                    return fail_result(err_msg, error_code=err_code, secret=secret, data={"http_status": val[0]})
                if kind == "json_ok":
                    res = _parse_chat_json_response(val, route_used, secret)
                    if res.ok and stream_sink is not None and res.data.get("reply"):
                        await stream_sink.emit_delta(str(res.data["reply"]))
                    return res
                if kind == "sse_event":
                    payload = val.strip()
                    if payload == "[DONE]":
                        break
                    if not payload:
                        continue
                    try:
                        chunk = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(chunk, Mapping):
                        continue
                    if "error" in chunk:
                        err_code, err_msg = classify_http_error(502, payload, secret)
                        return fail_result(err_msg, error_code=err_code, secret=secret)
                    if isinstance(chunk.get("model"), str) and chunk["model"]:
                        served_model = chunk["model"]
                    if isinstance(chunk.get("usage"), Mapping):
                        usage = dict(chunk["usage"])
                    choices = chunk.get("choices")
                    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                        c0 = choices[0]
                        if isinstance(c0.get("finish_reason"), str) and c0["finish_reason"]:
                            finish_reason = c0["finish_reason"]
                        delta = c0.get("delta")
                        delta_text = (
                            delta["content"]
                            if isinstance(delta, Mapping) and isinstance(delta.get("content"), str)
                            else (c0["text"] if isinstance(c0.get("text"), str) else _extract_choice_content(c0))
                        )
                        if delta_text:
                            acc.append(delta_text)
                            if stream_sink is not None:
                                await stream_sink.emit_delta(delta_text)
        finally:
            stop_flag.set()
            for conn in conn_holder:
                self._release_conn(conn, abort=True)
            thread.join(timeout=0.5)

        reply = "".join(acc)
        return CapabilityResult.ok({
            "reply": reply,
            "model": served_model,
            "served_model": served_model,
            "route_group": route_used,
            "route_used": route_used,
            "finish_reason": finish_reason,
            "usage": usage,
            "message": {"role": "assistant", "content": reply},
            "streaming": True,
        })
