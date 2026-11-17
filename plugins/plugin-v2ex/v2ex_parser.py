"""HTML DOM and API v1/v2 JSON parsers for V2EX feeds, topics, formulas, code blocks, and reply trees."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from omnidesk_sdk import CommentNode, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .v2ex_security import DEFAULT_V2EX_BASE_URL, V2exError, parse_topic_id_or_url, sanitize_node_slug
else:
    from v2ex_security import DEFAULT_V2EX_BASE_URL, V2exError, parse_topic_id_or_url, sanitize_node_slug

_MAX_REPLY_TREE_DEPTH = 24
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "ul", "ol", "li", "table", "tr"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,32})")
_FLOOR_RE = re.compile(r"(?:^|[\s,，:：@A-Za-z0-9_])#(\d{1,5})\b")
_DIGITS_RE = re.compile(r"\d+")
_LANG_CLASS_RE = re.compile(r"(?:^|\s)(?:language-|lang-)([A-Za-z0-9_+-]+)(?:\s|$)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.*))?$")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_positive_int(text: str, fallback: int = 0) -> int:
    m = _DIGITS_RE.search(text or "")
    if not m:
        return fallback
    try:
        return int(m.group(0))
    except ValueError:
        return fallback


def format_timestamp(epoch_sec: Any) -> str:
    """Format a positive Unix epoch timestamp in UTC `YYYY-MM-DD HH:MM`."""
    if isinstance(epoch_sec, bool) or not isinstance(epoch_sec, (int, float)) or epoch_sec <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch_sec), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(int(epoch_sec))


@dataclass(slots=True)
class _Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    classes: frozenset[str] = field(default_factory=frozenset)
    children: list[_Element | str] = field(default_factory=list)
    parent: _Element | None = None

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    def has_class(self, cls: str) -> bool:
        return cls in self.classes


class _DomBuilder(HTMLParser):
    """Tolerant HTML tree builder for untrusted/malformed V2EX HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element(tag="document")
        self._stack: list[_Element] = [self.root]
        self.element_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs}
        cls_tokens = frozenset(tok for tok in attr_map.get("class", "").split() if tok)
        parent = self._stack[-1]
        elem = _Element(tag=t, attrs=attr_map, classes=cls_tokens, parent=parent)
        parent.children.append(elem)
        self.element_count += 1
        if t not in _VOID_TAGS:
            self._stack.append(elem)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs}
        cls_tokens = frozenset(tok for tok in attr_map.get("class", "").split() if tok)
        parent = self._stack[-1]
        elem = _Element(tag=t, attrs=attr_map, classes=cls_tokens, parent=parent)
        parent.children.append(elem)
        self.element_count += 1

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            return
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _parse_dom(html: str) -> tuple[_Element, int]:
    builder = _DomBuilder()
    builder.feed(html)
    builder.close()
    return builder.root, builder.element_count


def _find_nodes(root: _Element, pred: Callable[[_Element], bool]) -> list[_Element]:
    found: list[_Element] = []

    def _walk(node: _Element) -> None:
        if node.tag != "document" and pred(node):
            found.append(node)
        for ch in node.children:
            if isinstance(ch, _Element):
                _walk(ch)

    _walk(root)
    return found


def _find_first_node(root: _Element, pred: Callable[[_Element], bool]) -> _Element | None:
    if root.tag != "document" and pred(root):
        return root
    for ch in root.children:
        if isinstance(ch, _Element):
            found = _find_first_node(ch, pred)
            if found is not None:
                return found
    return None


def _collect_text_raw(node: _Element | str, out: list[str], *, preserve_newlines: bool = False) -> None:
    if isinstance(node, str):
        out.append(node)
        return
    tag = node.tag
    if tag in {"script", "style", "noscript"}:
        return
    if tag == "br":
        out.append("\n")
        return
    if tag == "img":
        alt = node.attr("alt")
        src = node.attr("src")
        if src and "/static/" not in src and "heart" not in src:
            joined = "".join(out)
            if joined and joined[-1] not in (" ", "\n"):
                out.append(" ")
            out.append(f"[图片: {src}]")
        elif alt:
            out.append(alt)
        return
    for ch in node.children:
        _collect_text_raw(ch, out, preserve_newlines=preserve_newlines)
    if preserve_newlines and tag in {"p", "div", "li", "tr", "blockquote"}:
        joined = "".join(out)
        if joined and not joined.endswith("\n"):
            out.append("\n")


def _node_text(node: _Element | None, *, preserve_newlines: bool = False) -> str:
    if node is None:
        return ""
    parts: list[str] = []
    _collect_text_raw(node, parts, preserve_newlines=preserve_newlines)
    raw = "".join(parts)
    return raw.strip() if preserve_newlines else _collapse_ws(raw)


