"""Tolerant HTML5 DOM parser, CSS selector engine, readability extractor, and TextParagraph mapper."""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from omnidesk_sdk import TextParagraph

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_NOISE_HINTS = ("sidebar", "navbar", "cookie", "footer", "share-buttons", "related-posts")
_FORMULA_CLASSES = ("katex", "mathjax", "mjx-", "math-display")
_MAX_HTML_CHARS = 8 * 1024 * 1024
_MAX_DOM_DEPTH = 256
_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def decode_html_entities(text: str) -> str:
    return html_mod.unescape(text or "")


@dataclass(slots=True)
class DomNode:
    """Lightweight HTML DOM node (`#document`, `#text`, or element tag)."""

    tag: str  # "#document", "#text", or lowercase element name
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[DomNode] = field(default_factory=list)
    parent: DomNode | None = None

    @property
    def is_element(self) -> bool:
        return not self.tag.startswith("#")

    @property
    def is_text(self) -> bool:
        return self.tag == "#text"


class _DomBuilder(HTMLParser):
    """Error-tolerant HTMLParser constructing a bounded DomNode tree."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = DomNode(tag="#document")
        self._stack: list[DomNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs if k}
        parent = self._stack[-1]
        if t == "li" and parent.tag == "li":
            self._stack.pop()
            parent = self._stack[-1]
        elif t == "p" and parent.tag == "p":
            self._stack.pop()
            parent = self._stack[-1]

        node = DomNode(tag=t, attrs=attr_map, parent=parent)
        parent.children.append(node)
        if t not in _VOID_TAGS and len(self._stack) < _MAX_DOM_DEPTH:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs if k}
        parent = self._stack[-1]
        parent.children.append(DomNode(tag=t, attrs=attr_map, parent=parent))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            return
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        parent = self._stack[-1]
        if parent.children and parent.children[-1].is_text:
            parent.children[-1].text += data
        else:
            parent.children.append(DomNode(tag="#text", text=data, parent=parent))


class HtmlDocument:
    """Parsed HTML5-tolerant DOM tree mirroring v1 HtmlDocument API."""

    def __init__(self, html_text: str) -> None:
        builder = _DomBuilder()
        builder.feed((html_text or "")[:_MAX_HTML_CHARS])
        builder.close()
        self.root: DomNode = builder.root

    def body(self) -> DomNode:
        bodies = select_nodes(self.root, "body")
        return bodies[0] if bodies else self.root

    def title(self) -> str:
        titles = select_nodes(self.root, "title")
        return node_inner_text(titles[0]) if titles else ""


def _element_children(parent: DomNode | None) -> list[DomNode]:
    if parent is None:
        return []
    return [c for c in parent.children if c.is_element]


def node_attribute(node: DomNode | None, attr_name: str) -> str:
    if node is None or not node.is_element or not attr_name:
        return ""
    return node.attrs.get(attr_name.lower(), "")


def _collect_text_recursive(node: DomNode | None, acc: list[str], preserve_ws: bool) -> None:
    if node is None:
        return
    if node.is_text:
        acc.append(node.text)
        return
    if not node.is_element:
        for ch in node.children:
            _collect_text_recursive(ch, acc, preserve_ws)
        return
    tag = node.tag
    if tag in _IGNORED_TAGS:
        return
    if tag == "br":
        acc.append("\n")
        return
    child_preserve = preserve_ws or tag in {"pre", "code"}
    for ch in node.children:
        _collect_text_recursive(ch, acc, child_preserve)
    if not child_preserve and (
        tag in {"p", "div", "li", "blockquote", "tr"} or (len(tag) == 2 and tag[0] == "h")
    ):
        if acc and not acc[-1].endswith((" ", "\n")):
            acc.append(" ")


def node_inner_text(node: DomNode | None) -> str:
    parts: list[str] = []
    _collect_text_recursive(node, parts, False)
    return normalize_ws("".join(parts))


def node_own_text(node: DomNode | None) -> str:
    if node is None or not node.is_element:
        return ""
    return normalize_ws("".join(ch.text for ch in node.children if ch.is_text))


def _serialize_node_html(node: DomNode | None, out: list[str], outer: bool, depth: int = 0) -> None:
    if node is None or depth > _MAX_DOM_DEPTH:
        return
    if node.is_text:
        out.append(node.text)
        return
    if outer and node.is_element:
        attrs_str = "".join(
            f' {k}="{html_mod.escape(v, quote=True)}"' for k, v in node.attrs.items()
        )
        out.append(f"<{node.tag}{attrs_str}>")
    if node.is_element and node.tag in _VOID_TAGS:
        return
    for ch in node.children:
        _serialize_node_html(ch, out, True, depth + 1)
    if outer and node.is_element:
        out.append(f"</{node.tag}>")


def node_inner_html(node: DomNode | None) -> str:
    if node is None:
        return ""
    out: list[str] = []
    _serialize_node_html(node, out, False)
    return "".join(out)


@dataclass(slots=True)
class _AttrCond:
    name: str
    op: str = "exists"  # exists | = | ^= | $= | *= | ~=
    value: str = ""


@dataclass(slots=True)
class _PseudoCond:
    kind: str  # first-child | last-child | nth-child
    a: int = 0
    b: int = 1


@dataclass(slots=True)
class _SimpleSelector:
    tag: str = ""
    id: str = ""
    classes: list[str] = field(default_factory=list)
    attrs: list[_AttrCond] = field(default_factory=list)
    pseudos: list[_PseudoCond] = field(default_factory=list)


@dataclass(slots=True)
class _SelectorStep:
    comb: str = ""  # "" | " " | ">" | "+" | "~"
    simple: _SimpleSelector = field(default_factory=_SimpleSelector)


def _strip_quotes(s: str) -> str:
    t = s.strip()
    if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
        return t[1:-1]
    return t


def _parse_nth_expr(raw: str) -> _PseudoCond:
    s = re.sub(r"\s+", "", raw.lower())
    if s == "odd":
        return _PseudoCond("nth-child", 2, 1)
    if s == "even":
        return _PseudoCond("nth-child", 2, 0)
    if "n" not in s:
        try:
            return _PseudoCond("nth-child", 0, int(s))
        except ValueError:
            return _PseudoCond("nth-child", 0, -1)
    a_str, b_str = s.split("n", 1)
    if a_str in {"", "+"}:
        a = 1
    elif a_str == "-":
        a = -1
    else:
        try:
            a = int(a_str)
        except ValueError:
            a = 0
    try:
        b = int(b_str) if b_str else 0
    except ValueError:
        b = 0
    return _PseudoCond("nth-child", a, b)


def _split_selector_comma(sel: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    bracket = 0
    paren = 0
    quote = ""
    for ch in sel:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in {"'", '"'}:
            quote = ch
            cur.append(ch)
            continue
        if ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "," and bracket == 0 and paren == 0:
            part = "".join(cur).strip()
            if part:
                out.append(part)
            cur.clear()
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _parse_selector_steps(raw: str) -> list[_SelectorStep]:
    steps: list[_SelectorStep] = []
    i = 0
    n = len(raw)
    pending_comb = ""

    while i < n:
        saw_space = False
        while i < n and raw[i].isspace():
            saw_space = True
            i += 1
        if i >= n:
            break

        if raw[i] in {">", "+", "~"}:
            if steps:
                pending_comb = raw[i]
            i += 1
            while i < n and raw[i].isspace():
                i += 1
            if i >= n:
                break
        elif saw_space and steps and not pending_comb:
            pending_comb = " "

        step = _SelectorStep(comb=pending_comb)
        pending_comb = ""

        while i < n:
            c = raw[i]
            if c.isspace() or c in {">", "+", "~"}:
                break
            if c == "#":
                i += 1
                start = i
                while i < n and (raw[i].isalnum() or raw[i] in {"-", "_"}):
                    i += 1
                step.simple.id = raw[start:i]
            elif c == ".":
                i += 1
                start = i
                while i < n and (raw[i].isalnum() or raw[i] in {"-", "_"}):
                    i += 1
                cls_name = raw[start:i]
                if cls_name:
                    step.simple.classes.append(cls_name)
            elif c == "[":
                i += 1
                start = i
                q = ""
                while i < n and (q or raw[i] != "]"):
                    if q:
                        if raw[i] == q:
                            q = ""
                    elif raw[i] in {"'", '"'}:
                        q = raw[i]
                    i += 1
                inside = raw[start:i].strip()
                if i < n and raw[i] == "]":
                    i += 1
                if "=" not in inside:
                    if inside:
                        step.simple.attrs.append(_AttrCond(name=inside.lower(), op="exists"))
                else:
                    lhs, rhs = inside.split("=", 1)
                    lhs = lhs.strip()
                    op = "="
                    if lhs and lhs[-1] in {"^", "$", "*", "~"}:
                        op = f"{lhs[-1]}="
                        lhs = lhs[:-1].strip()
                    if lhs:
                        step.simple.attrs.append(_AttrCond(name=lhs.lower(), op=op, value=_strip_quotes(rhs)))
            elif c == ":":
                i += 1
                start = i
                while i < n and (raw[i].isalnum() or raw[i] in {"-", "_"}):
                    i += 1
                pname = raw[start:i].lower()
                parg = ""
                if i < n and raw[i] == "(":
                    i += 1
                    astart = i
                    while i < n and raw[i] != ")":
                        i += 1
                    parg = raw[astart:i]
                    if i < n and raw[i] == ")":
                        i += 1
                if pname == "first-child":
                    step.simple.pseudos.append(_PseudoCond("first-child"))
                elif pname == "last-child":
                    step.simple.pseudos.append(_PseudoCond("last-child"))
                elif pname == "nth-child":
                    step.simple.pseudos.append(_parse_nth_expr(parg))
            else:
                start = i
                while i < n and (raw[i].isalnum() or raw[i] in {"-", "_", "*"}):
                    i += 1
                if i == start:
                    i += 1
                else:
                    step.simple.tag = raw[start:i].lower()
        steps.append(step)
    return steps


def _matches_simple(node: DomNode, sel: _SimpleSelector) -> bool:
    if not node.is_element:
        return False
    if sel.tag and sel.tag != "*" and node.tag != sel.tag:
        return False
    if sel.id and node.attrs.get("id", "") != sel.id:
        return False
    if sel.classes:
        tokens = set(node.attrs.get("class", "").split())
        if not all(need in tokens for need in sel.classes):
            return False
    for cond in sel.attrs:
        if cond.name not in node.attrs:
            return False
        val = node.attrs[cond.name]
        if cond.op == "exists":
            continue
        if cond.op == "=" and val != cond.value:
            return False
        if cond.op == "^=" and not val.startswith(cond.value):
            return False
        if cond.op == "$=" and not val.endswith(cond.value):
            return False
        if cond.op == "*=" and cond.value not in val:
            return False
        if cond.op == "~=" and cond.value not in val.split():
            return False
    if sel.pseudos:
        siblings = _element_children(node.parent)
        try:
            index1 = siblings.index(node) + 1
        except ValueError:
            return False
        for p in sel.pseudos:
            if p.kind == "first-child" and index1 != 1:
                return False
            if p.kind == "last-child" and index1 != len(siblings):
                return False
            if p.kind == "nth-child":
                if p.a == 0:
                    if index1 != p.b:
                        return False
                else:
                    diff = index1 - p.b
                    if diff % p.a != 0 or diff // p.a < 0:
                        return False
    return True


def _matches_steps_at(node: DomNode, scope_root: DomNode, steps: list[_SelectorStep], step_idx: int) -> bool:
    if not _matches_simple(node, steps[step_idx].simple):
        return False
    if step_idx == 0:
        return True
    comb = steps[step_idx].comb
    prev_idx = step_idx - 1
    stop_parent = scope_root.parent

    if comb == ">":
        parent = node.parent
        if parent is None or parent is stop_parent or not parent.is_element:
            return False
        return _matches_steps_at(parent, scope_root, steps, prev_idx)
    if comb == " ":
        cur = node.parent
        while cur is not None and cur is not stop_parent and cur.is_element:
            if _matches_steps_at(cur, scope_root, steps, prev_idx):
                return True
            cur = cur.parent
        return False
    if comb in {"+", "~"}:
        siblings = _element_children(node.parent)
        try:
            pos = siblings.index(node)
        except ValueError:
            return False
        if pos == 0:
            return False
        if comb == "+":
            return _matches_steps_at(siblings[pos - 1], scope_root, steps, prev_idx)
        return any(_matches_steps_at(siblings[k], scope_root, steps, prev_idx) for k in range(pos - 1, -1, -1))
    return False


def select_nodes(root: DomNode | None, selector: str) -> list[DomNode]:
    """Match CSS selector against DomNode subtree (preserving document order, deduplicated)."""
    if root is None or not (selector or "").strip():
        return []
    out: list[DomNode] = []
    seen: set[int] = set()
    for sub in _split_selector_comma(selector):
        steps = _parse_selector_steps(sub)
        if not steps:
            continue
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur.is_element and _matches_steps_at(cur, root, steps, len(steps) - 1):
                nid = id(cur)
                if nid not in seen:
                    seen.add(nid)
                    out.append(cur)
            for ch in reversed(cur.children):
                if ch.is_element:
                    stack.append(ch)
    return out


def _split_extractor_suffix(expr: str) -> tuple[str, str]:
    raw = (expr or "").strip()
    attr = "text"
    bracket = 0
    paren = 0
    quote = ""
    at_pos = -1
    for i, c in enumerate(raw):
        if quote:
            if c == quote:
                quote = ""
            continue
        if c in {"'", '"'}:
            quote = c
            continue
        if c == "[":
            bracket += 1
        elif c == "]":
            bracket = max(0, bracket - 1)
        elif c == "(":
            paren += 1
        elif c == ")":
            paren = max(0, paren - 1)
        elif c == "@" and bracket == 0 and paren == 0:
            at_pos = i
    if at_pos != -1:
        attr = raw[at_pos + 1 :].strip()
        raw = raw[:at_pos].strip()
        if attr.startswith("attr(") and attr.endswith(")"):
            attr = attr[5:-1].strip()
    return raw, attr


def _extract_from_node(node: DomNode | None, attr: str) -> str:
    if node is None:
        return ""
    a = (attr or "").strip().lower()
    if not a or a == "text":
        return node_inner_text(node)
    if a == "own_text":
        return node_own_text(node)
    if a == "html":
        return node_inner_html(node)
    return node_attribute(node, a).strip()


def extract_value(root: DomNode | None, selector_with_attr: str) -> str:
    """Evaluate selector with optional @text / @own_text / @html / @<attr> / @attr(name)."""
    if root is None:
        return ""
    sel, attr = _split_extractor_suffix(selector_with_attr)
    if not sel:
        return _extract_from_node(root, attr)
    matches = select_nodes(root, sel)
    return _extract_from_node(matches[0], attr) if matches else ""


def extract_all_values(root: DomNode | None, selector_with_attr: str) -> list[str]:
    if root is None:
        return []
    sel, attr = _split_extractor_suffix(selector_with_attr)
    if not sel:
        v = _extract_from_node(root, attr)
        return [v] if v else []
    out: list[str] = []
    for node in select_nodes(root, sel):
        v = _extract_from_node(node, attr)
        if v:
            out.append(v)
    return out


def strip_html_tags_to_text(html_fragment: str) -> str:
    """Strip HTML markup and decode entities into normalized plain text."""
    if not html_fragment:
        return ""
    if "<" not in html_fragment:
        return normalize_ws(decode_html_entities(html_fragment))
    doc = HtmlDocument(f"<body>{html_fragment}</body>")
    return node_inner_text(doc.body())


def _contains_formula_hint(node: DomNode) -> bool:
    cls = node_attribute(node, "class").lower()
    if any(hint in cls for hint in _FORMULA_CLASSES):
        return True
    if node.tag == "img" and bool(node_attribute(node, "data-formula").strip()):
        return True
    if bool(node_attribute(node, "data-tex").strip()) or bool(node_attribute(node, "alttext").strip()):
        return True
    return False


def _extract_formula_tex(node: DomNode) -> str:
    for attr in ("alttext", "data-tex", "data-formula"):
        val = node_attribute(node, attr).strip()
        if val:
            return val
    annotations = select_nodes(node, 'annotation[encoding*="tex"], annotation')
    for ann in annotations:
        txt = node_inner_text(ann)
        if txt:
            return txt
    return node_inner_text(node)


def _contains_noise_hint(node: DomNode) -> bool:
    idcls = (node_attribute(node, "id") + " " + node_attribute(node, "class")).lower()
    return any(hint in idcls for hint in _NOISE_HINTS)


def extract_readability_html(full_html: str, preferred_selector: str = "") -> str:
    """Extract main readable article HTML block matching v1 Readability scoring."""
    doc = HtmlDocument(full_html)
    body = doc.body()
    if body is None:
        return full_html

    def _try_sel(sel: str) -> str:
        if not sel.strip():
            return ""
        best: DomNode | None = None
        best_len = 0
        for node in select_nodes(body, sel):
            tlen = len(node_inner_text(node))
            if tlen > best_len:
                best = node
                best_len = tlen
        return node_inner_html(best) if (best is not None and best_len >= 40) else ""

    if preferred_selector:
        picked = _try_sel(preferred_selector)
        if picked:
            return picked

    for common in ("article", "main", "[role=main]", ".post-content", ".entry-content", ".article-content", "#content"):
        picked = _try_sel(common)
        if picked:
            return picked

    best_node = body
    best_score = 0.0
    for candidate in select_nodes(body, "div, section"):
        if _contains_noise_hint(candidate):
            continue
        paras = select_nodes(candidate, "p, pre, blockquote")
        p_text = sum(len(node_inner_text(p)) for p in paras)
        if p_text < 60:
            continue
        total_text = max(1, len(node_inner_text(candidate)))
        links = sum(len(node_inner_text(a)) for a in select_nodes(candidate, "a"))
        link_density = links / float(total_text)
        if link_density > 0.55:
            continue
        score = float(p_text) * (1.0 - link_density) + float(len(paras)) * 25.0
        if score > best_score:
            best_score = score
            best_node = candidate
    return node_inner_html(best_node)


def html_to_paragraphs(html_fragment: str, base_url: str = "") -> list[TextParagraph]:
    """Convert HTML fragment into structured `TextParagraph` blocks preserving headings, code, quotes, and formulas."""
    _ = base_url
    if not (html_fragment or "").strip():
        return []
    doc = HtmlDocument(f"<body>{html_fragment}</body>")
    body = doc.body()
    out: list[TextParagraph] = []
    counter = 0

    def _push(kind: str, text: str) -> None:
        nonlocal counter
        cleaned = text.strip() if kind in {"code", "formula"} else normalize_ws(text)
        if not cleaned:
            return
        counter += 1
        out.append(TextParagraph(id=f"p_{counter}", kind=kind, original=cleaned, translated=""))

    def _walk(node: DomNode, depth: int = 0) -> None:
        if depth > _MAX_DOM_DEPTH or not node.is_element:
            return
        tag = node.tag
        if tag in _IGNORED_TAGS or tag in {"nav", "footer", "aside", "header"}:
            return
        if _contains_noise_hint(node):
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            _push("heading", node_inner_text(node))
            return
        if tag == "pre":
            parts: list[str] = []
            _collect_text_recursive(node, parts, True)
            _push("code", "".join(parts))
            return
        if tag == "blockquote":
            _push("quote", node_inner_text(node))
            return
        if tag == "math" or _contains_formula_hint(node):
            _push("formula", _extract_formula_tex(node))
            return
        if tag in {"p", "li", "figcaption"}:
            el_kids = _element_children(node)
            if len(el_kids) == 1 and el_kids[0].tag == "code" and not node_own_text(node):
                parts = []
                _collect_text_recursive(el_kids[0], parts, True)
                code_text = "".join(parts)
                if "\n" in code_text or len(code_text) > 64:
                    _push("code", code_text)
                    return
            if len(el_kids) == 1 and (el_kids[0].tag == "math" or _contains_formula_hint(el_kids[0])) and not node_own_text(node):
                _push("formula", _extract_formula_tex(el_kids[0]))
                return
            txt = node_inner_text(node)
            if tag == "p" and txt.startswith("$$") and txt.endswith("$$") and len(txt) > 4:
                _push("formula", txt[2:-2].strip())
                return
            if tag == "li" and txt:
                txt = f"• {txt}"
            _push("paragraph", txt)
            return

        loose: list[str] = []
        for child in node.children:
            if child.is_text:
                loose.append(child.text)
            elif child.is_element:
                ctag = child.tag
                if ctag in {"a", "span", "strong", "em", "b", "i", "code", "small", "img"}:
                    if _contains_formula_hint(child):
                        _push("paragraph", "".join(loose))
                        loose.clear()
                        _walk(child, depth + 1)
                    elif ctag != "img":
                        loose.append(" ")
                        loose.append(node_inner_text(child))
                else:
                    if len(normalize_ws("".join(loose))) >= 20:
                        _push("paragraph", "".join(loose))
                    loose.clear()
                    _walk(child, depth + 1)
        if len(normalize_ws("".join(loose))) >= 20:
            _push("paragraph", "".join(loose))

    _walk(body)
    if not out:
        fallback = node_inner_text(body)
        if fallback:
            _push("paragraph", fallback)
    return out


__all__ = [
    "DomNode",
    "HtmlDocument",
    "decode_html_entities",
    "extract_all_values",
    "extract_readability_html",
    "extract_value",
    "html_to_paragraphs",
    "node_attribute",
    "node_inner_html",
    "node_inner_text",
    "node_own_text",
    "normalize_ws",
    "select_nodes",
    "strip_html_tags_to_text",
]
