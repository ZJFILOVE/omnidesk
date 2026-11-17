"""Discourse cooked HTML parser preserving code indentation, LaTeX formulas, quotes, lists, and tables."""

from __future__ import annotations

from omnidesk_sdk.media_markup import image_markdown

import html as html_lib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from omnidesk_sdk import TextParagraph

if __package__:
    from .security import DEFAULT_BASE_URL, extract_topic_id, resolve_relative_url
else:
    from security import DEFAULT_BASE_URL, extract_topic_id, resolve_relative_url  # type: ignore[no-redef]

_MAX_HTML_CHARS = 8 * 1024 * 1024
_MAX_DOM_DEPTH = 128
_MAX_PARAGRAPHS = 500
_IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "iframe", "svg", "object", "embed"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "pre", "blockquote", "aside", "ul", "ol", "table",
        "h1", "h2", "h3", "h4", "h5", "h6", "details", "summary", "hr",
    }
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "summary"})
_LANG_CLASS_RE = re.compile(r"^(?:lang(?:uage)?-)([A-Za-z0-9_+#.-]+)$")


@dataclass
class _Node:
    tag: str  # "#root", "#text", or lowercase html tag
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[_Node] = field(default_factory=list)
    parent: _Node | None = None

    def classes(self) -> set[str]:
        raw = self.attrs.get("class", "")
        return set(raw.split()) if raw else set()

    def has_class(self, cls: str) -> bool:
        return cls in self.classes()


