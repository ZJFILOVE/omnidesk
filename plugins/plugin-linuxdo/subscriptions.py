"""Pinned category subscriptions (subscriptions.json), category discovery cache, and channel builder."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import TextChannel

if __package__:
    from .html_parser import decode_html_entities
    from .security import json_int, json_str, validate_safe_file_path
else:
    from html_parser import decode_html_entities  # type: ignore[no-redef]
    from security import json_int, json_str, validate_safe_file_path  # type: ignore[no-redef]

_CATEGORY_TTL_SEC = 600.0
_FAILURE_COOLDOWN_SEC = 120.0
_MAX_DYNAMIC_CATEGORIES = 6


@dataclass
class DiscourseCategory:
    """Discourse board / category metadata."""

    id: int = 0
    slug: str = ""
    name: str = ""
    description: str = ""
    topic_count: int = 0
    topics_week: int = 0
    parent_id: int = 0
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "topic_count": self.topic_count,
            "topics_week": self.topics_week,
            "parent_id": self.parent_id,
            "pinned": self.pinned,
        }


def _default_linuxdo_boards() -> list[DiscourseCategory]:
    return [
        DiscourseCategory(id=4, slug="develop", name="开发调优"),
        DiscourseCategory(id=14, slug="resource", name="资源荟萃"),
        DiscourseCategory(id=42, slug="wiki", name="文档共建"),
        DiscourseCategory(id=34, slug="news", name="前沿快讯"),
    ]


def _category_channel_id(cat: DiscourseCategory) -> str:
    return f"cat:{cat.id}" if cat.id > 0 else f"cat:{cat.slug}"


class SubscriptionStore:
    """Thread-safe pinned category store persisted atomically to `subscriptions.json`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data_dir: Path | None = None
        self._file_path: Path | None = None
        self._loaded = False
        self._last_config_spec = ""
        self._pinned: list[DiscourseCategory] = []
        self._discovered: list[DiscourseCategory] = []
        self._discovered_at = 0.0
        self._last_refresh_failed_at = 0.0

    def set_data_dir(self, data_dir: Path | str | None) -> None:
        with self._lock:
            if not data_dir:
                self._data_dir = None
                self._file_path = None
                return
            root = Path(data_dir).expanduser()
            next_path = validate_safe_file_path("subscriptions.json", base_dir=root)
            if next_path != self._file_path:
                self._data_dir = root
                self._file_path = next_path
                self._loaded = False
                self._load_locked()

    def _load_locked(self) -> None:
        self._loaded = True
        if self._file_path is None or self._file_path.is_symlink() or not self._file_path.is_file():
            return
        try:
            doc = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(doc, Mapping) or not isinstance(doc.get("pinned_categories"), list):
            return
        self._pinned.clear()
        for item in doc["pinned_categories"]:
            if not isinstance(item, Mapping):
                continue
            cid = json_int(item, "id", 0)
            slug = json_str(item, "slug", "").strip()
            name = json_str(item, "name", "").strip()
            if cid > 0 or slug:
                self._pinned.append(
                    DiscourseCategory(
                        id=cid,
                        slug=slug,
                        name=name or slug or f"板块 #{cid}",
                        pinned=True,
                    )
                )

    def _save_locked(self) -> bool:
        if self._file_path is None:
            return False
        self._file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        doc = {
            "schema_version": 1,
            "pinned_categories": [
                {"id": c.id, "slug": c.slug, "name": c.name} for c in self._pinned
            ],
        }
        serialized = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        tmp_path: Path | None = None
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._file_path.parent,
            prefix=".subscriptions.",
            suffix=".tmp",
            delete=False,
        )
        try:
            tmp_path = Path(tmp.name)
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(serialized)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp_path, self._file_path)
            os.chmod(self._file_path, 0o600)
            return True
        except BaseException:
            if not tmp.closed:
                tmp.close()
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

    def _merge_pinned_locked(self, cat: DiscourseCategory) -> None:
        for existing in self._pinned:
            if (cat.id > 0 and existing.id == cat.id) or (cat.slug and existing.slug == cat.slug):
                if existing.id <= 0 < cat.id:
                    existing.id = cat.id
                if not existing.slug and cat.slug:
                    existing.slug = cat.slug
                if cat.name:
                    existing.name = cat.name
                existing.pinned = True
                return
        self._pinned.append(
            DiscourseCategory(
                id=cat.id,
                slug=cat.slug,
                name=cat.name or cat.slug or f"板块 #{cat.id}",
                pinned=True,
            )
        )

    def apply_config_pinned(self, pinned_csv_or_json: str) -> None:
        """Merge configured `pinned_categories` CSV or JSON array on change."""
        with self._lock:
            if not self._loaded:
                self._load_locked()
            spec = (pinned_csv_or_json or "").strip()
            if not spec or spec == self._last_config_spec:
                return
            self._last_config_spec = spec

            changed = False
            if spec.startswith("["):
                try:
                    parsed = json.loads(spec)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    for item in parsed:
                        cat = DiscourseCategory()
                        if isinstance(item, Mapping):
                            cat.id = json_int(item, "id", 0)
                            cat.slug = json_str(item, "slug", "").strip()
                            cat.name = json_str(item, "name", "").strip()
                        elif isinstance(item, int) and not isinstance(item, bool):
                            cat.id = item
                        elif isinstance(item, str):
                            s = item.strip()
                            if s.isdigit():
                                cat.id = int(s)
                            else:
                                cat.slug = s
                        if cat.id > 0 or cat.slug:
                            self._merge_pinned_locked(cat)
                            changed = True
                    if changed:
                        self._save_locked()
                    return

            for token in spec.split(","):
                tok = token.strip()
                if not tok:
                    continue
                cat = DiscourseCategory()
                if ":" in tok:
                    left, right = (p.strip() for p in tok.split(":", 1))
                    if left.isdigit():
                        cat.id = int(left)
                    else:
                        cat.slug = left
                    cat.name = right
                elif tok.isdigit():
                    cat.id = int(tok)
                else:
                    cat.slug = tok
                if cat.id > 0 or cat.slug:
                    self._merge_pinned_locked(cat)
                    changed = True
            if changed:
                self._save_locked()

    def pin_category(self, cat_id: int = 0, slug: str = "", name: str = "") -> bool:
        with self._lock:
            if not self._loaded:
                self._load_locked()
            clean_slug = (slug or "").strip()
            clean_name = decode_html_entities((name or "").strip())
            if cat_id <= 0 and not clean_slug:
                return False
            cat = DiscourseCategory(id=cat_id, slug=clean_slug, name=clean_name, pinned=True)
            for d in self._discovered:
                if (cat.id > 0 and d.id == cat.id) or (cat.slug and d.slug == cat.slug):
                    if cat.id <= 0:
                        cat.id = d.id
                    if not cat.slug:
                        cat.slug = d.slug
                    if not cat.name:
                        cat.name = d.name
                    break
            self._merge_pinned_locked(cat)
            self._save_locked()
            return True

    def unpin_category(self, id_or_slug: str) -> bool:
        with self._lock:
            if not self._loaded:
                self._load_locked()
            key = (id_or_slug or "").strip()
            if key.startswith("cat:"):
                key = key[4:]
            if not key:
                return False
            target_id = int(key) if key.isdigit() else 0
            before = len(self._pinned)
            self._pinned = [
                c
                for c in self._pinned
                if not ((target_id > 0 and c.id == target_id) or (c.slug and c.slug == key))
            ]
            if len(self._pinned) != before:
                self._save_locked()
                return True
            return False

    def list_pinned(self) -> list[DiscourseCategory]:
        with self._lock:
            return list(self._pinned)

    def list_discovered(self) -> list[DiscourseCategory]:
        with self._lock:
            return list(self._discovered)

    def update_discovered(self, categories: list[DiscourseCategory], now_ts: float) -> None:
        with self._lock:
            self._discovered = list(categories)
            self._discovered_at = now_ts
            self._last_refresh_failed_at = 0.0
            updated = False
            for p in self._pinned:
                for d in self._discovered:
                    if (p.id > 0 and p.id == d.id) or (p.slug and p.slug == d.slug):
                        if p.id <= 0 < d.id:
                            p.id = d.id
                            updated = True
                        if not p.slug and d.slug:
                            p.slug = d.slug
                            updated = True
                        if (not p.name or p.name == p.slug or p.name.startswith("板块 #")) and d.name:
                            p.name = d.name
                            updated = True
                        break
            if updated:
                self._save_locked()

    def mark_refresh_failed(self, now_ts: float) -> None:
        with self._lock:
            self._last_refresh_failed_at = now_ts

    def needs_category_refresh(self, now_ts: float, ttl_sec: float = _CATEGORY_TTL_SEC) -> bool:
        with self._lock:
            if self._last_refresh_failed_at > 0 and (now_ts - self._last_refresh_failed_at) < _FAILURE_COOLDOWN_SEC:
                return False
            if not self._discovered:
                return True
            return (now_ts - self._discovered_at) >= ttl_sec

    def resolve_category(self, key: str) -> DiscourseCategory | None:
        with self._lock:
            k = (key or "").strip()
            if k.startswith("cat:"):
                k = k[4:]
            if not k:
                return None
            num_id = int(k) if k.isdigit() else 0
            pinned_keys = {(p.id, p.slug) for p in self._pinned}

            for d in self._discovered:
                if (num_id > 0 and d.id == num_id) or (d.slug and d.slug == k):
                    is_pin = any((p_id > 0 and p_id == d.id) or (p_slug and p_slug == d.slug) for p_id, p_slug in pinned_keys)
                    return DiscourseCategory(
                        id=d.id,
                        slug=d.slug,
                        name=d.name,
                        description=d.description,
                        topic_count=d.topic_count,
                        topics_week=d.topics_week,
                        parent_id=d.parent_id,
                        pinned=is_pin,
                    )
            for p in self._pinned:
                if (num_id > 0 and p.id == num_id) or (p.slug and p.slug == k):
                    return p
            for d in _default_linuxdo_boards():
                if (num_id > 0 and d.id == num_id) or (d.slug and d.slug == k):
                    return d
            if num_id > 0:
                return DiscourseCategory(id=num_id, name=f"板块 #{num_id}")
            return DiscourseCategory(slug=k, name=k)

    def category_name_by_id(self, cat_id: int) -> str:
        if cat_id <= 0:
            return ""
        with self._lock:
            for c in (*self._pinned, *self._discovered, *_default_linuxdo_boards()):
                if c.id == cat_id and c.name:
                    return c.name
            return ""

    def build_channels(self, *, is_authenticated: bool) -> list[TextChannel]:
        with self._lock:
            out: list[TextChannel] = [
                TextChannel(id="latest", name="最新", badge="", requires_auth=False),
                TextChannel(id="top", name="热门", badge="", requires_auth=False),
                TextChannel(
                    id="unread",
                    name="未读",
                    badge="" if is_authenticated else "需登录",
                    requires_auth=True,
                ),
            ]
            seen_ids: set[int] = set()
            seen_slugs: set[str] = set()

            for p in self._pinned:
                out.append(
                    TextChannel(
                        id=_category_channel_id(p),
                        name=p.name or p.slug or f"板块 #{p.id}",
                        badge="置顶",
                        requires_auth=False,
                    )
                )
                if p.id > 0:
                    seen_ids.add(p.id)
                if p.slug:
                    seen_slugs.add(p.slug)

            if self._discovered:
                sorted_disc = sorted(
                    self._discovered,
                    key=lambda c: (c.topics_week, c.topic_count),
                    reverse=True,
                )
                added = 0
                for d in sorted_disc:
                    if added >= _MAX_DYNAMIC_CATEGORIES:
                        break
                    if d.parent_id > 0:
                        continue
                    if d.id > 0 and d.id in seen_ids:
                        continue
                    if d.slug and d.slug in seen_slugs:
                        continue
                    out.append(
                        TextChannel(
                            id=_category_channel_id(d),
                            name=d.name or d.slug or f"板块 #{d.id}",
                            badge="版面",
                            requires_auth=False,
                        )
                    )
                    if d.id > 0:
                        seen_ids.add(d.id)
                    if d.slug:
                        seen_slugs.add(d.slug)
                    added += 1
            elif not self._pinned:
                for d in _default_linuxdo_boards():
                    out.append(
                        TextChannel(
                            id=_category_channel_id(d),
                            name=d.name,
                            badge="版面",
                            requires_auth=False,
                        )
                    )
            return out