def _append_split_formula_and_paragraphs(
    text: str,
    id_prefix: str,
    seq: list[int],
    out: list[TextParagraph],
) -> None:
    trimmed = text.strip()
    if not trimmed:
        return
    pos = 0
    while pos < len(trimmed):
        start = trimmed.find("$$", pos)
        if start == -1:
            rem = trimmed[pos:].strip()
            if rem:
                seq[0] += 1
                out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="paragraph", original=rem))
            break
        if start > pos:
            before = trimmed[pos:start].strip()
            if before:
                seq[0] += 1
                out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="paragraph", original=before))
        end = trimmed.find("$$", start + 2)
        if end == -1:
            rem = trimmed[start:].strip()
            if rem:
                seq[0] += 1
                out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="paragraph", original=rem))
            break
        formula = trimmed[start + 2 : end].strip()
        if formula:
            seq[0] += 1
            out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="formula", original=formula))
        pos = end + 2


def _walk_rich_dom(
    node: _Element | str,
    id_prefix: str,
    seq: list[int],
    inline_buf: list[str],
    out: list[TextParagraph],
) -> None:
    def _flush_inline() -> None:
        cleaned = "".join(inline_buf).strip()
        inline_buf.clear()
        if not cleaned:
            return
        para_lines: list[str] = []
        for line in cleaned.splitlines():
            tline = line.strip()
            if not tline:
                if para_lines:
                    _append_split_formula_and_paragraphs("\n".join(para_lines), id_prefix, seq, out)
                    para_lines.clear()
            else:
                para_lines.append(tline)
        if para_lines:
            _append_split_formula_and_paragraphs("\n".join(para_lines), id_prefix, seq, out)

    if isinstance(node, str):
        inline_buf.append(node)
        return

    tag = node.tag
    if tag == "script":
        stype = node.attr("type").lower()
        if "math/tex" in stype:
            _flush_inline()
            raw = "".join(ch for ch in node.children if isinstance(ch, str)).strip()
            if raw:
                seq[0] += 1
                out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="formula", original=raw))
        return
    if tag in {"style", "noscript"}:
        return
    if tag == "br":
        inline_buf.append("\n")
        return
    if tag == "img":
        data_formula = node.attr("data-formula").strip()
        if data_formula:
            _flush_inline()
            seq[0] += 1
            out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="formula", original=data_formula))
            return
        src = node.attr("src").strip()
        if src:
            cur = "".join(inline_buf)
            if cur and cur[-1] not in (" ", "\n"):
                inline_buf.append(" ")
            inline_buf.append(f"[图片: {src}]")
        return
    if tag in _HEADING_TAGS:
        _flush_inline()
        txt = _node_text(node, preserve_newlines=False)
        if txt:
            seq[0] += 1
            out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="heading", original=txt))
        return
    if tag == "pre":
        _flush_inline()
        code_parts: list[str] = []
        _collect_text_raw(node, code_parts, preserve_newlines=False)
        code_raw = "".join(code_parts).lstrip("\r\n").rstrip()
        if code_raw:
            lang = ""
            code_child = _find_first_node(node, lambda n: n.tag == "code")
            if code_child is not None:
                m_lang = _LANG_CLASS_RE.search(code_child.attr("class"))
                if m_lang:
                    lang = m_lang.group(1)
            seq[0] += 1
            out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="code", original=code_raw, language=lang))
        return
    if tag == "blockquote":
        _flush_inline()
        quote_txt = _node_text(node, preserve_newlines=True)
        if quote_txt:
            seq[0] += 1
            out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="quote", original=quote_txt))
        return
    if node.has_class("math") or node.has_class("katex-display") or node.has_class("MathJax_Display"):
        _flush_inline()
        math_txt = _node_text(node, preserve_newlines=False)
        if math_txt:
            seq[0] += 1
            out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="formula", original=math_txt))
        return

    is_block = tag in _BLOCK_TAGS
    if is_block:
        _flush_inline()
    if tag == "li":
        inline_buf.append("• ")
    for ch in node.children:
        _walk_rich_dom(ch, id_prefix, seq, inline_buf, out)
    if is_block:
        _flush_inline()


def parse_rich_html_to_paragraphs(html: str, id_prefix: str = "p") -> list[TextParagraph]:
    """Parse rich HTML content into structured heading/paragraph/quote/code/formula blocks."""
    if not (html or "").strip():
        return []
    root, _ = _parse_dom(html)
    out: list[TextParagraph] = []
    seq = [0]
    inline_buf: list[str] = []
    _walk_rich_dom(root, id_prefix, seq, inline_buf, out)
    if "".join(inline_buf).strip():
        _append_split_formula_and_paragraphs("".join(inline_buf), id_prefix, seq, out)
    return out


