"""Reddit JSON, Markdown (code/formula preservation), comment tree, and HTML fallback parsers."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from omnidesk_sdk import CommentNode, TextArticle, TextFeedItem, TextParagraph
from omnidesk_sdk.http import is_localhost_host, url_origin

if __package__:
    from .reddit_security import RedditPluginError, is_reddit_domain_host, origin_str, validate_https_url
    from .reddit_subscriptions import SubredditSubscription, normalize_subreddit_name
else:
    from reddit_security import RedditPluginError, is_reddit_domain_host, origin_str, validate_https_url
    from reddit_subscriptions import SubredditSubscription, normalize_subreddit_name

MAX_COMMENT_DEPTH = 64
_POST_ID_RE = re.compile(r"^[A-Za-z0-9]{4,16}$")
_THING_PREFIX_RE = re.compile(r"^t\d+_")
_ATX_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.*)|\s*)$")


class RedditParseError(RedditPluginError):
    """Raised when Reddit JSON or HTML payload is malformed or fails structural validation."""

    def __init__(self, message: str, *, error_code: str = "parse_error") -> None:
        super().__init__(error_code, message)


@dataclass(slots=True)
class MoreChildrenStub:
    """Deferred Reddit `more` comment stub holding child IDs for `/api/morechildren.json`."""

    id: str = ""
    parent_id: str = ""
    depth: int = 0
    count: int = 0
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "count": self.count,
            "children": list(self.children),
        }


@dataclass(slots=True)
class FeedListingResult:
    """Parsed listing items plus Reddit `after`/`before` cursors."""

    items: list[TextFeedItem] = field(default_factory=list)
    after: str = ""
    before: str = ""


@dataclass(slots=True)
class ArticleParseResult:
    """Parsed Reddit post article, link fullname, subreddit, and deferred comment stubs."""

    article: TextArticle
    link_fullname: str = ""
    subreddit: str = ""
    more_stubs: list[MoreChildrenStub] = field(default_factory=list)


@dataclass(slots=True)
class ResolvedRedditTarget:
    """Resolved post ID, optional subreddit hint, and requested comment sort."""

    post_id: str = ""
    subreddit: str = ""
    comment_sort: str = "confidence"


def decode_html_entities(raw: str | None) -> str:
    """Decode HTML entities including `&nbsp;` -> space."""
    if not raw:
        return ""
    if "&" not in raw:
        return raw
    return html.unescape(raw).replace("\xa0", " ")


def format_timestamp_utc(created_utc: int | float) -> str:
    """Format epoch seconds as `YYYY-MM-DD HH:MM UTC`."""
    try:
        ts = int(created_utc)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def strip_thing_prefix(value: str | None) -> str:
    """Remove Reddit fullname prefix like `t1_`, `t3_`, `t5_`."""
    s = str(value or "").strip()
    return _THING_PREFIX_RE.sub("", s, count=1)


def _read_str(obj: Mapping[str, Any], key: str, default: str = "") -> str:
    val = obj.get(key)
    return val if isinstance(val, str) else default


def _read_int(obj: Mapping[str, Any], key: str, default: int = 0) -> int:
    val = obj.get(key)
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return int(val)
    return default


def _read_bool(obj: Mapping[str, Any], key: str, default: bool = False) -> bool:
    val = obj.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    return default


def _make_excerpt(raw_text: str, max_len: int = 220) -> str:
    collapsed = " ".join(raw_text.split())
    if len(collapsed) > max_len:
        return collapsed[:max_len].rstrip() + "…"
    return collapsed


def _safe_external_or_reddit_url(raw_url: str, fallback: str = "") -> str:
    candidate = decode_html_entities(raw_url).strip()
    if not candidate:
        return fallback
    low = candidate.lower()
    if low.startswith("/") and not low.startswith("//"):
        try:
            return validate_https_url(f"https://www.reddit.com{candidate}")
        except RedditPluginError:
            return fallback
    if low.startswith("https://"):
        try:
            return validate_https_url(candidate)
        except RedditPluginError:
            return fallback
    if low.startswith("http://"):
        upgraded = "https://" + candidate[7:]
        try:
            return validate_https_url(upgraded)
        except RedditPluginError:
            return fallback
    return fallback


def _post_images(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Extract galleries in display order, then single images/previews."""
    images: list[tuple[str, str]] = []

    def add(raw: str, caption: str = "") -> None:
        url = _safe_external_or_reddit_url(raw)
        if url and url not in {item[0] for item in images}:
            images.append((url, decode_html_entities(caption)))

    gallery = data.get("gallery_data")
    metadata = data.get("media_metadata")
    if isinstance(gallery, Mapping) and isinstance(metadata, Mapping):
        entries = gallery.get("items")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            media = metadata.get(_read_str(entry, "media_id"))
            if not isinstance(media, Mapping) or media.get("status") != "valid":
                continue
            source = media.get("s")
            if isinstance(source, Mapping) and media.get("e") in {"Image", "AnimatedImage"}:
                add(_read_str(source, "gif") or _read_str(source, "u"), _read_str(entry, "caption"))
    if not images:
        direct = _read_str(data, "url_overridden_by_dest") or _read_str(data, "url")
        if re.search(r"\.(?:png|jpe?g|gif|webp|avif)$", urllib.parse.urlsplit(direct).path, re.I):
            add(direct)
    if not images:
        preview = data.get("preview")
        entries = preview.get("images") if isinstance(preview, Mapping) else None
        for entry in entries if isinstance(entries, list) else []:
            source = entry.get("source") if isinstance(entry, Mapping) else None
            if isinstance(source, Mapping):
                add(_read_str(source, "url"))
    if not images:
        parents = data.get("crosspost_parent_list")
        if isinstance(parents, list) and parents and isinstance(parents[0], Mapping):
            # Only one parent hop; malformed nested crossposts must stay bounded.
            parent = {k: v for k, v in parents[0].items() if k != "crosspost_parent_list"}
            images.extend(_post_images(parent))
    return images


