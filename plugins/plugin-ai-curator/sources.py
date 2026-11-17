"""Domain data sources (MediaSource, TextSource, BookSource) for plugin-ai-curator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnidesk_sdk import (
    BookItem,
    BookPageBlock,
    BookSource,
    BookSpread,
    ChapterSegment,
    LyricLine,
    MediaItem,
    MediaSource,
    TextArticle,
    TextChannel,
    TextFeedItem,
    TextSource,
    VocabCard,
)

if __package__:
    from .models import parse_query_filter
    from .store import book_identity_key, media_identity_key, media_matches, text_identity_key, text_matches
else:
    from models import parse_query_filter  # type: ignore[no-redef]
    from store import book_identity_key, media_identity_key, media_matches, text_identity_key, text_matches  # type: ignore[no-redef]

if TYPE_CHECKING:
    if __package__:
        from .engine import CurationEngine
    else:
        from engine import CurationEngine  # type: ignore[no-redef]


class CuratorMediaSource(MediaSource):
    """Domain 1 (Media & Music) data provider backed by CurationEngine."""

    source_id = "plugin-ai-curator"
    display_name = "AI 策展与历史精选"

    def __init__(self, engine: CurationEngine) -> None:
        self._engine = engine

    def categories(self) -> list[str]:
        return ["每日 5 首 · 最新", "每日 5 首 · 全量曲库", "YouTube 每日精选", "AI 灵感推荐"]

    async def fetch_items(
        self,
        category: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[MediaItem]:
        _ = force_refresh
        self._engine.ensure_loaded()
        date_filter, kw = parse_query_filter(query)
        out: list[MediaItem] = []

        if category == "AI 灵感推荐":
            for sess in reversed(self._engine.interactive_sessions):
                for m in sess.media_items:
                    if m.id not in self._engine.dismissed_ids and media_matches(m, date_filter, kw):
                        out.append(m)
        elif category == "每日 5 首 · 最新":
            latest = max(
                (
                    str(m.business_date or m.extra.get("business_date") or "")
                    for m in (*self._engine.daily_media, *self._engine.archive.media_items)
                    if m.kind == "music"
                ),
                default="",
            )
            target_date = date_filter or latest
            out = self._dedup_media(
                (*self._engine.daily_media, *self._engine.archive.media_items),
                date_filter=target_date,
                kw=kw,
                kind_filter="music",
            )
        elif category in {"每日 5 首 · 全量曲库", "music"}:
            out = self._dedup_media(
                (*self._engine.daily_media, *self._engine.archive.media_items),
                date_filter=date_filter,
                kw=kw,
                kind_filter="music",
            )
        elif category in {"YouTube 每日精选", "video"}:
            out = self._dedup_media(
                (*self._engine.daily_media, *self._engine.archive.media_items),
                date_filter=date_filter,
                kw=kw,
                kind_filter="video",
            )
        else:
            combined = [
                *(m for s in reversed(self._engine.interactive_sessions) for m in s.media_items),
                *self._engine.daily_media,
                *self._engine.archive.media_items,
            ]
            out = self._dedup_media(combined, date_filter=date_filter, kw=kw, kind_filter=None)

        out.sort(key=lambda m: 0 if m.id in self._engine.pinned_ids else 1)
        page_size = 500
        start = max(0, (max(1, page) - 1) * page_size)
        return out[start : start + page_size]

    def _dedup_media(
        self,
        items: tuple[MediaItem, ...] | list[MediaItem],
        *,
        date_filter: str,
        kw: str,
        kind_filter: str | None,
    ) -> list[MediaItem]:
        seen: set[str] = set()
        out: list[MediaItem] = []
        for m in items:
            if kind_filter and m.kind != kind_filter:
                continue
            if m.id in self._engine.dismissed_ids or not media_matches(m, date_filter, kw):
                continue
            key = media_identity_key(m)
            if key not in seen:
                seen.add(key)
                out.append(m)
        return out

    async def fetch_chapters(self, item_id: str) -> list[ChapterSegment]:
        self._engine.ensure_loaded()
        for m in (*self._engine.daily_media, *self._engine.archive.media_items):
            if m.id == item_id:
                return list(m.chapters)
        for sess in reversed(self._engine.interactive_sessions):
            for m in sess.media_items:
                if m.id == item_id:
                    return list(m.chapters)
        return []

    async def fetch_lyrics(self, item_id: str) -> list[LyricLine]:
        self._engine.ensure_loaded()
        for m in (*self._engine.daily_media, *self._engine.archive.media_items):
            if m.id == item_id:
                return list(m.lyrics)
        for sess in reversed(self._engine.interactive_sessions):
            for m in sess.media_items:
                if m.id == item_id:
                    return list(m.lyrics)
        return []


class CuratorTextSource(TextSource):
    """Domain 2 (Text, Communities & Scientific Journals) dynamic multi-channel source."""

    source_id = "plugin-ai-curator"
    display_name = "AI 每日精读与期刊"
    category = "multi"

    def __init__(self, engine: CurationEngine) -> None:
        self._engine = engine

    def channels(self) -> list[TextChannel]:
        self._engine.ensure_loaded()
        counts = {"news": 0, "journal": 0, "reddit": 0, "english": 0, "interactive": 0}
        seen: set[str] = set()
        for t in (*self._engine.daily_texts, *self._engine.archive.text_items):
            if t.id in self._engine.dismissed_ids:
                continue
            art = self._engine.daily_articles.get(t.id) or self._engine.archive.articles.get(t.id)
            key = text_identity_key(t, art)
            if key in seen:
                continue
            seen.add(key)
            if t.category in counts:
                counts[t.category] += 1
        for sess in self._engine.interactive_sessions:
            for t in sess.text_items:
                if t.id not in self._engine.dismissed_ids:
                    counts["interactive"] += 1
        return [
            TextChannel(id="news", name="每日早报 (News)", badge=str(counts["news"]), count=counts["news"]),
            TextChannel(id="journal", name="科学期刊精读 (Journal)", badge=str(counts["journal"]), count=counts["journal"]),
            TextChannel(id="reddit", name="Reddit 每日精选", badge=str(counts["reddit"]), count=counts["reddit"]),
            TextChannel(id="english", name="英语每日伴读 (English)", badge=str(counts["english"]), count=counts["english"]),
            TextChannel(id="interactive", name="AI 灵感阅读 (可重温)", badge=str(counts["interactive"]), count=counts["interactive"]),
        ]

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        _ = force_refresh
        self._engine.ensure_loaded()
        date_filter, kw = parse_query_filter(query)
        out: list[TextFeedItem] = []

        if not channel_id or channel_id == "interactive":
            for sess in reversed(self._engine.interactive_sessions):
                for t in sess.text_items:
                    art = sess.articles.get(t.id)
                    if t.id not in self._engine.dismissed_ids and text_matches(t, art, date_filter, kw):
                        out.append(t)

        if channel_id != "interactive":
            seen: set[str] = set()
            for t in (*self._engine.daily_texts, *self._engine.archive.text_items):
                if t.id in self._engine.dismissed_ids:
                    continue
                if channel_id and t.category != channel_id:
                    continue
                art = self._engine.daily_articles.get(t.id) or self._engine.archive.articles.get(t.id)
                key = text_identity_key(t, art)
                if key not in seen and text_matches(t, art, date_filter, kw):
                    seen.add(key)
                    out.append(t)

        out.sort(key=lambda t: 0 if t.id in self._engine.pinned_ids else 1)
        page_size = 500
        start = max(0, (max(1, page) - 1) * page_size)
        return out[start : start + page_size]

    async def fetch_article(self, article_id: str) -> TextArticle:
        self._engine.ensure_loaded()
        for sess in reversed(self._engine.interactive_sessions):
            if article_id in sess.articles:
                return sess.articles[article_id]
        if article_id in self._engine.daily_articles:
            return self._engine.daily_articles[article_id]
        if article_id in self._engine.archive.articles:
            return self._engine.archive.articles[article_id]
        raise LookupError(f"Article not found: {article_id}")


class CuratorBookSource(BookSource):
    """Domain 3 (Books & English Daily Archive) data provider backed by CurationEngine."""

    source_id = "plugin-ai-curator"
    display_name = "英语精读归档与生词本"

    def __init__(self, engine: CurationEngine) -> None:
        self._engine = engine

    async def list_books(self, format_filter: str = "", query: str = "") -> list[BookItem]:
        self._engine.ensure_loaded()
        fmt = format_filter.strip().lower()
        date_filter, kw = parse_query_filter(query)
        out: list[BookItem] = []
        seen: set[str] = set()

        def _append(book: BookItem) -> None:
            key = book_identity_key(book)
            if key not in seen:
                seen.add(key)
                out.append(book)

        if fmt in {"", "all", "english_daily", "markdown"} and self._engine.archive.english_days:
            if not date_filter:
                master = BookItem(
                    id="english-archive-all",
                    source_id=self.source_id,
                    title="英语每日精读与翻译归档册",
                    author="AI Curation · 2026-09-04 至今",
                    format="english_daily",
                    total_pages=max(1, len(self._engine.archive.english_days)),
                    bilingual_ready=True,
                    vocab_count=len(self._engine.archive.vocab_cards),
                )
                if not kw or kw in f"{master.title} {master.author}".lower():
                    _append(master)
            for d, bundle in sorted(self._engine.archive.english_days.items(), reverse=True):
                if date_filter and d != date_filter:
                    continue
                vc_count = sum(len(n.vocab_cards) for n in bundle.notes)
                titles = " ".join(n.title for n in bundle.notes)
                b = BookItem(
                    id=f"english-date-{d}",
                    source_id=self.source_id,
                    title=f"英语伴读 · {d}",
                    author="AI Curation",
                    format="english_daily",
                    total_pages=max(1, len(bundle.notes)),
                    bilingual_ready=True,
                    vocab_count=vc_count,
                )
                if not kw or kw in f"{b.title} {titles}".lower():
                    _append(b)

        for b in (*self._engine.daily_books, *(b for s in self._engine.interactive_sessions for b in s.book_items)):
            if fmt and fmt != "all" and b.format.lower() != fmt:
                continue
            if date_filter and date_filter not in b.id and date_filter not in b.title:
                continue
            if kw and kw not in f"{b.title} {b.author}".lower():
                continue
            _append(b)
        return out

    async def fetch_spread(self, book_id: str, page_number: int = 1) -> BookSpread:
        self._engine.ensure_loaded()
        eng_days = self._engine.archive.english_days
        if book_id == "english-archive-all" and eng_days:
            ordered_dates = sorted(eng_days.keys(), reverse=True)
            total = len(ordered_dates)
            idx = max(0, min(total - 1, page_number - 1))
            d = ordered_dates[idx]
            bundle = eng_days[d]
            blocks: list[BookPageBlock] = []
            bidx = 1
            for note in bundle.notes:
                blocks.append(BookPageBlock(id=f"blk-{bidx}", kind="heading", original=note.title))
                bidx += 1
                for blk in note.blocks:
                    blocks.append(blk.model_copy(update={"id": f"blk-{bidx}"}))
                    bidx += 1
            return BookSpread(
                book_id=book_id,
                chapter_title=f"英语每日归档 · {d}",
                page_number=idx + 1,
                total_pages=total,
                blocks=blocks,
            )

        if book_id.startswith("english-date-"):
            d = book_id.removeprefix("english-date-")
            bundle = eng_days.get(d)
            if bundle and bundle.notes:
                total = len(bundle.notes)
                idx = max(0, min(total - 1, page_number - 1))
                note = bundle.notes[idx]
                return BookSpread(
                    book_id=book_id,
                    chapter_title=note.title,
                    page_number=idx + 1,
                    total_pages=total,
                    blocks=note.blocks,
                )

        for sess in reversed(self._engine.interactive_sessions):
            if book_id in sess.book_spreads:
                return sess.book_spreads[book_id]
        raise LookupError(f"Book not found: {book_id}")

    async def list_vocab(self) -> list[VocabCard]:
        self._engine.ensure_loaded()
        seen: set[str] = set()
        out: list[VocabCard] = []
        for vc in (*self._engine.archive.vocab_cards, *self._engine.daily_vocabs):
            key = vc.word.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(vc)
        return out