def parse_plain_text_to_paragraphs(text: str, id_prefix: str = "p") -> list[TextParagraph]:
    """Parse markdown-like plain text (with fenced code, headings, quotes, and $$ math) into TextParagraphs."""
    trimmed = (text or "").strip()
    if not trimmed:
        return []
    out: list[TextParagraph] = []
    seq = [0]
    in_fence = False
    fence_lang = ""
    code_lines: list[str] = []
    para_lines: list[str] = []

    def _flush_para() -> None:
        p = "\n".join(para_lines).strip()
        para_lines.clear()
        if not p:
            return
        m_head = _MD_HEADING_RE.match(p)
        if m_head and "\n" not in p:
            heading = (m_head.group(2) or "").strip()
            if heading:
                seq[0] += 1
                out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="heading", original=heading))
                return
        if p.startswith(">"):
            quote_lines = [
                ln.lstrip()[1:].strip() if ln.lstrip().startswith(">") else ln.strip()
                for ln in p.splitlines()
            ]
            quote = "\n".join(ql for ql in quote_lines if ql).strip()
            if quote:
                seq[0] += 1
                out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="quote", original=quote))
                return
        _append_split_formula_and_paragraphs(p, id_prefix, seq, out)

    for raw_line in trimmed.splitlines():
        line = raw_line.rstrip("\r")
        tline = line.strip()
        if tline.startswith("```"):
            if not in_fence:
                _flush_para()
                in_fence = True
                fence_lang = tline[3:].strip().split()[0] if tline[3:].strip() else ""
                code_lines.clear()
            else:
                in_fence = False
                code = "\n".join(code_lines).strip()
                code_lines.clear()
                if code:
                    seq[0] += 1
                    out.append(TextParagraph(id=f"{id_prefix}_{seq[0]}", kind="code", original=code, language=fence_lang))
                fence_lang = ""
            continue
        if in_fence:
            code_lines.append(line)
            continue
        if not tline:
            _flush_para()
        else:
            para_lines.append(line)

    if in_fence and "\n".join(code_lines).strip():
        seq[0] += 1
        out.append(
            TextParagraph(
                id=f"{id_prefix}_{seq[0]}",
                kind="code",
                original="\n".join(code_lines).strip(),
                language=fence_lang,
            )
        )
    _flush_para()
    return out


def html_to_clean_text(html: str) -> str:
    """Convert HTML fragment to clean text preserving paragraph line breaks."""
    if not (html or "").strip():
        return ""
    root, _ = _parse_dom(html)
    return _node_text(root, preserve_newlines=True)


@dataclass(frozen=True, slots=True)
class SubscribedNode:
    slug: str
    title: str


@dataclass(slots=True)
class RawReply:
    id: str = ""
    floor: int = 0
    author: str = ""
    role_badge: str = ""
    body: str = ""
    created_at: str = ""
    score: int = 0
    mention_users: list[str] = field(default_factory=list)
    explicit_floor: int = 0


@dataclass(slots=True)
class ParsedTopicPage:
    article: TextArticle
    raw_replies: list[RawReply] = field(default_factory=list)
    current_page: int = 1
    total_pages: int = 1
    expected_total_replies: int = 0


