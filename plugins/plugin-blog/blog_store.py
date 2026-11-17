"""Persisted blog subscriptions stored as 0600 `subscriptions.json`."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class BlogStoreError(RuntimeError):
    """Subscription storage / validation error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def normalize_blog_url(value: str, *, allow_http: bool = False) -> str:
    """Validate and normalize a public blog URL."""
    raw = (value or "").strip()
    if not raw:
        raise BlogStoreError("invalid_argument", "博客地址不能为空")
    if not _SCHEME_RE.match(raw):
        raw = f"https://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ({"http", "https"} if allow_http else {"https"}):
        raise BlogStoreError("invalid_argument", "博客地址必须使用 HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise BlogStoreError("invalid_argument", "博客地址缺少有效域名")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise BlogStoreError("invalid_argument", "不允许订阅本地或内网地址")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((scheme, parsed.netloc.lower(), path, parsed.query, ""))


def blog_id_for(url: str) -> str:
    return f"blog-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


@dataclass(slots=True)
class BlogSubscription:
    """One persisted blog subscription."""

    id: str
    url: str
    title: str = ""
    feed_url: str = ""
    site_url: str = ""
    added_at: str = ""
    last_checked: str = ""
    last_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BlogSubscription | None:
        url = str(data.get("url") or "").strip()
        if not url:
            return None
        try:
            normalized = normalize_blog_url(url)
        except BlogStoreError:
            return None
        title = str(data.get("title") or "").strip() or urllib.parse.urlsplit(normalized).hostname or normalized
        feed_url = str(data.get("feed_url") or "").strip()
        if feed_url:
            try:
                feed_url = normalize_blog_url(feed_url)
            except BlogStoreError:
                feed_url = ""
        return cls(
            id=str(data.get("id") or blog_id_for(normalized)),
            url=normalized,
            title=title,
            feed_url=feed_url,
            site_url=str(data.get("site_url") or "").strip(),
            added_at=str(data.get("added_at") or "").strip(),
            last_checked=str(data.get("last_checked") or "").strip(),
            last_error=str(data.get("last_error") or "").strip(),
            extra=dict(data.get("extra") or {}) if isinstance(data.get("extra"), Mapping) else {},
        )


class BlogSubscriptionStore:
    """Thread-free async-plugin-friendly store with atomic writes."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._path: Path | None = None
        self._loaded = False
        self._items: list[BlogSubscription] = []
        self._last_default_spec = ""
        self.set_data_dir(data_dir)

    def set_data_dir(self, data_dir: Path | None) -> None:
        if data_dir is None:
            self._path = None
            return
        path = Path(data_dir) / "subscriptions.json"
        if path != self._path:
            self._path = path
            self._loaded = False
            self._items = []

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._path
        if path is None or path.is_symlink() or not path.is_file():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        raw = doc.get("subscriptions") if isinstance(doc, Mapping) else None
        if not isinstance(raw, list):
            return
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            sub = BlogSubscription.from_dict(item)
            if sub is None or sub.id in seen:
                continue
            seen.add(sub.id)
            self._items.append(sub)

    def _save(self) -> None:
        path = self._path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "subscriptions": [item.to_dict() for item in self._items],
        }
        fd, temp_name = tempfile.mkstemp(prefix=".subscriptions.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def list(self) -> list[BlogSubscription]:
        self._ensure_loaded()
        return list(self._items)

    def get(self, identifier: str) -> BlogSubscription | None:
        self._ensure_loaded()
        target = (identifier or "").strip()
        if not target:
            return None
        for item in self._items:
            if item.id == target:
                return item
        try:
            normalized = normalize_blog_url(target)
        except BlogStoreError:
            normalized = ""
        if normalized:
            for item in self._items:
                if item.url == normalized:
                    return item
        return None

    def add(
        self,
        url: str,
        *,
        title: str = "",
        feed_url: str = "",
        site_url: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> BlogSubscription:
        self._ensure_loaded()
        normalized = normalize_blog_url(url)
        existing = self.get(normalized)
        now = datetime.now(timezone.utc).isoformat()
        if existing is not None:
            existing.title = str(title or existing.title).strip()
            existing.feed_url = str(feed_url or existing.feed_url).strip()
            existing.site_url = str(site_url or existing.site_url).strip()
            if extra:
                existing.extra.update(dict(extra))
            self._save()
            return existing
        host = urllib.parse.urlsplit(normalized).hostname or normalized
        item = BlogSubscription(
            id=blog_id_for(normalized),
            url=normalized,
            title=str(title).strip() or host,
            feed_url=str(feed_url).strip(),
            site_url=str(site_url).strip() or normalized,
            added_at=now,
            extra=dict(extra or {}),
        )
        self._items.append(item)
        self._save()
        return item

    def update(self, sub: BlogSubscription) -> BlogSubscription:
        self._ensure_loaded()
        for index, item in enumerate(self._items):
            if item.id == sub.id:
                self._items[index] = sub
                self._save()
                return sub
        self._items.append(sub)
        self._save()
        return sub

    def remove(self, identifier: str) -> bool:
        self._ensure_loaded()
        sub = self.get(identifier)
        if sub is None:
            return False
        self._items = [item for item in self._items if item.id != sub.id]
        self._save()
        return True

    def apply_default_spec(self, spec: str) -> None:
        clean = (spec or "").strip()
        if not clean or clean == self._last_default_spec:
            return
        self._last_default_spec = clean
        tokens = [part.strip() for part in re.split(r"[\n,]+", clean) if part.strip()]
        for token in tokens:
            if token.startswith(("http://", "https://")):
                url, title = token, ""
                for separator in ("|", "：", " : "):
                    if separator in token:
                        url, _, title = token.partition(separator)
                        break
            else:
                url, _, title = token.partition("|")
            if not url:
                continue
            try:
                self.add(url, title=title.strip())
            except BlogStoreError:
                continue
