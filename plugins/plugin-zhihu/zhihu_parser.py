"""Zhihu HTML/DOM, js-initialData, API JSON, LaTeX formula, and nested comment tree parser."""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from omnidesk_sdk import CommentNode, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .zhihu_errors import ZhihuError, validate_segment_id, validate_zhihu_url
else:
    from zhihu_errors import ZhihuError, validate_segment_id, validate_zhihu_url  # type: ignore[no-redef]

_MAX_HTML_CHARS = 8 * 1024 * 1024
_MAX_DOM_DEPTH = 128
_MAX_COMMENT_DEPTH = 16
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LANG_CLASS_RE = re.compile(r"^(?:lang(?:uage)?-)([A-Za-z0-9_+#.-]+)$")
_QA_REGEX = re.compile(r"question[s]?/(\d+)/answer[s]?/(\d+)")
_ANS_REGEX = re.compile(r"answer[s]?/(\d+)")
_ART_REGEX = re.compile(r"(?:zhuanlan\.zhihu\.com/p|articles)/(\d+)")
_Q_REGEX = re.compile(r"question[s]?/(\d+)")
_ZHIHU_RICH_CLASSES = frozenset({"RichText", "Post-RichText", "QuestionAnswer-content", "AnswerItem", "Post-Main"})


@dataclass(frozen=True, slots=True)
class ParsedZhihuTarget:
    """Normalized Zhihu question / answer / article target identifier."""

    kind: str = "unknown"
    question_id: str = ""
    answer_id: str = ""
    article_id: str = ""
    canonical_id: str = ""
    web_url: str = ""


@dataclass
class CommentPageResult:
    """Paginated comment tree result matching v1 `text.comments.v1`."""

    comments: list[CommentNode] = field(default_factory=list)
    is_end: bool = True
    next_url: str = ""
    total: int = 0


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    """Summary metadata for a user's Zhihu collection."""

    id: str
    title: str
    item_count: int = 0


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[_Node] = field(default_factory=list)

    def classes(self) -> set[str]:
        raw = self.attrs.get("class", "")
        return set(raw.split()) if raw else set()

    def has_class(self, cls: str) -> bool:
        return cls in self.classes()