def parse_html_topic_list(
    html: str,
    channel_id: str = "",
    fallback_node_title: str = "",
    *,
    base_url: str = DEFAULT_V2EX_BASE_URL,
) -> list[TextFeedItem]:
    """Parse V2EX tab or node HTML topic list; raise V2exError('parse_error') on malformed HTML."""
    stripped = (html or "").strip()
    if not stripped:
        raise V2exError("parse_error", "V2EX 列表页面响应为空")

    root, elem_count = _parse_dom(html)
    if elem_count == 0:
        raise V2exError("parse_error", "V2EX 列表页面包含非 HTML 畸形内容")

    cells = _find_nodes(
        root,
        lambda n: n.tag == "div"
        and n.has_class("cell")
        and _find_first_node(n, lambda sub: sub.tag == "span" and sub.has_class("item_title")) is not None,
    )
    any_item_titles = _find_nodes(root, lambda n: n.tag == "span" and n.has_class("item_title"))

    items: list[TextFeedItem] = []
    seen_ids: set[str] = set()
    for cell in cells:
        title_span = _find_first_node(cell, lambda n: n.tag == "span" and n.has_class("item_title"))
        title_a = _find_first_node(title_span, lambda n: n.tag == "a") if title_span else None
        if title_a is None:
            continue
        href = title_a.attr("href")
        try:
            target = parse_topic_id_or_url(
                href,
                base_url=base_url,
                allow_loopback_http=base_url.startswith("http://"),
            )
        except V2exError:
            continue
        if target.topic_id in seen_ids:
            continue
        title_text = _node_text(title_a, preserve_newlines=False)
        if not title_text:
            continue
        seen_ids.add(target.topic_id)

        tags: list[str] = []
        node_a = _find_first_node(cell, lambda n: n.tag == "a" and n.has_class("node"))
        if node_a is not None:
            node_label = _node_text(node_a, preserve_newlines=False)
            if node_label:
                tags.append(node_label)
        elif fallback_node_title:
            tags.append(fallback_node_title)
        if channel_id and channel_id not in tags:
            tags.append(channel_id)

        author = ""
        published_at = ""
        score = 0
        excerpt = ""
        info_span = _find_first_node(
            cell,
            lambda n: n.tag == "span" and (n.has_class("topic_info") or n.has_class("small")),
        )
        if info_span is not None:
            author_a = _find_first_node(info_span, lambda n: n.tag == "a" and n.attr("href").startswith("/member/"))
            if author_a is not None:
                author = _node_text(author_a, preserve_newlines=False)
            time_span = _find_first_node(info_span, lambda n: n.tag == "span" and bool(n.attr("title")))
            if time_span is not None:
                published_at = _node_text(time_span, preserve_newlines=False) or time_span.attr("title").strip()
            votes_el = _find_first_node(info_span, lambda n: n.has_class("votes"))
            if votes_el is not None:
                score = _extract_positive_int(_node_text(votes_el, preserve_newlines=False), 0)
            excerpt = _node_text(info_span, preserve_newlines=False)

        comment_count = 0
        count_a = _find_first_node(
            cell,
            lambda n: n.tag == "a" and (n.has_class("count_livid") or n.has_class("count_orange") or n.has_class("count_blue")),
        )
        if count_a is not None:
            comment_count = _extract_positive_int(_node_text(count_a, preserve_newlines=False), 0)
        elif "#reply" in href:
            comment_count = _extract_positive_int(href.split("#reply", 1)[1], 0)

        items.append(
            TextFeedItem(
                id=target.topic_id,
                source_id="plugin-v2ex",
                source_name="V2EX",
                category="v2ex",
                channel_id=channel_id,
                title=title_text,
                excerpt=excerpt,
                author=author,
                published_at=published_at,
                url=target.web_url,
                tags=tags,
                comment_count=max(0, comment_count),
                score=score,
            )
        )

    if items:
        return items

    if any_item_titles:
        raise V2exError("parse_error", "V2EX 列表卡片畸形，无法提取有效主题")

    has_v2ex_marker = (
        _find_first_node(
            root,
            lambda n: n.attr("id") in {"Main", "Wrapper", "TopicsNode"}
            or n.has_class("box")
            or n.has_class("header"),
        )
        is not None
        or "v2ex" in _node_text(_find_first_node(root, lambda n: n.tag == "title")).lower()
    )
    if not has_v2ex_marker:
        raise V2exError("parse_error", "V2EX 列表页面结构无效，无法解析主题列表")
    return []


def parse_api_v2_node_topics_json(
    data: Any,
    fallback_node_slug: str = "",
    fallback_node_title: str = "",
    *,
    channel_id: str = "",
    base_url: str = DEFAULT_V2EX_BASE_URL,
) -> list[TextFeedItem]:
    """Parse V2EX API v2 node topics JSON payload (or API v1 array)."""
    if isinstance(data, Mapping):
        if "result" not in data or not isinstance(data["result"], list):
            raise V2exError("parse_error", "V2EX API v2 主题列表缺少 result 数组")
        arr = data["result"]
    elif isinstance(data, list):
        arr = data
    else:
        raise V2exError("parse_error", "V2EX API 主题列表 JSON 格式无效")

    items: list[TextFeedItem] = []
    clean_base = base_url.rstrip("/")
    for elem in arr:
        if not isinstance(elem, Mapping):
            continue
        raw_id = elem.get("id")
        tid = str(raw_id).strip() if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
        title = str(elem.get("title") or "").strip()
        if not tid or not title:
            continue

        raw_content = str(elem.get("content") or "")
        if not raw_content and isinstance(elem.get("content_rendered"), str):
            raw_content = html_to_clean_text(elem["content_rendered"])
        preview = _collapse_ws(raw_content)
        if len(preview) > 180:
            preview = preview[:180] + "..."

        author = ""
        member = elem.get("member")
        if isinstance(member, Mapping):
            author = str(member.get("username") or "").strip()
        if not author:
            author = str(elem.get("last_reply_by") or "").strip()

        published_at = format_timestamp(elem.get("created"))
        tags: list[str] = []
        node_obj = elem.get("node")
        node_title = fallback_node_title
        node_slug = fallback_node_slug
        if isinstance(node_obj, Mapping):
            node_title = str(node_obj.get("title") or node_title or "").strip()
            node_slug = str(node_obj.get("name") or node_slug or "").strip()
        if node_title:
            tags.append(node_title)
        elif node_slug:
            tags.append(node_slug)
        if channel_id and channel_id not in tags:
            tags.append(channel_id)

        replies = elem.get("replies", 0)
        reply_count = replies if isinstance(replies, int) and not isinstance(replies, bool) and replies >= 0 else 0

        items.append(
            TextFeedItem(
                id=tid,
                source_id="plugin-v2ex",
                source_name="V2EX",
                category="v2ex",
                channel_id=channel_id,
                title=title,
                excerpt=preview,
                author=author,
                published_at=published_at,
                url=str(elem.get("url") or f"{clean_base}/t/{tid}"),
                tags=tags,
                comment_count=reply_count,
            )
        )
    return items


