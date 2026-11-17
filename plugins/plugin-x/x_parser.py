"""GraphQL, Syndication JSON, HTML DOM, thread unrolling, and comment-tree parsers for plugin-x."""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from omnidesk_sdk import CommentNode, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .x_security import XPluginError
else:
    from x_security import XPluginError  # type: ignore[no-redef]

_STATUS_URL_RE = re.compile(
    r"^(?:https://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+|i/web|i)/status(?:es)?/([0-9]+)",
    re.IGNORECASE,
)
_LIST_URL_RE = re.compile(
    r"^(?:https://)?(?:www\.)?(?:x|twitter)\.com/i/lists/([0-9]+)",
    re.IGNORECASE,
)
_EXPLICIT_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_MONTH_ABBREV = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CLOUDFLARE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "challenges.cloudflare.com",
    "attention required! | cloudflare",
    "just a moment...",
    "cf-challenge-running",
    "enable javascript and cookies to continue",
)
_LOGIN_WALL_MARKERS = (
    "log in to x",
    "sign in to x",
    "log in to twitter",
    "sign in to twitter",
)
_GENERIC_SHELL_TITLES = frozenset({"x", "twitter", "home / x", "explore / x", "error", "loading"})


@dataclass(slots=True)
class ParsedXTarget:
    """Normalized X target parsed from tweet ID, list ID, handle, or HTTPS URL."""

    kind: str = "tweet"
    tweet_id: str = ""
    list_id: str = ""
    user_handle: str = ""
    canonical_id: str = ""
    web_url: str = ""
    unsafe_scheme: bool = False


@dataclass(slots=True)
class XMediaAttachment:
    """Photo, video, or animated GIF attachment on a tweet."""

    type: str = "photo"
    url: str = ""
    poster_url: str = ""
    alt_text: str = ""


@dataclass(slots=True)
class ParsedTweet:
    """Normalized tweet entity extracted from GraphQL or Syndication payloads."""

    id: str = ""
    conversation_id: str = ""
    author_id: str = ""
    author_name: str = ""
    author_handle: str = ""
    author_verified: bool = False
    text: str = ""
    article_title: str = ""
    created_at: str = ""
    url: str = ""
    in_reply_to_tweet_id: str = ""
    in_reply_to_handle: str = ""
    retweeted_by_handle: str = ""
    retweeted_by_name: str = ""
    quoted_tweet_id: str = ""
    quoted_author_name: str = ""
    quoted_author_handle: str = ""
    quoted_text: str = ""
    quoted_url: str = ""
    hashtags: list[str] = field(default_factory=list)
    media: list[XMediaAttachment] = field(default_factory=list)
    like_count: int = 0
    reply_count: int = 0
    retweet_count: int = 0
    bookmark_count: int = 0
    view_count: int = 0
    is_note_tweet: bool = False
    is_article: bool = False
    is_promoted: bool = False


@dataclass(slots=True)
class XListInfo:
    """X List metadata persisted to `subscriptions.json`."""

    id: str
    name: str = ""
    description: str = ""
    owner_handle: str = ""
    member_count: int = 0
    subscriber_count: int = 0
    pinned: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "owner_handle": self.owner_handle,
            "member_count": self.member_count,
            "subscriber_count": self.subscriber_count,
            "pinned": self.pinned,
        }


@dataclass(slots=True)
class TimelineParseResult:
    """Parsed timeline feed items, raw tweets, and pagination cursors."""

    items: list[TextFeedItem] = field(default_factory=list)
    tweets: list[ParsedTweet] = field(default_factory=list)
    top_cursor: str = ""
    bottom_cursor: str = ""


@dataclass(slots=True)
class ThreadParseResult:
    """Parsed thread article, focal/thread tweets, and recursive comment tree."""

    article: TextArticle | None = None
    focal_tweet: ParsedTweet | None = None
    thread_tweets: list[ParsedTweet] = field(default_factory=list)
    comments: list[CommentNode] = field(default_factory=list)
    next_cursor: str = ""
    has_more: bool = False


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate_chars(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def _read_int(obj: Mapping[str, Any], key: str, fallback: int = 0) -> int:
    val = obj.get(key)
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return fallback
    return fallback


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, Mapping) or k not in cur:
            return None
        cur = cur[k]
    return cur


def decode_html_entities(text: str) -> str:
    """Decode HTML character references cleanly."""
    if not text or "&" not in text:
        return text
    return html.unescape(text)


