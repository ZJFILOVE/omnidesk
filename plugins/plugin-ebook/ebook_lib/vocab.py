"""SM-2 spaced-repetition vocabulary store with atomic 0600 XDG persistence."""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnidesk_sdk import VocabCard

from .errors import BookNotFoundError, InvalidEbookArgError
from .security import atomic_write_json_0600, read_json_nofollow

_QUALITY_ALIASES: dict[str, int] = {"again": 1, "hard": 3, "good": 4, "easy": 5}
_VALID_QUALITIES: frozenset[int] = frozenset({1, 3, 4, 5})
_SLUG_SAFE_RE = re.compile(r"^[a-z0-9_-]+$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_SPACE_RE = re.compile(r"\s+")


def normalize_word(word: Any) -> str:
    """Collapse internal whitespace and normalize case to lowercase."""
    if not isinstance(word, str):
        raise InvalidEbookArgError("word must be a non-empty string")
    cleaned = _SPACE_RE.sub(" ", word.strip()).lower()
    if not cleaned:
        raise InvalidEbookArgError("word must be a non-empty string")
    return cleaned


def canonical_vocab_id(normalized_word: str) -> str:
    """Compute a deterministic, traversal-free ID for a normalized word."""
    if _SLUG_SAFE_RE.match(normalized_word) and len(normalized_word) <= 64:
        return f"vocab-{normalized_word}"
    slug = _NON_SLUG_RE.sub("-", normalized_word).strip("-")[:32] or "term"
    digest = hashlib.sha256(normalized_word.encode("utf-8")).hexdigest()[:10]
    return f"vocab-{slug}-{digest}"


def parse_sm2_quality(raw: Any) -> int:
    """Validate SM-2 quality grade (1, 3, 4, 5 or again/hard/good/easy)."""
    if isinstance(raw, bool):
        raise InvalidEbookArgError("quality cannot be a boolean; expected 1, 3, 4, or 5")
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in _QUALITY_ALIASES:
            return _QUALITY_ALIASES[key]
        try:
            raw = int(key)
        except ValueError as exc:
            raise InvalidEbookArgError(f"Unsupported SM-2 quality {raw!r}; expected 1, 3, 4, or 5") from exc
    if isinstance(raw, int) and raw in _VALID_QUALITIES:
        return raw
    raise InvalidEbookArgError(f"Unsupported SM-2 quality {raw!r}; expected 1, 3, 4, or 5")


def _parse_utc_timestamp(raw: Any, *, default: datetime | None = None) -> datetime:
    if raw is None or raw == "":
        return default if default is not None else datetime.now(UTC)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(raw):
            raise InvalidEbookArgError("timestamp must be finite")
        return datetime.fromtimestamp(float(raw), tz=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return default if default is not None else datetime.now(UTC)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidEbookArgError(f"Invalid ISO timestamp {raw!r}") from exc
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    raise InvalidEbookArgError(f"Unsupported timestamp value: {raw!r}")


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def apply_sm2(card: Mapping[str, Any], quality: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Apply SuperMemo SM-2 update to a vocab card dict for quality in {1, 3, 4, 5}."""
    q = parse_sm2_quality(quality)
    ts = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    prev_ef = float(card.get("ease_factor", 2.5))
    if not math.isfinite(prev_ef) or prev_ef < 1.3:
        prev_ef = 2.5
    prev_reps = max(0, int(card.get("repetitions", 0)))
    prev_interval = max(0, int(card.get("interval_days", 1)))

    next_ef = max(1.3, round(prev_ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)), 4))
    if q < 3:
        next_reps = 0
        next_interval = 1
        due_at = ts
        due_label = "今日待复习"
    else:
        if prev_reps == 0:
            next_interval = 1
        elif prev_reps == 1:
            next_interval = 6
        else:
            next_interval = max(1, round(prev_interval * next_ef))
        next_reps = prev_reps + 1
        due_at = ts + timedelta(days=next_interval)
        due_label = f"{next_interval} 天后到期"

    updated = dict(card)
    updated["ease_factor"] = next_ef
    updated["repetitions"] = next_reps
    updated["interval_days"] = next_interval
    updated["due_at"] = _iso_utc(due_at)
    updated["due_label"] = due_label
    updated["last_reviewed_at"] = _iso_utc(ts)
    updated["review_count"] = max(0, int(card.get("review_count", 0))) + 1
    updated["updated_at"] = _iso_utc(ts)
    VocabCard.model_validate(updated)
    return updated


def _coerce_card_input(raw: Any, *, now: datetime, preserve_schedule: bool = False) -> dict[str, Any]:
    if isinstance(raw, VocabCard):
        data = raw.model_dump()
        extra_raw: Mapping[str, Any] = {}
    elif isinstance(raw, str):
        data = {"word": raw}
        extra_raw = {}
    elif isinstance(raw, Mapping):
        extra_raw = raw
        data = dict(raw)
    else:
        raise InvalidEbookArgError("Vocab card entry must be a mapping, VocabCard, or word string")

    norm_word = normalize_word(data.get("word"))
    data["word"] = norm_word
    if not preserve_schedule:
        data.setdefault("interval_days", 1)
        data.setdefault("repetitions", 0)
        data.setdefault("ease_factor", 2.5)
    try:
        validated = VocabCard.model_validate(data)
    except Exception as exc:
        raise InvalidEbookArgError(f"Invalid vocab card payload: {exc}") from exc

    dumped = validated.model_dump()
    dumped["id"] = canonical_vocab_id(norm_word)
    dumped["word"] = norm_word
    if not dumped["lemma"]:
        dumped["lemma"] = norm_word
    if not dumped["source_date"]:
        dumped["source_date"] = now.astimezone(UTC).date().isoformat()
    if not dumped["due_label"]:
        dumped["due_label"] = "今日待复习" if dumped["repetitions"] == 0 else f"{dumped['interval_days']} 天后到期"

    due_at_dt = _parse_utc_timestamp(extra_raw.get("due_at"), default=now) if preserve_schedule else now
    dumped["due_at"] = _iso_utc(due_at_dt)
    dumped["last_reviewed_at"] = str(extra_raw.get("last_reviewed_at") or "") if preserve_schedule else ""
    dumped["review_count"] = max(0, int(extra_raw.get("review_count", 0))) if preserve_schedule else 0
    dumped["created_at"] = str(extra_raw.get("created_at") or _iso_utc(now))
    dumped["updated_at"] = str(extra_raw.get("updated_at") or _iso_utc(now)) if preserve_schedule else _iso_utc(now)
    return dumped


def _merge_blank_metadata(existing: dict[str, Any], incoming: Mapping[str, Any], *, now: datetime) -> bool:
    """Fill only empty metadata fields on an existing card without altering SM-2 progress."""
    changed = False
    for key in ("lemma", "phonetic", "pos", "etymology", "meaning", "context_sentence", "source_book", "source_date"):
        cur = str(existing.get(key) or "").strip()
        cand = str(incoming.get(key) or "").strip()
        if not cur and cand:
            existing[key] = cand
            changed = True
    if existing.get("nas_clip") is None and isinstance(incoming.get("nas_clip"), Mapping):
        existing["nas_clip"] = dict(incoming["nas_clip"])
        changed = True
    if changed:
        existing["updated_at"] = _iso_utc(now)
    return changed


class VocabStore:
    """Persistent, thread-safe SM-2 vocabulary card store."""

    def __init__(self, path_resolver: Callable[[], Path], plugin_id: str = "plugin-ebook") -> None:
        self._path_resolver = path_resolver
        self._plugin_id = plugin_id
        self._lock = threading.RLock()

    @property
    def storage_path(self) -> Path:
        return self._path_resolver()

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        path = self.storage_path
        raw = read_json_nofollow(path)
        if not raw:
            return {}
        cards_obj = raw.get("cards", raw if "version" not in raw else {})
        if isinstance(cards_obj, Mapping):
            items = cards_obj.values()
        elif isinstance(cards_obj, list):
            items = cards_obj
        else:
            return {}
        cards: dict[str, dict[str, Any]] = {}
        now = datetime.now(UTC)
        for entry in items:
            if not isinstance(entry, Mapping):
                continue
            try:
                coerced = _coerce_card_input(entry, now=now, preserve_schedule=True)
            except InvalidEbookArgError:
                continue
            cards[coerced["word"]] = coerced
        return cards

    def _save_unlocked(self, cards: Mapping[str, Mapping[str, Any]]) -> None:
        ordered = {k: dict(cards[k]) for k in sorted(cards)}
        payload = {
            "version": 1,
            "plugin_id": self._plugin_id,
            "updated_at": _iso_utc(datetime.now(UTC)),
            "cards": ordered,
        }
        atomic_write_json_0600(self.storage_path, payload)

    def _resolve_key(self, cards: Mapping[str, dict[str, Any]], word_or_id: Any) -> str | None:
        if not isinstance(word_or_id, str) or not word_or_id.strip():
            raise InvalidEbookArgError("word or id must be a non-empty string")
        cleaned = word_or_id.strip()
        norm = _SPACE_RE.sub(" ", cleaned).lower()
        if norm in cards:
            return norm
        for key, card in cards.items():
            if card.get("id") == cleaned or card.get("id") == norm:
                return key
        return None

    def add_card(self, raw: Mapping[str, Any] | VocabCard | str, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        """Add a normalized card or enrich metadata if it already exists (never resetting SM-2)."""
        ts = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        candidate = _coerce_card_input(raw, now=ts, preserve_schedule=False)
        key = candidate["word"]
        with self._lock:
            cards = self._load_unlocked()
            existing = cards.get(key)
            if existing is not None:
                updated = _merge_blank_metadata(existing, candidate, now=ts)
                if updated:
                    self._save_unlocked(cards)
                return dict(existing), False
            cards[key] = candidate
            self._save_unlocked(cards)
            return dict(candidate), True

    def get_card(self, word_or_id: str) -> dict[str, Any] | None:
        with self._lock:
            cards = self._load_unlocked()
            key = self._resolve_key(cards, word_or_id)
            return dict(cards[key]) if key is not None else None

    def list_cards(
        self,
        *,
        query: str = "",
        source_book: str = "",
        due_only: bool = False,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        ts = as_of.astimezone(UTC) if as_of is not None else datetime.now(UTC)
        q = query.strip().lower()
        src_filter = source_book.strip().lower()
        with self._lock:
            cards = list(self._load_unlocked().values())
        out: list[dict[str, Any]] = []
        for card in cards:
            if src_filter and src_filter not in str(card.get("source_book", "")).lower():
                continue
            if q and not any(
                q in str(card.get(field, "")).lower()
                for field in ("word", "lemma", "meaning", "etymology", "context_sentence", "source_book")
            ):
                continue
            if due_only and _parse_utc_timestamp(card.get("due_at"), default=ts) > ts:
                continue
            out.append(dict(card))
        out.sort(key=lambda c: (str(c.get("due_at", "")), str(c.get("word", ""))))
        return out

    def list_vocab_models(self) -> list[VocabCard]:
        return [VocabCard.model_validate(item) for item in self.list_cards()]

    def known_words(self) -> set[str]:
        with self._lock:
            return set(self._load_unlocked().keys())

    def review_card(self, word_or_id: str, quality: Any, *, now: datetime | None = None) -> dict[str, Any]:
        ts = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        with self._lock:
            cards = self._load_unlocked()
            key = self._resolve_key(cards, word_or_id)
            if key is None:
                raise BookNotFoundError(f"Vocabulary card not found: {word_or_id!r}")
            updated = apply_sm2(cards[key], quality, now=ts)
            cards[key] = updated
            self._save_unlocked(cards)
            return dict(updated)

    def due_cards(self, *, as_of: Any = None, limit: int | None = None) -> list[dict[str, Any]]:
        ts = _parse_utc_timestamp(as_of) if as_of is not None else datetime.now(UTC)
        items = self.list_cards(due_only=True, as_of=ts)
        if limit is not None and limit > 0:
            return items[:limit]
        return items

    def import_cards(self, items: Sequence[Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Idempotently import cards without overwriting existing cards or SM-2 progress."""
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise InvalidEbookArgError("import requires a list of vocab card items")
        ts = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        imported = 0
        skipped_existing = 0
        skipped_invalid = 0
        imported_cards: list[dict[str, Any]] = []

        with self._lock:
            cards = self._load_unlocked()
            dirty = False
            for raw in items:
                try:
                    coerced = _coerce_card_input(raw, now=ts, preserve_schedule=True)
                except InvalidEbookArgError:
                    skipped_invalid += 1
                    continue
                key = coerced["word"]
                if key in cards:
                    skipped_existing += 1
                    continue
                cards[key] = coerced
                imported += 1
                dirty = True
                imported_cards.append(dict(coerced))
            if dirty:
                self._save_unlocked(cards)
            total = len(cards)

        return {
            "imported": imported,
            "skipped_existing": skipped_existing,
            "skipped_invalid": skipped_invalid,
            "total_cards": total,
            "cards": imported_cards,
        }

    def export_cards(self) -> dict[str, Any]:
        items = self.list_cards()
        return {
            "version": 1,
            "plugin_id": self._plugin_id,
            "count": len(items),
            "cards": items,
        }

    def delete_card(self, word_or_id: str) -> bool:
        with self._lock:
            cards = self._load_unlocked()
            key = self._resolve_key(cards, word_or_id)
            if key is None:
                return False
            cards.pop(key, None)
            self._save_unlocked(cards)
            return True
