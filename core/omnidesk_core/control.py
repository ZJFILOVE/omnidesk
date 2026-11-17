"""Unix domain socket (0600) JSON-RPC & NDJSON streaming server and client for control.sock."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import select
import socket
import stat
import struct
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .bus import BusEvent, EntityBus
from .config import ConfigStore, DEFAULT_SECRET_KEYS, ensure_private_dir
from .domain import DomainRegistry
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
from .paths import OmnideskPaths, validate_plugin_id
from .plugins import PluginManager
from .registry import CapabilityRegistry, CapabilityResult, parse_capability_id
from .vault import SecretVault

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_HEARTBEAT_INTERVAL = 15.0
DEFAULT_SUBSCRIBE_QUEUE_SIZE = 256
MAX_SUBSCRIBE_TOPICS = 64
MAX_STREAM_TIMEOUT_SEC = 3600.0

_TOPIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_SECRET_KEY_NAME_RE = re.compile(
    r"(?i)\A[A-Za-z0-9_.-]*(?:api[_-]?(?:key|secret|token)|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|bearer[_-]?token|client[_-]?secret|session[_-]?secret|private[_-]?key|"
    r"gateway[_-]?key|cookie(?:_header)?|set[_-]?cookie|secret|token|password|passwd|passphrase|"
    r"authorization)\Z"
)
_RESERVED_NATIVE_PARAMS = frozenset({"id", "op", "method", "caller", "params"})

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


class _FrameTooLargeError(Exception):
    """Internal marker when a serialized outbound frame exceeds max_payload_bytes."""


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


class _NdjsonReader:
    """Incremental NDJSON line reader preserving carryover bytes and enforcing byte limits."""

    def __init__(self, reader: asyncio.StreamReader, max_bytes: int) -> None:
        self._reader = reader
        self._max_bytes = max_bytes
        self._carry = bytearray()
        self._eof = False

    async def read_frame(self) -> tuple[bytes | None, bool]:
        """Return `(line_bytes, too_large)`; `(None, False)` on clean EOF."""
        while True:
            if (nl := self._carry.find(b"\n")) != -1:
                if nl > self._max_bytes:
                    del self._carry[: nl + 1]
                    return bytes(self._carry[:0]), True
                frame = bytes(self._carry[:nl])
                del self._carry[: nl + 1]
                return frame.removesuffix(b"\r"), False

            if len(self._carry) > self._max_bytes:
                overflow = bytes(self._carry[: self._max_bytes])
                self._carry.clear()
                return overflow, True

            if self._eof:
                if not self._carry:
                    return None, False
                frame = bytes(self._carry)
                self._carry.clear()
                return frame.removesuffix(b"\r"), len(frame) > self._max_bytes

            chunk = await self._reader.read(65536)
            if not chunk:
                self._eof = True
                if not self._carry:
                    return None, False
                frame = bytes(self._carry)
                self._carry.clear()
                return frame.removesuffix(b"\r"), len(frame) > self._max_bytes
            self._carry.extend(chunk)


async def _read_single_json_message(reader: asyncio.StreamReader, max_bytes: int) -> tuple[bytes, bool]:
    raw, too_large = await _NdjsonReader(reader, max_bytes).read_frame()
    return (b"" if raw is None else raw), too_large


def _validate_subscribe_topic(raw: Any) -> str:
    if not isinstance(raw, str):
        raise InvalidArgumentError("Subscription topic pattern must be a string")
    cleaned = raw.strip()
    if not cleaned or cleaned != raw or len(cleaned) > 128 or ".." in cleaned or any(ch.isspace() for ch in cleaned):
        raise InvalidArgumentError(f"Invalid subscription topic pattern {raw!r}")
    if cleaned == "*":
        return "*"
    if cleaned.endswith(".*"):
        prefix = cleaned[:-2]
        if not prefix or not _TOPIC_TOKEN_RE.match(prefix) or ".." in prefix:
            raise InvalidArgumentError(f"Invalid wildcard topic pattern {raw!r}; expected 'prefix.*'")
        return cleaned
    if not _TOPIC_TOKEN_RE.match(cleaned):
        raise InvalidArgumentError(f"Invalid subscription topic {raw!r}")
    return cleaned


def _matches_topic_patterns(topic: str, patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        if pat == "*":
            return True
        if pat.endswith(".*"):
            if topic.startswith(pat[:-1]):
                return True
        elif topic == pat:
            return True
    return False


def _validate_non_empty_key(raw: Any, label: str = "key") -> str:
    if not isinstance(raw, str):
        raise InvalidArgumentError(f"{label} must be a non-empty string")
    cleaned = raw.strip()
    if not cleaned or cleaned != raw or "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        raise InvalidArgumentError(f"Invalid {label}: must be a trimmed non-empty string without control characters")
    return raw


def _validate_stream_timeout(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InvalidArgumentError("timeout must be a finite positive number")
    val = float(raw)
    if not math.isfinite(val) or val <= 0.0 or val > MAX_STREAM_TIMEOUT_SEC:
        raise InvalidArgumentError(f"timeout must be in (0, {MAX_STREAM_TIMEOUT_SEC}] seconds")
    return val


def _validate_caller(raw_req: Mapping[str, Any], params: Mapping[str, Any], peer_pid: int | None, peer_uid: int | None) -> str:
    c_raw = raw_req.get("caller")
    c_param = params.get("caller") if params is not raw_req else None
    if c_raw is not None and c_param is not None and c_raw != c_param:
        raise InvalidArgumentError("Conflicting 'caller' fields")
    candidate = c_param if c_param is not None else c_raw
    if candidate is not None:
        if not isinstance(candidate, str):
            raise InvalidArgumentError("'caller' must be a non-empty string")
        cleaned = candidate.strip()
        if not cleaned or cleaned != candidate or len(cleaned) > 128 or any(ord(ch) < 32 for ch in cleaned):
            raise InvalidArgumentError("'caller' must be a valid printable identifier (<= 128 chars)")
        return cleaned
    return f"uid:{peer_uid}/pid:{peer_pid}" if peer_uid is not None else "anonymous"


def _check_peer_credentials(writer: asyncio.StreamWriter) -> tuple[int | None, int | None]:
    if not hasattr(socket, "SO_PEERCRED"):
        return None, None
    sock = writer.get_extra_info("socket")
    if sock is None or getattr(sock, "family", None) != socket.AF_UNIX:
        return None, None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", raw)
    except OSError as exc:
        raise OmniDeskError(f"Failed to verify control.sock peer credentials: {exc}", code=ErrorCode.PERMISSION_DENIED) from exc
    if hasattr(os, "geteuid") and uid != os.geteuid():
        raise OmniDeskError(f"Refusing control.sock connection from unauthorized UID {uid}", code=ErrorCode.PERMISSION_DENIED)
    return pid, uid


def _is_peer_closed(writer: asyncio.StreamWriter) -> bool:
    if writer.is_closing():
        return True
    sock = writer.get_extra_info("socket")
    if sock is None:
        return True
    try:
        fd = sock.fileno()
    except (OSError, ValueError):
        return True
    if fd < 0:
        return True
    if hasattr(select, "poll"):
        try:
            poller = select.poll()
            poller.register(fd, select.POLLERR | select.POLLHUP)
            return any(ev & (select.POLLERR | select.POLLHUP) for _, ev in poller.poll(0))
        except OSError:
            return True
    return False


def _sanitize_json_value(value: Any, extra_secrets: Iterable[str] = (), *, secret_keys: set[str] | None = None) -> Any:
    """Return a JSON-serializable copy with secret-named keys and vaulted values redacted."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_secrets(value, extra_secrets)
    if isinstance(value, Path):
        return str(value)
    if callable(value):
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        return _sanitize_json_value(dumped, extra_secrets, secret_keys=secret_keys)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            ks = str(k)
            is_sec = bool(_SECRET_KEY_NAME_RE.match(ks) or (secret_keys and ks in secret_keys))
            if is_sec and v is not None:
                out[ks] = "[REDACTED]"
            else:
                out[ks] = _sanitize_json_value(v, extra_secrets, secret_keys=secret_keys)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_json_value(item, extra_secrets, secret_keys=secret_keys) for item in value]
    return redact_secrets(str(value), extra_secrets)