def format_twitter_timestamp(raw: Any) -> str:
    """Normalize X `created_at` (`Wed Nov 14 08:30:15 +0000 2026` or ISO-8601) to `YYYY-MM-DD HH:MM`."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    if len(s) >= 16 and s[:4].isdigit() and s[4] == "-" and s[7] == "-":
        return s[:16].replace("T", " ")
    parts = s.split()
    if len(parts) >= 6:
        mon = _MONTH_ABBREV.get(parts[1].lower(), 0)
        try:
            day = int(parts[2])
            year = int(parts[5])
        except ValueError:
            return s
        hhmm = parts[3][:5] if len(parts[3]) >= 5 else parts[3]
        if 1 <= mon <= 12 and 1 <= day <= 31 and year >= 2000:
            return f"{year:04d}-{mon:02d}-{day:02d} {hhmm}"
    return s


def format_author_display(tweet: ParsedTweet) -> str:
    if tweet.author_name and tweet.author_handle:
        return f"{tweet.author_name} (@{tweet.author_handle})"
    if tweet.author_handle:
        return f"@{tweet.author_handle}"
    if tweet.author_name:
        return tweet.author_name
    return "X User"


def make_tweet_title(tweet: ParsedTweet) -> str:
    prefix = f"[RT @{tweet.retweeted_by_handle}] " if tweet.retweeted_by_handle else ""
    author_tag = f"@{tweet.author_handle}" if tweet.author_handle else tweet.author_name
    if tweet.article_title:
        return f"{prefix}{author_tag + ': ' if author_tag else ''}{tweet.article_title}"

    first_line = ""
    for line in tweet.text.splitlines():
        collapsed = _collapse_ws(line)
        if collapsed:
            first_line = collapsed
            break
    if not first_line and tweet.quoted_text:
        first_line = _collapse_ws(tweet.quoted_text)
    if not first_line and tweet.media:
        first_line = f"[媒体推文 · {len(tweet.media)} 项附件]"
    if not first_line:
        first_line = f"Post #{tweet.id}"
    clipped = _truncate_chars(first_line, 96)
    return f"{prefix}{author_tag}: {clipped}" if author_tag else f"{prefix}{clipped}"


def make_tweet_excerpt(tweet: ParsedTweet, max_chars: int = 180) -> str:
    body = _collapse_ws(tweet.text)
    if tweet.quoted_text:
        qauthor = f"@{tweet.quoted_author_handle}" if tweet.quoted_author_handle else tweet.quoted_author_name
        body += f" [QT {qauthor}: {_collapse_ws(tweet.quoted_text)}]"
    if not body and tweet.media:
        body = f"[{tweet.media[0].type}] {tweet.media[0].url}"
    return _truncate_chars(body, max_chars)


def tweet_to_feed_item(tweet: ParsedTweet, channel_tag: str = "", channel_id: str = "") -> TextFeedItem:
    """Convert `ParsedTweet` to validated SDK `TextFeedItem`."""
    tags = list(tweet.hashtags)
    if channel_tag and channel_tag not in tags:
        tags.append(channel_tag)
    return TextFeedItem(
        id=tweet.id,
        source_id="plugin-x",
        source_name="X" if not channel_tag else f"X · {channel_tag}",
        category="x",
        channel_id=channel_id,
        title=make_tweet_title(tweet),
        excerpt=make_tweet_excerpt(tweet, 200),
        author=format_author_display(tweet),
        published_at=tweet.created_at,
        url=tweet.url,
        tags=tags,
        comment_count=max(0, tweet.reply_count),
        score=tweet.like_count,
    )


def parse_target_id_or_url(id_or_url: str) -> ParsedXTarget:
    """Parse tweet ID, `tweet:<id>`, `list:<id>`, `@handle`, or HTTPS X URL."""
    s = (id_or_url or "").strip()
    if not s:
        return ParsedXTarget(kind="unknown")
    if s.isdigit():
        return ParsedXTarget(
            kind="tweet",
            tweet_id=s,
            canonical_id=s,
            web_url=f"https://x.com/i/web/status/{s}",
        )
    low = s.lower()
    if low.startswith(("tweet:", "status:")):
        tid = s.split(":", 1)[1].strip()
        if tid.isdigit():
            return ParsedXTarget(
                kind="tweet",
                tweet_id=tid,
                canonical_id=tid,
                web_url=f"https://x.com/i/web/status/{tid}",
            )
    if low.startswith("list:"):
        lid = s[5:].strip()
        if lid:
            return ParsedXTarget(
                kind="list",
                list_id=lid,
                canonical_id=f"list:{lid}",
                web_url=f"https://x.com/i/lists/{lid}",
            )

    if scheme_m := _EXPLICIT_SCHEME_RE.match(s):
        scheme = scheme_m.group(1).lower()
        if scheme != "https":
            return ParsedXTarget(kind="unknown", canonical_id=s, unsafe_scheme=True)

    if m := _STATUS_URL_RE.match(s):
        raw_handle, tid = m.group(1), m.group(2)
        handle = "" if raw_handle.lower() in {"i", "i/web"} else raw_handle
        web_url = f"https://x.com/{handle}/status/{tid}" if handle else f"https://x.com/i/web/status/{tid}"
        return ParsedXTarget(
            kind="tweet",
            tweet_id=tid,
            user_handle=handle,
            canonical_id=tid,
            web_url=web_url,
        )
    if m := _LIST_URL_RE.match(s):
        lid = m.group(1)
        return ParsedXTarget(
            kind="list",
            list_id=lid,
            canonical_id=f"list:{lid}",
            web_url=f"https://x.com/i/lists/{lid}",
        )
    if s.startswith("@") and len(s) > 1 and " " not in s:
        handle = s[1:]
        return ParsedXTarget(
            kind="user",
            user_handle=handle,
            canonical_id=f"@{handle}",
            web_url=f"https://x.com/{handle}",
        )
    return ParsedXTarget(kind="unknown", canonical_id=s)


def _find_instructions_arrays(node: Any, out: list[list[Any]], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, Mapping):
        inst = node.get("instructions")
        if isinstance(inst, list):
            out.append(inst)
            return
        for v in node.values():
            _find_instructions_arrays(v, out, depth + 1)
    elif isinstance(node, list):
        for elem in node:
            _find_instructions_arrays(elem, out, depth + 1)


def _is_promoted_item(entry_id: str, item_content: Any) -> bool:
    low_id = entry_id.lower()
    if low_id.startswith("promoted-") or "promoted-tweet" in low_id:
        return True
    return isinstance(item_content, Mapping) and "promotedMetadata" in item_content


def _extract_cursor(
    entry_id: str,
    obj: Any,
    cursors: dict[str, str],
) -> None:
    if not isinstance(obj, Mapping):
        return
    val = str(obj.get("value") or "").strip()
    if not val:
        return
    ctype = str(obj.get("cursorType") or "").strip().lower()
    low_id = entry_id.lower()
    if ctype == "top" or low_id.startswith("cursor-top"):
        if not cursors["top"]:
            cursors["top"] = val
    elif ctype == "bottom" or low_id.startswith("cursor-bottom"):
        cursors["bottom"] = val
    elif ctype in {"showmorethreads", "showmore", "showmorethreadsbarred"} or "cursor-showmore" in low_id:
        if not cursors["show_more"]:
            cursors["show_more"] = val


def parse_tweet_result_node(result_node: Any, quote_depth: int = 0) -> ParsedTweet | None:
    """Parse an X GraphQL tweet node (`Tweet`, `TweetWithVisibilityResults`, `NoteTweet`, `Article`, RT, QT)."""
    if not isinstance(result_node, Mapping):
        return None
    if isinstance(_dig(result_node, "tweet_results", "result"), Mapping):
        return parse_tweet_result_node(result_node["tweet_results"]["result"], quote_depth)
    if (
        isinstance(result_node.get("result"), Mapping)
        and "legacy" not in result_node
        and "rest_id" not in result_node
    ):
        return parse_tweet_result_node(result_node["result"], quote_depth)

    typename = str(result_node.get("__typename") or "")
    if typename in {"TweetTombstone", "TweetUnavailable"}:
        return None
    if typename == "TweetWithVisibilityResults" and isinstance(result_node.get("tweet"), Mapping):
        return parse_tweet_result_node(result_node["tweet"], quote_depth)

    legacy = result_node.get("legacy") if isinstance(result_node.get("legacy"), Mapping) else {}
    rest_id = str(result_node.get("rest_id") or legacy.get("id_str") or "").strip()
    if not rest_id:
        return None

    author_id = str(legacy.get("user_id_str") or "").strip()
    author_name = ""
    author_handle = ""
    author_verified = False
    user_res = _dig(result_node, "core", "user_results", "result") or _dig(
        result_node, "core", "user_result", "result"
    )
    if isinstance(user_res, Mapping):
        if not author_id:
            author_id = str(user_res.get("rest_id") or "").strip()
        author_verified = bool(user_res.get("is_blue_verified", False))
        ucore = user_res.get("core")
        if isinstance(ucore, Mapping):
            author_name = str(ucore.get("name") or "").strip()
            author_handle = str(ucore.get("screen_name") or "").strip()
        uleg = user_res.get("legacy")
        if isinstance(uleg, Mapping):
            if not author_name:
                author_name = str(uleg.get("name") or "").strip()
            if not author_handle:
                author_handle = str(uleg.get("screen_name") or "").strip()
            if not author_verified:
                author_verified = bool(uleg.get("verified", False))

    if quote_depth == 0:
        rt_inner = _dig(legacy, "retweeted_status_result", "result")
        if isinstance(rt_inner, Mapping):
            inner = parse_tweet_result_node(rt_inner, quote_depth)
            if inner is not None:
                inner.retweeted_by_handle = author_handle
                inner.retweeted_by_name = author_name
                return inner

    tweet = ParsedTweet(
        id=rest_id,
        author_id=author_id,
        author_name=decode_html_entities(author_name),
        author_handle=author_handle,
        author_verified=author_verified,
        conversation_id=str(legacy.get("conversation_id_str") or "").strip() or rest_id,
        in_reply_to_tweet_id=str(legacy.get("in_reply_to_status_id_str") or "").strip(),
        in_reply_to_handle=str(legacy.get("in_reply_to_screen_name") or "").strip(),
        created_at=format_twitter_timestamp(legacy.get("created_at") or ""),
        like_count=_read_int(legacy, "favorite_count", 0),
        reply_count=_read_int(legacy, "reply_count", 0),
        retweet_count=_read_int(legacy, "retweet_count", 0),
        bookmark_count=_read_int(legacy, "bookmark_count", 0),
        view_count=_read_int(result_node.get("views") or {}, "count", 0)
        if isinstance(result_node.get("views"), Mapping)
        else 0,
        url=f"https://x.com/{author_handle}/status/{rest_id}"
        if author_handle
        else f"https://x.com/i/web/status/{rest_id}",
    )

    raw_text = ""
    note_entities: Mapping[str, Any] | None = None

    art = _dig(result_node, "article", "article_results", "result")
    if isinstance(art, Mapping):
        tweet.is_article = True
        tweet.article_title = decode_html_entities(str(art.get("title") or "").strip())
        raw_text = str(art.get("plain_text") or "").strip()
        if not raw_text and isinstance(_dig(art, "content_state", "blocks"), list):
            blocks_out: list[str] = []
            for blk in art["content_state"]["blocks"]:
                if not isinstance(blk, Mapping):
                    continue
                btext = str(blk.get("text") or "").strip()
                btype = str(blk.get("type") or "unstyled")
                if not btext:
                    continue
                if btype in {"header-one", "header-two", "header-three"}:
                    blocks_out.append(f"# {btext}")
                elif btype == "blockquote":
                    blocks_out.append(f"> {btext}")
                elif btype == "code-block":
                    blocks_out.append(f"```\n{btext}\n```")
                else:
                    blocks_out.append(btext)
            raw_text = "\n\n".join(blocks_out)
        if not raw_text:
            raw_text = str(art.get("preview_text") or "").strip()

    if not raw_text:
        nt = _dig(result_node, "note_tweet", "note_tweet_results", "result")
        if isinstance(nt, Mapping):
            tweet.is_note_tweet = True
            raw_text = str(nt.get("text") or "")
            if isinstance(nt.get("entity_set"), Mapping):
                note_entities = nt["entity_set"]

    if not raw_text:
        raw_text = str(legacy.get("full_text") or legacy.get("text") or "")

    raw_text = decode_html_entities(raw_text)

    def _apply_entities(ent: Any) -> None:
        nonlocal raw_text
        if not isinstance(ent, Mapping):
            return
        if isinstance(ent.get("urls"), list):
            for u in ent["urls"]:
                if isinstance(u, Mapping):
                    su = str(u.get("url") or "")
                    eu = str(u.get("expanded_url") or "")
                    if su and eu:
                        raw_text = raw_text.replace(su, eu)
        if isinstance(ent.get("hashtags"), list):
            for h in ent["hashtags"]:
                if isinstance(h, Mapping):
                    tag = str(h.get("text") or "").strip()
                    if tag and tag not in tweet.hashtags:
                        tweet.hashtags.append(tag)

    _apply_entities(legacy.get("entities"))
    if note_entities is not None:
        _apply_entities(note_entities)

    media_arr = _dig(legacy, "extended_entities", "media")
    if not isinstance(media_arr, list):
        media_arr = _dig(legacy, "entities", "media")
    if isinstance(media_arr, list):
        for m in media_arr:
            if not isinstance(m, Mapping):
                continue
            short_media_url = str(m.get("url") or "")
            if short_media_url:
                raw_text = raw_text.replace(short_media_url, "")
            mtype = str(m.get("type") or "photo")
            poster = str(m.get("media_url_https") or m.get("media_url") or "")
            best_url = poster
            alt = str(m.get("ext_alt_text") or "").strip()
            if mtype in {"video", "animated_gif"} and isinstance(_dig(m, "video_info", "variants"), list):
                best_br = -1
                for var in m["video_info"]["variants"]:
                    if not isinstance(var, Mapping) or var.get("content_type") != "video/mp4":
                        continue
                    vurl = str(var.get("url") or "")
                    br = _read_int(var, "bitrate", 0)
                    if vurl and br >= best_br:
                        best_br = br
                        best_url = vurl
            if best_url:
                tweet.media.append(XMediaAttachment(type=mtype, url=best_url, poster_url=poster, alt_text=alt))

    tweet.text = raw_text.strip()

    if quote_depth == 0:
        qt_node = _dig(result_node, "quoted_status_result", "result")
        if isinstance(qt_node, Mapping):
            qt = parse_tweet_result_node(qt_node, quote_depth=1)
            if qt is not None:
                tweet.quoted_tweet_id = qt.id
                tweet.quoted_author_name = qt.author_name
                tweet.quoted_author_handle = qt.author_handle
                tweet.quoted_text = qt.text
                tweet.quoted_url = qt.url
                if qt.url:
                    tweet.text = tweet.text.replace(qt.url, "")
                    if qt.author_handle:
                        tweet.text = tweet.text.replace(
                            f"https://twitter.com/{qt.author_handle}/status/{qt.id}", ""
                        )
                    tweet.text = tweet.text.strip()

    return tweet


def append_text_segments_to_paragraphs(
    text: str,
    id_prefix: str,
    seq: int,
    out: list[TextParagraph],
) -> int:
    """Split tweet markdown-like text into structured `TextParagraph` blocks preserving code & LaTeX."""
    if not text:
        return seq

    in_code = False
    code_lang = ""
    in_formula = False
    code_lines: list[str] = []
    formula_lines: list[str] = []
    para_lines: list[str] = []

    def _flush_para() -> None:
        nonlocal seq
        p = "\n".join(para_lines).strip()
        para_lines.clear()
        if not p:
            return
        seq += 1
        pid = f"{id_prefix}_{seq}"
        if len(p) >= 4 and p.startswith("$") and p.endswith("$"):
            out.append(TextParagraph(id=pid, kind="formula", original=p))
        elif p.startswith(("# ", "## ", "### ")):
            stripped_h = p.lstrip("#").strip()
            out.append(TextParagraph(id=pid, kind="heading", original=stripped_h))
        elif p.startswith("> ") or (len(p) > 1 and p.startswith(">")):
            cleaned_q = "\n".join(
                ln.strip()[1:].strip() if ln.strip().startswith(">") else ln.strip() for ln in p.splitlines()
            )
            out.append(TextParagraph(id=pid, kind="quote", original=cleaned_q))
        else:
            out.append(TextParagraph(id=pid, kind="paragraph", original=p))

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        tline = line.strip()

        if in_code:
            if tline.startswith("```"):
                in_code = False
                code_str = "\n".join(code_lines)
                code_lines.clear()
                if code_str.strip():
                    seq += 1
                    out.append(
                        TextParagraph(
                            id=f"{id_prefix}_{seq}",
                            kind="code",
                            original=code_str,
                            language=code_lang,
                        )
                    )
                code_lang = ""
            else:
                code_lines.append(line)
            continue

        if in_formula:
            if tline in {"$$", r"\]"}:
                in_formula = False
                f_str = "\n".join(formula_lines).strip()
                formula_lines.clear()
                if f_str:
                    seq += 1
                    out.append(TextParagraph(id=f"{id_prefix}_{seq}", kind="formula", original=f_str))
            else:
                formula_lines.append(line)
            continue

        if tline.startswith("```"):
            _flush_para()
            in_code = True
            code_lang = tline[3:].strip()
            continue

        if tline in {"$$", r"\["}:
            _flush_para()
            in_formula = True
            continue

        if len(tline) > 4 and tline.startswith("$$") and tline.endswith("$$"):
            _flush_para()
            seq += 1
            out.append(
                TextParagraph(id=f"{id_prefix}_{seq}", kind="formula", original=tline[2:-2].strip())
            )
            continue

        if not tline:
            _flush_para()
            continue

        para_lines.append(line)

    if in_code and "\n".join(code_lines).strip():
        seq += 1
        out.append(
            TextParagraph(
                id=f"{id_prefix}_{seq}",
                kind="code",
                original="\n".join(code_lines),
                language=code_lang,
            )
        )
    if in_formula and "\n".join(formula_lines).strip():
        seq += 1
        out.append(
            TextParagraph(id=f"{id_prefix}_{seq}", kind="formula", original="\n".join(formula_lines).strip())
        )
    _flush_para()
    return seq


def split_tweet_text_to_paragraphs(
    tweet: ParsedTweet,
    id_prefix: str = "p",
    thread_index: int = 0,
    thread_total: int = 1,
) -> list[TextParagraph]:
    """Convert one `ParsedTweet` into structured `TextParagraph` blocks."""
    out: list[TextParagraph] = []
    seq = 0
    if thread_total > 1 and thread_index > 0:
        stamp = f" · {tweet.created_at}" if tweet.created_at else ""
        out.append(
            TextParagraph(
                id=f"{id_prefix}_hdr",
                kind="heading",
                original=f"[{thread_index}/{thread_total}] {format_author_display(tweet)}{stamp}",
            )
        )
    elif tweet.article_title:
        out.append(TextParagraph(id=f"{id_prefix}_art_hdr", kind="heading", original=tweet.article_title))

    seq = append_text_segments_to_paragraphs(tweet.text, id_prefix, seq, out)

    if tweet.quoted_text or tweet.quoted_tweet_id:
        seq += 1
        qauthor = f"@{tweet.quoted_author_handle}" if tweet.quoted_author_handle else tweet.quoted_author_name
        q_body = f"引用 {qauthor or '推文'}:\n{tweet.quoted_text}"
        if tweet.quoted_url:
            q_body += f"\n{tweet.quoted_url}"
        out.append(TextParagraph(id=f"{id_prefix}_quote_{seq}", kind="quote", original=q_body))

    for m in tweet.media:
        seq += 1
        label = (
            "[视频附件] "
            if m.type == "video"
            else ("[GIF 附件] " if m.type == "animated_gif" else "[图片附件] ")
        )
        text_val = f"{label}{m.url}" + (f" ({m.alt_text})" if m.alt_text else "")
        out.append(TextParagraph(id=f"{id_prefix}_media_{seq}", kind="paragraph", original=text_val))

    return out


def build_article_from_thread(
    author_thread: list[ParsedTweet],
    comments: list[CommentNode],
    focal_tweet_id: str = "",
) -> TextArticle | None:
    """Assemble unrolled OP thread + nested comments into a validated `TextArticle`."""
    if not author_thread:
        return None
    focal = author_thread[0]
    if focal_tweet_id:
        for t in author_thread:
            if t.id == focal_tweet_id:
                focal = t
                break
    root = author_thread[0]
    sub_parts = [format_author_display(root)]
    if focal.retweeted_by_handle:
        sub_parts.append(f"RT by @{focal.retweeted_by_handle}")
    sub_parts.extend(
        [
            f"❤ {focal.like_count}",
            f"🔁 {focal.retweet_count}",
            f"💬 {focal.reply_count}",
        ]
    )
    if focal.view_count > 0:
        sub_parts.append(f"👁 {focal.view_count}")

    paragraphs: list[TextParagraph] = []
    total = len(author_thread)
    for i, tw in enumerate(author_thread, start=1):
        paragraphs.extend(split_tweet_text_to_paragraphs(tw, f"t{i}", i, total))

    return TextArticle(
        id=focal.id,
        source_id="plugin-x",
        source_name=f"X Thread · {total} 推串推" if total > 1 else "X",
        category="x",
        title=make_tweet_title(focal),
        subtitle=" · ".join(sub_parts),
        authors=format_author_display(root),
        published_at=focal.created_at,
        url=focal.url,
        tags=list(focal.hashtags),
        paragraphs=paragraphs,
        comments=comments,
    )


def parse_timeline_graphql(
    root: Any,
    channel_tag: str = "",
    channel_id: str = "",
) -> TimelineParseResult:
    """Parse X URT GraphQL timeline responses into `TextFeedItem`s and cursors."""
    if not isinstance(root, Mapping):
        raise XPluginError("parse_error", "X Timeline 响应不是有效 JSON 对象")
    instruction_lists: list[list[Any]] = []
    _find_instructions_arrays(root, instruction_lists)

    out = TimelineParseResult()
    seen_ids: set[str] = set()
    cursors = {"top": "", "bottom": "", "show_more": ""}

    def _record(item_content: Any, entry_id: str) -> None:
        if _is_promoted_item(entry_id, item_content):
            return
        if not isinstance(item_content, Mapping) or "tweet_results" not in item_content:
            return
        tweet = parse_tweet_result_node(item_content["tweet_results"])
        if tweet is None or not tweet.id or tweet.id in seen_ids:
            return
        seen_ids.add(tweet.id)
        out.items.append(tweet_to_feed_item(tweet, channel_tag=channel_tag, channel_id=channel_id))
        out.tweets.append(tweet)

    def _process_entry(entry: Any) -> None:
        if not isinstance(entry, Mapping):
            return
        entry_id = str(entry.get("entryId") or "")
        content = entry.get("content")
        if not isinstance(content, Mapping):
            return
        _extract_cursor(entry_id, content, cursors)
        if isinstance(content.get("itemContent"), Mapping):
            _extract_cursor(entry_id, content["itemContent"], cursors)
            _record(content["itemContent"], entry_id)
        if isinstance(content.get("items"), list):
            for mod_item in content["items"]:
                if not isinstance(mod_item, Mapping):
                    continue
                sub_id = str(mod_item.get("entryId") or entry_id)
                ic = _dig(mod_item, "item", "itemContent")
                if isinstance(ic, Mapping):
                    _extract_cursor(sub_id, ic, cursors)
                    _record(ic, sub_id)

    for inst_arr in instruction_lists:
        for inst in inst_arr:
            if not isinstance(inst, Mapping):
                continue
            if isinstance(inst.get("entries"), list):
                for entry in inst["entries"]:
                    _process_entry(entry)
            if isinstance(inst.get("entry"), Mapping):
                _process_entry(inst["entry"])
            if isinstance(inst.get("moduleItems"), list):
                for mod_item in inst["moduleItems"]:
                    ic = _dig(mod_item, "item", "itemContent")
                    if isinstance(ic, Mapping):
                        _record(ic, str(mod_item.get("entryId") or ""))

    out.top_cursor = cursors["top"]
    out.bottom_cursor = cursors["bottom"]
    return out


def parse_lists_graphql(root: Any) -> list[XListInfo]:
    """Recursively extract X List metadata from `ListsManagementPageTimeline` JSON."""
    if not isinstance(root, Mapping):
        return []
    out: list[XListInfo] = []
    seen: set[str] = set()

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 16:
            return
        if isinstance(node, Mapping):
            id_str = str(node.get("id_str") or "").strip()
            name = str(node.get("name") or "").strip()
            looks_like_list = (
                bool(id_str)
                and id_str.isdigit()
                and bool(name)
                and (
                    "member_count" in node
                    or "subscriber_count" in node
                    or "mode" in node
                    or node.get("__typename") == "TwitterList"
                )
            )
            if looks_like_list and id_str not in seen:
                seen.add(id_str)
                owner_handle = ""
                ures = _dig(node, "user_results", "result")
                if isinstance(ures, Mapping):
                    owner_handle = str(
                        _dig(ures, "core", "screen_name") or _dig(ures, "legacy", "screen_name") or ""
                    ).strip()
                out.append(
                    XListInfo(
                        id=id_str,
                        name=decode_html_entities(name),
                        description=decode_html_entities(str(node.get("description") or "").strip()),
                        owner_handle=owner_handle,
                        member_count=_read_int(node, "member_count", 0),
                        subscriber_count=_read_int(node, "subscriber_count", 0),
                        pinned=True,
                    )
                )
                return
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for elem in node:
                _walk(elem, depth + 1)

    _walk(root)
    return out


def _same_author(a: ParsedTweet, b: ParsedTweet) -> bool:
    if a.author_id and b.author_id:
        return a.author_id == b.author_id
    if a.author_handle and b.author_handle:
        return a.author_handle.lower() == b.author_handle.lower()
    return False


def _tweet_to_comment_node(
    tweet: ParsedTweet,
    *,
    is_op: bool = False,
) -> CommentNode:
    role_badge = "OP" if is_op else ("Verified" if tweet.author_verified else "")
    body_parts = [tweet.text] if tweet.text else []
    if tweet.quoted_text:
        qa = f"@{tweet.quoted_author_handle}" if tweet.quoted_author_handle else tweet.quoted_author_name
        body_parts.append(f"> [引用 {qa}] {tweet.quoted_text}")
    for m in tweet.media:
        body_parts.append(f"[{m.type}] {m.url}")
    return CommentNode(
        id=tweet.id,
        author=format_author_display(tweet),
        role_badge=role_badge,
        body="\n".join(body_parts).strip(),
        created_at=tweet.created_at,
        score=tweet.like_count,
    )


def _assemble_comment_tree(
    comment_order: list[str],
    comment_map: dict[str, CommentNode],
    parent_map: dict[str, str],
    thread_ids: set[str],
) -> list[CommentNode]:
    visited: set[str] = set()

    def _build_subtree(cid: str) -> CommentNode:
        node = comment_map[cid]
        visited.add(cid)
        replies: list[CommentNode] = []
        for child_id in comment_order:
            if child_id == cid or child_id in visited:
                continue
            if parent_map.get(child_id) == cid:
                replies.append(_build_subtree(child_id))
        return node.model_copy(update={"replies": replies})

    roots: list[CommentNode] = []
    for cid in comment_order:
        if cid in visited:
            continue
        pid = parent_map.get(cid, "")
        if not pid or pid in thread_ids or pid not in comment_map:
            roots.append(_build_subtree(cid))
    for cid in comment_order:
        if cid not in visited:
            roots.append(_build_subtree(cid))
    return roots


def parse_single_tweet_result_graphql(root: Any, fallback_id: str = "") -> ThreadParseResult:
    """Parse single `TweetResultByRestId` GraphQL payload."""
    if not isinstance(root, Mapping):
        raise XPluginError("parse_error", "TweetResult 响应不是有效 JSON 对象")
    res = ThreadParseResult()
    node = _dig(root, "data", "tweetResult", "result") or _dig(root, "data", "tweet_result", "result")
    if node is None and ("rest_id" in root or "legacy" in root):
        node = root
    if not isinstance(node, Mapping):
        return res
    t = parse_tweet_result_node(node)
    if t is None:
        return res
    if not t.id:
        t.id = fallback_id
    res.focal_tweet = t
    res.thread_tweets = [t]
    res.article = build_article_from_thread(res.thread_tweets, [], t.id)
    return res


def parse_tweet_detail_graphql(root: Any, focal_tweet_id: str = "") -> ThreadParseResult:
    """Parse `TweetDetail` GraphQL response into unrolled OP thread + nested comment tree."""
    if not isinstance(root, Mapping):
        raise XPluginError("parse_error", "TweetDetail 响应不是有效 JSON 对象")
    instruction_lists: list[list[Any]] = []
    _find_instructions_arrays(root, instruction_lists)

    @dataclass(slots=True)
    class _RawRow:
        tweet: ParsedTweet
        in_module: bool = False
        module_id: str = ""
        prev_in_module_id: str = ""

    ordered: list[_RawRow] = []
    seen_ids: set[str] = set()
    cursors = {"top": "", "bottom": "", "show_more": ""}
    top_level_tweet_id = ""

    def _record_item(
        ic: Any,
        entry_id: str,
        in_module: bool,
        mod_id: str,
        prev_mod_tid: str,
    ) -> str:
        nonlocal top_level_tweet_id
        if _is_promoted_item(entry_id, ic):
            return prev_mod_tid
        if not isinstance(ic, Mapping) or "tweet_results" not in ic:
            return prev_mod_tid
        t = parse_tweet_result_node(ic["tweet_results"])
        if t is None or not t.id:
            return prev_mod_tid
        if t.id in seen_ids:
            return t.id
        seen_ids.add(t.id)
        if not in_module and not top_level_tweet_id:
            top_level_tweet_id = t.id
        ordered.append(_RawRow(tweet=t, in_module=in_module, module_id=mod_id, prev_in_module_id=prev_mod_tid))
        return t.id

    def _process_entry(entry: Any) -> None:
        if not isinstance(entry, Mapping):
            return
        entry_id = str(entry.get("entryId") or "")
        content = entry.get("content")
        if not isinstance(content, Mapping):
            return
        _extract_cursor(entry_id, content, cursors)
        is_conv_mod = "conversationthread" in entry_id.lower() or isinstance(content.get("items"), list)
        if isinstance(content.get("itemContent"), Mapping):
            _extract_cursor(entry_id, content["itemContent"], cursors)
            _record_item(content["itemContent"], entry_id, False, "", "")
        if isinstance(content.get("items"), list):
            prev_tid = ""
            for mod_item in content["items"]:
                if not isinstance(mod_item, Mapping):
                    continue
                sub_id = str(mod_item.get("entryId") or entry_id)
                ic = _dig(mod_item, "item", "itemContent")
                if isinstance(ic, Mapping):
                    _extract_cursor(sub_id, ic, cursors)
                    prev_tid = _record_item(ic, sub_id, is_conv_mod, entry_id, prev_tid)

    for inst_arr in instruction_lists:
        for inst in inst_arr:
            if not isinstance(inst, Mapping):
                continue
            if isinstance(inst.get("entries"), list):
                for entry in inst["entries"]:
                    _process_entry(entry)
            if isinstance(inst.get("entry"), Mapping):
                _process_entry(inst["entry"])
            if isinstance(inst.get("moduleItems"), list):
                prev_tid = ""
                for mod_item in inst["moduleItems"]:
                    if not isinstance(mod_item, Mapping):
                        continue
                    sub_id = str(mod_item.get("entryId") or "")
                    ic = _dig(mod_item, "item", "itemContent")
                    if isinstance(ic, Mapping):
                        _extract_cursor(sub_id, ic, cursors)
                        prev_tid = _record_item(ic, sub_id, True, "module", prev_tid)

    res = ThreadParseResult()
    res.next_cursor = cursors["bottom"] or cursors["show_more"]
    res.has_more = bool(res.next_cursor)

    if not ordered:
        single = parse_single_tweet_result_graphql(root, focal_tweet_id)
        if single.article is not None:
            return single
        return res

    idx_by_id = {row.tweet.id: i for i, row in enumerate(ordered)}
    if focal_tweet_id and focal_tweet_id in idx_by_id:
        focal_idx = idx_by_id[focal_tweet_id]
    elif top_level_tweet_id and top_level_tweet_id in idx_by_id:
        focal_idx = idx_by_id[top_level_tweet_id]
    elif focal_tweet_id:
        comment_map: dict[str, CommentNode] = {}
        parent_map: dict[str, str] = {}
        comment_order: list[str] = []
        for row in ordered:
            t = row.tweet
            comment_map[t.id] = _tweet_to_comment_node(t, is_op=False)
            parent_map[t.id] = t.in_reply_to_tweet_id or row.prev_in_module_id
            comment_order.append(t.id)
        res.comments = _assemble_comment_tree(comment_order, comment_map, parent_map, set())
        return res
    else:
        focal_idx = 0

    res.focal_tweet = ordered[focal_idx].tweet

    ancestors: list[ParsedTweet] = []
    seen_anc: set[str] = set()
    cur_parent = res.focal_tweet.in_reply_to_tweet_id
    while cur_parent and cur_parent in idx_by_id and cur_parent not in seen_anc:
        seen_anc.add(cur_parent)
        pt = ordered[idx_by_id[cur_parent]].tweet
        ancestors.append(pt)
        cur_parent = pt.in_reply_to_tweet_id
    ancestors.reverse()
    if not ancestors and focal_idx > 0:
        for i in range(focal_idx):
            if not ordered[i].in_module:
                ancestors.append(ordered[i].tweet)

    thread_ids: set[str] = set()
    for t in ancestors:
        if t.id not in thread_ids:
            thread_ids.add(t.id)
            res.thread_tweets.append(t)
    if res.focal_tweet.id not in thread_ids:
        thread_ids.add(res.focal_tweet.id)
        res.thread_tweets.append(res.focal_tweet)

    thread_op = res.thread_tweets[0]
    extended = True
    while extended:
        extended = False
        tail_id = res.thread_tweets[-1].id
        for i in range(focal_idx + 1, len(ordered)):
            cand = ordered[i].tweet
            if cand.id in thread_ids:
                continue
            eff_parent = cand.in_reply_to_tweet_id or ordered[i].prev_in_module_id
            if eff_parent == tail_id and (_same_author(cand, res.focal_tweet) or _same_author(cand, thread_op)):
                thread_ids.add(cand.id)
                res.thread_tweets.append(cand)
                extended = True
                break

    comment_map = {}
    parent_map = {}
    comment_order = []
    for row in ordered:
        t = row.tweet
        if t.id in thread_ids:
            continue
        is_op = _same_author(t, res.focal_tweet) or _same_author(t, thread_op)
        comment_map[t.id] = _tweet_to_comment_node(t, is_op=is_op)
        p_id = t.in_reply_to_tweet_id
        if not p_id or (p_id not in comment_map and p_id not in thread_ids):
            if row.prev_in_module_id:
                p_id = row.prev_in_module_id
        parent_map[t.id] = p_id
        comment_order.append(t.id)

    res.comments = _assemble_comment_tree(comment_order, comment_map, parent_map, thread_ids)
    res.article = build_article_from_thread(res.thread_tweets, res.comments, res.focal_tweet.id)
    return res


def parse_syndication_tweet_json(root: Any, fallback_id: str = "") -> ThreadParseResult:
    """Parse public X Syndication JSON (`cdn.syndication.twimg.com/tweet-result`)."""
    if not isinstance(root, Mapping):
        raise XPluginError("parse_error", "Syndication 响应不是有效 JSON 对象")
    res = ThreadParseResult()
    tid = str(root.get("id_str") or fallback_id or "").strip()
    if not tid:
        return res

    user = root.get("user") if isinstance(root.get("user"), Mapping) else {}
    handle = str(user.get("screen_name") or "").strip()
    text = decode_html_entities(str(root.get("text") or "").strip())
    if not text and not root.get("mediaDetails") and not root.get("quoted_tweet"):
        return res

    urls = _dig(root, "entities", "urls")
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, Mapping):
                su = str(u.get("url") or "")
                eu = str(u.get("expanded_url") or "")
                if su and eu:
                    text = text.replace(su, eu)

    media: list[XMediaAttachment] = []
    if isinstance(root.get("mediaDetails"), list):
        for m in root["mediaDetails"]:
            if isinstance(m, Mapping):
                murl = str(m.get("media_url_https") or "")
                if murl:
                    media.append(
                        XMediaAttachment(
                            type=str(m.get("type") or "photo"),
                            url=murl,
                            poster_url=murl,
                        )
                    )

    qt = root.get("quoted_tweet") if isinstance(root.get("quoted_tweet"), Mapping) else {}
    qt_user = qt.get("user") if isinstance(qt.get("user"), Mapping) else {}

    t = ParsedTweet(
        id=tid,
        author_id=str(user.get("id_str") or "").strip(),
        author_name=decode_html_entities(str(user.get("name") or "").strip()),
        author_handle=handle,
        author_verified=bool(user.get("is_blue_verified", user.get("verified", False))),
        text=text,
        created_at=format_twitter_timestamp(root.get("created_at") or ""),
        like_count=_read_int(root, "favorite_count", 0),
        reply_count=_read_int(root, "conversation_count", 0),
        url=f"https://x.com/{handle}/status/{tid}" if handle else f"https://x.com/i/web/status/{tid}",
        media=media,
        quoted_tweet_id=str(qt.get("id_str") or "").strip(),
        quoted_text=decode_html_entities(str(qt.get("text") or "").strip()),
        quoted_author_name=str(qt_user.get("name") or "").strip(),
        quoted_author_handle=str(qt_user.get("screen_name") or "").strip(),
    )
    res.focal_tweet = t
    res.thread_tweets = [t]
    res.article = build_article_from_thread(res.thread_tweets, [], t.id)
    return res


class _XTweetHtmlParser(HTMLParser):
    """Stdlib HTMLParser extracting OpenGraph meta tags, rendered tweet text, and structure health."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.title_closed = False
        self.tweet_texts: list[str] = []
        self.user_names: list[str] = []
        self.published_times: list[str] = []
        self.noscript_parts: list[str] = []
        self.body_text_parts: list[str] = []
        self.tag_stack: list[str] = []
        self._in_title = False
        self._title_buf: list[str] = []
        self._ignored_depth = 0
        self._in_noscript = False
        self._tweet_depth = 0
        self._tweet_buf: list[str] = []
        self._user_depth = 0
        self._user_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t not in {"meta", "link", "br", "hr", "img", "input"}:
            self.tag_stack.append(t)

        if t == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or "").strip().lower()
            val = attr_map.get("content") or ""
            if key and val and key not in self.meta:
                self.meta[key] = decode_html_entities(val.strip())
            return
        if t == "noscript":
            self._in_noscript = True
            return
        if t in {"script", "style", "template", "iframe", "svg"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth > 0:
            return
        if t == "title":
            self._in_title = True
            self._title_buf = []
        if t == "time" and attr_map.get("datetime"):
            self.published_times.append(attr_map["datetime"].strip())
        if attr_map.get("data-testid") == "tweetText":
            self._tweet_depth = 1
            self._tweet_buf = []
        elif self._tweet_depth > 0:
            self._tweet_depth += 1
        if attr_map.get("data-testid") == "User-Name":
            self._user_depth = 1
            self._user_buf = []
        elif self._user_depth > 0:
            self._user_depth += 1
        if t == "br" and self._tweet_depth > 0:
            self._tweet_buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.tag_stack:
            for idx in range(len(self.tag_stack) - 1, -1, -1):
                if self.tag_stack[idx] == t:
                    del self.tag_stack[idx:]
                    break
        if t == "noscript":
            self._in_noscript = False
            return
        if t in {"script", "style", "template", "iframe", "svg"}:
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            return
        if self._ignored_depth > 0:
            return
        if t == "title" and self._in_title:
            self._in_title = False
            self.title_closed = True
            self.title = _collapse_ws("".join(self._title_buf))
        if self._tweet_depth > 0:
            self._tweet_depth -= 1
            if self._tweet_depth == 0:
                txt = "".join(self._tweet_buf).strip()
                if txt:
                    self.tweet_texts.append(txt)
        if self._user_depth > 0:
            self._user_depth -= 1
            if self._user_depth == 0:
                uname = _collapse_ws("".join(self._user_buf))
                if uname:
                    self.user_names.append(uname)

    def handle_data(self, data: str) -> None:
        if self._in_noscript:
            self.noscript_parts.append(data)
            return
        if self._ignored_depth > 0:
            return
        if self._in_title:
            self._title_buf.append(data)
            return
        self.body_text_parts.append(data)
        if self._tweet_depth > 0:
            self._tweet_buf.append(data)
        if self._user_depth > 0:
            self._user_buf.append(data)


def parse_html_fallback(raw_html: str, target: ParsedXTarget) -> ThreadParseResult:
    """Parse X rendered DOM or OpenGraph HTML fail-closed without regex scraping."""
    if not isinstance(raw_html, str) or not raw_html.strip():
        raise XPluginError("parse_error", "HTML 响应为空")
    if "\x00" in raw_html or "\ufffd\ufffd\ufffd" in raw_html:
        raise XPluginError("parse_error", "HTML 响应包含损坏或二进制控制字符")

    low = raw_html.lower()
    if any(marker in low for marker in _CLOUDFLARE_MARKERS):
        raise XPluginError("cloudflare_blocked", "触发 Cloudflare 人机验证，请通过 Chrome 完成校验")

    parser = _XTweetHtmlParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception as exc:
        raise XPluginError("parse_error", f"畸形 HTML 解析失败: {type(exc).__name__}") from None

    if parser._in_title and not parser.title_closed:
        raise XPluginError("parse_error", "畸形 HTML：title 标签未闭合")

    og_title = parser.meta.get("og:title", "").strip()
    og_desc = (parser.meta.get("og:description") or parser.meta.get("description") or "").strip()
    dom_text = "\n\n".join(parser.tweet_texts).strip()

    combined_diag = f"{parser.title} {og_title} {' '.join(parser.noscript_parts)} {' '.join(parser.body_text_parts)}".lower()
    if not dom_text and not og_desc:
        if any(m in combined_diag for m in _LOGIN_WALL_MARKERS):
            raise XPluginError("auth_required", "需要登录 X 才能查看该推文内容")
        if "javascript is not available" in combined_diag or "something went wrong" in combined_diag:
            raise XPluginError("parse_error", "页面仅返回 JS 壳或错误提示，未能提取推文正文")

    body_text = dom_text or og_desc
    if not body_text:
        if parser.title and parser.title.lower() not in _GENERIC_SHELL_TITLES and not parser.tag_stack:
            body_text = parser.title
        else:
            raise XPluginError("parse_error", "HTML 中缺少有效的推文正文或 OpenGraph 摘要")

    author_name = og_title or (parser.user_names[0] if parser.user_names else "")
    author_handle = target.user_handle
    if not author_handle and author_name:
        if m := re.search(r"@([A-Za-z0-9_]{1,30})", author_name):
            author_handle = m.group(1)

    tid = target.tweet_id or target.canonical_id
    if not tid:
        raise XPluginError("parse_error", "无法确定推文 ID")

    t = ParsedTweet(
        id=tid,
        author_handle=author_handle,
        author_name=author_name,
        text=body_text,
        created_at=format_twitter_timestamp(parser.published_times[0]) if parser.published_times else "",
        url=target.web_url or f"https://x.com/i/web/status/{tid}",
    )
    res = ThreadParseResult()
    res.focal_tweet = t
    res.thread_tweets = [t]
    res.article = build_article_from_thread(res.thread_tweets, [], t.id)
    return res


def is_valid_x_target(target: ParsedXTarget) -> bool:
    """Validate that a parsed target does not carry an unsafe scheme."""
    return not target.unsafe_scheme
