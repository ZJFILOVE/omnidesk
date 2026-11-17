"""Subreddit subscription store, normalization, persistence, and channel builder."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import TextChannel

if __package__:
    from .reddit_security import RedditPluginError, atomic_write_json, validate_safe_file_path
else:
    from reddit_security import RedditPluginError, atomic_write_json, validate_safe_file_path

_VALID_SORTS = frozenset({"hot", "new", "top", "rising", "controversial"})
_SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,50}$")


@dataclass(slots=True)
class SubredditSubscription:
    """Single subscribed/pinned Reddit community entry."""

    name: str
    display_name: str = ""
    default_sort: str = "hot"
    subscribers: int = 0
    pinned: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or f"r/{self.name}",
            "default_sort": self.default_sort,
            "subscribers": self.subscribers,
            "pinned": self.pinned,
        }


def normalize_subreddit_name(raw: str) -> str:
    """Normalize r/name, URL, or prefix variants into canonical subreddit name or ''."""
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(("https://", "http://")):
        idx = low.find("/r/")
        if idx != -1:
            s = s[idx + 3 :]
    s = s.lstrip("/")
    low = s.lower()
    if low.startswith("r/"):
        s = s[2:]
    elif low.startswith("sub:"):
        s = s[4:]
    elif low.startswith("subreddit:"):
        s = s[10:]
    for sep in ("/", "?", "#", ":"):
        pos = s.find(sep)
        if pos != -1:
            s = s[:pos]
    s = s.strip()
    return s if _SUBREDDIT_NAME_RE.match(s) else ""


def is_valid_subreddit_name(name: str) -> bool:
    """Return True if normalized name satisfies Reddit subreddit identifier bounds."""
    return bool(_SUBREDDIT_NAME_RE.match(name or ""))


def normalize_sort(sort: str | None) -> str:
    """Normalize feed sort parameter to supported Reddit listing sorts."""
    low = str(sort or "").strip().lower()
    return low if low in _VALID_SORTS else "hot"


class SubscriptionStore:
    """Thread-safe persistent subreddit subscription & pinned-channel store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._file_path: Path | None = None
        self._loaded = False
        self._last_config_spec = ""
        self._subscriptions: list[SubredditSubscription] = []

    def set_data_dir(self, data_dir: Path | str | None) -> None:
        """Bind `subscriptions.json` inside plugin `data_dir` and reload from disk if changed."""
        with self._lock:
            if not data_dir:
                self._file_path = None
                return
            base = Path(str(data_dir)).expanduser()
            next_path = validate_safe_file_path(str(base / "subscriptions.json"))
            if next_path != self._file_path:
                self._file_path = next_path
                self._loaded = False
                self._load_from_disk_locked()

    def file_path(self) -> str:
        with self._lock:
            return str(self._file_path) if self._file_path is not None else ""

    def _load_from_disk_locked(self) -> None:
        self._loaded = True
        if self._file_path is None or not self._file_path.is_file():
            return
        if self._file_path.is_symlink():
            raise RedditPluginError("path_safety", f"拒绝读取符号链接订阅文件: {self._file_path}")
        try:
            doc = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(doc, dict):
            return
        if isinstance(doc.get("last_config_spec"), str):
            self._last_config_spec = doc["last_config_spec"]
        arr = doc.get("subreddits") if isinstance(doc.get("subreddits"), list) else doc.get("subscriptions")
        if not isinstance(arr, list):
            return
        self._subscriptions.clear()
        for item in arr:
            if isinstance(item, str):
                norm = normalize_subreddit_name(item)
                if norm:
                    self._upsert_locked(SubredditSubscription(name=norm, display_name=f"r/{norm}"), mark_pinned=True)
            elif isinstance(item, dict):
                norm = normalize_subreddit_name(str(item.get("name") or ""))
                if not norm:
                    continue
                disp = str(item.get("display_name") or "").strip() or f"r/{norm}"
                sort = normalize_sort(item.get("default_sort"))
                subs = int(item.get("subscribers") or 0) if isinstance(item.get("subscribers"), (int, float)) else 0
                pinned = bool(item.get("pinned", True))
                self._upsert_locked(
                    SubredditSubscription(
                        name=norm, display_name=disp, default_sort=sort, subscribers=max(0, subs), pinned=pinned
                    ),
                    mark_pinned=pinned,
                )

    def _save_to_disk_locked(self) -> bool:
        if self._file_path is None:
            return False
        doc = {
            "schema_version": 1,
            "last_config_spec": self._last_config_spec,
            "subreddits": [s.to_dict() for s in self._subscriptions],
        }
        try:
            atomic_write_json(self._file_path, doc, file_mode=0o600)
            return True
        except OSError:
            return False

    def _upsert_locked(self, sub: SubredditSubscription, *, mark_pinned: bool) -> None:
        if not sub.name:
            return
        low = sub.name.lower()
        for existing in self._subscriptions:
            if existing.name.lower() == low:
                if sub.display_name and sub.display_name != f"r/{existing.name}":
                    existing.display_name = sub.display_name
                if sub.default_sort:
                    existing.default_sort = normalize_sort(sub.default_sort)
                if sub.subscribers > 0:
                    existing.subscribers = sub.subscribers
                if mark_pinned:
                    existing.pinned = True
                return
        self._subscriptions.append(
            SubredditSubscription(
                name=sub.name,
                display_name=sub.display_name or f"r/{sub.name}",
                default_sort=normalize_sort(sub.default_sort),
                subscribers=max(0, int(sub.subscribers)),
                pinned=mark_pinned,
            )
        )

    def apply_config_defaults(self, subreddits_csv_or_json: str) -> None:
        """Apply default pinned subreddits from config CSV or JSON array unless unchanged."""
        with self._lock:
            if not self._loaded:
                self._load_from_disk_locked()
            spec = str(subreddits_csv_or_json or "").strip()
            if not spec:
                return
            if spec == self._last_config_spec and self._subscriptions:
                return
            self._last_config_spec = spec

            changed = False
            if spec.startswith("["):
                try:
                    parsed = json.loads(spec)
                except json.JSONDecodeError:
                    parsed = []
                if isinstance(parsed, list):
                    for el in parsed:
                        raw_name = ""
                        disp = ""
                        sort = "hot"
                        if isinstance(el, str):
                            raw_name = el
                        elif isinstance(el, dict):
                            raw_name = str(el.get("name") or "")
                            disp = str(el.get("display_name") or "").strip()
                            sort = str(el.get("default_sort") or "hot")
                        norm = normalize_subreddit_name(raw_name)
                        if norm:
                            self._upsert_locked(
                                SubredditSubscription(
                                    name=norm,
                                    display_name=disp or f"r/{norm}",
                                    default_sort=normalize_sort(sort),
                                ),
                                mark_pinned=True,
                            )
                            changed = True
            else:
                for token in spec.split(","):
                    norm = normalize_subreddit_name(token)
                    if not norm:
                        continue
                    self._upsert_locked(
                        SubredditSubscription(name=norm, display_name=f"r/{norm}"),
                        mark_pinned=True,
                    )
                    changed = True

            if changed:
                self._save_to_disk_locked()

    def subscribe(self, subreddit_name: str, display_name: str = "", default_sort: str = "hot") -> bool:
        """Pin/subscribe a subreddit and persist to disk."""
        with self._lock:
            if not self._loaded:
                self._load_from_disk_locked()
            norm = normalize_subreddit_name(subreddit_name)
            if not norm:
                return False
            disp = str(display_name or "").strip() or f"r/{norm}"
            self._upsert_locked(
                SubredditSubscription(
                    name=norm,
                    display_name=disp,
                    default_sort=normalize_sort(default_sort),
                    pinned=True,
                ),
                mark_pinned=True,
            )
            self._save_to_disk_locked()
            return True

    def unsubscribe(self, subreddit_name: str) -> bool:
        """Remove a subreddit from subscriptions and persist to disk."""
        with self._lock:
            if not self._loaded:
                self._load_from_disk_locked()
            norm = normalize_subreddit_name(subreddit_name)
            if not norm:
                return False
            before = len(self._subscriptions)
            low = norm.lower()
            self._subscriptions = [s for s in self._subscriptions if s.name.lower() != low]
            if len(self._subscriptions) != before:
                self._save_to_disk_locked()
                return True
            return False

    def merge_remote_subscriptions(self, remote_subs: Sequence[SubredditSubscription]) -> int:
        """Merge remote authenticated user subreddits into local store."""
        with self._lock:
            if not self._loaded:
                self._load_from_disk_locked()
            added = 0
            for r in remote_subs:
                norm = normalize_subreddit_name(r.name)
                if not norm:
                    continue
                self._upsert_locked(
                    SubredditSubscription(
                        name=norm,
                        display_name=r.display_name or f"r/{norm}",
                        default_sort=r.default_sort,
                        subscribers=r.subscribers,
                        pinned=r.pinned,
                    ),
                    mark_pinned=r.pinned,
                )
                added += 1
            if added > 0:
                self._save_to_disk_locked()
            return added

    def list_subscriptions(self) -> list[SubredditSubscription]:
        with self._lock:
            return [
                SubredditSubscription(
                    name=s.name,
                    display_name=s.display_name,
                    default_sort=s.default_sort,
                    subscribers=s.subscribers,
                    pinned=s.pinned,
                )
                for s in self._subscriptions
            ]

    def is_subscribed(self, subreddit_name: str) -> bool:
        norm = normalize_subreddit_name(subreddit_name)
        if not norm:
            return False
        low = norm.lower()
        with self._lock:
            return any(s.name.lower() == low for s in self._subscriptions)

    def build_channels(self, is_authenticated: bool) -> list[TextChannel]:
        """Build dynamic TextChannel list: home, popular, and pinned/subscribed subreddits."""
        with self._lock:
            out: list[TextChannel] = [
                TextChannel(
                    id="home",
                    name="Home · 订阅流",
                    badge="个人" if is_authenticated else "需登录",
                    requires_auth=True,
                ),
                TextChannel(
                    id="popular",
                    name="r/popular · 全站热门",
                    badge="热门",
                    requires_auth=False,
                ),
            ]
            for s in self._subscriptions:
                if not s.name or s.name.lower() in {"home", "popular"}:
                    continue
                out.append(
                    TextChannel(
                        id=f"r/{s.name}",
                        name=s.display_name or f"r/{s.name}",
                        badge="Subreddit" if s.pinned else "同步",
                        requires_auth=False,
                    )
                )
            return out