def _image_paragraphs(images: list[tuple[str, str]], prefix: str = "image") -> list[TextParagraph]:
    return [TextParagraph(id=f"{prefix}-{idx}", kind="image", image_url=url, caption=caption, alt=caption)
            for idx, (url, caption) in enumerate(images, start=1)]


def resolve_article_target(
    article_id_or_url: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> ResolvedRedditTarget:
    """Parse Reddit post URL, `reddit:<sub>:<id>`, `r/<sub>/<id>`, or base36 ID."""
    raw = str(article_id_or_url or "").strip()
    if not raw:
        raise RedditParseError("article_id 或 URL 不能为空", error_code="invalid_argument")

    if "://" in raw:
        safe_raw = validate_https_url(
            raw,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        _, host, _ = url_origin(safe_raw)
        allowed_set = set(allowed_loopback_origins)
        is_allowed_lb = is_localhost_host(host) and (allow_loopback_http or origin_str(safe_raw) in allowed_set)
        if not is_reddit_domain_host(host) and not is_allowed_lb:
            raise RedditParseError(f"非 Reddit 域名链接被拒绝: {host}", error_code="invalid_argument")
        raw = safe_raw

    parsed_url = urllib.parse.urlsplit(raw)
    comment_sort = "confidence"
    if parsed_url.query:
        qs = urllib.parse.parse_qs(parsed_url.query)
        if qs.get("sort"):
            comment_sort = qs["sort"][0].strip() or "confidence"
    path_only = parsed_url.path if parsed_url.scheme else raw.split("?", 1)[0].split("#", 1)[0]

    subreddit = ""
    post_id = ""

    r_idx = path_only.find("/r/")
    if r_idx != -1:
        sub_part = path_only[r_idx + 3 :].split("/", 1)[0]
        subreddit = normalize_subreddit_name(sub_part)

    c_idx = path_only.find("/comments/")
    if c_idx != -1:
        rest = path_only[c_idx + 10 :]
        post_id = strip_thing_prefix(rest.split("/", 1)[0].split(".", 1)[0])
    elif "redd.it/" in raw:
        rest = raw.split("redd.it/", 1)[1].split("?", 1)[0].split("#", 1)[0]
        post_id = strip_thing_prefix(rest.split("/", 1)[0].split(".", 1)[0])
    elif raw.startswith("reddit:"):
        tail = raw[7:]
        if ":" in tail:
            sub_raw, pid_raw = tail.split(":", 1)
            subreddit = normalize_subreddit_name(sub_raw)
            post_id = strip_thing_prefix(pid_raw)
        else:
            post_id = strip_thing_prefix(tail)
    elif raw.startswith("r/"):
        parts = raw[2:].split("/")
        if len(parts) >= 2:
            subreddit = normalize_subreddit_name(parts[0])
            post_id = strip_thing_prefix(parts[1])
    else:
        post_id = strip_thing_prefix(path_only)

    if not _POST_ID_RE.match(post_id):
        raise RedditParseError(f"无效的 Reddit 帖子 ID: {article_id_or_url!r}", error_code="invalid_argument")
    return ResolvedRedditTarget(post_id=post_id, subreddit=subreddit, comment_sort=comment_sort)


def parse_markdown_to_paragraphs(markdown: str, id_prefix: str = "p") -> list[TextParagraph]:
    """Parse Reddit Markdown into `TextParagraph` blocks preserving headings, quotes, code, and formulas."""
    if not markdown or not markdown.strip():
        return []

    out: list[TextParagraph] = []
    seq = 1

    def _push(kind: str, text: str, language: str = "") -> None:
        nonlocal seq
        cleaned = text if kind in {"code", "formula"} else text.strip()
        if not cleaned.strip():
            return
        out.append(
            TextParagraph(
                id=f"{id_prefix}{seq}",
                kind=kind,  # type: ignore[arg-type]
                original=cleaned,
                language=language,
            )
        )
        seq += 1

    in_fence = False
    fence_marker = ""
    fence_lang = ""
    fence_buf: list[str] = []

    in_math = False
    math_close = "$$"
    math_buf: list[str] = []

    para_buf: list[str] = []
    quote_buf: list[str] = []
    indent_buf: list[str] = []

    def _flush_text() -> None:
        if quote_buf:
            _push("quote", "\n".join(quote_buf))
            quote_buf.clear()
        if indent_buf:
            _push("code", "\n".join(indent_buf).rstrip("\r\n"))
            indent_buf.clear()
        if para_buf:
            _push("paragraph", "\n".join(para_buf))
            para_buf.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip("\r")
        trimmed = line.strip()

        if in_fence:
            if trimmed.startswith(fence_marker):
                _push("code", "\n".join(fence_buf), language=fence_lang)
                fence_buf.clear()
                in_fence = False
                fence_lang = ""
            else:
                fence_buf.append(line)
            continue

        if in_math:
            close_pos = trimmed.find(math_close)
            if close_pos != -1:
                before = trimmed[:close_pos]
                if before:
                    math_buf.append(before)
                _push("formula", "\n".join(math_buf).strip())
                math_buf.clear()
                in_math = False
            else:
                math_buf.append(line)
            continue

        if trimmed.startswith("```") or trimmed.startswith("~~~"):
            _flush_text()
            in_fence = True
            fence_marker = trimmed[:3]
            fence_lang = trimmed[3:].strip().split()[0] if trimmed[3:].strip() else ""
            fence_buf.clear()
            continue

        if trimmed.startswith("$$"):
            _flush_text()
            rest = trimmed[2:].strip()
            if rest.endswith("$$") and len(rest) >= 2:
                _push("formula", rest[:-2].strip())
            else:
                in_math = True
                math_close = "$$"
                math_buf.clear()
                if rest:
                    math_buf.append(rest)
            continue

        if trimmed.startswith(r"\["):
            _flush_text()
            rest = trimmed[2:].strip()
            if rest.endswith(r"\]") and len(rest) >= 2:
                _push("formula", rest[:-2].strip())
            else:
                in_math = True
                math_close = r"\]"
                math_buf.clear()
                if rest:
                    math_buf.append(rest)
            continue

        if len(trimmed) > 2 and trimmed.startswith("$") and trimmed.endswith("$") and trimmed[1] != "$" and not trimmed[1].isspace():
            _flush_text()
            _push("formula", trimmed[1:-1].strip())
            continue

        if not trimmed:
            _flush_text()
            continue

        if trimmed in {"---", "***", "___"}:
            _flush_text()
            continue

        image_match = re.fullmatch(r'!\[([^\]]*)\]\(<?(https?://[^\s>]+?)>?(?:\s+"[^"]*")?\)', trimmed)
        if image_match:
            image_url = _safe_external_or_reddit_url(image_match[2])
            if image_url:
                _flush_text()
                out.append(TextParagraph(id=f"{id_prefix}{seq}", kind="image", image_url=image_url, alt=image_match[1]))
                seq += 1
                continue

        heading_match = _ATX_HEADING_RE.match(trimmed)
        if heading_match:
            _flush_text()
            _push("heading", (heading_match.group(2) or "").strip())
            continue

        if trimmed.startswith(">"):
            if para_buf or indent_buf:
                _flush_text()
            quote_buf.append(trimmed[1:].strip())
            continue

        if not para_buf and not quote_buf and (line.startswith("    ") or line.startswith("\t")):
            indent_buf.append(line[1:] if line.startswith("\t") else line[4:])
            continue

        if quote_buf or indent_buf:
            _flush_text()
        para_buf.append(trimmed)

    if in_fence and fence_buf:
        _push("code", "\n".join(fence_buf), language=fence_lang)
    if in_math and math_buf:
        _push("formula", "\n".join(math_buf).strip())
    _flush_text()
    return out


def _build_role_badge(data: Mapping[str, Any], post_author: str) -> str:
    badges: list[str] = []
    author = _read_str(data, "author")
    is_submitter = _read_bool(data, "is_submitter", False) or (
        bool(author) and bool(post_author) and author == post_author and author != "[deleted]"
    )
    if is_submitter:
        badges.append("OP")
    distinguished = _read_str(data, "distinguished").lower()
    if distinguished == "moderator":
        badges.append("MOD")
    elif distinguished in {"admin", "special"}:
        badges.append("ADMIN")
    flair = decode_html_entities(_read_str(data, "author_flair_text")).strip()
    if flair and flair != "null":
        badges.append(flair)
    return " · ".join(badges)


def _parse_comment_thing(
    thing: Any,
    post_author: str,
    depth: int,
    out_stubs: list[MoreChildrenStub],
) -> CommentNode | None:
    if not isinstance(thing, Mapping) or depth > MAX_COMMENT_DEPTH:
        return None
    kind = _read_str(thing, "kind")
    data = thing.get("data") if isinstance(thing.get("data"), Mapping) else thing

    if kind == "more":
        raw_children = data.get("children")
        child_ids = [
            strip_thing_prefix(c)
            for c in (raw_children if isinstance(raw_children, list) else [])
            if isinstance(c, str) and strip_thing_prefix(c)
        ]
        if child_ids:
            out_stubs.append(
                MoreChildrenStub(
                    id=_read_str(data, "id"),
                    parent_id=_read_str(data, "parent_id"),
                    depth=_read_int(data, "depth", depth),
                    count=_read_int(data, "count", len(child_ids)),
                    children=child_ids,
                )
            )
        return None

    if kind and kind not in {"t1", "comment"}:
        return None

    raw_id = strip_thing_prefix(_read_str(data, "id") or _read_str(data, "name"))
    raw_author = _read_str(data, "author", "[deleted]")
    raw_body = decode_html_entities(_read_str(data, "body") or _read_str(data, "content"))
    if not raw_id and not raw_body:
        return None

    score = _read_int(data, "score", _read_int(data, "ups", 0))
    created_utc = _read_int(data, "created_utc", _read_int(data, "created", 0))
    created_at = format_timestamp_utc(created_utc) if created_utc > 0 else _read_str(data, "created_at")
    collapsed = _read_bool(data, "collapsed", False) or raw_body in {"[deleted]", "[removed]"} or score <= -5
    author = raw_author if (raw_author.startswith("u/") or raw_author == "[deleted]") else f"u/{raw_author}"

    replies: list[CommentNode] = []
    rep = data.get("replies")
    if depth < MAX_COMMENT_DEPTH:
        if isinstance(rep, Mapping):
            rep_data = rep.get("data") if isinstance(rep.get("data"), Mapping) else rep
            rep_children = rep_data.get("children")
            if isinstance(rep_children, list):
                replies = _parse_comment_children(rep_children, post_author, depth + 1, out_stubs)
        elif isinstance(rep, list):
            replies = _parse_comment_children(rep, post_author, depth + 1, out_stubs)

    return CommentNode(
        id=raw_id or f"c_{depth}",
        author=author,
        role_badge=_build_role_badge(data, post_author),
        body=raw_body,
        created_at=created_at,
        score=score,
        collapsed=collapsed,
        replies=replies,
    )


def _parse_comment_children(
    children: Sequence[Any],
    post_author: str,
    depth: int,
    out_stubs: list[MoreChildrenStub],
) -> list[CommentNode]:
    if depth > MAX_COMMENT_DEPTH:
        return []
    out: list[CommentNode] = []
    for child in children:
        node = _parse_comment_thing(child, post_author, depth, out_stubs)
        if node is not None:
            out.append(node)
    return out


def _comment_id_exists(nodes: Sequence[CommentNode], short_id: str) -> bool:
    for node in nodes:
        if strip_thing_prefix(node.id) == short_id:
            return True
        if node.replies and _comment_id_exists(node.replies, short_id):
            return True
    return False


def _append_reply_to_parent(nodes: list[CommentNode], parent_short_id: str, child: CommentNode) -> bool:
    for node in nodes:
        if strip_thing_prefix(node.id) == parent_short_id:
            node.replies.append(child)
            return True
        if node.replies and _append_reply_to_parent(node.replies, parent_short_id, child):
            return True
    return False


def _decode_json_strict(json_text: str) -> Any:
    if not json_text or not json_text.strip():
        raise RedditParseError("Reddit 返回空 JSON 响应")
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise RedditParseError(f"Reddit JSON 解析失败: {exc.msg}") from exc


def parse_listing_feed(json_text: str, fallback_subreddit: str = "") -> FeedListingResult:
    """Parse a Reddit Listing JSON response into `FeedListingResult` (strictly fail-closed on malformed JSON)."""
    root = _decode_json_strict(json_text)
    if isinstance(root, dict) and "error" in root and "data" not in root:
        raise RedditParseError(f"Reddit API 返回错误对象: {root.get('message') or root.get('error')}")

    listing_obj = root[0] if isinstance(root, list) and root and isinstance(root[0], Mapping) else root
    if not isinstance(listing_obj, Mapping):
        raise RedditParseError("Reddit Listing 响应结构无效")
    data_obj = listing_obj.get("data") if isinstance(listing_obj.get("data"), Mapping) else listing_obj
    children = data_obj.get("children") if isinstance(data_obj.get("children"), list) else data_obj.get("items")
    if not isinstance(children, list):
        raise RedditParseError("Reddit Listing 缺少 children 数组")

    items: list[TextFeedItem] = []
    for entry in children:
        if not isinstance(entry, Mapping):
            continue
        kind = _read_str(entry, "kind", "t3")
        if kind not in {"t3", "link", "post"}:
            continue
        d = entry.get("data") if isinstance(entry.get("data"), Mapping) else entry
        post_id = strip_thing_prefix(_read_str(d, "id") or _read_str(d, "name"))
        raw_title = decode_html_entities(_read_str(d, "title"))
        if not post_id or not raw_title.strip():
            continue

        sub = normalize_subreddit_name(_read_str(d, "subreddit", fallback_subreddit))
        sub_prefixed = _read_str(d, "subreddit_name_prefixed") or (f"r/{sub}" if sub else "r/reddit")
        flair = decode_html_entities(_read_str(d, "link_flair_text")).strip()
        title = f"[{flair}] {raw_title}" if flair and flair != "null" else raw_title
        raw_author = _read_str(d, "author", "unknown")
        author = raw_author if raw_author.startswith("u/") else f"u/{raw_author}"
        published_at = format_timestamp_utc(_read_int(d, "created_utc", _read_int(d, "created", 0)))

        permalink = _read_str(d, "permalink")
        ext_url = _read_str(d, "url")
        fallback_url = f"https://www.reddit.com/r/{sub}/comments/{post_id}" if sub else f"https://www.reddit.com/comments/{post_id}"
        url = _safe_external_or_reddit_url(permalink) or _safe_external_or_reddit_url(ext_url, fallback_url)

        selftext = decode_html_entities(_read_str(d, "selftext"))
        is_self = _read_bool(d, "is_self", bool(selftext))
        domain = _read_str(d, "domain")
        if selftext.strip():
            excerpt = _make_excerpt(selftext)
        elif not is_self and ext_url.strip():
            safe_ext = _safe_external_or_reddit_url(ext_url, ext_url.strip())
            excerpt = (f"[{domain}] " if domain else "[外链] ") + safe_ext
        else:
            excerpt = f"{sub_prefixed} · {author}"

        tags = ["Reddit"]
        if sub_prefixed:
            tags.append(sub_prefixed)
        if flair and flair != "null":
            tags.append(flair)
        if not is_self and domain and not domain.startswith("self."):
            tags.append(domain)

        images = _post_images(d)
        items.append(
            TextFeedItem(
                id=f"reddit:{sub}:{post_id}" if sub else f"reddit:t3_{post_id}",
                source_id="plugin-reddit",
                source_name=sub_prefixed,
                category="reddit",
                channel_id=f"r/{sub}" if sub else "reddit",
                title=title,
                excerpt=excerpt,
                author=author,
                published_at=published_at,
                url=url,
                cover_url=images[0][0] if images else _safe_external_or_reddit_url(_read_str(d, "thumbnail")),
                tags=tags,
                comment_count=max(0, _read_int(d, "num_comments", 0)),
                score=_read_int(d, "score", _read_int(d, "ups", 0)),
                bilingual_cached=False,
            )
        )

    after_val = data_obj.get("after")
    before_val = data_obj.get("before")
    return FeedListingResult(
        items=items,
        after=after_val if isinstance(after_val, str) else "",
        before=before_val if isinstance(before_val, str) else "",
    )


def parse_post_and_comments(json_text: str, fallback_post_id: str = "") -> ArticleParseResult:
    """Parse a Reddit `[post_listing, comment_listing]` JSON response into `ArticleParseResult`."""
    root = _decode_json_strict(json_text)
    if isinstance(root, Mapping) and "error" in root and "data" not in root:
        raise RedditParseError(f"Reddit API 返回错误: {root.get('message') or root.get('error')}")

    post_data: Mapping[str, Any] | None = None
    comment_children: list[Any] | None = None

    if isinstance(root, list):
        if root and isinstance(root[0], Mapping):
            d0 = root[0].get("data")
            ch0 = d0.get("children") if isinstance(d0, Mapping) else None
            if isinstance(ch0, list) and ch0 and isinstance(ch0[0], Mapping) and isinstance(ch0[0].get("data"), Mapping):
                post_data = ch0[0]["data"]
        if len(root) >= 2 and isinstance(root[1], Mapping):
            d1 = root[1].get("data")
            ch1 = d1.get("children") if isinstance(d1, Mapping) else None
            if isinstance(ch1, list):
                comment_children = ch1
    elif isinstance(root, Mapping):
        d = root.get("data")
        ch = d.get("children") if isinstance(d, Mapping) else None
        if isinstance(ch, list) and ch and isinstance(ch[0], Mapping):
            if _read_str(ch[0], "kind") == "t3" and isinstance(ch[0].get("data"), Mapping):
                post_data = ch[0]["data"]
            else:
                comment_children = ch
    else:
        raise RedditParseError("Reddit 帖子响应根类型无效")

    if post_data is None and not comment_children:
        raise RedditParseError("未找到 Reddit 帖子或评论数据", error_code="not_found")

    more_stubs: list[MoreChildrenStub] = []
    post_author = ""
    if post_data is not None:
        post_id = strip_thing_prefix(_read_str(post_data, "id") or fallback_post_id)
        if not post_id:
            raise RedditParseError("Reddit 帖子缺少有效 ID")
        link_fullname = _read_str(post_data, "name") or f"t3_{post_id}"
        sub = normalize_subreddit_name(_read_str(post_data, "subreddit"))
        sub_prefixed = _read_str(post_data, "subreddit_name_prefixed") or (f"r/{sub}" if sub else "r/reddit")
        post_author = _read_str(post_data, "author", "unknown")
        author_prefixed = post_author if post_author.startswith("u/") else f"u/{post_author}"
        published = format_timestamp_utc(_read_int(post_data, "created_utc", _read_int(post_data, "created", 0)))
        score = _read_int(post_data, "score", _read_int(post_data, "ups", 0))
        num_comments = max(0, _read_int(post_data, "num_comments", 0))

        sub_parts = [sub_prefixed, author_prefixed, f"▲ {score}", f"💬 {num_comments}"]
        if published:
            sub_parts.append(published)
        subtitle = " · ".join(sub_parts)

        permalink = _read_str(post_data, "permalink")
        ext_url = _read_str(post_data, "url")
        fallback_url = f"https://www.reddit.com/r/{sub}/comments/{post_id}" if sub else f"https://www.reddit.com/comments/{post_id}"
        article_url = _safe_external_or_reddit_url(permalink) or _safe_external_or_reddit_url(ext_url, fallback_url)

        tags = ["Reddit"]
        if sub_prefixed:
            tags.append(sub_prefixed)
        flair = decode_html_entities(_read_str(post_data, "link_flair_text")).strip()
        if flair and flair != "null":
            tags.append(flair)

        selftext = decode_html_entities(_read_str(post_data, "selftext"))
        is_self = _read_bool(post_data, "is_self", bool(selftext))
        paragraphs = parse_markdown_to_paragraphs(selftext, "p") if selftext.strip() else []
        images = _post_images(post_data)
        existing_image_urls = {p.image_url for p in paragraphs if p.kind == "image"}
        paragraphs.extend(_image_paragraphs([image for image in images if image[0] not in existing_image_urls]))

        safe_ext = _safe_external_or_reddit_url(ext_url)
        if not is_self and safe_ext and safe_ext != article_url:
            domain = _read_str(post_data, "domain", "external")
            paragraphs.append(
                TextParagraph(id="p_link", kind="quote", original=f"External Link ({domain}): {safe_ext}")
            )

        cp_list = post_data.get("crosspost_parent_list")
        if isinstance(cp_list, list) and cp_list and isinstance(cp_list[0], Mapping) and not paragraphs:
            cp = cp_list[0]
            cp_sub = _read_str(cp, "subreddit_name_prefixed")
            cp_text = decode_html_entities(_read_str(cp, "selftext"))
            if cp_text.strip():
                paragraphs.append(TextParagraph(id="p_cp_hdr", kind="quote", original=f"Crossposted from {cp_sub}"))
                paragraphs.extend(parse_markdown_to_paragraphs(cp_text, "p_cp_"))

        raw_title = decode_html_entities(_read_str(post_data, "title")).strip() or f"Reddit Post {post_id}"
        if not paragraphs:
            paragraphs.append(
                TextParagraph(id="p1", kind="paragraph", original=f"链接帖: {safe_ext}" if safe_ext else raw_title)
            )
        comments = _parse_comment_children(comment_children or [], post_author, 0, more_stubs)
        article = TextArticle(
            id=f"reddit:{sub}:{post_id}" if sub else f"reddit:t3_{post_id}",
            source_id="plugin-reddit",
            source_name=sub_prefixed,
            category="reddit",
            channel_id=f"r/{sub}" if sub else "reddit",
            title=raw_title,
            subtitle=subtitle,
            authors=author_prefixed,
            published_at=published,
            url=article_url,
            cover_url=images[0][0] if images else "",
            tags=tags,
            paragraphs=paragraphs,
            comments=comments,
        )
        return ArticleParseResult(article=article, link_fullname=link_fullname, subreddit=sub, more_stubs=more_stubs)

    # Comment-only listing fallback
    post_id = strip_thing_prefix(fallback_post_id) or "unknown"
    comments = _parse_comment_children(comment_children or [], "", 0, more_stubs)
    article = TextArticle(
        id=f"reddit:t3_{post_id}",
        source_id="plugin-reddit",
        source_name="Reddit",
        category="reddit",
        channel_id="reddit",
        title=f"Reddit Thread {post_id}",
        comments=comments,
    )
    return ArticleParseResult(article=article, link_fullname=f"t3_{post_id}", more_stubs=more_stubs)


def attach_more_children(
    root_comments: list[CommentNode],
    link_fullname: str,
    post_author: str,
    morechildren_json: str,
) -> int:
    """Parse `/api/morechildren.json` and stitch nested replies into `root_comments`."""
    root = _decode_json_strict(morechildren_json)
    things: Any = None
    if isinstance(root, Mapping):
        if isinstance(root.get("json"), Mapping):
            json_obj = root["json"]
            errs = json_obj.get("errors")
            if isinstance(errs, list) and errs:
                raise RedditParseError(f"Reddit morechildren 返回错误: {errs[0]}")
            data = json_obj.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("things"), list):
                things = data["things"]
        elif isinstance(root.get("data"), Mapping):
            data = root["data"]
            things = data.get("things") if isinstance(data.get("things"), list) else data.get("children")
        elif isinstance(root.get("things"), list):
            things = root["things"]
    elif isinstance(root, list):
        things = root

    if not isinstance(things, list):
        raise RedditParseError("morechildren 响应缺少 things 数组")

    batch: list[tuple[str, str, str, CommentNode]] = []
    ignored_stubs: list[MoreChildrenStub] = []
    for item in things:
        node = _parse_comment_thing(item, post_author, 0, ignored_stubs)
        if node is None:
            continue
        short_id = strip_thing_prefix(node.id)
        if not short_id or _comment_id_exists(root_comments, short_id):
            continue
        d = item.get("data") if isinstance(item, Mapping) and isinstance(item.get("data"), Mapping) else item
        parent_full = _read_str(d, "parent_id") if isinstance(d, Mapping) else ""
        batch.append((short_id, parent_full, strip_thing_prefix(parent_full), node))

    if not batch:
        return 0

    id_to_idx = {short_id: idx for idx, (short_id, _, _, _) in enumerate(batch)}
    attached_within = [False] * len(batch)
    for i in range(len(batch) - 1, -1, -1):
        _, parent_full, parent_short, node = batch[i]
        if parent_full.startswith("t1_"):
            p_idx = id_to_idx.get(parent_short)
            if p_idx is not None and p_idx != i:
                batch[p_idx][3].replies.append(node)
                attached_within[i] = True

    link_short = strip_thing_prefix(link_fullname)
    for i, (_, parent_full, parent_short, node) in enumerate(batch):
        if attached_within[i]:
            continue
        if parent_full.startswith("t3_") or (link_short and parent_short == link_short) or not parent_short:
            root_comments.append(node)
        elif not _append_reply_to_parent(root_comments, parent_short, node):
            root_comments.append(node)
    return len(batch)


def parse_subscribed_subreddits(json_text: str) -> list[SubredditSubscription]:
    """Parse `/subreddits/mine/subscriber.json` into `SubredditSubscription` records."""
    root = _decode_json_strict(json_text)
    if not isinstance(root, Mapping) or not isinstance(root.get("data"), Mapping):
        raise RedditParseError("Reddit 订阅响应格式无效")
    children = root["data"].get("children")
    if not isinstance(children, list):
        raise RedditParseError("Reddit 订阅响应缺少 children 数组")

    out: list[SubredditSubscription] = []
    for entry in children:
        if not isinstance(entry, Mapping):
            continue
        kind = _read_str(entry, "kind", "t5")
        if kind not in {"t5", "subreddit"}:
            continue
        d = entry.get("data") if isinstance(entry.get("data"), Mapping) else entry
        name = normalize_subreddit_name(_read_str(d, "display_name"))
        if not name:
            continue
        prefixed = _read_str(d, "display_name_prefixed", f"r/{name}")
        title = decode_html_entities(_read_str(d, "title")).strip()
        disp = f"{prefixed} ({title})" if (title and title not in {name, prefixed} and len(title) <= 40) else prefixed
        out.append(
            SubredditSubscription(
                name=name,
                display_name=disp,
                subscribers=max(0, _read_int(d, "subscribers", 0)),
                pinned=True,
            )
        )
    return out


_VOID_HTML_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


class _OldRedditHtmlParser(HTMLParser):
    """Strict stdlib HTMLParser extracting `old.reddit.com` and `shreddit` things with tree nesting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.things: list[dict[str, Any]] = []
        self.stack: list[str] = []
        self._thing_stack: list[tuple[int, dict[str, Any]]] = []
        self._capture: str | None = None
        self._capture_depth = 0
        self._capture_thing: dict[str, Any] | None = None
        self._buf: list[str] = []
        self._ignored_depth = 0
        self.unclosed_script = False

    def _current_thing(self) -> dict[str, Any] | None:
        return self._thing_stack[-1][1] if self._thing_stack else None

    def _finish_capture(self) -> None:
        target = self._capture_thing
        if target is None or self._capture is None:
            self._capture = None
            self._capture_thing = None
            self._buf = []
            return
        raw_text = "".join(self._buf)
        if self._capture == "title":
            target["title"] = " ".join(raw_text.split())
        elif self._capture == "score":
            digits = re.sub(r"[^0-9\-]", "", raw_text.strip())
            if digits and digits != "-":
                try:
                    target["score"] = int(digits)
                except ValueError:
                    pass
        elif self._capture == "body":
            txt = "\n".join(ln.rstrip() for ln in raw_text.splitlines()).strip()
            if txt and not target["body"]:
                target["body"] = txt
        self._capture = None
        self._capture_depth = 0
        self._capture_thing = None
        self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in {"script", "style"}:
            self._ignored_depth += 1
            self.unclosed_script = True
            return
        if self._ignored_depth > 0:
            return
        if t == "br" and self._capture == "body":
            self._buf.append("\n")
        if t == "img":
            cur = self._current_thing()
            attrs_dict = dict(attrs)
            # Avatars and award icons are not post content.
            if cur is not None and not cur["is_comment"] and (
                self._capture == "body" or attrs_dict.get("id") == "post-image"
                or attrs_dict.get("slot") == "post-media"
            ):
                url = _safe_external_or_reddit_url(attrs_dict.get("data-src") or attrs_dict.get("src") or "")
                if url and url not in {image[0] for image in cur.get("images", [])}:
                    cur.setdefault("images", []).append((url, attrs_dict.get("alt") or ""))
        if t in _VOID_HTML_TAGS:
            return

        self.stack.append(t)
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_tokens = set(attr_map.get("class", "").split())

        if t == "div" and "thing" in cls_tokens:
            fullname = attr_map.get("data-fullname", "").strip()
            pid = strip_thing_prefix(fullname)
            if pid:
                item = {
                    "fullname": fullname,
                    "id": pid,
                    "subreddit": normalize_subreddit_name(attr_map.get("data-subreddit", "")),
                    "author": attr_map.get("data-author", "").strip(),
                    "permalink": attr_map.get("data-permalink", "").strip(),
                    "url": attr_map.get("data-url", "").strip(),
                    "is_comment": "comment" in cls_tokens,
                    "title": "",
                    "body": "",
                    "score": 0,
                    "replies": [],
                }
                self._thing_stack.append((len(self.stack), item))
                return

        if t == "shreddit-post":
            fullname = (attr_map.get("id") or attr_map.get("post-id") or "").strip()
            pid = strip_thing_prefix(fullname)
            if pid:
                score_raw = attr_map.get("score", "0").strip()
                item = {
                    "fullname": fullname,
                    "id": pid,
                    "subreddit": normalize_subreddit_name(attr_map.get("subreddit-name", "")),
                    "author": attr_map.get("author", "").strip(),
                    "permalink": attr_map.get("permalink", "").strip(),
                    "url": attr_map.get("content-href", "") or attr_map.get("permalink", ""),
                    "is_comment": False,
                    "title": decode_html_entities(attr_map.get("post-title", "")).strip(),
                    "body": "",
                    "score": int(score_raw) if score_raw.lstrip("-").isdigit() else 0,
                    "replies": [],
                }
                self._thing_stack.append((len(self.stack), item))
                return

        if t == "shreddit-comment":
            fullname = attr_map.get("thingid", "").strip()
            pid = strip_thing_prefix(fullname)
            if pid:
                score_raw = attr_map.get("score", "0").strip()
                item = {
                    "fullname": fullname,
                    "id": pid,
                    "subreddit": "",
                    "author": attr_map.get("author", "").strip(),
                    "permalink": attr_map.get("permalink", "").strip(),
                    "url": "",
                    "is_comment": True,
                    "title": "",
                    "body": "",
                    "score": int(score_raw) if score_raw.lstrip("-").isdigit() else 0,
                    "replies": [],
                }
                self._thing_stack.append((len(self.stack), item))
                return

        cur = self._current_thing()
        if cur is not None and self._capture is None:
            if t == "a" and "title" in cls_tokens and not cur["title"]:
                self._capture = "title"
                self._capture_depth = len(self.stack)
                self._capture_thing = cur
                self._buf = []
                if not cur["url"]:
                    cur["url"] = attr_map.get("href", "").strip()
            elif t == "div" and ("md" in cls_tokens or attr_map.get("slot") in {"text-body", "comment"}) and not cur["body"]:
                self._capture = "body"
                self._capture_depth = len(self.stack)
                self._capture_thing = cur
                self._buf = []
            elif t == "div" and "score" in cls_tokens and "unvoted" in cls_tokens:
                self._capture = "score"
                self._capture_depth = len(self.stack)
                self._capture_thing = cur
                self._buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in {"script", "style"}:
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            if self._ignored_depth == 0:
                self.unclosed_script = False
            return
        if self._ignored_depth > 0 or t in _VOID_HTML_TAGS:
            return

        if self._capture == "body" and t in {"p", "pre", "blockquote", "li", "h1", "h2", "h3"}:
            self._buf.append("\n")

        if self.stack and self.stack[-1] != t and t in self.stack:
            floor = self._thing_stack[-1][0] if self._thing_stack else 0
            while len(self.stack) > floor and self.stack[-1] != t:
                self.stack.pop()

        if self._capture is not None and len(self.stack) == self._capture_depth:
            if (self._capture == "title" and t == "a") or (self._capture in {"body", "score"} and t == "div"):
                self._finish_capture()

        if self.stack and self.stack[-1] == t:
            self.stack.pop()

        while self._thing_stack and len(self.stack) < self._thing_stack[-1][0]:
            _, finished = self._thing_stack.pop()
            if self._thing_stack:
                parent = self._thing_stack[-1][1]
                if finished.get("is_comment"):
                    parent["replies"].append(finished)
                else:
                    self.things.append(finished)
            else:
                self.things.append(finished)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        if self._capture is not None:
            self._buf.append(data)


def _parse_html_things(html_text: str) -> list[dict[str, Any]]:
    if not html_text or "\x00" in html_text or "<" not in html_text:
        raise RedditParseError("畸形或空 HTML 响应")
    parser = _OldRedditHtmlParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:
        raise RedditParseError(f"HTML 解析异常: {exc}") from exc
    if parser.unclosed_script or parser._thing_stack or not parser.things:
        raise RedditParseError("HTML 缺少有效 Reddit 内容或存在未闭合标签")
    return parser.things


def _build_comment_node_from_html(thing: Mapping[str, Any]) -> CommentNode:
    author = str(thing.get("author") or "unknown")
    author_pref = author if author.startswith("u/") or author == "[deleted]" else f"u/{author}"
    replies = [
        _build_comment_node_from_html(rep)
        for rep in (thing.get("replies") or [])
        if isinstance(rep, Mapping) and (rep.get("body") or rep.get("replies"))
    ]
    return CommentNode(
        id=str(thing.get("id") or ""),
        author=author_pref,
        body=str(thing.get("body") or ""),
        score=int(thing.get("score") or 0),
        replies=replies,
    )


def parse_listing_html(html_text: str, fallback_subreddit: str = "") -> FeedListingResult:
    """Parse rendered `old.reddit.com` listing HTML into `FeedListingResult` (fail-closed if malformed)."""
    things = _parse_html_things(html_text)
    items: list[TextFeedItem] = []
    for t in things:
        if t.get("is_comment") or not t.get("title"):
            continue
        sub = t.get("subreddit") or normalize_subreddit_name(fallback_subreddit)
        pid = t["id"]
        sub_prefixed = f"r/{sub}" if sub else "r/reddit"
        author = t["author"] or "unknown"
        if not author.startswith("u/"):
            author = f"u/{author}"
        url = _safe_external_or_reddit_url(
            t.get("permalink") or t.get("url") or "",
            f"https://www.reddit.com/comments/{pid}",
        )
        items.append(
            TextFeedItem(
                id=f"reddit:{sub}:{pid}" if sub else f"reddit:t3_{pid}",
                source_id="plugin-reddit",
                source_name=sub_prefixed,
                category="reddit",
                channel_id=f"r/{sub}" if sub else "reddit",
                title=t["title"],
                excerpt=_make_excerpt(t.get("body") or f"{sub_prefixed} · {author}"),
                author=author,
                url=url,
                cover_url=(t.get("images") or [("", "")])[0][0],
                tags=["Reddit", sub_prefixed],
                score=int(t.get("score") or 0),
            )
        )
    if not items:
        raise RedditParseError("渲染 HTML 中未提取到有效 Reddit 帖子列表")
    # Rendered listings omit the JSON envelope, but Reddit accepts the last
    # post fullname as `after` on the HTML route as well. Keep pagination
    # working when API access is blocked and the signed-in page is readable.
    last_id = items[-1].id.rsplit(":", 1)[-1]
    return FeedListingResult(items=items, after=f"t3_{strip_thing_prefix(last_id)}")


def parse_post_html(html_text: str, fallback_post_id: str = "") -> ArticleParseResult:
    """Parse rendered `old.reddit.com` thread HTML into `ArticleParseResult` (fail-closed if malformed)."""
    things = _parse_html_things(html_text)
    posts = [t for t in things if not t.get("is_comment") and t.get("title")]
    if not posts:
        raise RedditParseError("渲染 HTML 中未提取到有效 Reddit 帖子正文")
    p = posts[0]
    sub = p.get("subreddit") or ""
    pid = p.get("id") or strip_thing_prefix(fallback_post_id)
    sub_prefixed = f"r/{sub}" if sub else "r/reddit"
    author = p.get("author") or "unknown"
    author_prefixed = author if author.startswith("u/") else f"u/{author}"
    body = p.get("body") or p["title"]
    paragraphs = parse_markdown_to_paragraphs(body, "p")
    paragraphs.extend(_image_paragraphs(p.get("images") or []))
    comments = [
        _build_comment_node_from_html(c)
        for c in things
        if c.get("is_comment") and (c.get("body") or c.get("replies"))
    ]
    article = TextArticle(
        id=f"reddit:{sub}:{pid}" if sub else f"reddit:t3_{pid}",
        source_id="plugin-reddit",
        source_name=sub_prefixed,
        category="reddit",
        channel_id=f"r/{sub}" if sub else "reddit",
        title=p["title"],
        subtitle=f"{sub_prefixed} · {author_prefixed} · ▲ {int(p.get('score') or 0)}",
        authors=author_prefixed,
        cover_url=(p.get("images") or [("", "")])[0][0],
        url=_safe_external_or_reddit_url(
            p.get("permalink") or p.get("url") or "",
            f"https://www.reddit.com/comments/{pid}",
        ),
        tags=["Reddit", sub_prefixed],
        paragraphs=paragraphs,
        comments=comments,
    )
    return ArticleParseResult(article=article, link_fullname=f"t3_{pid}", subreddit=sub)