class _DomBuilder(HTMLParser):
    """Depth-bounded stdlib HTMLParser building a resilient tree for untrusted/malformed HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="#root")
        self._stack: list[_Node] = [self.root]
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _IGNORED_TAGS:
            if t not in _VOID_TAGS:
                self._ignored_depth += 1
            return
        if self._ignored_depth > 0 or len(self._stack) >= _MAX_DOM_DEPTH:
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        node = _Node(tag=t, attrs=attr_map, parent=self._stack[-1])
        self._stack[-1].children.append(node)
        if t not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _IGNORED_TAGS or self._ignored_depth > 0 or len(self._stack) >= _MAX_DOM_DEPTH:
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self._stack[-1].children.append(_Node(tag=t, attrs=attr_map, parent=self._stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _IGNORED_TAGS:
            if t not in _VOID_TAGS and self._ignored_depth > 0:
                self._ignored_depth -= 1
            return
        if self._ignored_depth > 0:
            return
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0 or not data:
            return
        self._stack[-1].children.append(_Node(tag="#text", text=data, parent=self._stack[-1]))


def _parse_dom(html: str) -> _Node:
    builder = _DomBuilder()
    builder.feed(html[:_MAX_HTML_CHARS])
    builder.close()
    return builder.root


def decode_html_entities(text: str) -> str:
    return html_lib.unescape(text) if text else ""


def _normalize_inline_ws(text: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", text).strip()


def _clean_multiline(text: str) -> str:
    lines: list[str] = []
    last_blank = True
    for raw in text.splitlines():
        trimmed = raw.strip()
        if not trimmed:
            if not last_blank and lines:
                lines.append("")
                last_blank = True
        else:
            lines.append(trimmed)
            last_blank = False
    return "\n".join(lines).strip()


def _is_block_formula(text: str) -> str | None:
    t = text.strip()
    if len(t) >= 4 and ((t.startswith("$$") and t.endswith("$$")) or (t.startswith("\\[") and t.endswith("\\]"))):
        return t
    return None


def _is_http_url(url: str) -> bool:
    low = url.strip().lower()
    return low.startswith("https://") or low.startswith("http://")


def _collect_pre_text(node: _Node, parts: list[str]) -> None:
    if node.tag == "#text":
        parts.append(node.text)
        return
    if node.tag == "br":
        parts.append("\n")
        return
    for ch in node.children:
        _collect_pre_text(ch, parts)


def _extract_code_language(pre_node: _Node) -> str:
    for ch in pre_node.children:
        if ch.tag == "code":
            for cls in ch.classes():
                if m := _LANG_CLASS_RE.match(cls):
                    return m.group(1).lower()
    return ""


def _collect_inline_text(node: _Node, base_url: str, parts: list[str]) -> None:
    if node.tag == "#text":
        parts.append(node.text)
        return
    if node.tag in _IGNORED_TAGS:
        return
    if node.has_class("meta") and node.has_class("informations"):
        return
    if node.tag == "br":
        parts.append("\n")
        return
    if node.tag == "img":
        formula = node.attrs.get("data-formula", "").strip()
        if formula:
            parts.append(f"${formula}$")
            return
        if node.has_class("emoji"):
            parts.append(node.attrs.get("alt") or node.attrs.get("title") or "")
            return
        if node.has_class("avatar"):
            return
        alt = node.attrs.get("alt", "").strip()
        src = node.attrs.get("src", "").strip()
        if src and base_url:
            src = resolve_relative_url(base_url, src)
        parts.append(image_markdown(src, alt, base_url=base_url))
        return

    if node.has_class("math"):
        raw_parts: list[str] = []
        _collect_pre_text(node, raw_parts)
        math_raw = "".join(raw_parts).strip()
        if math_raw:
            parts.append(math_raw if math_raw.startswith("$") else f"${math_raw}$")
        return

    if node.tag == "code" and (node.parent is None or node.parent.tag != "pre"):
        code_parts: list[str] = []
        _collect_pre_text(node, code_parts)
        parts.append(f"`{''.join(code_parts).strip()}`")
        return

    if node.tag == "a":
        href = node.attrs.get("href", "").strip()
        inner_parts: list[str] = []
        for ch in node.children:
            _collect_inline_text(ch, base_url, inner_parts)
        inner = "".join(inner_parts).strip()
        resolved = resolve_relative_url(base_url, href) if href else ""
        if node.has_class("lightbox"):
            parts.append(inner or (f"[图片: {resolved}]" if resolved else "[图片]"))
            return
        if inner:
            parts.append(inner)
            if (
                href
                and not href.startswith("#")
                and not node.has_class("mention")
                and "http" not in inner
                and "[图片" not in inner
                and _is_http_url(resolved)
                and resolved != inner
            ):
                parts.append(f" ({resolved})")
        elif resolved:
            parts.append(resolved)
        return

    for ch in node.children:
        _collect_inline_text(ch, base_url, parts)


def _collect_list_text(list_node: _Node, *, ordered: bool, base_url: str) -> str:
    lines: list[str] = []
    idx = 1
    for ch in list_node.children:
        if ch.tag != "li":
            continue
        buf: list[str] = []
        _collect_inline_text(ch, base_url, buf)
        txt = _clean_multiline("".join(buf))
        if txt:
            lines.append(f"{idx}. {txt}" if ordered else f"• {txt}")
            if ordered:
                idx += 1
    return "\n".join(lines)


def _collect_table_text(table_node: _Node, base_url: str) -> str:
    rows: list[str] = []
    queue: list[_Node] = [table_node]
    while queue:
        cur = queue.pop(0)
        if cur.tag == "tr":
            cells: list[str] = []
            for cell in cur.children:
                if cell.tag in {"th", "td"}:
                    buf: list[str] = []
                    _collect_inline_text(cell, base_url, buf)
                    cells.append(_normalize_inline_ws("".join(buf)))
            if cells:
                rows.append(" | ".join(cells))
            continue
        queue.extend(cur.children)
    return "\n".join(rows)


def _push_paragraph(out: list[TextParagraph], kind: str, text: str, language: str = "") -> None:
    if len(out) >= _MAX_PARAGRAPHS:
        return
    cleaned = text.strip() if kind in {"code", "formula"} else _clean_multiline(text)
    if not cleaned:
        return
    out.append(TextParagraph(id=f"p{len(out) + 1}", kind=kind, original=cleaned, language=language))  # type: ignore[arg-type]


def _walk_block(node: _Node, base_url: str, out: list[TextParagraph]) -> None:
    if len(out) >= _MAX_PARAGRAPHS:
        return
    if node.tag == "#text":
        t = _normalize_inline_ws(node.text)
        if t:
            if formula := _is_block_formula(t):
                _push_paragraph(out, "formula", formula)
            else:
                _push_paragraph(out, "paragraph", t)
        return
    if node.tag in _IGNORED_TAGS:
        return
    if node.has_class("lightbox-wrapper"):
        buf: list[str] = []
        _collect_inline_text(node, base_url, buf)
        _push_paragraph(out, "paragraph", "".join(buf))
        return
    if node.tag in _HEADING_TAGS:
        buf = []
        _collect_inline_text(node, base_url, buf)
        _push_paragraph(out, "heading", _normalize_inline_ws("".join(buf)))
        return
    if node.tag == "pre":
        code_parts: list[str] = []
        _collect_pre_text(node, code_parts)
        code = "".join(code_parts).strip("\r\n")
        if code and len(out) < _MAX_PARAGRAPHS:
            out.append(
                TextParagraph(
                    id=f"p{len(out) + 1}",
                    kind="code",
                    original=code,
                    language=_extract_code_language(node),
                )
            )
        return
    if node.has_class("math"):
        raw_parts: list[str] = []
        _collect_pre_text(node, raw_parts)
        math_raw = "".join(raw_parts).strip()
        if math_raw:
            if not math_raw.startswith("$") and not math_raw.startswith("\\["):
                math_raw = f"$$\n{math_raw}\n$$"
            _push_paragraph(out, "formula", math_raw)
        return
    if node.tag == "aside" and node.has_class("quote"):
        q_parts: list[str] = []
        for ch in node.children:
            if ch.tag == "#text":
                continue
            buf = []
            _collect_inline_text(ch, base_url, buf)
            part = _clean_multiline("".join(buf))
            if part:
                q_parts.append(part)
        _push_paragraph(out, "quote", "\n".join(q_parts))
        return
    if (node.tag == "aside" and node.has_class("onebox")) or node.tag == "blockquote":
        buf = []
        _collect_inline_text(node, base_url, buf)
        _push_paragraph(out, "quote", "".join(buf))
        return
    if node.tag in {"ul", "ol"}:
        _push_paragraph(out, "paragraph", _collect_list_text(node, ordered=(node.tag == "ol"), base_url=base_url))
        return
    if node.tag == "table":
        _push_paragraph(out, "paragraph", _collect_table_text(node, base_url))
        return
    if node.tag == "p":
        buf = []
        _collect_inline_text(node, base_url, buf)
        txt = "".join(buf)
        if formula := _is_block_formula(txt):
            _push_paragraph(out, "formula", formula)
        else:
            _push_paragraph(out, "paragraph", txt)
        return
    if node.tag == "hr":
        return

    inline_buf: list[str] = []
    for ch in node.children:
        if ch.tag in _BLOCK_TAGS:
            inline_text = "".join(inline_buf)
            if inline_text.strip():
                if formula := _is_block_formula(inline_text):
                    _push_paragraph(out, "formula", formula)
                else:
                    _push_paragraph(out, "paragraph", inline_text)
            inline_buf.clear()
            _walk_block(ch, base_url, out)
        else:
            _collect_inline_text(ch, base_url, inline_buf)
    leftover = "".join(inline_buf)
    if leftover.strip():
        if formula := _is_block_formula(leftover):
            _push_paragraph(out, "formula", formula)
        else:
            _push_paragraph(out, "paragraph", leftover)


def strip_html_and_decode(html: str) -> str:
    """Strip HTML tags and decode entities into normalized plain text."""
    if not html:
        return ""
    root = _parse_dom(html)
    parts: list[str] = []
    _collect_inline_text(root, "", parts)
    return _normalize_inline_ws(decode_html_entities("".join(parts)))


def parse_cooked_to_paragraphs(html: str, base_url: str = DEFAULT_BASE_URL) -> list[TextParagraph]:
    """Parse Discourse cooked HTML into structured `TextParagraph` blocks."""
    if not html or not html.strip():
        return []
    root = _parse_dom(html)
    out: list[TextParagraph] = []
    _walk_block(root, base_url, out)
    return out


def parse_cooked_to_comment_text(html: str, base_url: str = DEFAULT_BASE_URL) -> str:
    """Parse Discourse reply cooked HTML into markdown-preserving comment text."""
    paras = parse_cooked_to_paragraphs(html, base_url)
    if not paras:
        return strip_html_and_decode(html)
    chunks: list[str] = []
    for p in paras:
        if p.kind == "code":
            chunks.append(f"```\n{p.original}\n```")
        elif p.kind == "quote":
            chunks.append("\n".join(f"> {ln}" for ln in p.original.splitlines()))
        else:
            chunks.append(p.original)
    return "\n\n".join(chunks).strip()


def _try_parse_json_text(candidate: str) -> Any | None:
    trimmed = (candidate or "").strip()
    if not trimmed or trimmed[0] not in {"{", "["}:
        return None
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        return None


def _unwrap_discourse_preloaded(raw_attr: str) -> dict[str, Any] | None:
    outer = _try_parse_json_text(decode_html_entities(raw_attr))
    if not isinstance(outer, Mapping):
        return None
    for key, val in outer.items():
        if isinstance(key, str) and key.startswith("topic_") and key != "topic_list" and isinstance(val, str):
            inner = _try_parse_json_text(val)
            if isinstance(inner, dict) and "post_stream" in inner:
                return inner
    for key in ("topic_list", "categories_list"):
        if isinstance(outer.get(key), str):
            inner = _try_parse_json_text(outer[key])
            if isinstance(inner, dict):
                if key == "topic_list" and "topic_list" not in inner:
                    return {"topic_list": inner}
                return inner
    return None


def parse_extract_dom_payload(extract_data: Mapping[str, Any], request_url: str) -> dict[str, Any] | None:
    """Extract Discourse JSON or DOM metadata from a `web.extract.v1` response payload."""
    for key in ("body", "text"):
        raw = extract_data.get(key)
        if isinstance(raw, str) and len(raw) <= _MAX_HTML_CHARS:
            for cand in (raw, decode_html_entities(raw)):
                parsed = _try_parse_json_text(cand)
                if isinstance(parsed, dict):
                    return parsed

    html = extract_data.get("html") if isinstance(extract_data.get("html"), str) else ""
    if len(html) > _MAX_HTML_CHARS:
        return None
    if html:
        if isinstance(parsed := _try_parse_json_text(html), dict):
            return parsed
        root = _parse_dom(html)
        pre_text = ""
        preloaded_attr = ""
        topic_links: list[tuple[str, str]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if not preloaded_attr and node.attrs.get("data-preloaded"):
                preloaded_attr = node.attrs["data-preloaded"]
            if not pre_text and node.tag == "pre":
                parts: list[str] = []
                _collect_pre_text(node, parts)
                pre_text = "".join(parts)
            if node.tag == "a":
                href = node.attrs.get("href", "").strip()
                if "/t/" in href:
                    lbl_parts: list[str] = []
                    _collect_pre_text(node, lbl_parts)
                    label = "".join(lbl_parts).strip()
                    if label:
                        topic_links.append((href, label))
            stack.extend(reversed(node.children))

        if pre_text:
            for cand in (pre_text, decode_html_entities(pre_text)):
                if isinstance(parsed := _try_parse_json_text(cand), dict):
                    return parsed
        if preloaded_attr:
            if preloaded := _unwrap_discourse_preloaded(preloaded_attr):
                return preloaded
        stripped = strip_html_and_decode(html)
        if isinstance(parsed := _try_parse_json_text(stripped), dict):
            return parsed
    else:
        topic_links = []

    tid = extract_topic_id(request_url)
    title = str(extract_data.get("title") or "").strip()
    if tid > 0:
        cooked = html
        if not cooked and isinstance(extract_data.get("paragraphs"), list):
            cooked = "".join(
                f"<p>{html_lib.escape(str(p.get('original') if isinstance(p, Mapping) else p))}</p>"
                for p in extract_data["paragraphs"]
                if str(p.get("original") if isinstance(p, Mapping) else p).strip()
            )
        if not cooked and isinstance(extract_data.get("text"), str) and extract_data["text"].strip():
            cooked = f"<p>{html_lib.escape(extract_data['text'].strip())}</p>"
        if cooked or title:
            return {
                "id": tid,
                "title": title or f"Linux.do 话题 #{tid}",
                "posts_count": 1,
                "post_stream": {
                    "posts": [{"id": tid * 10 + 1, "post_number": 1, "username": "linuxdo", "cooked": cooked}],
                    "stream": [tid * 10 + 1],
                },
            }
    elif topic_links:
        topics: list[dict[str, Any]] = []
        seen_tids: set[int] = set()
        for href, label in topic_links:
            link_tid = extract_topic_id(href)
            if link_tid <= 0 or link_tid in seen_tids:
                continue
            seen_tids.add(link_tid)
            topics.append({"id": link_tid, "title": label, "posts_count": 1})
        if topics:
            return {"topic_list": {"topics": topics}}
    return None
