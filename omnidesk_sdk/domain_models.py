"""Domain Pydantic v2 models for Media, Text, and Book workbenches."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AliasChoices, BaseModel, ConfigDict, Field, model_validator

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ContentDomain = Literal["core", "media", "text", "book"]
PluginKind = Literal["provider", "workspace", "hybrid", "extension"]
CurationMode = Literal["daily_immutable", "interactive_ephemeral"]
MediaKind = Literal["video", "music", "live", "nas"]
ChapterKind = Literal["content", "ad", "sponsor"]
TextParagraphKind = Literal["heading", "paragraph", "quote", "code", "formula", "image"]
BookFormat = Literal["pdf", "epub", "txt", "english_daily", "mobi", "azw3", "azw", "azw4", "fb2", "fbz", "rtf", "html", "htm", "htmlz", "docx", "odt", "cbz", "cbr", "cb7", "chm", "djvu", "lit", "lrf", "prc", "pdb", "pml", "rb", "snb", "tcr", "txtz", "kepub"]
BookBlockKind = Literal["heading", "paragraph", "caption", "formula", "image"]


def validate_non_empty_str(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("value must be a non-empty string")
    return stripped


def validate_iso_date(value: str) -> str:
    stripped = value.strip()
    if not _DATE_RE.match(stripped):
        raise ValueError(f"Invalid date {value!r}: expected YYYY-MM-DD")
    date.fromisoformat(stripped)
    return stripped


NonEmptyStr = Annotated[str, AfterValidator(validate_non_empty_str)]
IsoDateStr = Annotated[str, AfterValidator(validate_iso_date)]


def _infer_total_pages(data: Any, page_keys: tuple[str, ...]) -> Any:
    """Default `total_pages` to `page` when omitted and `page > 1`."""
    if not isinstance(data, Mapping):
        return data
    if "total_pages" in data or "totalPages" in data:
        return data
    for key in page_keys:
        if key in data:
            page = data[key]
            if isinstance(page, int) and not isinstance(page, bool) and page > 1:
                copied = dict(data)
                copied["total_pages"] = page
                return copied
            break
    return data


class LyricLine(BaseModel):
    """Synchronized bilingual lyric cue for MediaWorkbench."""

    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    time_sec: float = Field(default=0.0, ge=0.0, validation_alias=AliasChoices("time_sec", "timeSec"))
    original: str = ""
    translation: str = ""
    note: str = ""


class ChapterSegment(BaseModel):
    """Timeline chapter or SponsorBlock segment attached to a MediaItem."""

    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    id: str = ""
    start_sec: float = Field(default=0.0, ge=0.0, validation_alias=AliasChoices("start_sec", "startSec"))
    end_sec: float | None = Field(default=None, ge=0.0, validation_alias=AliasChoices("end_sec", "endSec"))
    label: str = ""
    digest: str = ""
    kind: ChapterKind = "content"

    @model_validator(mode="after")
    def _validate_bounds(self) -> ChapterSegment:
        if self.end_sec is not None and self.end_sec < self.start_sec:
            raise ValueError(f"end_sec ({self.end_sec}) must be >= start_sec ({self.start_sec})")
        return self


class MediaItem(BaseModel):
    """Domain 1 (Media & Music) item exchanged across plugins and workbenches."""

    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    id: NonEmptyStr
    source_id: str = Field(default="", validation_alias=AliasChoices("source_id", "sourceId"))
    title: NonEmptyStr
    subtitle: str = ""
    creator: str = ""
    kind: MediaKind = "video"
    source_badge: str = Field(default="", validation_alias=AliasChoices("source_badge", "badge"))
    cover_url: str = Field(default="", validation_alias=AliasChoices("cover_url", "coverUrl"))
    play_url: str = Field(default="", validation_alias=AliasChoices("play_url", "playUrl"))
    quality_label: str = Field(default="", validation_alias=AliasChoices("quality_label", "qualityLabel"))
    duration_sec: float = Field(default=0.0, ge=0.0, validation_alias=AliasChoices("duration_sec", "durationSec"))
    progress_sec: float = Field(default=0.0, ge=0.0, validation_alias=AliasChoices("progress_sec", "progressSec"))
    view_count: int = Field(default=0, ge=0, validation_alias=AliasChoices("view_count", "viewCount"))
    has_bilingual_sub: bool = Field(
        default=False,
        validation_alias=AliasChoices("has_bilingual_sub", "hasBilingualSub"),
    )
    aspect: Literal["16:9", "1:1"] | None = None
    summary: str = ""
    listening_cue: str = Field(default="", validation_alias=AliasChoices("listening_cue", "listeningCue"))
    curation_mode: CurationMode | None = Field(
        default=None,
        validation_alias=AliasChoices("curation_mode", "curationMode"),
    )
    business_date: IsoDateStr | None = Field(
        default=None,
        validation_alias=AliasChoices("business_date", "businessDate"),
    )
    previously_recommended_on: IsoDateStr | None = Field(
        default=None,
        validation_alias=AliasChoices("previously_recommended_on", "previouslyRecommendedOn"),
    )
    chapters: list[ChapterSegment] = Field(default_factory=list)
    lyrics: list[LyricLine] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class TextChannel(BaseModel):
    """Dynamic channel / sub-category exposed by a TextSource."""

    model_config = ConfigDict(populate_by_name=True)

    id: NonEmptyStr
    name: NonEmptyStr
    badge: str = ""
    requires_auth: bool = Field(default=False, validation_alias=AliasChoices("requires_auth", "requiresAuth"))
    count: int = Field(default=0, ge=0)


class TextFeedItem(BaseModel):
    """Domain 2 (Text, Communities & Journals) feed entry."""

    model_config = ConfigDict(populate_by_name=True)

    id: NonEmptyStr
    source_id: str = Field(default="", validation_alias=AliasChoices("source_id", "sourceId"))
    source_name: str = Field(default="", validation_alias=AliasChoices("source_name", "sourceName"))
    category: NonEmptyStr = "blog"
    channel_id: str = Field(default="", validation_alias=AliasChoices("channel_id", "channelId"))
    title: NonEmptyStr
    title_zh: str = Field(default="", validation_alias=AliasChoices("title_zh", "titleZh"))
    excerpt: str = ""
    author: str = ""
    published_at: str = Field(default="", validation_alias=AliasChoices("published_at", "publishedAt"))
    url: str = ""
    doi: str = ""
    cover_url: str = Field(default="", validation_alias=AliasChoices("cover_url", "coverUrl"))
    image_headers: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("image_headers", "imageHeaders"),
    )
    tags: list[str] = Field(default_factory=list)
    comment_count: int = Field(default=0, ge=0, validation_alias=AliasChoices("comment_count", "commentCount"))
    score: int = 0
    bilingual_cached: bool = Field(
        default=False,
        validation_alias=AliasChoices("bilingual_cached", "bilingualCached"),
    )
    curation_mode: CurationMode | None = Field(
        default=None,
        validation_alias=AliasChoices("curation_mode", "curationMode"),
    )
    business_date: IsoDateStr | None = Field(
        default=None,
        validation_alias=AliasChoices("business_date", "businessDate"),
    )
    previously_recommended_on: IsoDateStr | None = Field(
        default=None,
        validation_alias=AliasChoices("previously_recommended_on", "previouslyRecommendedOn"),
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class TextParagraph(BaseModel):
    """Structured bilingual content block inside a TextArticle."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = ""
    kind: TextParagraphKind = "paragraph"
    original: str = ""
    translated: str = ""
    language: str = ""


