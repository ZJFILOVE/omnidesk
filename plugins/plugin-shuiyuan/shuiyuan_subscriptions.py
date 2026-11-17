"""Category discovery cache and pinned board persistence (`subscriptions.json`)."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import TextChannel

if __package__:
    from .shuiyuan_security import ShuiyuanError, atomic_write_json, has_control_chars, safe_child_path
else:
    from shuiyuan_security import ShuiyuanError, atomic_write_json, has_control_chars, safe_child_path

_MAX_DYNAMIC_CATEGORIES = 6


@dataclass(slots=True)
class DiscourseCategory:
    """Normalized Discourse category / board metadata."""

    id: int = 0
    slug: str = ""
    name: str = ""
    description: str = ""
    topic_count: int = 0
    topics_week: int = 0
    parent_id: int = 0
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _category_channel_id(cat: DiscourseCategory) -> str:
    return f"cat:{cat.id}" if cat.id > 0 else f"cat:{cat.slug}"


def _valid_slug(raw: Any) -> str:
    slug = str(raw or "").strip()
    if not slug:
        return ""
    if has_control_chars(slug) or "/" in slug or "\\" in slug or ".." in slug:
        raise ShuiyuanError("invalid_argument", f"版面 slug 包含非法字符: {slug!r}")
    return slug


class SubscriptionStore:
    """Manages pinned and discovered Shuiyuan categories backed by `subscriptions.json`."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._file_path: Path | None = None
        self._loaded = False
        self._last_config_spec = ""
        self._pinned: list[DiscourseCategory] = []
        self._discovered: list[DiscourseCategory] = []
        self._discovered_at_epoch = 0.0
        self.set_data_dir(data_dir)

    def set_data_dir(self, data_dir: Path | None) -> None:
        if data_dir is None:
            self._file_path = None
            return
        next_path = safe_child_path(Path(data_dir), "subscriptions.json")
        if next_path != self._file_path:
            self._file_path = next_path
            self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._file_path is None or self._file_path.is_symlink() or not self._file_path.is_file():
            return
        try:
            doc = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(doc, Mapping):
            return
        raw_pinned = doc.get("pinned_categories")
        if isinstance(raw_pinned, list):
            self._pinned = []
            for item in raw_pinned:
                if not isinstance(item, Mapping):
                    continue
                raw_id = item.get("id", 0)
                cid = int(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
                try:
                    slug = _valid_slug(item.get("slug"))
                except ShuiyuanError:
                    slug = ""
                name = str(item.get("name") or "").strip()
                if cid > 0 or slug:
                    self._pinned.append(
                        DiscourseCategory(
                            id=cid,
                            slug=slug,
                            name=name or slug or f"版面 #{cid}",
                            pinned=True,
                        )
                    )

    def _save_to_disk(self) -> None:
        if self._file_path is None:
            return
        payload = {
            "schema_version": 1,
            "pinned_categories": [{"id": c.id, "slug": c.slug, "name": c.name} for c in self._pinned],
        }
        atomic_write_json(self._file_path, payload, mode=0o600)

    def _merge_pinned(self, cat: DiscourseCategory) -> None:
        for existing in self._pinned:
            if (cat.id > 0 and existing.id == cat.id) or (
                cat.slug and existing.slug and existing.slug == cat.slug
            ):
                if existing.id <= 0 and cat.id > 0:
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
                name=cat.name or cat.slug or f"版面 #{cat.id}",
                pinned=True,
            )
        )

    def apply_config_pinned(self, pinned_spec: str) -> None:
        """Merge configured CSV or JSON array `pinned_categories` into persisted subscriptions."""
        self._ensure_loaded()
        spec = str(pinned_spec or "").strip()
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
                    cat = DiscourseCategory(pinned=True)
                    if isinstance(item, Mapping):
                        raw_id = item.get("id", 0)
                        cat.id = int(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
                        cat.slug = _valid_slug(item.get("slug"))
                        cat.name = str(item.get("name") or "").strip()
                    elif isinstance(item, int) and not isinstance(item, bool):
                        cat.id = item
                    elif isinstance(item, str):
                        s = item.strip()
                        if s.isdigit():
                            cat.id = int(s)
                        else:
                            cat.slug = _valid_slug(s)
                    if cat.id > 0 or cat.slug:
                        self._merge_pinned(cat)
                        changed = True
                if changed:
                    self._save_to_disk()
                return

        for raw_token in spec.split(","):
            token = raw_token.strip()
            if not token:
                continue
            cat = DiscourseCategory(pinned=True)
            if ":" in token:
                left, right = (p.strip() for p in token.split(":", 1))
                if left.isdigit():
                    cat.id = int(left)
                else:
                    cat.slug = _valid_slug(left)
                cat.name = right
            elif token.isdigit():
                cat.id = int(token)
            else:
                cat.slug = _valid_slug(token)
            if cat.id > 0 or cat.slug:
                self._merge_pinned(cat)
                changed = True
        if changed:
            self._save_to_disk()

    def pin_category(self, category_id: int = 0, slug: str = "", name: str = "") -> bool:
        """Pin a category by numeric ID or slug and persist to `subscriptions.json`."""
        self._ensure_loaded()
        clean_slug = _valid_slug(slug)
        clean_name = str(name or "").strip()
        if category_id <= 0 and not clean_slug:
            return False
        cat = DiscourseCategory(id=max(0, category_id), slug=clean_slug, name=clean_name, pinned=True)
        for d in self._discovered:
            if (cat.id > 0 and d.id == cat.id) or (cat.slug and d.slug == cat.slug):
                if cat.id <= 0:
                    cat.id = d.id
                if not cat.slug:
                    cat.slug = d.slug
                if not cat.name:
                    cat.name = d.name
                break
        self._merge_pinned(cat)
        self._save_to_disk()
        return True

    def unpin_category(self, id_or_slug: str) -> bool:
        """Remove a category from the pinned list by ID, `cat:<id>`, or slug."""
        self._ensure_loaded()
        key = str(id_or_slug or "").strip()
        if key.startswith("cat:"):
            key = key[4:].strip()
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
            self._save_to_disk()
            return True
        return False

    def list_pinned(self) -> list[DiscourseCategory]:
        self._ensure_loaded()
        return list(self._pinned)

    def list_discovered(self) -> list[DiscourseCategory]:
        return list(self._discovered)

    def update_discovered(self, categories: list[DiscourseCategory], now_epoch: float | None = None) -> None:
        self._ensure_loaded()
        self._discovered = list(categories)
        self._discovered_at_epoch = time.monotonic() if now_epoch is None else float(now_epoch)
        updated = False
        for p in self._pinned:
            for d in self._discovered:
                if (p.id > 0 and p.id == d.id) or (p.slug and p.slug == d.slug):
                    if p.id <= 0 and d.id > 0:
                        p.id = d.id
                        updated = True
                    if not p.slug and d.slug:
                        p.slug = d.slug
                        updated = True
                    if (not p.name or p.name == p.slug or p.name.startswith("版面 #")) and d.name:
                        p.name = d.name
                        updated = True
                    break
        if updated:
            self._save_to_disk()

    def needs_category_refresh(self, ttl_sec: float = 600.0) -> bool:
        if not self._discovered:
            return True
        return (time.monotonic() - self._discovered_at_epoch) >= ttl_sec

    def resolve_category(self, key: str) -> DiscourseCategory | None:
        self._ensure_loaded()
        k = str(key or "").strip()
        if k.startswith("cat:"):
            k = k[4:].strip()
        if not k:
            return None
        num_id = int(k) if k.isdigit() else 0
        for d in self._discovered:
            if (num_id > 0 and d.id == num_id) or (d.slug and d.slug == k):
                is_pinned = any((p.id > 0 and p.id == d.id) or (p.slug and p.slug == d.slug) for p in self._pinned)
                return DiscourseCategory(
                    id=d.id,
                    slug=d.slug,
                    name=d.name,
                    description=d.description,
                    topic_count=d.topic_count,
                    topics_week=d.topics_week,
                    parent_id=d.parent_id,
                    pinned=is_pinned,
                )
        for p in self._pinned:
            if (num_id > 0 and p.id == num_id) or (p.slug and p.slug == k):
                return DiscourseCategory(id=p.id, slug=p.slug, name=p.name, pinned=True)
        if num_id > 0:
            return DiscourseCategory(id=num_id, name=f"版面 #{num_id}")
        slug = _valid_slug(k)
        return DiscourseCategory(slug=slug, name=slug)

    def category_name_by_id(self, category_id: int) -> str:
        if category_id <= 0:
            return ""
        self._ensure_loaded()
        for c in (*self._pinned, *self._discovered):
            if c.id == category_id and c.name:
                return c.name
        return ""

    def build_channels(self, *, is_authenticated: bool) -> list[TextChannel]:
        """Build TextChannel list matching v1 order: latest, top, unread, pinned, top-6 discovered."""
        self._ensure_loaded()
        out: list[TextChannel] = [
            TextChannel(id="latest", name="最新", badge="", requires_auth=False),
            TextChannel(id="top", name="热门", badge="", requires_auth=False),
            TextChannel(id="unread", name="未读", badge="" if is_authenticated else "需登录", requires_auth=True),
        ]
        seen_ids: set[int] = set()
        seen_slugs: set[str] = set()

        for p in self._pinned:
            out.append(
                TextChannel(
                    id=_category_channel_id(p),
                    name=p.name or p.slug,
                    badge="置顶",
                    requires_auth=False,
                )
            )
            if p.id > 0:
                seen_ids.add(p.id)
            if p.slug:
                seen_slugs.add(p.slug)

        if self._discovered:
            sorted_disc = sorted(self._discovered, key=lambda c: (-c.topics_week, -c.topic_count))
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
                        name=d.name or d.slug,
                        badge="版面",
                        requires_auth=False,
                        count=max(0, d.topic_count),
                    )
                )
                if d.id > 0:
                    seen_ids.add(d.id)
                if d.slug:
                    seen_slugs.add(d.slug)
                added += 1
        elif not self._pinned:
            out.append(TextChannel(id="pinned", name="置顶版面", badge="版面", requires_auth=False))

        return out