def parse_api_v1_topic_list_json(
    data: Any,
    channel_id: str = "",
    *,
    base_url: str = DEFAULT_V2EX_BASE_URL,
) -> list[TextFeedItem]:
    """Parse V2EX legacy public API v1 topic array JSON."""
    return parse_api_v2_node_topics_json(data, channel_id=channel_id, base_url=base_url)


def parse_my_nodes_html(html: str) -> list[SubscribedNode]:
    """Parse /my/nodes HTML into SubscribedNode entries."""
    if not (html or "").strip():
        return []
    root, _ = _parse_dom(html)
    links = _find_nodes(
        root,
        lambda n: n.tag == "a"
        and n.attr("href").startswith("/go/")
        and (n.has_class("grid_item") or n.has_class("node") or n.has_class("item_node")),
    )
    out: list[SubscribedNode] = []
    seen: set[str] = set()
    for a in links:
        href = a.attr("href")
        slug = sanitize_node_slug(href.split("?", 1)[0].split("#", 1)[0])
        if not slug or slug in seen:
            continue
        seen.add(slug)
        label = _node_text(a, preserve_newlines=False)
        fav_count = _find_first_node(a, lambda sub: sub.tag == "span" and sub.has_class("fade"))
        if fav_count is not None:
            ftxt = _node_text(fav_count, preserve_newlines=False)
            if ftxt and label.endswith(ftxt):
                label = label[: -len(ftxt)].strip()
        out.append(SubscribedNode(slug=slug, title=label or slug))
    return out


def extract_reply_mentions_and_floor(text: str) -> tuple[list[str], int]:
    """Extract deduplicated @mentions and explicit #floor references from reply text."""
    if not text:
        return [], 0
    users: list[str] = []
    seen: set[str] = set()
    for m in _MENTION_RE.finditer(text):
        uname = m.group(1)
        low = uname.lower()
        if uname and low not in seen:
            seen.add(low)
            users.append(uname)
    explicit_floor = 0
    fm = _FLOOR_RE.search(text)
    if fm:
        try:
            explicit_floor = int(fm.group(1))
        except ValueError:
            explicit_floor = 0
    return users, explicit_floor


def build_reply_tree(raw_replies: Sequence[RawReply], topic_author: str = "") -> list[CommentNode]:
    """Assemble nested CommentNode tree from flat ordered V2EX replies."""
    if not raw_replies:
        return []

    ordered: list[RawReply] = []
    seen_ids: set[str] = set()
    for r in raw_replies:
        key = r.id or f"floor:{r.floor}"
        if key not in seen_ids:
            seen_ids.add(key)
            ordered.append(r)

    n = len(ordered)
    low_op = topic_author.strip().lower()
    flat_nodes: list[CommentNode] = []
    parent_idx = [-1] * n
    depth = [0] * n
    floor_to_idx: dict[int, int] = {}
    latest_user_idx: dict[str, int] = {}

    for i, src in enumerate(ordered):
        floor_num = src.floor if src.floor > 0 else (i + 1)
        is_op = (bool(low_op) and src.author.strip().lower() == low_op) or src.role_badge.strip().lower() == "op"
        if is_op:
            badge = f"OP · #{floor_num}"
        elif src.role_badge:
            badge = f"{src.role_badge} · #{floor_num}"
        else:
            badge = f"#{floor_num}"

        p = -1
        if 0 < src.explicit_floor < floor_num:
            cand = floor_to_idx.get(src.explicit_floor, -1)
            if 0 <= cand < i:
                p = cand
        if p < 0:
            for mentioned in src.mention_users:
                low_m = mentioned.strip().lower()
                if not low_m:
                    continue
                cand = latest_user_idx.get(low_m, -1)
                if 0 <= cand < i:
                    p = cand
                    break

        while p >= 0 and depth[p] + 1 >= _MAX_REPLY_TREE_DEPTH:
            p = parent_idx[p]
        if p >= 0:
            parent_idx[i] = p
            depth[i] = depth[p] + 1

        floor_to_idx[floor_num] = i
        if src.author.strip():
            latest_user_idx[src.author.strip().lower()] = i

        flat_nodes.append(
            CommentNode(
                id=src.id or f"r_{floor_num}",
                author=src.author or "anonymous",
                role_badge=badge,
                body=src.body,
                created_at=src.created_at,
                score=src.score,
                collapsed=False,
                replies=[],
            )
        )

    children_by_parent: list[list[CommentNode]] = [[] for _ in range(n)]
    roots: list[CommentNode] = []
    for i in range(n - 1, -1, -1):
        if children_by_parent[i]:
            flat_nodes[i].replies = list(reversed(children_by_parent[i]))
        p = parent_idx[i]
        if 0 <= p < i:
            children_by_parent[p].append(flat_nodes[i])
    for i in range(n):
        if parent_idx[i] < 0:
            roots.append(flat_nodes[i])
    return roots