class CommentNode(BaseModel):
    """Recursive comment tree node attached to a TextArticle."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = ""
    author: str = ""
    role_badge: str = Field(default="", validation_alias=AliasChoices("role_badge", "roleBadge"))
    body: str = Field(default="", validation_alias=AliasChoices("body", "original"))
    translated_body: str = Field(default="", validation_alias=AliasChoices("translated_body", "translated"))
    created_at: str = Field(default="", validation_alias=AliasChoices("created_at", "createdAt", "timeAgo"))
    score: int = 0
    collapsed: bool = False
    replies: list[CommentNode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("replies", "children"),
    )


class TextArticle(BaseModel):
    """Full structured article, community thread, or journal paper payload."""

    model_config = ConfigDict(populate_by_name=True)

    id: NonEmptyStr
    source_id: str = Field(default="", validation_alias=AliasChoices("source_id", "sourceId"))
    source_name: str = Field(default="", validation_alias=AliasChoices("source_name", "sourceName"))
    category: NonEmptyStr = "blog"
    channel_id: str = Field(default="", validation_alias=AliasChoices("channel_id", "channelId"))
    title: NonEmptyStr
    title_zh: str = Field(default="", validation_alias=AliasChoices("title_zh", "titleZh"))
    subtitle: str = ""
    authors: str = ""
    author: str = ""
    published_at: str = Field(default="", validation_alias=AliasChoices("published_at", "publishedAt"))
    read_time: str = Field(default="", validation_alias=AliasChoices("read_time", "readTime"))
    url: str = ""
    doi: str = ""
    journal_name: str = Field(default="", validation_alias=AliasChoices("journal_name", "journalName"))
    impact_score: str = Field(default="", validation_alias=AliasChoices("impact_score", "impactScore"))
    summary_zh: str = Field(default="", validation_alias=AliasChoices("summary_zh", "summaryZh"))
    cover_url: str = Field(default="", validation_alias=AliasChoices("cover_url", "coverUrl"))
    image_headers: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("image_headers", "imageHeaders"),
    )
    tags: list[str] = Field(default_factory=list)
    paragraphs: list[TextParagraph] = Field(
        default_factory=list,
        validation_alias=AliasChoices("paragraphs", "blocks"),
    )
    comments: list[CommentNode] = Field(default_factory=list)
    curation_mode: CurationMode | None = Field(
        default=None,
        validation_alias=AliasChoices("curation_mode", "curationMode"),
    )
    business_date: IsoDateStr | None = Field(
        default=None,
        validation_alias=AliasChoices("business_date", "businessDate"),
    )
    previously_recommended_on: IsoDateStr | None = Field(
        default=None,
        validation_alias=AliasChoices("previously_recommended_on", "previouslyRecommendedOn"),
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_author_fields(self) -> TextArticle:
        if self.author and not self.authors:
            self.authors = self.author
        elif self.authors and not self.author:
            self.author = self.authors
        return self


class BookPageBlock(BaseModel):
    """Single bilingual block inside a BookSpread."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = ""
    kind: BookBlockKind = "paragraph"
    original: str = ""
    translated: str = ""
    image_url: str = ""
    translated_image_url: str = ""
    highlighted_words: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("highlighted_words", "highlightedWords", "keyTerms"),
    )


