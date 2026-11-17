"""Persistent local conversation sessions for `ai.agent.v1`."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_SCHEMA_VERSION = 1
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MAX_TITLE_CHARS = 120
_MAX_MESSAGE_CHARS = 200_000
_MAX_MESSAGES = 2_000
_MAX_LIST_LIMIT = 200


class AgentSessionError(ValueError):
    """Session validation or storage failure surfaced through the capability envelope."""

    def __init__(self, message: str, *, code: str = "invalid_argument") -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def _clean_title(value: Any, fallback: str = "") -> str:
    title = _clean_text(value, max_chars=_MAX_TITLE_CHARS)
    if not title:
        title = _clean_text(fallback.replace("\n", " "), max_chars=_MAX_TITLE_CHARS)
    return title or "未命名会话"


def _new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"agent-{stamp}-{uuid.uuid4().hex[:8]}"


def _new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex}"


def _validate_session_id(session_id: Any) -> str:
    cleaned = str(session_id or "").strip()
    if not cleaned or not _SESSION_ID_RE.match(cleaned):
        raise AgentSessionError("无效的会话 ID", code="invalid_session_id")
    return cleaned


def _message_summary(message: Mapping[str, Any]) -> str:
    content = _clean_text(message.get("content"), max_chars=160).replace("\n", " ")
    return content


def _session_summary(session: Mapping[str, Any]) -> dict[str, Any]:
    messages = session.get("messages")
    message_list = messages if isinstance(messages, list) else []
    last_message = message_list[-1] if message_list and isinstance(message_list[-1], Mapping) else {}
    return {
        "id": str(session.get("id") or ""),
        "session_id": str(session.get("id") or ""),
        "title": str(session.get("title") or "未命名会话"),
        "domain": str(session.get("domain") or "core"),
        "context_title": str(session.get("context_title") or ""),
        "message_count": len(message_list),
        "created_at": str(session.get("created_at") or ""),
        "updated_at": str(session.get("updated_at") or ""),
        "model": str(session.get("model") or ""),
        "served_model": str(session.get("served_model") or ""),
        "route_group": str(session.get("route_group") or ""),
        "last_message": _message_summary(last_message),
        "last_role": str(last_message.get("role") or ""),
    }


class AgentSessionStore:
    """0600 JSON session storage scoped to the Magpie agent plugin data directory."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir).expanduser() / "agent-sessions"
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root.is_symlink() or not self._root.is_dir():
            raise AgentSessionError("会话目录不安全", code="session_storage_error")
        try:
            os.chmod(self._root, 0o700)
        except OSError:
            pass

    def _path(self, session_id: Any) -> Path:
        clean_id = _validate_session_id(session_id)
        self._ensure_root()
        target = self._root / f"{clean_id}.json"
        if target.is_symlink():
            raise AgentSessionError("拒绝读取符号链接会话文件", code="session_storage_error")
        return target

    def _write(self, session: Mapping[str, Any]) -> None:
        session_id = _validate_session_id(session.get("id"))
        target = self._path(session_id)
        serialized = json.dumps(dict(session), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp_path: Path | None = None
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._root,
            prefix=f".{session_id}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            tmp_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(tmp_path, target)
            os.chmod(target, 0o600)
        except BaseException:
            try:
                if not handle.closed:
                    handle.close()
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
            raise

    def _read(self, session_id: Any) -> dict[str, Any]:
        target = self._path(session_id)
        if not target.exists():
            raise AgentSessionError("会话不存在", code="session_not_found")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                payload = json.loads(handle.read())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentSessionError("会话文件损坏", code="session_corrupt") from exc
        if not isinstance(payload, Mapping):
            raise AgentSessionError("会话文件结构无效", code="session_corrupt")
        if payload.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise AgentSessionError("不支持的会话版本", code="session_corrupt")
        if str(payload.get("id") or "") != str(session_id):
            raise AgentSessionError("会话 ID 不匹配", code="session_corrupt")
        if not isinstance(payload.get("messages"), list):
            raise AgentSessionError("会话消息结构无效", code="session_corrupt")
        return dict(payload)

    def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise AgentSessionError("limit 必须为正整数")
        self._ensure_root()
        summaries: list[dict[str, Any]] = []
        with self._lock:
            for entry in self._root.glob("*.json"):
                if entry.is_symlink() or not entry.is_file():
                    continue
                try:
                    session = self._read(entry.stem)
                except AgentSessionError:
                    continue
                summaries.append(_session_summary(session))
        summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return summaries[: min(limit, _MAX_LIST_LIMIT)]

    def get_session(self, session_id: Any) -> dict[str, Any]:
        with self._lock:
            return self._read(session_id)

    @staticmethod
    def summary(session: Mapping[str, Any]) -> dict[str, Any]:
        return _session_summary(session)

    def begin_turn(
        self,
        session_id: Any,
        *,
        prompt: str,
        action: str,
        domain: str,
        context_title: str = "",
        session_title: str = "",
        context_refs: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        clean_prompt = _clean_text(prompt, max_chars=_MAX_MESSAGE_CHARS)
        if not clean_prompt:
            raise AgentSessionError("会话消息不能为空", code="invalid_argument")
        now = _utc_now()
        created = not bool(str(session_id or "").strip())
        with self._lock:
            if created:
                sid = _new_session_id()
                session = {
                    "schema_version": SESSION_SCHEMA_VERSION,
                    "id": sid,
                    "title": _clean_title(session_title, clean_prompt),
                    "domain": domain,
                    "context_title": context_title,
                    "context_refs": self._safe_context_refs(context_refs),
                    "created_at": now,
                    "updated_at": now,
                    "model": "",
                    "served_model": "",
                    "route_group": "",
                    "messages": [],
                }
            else:
                session = self._read(session_id)
            messages = session.get("messages")
            if not isinstance(messages, list):
                raise AgentSessionError("会话消息结构无效", code="session_corrupt")
            if len(messages) >= _MAX_MESSAGES:
                raise AgentSessionError("会话消息数量已达上限", code="session_full")
            user_message = {
                "id": _new_message_id(),
                "role": "user",
                "content": clean_prompt,
                "created_at": now,
                "action": action,
                "domain": domain,
            }
            messages.append(user_message)
            session["messages"] = messages
            session["domain"] = domain
            if context_title:
                session["context_title"] = context_title
            if session_title:
                session["title"] = _clean_title(session_title, clean_prompt)
            elif created or not str(session.get("title") or "").strip():
                session["title"] = _clean_title(session.get("title"), clean_prompt)
            refs = self._safe_context_refs(context_refs)
            if refs:
                session["context_refs"] = refs
            session["updated_at"] = now
            self._write(session)
            return session, user_message, created

    def finish_turn(
        self,
        session_id: Any,
        *,
        content: str,
        action: str,
        domain: str,
        served_model: str = "",
        route_group: str = "",
        usage: Any = None,
        is_error: bool = False,
        error_code: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        clean_content = _clean_text(content, max_chars=_MAX_MESSAGE_CHARS)
        if not clean_content:
            clean_content = "后端未返回回复正文"
        now = _utc_now()
        with self._lock:
            session = self._read(session_id)
            messages = session.get("messages")
            if not isinstance(messages, list):
                raise AgentSessionError("会话消息结构无效", code="session_corrupt")
            if len(messages) >= _MAX_MESSAGES:
                raise AgentSessionError("会话消息数量已达上限", code="session_full")
            assistant_message: dict[str, Any] = {
                "id": _new_message_id(),
                "role": "assistant",
                "content": clean_content,
                "created_at": now,
                "action": action,
                "domain": domain,
                "served_model": served_model,
                "route_group": route_group,
                "is_error": is_error,
            }
            if isinstance(usage, Mapping):
                assistant_message["usage"] = {
                    key: value for key, value in usage.items() if isinstance(value, (int, float))
                }
            if error_code:
                assistant_message["error_code"] = error_code
            messages.append(assistant_message)
            session["messages"] = messages
            session["updated_at"] = now
            if served_model:
                session["served_model"] = served_model
                session["model"] = served_model
            if route_group:
                session["route_group"] = route_group
            self._write(session)
            return session, assistant_message

    def rename_session(self, session_id: Any, title: str) -> dict[str, Any]:
        clean_title = _clean_title(title)
        with self._lock:
            session = self._read(session_id)
            session["title"] = clean_title
            session["updated_at"] = _utc_now()
            self._write(session)
            return session

    def delete_session(self, session_id: Any) -> bool:
        target = self._path(session_id)
        with self._lock:
            if not target.exists():
                raise AgentSessionError("会话不存在", code="session_not_found")
            target.unlink()
            return True

    def export_session(self, session_id: Any, *, fmt: str = "markdown") -> dict[str, Any]:
        clean_format = str(fmt or "markdown").strip().lower()
        with self._lock:
            session = self._read(session_id)
        if clean_format in {"md", "markdown"}:
            content = self._to_markdown(session)
            return {
                "session_id": str(session["id"]),
                "format": "markdown",
                "mime_type": "text/markdown; charset=utf-8",
                "filename": f"{session['id']}.md",
                "content": content,
            }
        if clean_format == "json":
            content = json.dumps(session, ensure_ascii=False, indent=2) + "\n"
            return {
                "session_id": str(session["id"]),
                "format": "json",
                "mime_type": "application/json; charset=utf-8",
                "filename": f"{session['id']}.json",
                "content": content,
            }
        raise AgentSessionError("导出格式仅支持 markdown 或 json", code="invalid_argument")

    @staticmethod
    def _safe_context_refs(raw: Any) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            return []
        refs: list[dict[str, str]] = []
        for item in raw[:12]:
            if not isinstance(item, Mapping):
                continue
            refs.append({
                "capability": _clean_text(item.get("capability"), max_chars=120),
                "label": _clean_text(item.get("label"), max_chars=160),
            })
        return [item for item in refs if item["capability"] or item["label"]]

    @staticmethod
    def _to_markdown(session: Mapping[str, Any]) -> str:
        lines = [
            f"# {session.get('title') or '未命名会话'}",
            "",
            f"- 会话 ID：{session.get('id') or ''}",
            f"- 创建时间：{session.get('created_at') or ''}",
            f"- 更新时间：{session.get('updated_at') or ''}",
            f"- 领域：{session.get('domain') or ''}",
            f"- 上下文：{session.get('context_title') or ''}",
            f"- 模型：{session.get('served_model') or session.get('model') or ''}",
            f"- 路由：{session.get('route_group') or ''}",
            "",
        ]
        messages = session.get("messages")
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "")
            heading = "用户" if role == "user" else "助手" if role == "assistant" else role or "消息"
            lines.extend([f"## {heading}", "", str(message.get("content") or ""), ""])
            details: list[str] = []
            if message.get("created_at"):
                details.append(f"时间：{message.get('created_at')}")
            if message.get("served_model"):
                details.append(f"模型：{message.get('served_model')}")
            if message.get("route_group"):
                details.append(f"路由：{message.get('route_group')}")
            if message.get("is_error"):
                details.append("状态：错误")
            if details:
                lines.extend(["> " + " · ".join(details), ""])
        return "\n".join(lines).rstrip() + "\n"


__all__ = ["AgentSessionError", "AgentSessionStore", "SESSION_SCHEMA_VERSION"]
