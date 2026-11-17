"""Untrusted Discourse cooked HTML parser preserving headings, code blocks, quotes, lists, tables, and math."""

from __future__ import annotations

import html as std_html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from omnidesk_sdk import TextParagraph

if __package__:
    from .shuiyuan_security import DEFAULT_BASE_URL, resolve_url
else:
    from shuiyuan_security import DEFAULT_BASE_URL, resolve_url

MAX_HTML_CHARS = 8 * 1024 * 1024
_MAX_TREE_DEPTH = 256
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_BLOCK_TAGS = frozenset({
    "p", "div", "pre", "blockquote", "aside", "ul", "ol", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "details", "hr", "section", "article",
})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
_CODE_LANG_RE = re.compile(r"^(?:lang|language)-([A-Za-z0-9_+#.-]+)$", re.IGNORECASE)


@dataclass(slots=True)
class _DomNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    classes: frozenset[str] = field(default_factory=frozenset)
    children: list[_DomNode | str] = field(default_factory=list)
    parent: _DomNode | None = None


class _DomBuilder(HTMLParser):
    """Stdlib HTMLParser building a bounded DOM tree safely on malformed input."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _DomNode(tag="root")
        self._stack: list[_DomNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if len(self._stack) >= _MAX_TREE_DEPTH:
            return
        attr_map: dict[str, str] = {}
        for k, v in attrs:
            if k:
                attr_map[k.lower()] = "" if v is None else v
        classes = frozenset(tok for tok in attr_map.get("class", "").split() if tok)
        node = _DomNode(tag=t, attrs=attr_map, classes=classes, parent=self._stack[-1])
        self._stack[-1].children.append(node)
        if t not in _VOID_TAGS:
            self._stack.append(node)

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

    def handle_entityref(self, name: str) -> None:
        self._stack[-1].children.append(decode_html_entities(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._stack[-1].children.append(decode_html_entities(f"&#{name};"))


def sanitize_utf8_text(text: str) -> str:
    """Replace lone UTF-16 surrogates and strip NUL bytes so JSON serialization never fails."""
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if cp == 0:
            continue
        if 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            out.append("\ufffd")
        else:
            out.append(ch)
    return "".join(out)


def decode_html_entities(text: str) -> str:
    """Decode HTML entities safely while replacing unpaired surrogates and NULs."""
    if not text:
        return ""
    decoded = std_html.unescape(text)
    decoded = decoded.replace("\xa0", " ")
    return sanitize_utf8_text(decoded)


def _normalize_inline_ws(text: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", text).strip()


def _clean_multiline_text(text: str) -> str:
    lines: list[str] = []
    last_blank = True
    for raw_line in text.splitlines():
        cleaned = raw_line.strip()
        if not cleaned:
            if not last_blank and lines:
                lines.append("")
                last_blank = True
        else:
            lines.append(cleaned)
            last_blank = False
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _is_block_formula(text: str) -> str | None:
    t = text.strip()
    if len(t) >= 4 and ((t.startswith("$$") and t.endswith("$$")) or (t.startswith(r"\[") and t.endswith(r"\]"))):
        return t
    return None


def _collect_pre_text(node: _DomNode | str, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
        return
    if node.tag == "br":
        out.append("\n")
        return
    for ch in node.children:
        _collect_pre_text(ch, out)


def _collect_inline_text(node: _DomNode | str, base_url: str, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
        return
    tag = node.tag
    if tag in _IGNORED_TAGS:
        return
    if "meta" in node.classes and "informations" in node.classes:
        return
    if tag == "br":
        out.append("\n")
        return
    if tag == "img":
        if "emoji" in node.classes:
            alt = node.attrs.get("alt") or node.attrs.get("title") or ""
            out.append(alt)
            return
        if "avatar" in node.classes:
            return
        alt = node.attrs.get("alt", "").strip()
        src = resolve_url(base_url, node.attrs.get("src", "")) if base_url else node.attrs.get("src", "").strip()
        if alt:
            out.append(f"[图片: {alt}]")
        elif src:
            out.append(f"[图片: {src}]")
        else:
            out.append("[图片]")
        return
    if "math" in node.classes:
        parts: list[str] = []
        _collect_pre_text(node, parts)
        raw = "".join(parts).strip()
        if raw:
            out.append(raw if raw.startswith("$") else f"${raw}$")
        return
    if tag == "code" and (node.parent is None or node.parent.tag != "pre"):
        parts = []
        _collect_pre_text(node, parts)
        out.append(f"`{''.join(parts).strip()}`")
        return
    if tag == "a":
        href = node.attrs.get("href", "").strip()
        inner_parts: list[str] = []
        for ch in node.children:
            _collect_inline_text(ch, base_url, inner_parts)
        inner = "".join(inner_parts).strip()
        if "lightbox" in node.classes:
            resolved = resolve_url(base_url, href) or href
            out.append(inner or f"[图片: {resolved}]")
            return
        if inner:
            out.append(inner)
            if (
                href
                and not href.startswith("#")
                and "mention" not in node.classes
                and "http" not in inner
                and "[图片" not in inner
            ):
                resolved = resolve_url(base_url, href)
                if resolved and resolved != inner:
                    out.append(f" ({resolved})")
        elif href:
            out.append(resolve_url(base_url, href) or href)
        return
    for ch in node.children:
        _collect_inline_text(ch, base_url, out)


def _collect_list_text(list_node: _DomNode, *, ordered: bool, base_url: str) -> str:
    lines: list[str] = []
    idx = 1
    for ch in list_node.children:
        if not isinstance(ch, _DomNode) or ch.tag != "li":
            continue
        parts: list[str] = []
        _collect_inline_text(ch, base_url, parts)
        item_txt = _clean_multiline_text("".join(parts))
        if item_txt:
            prefix = f"{idx}. " if ordered else "• "
            if ordered:
                idx += 1
            lines.append(f"{prefix}{item_txt}")
    return "\n".join(lines)


def _collect_table_text(table_node: _DomNode, base_url: str) -> str:
    rows: list[str] = []
    queue: list[_DomNode] = [table_node]
    while queue:
        cur = queue.pop(0)
        if cur.tag == "tr":
            cells: list[str] = []
            for ch in cur.children:
                if isinstance(ch, _DomNode) and ch.tag in {"th", "td"}:
                    parts: list[str] = []
                    _collect_inline_text(ch, base_url, parts)
                    cells.append(_normalize_inline_ws("".join(parts)))
            if cells:
                rows.append(" | ".join(cells))
            continue
        for ch in cur.children:
            if isinstance(ch, _DomNode):
                queue.append(ch)
    return "\n".join(rows)


def _extract_code_language(pre_node: _DomNode) -> str:
    for cls in pre_node.classes:
        m = _CODE_LANG_RE.match(cls)
        if m and m.group(1).lower() != "auto":
            return m.group(1).lower()
    for ch in pre_node.children:
        if isinstance(ch, _DomNode) and ch.tag == "code":
            for cls in ch.classes:
                m = _CODE_LANG_RE.match(cls)
                if m and m.group(1).lower() != "auto":
                    return m.group(1).lower()
    return ""


def _push_paragraph(out: list[TextParagraph], kind: str, text: str, *, language: str = "") -> None:
    cleaned = text.strip() if kind in {"code", "formula"} else _clean_multiline_text(text)
    if not cleaned:
        return
    out.append(
        TextParagraph(
            id=f"p{len(out) + 1}",
            kind=kind,  # type: ignore[arg-type]
            original=sanitize_utf8_text(cleaned),
            language=language,
        )
    )


def _walk_block_node(node: _DomNode | str, base_url: str, out: list[TextParagraph]) -> None:
    if isinstance(node, str):
        t = _normalize_inline_ws(node)
        if t:
            formula = _is_block_formula(t)
            _push_paragraph(out, "formula" if formula else "paragraph", formula or t)
        return

    tag = node.tag
    if tag in _IGNORED_TAGS or tag == "hr":
        return

    if "lightbox-wrapper" in node.classes:
        parts: list[str] = []
        _collect_inline_text(node, base_url, parts)
        _push_paragraph(out, "paragraph", "".join(parts))
        return

    if tag in _HEADING_TAGS:
        parts = []
        _collect_inline_text(node, base_url, parts)
        _push_paragraph(out, "heading", _normalize_inline_ws("".join(parts)))
        return

    if tag == "pre":
        parts = []
        _collect_pre_text(node, parts)
        code = sanitize_utf8_text("".join(parts).strip("\r\n"))
        if code:
            out.append(
                TextParagraph(
                    id=f"p{len(out) + 1}",
                    kind="code",
                    original=code,
                    language=_extract_code_language(node),
                )
            )
        return

    if "math" in node.classes:
        parts = []
        _collect_pre_text(node, parts)
        math_raw = "".join(parts).strip()
        if math_raw:
            if not math_raw.startswith("$") and not math_raw.startswith(r"\["):
                math_raw = f"$$\n{math_raw}\n$$"
            _push_paragraph(out, "formula", math_raw)
        return

    if tag == "aside" and "quote" in node.classes:
        quote_parts: list[str] = []
        for ch in node.children:
            if not isinstance(ch, _DomNode):
                continue
            parts = []
            _collect_inline_text(ch, base_url, parts)
            part = _clean_multiline_text("".join(parts))
            if part:
                quote_parts.append(part)
        _push_paragraph(out, "quote", "\n".join(quote_parts))
        return

    if (tag == "aside" and "onebox" in node.classes) or tag == "blockquote":
        parts = []
        _collect_inline_text(node, base_url, parts)
        _push_paragraph(out, "quote", "".join(parts))
        return

    if tag in {"ul", "ol"}:
        _push_paragraph(out, "paragraph", _collect_list_text(node, ordered=(tag == "ol"), base_url=base_url))
        return

    if tag == "table":
        _push_paragraph(out, "paragraph", _collect_table_text(node, base_url))
        return

    if tag == "p":
        parts = []
        _collect_inline_text(node, base_url, parts)
        txt = "".join(parts)
        formula = _is_block_formula(txt)
        _push_paragraph(out, "formula" if formula else "paragraph", formula or txt)
        return

    inline_buf: list[str] = []
    for ch in node.children:
        is_block = isinstance(ch, _DomNode) and (ch.tag in _BLOCK_TAGS or "math" in ch.classes)
        if is_block:
            buf_txt = "".join(inline_buf)
            if buf_txt.strip():
                formula = _is_block_formula(buf_txt)
                _push_paragraph(out, "formula" if formula else "paragraph", formula or buf_txt)
            inline_buf.clear()
            _walk_block_node(ch, base_url, out)
        else:
            _collect_inline_text(ch, base_url, inline_buf)
    buf_txt = "".join(inline_buf)
    if buf_txt.strip():
        formula = _is_block_formula(buf_txt)
        _push_paragraph(out, "formula" if formula else "paragraph", formula or buf_txt)


def _find_body_node(root: _DomNode) -> _DomNode:
    for ch in root.children:
        if isinstance(ch, _DomNode) and ch.tag == "html":
            for sub in ch.children:
                if isinstance(sub, _DomNode) and sub.tag == "body":
                    return sub
        if isinstance(ch, _DomNode) and ch.tag == "body":
            return ch
    return root


def strip_html_and_decode(raw_html: str) -> str:
    """Strip HTML tags and decode entities into normalized single-line text."""
    if not raw_html or not raw_html.strip():
        return ""
    builder = _DomBuilder()
    builder.feed(sanitize_utf8_text(raw_html[:MAX_HTML_CHARS]))
    builder.close()
    parts: list[str] = []
    _collect_inline_text(builder.root, "", parts)
    return _normalize_inline_ws(decode_html_entities("".join(parts)))


def parse_cooked_to_paragraphs(raw_html: str, base_url: str = DEFAULT_BASE_URL) -> list[TextParagraph]:
    """Parse Discourse `cooked` HTML into structured `TextParagraph` blocks."""
    if not raw_html or not raw_html.strip():
        return []
    builder = _DomBuilder()
    builder.feed(sanitize_utf8_text(raw_html[:MAX_HTML_CHARS]))
    builder.close()
    out: list[TextParagraph] = []
    _walk_block_node(_find_body_node(builder.root), base_url, out)
    return out


def parse_cooked_to_comment_text(raw_html: str, base_url: str = DEFAULT_BASE_URL) -> str:
    """Render Discourse reply `cooked` HTML to readable markdown-friendly comment text."""
    paras = parse_cooked_to_paragraphs(raw_html, base_url)
    if not paras:
        return strip_html_and_decode(raw_html)
    blocks: list[str] = []
    for p in paras:
        if p.kind == "code":
            fence = f"```{p.language}" if p.language else "```"
            blocks.append(f"{fence}\n{p.original}\n```")
        elif p.kind == "quote":
            blocks.append("\n".join(f"> {line}" for line in p.original.splitlines()))
        else:
            blocks.append(p.original)
    return "\n\n".join(blocks).strip()
