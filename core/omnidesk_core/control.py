"""Unix domain socket (0600) single-request JSON-RPC server and client for control.sock."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .bus import EntityBus
from .config import ensure_private_dir
from .errors import (
    CapabilityNotFoundError,
    ErrorCode,
    InvalidArgumentError,
    InvalidCapabilityIdError,
    OmniDeskError,
    PathSafetyError,
    PluginError,
    redact_secrets,
)
from .paths import OmnideskPaths
from .plugins import PluginManager
from .registry import CapabilityRegistry, CapabilityResult

DEFAULT_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

_JSONRPC_CODES: dict[str, int] = {
    "invalid_json": -32700,
    "invalid_request": -32600,
    "unknown_operation": -32601,
    "method_not_found": -32601,
    ErrorCode.INVALID_ARGUMENT: -32602,
    ErrorCode.VALIDATION_ERROR: -32602,
    ErrorCode.CAPABILITY_NOT_FOUND: -32001,
    ErrorCode.NOT_FOUND: -32001,
    ErrorCode.PLUGIN_FAULT: -32002,
    ErrorCode.PERMISSION_DENIED: -32003,
    ErrorCode.PATH_SAFETY: -32003,
    ErrorCode.TIMEOUT: -32004,
}


class ControlRpcError(OmniDeskError):
    """Raised when control.sock transport or RPC framing fails."""

    default_code = ErrorCode.SERVICE_OFFLINE


def _resolve_socket_path(socket_path: Path | str | None, paths: OmnideskPaths | None) -> Path:
    if socket_path is not None:
        return Path(socket_path).expanduser()
    return (paths or OmnideskPaths.from_env()).control_socket_path


def _parse_response_bytes(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        raise ControlRpcError("Empty response from control.sock", code=ErrorCode.INTERNAL_ERROR)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlRpcError(f"Malformed JSON response from control.sock: {exc}", code=ErrorCode.INTERNAL_ERROR, cause=exc) from exc
    if not isinstance(parsed, dict):
        raise ControlRpcError("Control response must be a JSON object", code=ErrorCode.INTERNAL_ERROR)
    return parsed


async def _read_single_json_message(reader: asyncio.StreamReader, max_bytes: int) -> tuple[bytes, bool]:
    """Read a single NDJSON line or EOF-terminated JSON payload up to max_bytes."""
    buf = bytearray()
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            return bytes(buf), False
        if (nl := chunk.find(b"\n")) != -1:
            buf.extend(chunk[:nl])
            return bytes(buf), len(buf) > max_bytes
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return bytes(buf[:max_bytes]), True


class ControlServer:
    """Asyncio Unix socket RPC server listening on 0600 control.sock."""

    def __init__(
        self, *, socket_path: Path | str | None = None, paths: OmnideskPaths | None = None,
        capabilities: CapabilityRegistry | None = None, bus: EntityBus | None = None,
        plugins: PluginManager | None = None, secret_provider: Callable[[], Iterable[str]] | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, request_timeout: float = 30.0,
    ) -> None:
        self._paths = paths or (plugins.paths if plugins is not None else OmnideskPaths.from_env())
        self._socket_path = _resolve_socket_path(socket_path, self._paths)
        self._plugins = plugins
        self._bus = bus or (plugins.bus if plugins is not None else EntityBus())
        self._capabilities = capabilities or (plugins.capabilities if plugins is not None else CapabilityRegistry(bus=self._bus))
        self._secret_provider = secret_provider or (plugins.secret_vault.secret_values if plugins is not None else None)
        self._max_payload_bytes = max_payload_bytes
        self._request_timeout = request_timeout
        self._server: asyncio.AbstractServer | None = None

    @property
    def socket_path(self) -> Path: return self._socket_path
    @property
    def is_running(self) -> bool: return self._server is not None and self._server.is_serving()

    def _redact(self, text: str) -> str:
        try:
            extra = self._secret_provider() if self._secret_provider is not None else ()
        except Exception:
            extra = ()
        return redact_secrets(text, extra)

    def _prepare_socket_path(self) -> None:
        if self._socket_path.is_symlink():
            raise PathSafetyError(f"Refusing symlinked control socket: {self._socket_path}")
        ensure_private_dir(self._socket_path.parent)
        if self._socket_path.exists():
            if not stat.S_ISSOCK(self._socket_path.lstat().st_mode):
                raise PathSafetyError(f"Existing non-socket file at {self._socket_path}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(str(self._socket_path))
                except OSError:
                    self._socket_path.unlink(missing_ok=True)
                else:
                    raise OmniDeskError(f"Control socket already in use: {self._socket_path}", code=ErrorCode.CONFLICT)

    async def start(self) -> Path:
        """Bind the Unix domain socket with 0600 permissions and start serving."""
        if self._server is not None:
            return self._socket_path
        self._prepare_socket_path()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self._socket_path))
        os.chmod(self._socket_path, 0o600)
        return self._socket_path

    async def stop(self) -> None:
        """Stop the server and remove control.sock."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        if self._socket_path.exists() and not self._socket_path.is_symlink():
            self._socket_path.unlink(missing_ok=True)

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    def _error_envelope(
        self, req_id: Any, message: str, *, error_code: str, provider_plugin_id: str = "", jsonrpc: bool = False, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        safe_msg, code_str, det = self._redact(message), str(error_code), dict(details or {})
        if jsonrpc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": _JSONRPC_CODES.get(code_str, -32000), "message": safe_msg, "data": {"error_code": code_str, **det}}}
        return {
            "id": req_id, "ok": False, "provider_plugin_id": provider_plugin_id,
            "data": {"error_code": code_str, **det}, "error": safe_msg, "error_code": code_str,
            "error_detail": {"code": code_str, "message": safe_msg, "details": det},
        }

    def _ok_envelope(self, req_id: Any, data: Mapping[str, Any], *, provider_plugin_id: str = "", jsonrpc: bool = False) -> dict[str, Any]:
        if jsonrpc:
            return {"jsonrpc": "2.0", "id": req_id, "result": {"ok": True, "provider_plugin_id": provider_plugin_id, "data": dict(data)}}
        return {"id": req_id, "ok": True, "provider_plugin_id": provider_plugin_id, "data": dict(data), "error": ""}

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(self._request_timeout):
                raw, too_large = await _read_single_json_message(reader, self._max_payload_bytes)
                if too_large:
                    resp = self._error_envelope(None, f"Request exceeds {self._max_payload_bytes} byte limit", error_code="invalid_request")
                elif not raw.strip():
                    resp = self._error_envelope(None, "Empty control.sock request", error_code="invalid_request")
                else:
                    resp = await self._dispatch_bytes(raw)
        except TimeoutError:
            resp = self._error_envelope(None, "Control request timed out", error_code=ErrorCode.TIMEOUT)
        except Exception as exc:
            resp = self._error_envelope(None, f"Internal control server error: {exc}", error_code=ErrorCode.INTERNAL_ERROR)
        try:
            writer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _dispatch_bytes(self, raw: bytes) -> dict[str, Any]:
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._error_envelope(None, f"Invalid JSON request: {exc}", error_code="invalid_json")
        if not isinstance(req, dict):
            return self._error_envelope(None, "Request payload must be a JSON object", error_code="invalid_request")

        req_id, is_jsonrpc = req.get("id"), req.get("jsonrpc") == "2.0"
        if "jsonrpc" in req and not is_jsonrpc:
            return self._error_envelope(req_id, "Unsupported jsonrpc version; expected '2.0'", error_code="invalid_request", jsonrpc=True)
        op = req.get("method") if is_jsonrpc else (req.get("op") or req.get("method") or ("capability.call" if "capability" in req else ""))
        if not isinstance(op, str) or not op.strip():
            return self._error_envelope(req_id, "Missing request 'op' or 'method'", error_code="invalid_request", jsonrpc=is_jsonrpc)
        params = req.get("params") if is_jsonrpc and isinstance(req.get("params"), Mapping) else req
        try:
            return await self._execute_op(op.strip(), req_id, params, req, is_jsonrpc)
        except OmniDeskError as exc:
            return self._error_envelope(req_id, exc.message, error_code=exc.code, jsonrpc=is_jsonrpc, details=exc.details)
        except Exception as exc:
            return self._error_envelope(req_id, str(exc) or type(exc).__name__, error_code=ErrorCode.INTERNAL_ERROR, jsonrpc=is_jsonrpc)

    async def _execute_op(self, op: str, req_id: Any, params: Mapping[str, Any], raw_req: Mapping[str, Any], is_jsonrpc: bool) -> dict[str, Any]:
        if op in {"ping", "health", "core.ping"}:
            return self._ok_envelope(req_id, {"status": "ok", "capabilities": len(self._capabilities.list_capabilities()), "plugins": len(self._plugins.list_plugins()) if self._plugins else 0}, jsonrpc=is_jsonrpc)
        if op == "capability.list":
            return self._ok_envelope(req_id, {"capabilities": [c.to_dict() for c in self._capabilities.list_capabilities()]}, jsonrpc=is_jsonrpc)
        if op == "capability.call":
            cap_id = params.get("capability") or params.get("capability_id") or raw_req.get("capability")
            if not isinstance(cap_id, str) or not cap_id.strip():
                return self._error_envelope(req_id, "Missing 'capability' ID", error_code=ErrorCode.INVALID_ARGUMENT, jsonrpc=is_jsonrpc)
            payload = params.get("payload", params.get("request", {}))
            if payload is not None and not isinstance(payload, Mapping):
                return self._error_envelope(req_id, "Capability 'payload' must be a JSON object", error_code=ErrorCode.INVALID_ARGUMENT, jsonrpc=is_jsonrpc)
            try:
                res = await self._capabilities.call(cap_id.strip(), payload or {}, timeout=params.get("timeout"), isolate_errors=True)
            except CapabilityNotFoundError as exc:
                return self._error_envelope(req_id, exc.message, error_code=ErrorCode.CAPABILITY_NOT_FOUND, jsonrpc=is_jsonrpc, details=exc.details)
            except (InvalidCapabilityIdError, InvalidArgumentError) as exc:
                return self._error_envelope(req_id, exc.message, error_code=exc.code, jsonrpc=is_jsonrpc, details=exc.details)
            if not res.ok:
                err_code = str(res.data.get("error_code") or ErrorCode.PLUGIN_FAULT)
                return self._error_envelope(req_id, res.error or "Capability failed", error_code=err_code, provider_plugin_id=res.provider_plugin_id, jsonrpc=is_jsonrpc, details=res.data)
            return self._ok_envelope(req_id, res.data, provider_plugin_id=res.provider_plugin_id, jsonrpc=is_jsonrpc)
        if op in {"bus.publish", "bus.emit"}:
            topic = params.get("topic")
            if not isinstance(topic, str) or not topic.strip():
                return self._error_envelope(req_id, "Missing 'topic'", error_code=ErrorCode.INVALID_ARGUMENT, jsonrpc=is_jsonrpc)
            pub = await self._bus.publish(topic.strip(), params.get("payload", {}), source_plugin_id=str(params.get("source_plugin_id") or raw_req.get("caller") or "control.sock"))
            return self._ok_envelope(req_id, {"event_id": pub.event.event_id, "topic": pub.event.topic, "delivered": pub.delivered, "failed": pub.failed}, jsonrpc=is_jsonrpc)
        if op.startswith("plugin."):
            return await self._execute_plugin_op(op, req_id, params, is_jsonrpc)
        return self._error_envelope(req_id, f"Unknown control operation: {op}", error_code="unknown_operation", jsonrpc=is_jsonrpc)

    async def _execute_plugin_op(self, op: str, req_id: Any, params: Mapping[str, Any], is_jsonrpc: bool) -> dict[str, Any]:
        if self._plugins is None:
            return self._error_envelope(req_id, "PluginManager is not attached to ControlServer", error_code=ErrorCode.NOT_CONFIGURED, jsonrpc=is_jsonrpc)
        if op == "plugin.list":
            return self._ok_envelope(req_id, {"plugins": [r.to_dict() for r in self._plugins.list_plugins()]}, jsonrpc=is_jsonrpc)
        target = params.get("plugin_id") or params.get("path")
        if not isinstance(target, str) or not target.strip():
            return self._error_envelope(req_id, "Missing 'plugin_id'", error_code=ErrorCode.INVALID_ARGUMENT, jsonrpc=is_jsonrpc)
        pid = target.strip()
        try:
            if op == "plugin.load":
                rec = await self._plugins.load_plugin(pid, isolate_errors=False)
                return self._ok_envelope(req_id, rec.to_dict(), provider_plugin_id=rec.plugin_id, jsonrpc=is_jsonrpc)
            if op == "plugin.unload":
                rec = await self._plugins.unload_plugin(pid)
                return self._ok_envelope(req_id, rec.to_dict(), provider_plugin_id=rec.plugin_id, jsonrpc=is_jsonrpc)
            if op == "plugin.reload":
                rec = await self._plugins.reload_plugin(pid, isolate_errors=False)
                return self._ok_envelope(req_id, rec.to_dict(), provider_plugin_id=rec.plugin_id, jsonrpc=is_jsonrpc)
            if op == "plugin.verify":
                res = await self._plugins.verify_plugin(pid)
                if not res.ok:
                    return self._error_envelope(req_id, "; ".join(res.errors) or "Verification failed", error_code=ErrorCode.VALIDATION_ERROR, provider_plugin_id=res.plugin_id, jsonrpc=is_jsonrpc, details=res.to_dict())
                return self._ok_envelope(req_id, res.to_dict(), provider_plugin_id=res.plugin_id, jsonrpc=is_jsonrpc)
            if op == "plugin.health":
                return self._ok_envelope(req_id, await self._plugins.health_check(pid), provider_plugin_id=pid, jsonrpc=is_jsonrpc)
        except PluginError as exc:
            return self._error_envelope(req_id, exc.message, error_code=exc.code, provider_plugin_id=exc.plugin_id or pid, jsonrpc=is_jsonrpc, details=exc.details)
        return self._error_envelope(req_id, f"Unknown plugin operation: {op}", error_code="unknown_operation", jsonrpc=is_jsonrpc)


class ControlClient:
    """Single-request Unix socket client for control.sock."""

    def __init__(self, socket_path: Path | str | None = None, *, paths: OmnideskPaths | None = None, timeout: float = 15.0, caller: str = "omnidesk-pm") -> None:
        self._socket_path = _resolve_socket_path(socket_path, paths)
        self._timeout = timeout
        self._caller = caller

    @property
    def socket_path(self) -> Path: return self._socket_path

    def _check_client_socket(self) -> None:
        if self._socket_path.is_symlink():
            raise PathSafetyError(f"Refusing symlinked control socket: {self._socket_path}")
        if not self._socket_path.exists():
            raise ControlRpcError(f"Control socket not found at {self._socket_path}", code=ErrorCode.SERVICE_OFFLINE)

    async def request(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Send one JSON request frame over control.sock and return the parsed JSON response."""
        self._check_client_socket()
        effective_timeout = self._timeout if timeout is None else timeout
        encoded = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8") + b"\n"
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(effective_timeout):
                reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
                writer.write(encoded)
                await writer.drain()
                raw, too_large = await _read_single_json_message(reader, DEFAULT_MAX_PAYLOAD_BYTES)
                if too_large:
                    raise ControlRpcError("Control response exceeded payload limit", code=ErrorCode.INTERNAL_ERROR)
                return _parse_response_bytes(raw)
        except TimeoutError as exc:
            raise ControlRpcError(f"Timed out waiting for control.sock ({effective_timeout}s)", code=ErrorCode.TIMEOUT, cause=exc) from exc
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise ControlRpcError(f"Cannot connect to control.sock at {self._socket_path}: {exc}", code=ErrorCode.SERVICE_OFFLINE, cause=exc) from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    def request_sync(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Blocking single-request Unix socket call for sync callers and CLI."""
        self._check_client_socket()
        effective_timeout = self._timeout if timeout is None else timeout
        encoded = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(effective_timeout)
                sock.connect(str(self._socket_path))
                sock.sendall(encoded)
                buf = bytearray()
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    if (nl := chunk.find(b"\n")) != -1:
                        buf.extend(chunk[:nl]); break
                    buf.extend(chunk)
                    if len(buf) > DEFAULT_MAX_PAYLOAD_BYTES:
                        raise ControlRpcError("Control response exceeded payload limit", code=ErrorCode.INTERNAL_ERROR)
            return _parse_response_bytes(bytes(buf))
        except TimeoutError as exc:
            raise ControlRpcError(f"Timed out waiting for control.sock ({effective_timeout}s)", code=ErrorCode.TIMEOUT, cause=exc) from exc
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise ControlRpcError(f"Cannot connect to control.sock at {self._socket_path}: {exc}", code=ErrorCode.SERVICE_OFFLINE, cause=exc) from exc

    @staticmethod
    def _cap_req(cap_id: str, payload: Mapping[str, Any] | None, caller: str, timeout: float | None) -> dict[str, Any]:
        req: dict[str, Any] = {"id": uuid.uuid4().hex, "op": "capability.call", "capability": cap_id, "caller": caller, "payload": dict(payload or {})}
        if timeout is not None:
            req["timeout"] = timeout
        return req

    @staticmethod
    def _to_cap_result(resp: dict[str, Any]) -> CapabilityResult:
        if resp.get("ok"):
            return CapabilityResult.ok(resp.get("data", {}), provider_plugin_id=str(resp.get("provider_plugin_id", "")))
        err_code = str(resp.get("error_code") or (resp.get("data") or {}).get("error_code") or ErrorCode.PLUGIN_FAULT)
        return CapabilityResult.fail(str(resp.get("error") or "RPC failed"), error_code=err_code, data=resp.get("data"), provider_plugin_id=str(resp.get("provider_plugin_id", "")))

    async def ping(self) -> dict[str, Any]: return await self.request({"id": uuid.uuid4().hex, "op": "ping", "caller": self._caller})
    def ping_sync(self) -> dict[str, Any]: return self.request_sync({"id": uuid.uuid4().hex, "op": "ping", "caller": self._caller})

    async def call_capability(self, capability_id: str, payload: Mapping[str, Any] | None = None, *, caller: str | None = None, timeout: float | None = None) -> CapabilityResult:
        return self._to_cap_result(await self.request(self._cap_req(capability_id, payload, caller or self._caller, timeout), timeout=timeout))

    def call_capability_sync(self, capability_id: str, payload: Mapping[str, Any] | None = None, *, caller: str | None = None, timeout: float | None = None) -> CapabilityResult:
        return self._to_cap_result(self.request_sync(self._cap_req(capability_id, payload, caller or self._caller, timeout), timeout=timeout))

    async def call_jsonrpc(self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | int | None = None, timeout: float | None = None) -> dict[str, Any]:
        return await self.request({"jsonrpc": "2.0", "id": uuid.uuid4().hex if request_id is None else request_id, "method": method, "params": dict(params or {})}, timeout=timeout)

    def call_jsonrpc_sync(self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | int | None = None, timeout: float | None = None) -> dict[str, Any]:
        return self.request_sync({"jsonrpc": "2.0", "id": uuid.uuid4().hex if request_id is None else request_id, "method": method, "params": dict(params or {})}, timeout=timeout)

    async def reload_plugin(self, plugin_id: str) -> dict[str, Any]: return await self.request({"id": uuid.uuid4().hex, "op": "plugin.reload", "plugin_id": plugin_id, "caller": self._caller})
    def reload_plugin_sync(self, plugin_id: str) -> dict[str, Any]: return self.request_sync({"id": uuid.uuid4().hex, "op": "plugin.reload", "plugin_id": plugin_id, "caller": self._caller})
    async def list_plugins(self) -> dict[str, Any]: return await self.request({"id": uuid.uuid4().hex, "op": "plugin.list", "caller": self._caller})
    def list_plugins_sync(self) -> dict[str, Any]: return self.request_sync({"id": uuid.uuid4().hex, "op": "plugin.list", "caller": self._caller})
    async def publish(self, topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]: return await self.request({"id": uuid.uuid4().hex, "op": "bus.publish", "topic": topic, "payload": dict(payload or {}), "caller": self._caller})
    def publish_sync(self, topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]: return self.request_sync({"id": uuid.uuid4().hex, "op": "bus.publish", "topic": topic, "payload": dict(payload or {}), "caller": self._caller})


__all__ = ["ControlClient", "ControlRpcError", "ControlServer", "DEFAULT_MAX_PAYLOAD_BYTES"]