def _contains_nested_secret_key(value: Any, secret_keys: set[str]) -> str | None:
    if isinstance(value, Mapping):
        for k, v in value.items():
            ks = str(k)
            if ks in secret_keys or _SECRET_KEY_NAME_RE.match(ks) or ks.lower() in DEFAULT_SECRET_KEYS:
                return ks
            if (nested := _contains_nested_secret_key(v, secret_keys)) is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            if (nested := _contains_nested_secret_key(item, secret_keys)) is not None:
                return nested
    return None


def _ensure_json_valid(value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"Configuration value must be JSON-serializable without NaN/Inf: {exc}") from exc


async def _watch_client_disconnect(
    ndjson_reader: _NdjsonReader,
    writer: asyncio.StreamWriter,
    stop_event: asyncio.Event,
) -> str:
    """Wait until peer disconnects, sends cancel/unsubscribe, or server stops."""
    eof_seen = False
    while not stop_event.is_set():
        if _is_peer_closed(writer):
            return "peer_closed"
        if not eof_seen:
            wait_stop = asyncio.create_task(stop_event.wait())
            wait_line = asyncio.create_task(ndjson_reader.read_frame())
            try:
                done, pending = await asyncio.wait({wait_stop, wait_line}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (wait_stop, wait_line):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(wait_stop, wait_line, return_exceptions=True)
            if wait_stop in done or stop_event.is_set():
                return "server_stop"
            try:
                raw, too_large = wait_line.result()
            except OSError:
                return "read_error"
            if too_large:
                return "client_oversized_frame"
            if raw is None:
                eof_seen = True
                if _is_peer_closed(writer):
                    return "peer_closed"
            elif raw.strip():
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(msg, Mapping):
                    ctrl = str(msg.get("op") or msg.get("method") or "").strip().lower()
                    if ctrl in {"cancel", "unsubscribe", "bus.unsubscribe", "stream.cancel"}:
                        return "client_cancelled"
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.05)
                return "server_stop"
            except TimeoutError:
                pass
    return "server_stop"