class _DomBuilder(HTMLParser):
    """Depth-bounded stdlib HTMLParser resilient to malformed HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="#root")
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self._stack) >= _MAX_DOM_DEPTH:
            return
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        node = _Node(tag=t, attrs=attr_map)
        self._stack[-1].children.append(node)
        if t not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self._stack) >= _MAX_DOM_DEPTH:
            return
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self._stack[-1].children.append(_Node(tag=t, attrs=attr_map))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(_Node(tag="#text", text=data))


def _parse_dom(html_text: str) -> _Node:
    builder = _DomBuilder()
    builder.feed(html_text[:_MAX_HTML_CHARS])
    builder.close()
    return builder.root


def _collapse_spaces(s: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", s.replace("\r", "")).strip()


def _read_id_str(obj: Mapping[str, Any], key: str) -> str:
    val = obj.get(key)
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    return ""


def _read_int(obj: Mapping[str, Any], key: str, fallback: int = 0) -> int:
    val = obj.get(key)
    if isinstance(val, bool):
        return fallback
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return fallback
    return fallback


def _read_str(obj: Mapping[str, Any], key: str, fallback: str = "") -> str:
    val = obj.get(key)
    return val if isinstance(val, str) else fallback


def _read_bool(obj: Mapping[str, Any], key: str, fallback: bool = False) -> bool:
    val = obj.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    return fallback


def decode_html_entities(s: str) -> str:
    """Decode HTML character references cleanly."""
    return html_lib.unescape(s) if s else ""


def format_timestamp(epoch_sec: int) -> str:
    """Format UNIX timestamp in seconds to local `YYYY-MM-DD HH:MM`."""
    if epoch_sec <= 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch_sec).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def parse_article_id_or_url(
    id_or_url: str,
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> ParsedZhihuTarget:
    """Parse canonical ID (`question:..`, `answer:..`, `article:..`) or Zhihu HTTPS URL."""
    s = (id_or_url or "").strip()
    if not s:
        return ParsedZhihuTarget(kind="unknown")

    if s.startswith("question:"):
        qid = validate_segment_id(s[9:], field="question_id")
        if not qid.isdigit():
            raise ZhihuError("invalid_argument", "question_id 必须为数字")
        return ParsedZhihuTarget(
            kind="question",
            question_id=qid,
            canonical_id=f"question:{qid}",
            web_url=f"https://www.zhihu.com/question/{qid}",
        )

    if s.startswith("answer:"):
        rest = s[7:].strip()
        if ":" in rest:
            qid_raw, aid_raw = rest.split(":", 1)
            qid = validate_segment_id(qid_raw, field="question_id")
            aid = validate_segment_id(aid_raw, field="answer_id")
            if not qid.isdigit() or not aid.isdigit():
                raise ZhihuError("invalid_argument", "question_id / answer_id 必须为数字")
            return ParsedZhihuTarget(
                kind="answer",
                question_id=qid,
                answer_id=aid,
                canonical_id=f"answer:{qid}:{aid}",
                web_url=f"https://www.zhihu.com/question/{qid}/answer/{aid}",
            )
        aid = validate_segment_id(rest, field="answer_id")
        if not aid.isdigit():
            raise ZhihuError("invalid_argument", "answer_id 必须为数字")
        return ParsedZhihuTarget(
            kind="answer",
            answer_id=aid,
            canonical_id=f"answer:{aid}",
            web_url=f"https://www.zhihu.com/answer/{aid}",
        )

    if s.startswith("article:"):
        pid = validate_segment_id(s[8:], field="article_id")
        if not pid.isdigit():
            raise ZhihuError("invalid_argument", "article_id 必须为数字")
        return ParsedZhihuTarget(
            kind="article",
            article_id=pid,
            canonical_id=f"article:{pid}",
            web_url=f"https://zhuanlan.zhihu.com/p/{pid}",
        )

    low = s.lower()
    if "://" in low or low.startswith("//"):
        candidate = f"https:{s}" if low.startswith("//") else s
        safe_url = validate_zhihu_url(
            candidate,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        s = safe_url

    if m := _QA_REGEX.search(s):
        qid, aid = m.group(1), m.group(2)
        return ParsedZhihuTarget(
            kind="answer",
            question_id=qid,
            answer_id=aid,
            canonical_id=f"answer:{qid}:{aid}",
            web_url=f"https://www.zhihu.com/question/{qid}/answer/{aid}",
        )
    if m := _ANS_REGEX.search(s):
        aid = m.group(1)
        return ParsedZhihuTarget(
            kind="answer",
            answer_id=aid,
            canonical_id=f"answer:{aid}",
            web_url=f"https://www.zhihu.com/answer/{aid}",
        )
    if m := _ART_REGEX.search(s):
        pid = m.group(1)
        return ParsedZhihuTarget(
            kind="article",
            article_id=pid,
            canonical_id=f"article:{pid}",
            web_url=f"https://zhuanlan.zhihu.com/p/{pid}",
        )
    if m := _Q_REGEX.search(s):
        qid = m.group(1)
        return ParsedZhihuTarget(
            kind="question",
            question_id=qid,
            canonical_id=f"question:{qid}",
            web_url=f"https://www.zhihu.com/question/{qid}",
        )

    return ParsedZhihuTarget(
        kind="unknown",
        canonical_id=s,
        web_url=s if s.lower().startswith("https://") else "",
    )


def _find_by_id(node: _Node, target_id: str) -> _Node | None:
    if node.attrs.get("id") == target_id:
        return node
    for ch in node.children:
        if hit := _find_by_id(ch, target_id):
            return hit
    return None


def _find_by_class(node: _Node, cls_set: frozenset[str] | set[str]) -> _Node | None:
    if node.classes() & cls_set:
        return node
    for ch in node.children:
        if hit := _find_by_class(ch, cls_set):
            return hit
    return None


def _find_first_tag(node: _Node, tag_name: str) -> _Node | None:
    if node.tag == tag_name:
        return node
    for ch in node.children:
        if hit := _find_first_tag(ch, tag_name):
            return hit
    return None


def _collect_raw_text(node: _Node) -> str:
    if node.tag == "#text":
        return node.text
    if node.tag == "br":
        return "\n"
    return "".join(_collect_raw_text(ch) for ch in node.children)


def _extract_code_language(pre_node: _Node) -> str:
    for ch in pre_node.children:
        if ch.tag == "code":
            for cls in ch.classes():
                if m := _LANG_CLASS_RE.match(cls):
                    return m.group(1).lower()
    return ""


def _extract_formula(node: _Node) -> str:
    if node.tag == "img":
        df = (node.attrs.get("data-formula") or node.attrs.get("data-tex") or "").strip()
        if df:
            return df
    if node.tag == "span" and node.has_class("ztext-math"):
        tex = node.attrs.get("data-tex", "").strip()
        if tex:
            return tex
        txt = _collect_raw_text(node).strip()
        if len(txt) >= 4 and txt.startswith("$$") and txt.endswith("$$"):
            return txt[2:-2].strip()
        if len(txt) >= 2 and txt.startswith("$") and txt.endswith("$"):
            return txt[1:-1].strip()
        return txt
    return ""


def _collect_inline(
    node: _Node,
    out_parts: list[str],
    out_formulas: list[str],
) -> bool:
    """Collect inline text and `$formula$` tokens, returning True if non-formula prose was seen."""
    if node.tag == "#text":
        if node.text.strip():
            out_parts.append(node.text)
            return True
        out_parts.append(node.text)
        return False
    if node.tag in {"script", "style", "noscript"}:
        return False
    if node.tag == "br":
        out_parts.append("\n")
        return False

    formula = _extract_formula(node)
    if formula:
        out_formulas.append(formula)
        out_parts.append(f"${formula}$")
        return False

    if node.tag == "img":
        alt = node.attrs.get("alt", "").strip()
        if alt and alt != "[公式]":
            out_parts.append(f"[{alt}]")
            return True
        return False

    has_prose = False
    for ch in node.children:
        if _collect_inline(ch, out_parts, out_formulas):
            has_prose = True
    return has_prose


def _emit_paragraph(
    out: list[TextParagraph],
    id_prefix: str,
    kind: str,
    text: str,
    *,
    language: str = "",
) -> None:
    cleaned = text.strip() if kind == "code" else _collapse_spaces(text)
    if not cleaned:
        return
    out.append(
        TextParagraph(
            id=f"{id_prefix}-{len(out) + 1}",
            kind=kind,  # type: ignore[arg-type]
            original=cleaned,
            language=language,
        )
    )


def _walk_rich_nodes(node: _Node, out: list[TextParagraph], id_prefix: str) -> None:
    if node.tag == "#text":
        cleaned = _collapse_spaces(node.text)
        if cleaned:
            _emit_paragraph(out, id_prefix, "paragraph", cleaned)
        return

    if node.tag in {"script", "style", "noscript"}:
        return

    direct_formula = _extract_formula(node)
    if direct_formula:
        _emit_paragraph(out, id_prefix, "formula", direct_formula)
        return

    if node.tag == "img":
        for key in ("data-original", "data-actualsrc", "data-src", "src"):
            url = decode_html_entities(node.attrs.get(key, "")).strip()
            if url.startswith("//"):
                url = "https:" + url
            try:
                parsed = urllib.parse.urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                continue
            alt = node.attrs.get("alt", "").strip()
            out.append(TextParagraph(id=f"{id_prefix}-{len(out) + 1}", kind="image", image_url=url, alt=alt))
            break
        return

    # Keep prose on either side of inline images in its original order.
    if node.tag in {"p", "blockquote"} and _has_content_image(node):
        parts: list[str] = []
        kind = "quote" if node.tag == "blockquote" else "paragraph"

        def flush() -> None:
            _emit_paragraph(out, id_prefix, kind, "".join(parts))
            parts.clear()

        def visit(child: _Node) -> None:
            if child.tag in {"script", "style", "noscript"}:
                return
            if child.tag == "img" and not _extract_formula(child):
                flush()
                _walk_rich_nodes(child, out, id_prefix)
            elif _has_content_image(child):
                for nested in child.children:
                    visit(nested)
            else:
                _collect_inline(child, parts, [])

        for child in node.children:
            visit(child)
        flush()
        return

    if node.tag in _HEADING_TAGS:
        parts: list[str] = []
        formulas: list[str] = []
        _collect_inline(node, parts, formulas)
        _emit_paragraph(out, id_prefix, "heading", "".join(parts))
        return

    if node.tag == "pre":
        lang = _extract_code_language(node)
        _emit_paragraph(out, id_prefix, "code", _collect_raw_text(node), language=lang)
        return

    if node.tag == "blockquote":
        parts = []
        formulas = []
        has_prose = _collect_inline(node, parts, formulas)
        if not has_prose and formulas:
            for f in formulas:
                _emit_paragraph(out, id_prefix, "formula", f)
        else:
            _emit_paragraph(out, id_prefix, "quote", "".join(parts))
        return

    if node.tag == "p":
        parts = []
        formulas = []
        has_prose = _collect_inline(node, parts, formulas)
        if not has_prose and formulas:
            for f in formulas:
                _emit_paragraph(out, id_prefix, "formula", f)
        else:
            _emit_paragraph(out, id_prefix, "paragraph", "".join(parts))
        return

    if node.tag == "figure":
        caption = " ".join(_collapse_spaces(_collect_raw_text(ch)) for ch in node.children if ch.tag == "figcaption").strip()
        start = len(out)
        for ch in node.children:
            if ch.tag != "figcaption":
                _walk_rich_nodes(ch, out, id_prefix)
        images = [p for p in out[start:] if p.kind == "image"]
        if images:
            images[-1].caption = caption
        elif caption:
            _emit_paragraph(out, id_prefix, "paragraph", f"[图注] {caption}")
        return

    if node.tag in {"ul", "ol"}:
        ordered = node.tag == "ol"
        idx = 1
        for ch in node.children:
            if ch.tag != "li":
                continue
            parts = []
            formulas = []
            _collect_inline(ch, parts, formulas)
            cleaned = _collapse_spaces("".join(parts))
            if cleaned:
                prefix = f"{idx}. " if ordered else "• "
                if ordered:
                    idx += 1
                _emit_paragraph(out, id_prefix, "paragraph", prefix + cleaned)
        return

    for ch in node.children:
        _walk_rich_nodes(ch, out, id_prefix)


def _has_content_image(node: _Node) -> bool:
    if node.tag in {"script", "style", "noscript"}:
        return False
    return (node.tag == "img" and not _extract_formula(node)) or any(_has_content_image(ch) for ch in node.children)


def extract_initial_data(html_text: str) -> dict[str, Any] | None:
    """Extract and parse `<script id="js-initialData">` JSON payload from Zhihu HTML."""
    if not html_text or not html_text.strip():
        return None
    root = _parse_dom(html_text)
    script_node = _find_by_id(root, "js-initialData")
    raw_json = _collect_raw_text(script_node).strip() if script_node is not None else ""

    if not raw_json:
        m = re.search(
            r"""id\s*=\s*(?:"js-initialData"|'js-initialData')[^>]*>(.*?)</script>""",
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            raw_json = m.group(1).strip()

    if not raw_json:
        return None
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_rich_html_to_paragraphs(
    html_text: str, id_prefix: str = "p"
) -> list[TextParagraph]:
    """Parse Zhihu rich HTML into structured `TextParagraph` blocks preserving code & LaTeX."""
    if not html_text or not html_text.strip():
        return []
    root = _parse_dom(html_text)
    body = _find_first_tag(root, "body") or root
    out: list[TextParagraph] = []
    for ch in body.children:
        _walk_rich_nodes(ch, out, id_prefix)
    return out


def html_to_clean_text(html_text: str) -> str:
    """Convert inline HTML (e.g. titles, excerpts, comments) to clean text preserving `$formula$`."""
    if not html_text or not html_text.strip():
        return ""
    if "<" not in html_text:
        return _collapse_spaces(decode_html_entities(html_text))
    root = _parse_dom(html_text)
    body = _find_first_tag(root, "body") or root
    parts: list[str] = []
    formulas: list[str] = []
    _collect_inline(body, parts, formulas)
    return _collapse_spaces("".join(parts))


def _resolve_initial_state(initial_data: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(initial_data, Mapping):
        return None
    inner = initial_data.get("initialState")
    return inner if isinstance(inner, Mapping) else initial_data


def _read_author_name(obj: Mapping[str, Any]) -> str:
    author = obj.get("author")
    if isinstance(author, Mapping):
        member = author.get("member")
        if isinstance(member, Mapping):
            name = _read_str(member, "name").strip()
            if name:
                return name
        name = _read_str(author, "name").strip()
        if name:
            return name
    actors = obj.get("actors")
    if isinstance(actors, list) and actors:
        first = actors[0]
        if isinstance(first, Mapping):
            name = _read_str(first, "name").strip()
            if name:
                return name
    return ""


def _normalize_zhihu_web_url(
    raw_url: str, item_type: str, item_id: str, qid: str = ""
) -> str:
    if raw_url:
        if any(
            seg in raw_url
            for seg in ("api.zhihu.com/questions/", "api.zhihu.com/answers/", "api.zhihu.com/articles/")
        ):
            try:
                target = parse_article_id_or_url(raw_url)
                if target.web_url:
                    return target.web_url
            except ZhihuError:
                pass
        if raw_url.startswith("https://"):
            return raw_url
        if raw_url.startswith("http://"):
            return "https://" + raw_url[7:]
    if item_type == "question" and item_id:
        return f"https://www.zhihu.com/question/{item_id}"
    if item_type == "answer" and item_id:
        if qid:
            return f"https://www.zhihu.com/question/{qid}/answer/{item_id}"
        return f"https://www.zhihu.com/answer/{item_id}"
    if item_type == "article" and item_id:
        return f"https://zhuanlan.zhihu.com/p/{item_id}"
    return "https://www.zhihu.com/"


def _parse_heat_score(detail_text: str) -> int:
    m = re.search(r"(\d+(?:\.\d+)?)", detail_text or "")
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0
    if "万" in (detail_text or ""):
        val *= 10000.0
    return int(val)


def _extract_topic_tags(obj: Mapping[str, Any]) -> list[str]:
    topics = obj.get("topics")
    if not isinstance(topics, list):
        return []
    out: list[str] = []
    for tp in topics:
        if isinstance(tp, Mapping):
            name = _read_str(tp, "name").strip()
            if name:
                out.append(name)
    return out


def _build_feed_item_from_target(
    target: Mapping[str, Any],
    channel_tag: str = "",
    extra_detail: str = "",
    *,
    channel_id: str = "",
) -> TextFeedItem | None:
    item_type = _read_str(target, "type").strip()
    tid = _read_id_str(target, "id")
    q_obj = target.get("question")
    qid = _read_id_str(q_obj, "id") if isinstance(q_obj, Mapping) else ""

    if item_type == "answer":
        source_name = "知乎问答"
        canonical_id = f"answer:{qid}:{tid}" if qid else (f"answer:{tid}" if tid else "")
        raw_title = _read_str(q_obj, "title") if isinstance(q_obj, Mapping) else _read_str(target, "title")
        title = html_to_clean_text(raw_title)
        url = _normalize_zhihu_web_url(_read_str(target, "url"), "answer", tid, qid)
    elif item_type == "article":
        source_name = "知乎专栏"
        canonical_id = f"article:{tid}" if tid else ""
        title = html_to_clean_text(_read_str(target, "title"))
        url = _normalize_zhihu_web_url(_read_str(target, "url"), "article", tid)
    elif item_type == "question":
        source_name = "知乎热榜"
        canonical_id = f"question:{tid}" if tid else ""
        title = html_to_clean_text(_read_str(target, "title"))
        url = _normalize_zhihu_web_url(_read_str(target, "url"), "question", tid)
    else:
        source_name = "知乎"
        canonical_id = f"{item_type}:{tid}" if (item_type and tid) else tid
        title = html_to_clean_text(_read_str(target, "title"))
        raw_u = _read_str(target, "url")
        url = _normalize_zhihu_web_url(raw_u, item_type, tid, qid)

    if not canonical_id or not title:
        return None

    excerpt = html_to_clean_text(_read_str(target, "excerpt") or _read_str(target, "excerpt_new"))
    if not excerpt and extra_detail:
        excerpt = extra_detail

    author = _read_author_name(target)
    if not author and item_type == "question":
        ans_cnt = _read_int(target, "answer_count", 0)
        if ans_cnt > 0:
            author = f"{ans_cnt} 个回答"

    ts = _read_int(target, "created_time", _read_int(target, "created", _read_int(target, "updated_time", 0)))
    published_at = format_timestamp(ts) if ts > 0 else ""
    comment_count = max(0, _read_int(target, "comment_count", 0))
    score = _read_int(target, "voteup_count", 0)
    if score == 0 and extra_detail:
        score = _parse_heat_score(extra_detail)

    tags: list[str] = []
    if channel_tag:
        tags.append(channel_tag)
    if extra_detail:
        tags.append(extra_detail)
    tags.extend(_extract_topic_tags(target))

    return TextFeedItem(
        id=canonical_id,
        source_id="plugin-zhihu",
        source_name=source_name,
        category="zhihu",
        channel_id=channel_id,
        title=title,
        excerpt=excerpt,
        author=author,
        published_at=published_at,
        url=url,
        tags=tags,
        comment_count=comment_count,
        score=score,
    )


def parse_hot_list_json(j: Any) -> list[TextFeedItem]:
    """Parse `/api/v3/feed/topstory/hot-lists/total` JSON payload."""
    if not isinstance(j, Mapping) or not isinstance(j.get("data"), list):
        return []
    items: list[TextFeedItem] = []
    rank = 1
    for entry in j["data"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("target"), Mapping):
            continue
        detail = _read_str(entry, "detail_text")
        item = _build_feed_item_from_target(
            entry["target"], f"热榜 #{rank}", detail, channel_id="hot"
        )
        rank += 1
        if item is not None:
            items.append(item)
    return items


def parse_hot_list_initial_data(initial_data: Any) -> list[TextFeedItem]:
    """Parse `js-initialData.initialState.topstory.hotList` HTML fallback."""
    state = _resolve_initial_state(initial_data)
    if not state or not isinstance(state.get("topstory"), Mapping):
        return []
    hot_list = state["topstory"].get("hotList")
    if not isinstance(hot_list, list):
        return []

    items: list[TextFeedItem] = []
    rank = 1
    for entry in hot_list:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("target"), Mapping):
            continue
        t = entry["target"]
        title_area = t.get("titleArea")
        title = html_to_clean_text(_read_str(title_area, "text")) if isinstance(title_area, Mapping) else ""
        if not title:
            continue
        excerpt_area = t.get("excerptArea")
        excerpt = html_to_clean_text(_read_str(excerpt_area, "text")) if isinstance(excerpt_area, Mapping) else ""
        metrics_area = t.get("metricsArea")
        metrics = _read_str(metrics_area, "text") if isinstance(metrics_area, Mapping) else ""
        link = t.get("link")
        raw_url = _read_str(link, "url") if isinstance(link, Mapping) else ""
        try:
            parsed_target = parse_article_id_or_url(raw_url)
        except ZhihuError:
            parsed_target = ParsedZhihuTarget()
        cid = parsed_target.canonical_id or f"hot:{rank}"
        url = parsed_target.web_url or (raw_url if raw_url.startswith("https://") else "")
        tags = [f"热榜 #{rank}"]
        if metrics:
            tags.append(metrics)
        rank += 1
        items.append(
            TextFeedItem(
                id=cid,
                source_id="plugin-zhihu",
                source_name="知乎热榜",
                category="zhihu",
                channel_id="hot",
                title=title,
                excerpt=excerpt,
                url=url,
                score=_parse_heat_score(metrics),
                tags=tags,
            )
        )
    return items


def parse_topstory_feed_json(
    j: Any, channel_tag: str = "", *, channel_id: str = ""
) -> list[TextFeedItem]:
    """Parse recommend / follow / moments JSON feed payload."""
    if not isinstance(j, Mapping) or not isinstance(j.get("data"), list):
        return []
    items: list[TextFeedItem] = []
    for entry in j["data"]:
        if not isinstance(entry, Mapping):
            continue
        target = entry.get("target")
        if not isinstance(target, Mapping):
            target = entry if "type" in entry else None
        if not isinstance(target, Mapping):
            continue
        action_text = _read_str(entry, "action_text")
        item = _build_feed_item_from_target(
            target, channel_tag, action_text, channel_id=channel_id
        )
        if item is not None:
            if not item.author:
                item = item.model_copy(update={"author": _read_author_name(entry)})
            items.append(item)
    return items


def parse_topstory_initial_data(
    initial_data: Any, channel_tag: str = "", *, channel_id: str = "recommend"
) -> list[TextFeedItem]:
    """Parse homepage `js-initialData.initialState.entities` answers/articles."""
    state = _resolve_initial_state(initial_data)
    if not state or not isinstance(state.get("entities"), Mapping):
        return []
    entities = state["entities"]
    items: list[TextFeedItem] = []
    for bucket_name in ("answers", "articles"):
        bucket = entities.get(bucket_name)
        if not isinstance(bucket, Mapping):
            continue
        for obj in bucket.values():
            if not isinstance(obj, Mapping):
                continue
            item = _build_feed_item_from_target(obj, channel_tag, channel_id=channel_id)
            if item is not None:
                items.append(item)
    return items


def parse_collections_list_json(j: Any) -> list[CollectionSummary]:
    """Parse `/api/v4/people/.../collections` response."""
    if not isinstance(j, Mapping) or not isinstance(j.get("data"), list):
        return []
    out: list[CollectionSummary] = []
    for entry in j["data"]:
        if not isinstance(entry, Mapping):
            continue
        cid = _read_id_str(entry, "id")
        title = _read_str(entry, "title").strip()
        count = max(0, _read_int(entry, "item_count", _read_int(entry, "answer_count", 0)))
        if cid and title:
            out.append(CollectionSummary(id=cid, title=title, item_count=count))
    return out


def parse_collection_items_json(
    j: Any, collection_title: str = "", *, channel_id: str = "collections"
) -> list[TextFeedItem]:
    """Parse `/api/v4/collections/{id}/items` response."""
    if not isinstance(j, Mapping) or not isinstance(j.get("data"), list):
        return []
    tag = f"收藏夹: {collection_title}" if collection_title else "收藏夹"
    items: list[TextFeedItem] = []
    for entry in j["data"]:
        if not isinstance(entry, Mapping):
            continue
        target = entry.get("content") if isinstance(entry.get("content"), Mapping) else entry.get("target")
        if not isinstance(target, Mapping):
            continue
        item = _build_feed_item_from_target(target, tag, channel_id=channel_id)
        if item is not None:
            items.append(item)
    return items


def parse_search_json(j: Any) -> list[TextFeedItem]:
    """Parse `/api/v4/search_v3` response."""
    if not isinstance(j, Mapping) or not isinstance(j.get("data"), list):
        return []
    items: list[TextFeedItem] = []
    for entry in j["data"]:
        if not isinstance(entry, Mapping):
            continue
        target = entry.get("object") if isinstance(entry.get("object"), Mapping) else entry.get("target")
        if not isinstance(target, Mapping):
            continue
        item = _build_feed_item_from_target(target, "搜索结果", channel_id="search")
        if item is not None:
            items.append(item)
    return items


def parse_answer_api_json(
    answer_json: Any, fallback_qid: str = ""
) -> TextArticle | None:
    """Parse Zhihu answer JSON (from API v4 or `initialState.entities.answers`)."""
    if not isinstance(answer_json, Mapping):
        return None
    aid = _read_id_str(answer_json, "id")
    if not aid:
        return None
    qid = fallback_qid
    q_title = ""
    q_obj = answer_json.get("question")
    if isinstance(q_obj, Mapping):
        if not qid:
            qid = _read_id_str(q_obj, "id")
        q_title = html_to_clean_text(_read_str(q_obj, "title"))

    canonical_id = f"answer:{qid}:{aid}" if qid else f"answer:{aid}"
    title = q_title or html_to_clean_text(_read_str(answer_json, "title")) or f"知乎回答 #{aid}"
    authors = _read_author_name(answer_json) or "知乎用户"
    ts = _read_int(
        answer_json,
        "updated_time",
        _read_int(answer_json, "updatedTime", _read_int(answer_json, "created_time", _read_int(answer_json, "createdTime", 0))),
    )
    published_at = format_timestamp(ts)
    votes = _read_int(answer_json, "voteup_count", _read_int(answer_json, "voteupCount", 0))
    subtitle = f"知乎回答 · {authors} · ▲ {votes}"
    if published_at:
        subtitle += f" · {published_at}"
    paragraphs = parse_rich_html_to_paragraphs(_read_str(answer_json, "content"), "ans")
    return TextArticle(
        id=canonical_id,
        source_id="plugin-zhihu",
        source_name="知乎问答",
        category="zhihu",
        title=title,
        subtitle=subtitle,
        authors=authors,
        published_at=published_at,
        url=_normalize_zhihu_web_url("", "answer", aid, qid),
        paragraphs=paragraphs,
    )


def parse_zhuanlan_api_json(article_json: Any) -> TextArticle | None:
    """Parse Zhihu Zhuanlan article JSON (from API v4 or `initialState.entities.articles`)."""
    if not isinstance(article_json, Mapping):
        return None
    pid = _read_id_str(article_json, "id")
    title = html_to_clean_text(_read_str(article_json, "title"))
    if not pid or not title:
        return None
    authors = _read_author_name(article_json) or "专栏作者"
    ts = _read_int(
        article_json, "updated", _read_int(article_json, "created", _read_int(article_json, "updated_time", 0))
    )
    published_at = format_timestamp(ts)
    votes = _read_int(article_json, "voteup_count", _read_int(article_json, "voteupCount", 0))
    col_obj = article_json.get("column")
    col_title = _read_str(col_obj, "title") if isinstance(col_obj, Mapping) else ""
    subtitle = f"{col_title or '知乎专栏'} · {authors} · ▲ {votes}"
    if published_at:
        subtitle += f" · {published_at}"
    paragraphs = parse_rich_html_to_paragraphs(_read_str(article_json, "content"), "art")
    return TextArticle(
        id=f"article:{pid}",
        source_id="plugin-zhihu",
        source_name="知乎专栏",
        category="zhihu",
        title=title,
        subtitle=subtitle,
        authors=authors,
        published_at=published_at,
        url=_normalize_zhihu_web_url("", "article", pid),
        tags=_extract_topic_tags(article_json),
        paragraphs=paragraphs,
    )


def parse_question_with_answers_api_json(
    question_json: Any, answers_json: Any
) -> TextArticle | None:
    """Parse Zhihu question + top answers into a unified `TextArticle`."""
    if not isinstance(question_json, Mapping):
        return None
    qid = _read_id_str(question_json, "id")
    title = html_to_clean_text(_read_str(question_json, "title"))
    if not qid or not title:
        return None
    ans_count = _read_int(question_json, "answer_count", _read_int(question_json, "answerCount", 0))
    follower_count = _read_int(
        question_json, "follower_count", _read_int(question_json, "followerCount", 0)
    )
    published_at = format_timestamp(_read_int(question_json, "created", 0))
    subtitle = f"知乎问题 · {ans_count} 个回答 · {follower_count} 关注"

    paragraphs: list[TextParagraph] = []
    q_paras = parse_rich_html_to_paragraphs(_read_str(question_json, "detail"), "q")
    if q_paras:
        paragraphs.append(TextParagraph(id="q-hdr", kind="heading", original="问题描述"))
        for p in q_paras:
            paragraphs.append(p.model_copy(update={"kind": "quote"}) if p.kind == "paragraph" else p)

    first_author = ""
    if isinstance(answers_json, Mapping) and isinstance(answers_json.get("data"), list):
        for idx, ans in enumerate(answers_json["data"], start=1):
            if not isinstance(ans, Mapping):
                continue
            author = _read_author_name(ans) or "知乎用户"
            if idx == 1:
                first_author = author
            votes = _read_int(ans, "voteup_count", _read_int(ans, "voteupCount", 0))
            paragraphs.append(
                TextParagraph(
                    id=f"ans-{idx}-hdr",
                    kind="heading",
                    original=f"回答 {idx} · {author} (▲ {votes})",
                )
            )
            paragraphs.extend(parse_rich_html_to_paragraphs(_read_str(ans, "content"), f"a{idx}"))

    return TextArticle(
        id=f"question:{qid}",
        source_id="plugin-zhihu",
        source_name="知乎问答",
        category="zhihu",
        title=title,
        subtitle=subtitle,
        authors=first_author,
        published_at=published_at,
        url=_normalize_zhihu_web_url("", "question", qid),
        tags=_extract_topic_tags(question_json),
        paragraphs=paragraphs,
    )


def _detect_comment_role_badge(c: Mapping[str, Any], content_author_name: str) -> str:
    for key in ("author_tag", "author_tags"):
        tags = c.get(key)
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, Mapping):
                    txt = _read_str(tag, "text").strip()
                    if txt:
                        return txt
    if _read_bool(c, "is_author", False):
        return "作者"
    author_obj = c.get("author")
    if isinstance(author_obj, Mapping):
        role = _read_str(author_obj, "role")
        if role == "author":
            return "作者"
        if role == "answerer":
            return "答主"
    author_name = _read_author_name(c)
    if content_author_name and author_name and author_name == content_author_name:
        return "答主"
    if _read_bool(c, "hot", False) or _read_bool(c, "featured", False):
        return "热评"
    return ""


def _parse_single_comment_node(
    c: Mapping[str, Any], content_author_name: str, *, depth: int = 0
) -> CommentNode:
    cid = _read_id_str(c, "id")
    author = _read_author_name(c) or "知乎用户"
    role_badge = _detect_comment_role_badge(c, content_author_name)
    raw_body = _read_str(c, "content")

    reply_to = ""
    rta = c.get("reply_to_author")
    if isinstance(rta, Mapping):
        if isinstance(rta.get("member"), Mapping):
            reply_to = _read_str(rta["member"], "name").strip()
        else:
            reply_to = _read_str(rta, "name").strip()

    body = html_to_clean_text(raw_body)
    if reply_to and not body.startswith("回复 "):
        body = f"回复 @{reply_to}：{body}"

    ts = _read_int(c, "created_time", _read_int(c, "created", 0))
    created_at = format_timestamp(ts) if ts > 0 else ""
    score = _read_int(c, "vote_count", _read_int(c, "like_count", 0))
    collapsed = _read_bool(c, "collapsed", False)

    replies: list[CommentNode] = []
    raw_children = c.get("child_comments")
    if depth < _MAX_COMMENT_DEPTH and isinstance(raw_children, list):
        for child in raw_children:
            if isinstance(child, Mapping):
                replies.append(
                    _parse_single_comment_node(child, content_author_name, depth=depth + 1)
                )

    return CommentNode(
        id=cid,
        author=author,
        role_badge=role_badge,
        body=body,
        created_at=created_at,
        score=score,
        collapsed=collapsed,
        replies=replies,
    )


def parse_comments_json(j: Any, content_author_name: str = "") -> CommentPageResult:
    """Parse Zhihu `comment_v5` or `v4` comments JSON response."""
    res = CommentPageResult()
    if not isinstance(j, Mapping):
        return res
    paging = j.get("paging")
    if isinstance(paging, Mapping):
        res.is_end = _read_bool(paging, "is_end", True)
        res.next_url = _read_str(paging, "next")
        res.total = max(0, _read_int(paging, "totals", 0))
    counts = j.get("counts")
    if res.total == 0 and isinstance(counts, Mapping):
        res.total = max(0, _read_int(counts, "total_counts", 0))

    data = j.get("data")
    if isinstance(data, list):
        for c in data:
            if isinstance(c, Mapping):
                res.comments.append(_parse_single_comment_node(c, content_author_name))
    return res


def parse_comments_from_initial_data(
    initial_data: Any, content_author_name: str = ""
) -> list[CommentNode]:
    """Reconstruct nested comment tree from `initialState.entities.comments` safely."""
    state = _resolve_initial_state(initial_data)
    if not state or not isinstance(state.get("entities"), Mapping):
        return []
    cmap = state["entities"].get("comments")
    if not isinstance(cmap, Mapping):
        return []

    by_id: dict[str, CommentNode] = {}
    order: list[str] = []
    parent_map: dict[str, str] = {}

    for key, raw_c in cmap.items():
        if not isinstance(raw_c, Mapping):
            continue
        node = _parse_single_comment_node(raw_c, content_author_name)
        nid = node.id or str(key)
        if not node.id:
            node = node.model_copy(update={"id": nid})
        pid = _read_id_str(raw_c, "reply_to_comment_id")
        root_pid = _read_id_str(raw_c, "root_comment_id")
        eff_parent = pid or root_pid
        if eff_parent and eff_parent != nid:
            parent_map[nid] = eff_parent
        order.append(nid)
        by_id[nid] = node

    children_by_parent: dict[str, list[str]] = {}
    attached: set[str] = set()
    for cid in order:
        pid = parent_map.get(cid)
        if pid and pid in by_id:
            # Guard against cyclic parent references in malformed payloads.
            cur: str | None = pid
            seen_ancestors: set[str] = {cid}
            cyclic = False
            while cur is not None:
                if cur in seen_ancestors:
                    cyclic = True
                    break
                seen_ancestors.add(cur)
                cur = parent_map.get(cur)
            if not cyclic:
                children_by_parent.setdefault(pid, []).append(cid)
                attached.add(cid)

    def _build_tree(cid: str, depth: int = 0) -> CommentNode:
        node = by_id[cid]
        if depth >= _MAX_COMMENT_DEPTH:
            return node
        extra_ids = children_by_parent.get(cid, ())
        if not extra_ids:
            return node
        merged_replies = list(node.replies) + [_build_tree(ch_id, depth + 1) for ch_id in extra_ids]
        return node.model_copy(update={"replies": merged_replies})

    return [_build_tree(cid) for cid in order if cid not in attached]


def parse_article_from_initial_data(
    initial_data: Any, target: ParsedZhihuTarget
) -> TextArticle | None:
    """Extract target article, answer, or question + answers from `js-initialData`."""
    state = _resolve_initial_state(initial_data)
    if not state or not isinstance(state.get("entities"), Mapping):
        return None
    entities = state["entities"]
    article: TextArticle | None = None

    if target.kind == "article":
        arts = entities.get("articles")
        if isinstance(arts, Mapping) and arts:
            art_obj = arts.get(target.article_id) if target.article_id in arts else next(iter(arts.values()))
            article = parse_zhuanlan_api_json(art_obj)

    elif target.kind == "answer":
        ans_map = entities.get("answers")
        if isinstance(ans_map, Mapping) and ans_map:
            ans_obj = ans_map.get(target.answer_id) if target.answer_id in ans_map else next(iter(ans_map.values()))
            if isinstance(ans_obj, Mapping):
                qid = target.question_id
                if not qid and isinstance(ans_obj.get("question"), Mapping):
                    qid = _read_id_str(ans_obj["question"], "id")
                article = parse_answer_api_json(ans_obj, qid)
                questions = entities.get("questions")
                if article is not None and qid and isinstance(questions, Mapping) and isinstance(questions.get(qid), Mapping):
                    q_obj = questions[qid]
                    q_title = html_to_clean_text(_read_str(q_obj, "title"))
                    if q_title and article.title.startswith("知乎回答 #"):
                        article = article.model_copy(update={"title": q_title})
                    q_paras = parse_rich_html_to_paragraphs(_read_str(q_obj, "detail"), "q")
                    if q_paras:
                        merged: list[TextParagraph] = [
                            TextParagraph(id="q-hdr", kind="heading", original="问题背景")
                        ]
                        for qp in q_paras:
                            merged.append(
                                qp.model_copy(update={"kind": "quote"}) if qp.kind == "paragraph" else qp
                            )
                        merged.append(
                            TextParagraph(
                                id="a-hdr",
                                kind="heading",
                                original=f"答主回答 · {article.authors}",
                            )
                        )
                        merged.extend(article.paragraphs)
                        article = article.model_copy(update={"paragraphs": merged})

    elif target.kind == "question":
        q_map = entities.get("questions")
        if isinstance(q_map, Mapping) and q_map:
            q_obj = q_map.get(target.question_id) if target.question_id in q_map else next(iter(q_map.values()))
            ans_map = entities.get("answers")
            answers_arr: list[Mapping[str, Any]] = []
            if isinstance(ans_map, Mapping):
                for ans in ans_map.values():
                    if not isinstance(ans, Mapping):
                        continue
                    ans_qid = _read_id_str(ans["question"], "id") if isinstance(ans.get("question"), Mapping) else ""
                    if not target.question_id or not ans_qid or ans_qid == target.question_id:
                        answers_arr.append(ans)
            answers_arr.sort(
                key=lambda a: _read_int(a, "voteup_count", _read_int(a, "voteupCount", 0)),
                reverse=True,
            )
            article = parse_question_with_answers_api_json(q_obj, {"data": answers_arr})

    if article is not None and not article.comments:
        comments = parse_comments_from_initial_data(initial_data, article.authors)
        if comments:
            article = article.model_copy(update={"comments": comments})
    return article


def parse_fallback_html_dom(
    html_text: str, target: ParsedZhihuTarget
) -> TextArticle | None:
    """Fail-closed HTML DOM fallback requiring genuine Zhihu rich-content container and paragraphs."""
    if not html_text or not html_text.strip():
        return None
    root = _parse_dom(html_text)
    rich = _find_by_class(root, _ZHIHU_RICH_CLASSES) or _find_first_tag(root, "article")
    if rich is None:
        return None

    paragraphs: list[TextParagraph] = []
    for ch in rich.children:
        _walk_rich_nodes(ch, paragraphs, "dom")
    if not paragraphs:
        return None

    title = ""
    h1 = _find_first_tag(root, "h1")
    if h1 is not None:
        title = _collapse_spaces(_collect_raw_text(h1))
    if not title:
        title_el = _find_first_tag(root, "title")
        if title_el is not None:
            title = _collapse_spaces(_collect_raw_text(title_el))
    if not title or not target.canonical_id:
        return None

    return TextArticle(
        id=target.canonical_id,
        source_id="plugin-zhihu",
        source_name="知乎专栏" if target.kind == "article" else "知乎问答",
        category="zhihu",
        title=title,
        url=target.web_url,
        paragraphs=paragraphs,
    )