def count_comment_nodes(roots: Sequence[CommentNode]) -> int:
    """Count total comments across the recursive CommentNode tree."""
    return sum(1 + count_comment_nodes(node.replies) for node in roots)


def parse_html_topic_page(
    html: str,
    topic_id_hint: str = "",
    *,
    base_url: str = DEFAULT_V2EX_BASE_URL,
) -> ParsedTopicPage:
    """Parse V2EX HTML topic page into TextArticle + RawReplies + pagination info."""
    if not (html or "").strip():
        raise V2exError("parse_error", "V2EX 主题页面响应为空")

    clean_base = base_url.rstrip("/")
    tid = ""
    web_url = ""
    if topic_id_hint:
        try:
            target = parse_topic_id_or_url(
                topic_id_hint,
                base_url=base_url,
                allow_loopback_http=base_url.startswith("http://"),
            )
            tid = target.topic_id
            web_url = target.web_url
        except V2exError:
            tid = topic_id_hint
            web_url = f"{clean_base}/t/{tid}"

    root, elem_count = _parse_dom(html)
    if elem_count == 0:
        raise V2exError("parse_error", "V2EX 主题页面包含非 HTML 畸形内容")

    scope = _find_first_node(root, lambda n: n.attr("id") == "Main") or root
    header_div = _find_first_node(scope, lambda n: n.tag == "div" and n.has_class("header"))

    title = ""
    author = ""
    tags: list[str] = []
    subtitle = ""
    published_at = ""

    if header_div is not None:
        h1 = _find_first_node(header_div, lambda n: n.tag == "h1")
        if h1 is not None:
            title = _node_text(h1, preserve_newlines=False)
        author_a = _find_first_node(header_div, lambda n: n.tag == "a" and n.attr("href").startswith("/member/"))
        if author_a is not None:
            author = _node_text(author_a, preserve_newlines=False)
        node_a = _find_first_node(header_div, lambda n: n.tag == "a" and n.attr("href").startswith("/go/"))
        if node_a is not None:
            node_name = _node_text(node_a, preserve_newlines=False)
            if node_name:
                tags.append(node_name)
                subtitle = f"V2EX · {node_name}"
        time_span = _find_first_node(header_div, lambda n: n.tag == "span" and bool(n.attr("title")))
        if time_span is not None:
            published_at = time_span.attr("title").strip() or _node_text(time_span, preserve_newlines=False)

    paragraphs: list[TextParagraph] = []
    seq = [0]
    topic_contents = _find_nodes(scope, lambda n: n.tag == "div" and n.has_class("topic_content"))
    for tc in topic_contents:
        if tc.parent is not None and tc.parent.has_class("subtle"):
            continue
        inline_buf: list[str] = []
        _walk_rich_dom(tc, "body", seq, inline_buf, paragraphs)
        if "".join(inline_buf).strip():
            _append_split_formula_and_paragraphs("".join(inline_buf), "body", seq, paragraphs)

    subtles = _find_nodes(scope, lambda n: n.tag == "div" and n.has_class("subtle"))
    for sidx, sub in enumerate(subtles, start=1):
        fade = _find_first_node(sub, lambda n: n.tag == "span" and n.has_class("fade"))
        heading = _node_text(fade, preserve_newlines=False) if fade is not None else f"附言 {sidx}"
        if heading:
            paragraphs.append(TextParagraph(id=f"subtle_h_{sidx}", kind="heading", original=heading))
        sub_tc = _find_first_node(sub, lambda n: n.tag == "div" and n.has_class("topic_content"))
        if sub_tc is not None:
            inline_buf = []
            _walk_rich_dom(sub_tc, f"subtle_{sidx}", seq, inline_buf, paragraphs)
            if "".join(inline_buf).strip():
                _append_split_formula_and_paragraphs("".join(inline_buf), f"subtle_{sidx}", seq, paragraphs)

    current_page = 1
    total_pages = 1
    page_input = _find_first_node(scope, lambda n: n.tag == "input" and n.has_class("page_input"))
    if page_input is not None and page_input.attr("max").strip():
        current_page = max(1, _extract_positive_int(page_input.attr("value"), 1))
        total_pages = max(current_page, _extract_positive_int(page_input.attr("max"), 1))
    else:
        for pl in _find_nodes(scope, lambda n: n.has_class("page_normal") or n.has_class("page_current")):
            pnum = _extract_positive_int(_node_text(pl, preserve_newlines=False), 1)
            if pl.has_class("page_current"):
                current_page = max(current_page, pnum)
            total_pages = max(total_pages, pnum)

    reply_cells = _find_nodes(scope, lambda n: n.tag == "div" and n.has_class("cell") and n.attr("id").startswith("r_"))
    raw_replies: list[RawReply] = []
    fallback_floor = (current_page - 1) * 100
    for rc in reply_cells:
        rid = rc.attr("id")
        r_author_a = _find_first_node(
            rc, lambda n: n.tag == "a" and (n.has_class("dark") or n.attr("href").startswith("/member/"))
        )
        r_author = _node_text(r_author_a, preserve_newlines=False)
        floor_span = _find_first_node(rc, lambda n: n.tag == "span" and n.has_class("no"))
        if floor_span is not None:
            fallback_floor += 1
            floor = _extract_positive_int(_node_text(floor_span, preserve_newlines=False), fallback_floor)
        else:
            fallback_floor += 1
            floor = fallback_floor

        ago_span = _find_first_node(rc, lambda n: n.tag == "span" and n.has_class("ago"))
        created_at = ""
        if ago_span is not None:
            created_at = _node_text(ago_span, preserve_newlines=False) or ago_span.attr("title").strip()

        badge_el = _find_first_node(rc, lambda n: n.has_class("badge"))
        role_badge = _node_text(badge_el, preserve_newlines=False)

        score = 0
        for fs in _find_nodes(rc, lambda n: n.tag == "span" and n.has_class("small") and n.has_class("fade")):
            sc = _extract_positive_int(_node_text(fs, preserve_newlines=False), 0)
            if sc > 0:
                score = sc
                break

        reply_content = _find_first_node(rc, lambda n: n.tag == "div" and n.has_class("reply_content"))
        body = ""
        mentions: list[str] = []
        explicit_floor = 0
        if reply_content is not None:
            body = _node_text(reply_content, preserve_newlines=True)
            mentions, explicit_floor = extract_reply_mentions_and_floor(body)
            seen_m = {u.lower() for u in mentions}
            for ml in _find_nodes(reply_content, lambda n: n.tag == "a" and n.attr("href").startswith("/member/")):
                uname = _node_text(ml, preserve_newlines=False).lstrip("@").strip()
                if uname and uname.lower() not in seen_m:
                    seen_m.add(uname.lower())
                    mentions.append(uname)

        if body or r_author:
            raw_replies.append(
                RawReply(
                    id=rid,
                    floor=floor,
                    author=r_author,
                    role_badge=role_badge,
                    body=body,
                    created_at=created_at,
                    score=score,
                    mention_users=mentions,
                    explicit_floor=explicit_floor,
                )
            )

    if not title and not paragraphs and not raw_replies:
        raise V2exError("parse_error", "V2EX 主题页面结构无效或缺少标题与正文")

    article = TextArticle(
        id=tid or "unknown",
        source_id="plugin-v2ex",
        source_name="V2EX",
        category="v2ex",
        title=title or f"V2EX Topic #{tid or 'unknown'}",
        subtitle=subtitle,
        authors=author,
        author=author,
        published_at=published_at,
        url=web_url or f"{clean_base}/t/{tid}",
        tags=tags,
        paragraphs=paragraphs,
        comments=build_reply_tree(raw_replies, author),
    )
    return ParsedTopicPage(
        article=article,
        raw_replies=raw_replies,
        current_page=current_page,
        total_pages=total_pages,
    )