class ControlServer:
    """Asyncio Unix socket RPC & streaming server listening on 0600 control.sock."""

    def __init__(
        self,
        *,
        socket_path: Path | str | None = None,
        paths: OmnideskPaths | None = None,
        capabilities: CapabilityRegistry | None = None,
        bus: EntityBus | None = None,
        plugins: PluginManager | None = None,
        domains: DomainRegistry | None = None,
        config_store: ConfigStore | None = None,
        secret_vault: SecretVault | None = None,
        secret_provider: Callable[[], Iterable[str]] | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        request_timeout: float = 30.0,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        max_subscribe_queue: int = DEFAULT_SUBSCRIBE_QUEUE_SIZE,
    ) -> None:
        self._paths = paths or (plugins.paths if plugins is not None else OmnideskPaths.from_env())
        self._socket_path = _resolve_socket_path(socket_path, self._paths)
        self._plugins = plugins
        self._bus = bus or (plugins.bus if plugins is not None else EntityBus())
        self._capabilities = capabilities or (plugins.capabilities if plugins is not None else CapabilityRegistry(bus=self._bus))
        self._domains = domains or (plugins.domains if plugins is not None else DomainRegistry())
        self._config_store = config_store or (plugins.config_store if plugins is not None else ConfigStore(self._paths.config_dir))
        self._secret_vault = secret_vault or (plugins.secret_vault if plugins is not None else SecretVault(paths=self._paths))
        self._secret_provider = secret_provider or self._secret_vault.secret_values
        self._max_payload_bytes = max_payload_bytes
        self._request_timeout = request_timeout
        self._heartbeat_interval = max(0.05, float(heartbeat_interval))
        self._max_subscribe_queue = max(1, int(max_subscribe_queue))
        self._server: asyncio.AbstractServer | None = None
        self._stop_event = asyncio.Event()
        self._client_tasks: set[asyncio.Task[Any]] = set()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    def _extra_secrets(self) -> tuple[str, ...]:
        try:
            return tuple(self._secret_provider()) if self._secret_provider is not None else ()
        except Exception:
            return ()

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._extra_secrets())

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
        self._stop_event.clear()
        self._prepare_socket_path()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self._socket_path))
        os.chmod(self._socket_path, 0o600)
        return self._socket_path

    async def stop(self) -> None:
        """Stop the server, cancel active client streams, and remove control.sock."""
        self._stop_event.set()
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = list(self._client_tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.25)
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        if self._socket_path.exists() and not self._socket_path.is_symlink():
            self._socket_path.unlink(missing_ok=True)

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    def _error_envelope(
        self,
        req_id: Any,
        message: str,
        *,
        error_code: str,
        provider_plugin_id: str = "",
        jsonrpc: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_msg, code_str = self._redact(message), str(error_code)
        det = _sanitize_json_value(dict(details or {}), self._extra_secrets())
        if jsonrpc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": _JSONRPC_CODES.get(code_str, -32000), "message": safe_msg, "data": {"error_code": code_str, **det}},
            }
        return {
            "id": req_id,
            "ok": False,
            "provider_plugin_id": provider_plugin_id,
            "data": {"error_code": code_str, **det},
            "error": safe_msg,
            "error_code": code_str,
            "error_detail": {"code": code_str, "message": safe_msg, "details": det},
        }

    def _ok_envelope(self, req_id: Any, data: Mapping[str, Any], *, provider_plugin_id: str = "", jsonrpc: bool = False) -> dict[str, Any]:
        if jsonrpc:
            return {"jsonrpc": "2.0", "id": req_id, "result": {"ok": True, "provider_plugin_id": provider_plugin_id, "data": dict(data)}}
        return {"id": req_id, "ok": True, "provider_plugin_id": provider_plugin_id, "data": dict(data), "error": ""}

    def _stream_error_frame(
        self,
        req_id: Any,
        message: str,
        *,
        error_code: str,
        provider_plugin_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        env = self._error_envelope(req_id, message, error_code=error_code, provider_plugin_id=provider_plugin_id, details=details)
        return {"type": "error", "request_id": req_id, **env}

    def _encode_frame(self, payload: Mapping[str, Any]) -> bytes:
        try:
            encoded = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ControlRpcError(f"Unserializable response frame: {exc}", code=ErrorCode.INTERNAL_ERROR, cause=exc) from exc
        if len(encoded) > self._max_payload_bytes:
            raise _FrameTooLargeError()
        return encoded

    async def _write_frame(self, writer: asyncio.StreamWriter, payload: Mapping[str, Any]) -> bool:
        try:
            encoded = self._encode_frame(payload)
            writer.write(encoded)
            await writer.drain()
            return True
        except OSError:
            return False

    async def _write_error_safe(self, writer: asyncio.StreamWriter, payload: Mapping[str, Any]) -> bool:
        try:
            return await self._write_frame(writer, payload)
        except _FrameTooLargeError:
            return False

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        ndjson_reader = _NdjsonReader(reader, self._max_payload_bytes)
        try:
            try:
                peer_pid, peer_uid = _check_peer_credentials(writer)
            except OmniDeskError as exc:
                await self._write_error_safe(writer, self._error_envelope(None, exc.message, error_code=exc.code))
                return

            try:
                async with asyncio.timeout(self._request_timeout):
                    raw, too_large = await ndjson_reader.read_frame()
            except TimeoutError:
                await self._write_error_safe(writer, self._error_envelope(None, "Control request timed out", error_code=ErrorCode.TIMEOUT))
                return

            if too_large:
                await self._write_error_safe(
                    writer, self._error_envelope(None, f"Request exceeds {self._max_payload_bytes} byte limit", error_code="invalid_request")
                )
                return
            if raw is None or not raw.strip():
                await self._write_error_safe(writer, self._error_envelope(None, "Empty control.sock request", error_code="invalid_request"))
                return

            await self._dispatch_request(ndjson_reader, writer, raw, peer_pid, peer_uid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._write_error_safe(
                writer, self._error_envelope(None, f"Internal control server error: {exc}", error_code=ErrorCode.INTERNAL_ERROR)
            )
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _dispatch_request(
        self,
        ndjson_reader: _NdjsonReader,
        writer: asyncio.StreamWriter,
        raw: bytes,
        peer_pid: int | None,
        peer_uid: int | None,
    ) -> None:
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await self._write_error_safe(writer, self._error_envelope(None, f"Invalid JSON request: {exc}", error_code="invalid_json"))
            return
        if not isinstance(req, dict):
            await self._write_error_safe(writer, self._error_envelope(None, "Request payload must be a JSON object", error_code="invalid_request"))
            return

        req_id, is_jsonrpc = req.get("id"), req.get("jsonrpc") == "2.0"
        if "jsonrpc" in req and not is_jsonrpc:
            await self._write_error_safe(
                writer, self._error_envelope(req_id, "Unsupported jsonrpc version; expected '2.0'", error_code="invalid_request", jsonrpc=True)
            )
            return

        op = req.get("method") if is_jsonrpc else (req.get("op") or req.get("method") or ("capability.call" if "capability" in req else ""))
        if not isinstance(op, str) or not op.strip():
            await self._write_error_safe(
                writer, self._error_envelope(req_id, "Missing request 'op' or 'method'", error_code="invalid_request", jsonrpc=is_jsonrpc)
            )
            return
        op_name = op.strip()

        try:
            params = self._extract_params(req, is_jsonrpc=is_jsonrpc)
            caller = _validate_caller(req, params, peer_pid, peer_uid)
        except OmniDeskError as exc:
            await self._write_error_safe(
                writer, self._error_envelope(req_id, exc.message, error_code=exc.code, jsonrpc=is_jsonrpc, details=exc.details)
            )
            return

        logger.info(
            "control.sock op=%s caller=%s peer_uid=%s peer_pid=%s req_id=%s",
            self._redact(op_name),
            self._redact(caller),
            peer_uid,
            peer_pid,
            self._redact(str(req_id)),
        )

        if op_name == "bus.subscribe":
            await self._handle_bus_subscribe(ndjson_reader, writer, req_id, params, is_jsonrpc, caller)
            return
        if op_name == "capability.stream":
            await self._handle_capability_stream(ndjson_reader, writer, req_id, params, req, is_jsonrpc, caller)
            return

        try:
            async with asyncio.timeout(self._request_timeout):
                resp = await self._execute_op(op_name, req_id, params, req, is_jsonrpc, caller)
        except TimeoutError:
            resp = self._error_envelope(req_id, "Control request timed out", error_code=ErrorCode.TIMEOUT, jsonrpc=is_jsonrpc)
        except OmniDeskError as exc:
            resp = self._error_envelope(req_id, exc.message, error_code=exc.code, jsonrpc=is_jsonrpc, details=exc.details)
        except Exception as exc:
            resp = self._error_envelope(req_id, str(exc) or type(exc).__name__, error_code=ErrorCode.INTERNAL_ERROR, jsonrpc=is_jsonrpc)

        try:
            await self._write_frame(writer, resp)
        except _FrameTooLargeError:
            err = self._error_envelope(
                req_id,
                f"Response exceeds {self._max_payload_bytes} byte limit",
                error_code=ErrorCode.INTERNAL_ERROR,
                jsonrpc=is_jsonrpc,
            )
            try:
                await self._write_frame(writer, err)
            except _FrameTooLargeError:
                pass

    @staticmethod
    def _extract_params(req: Mapping[str, Any], *, is_jsonrpc: bool) -> Mapping[str, Any]:
        if is_jsonrpc:
            raw_params = req.get("params")
            if raw_params is None:
                return {}
            if not isinstance(raw_params, Mapping):
                raise InvalidArgumentError("JSON-RPC 'params' must be a JSON object")
            return raw_params
        if "params" not in req or req["params"] is None:
            return req
        nested = req["params"]
        if not isinstance(nested, Mapping):
            raise InvalidArgumentError("Request 'params' must be a JSON object")
        merged = dict(nested)
        for k, v in req.items():
            if k in _RESERVED_NATIVE_PARAMS:
                continue
            if k in merged and merged[k] != v:
                raise InvalidArgumentError(f"Conflicting field {k!r} in request and 'params'")
            merged.setdefault(k, v)
        return merged

    async def _handle_bus_subscribe(
        self,
        ndjson_reader: _NdjsonReader,
        writer: asyncio.StreamWriter,
        req_id: Any,
        params: Mapping[str, Any],
        is_jsonrpc: bool,
        caller: str,
    ) -> None:
        try:
            raw_topics = params.get("topics")
            if not isinstance(raw_topics, list) or not raw_topics:
                raise InvalidArgumentError("bus.subscribe requires a non-empty 'topics' string array")
            if len(raw_topics) > MAX_SUBSCRIBE_TOPICS:
                raise InvalidArgumentError(f"bus.subscribe supports at most {MAX_SUBSCRIBE_TOPICS} topic patterns")
            patterns = tuple(dict.fromkeys(_validate_subscribe_topic(t) for t in raw_topics))

            raw_since = params.get("since_event_id")
            since_event_id: str | None = None
            if raw_since is not None:
                since_event_id = _validate_non_empty_key(raw_since, "since_event_id")
                if len(since_event_id) > 128 or any(ch.isspace() for ch in since_event_id):
                    raise InvalidArgumentError("since_event_id must not contain whitespace or exceed 128 chars")

            hb_raw = params.get("heartbeat_interval", params.get("heartbeat_sec"))
            if hb_raw is not None:
                hb_interval = _validate_stream_timeout(hb_raw)
                assert hb_interval is not None
                if hb_interval < 0.05 or hb_interval > 300.0:
                    raise InvalidArgumentError("heartbeat_interval must be between 0.05 and 300 seconds")
            else:
                hb_interval = self._heartbeat_interval
        except OmniDeskError as exc:
            await self._write_error_safe(
                writer, self._error_envelope(req_id, exc.message, error_code=exc.code, jsonrpc=is_jsonrpc, details=exc.details)
            )
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=self._max_subscribe_queue)
        closed = False

        def _enqueue(evt: BusEvent) -> None:
            if closed:
                return
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(evt)
            except asyncio.QueueFull:
                pass

        def _on_bus_event(evt: BusEvent) -> None:
            if closed or not _matches_topic_patterns(evt.topic, patterns):
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                _enqueue(evt)
            elif not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(_enqueue, evt)
                except RuntimeError:
                    pass

        sub = self._bus.subscribe("*", _on_bus_event, plugin_id="control-sock")
        disconnect_task: asyncio.Task[str] | None = None
        get_task: asyncio.Task[BusEvent] | None = None
        try:
            history = self._bus.history()
            replay: list[BusEvent] = []
            since_found: bool | None = None
            if since_event_id is not None:
                idx = next((i for i, e in enumerate(history) if e.event_id == since_event_id), None)
                if idx is not None:
                    since_found = True
                    replay = [e for e in history[idx + 1 :] if _matches_topic_patterns(e.topic, patterns)]
                    replay = replay[-self._max_subscribe_queue :]
                else:
                    since_found = False

            ack_data = {
                "subscribed": True,
                "topics": list(patterns),
                "since_event_id": since_event_id,
                "since_found": since_found,
                "heartbeat_interval": hb_interval,
                "caller": caller,
            }
            if not await self._write_error_safe(writer, self._ok_envelope(req_id, ack_data, jsonrpc=is_jsonrpc)):
                return

            sent_ids: deque[str] = deque(maxlen=max(256, self._max_subscribe_queue * 2))
            for evt in replay:
                sent_ids.append(evt.event_id)
                if not await self._write_bus_event(writer, evt):
                    return

            disconnect_task = asyncio.create_task(_watch_client_disconnect(ndjson_reader, writer, self._stop_event))
            while not disconnect_task.done() and not self._stop_event.is_set():
                get_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {get_task, disconnect_task},
                    timeout=hb_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)
                    get_task = None
                    if disconnect_task.done() or _is_peer_closed(writer):
                        break
                    try:
                        if not await self._write_frame(writer, {"type": "heartbeat", "ts": time.time()}):
                            break
                    except _FrameTooLargeError:
                        break
                    continue

                if disconnect_task in done:
                    if not get_task.done():
                        get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)
                    get_task = None
                    break

                evt = get_task.result()
                get_task = None
                if evt.event_id in sent_ids:
                    continue
                sent_ids.append(evt.event_id)
                if not await self._write_bus_event(writer, evt):
                    break
        finally:
            closed = True
            self._bus.unsubscribe(sub)
            for t in (get_task, disconnect_task):
                if t is not None and not t.done():
                    t.cancel()
            pending_cleanup = [t for t in (get_task, disconnect_task) if t is not None]
            if pending_cleanup:
                await asyncio.gather(*pending_cleanup, return_exceptions=True)
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _write_bus_event(self, writer: asyncio.StreamWriter, evt: BusEvent) -> bool:
        frame = {
            "type": "event",
            "event_id": evt.event_id,
            "topic": evt.topic,
            "payload": _sanitize_json_value(evt.payload, self._extra_secrets()),
            "source_plugin_id": evt.source_plugin_id,
            "timestamp": evt.timestamp,
        }
        try:
            return await self._write_frame(writer, frame)
        except _FrameTooLargeError:
            logger.warning("Dropping oversized bus event topic=%s event_id=%s (> %d bytes)", evt.topic, evt.event_id, self._max_payload_bytes)
            return True

    async def _handle_capability_stream(
        self,
        ndjson_reader: _NdjsonReader,
        writer: asyncio.StreamWriter,
        req_id: Any,
        params: Mapping[str, Any],
        raw_req: Mapping[str, Any],
        is_jsonrpc: bool,
        caller: str,
    ) -> None:
        try:
            cap_id, payload, timeout = self._parse_capability_args(params, raw_req)
            _ = caller
            self._capabilities.resolve(cap_id)
        except CapabilityNotFoundError as exc:
            await self._write_error_safe(
                writer, self._stream_error_frame(req_id, exc.message, error_code=ErrorCode.CAPABILITY_NOT_FOUND, details=exc.details)
            )
            return
        except OmniDeskError as exc:
            await self._write_error_safe(
                writer, self._stream_error_frame(req_id, exc.message, error_code=exc.code, details=exc.details)
            )
            return

        stream_gen = self._capabilities.stream(cap_id, payload, timeout=timeout, isolate_errors=True)

        async def _pump() -> None:
            final_result: CapabilityResult | None = None
            provider_id = ""
            try:
                async for chunk in stream_gen:
                    if chunk.provider_plugin_id:
                        provider_id = chunk.provider_plugin_id
                    if chunk.done:
                        final_result = chunk.result
                        break
                    chunk_frame = {
                        "type": "chunk",
                        "request_id": req_id,
                        "id": req_id,
                        "event": chunk.event,
                        "data": _sanitize_json_value(chunk.data, self._extra_secrets()),
                        "provider_plugin_id": chunk.provider_plugin_id,
                    }
                    try:
                        if not await self._write_frame(writer, chunk_frame):
                            return
                    except _FrameTooLargeError:
                        err = self._stream_error_frame(
                            req_id,
                            f"Stream chunk exceeds {self._max_payload_bytes} byte limit",
                            error_code=ErrorCode.INTERNAL_ERROR,
                            provider_plugin_id=chunk.provider_plugin_id,
                        )
                        try:
                            await self._write_frame(writer, err)
                        except _FrameTooLargeError:
                            pass
                        return

                if final_result is None or final_result.ok:
                    end_data = _sanitize_json_value(final_result.data, self._extra_secrets()) if final_result is not None else {}
                    end_frame = {
                        "type": "end",
                        "request_id": req_id,
                        "id": req_id,
                        "ok": True,
                        "provider_plugin_id": (final_result.provider_plugin_id if final_result else "") or provider_id,
                        "data": end_data,
                        "error": "",
                    }
                    try:
                        await self._write_frame(writer, end_frame)
                    except _FrameTooLargeError:
                        err = self._stream_error_frame(
                            req_id,
                            f"Stream end payload exceeds {self._max_payload_bytes} byte limit",
                            error_code=ErrorCode.INTERNAL_ERROR,
                            provider_plugin_id=provider_id,
                        )
                        try:
                            await self._write_frame(writer, err)
                        except _FrameTooLargeError:
                            pass
                else:
                    err_code = str(final_result.data.get("error_code") or ErrorCode.PLUGIN_FAULT)
                    err = self._stream_error_frame(
                        req_id,
                        final_result.error or "Capability stream failed",
                        error_code=err_code,
                        provider_plugin_id=final_result.provider_plugin_id or provider_id,
                        details=final_result.data,
                    )
                    try:
                        await self._write_frame(writer, err)
                    except _FrameTooLargeError:
                        pass
            finally:
                await stream_gen.aclose()

        pump_task = asyncio.create_task(_pump())
        disconnect_task = asyncio.create_task(_watch_client_disconnect(ndjson_reader, writer, self._stop_event))
        try:
            done, pending = await asyncio.wait({pump_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
            if disconnect_task in done and not pump_task.done():
                pump_task.cancel()
            for t in pending:
                if t is disconnect_task:
                    t.cancel()
            await asyncio.gather(pump_task, disconnect_task, return_exceptions=True)
        except BaseException:
            for t in (pump_task, disconnect_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(pump_task, disconnect_task, return_exceptions=True)
            raise

    @staticmethod
    def _parse_capability_args(params: Mapping[str, Any], raw_req: Mapping[str, Any]) -> tuple[str, dict[str, Any], float | None]:
        cap_a = params.get("capability")
        cap_b = params.get("capability_id")
        if cap_a is not None and cap_b is not None and cap_a != cap_b:
            raise InvalidArgumentError("Conflicting 'capability' and 'capability_id'")
        cap_id = cap_a if cap_a is not None else (cap_b if cap_b is not None else raw_req.get("capability"))
        if not isinstance(cap_id, str) or not cap_id.strip():
            raise InvalidArgumentError("Missing 'capability' ID")
        clean_cap = cap_id.strip()
        parse_capability_id(clean_cap)

        has_payload = "payload" in params and params["payload"] is not None
        has_request = "request" in params and params["request"] is not None
        if has_payload and has_request and params["payload"] != params["request"]:
            raise InvalidArgumentError("Conflicting 'payload' and 'request'")
        raw_payload = params["payload"] if has_payload else (params["request"] if has_request else {})
        if not isinstance(raw_payload, Mapping):
            raise InvalidArgumentError("Capability 'payload' must be a JSON object")
        timeout = _validate_stream_timeout(params.get("timeout"))
        return clean_cap, dict(raw_payload), timeout

    async def _execute_op(
        self,
        op: str,
        req_id: Any,
        params: Mapping[str, Any],
        raw_req: Mapping[str, Any],
        is_jsonrpc: bool,
        caller: str,
    ) -> dict[str, Any]:
        if op in {"ping", "health", "core.ping"}:
            return self._ok_envelope(
                req_id,
                {"status": "ok", "capabilities": len(self._capabilities.list_capabilities()), "plugins": len(self._plugins.list_plugins()) if self._plugins else 0},
                jsonrpc=is_jsonrpc,
            )
        if op == "capability.list":
            return self._ok_envelope(
                req_id, {"capabilities": [c.to_dict() for c in self._capabilities.list_capabilities()]}, jsonrpc=is_jsonrpc
            )
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
                return self._error_envelope(
                    req_id, res.error or "Capability failed", error_code=err_code, provider_plugin_id=res.provider_plugin_id, jsonrpc=is_jsonrpc, details=res.data
                )
            return self._ok_envelope(req_id, res.data, provider_plugin_id=res.provider_plugin_id, jsonrpc=is_jsonrpc)
        if op in {"bus.publish", "bus.emit"}:
            topic = params.get("topic")
            if not isinstance(topic, str) or not topic.strip():
                return self._error_envelope(req_id, "Missing 'topic'", error_code=ErrorCode.INVALID_ARGUMENT, jsonrpc=is_jsonrpc)
            pub = await self._bus.publish(
                topic.strip(), params.get("payload", {}), source_plugin_id=str(params.get("source_plugin_id") or caller or "control.sock")
            )
            return self._ok_envelope(
                req_id,
                {"event_id": pub.event.event_id, "topic": pub.event.topic, "delivered": pub.delivered, "failed": pub.failed},
                jsonrpc=is_jsonrpc,
            )
        if op == "domain.snapshot":
            return self._execute_domain_snapshot(req_id, params, is_jsonrpc)
        if op.startswith("config."):
            return self._execute_config_op(op, req_id, params, is_jsonrpc)
        if op.startswith("plugin."):
            return await self._execute_plugin_op(op, req_id, params, is_jsonrpc)
        return self._error_envelope(req_id, f"Unknown control operation: {op}", error_code="unknown_operation", jsonrpc=is_jsonrpc)

    def _execute_domain_snapshot(self, req_id: Any, params: Mapping[str, Any], is_jsonrpc: bool) -> dict[str, Any]:
        raw_domain = params.get("domain")
        if raw_domain is not None and not isinstance(raw_domain, str):
            raise InvalidArgumentError("'domain' must be a string")
        raw_pid = params.get("plugin_id")
        if raw_pid is not None and not isinstance(raw_pid, str):
            raise InvalidArgumentError("'plugin_id' must be a string")
        domain = raw_domain.strip().lower() if isinstance(raw_domain, str) else None
        if domain == "":
            raise InvalidArgumentError("'domain' must not be empty")
        plugin_id = validate_plugin_id(raw_pid) if raw_pid is not None else None
        if self._plugins is not None:
            snapshot = self._plugins.domain_snapshot(domain=domain, plugin_id=plugin_id)
        else:
            if domain is not None and domain not in {"core", "media", "text", "book"}:
                raise InvalidArgumentError(f"Unsupported domain {raw_domain!r}; expected 'core', 'media', 'text', or 'book'")
            extra = self._extra_secrets()
            snapshot = {
                "media_sources": [_sanitize_json_value(e.to_dict(), extra) for e in (self._domains.list_media_sources(plugin_id=plugin_id) if domain in (None, "media") else [])],
                "text_sources": [_sanitize_json_value(e.to_dict(), extra) for e in (self._domains.list_text_sources(plugin_id=plugin_id) if domain in (None, "text") else [])],
                "book_sources": [_sanitize_json_value(e.to_dict(), extra) for e in (self._domains.list_book_sources(plugin_id=plugin_id) if domain in (None, "book") else [])],
                "workspace_tabs": [],
                "dock_widgets": [],
            }
        return self._ok_envelope(req_id, snapshot, jsonrpc=is_jsonrpc)

    def _plugin_config_context(self, plugin_id: str) -> tuple[Any, set[str], set[str], list[dict[str, Any]]]:
        manifest = None
        if self._plugins is not None:
            rec = self._plugins.get(plugin_id)
            if rec is None:
                self._plugins.discover()
                rec = self._plugins.get(plugin_id)
            manifest = rec.manifest if rec is not None else None
        schema_fields = manifest.config_schema if manifest is not None else None
        if schema_fields is not None:
            self._config_store.register_schema(plugin_id, schema_fields)
            schema_secrets = manifest.secret_keys()
            schema_plain = {f.key for f in schema_fields if f.type != "secret"}
            safe_schema: list[dict[str, Any]] = []
            for f in schema_fields:
                dumped = f.model_dump()
                if f.type == "secret":
                    dumped["default"] = ""
                safe_schema.append(dumped)
            return schema_fields, schema_secrets, schema_plain, safe_schema
        return None, set(), set(), []

    def _build_config_entry(self, plugin_id: str) -> dict[str, Any]:
        schema_fields, schema_secrets, schema_plain, safe_schema = self._plugin_config_context(plugin_id)
        vault_keys = set(self._secret_vault.list_keys(plugin_id))
        plain_cfg = self._config_store.read(plugin_id, config_schema=schema_fields, secret_keys=vault_keys, apply_defaults=True)
        effective_secrets = schema_secrets | vault_keys | (DEFAULT_SECRET_KEYS - schema_plain)
        clean_plain = _sanitize_json_value(plain_cfg, self._extra_secrets(), secret_keys=effective_secrets)
        all_secret_keys = sorted(schema_secrets | vault_keys)
        secrets_status = {k: {"set": self._secret_vault.has(plugin_id, k)} for k in all_secret_keys}
        return {
            "plugin_id": plugin_id,
            "config": clean_plain,
            "secrets": secrets_status,
            "secret_keys": all_secret_keys,
            "schema": safe_schema,
        }

    def _execute_config_op(self, op: str, req_id: Any, params: Mapping[str, Any], is_jsonrpc: bool) -> dict[str, Any]:
        if op == "config.list":
            raw_pid = params.get("plugin_id")
            if raw_pid is not None:
                if not isinstance(raw_pid, str):
                    raise InvalidArgumentError("'plugin_id' must be a string")
                pid = validate_plugin_id(raw_pid)
                entries = [self._build_config_entry(pid)]
            else:
                ids: set[str] = set(self._config_store.list_plugins()) | set(self._secret_vault.list_plugins())
                if self._plugins is not None:
                    ids.update(r.plugin_id for r in self._plugins.list_plugins())
                entries = [self._build_config_entry(pid) for pid in sorted(ids)]
            return self._ok_envelope(req_id, {"plugins": entries, "count": len(entries)}, jsonrpc=is_jsonrpc)

        raw_pid = params.get("plugin_id")
        if not isinstance(raw_pid, str) or not raw_pid.strip():
            raise InvalidArgumentError(f"{op} requires string 'plugin_id'")
        pid = validate_plugin_id(raw_pid)
        schema_fields, schema_secrets, schema_plain, _ = self._plugin_config_context(pid)
        vault_keys = set(self._secret_vault.list_keys(pid))
        effective_secret_keys = schema_secrets | vault_keys | (DEFAULT_SECRET_KEYS - schema_plain)

        def _is_secret(k: str) -> bool:
            return k in schema_secrets or k in vault_keys or (k.lower() in DEFAULT_SECRET_KEYS and k not in schema_plain)

        if op == "config.status":
            raw_key = params.get("key")
            if raw_key is not None:
                key = _validate_non_empty_key(raw_key)
                return self._ok_envelope(
                    req_id,
                    {"plugin_id": pid, "key": key, "is_secret": _is_secret(key), "set": self._secret_vault.has(pid, key)},
                    provider_plugin_id=pid,
                    jsonrpc=is_jsonrpc,
                )
            entry = self._build_config_entry(pid)
            return self._ok_envelope(
                req_id,
                {"plugin_id": pid, "secrets": entry["secrets"], "secret_keys": entry["secret_keys"]},
                provider_plugin_id=pid,
                jsonrpc=is_jsonrpc,
            )

        if op == "config.get":
            raw_status = params.get("status")
            if raw_status is not None and not isinstance(raw_status, bool):
                raise InvalidArgumentError("'status' must be a boolean")
            for opt_name in ("action", "mode"):
                if params.get(opt_name) is not None and not isinstance(params.get(opt_name), str):
                    raise InvalidArgumentError(f"{opt_name!r} must be a string")
            want_status = (
                raw_status is True
                or str(params.get("action") or "").strip().lower() == "status"
                or str(params.get("mode") or "").strip().lower() == "status"
            )
            raw_key = params.get("key")
            if raw_key is None:
                return self._ok_envelope(req_id, self._build_config_entry(pid), provider_plugin_id=pid, jsonrpc=is_jsonrpc)
            key = _validate_non_empty_key(raw_key)
            if _is_secret(key):
                if not want_status:
                    return self._error_envelope(
                        req_id,
                        "Reading plaintext secret is forbidden; only set and status operations are allowed",
                        error_code=ErrorCode.PERMISSION_DENIED,
                        provider_plugin_id=pid,
                        jsonrpc=is_jsonrpc,
                    )
                return self._ok_envelope(
                    req_id,
                    {"plugin_id": pid, "key": key, "is_secret": True, "set": self._secret_vault.has(pid, key)},
                    provider_plugin_id=pid,
                    jsonrpc=is_jsonrpc,
                )
            entry = self._build_config_entry(pid)
            cfg = entry["config"]
            return self._ok_envelope(
                req_id,
                {"plugin_id": pid, "key": key, "value": cfg.get(key), "is_secret": False, "set": key in cfg},
                provider_plugin_id=pid,
                jsonrpc=is_jsonrpc,
            )

        if op == "config.set":
            return self._handle_config_set(req_id, pid, params, is_jsonrpc, schema_fields, schema_secrets, schema_plain, effective_secret_keys, _is_secret)

        return self._error_envelope(req_id, f"Unknown config operation: {op}", error_code="unknown_operation", jsonrpc=is_jsonrpc)

    def _handle_config_set(
        self,
        req_id: Any,
        pid: str,
        params: Mapping[str, Any],
        is_jsonrpc: bool,
        schema_fields: Any,
        schema_secrets: set[str],
        schema_plain: set[str],
        effective_secret_keys: set[str],
        is_secret_fn: Callable[[str], bool],
    ) -> dict[str, Any]:
        replace_flag = params.get("replace", False)
        if not isinstance(replace_flag, bool):
            raise InvalidArgumentError("'replace' must be a boolean")

        has_key = "key" in params and params["key"] is not None
        patch_fields = [name for name in ("values", "config", "patch") if name in params and params[name] is not None]
        has_secrets_map = "secrets" in params and params["secrets"] is not None

        if len(patch_fields) > 1:
            raise InvalidArgumentError("Specify at most one of 'values', 'config', or 'patch'")
        if has_key and (patch_fields or has_secrets_map):
            raise InvalidArgumentError("Cannot combine single 'key' with 'values'/'config'/'patch'/'secrets'")
        if not has_key and not patch_fields and not has_secrets_map:
            raise InvalidArgumentError("config.set requires 'key'/'value' or 'values'/'config'/'patch'/'secrets'")

        plain_patch: dict[str, Any] = {}
        secret_patch: dict[str, str | None] = {}

        if has_key:
            key = _validate_non_empty_key(params["key"])
            if "value" not in params:
                raise InvalidArgumentError("config.set with 'key' requires 'value'")
            val = params["value"]
            flag_a, flag_b = params.get("is_secret"), params.get("secret")
            for fval, fname in ((flag_a, "is_secret"), (flag_b, "secret")):
                if fval is not None and not isinstance(fval, bool):
                    raise InvalidArgumentError(f"{fname!r} must be a boolean")
            if flag_a is not None and flag_b is not None and flag_a is not flag_b:
                raise InvalidArgumentError("Conflicting 'is_secret' and 'secret' flags")
            explicit_secret = flag_a if flag_a is not None else flag_b
            target_secret = is_secret_fn(key) if explicit_secret is None else explicit_secret
            if explicit_secret is True and key in schema_plain:
                raise InvalidArgumentError(f"Config key {key!r} is declared plain in plugin schema")
            if explicit_secret is False and is_secret_fn(key):
                raise InvalidArgumentError(f"Config key {key!r} is a secret field and cannot be stored as plain config")
            if target_secret:
                if val is not None and not isinstance(val, str):
                    raise InvalidArgumentError(f"Secret value for {key!r} must be a string or null")
                secret_patch[key] = val
            else:
                if (nested_sec := _contains_nested_secret_key(val, effective_secret_keys)) is not None:
                    raise InvalidArgumentError(f"Secret key {nested_sec!r} cannot be nested inside plain configuration")
                _ensure_json_valid(val)
                plain_patch[key] = val
        else:
            if patch_fields:
                raw_map = params[patch_fields[0]]
                if not isinstance(raw_map, Mapping):
                    raise InvalidArgumentError(f"{patch_fields[0]!r} must be a JSON object")
                for raw_k, val in raw_map.items():
                    key = _validate_non_empty_key(raw_k)
                    if is_secret_fn(key):
                        if val is not None and not isinstance(val, str):
                            raise InvalidArgumentError(f"Secret value for {key!r} must be a string or null")
                        secret_patch[key] = val
                    else:
                        if (nested_sec := _contains_nested_secret_key(val, effective_secret_keys)) is not None:
                            raise InvalidArgumentError(f"Secret key {nested_sec!r} cannot be nested inside plain configuration")
                        _ensure_json_valid(val)
                        plain_patch[key] = val
            if has_secrets_map:
                raw_sec = params["secrets"]
                if not isinstance(raw_sec, Mapping):
                    raise InvalidArgumentError("'secrets' must be a JSON object")
                for raw_k, val in raw_sec.items():
                    key = _validate_non_empty_key(raw_k)
                    if key in schema_plain:
                        raise InvalidArgumentError(f"Config key {key!r} is declared plain in plugin schema")
                    if val is not None and not isinstance(val, str):
                        raise InvalidArgumentError(f"Secret value for {key!r} must be a string or null")
                    secret_patch[key] = val

        for skey, sval in secret_patch.items():
            if sval is None:
                self._secret_vault.delete(pid, skey)
            else:
                self._secret_vault.set(pid, skey, sval)

        if plain_patch or replace_flag:
            secret_names = set(schema_secrets) | set(self._secret_vault.list_keys(pid)) | set(secret_patch)
            if replace_flag:
                self._config_store.write(pid, plain_patch, config_schema=schema_fields, secret_keys=secret_names)
            else:
                self._config_store.update(pid, plain_patch, config_schema=schema_fields, secret_keys=secret_names)

        entry = self._build_config_entry(pid)
        return self._ok_envelope(
            req_id,
            {
                "plugin_id": pid,
                "updated_keys": sorted(plain_patch),
                "secret_keys_updated": sorted(secret_patch),
                "config": entry["config"],
                "secrets": entry["secrets"],
            },
            provider_plugin_id=pid,
            jsonrpc=is_jsonrpc,
        )

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
                    return self._error_envelope(
                        req_id,
                        "; ".join(res.errors) or "Verification failed",
                        error_code=ErrorCode.VALIDATION_ERROR,
                        provider_plugin_id=res.plugin_id,
                        jsonrpc=is_jsonrpc,
                        details=res.to_dict(),
                    )
                return self._ok_envelope(req_id, res.to_dict(), provider_plugin_id=res.plugin_id, jsonrpc=is_jsonrpc)
            if op == "plugin.health":
                return self._ok_envelope(req_id, await self._plugins.health_check(pid), provider_plugin_id=pid, jsonrpc=is_jsonrpc)
        except PluginError as exc:
            return self._error_envelope(req_id, exc.message, error_code=exc.code, provider_plugin_id=exc.plugin_id or pid, jsonrpc=is_jsonrpc, details=exc.details)
        return self._error_envelope(req_id, f"Unknown plugin operation: {op}", error_code="unknown_operation", jsonrpc=is_jsonrpc)


class ControlClient:
    """Unix socket RPC and streaming client for control.sock."""

    def __init__(self, socket_path: Path | str | None = None, *, paths: OmnideskPaths | None = None, timeout: float = 15.0, caller: str = "omnidesk-pm") -> None:
        self._socket_path = _resolve_socket_path(socket_path, paths)
        self._timeout = timeout
        self._caller = caller

    @property
    def socket_path(self) -> Path:
        return self._socket_path

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

    async def subscribe(
        self,
        topics: Sequence[str],
        *,
        since_event_id: str | None = None,
        caller: str | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open a long-lived `bus.subscribe` NDJSON stream and yield event/heartbeat frames."""
        self._check_client_socket()
        effective_timeout = self._timeout if timeout is None else timeout
        req: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "op": "bus.subscribe",
            "topics": list(topics),
            "caller": caller or self._caller,
        }
        if since_event_id is not None:
            req["since_event_id"] = since_event_id
        encoded = json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n"
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(effective_timeout):
                reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
                writer.write(encoded)
                await writer.drain()
                ndjson = _NdjsonReader(reader, DEFAULT_MAX_PAYLOAD_BYTES)
                raw_ack, too_large = await ndjson.read_frame()
                if too_large or raw_ack is None:
                    raise ControlRpcError("Invalid bus.subscribe acknowledgement from control.sock", code=ErrorCode.INTERNAL_ERROR)
                ack = _parse_response_bytes(raw_ack)
                if not ack.get("ok"):
                    raise ControlRpcError(
                        str(ack.get("error") or "Subscription rejected"),
                        code=str(ack.get("error_code") or ErrorCode.INVALID_ARGUMENT),
                    )
            while True:
                raw, too_large = await ndjson.read_frame()
                if too_large:
                    raise ControlRpcError("Control event frame exceeded payload limit", code=ErrorCode.INTERNAL_ERROR)
                if raw is None:
                    break
                if raw.strip():
                    yield _parse_response_bytes(raw)
        except TimeoutError as exc:
            raise ControlRpcError(f"Timed out subscribing to control.sock ({effective_timeout}s)", code=ErrorCode.TIMEOUT, cause=exc) from exc
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise ControlRpcError(f"Cannot connect to control.sock at {self._socket_path}: {exc}", code=ErrorCode.SERVICE_OFFLINE, cause=exc) from exc
        finally:
            if writer is not None:
                try:
                    if not writer.is_closing():
                        writer.write(b'{"op":"unsubscribe"}\n')
                except OSError:
                    pass
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def stream_capability(
        self,
        capability_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        caller: str | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Invoke `capability.stream` and yield NDJSON `chunk`, `end`, or `error` frames."""
        self._check_client_socket()
        req: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "op": "capability.stream",
            "capability": capability_id,
            "payload": dict(payload or {}),
            "caller": caller or self._caller,
        }
        if timeout is not None:
            req["timeout"] = timeout
        encoded = json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n"
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
            writer.write(encoded)
            await writer.drain()
            ndjson = _NdjsonReader(reader, DEFAULT_MAX_PAYLOAD_BYTES)
            while True:
                raw, too_large = await ndjson.read_frame()
                if too_large:
                    raise ControlRpcError("Capability stream frame exceeded payload limit", code=ErrorCode.INTERNAL_ERROR)
                if raw is None:
                    break
                if not raw.strip():
                    continue
                frame = _parse_response_bytes(raw)
                yield frame
                if frame.get("type") in {"end", "error"}:
                    break
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
                        buf.extend(chunk[:nl])
                        break
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
        return CapabilityResult.fail(
            str(resp.get("error") or "RPC failed"), error_code=err_code, data=resp.get("data"), provider_plugin_id=str(resp.get("provider_plugin_id", ""))
        )

    async def ping(self) -> dict[str, Any]:
        return await self.request({"id": uuid.uuid4().hex, "op": "ping", "caller": self._caller})

    def ping_sync(self) -> dict[str, Any]:
        return self.request_sync({"id": uuid.uuid4().hex, "op": "ping", "caller": self._caller})

    async def call_capability(
        self, capability_id: str, payload: Mapping[str, Any] | None = None, *, caller: str | None = None, timeout: float | None = None
    ) -> CapabilityResult:
        return self._to_cap_result(await self.request(self._cap_req(capability_id, payload, caller or self._caller, timeout), timeout=timeout))

    def call_capability_sync(
        self, capability_id: str, payload: Mapping[str, Any] | None = None, *, caller: str | None = None, timeout: float | None = None
    ) -> CapabilityResult:
        return self._to_cap_result(self.request_sync(self._cap_req(capability_id, payload, caller or self._caller, timeout), timeout=timeout))

    async def call_jsonrpc(
        self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | int | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        return await self.request({"jsonrpc": "2.0", "id": uuid.uuid4().hex if request_id is None else request_id, "method": method, "params": dict(params or {})}, timeout=timeout)

    def call_jsonrpc_sync(
        self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | int | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        return self.request_sync({"jsonrpc": "2.0", "id": uuid.uuid4().hex if request_id is None else request_id, "method": method, "params": dict(params or {})}, timeout=timeout)

    async def reload_plugin(self, plugin_id: str) -> dict[str, Any]:
        return await self.request({"id": uuid.uuid4().hex, "op": "plugin.reload", "plugin_id": plugin_id, "caller": self._caller})

    def reload_plugin_sync(self, plugin_id: str) -> dict[str, Any]:
        return self.request_sync({"id": uuid.uuid4().hex, "op": "plugin.reload", "plugin_id": plugin_id, "caller": self._caller})

    async def list_plugins(self) -> dict[str, Any]:
        return await self.request({"id": uuid.uuid4().hex, "op": "plugin.list", "caller": self._caller})

    def list_plugins_sync(self) -> dict[str, Any]:
        return self.request_sync({"id": uuid.uuid4().hex, "op": "plugin.list", "caller": self._caller})

    async def publish(self, topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self.request({"id": uuid.uuid4().hex, "op": "bus.publish", "topic": topic, "payload": dict(payload or {}), "caller": self._caller})

    def publish_sync(self, topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.request_sync({"id": uuid.uuid4().hex, "op": "bus.publish", "topic": topic, "payload": dict(payload or {}), "caller": self._caller})

    async def config_list(self, *, plugin_id: str | None = None) -> dict[str, Any]:
        req: dict[str, Any] = {"id": uuid.uuid4().hex, "op": "config.list", "caller": self._caller}
        if plugin_id is not None:
            req["plugin_id"] = plugin_id
        return await self.request(req)

    async def config_get(self, plugin_id: str, *, key: str | None = None, status: bool = False) -> dict[str, Any]:
        req: dict[str, Any] = {"id": uuid.uuid4().hex, "op": "config.get", "plugin_id": plugin_id, "caller": self._caller}
        if key is not None:
            req["key"] = key
        if status:
            req["status"] = True
        return await self.request(req)

    async def config_set(
        self,
        plugin_id: str,
        *,
        key: str | None = None,
        value: Any = None,
        is_secret: bool | None = None,
        values: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str | None] | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        req: dict[str, Any] = {"id": uuid.uuid4().hex, "op": "config.set", "plugin_id": plugin_id, "caller": self._caller}
        if key is not None:
            req["key"] = key
            req["value"] = value
            if is_secret is not None:
                req["is_secret"] = is_secret
        if values is not None:
            req["values"] = dict(values)
        if secrets is not None:
            req["secrets"] = dict(secrets)
        if replace:
            req["replace"] = True
        return await self.request(req)

    async def domain_snapshot(self, *, domain: str | None = None, plugin_id: str | None = None) -> dict[str, Any]:
        req: dict[str, Any] = {"id": uuid.uuid4().hex, "op": "domain.snapshot", "caller": self._caller}
        if domain is not None:
            req["domain"] = domain
        if plugin_id is not None:
            req["plugin_id"] = plugin_id
        return await self.request(req)


__all__ = ["ControlClient", "ControlRpcError", "ControlServer", "DEFAULT_MAX_PAYLOAD_BYTES"]
