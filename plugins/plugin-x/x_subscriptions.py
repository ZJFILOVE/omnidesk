"""X List subscription parsing and persisted `subscriptions.json` storage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import TextChannel

if __package__:
    from .x_parser import XListInfo
    from .x_security import XPluginError, atomic_write_json_0600, safe_data_dir, validate_list_id
else:
    from x_parser import XListInfo  # type: ignore[no-redef]
    from x_security import XPluginError, atomic_write_json_0600, safe_data_dir, validate_list_id  # type: ignore[no-redef]

_LIST_URL_RE = re.compile(r"(?:x|twitter)\.com/i/lists/([A-Za-z0-9][A-Za-z0-9_-]{0,63})", re.IGNORECASE)


@dataclass(slots=True)
class PinnedList:
    id: str
    name: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class XSubscriptionStore:
    """Persist pinned X Lists and expose them as text channels."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._path: Path | None = None
        self._loaded = False
        self._last_default_spec = ""
        self._lists: list[PinnedList] = []
        self.set_data_dir(data_dir)

    def set_data_dir(self, data_dir: Path | None) -> None:
        if data_dir is None:
            self._path = None
            return
        path = Path(data_dir) / "subscriptions.json"
        if path != self._path:
            self._path = path
            self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or self._path.is_symlink() or not self._path.is_file():
            return
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        raw_items = doc.get("lists") if isinstance(doc, Mapping) else None
        if not isinstance(raw_items, list):
            return
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            try:
                list_id = validate_list_id(item.get("id"))
            except XPluginError:
                continue
            if list_id in seen:
                continue
            seen.add(list_id)
            self._lists.append(
                PinnedList(id=list_id, name=str(item.get("name") or f"List #{list_id}").strip())
            )

    def _save(self) -> None:
        if self._path is None:
            return
        atomic_write_json_0600(
            self._path,
            {
                "schema_version": 1,
                "lists": [item.to_dict() for item in self._lists],
            },
        )

    def _parse_token(self, raw: str) -> PinnedList | None:
        token = raw.strip()
        if not token:
            return None
        if match := _LIST_URL_RE.search(token):
            list_id = match.group(1)
            suffix = token[match.end() :].strip()
            name = suffix[1:].strip() if suffix.startswith(":") else ""
            return PinnedList(id=validate_list_id(list_id), name=name or f"List #{list_id}")
        if token.startswith("list:"):
            token = token[5:]
        left, sep, right = token.partition(":")
        list_id = validate_list_id(left)
        return PinnedList(id=list_id, name=right.strip() if sep and right.strip() else f"List #{list_id}")

    def apply_default_spec(self, spec: str) -> None:
        clean = str(spec or "").strip()
        if not clean or clean == self._last_default_spec:
            return
        self._last_default_spec = clean
        self._ensure_loaded()
        changed = False
        raw_items: list[Any]
        if clean.startswith("["):
            try:
                parsed = json.loads(clean)
            except json.JSONDecodeError:
                parsed = []
            raw_items = parsed if isinstance(parsed, list) else []
        else:
            raw_items = clean.split(",")
        for raw in raw_items:
            if isinstance(raw, Mapping):
                try:
                    item = PinnedList(
                        id=validate_list_id(raw.get("id")),
                        name=str(raw.get("name") or "").strip(),
                    )
                except XPluginError:
                    continue
                if not item.name:
                    item.name = f"List #{item.id}"
            else:
                try:
                    item = self._parse_token(str(raw))
                except XPluginError:
                    item = None
            if item is not None and self._merge(item):
                changed = True
        if changed:
            self._save()

    def _merge(self, item: PinnedList) -> bool:
        for existing in self._lists:
            if existing.id == item.id:
                if item.name and existing.name != item.name:
                    existing.name = item.name
                    return True
                return False
        self._lists.append(item)
        return True

    def pin(self, list_id: str, name: str = "") -> bool:
        self._ensure_loaded()
        item = PinnedList(
            id=validate_list_id(list_id),
            name=str(name or "").strip() or f"List #{validate_list_id(list_id)}",
        )
        changed = self._merge(item)
        if changed:
            self._save()
        return changed

    def remove(self, list_id: str) -> bool:
        self._ensure_loaded()
        target = validate_list_id(list_id)
        before = len(self._lists)
        self._lists = [item for item in self._lists if item.id != target]
        if len(self._lists) == before:
            return False
        self._save()
        return True

    def list_pinned(self) -> list[PinnedList]:
        self._ensure_loaded()
        return list(self._lists)

    def merge_remote(self, lists: list[XListInfo]) -> list[XListInfo]:
        self._ensure_loaded()
        known = {item.id for item in self._lists}
        return [item for item in lists if item.id in known or item.pinned]

    def build_channels(self) -> list[TextChannel]:
        self._ensure_loaded()
        channels = [
            TextChannel(id="following", name="关注", badge="", requires_auth=True),
            TextChannel(id="for_you", name="推荐", badge="", requires_auth=True),
            TextChannel(id="bookmarks", name="书签", badge="", requires_auth=True),
            TextChannel(id="lists", name="列表", badge="", requires_auth=True),
        ]
        for item in self._lists:
            channels.append(
                TextChannel(
                    id=f"list:{item.id}",
                    name=item.name or f"List #{item.id}",
                    badge="列表",
                    requires_auth=True,
                )
            )
        return channels