class BookSpread(BaseModel):
    """Bilingual page spread served by BookSource.fetch_spread."""

    model_config = ConfigDict(populate_by_name=True)

    book_id: NonEmptyStr = Field(validation_alias=AliasChoices("book_id", "bookId"))
    chapter_title: str = Field(default="", validation_alias=AliasChoices("chapter_title", "chapterTitle"))
    page_number: int = Field(default=1, ge=1, validation_alias=AliasChoices("page_number", "pageNumber", "page"))
    total_pages: int = Field(default=1, ge=1, validation_alias=AliasChoices("total_pages", "totalPages"))
    blocks: list[BookPageBlock] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _sync_total_pages(cls, data: Any) -> Any:
        return _infer_total_pages(data, ("page_number", "pageNumber", "page"))

    @model_validator(mode="after")
    def _validate_page_bounds(self) -> BookSpread:
        if self.page_number > self.total_pages:
            raise ValueError(f"page_number ({self.page_number}) must be <= total_pages ({self.total_pages})")
        return self


class BookItem(BaseModel):
    """Domain 3 (Books, BabelDOC PDF & EPUB Companion) shelf entry."""

    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    id: NonEmptyStr
    source_id: str = Field(default="", validation_alias=AliasChoices("source_id", "sourceId", "sourcePlugin"))
    title: NonEmptyStr
    author: str = ""
    format: BookFormat = "epub"
    language: str = ""
    file_path: str = Field(default="", validation_alias=AliasChoices("file_path", "filePath"))
    cover_path: str = Field(default="", validation_alias=AliasChoices("cover_path", "coverPath"))
    badge: str = ""
    current_page: int = Field(default=1, ge=1, validation_alias=AliasChoices("current_page", "currentPage"))
    total_pages: int = Field(default=1, ge=1, validation_alias=AliasChoices("total_pages", "totalPages"))
    progress_percent: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("progress_percent", "progressPct"),
    )
    bilingual_ready: bool = Field(default=False, validation_alias=AliasChoices("bilingual_ready", "bilingualReady"))
    vocab_count: int = Field(default=0, ge=0, validation_alias=AliasChoices("vocab_count", "vocabCount"))
    nas_synced: bool = Field(default=False, validation_alias=AliasChoices("nas_synced", "nasSynced"))
    spreads: list[BookSpread] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _sync_total_pages(cls, data: Any) -> Any:
        return _infer_total_pages(data, ("current_page", "currentPage"))

    @model_validator(mode="after")
    def _validate_page_bounds(self) -> BookItem:
        if self.current_page > self.total_pages:
            raise ValueError(f"current_page ({self.current_page}) must be <= total_pages ({self.total_pages})")
        return self


class VocabCard(BaseModel):
    """SM-2 spaced-repetition vocabulary card shared across workbenches."""

    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    id: str = ""
    word: NonEmptyStr
    lemma: str = ""
    phonetic: str = ""
    pos: str = ""
    etymology: str = ""
    meaning: str = Field(default="", validation_alias=AliasChoices("meaning", "meaning_zh", "meaningZh"))
    context_sentence: str = Field(default="", validation_alias=AliasChoices("context_sentence", "contextSentence"))
    source_book: str = Field(
        default="",
        validation_alias=AliasChoices("source_book", "book_title", "bookTitle"),
    )
    source_date: str = Field(default="", validation_alias=AliasChoices("source_date", "sourceDate"))
    interval_days: int = Field(default=1, ge=0, validation_alias=AliasChoices("interval_days", "intervalDays"))
    repetitions: int = Field(default=0, ge=0)
    ease_factor: float = Field(default=2.5, ge=1.3, validation_alias=AliasChoices("ease_factor", "easeFactor"))
    due_label: str = Field(default="", validation_alias=AliasChoices("due_label", "dueLabel"))
    nas_clip: dict[str, Any] | None = Field(default=None, validation_alias=AliasChoices("nas_clip", "nasClip"))