def parse_api_v2_topic_json(data: Any, *, base_url: str = DEFAULT_V2EX_BASE_URL) -> ParsedTopicPage:
    """Parse V2EX API v2 (or single-object API v1) topic JSON."""
    obj: Mapping[str, Any] | None = None
    if isinstance(data, Mapping):
        if isinstance(data.get("result"), Mapping):
            obj = data["result"]
        elif "id" in data or "title" in data:
            obj = data
    if obj is None:
        raise V2exError("parse_error", "V2EX API 主题详情 JSON 格式无效")

    raw_id = obj.get("id")
    tid = str(raw_id).strip() if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
    title = str(obj.get("title") or "").strip()
    if not tid or not title:
        raise V2exError("parse_error", "V2EX API 主题详情缺少 id 或 title")

    clean_base = base_url.rstrip("/")
    expected_replies = obj.get("replies", 0)
    if not isinstance(expected_replies, int) or isinstance(expected_replies, bool) or expected_replies < 0:
        expected_replies = 0
    total_pages = max(1, (expected_replies + 19) // 20)
    pagination = obj.get("pagination")
    if isinstance(pagination, Mapping):
        for pkey in ("total_pages", "pages"):
            pval = pagination.get(pkey)
            if isinstance(pval, int) and not isinstance(pval, bool) and pval >= 1:
                total_pages = max(1, pval)
                break

    author = ""
    if isinstance(obj.get("member"), Mapping):
        author = str(obj["member"].get("username") or "").strip()
    published_at = format_timestamp(obj.get("created"))

    tags: list[str] = []
    subtitle = ""
    if isinstance(obj.get("node"), Mapping):
        ntitle = str(obj["node"].get("title") or obj["node"].get("name") or "").strip()
        if ntitle:
            tags.append(ntitle)
            subtitle = f"V2EX · {ntitle}"

    paragraphs: list[TextParagraph] = []
    rendered = str(obj.get("content_rendered") or "")
    raw_content = str(obj.get("content") or "")
    if rendered.strip():
        paragraphs = parse_rich_html_to_paragraphs(rendered, "body")
    if not paragraphs and raw_content.strip():
        paragraphs = parse_plain_text_to_paragraphs(raw_content, "body")

    supplements = obj.get("supplements")
    if isinstance(supplements, list):
        for sidx, sup in enumerate(supplements, start=1):
            if not isinstance(sup, Mapping):
                continue
            heading = f"第 {sidx} 条附言"
            ts_str = format_timestamp(sup.get("created"))
            if ts_str:
                heading += f" · {ts_str}"
            paragraphs.append(TextParagraph(id=f"subtle_h_{sidx}", kind="heading", original=heading))
            s_rendered = str(sup.get("content_rendered") or "")
            s_raw = str(sup.get("content") or "")
            if s_rendered.strip():
                paragraphs.extend(parse_rich_html_to_paragraphs(s_rendered, f"subtle_{sidx}"))
            elif s_raw.strip():
                paragraphs.extend(parse_plain_text_to_paragraphs(s_raw, f"subtle_{sidx}"))

    article = TextArticle(
        id=tid,
        source_id="plugin-v2ex",
        source_name="V2EX",
        category="v2ex",
        title=title,
        subtitle=subtitle,
        authors=author,
        author=author,
        published_at=published_at,
        url=str(obj.get("url") or f"{clean_base}/t/{tid}"),
        tags=tags,
        paragraphs=paragraphs,
    )
    return ParsedTopicPage(
        article=article,
        total_pages=total_pages,
        expected_total_replies=expected_replies,
    )


def parse_api_v2_replies_json(
    data: Any,
    topic_author: str = "",
    page_number: int = 1,
    page_size: int = 20,
) -> list[RawReply]:
    """Parse V2EX API v2 (or API v1) replies JSON payload into RawReply entries."""
    _ = topic_author
    if isinstance(data, Mapping):
        if "result" not in data or not isinstance(data["result"], list):
            raise V2exError("parse_error", "V2EX API 回复列表缺少 result 数组")
        arr = data["result"]
    elif isinstance(data, list):
        arr = data
    else:
        raise V2exError("parse_error", "V2EX API 回复列表 JSON 格式无效")

    base_floor = max(0, (page_number - 1) * max(1, page_size))
    out: list[RawReply] = []
    for idx, item in enumerate(arr, start=1):
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            rid = f"r_{raw_id}"
        elif isinstance(raw_id, str) and raw_id.strip():
            rid = raw_id.strip()
        else:
            rid = f"r_floor_{base_floor + idx}"

        raw_no = item.get("no")
        floor = raw_no if isinstance(raw_no, int) and not isinstance(raw_no, bool) and raw_no > 0 else (base_floor + idx)
        raw_thanks = item.get("thanks", 0)
        score = raw_thanks if isinstance(raw_thanks, int) and not isinstance(raw_thanks, bool) else 0

        author = ""
        if isinstance(item.get("member"), Mapping):
            author = str(item["member"].get("username") or "").strip()
        created_at = format_timestamp(item.get("created"))

        raw_content = str(item.get("content") or "").strip()
        rendered = str(item.get("content_rendered") or "").strip()
        body = raw_content if raw_content else (html_to_clean_text(rendered) if rendered else "")
        mentions, explicit_floor = extract_reply_mentions_and_floor(body)
        out.append(
            RawReply(
                id=rid,
                floor=floor,
                author=author,
                body=body,
                created_at=created_at,
                score=score,
                mention_users=mentions,
                explicit_floor=explicit_floor,
            )
        )
    return out


def parse_api_v1_topic_json(data: Any, *, base_url: str = DEFAULT_V2EX_BASE_URL) -> ParsedTopicPage:
    """Parse V2EX public API v1 topic JSON (array of 1 object or single object)."""
    if isinstance(data, list):
        if not data or not isinstance(data[0], Mapping):
            raise V2exError("not_found", "V2EX API v1 未返回有效主题")
        return parse_api_v2_topic_json(data[0], base_url=base_url)
    return parse_api_v2_topic_json(data, base_url=base_url)


def parse_api_v1_replies_json(data: Any, topic_author: str = "") -> list[RawReply]:
    """Parse V2EX public API v1 replies array JSON."""
    return parse_api_v2_replies_json(data, topic_author=topic_author, page_number=1, page_size=10000)
