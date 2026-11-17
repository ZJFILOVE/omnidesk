"""Safe RSS 2.0, Atom 1.0, RDF 1.0, and simple XPath XML feed parser."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

if __package__:
    from .html_css import decode_html_entities, strip_html_tags_to_text
    from .security import FeedRecipeError
else:
    from html_css import decode_html_entities, strip_html_tags_to_text
    from security import FeedRecipeError

_DOCTYPE_OR_ENTITY_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_NS_PREFIX_RE = re.compile(r"^\{[^}]+\}")


@dataclass(slots=True)
class XmlFeedEntry:
    """Normalized entry extracted from RSS, Atom, RDF, or XPath XML feed."""

    id: str = ""
    title: str = ""
    url: str = ""
    author: str = ""
    published_at: str = ""
    summary: str = ""
    content_html: str = ""
    categories: list[str] = field(default_factory=list)


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return _NS_PREFIX_RE.sub("", tag)
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def _full_text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _children_by_name(parent: ET.Element, name: str) -> list[ET.Element]:
    want_local = _local_name(name).lower()
    want_exact = name.lower()
    out: list[ET.Element] = []
    for child in parent:
        tag = child.tag
        loc = _local_name(tag).lower()
        if ":" in name:
            # Support prefixed names like dc:creator or content:encoded
            prefix, lname = name.split(":", 1)
            if loc == lname.lower() and (prefix.lower() in tag.lower() or tag.startswith("{")):
                out.append(child)
        elif loc == want_local or tag.lower() == want_exact:
            out.append(child)
    return out


def _first_child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        for ch in _children_by_name(node, name):
            txt = _full_text(ch)
            if txt:
                return txt
    return ""


def _extract_atom_link(entry: ET.Element) -> str:
    fallback = ""
    for link in _children_by_name(entry, "link"):
        href = (link.attrib.get("href") or "").strip()
        if not href:
            inline = _full_text(link)
            if inline:
                return inline
            continue
        rel = (link.attrib.get("rel") or "").strip().lower()
        if not rel or rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def _safe_parse_xml(xml_text: str) -> ET.Element:
    cleaned = (xml_text or "").strip()
    if not cleaned:
        raise FeedRecipeError("validation_error", "XML 响应内容为空")
    if _DOCTYPE_OR_ENTITY_RE.search(cleaned):
        raise FeedRecipeError("validation_error", "安全限制: 拒绝包含 DOCTYPE/ENTITY 的 XML")
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise FeedRecipeError("validation_error", f"XML 解析失败: {exc}") from None


def _eval_simple_xpath_nodes(root: ET.Element, expr: str) -> list[ET.Element]:
    path = (expr or "").strip()
    if not path:
        return []
    if path.startswith("//"):
        target = path[2:].split("/", 1)[0].strip()
        rest = path[2:].split("/", 1)[1] if "/" in path[2:] else ""
        seeds = [el for el in root.iter() if _local_name(el.tag).lower() == _local_name(target).lower()]
        if not rest:
            return seeds
        path = rest
    else:
        seeds = [root]

    parts = [p.strip() for p in path.split("/") if p.strip()]
    cur = seeds
    for part in parts:
        nxt: list[ET.Element] = []
        for el in cur:
            nxt.extend(_children_by_name(el, part))
        cur = nxt
    return cur


def _eval_xpath_str(node: ET.Element, expr: str) -> str:
    if not expr:
        return ""
    for alt in expr.split("|"):
        cand = alt.strip()
        if not cand:
            continue
        if cand.startswith("@"):
            val = (node.attrib.get(cand[1:].strip()) or "").strip()
            if val:
                return val
            continue
        if "/@" in cand:
            elem_path, attr_name = cand.rsplit("/@", 1)
            hits = _eval_simple_xpath_nodes(node, elem_path)
            for hit in hits:
                val = (hit.attrib.get(attr_name.strip()) or "").strip()
                if val:
                    return val
            continue
        hits = _eval_simple_xpath_nodes(node, cand)
        for hit in hits:
            txt = _full_text(hit)
            if txt:
                return txt
    return ""


def _truncate_summary(raw_html: str) -> str:
    text = strip_html_tags_to_text(raw_html)
    if len(text) > 240:
        return text[:237] + "..."
    return text


def parse_xml_feed(
    xml_text: str,
    max_items: int = 50,
    item_xpath: str = "",
    field_xpaths: Mapping[str, Any] | None = None,
) -> list[XmlFeedEntry]:
    """Parse RSS 2.0, Atom 1.0, RDF 1.0, or custom XPath XML feed (fail-closed on malformed XML)."""
    root = _safe_parse_xml(xml_text)
    limit = max_items if max_items > 0 else 50
    fmap = dict(field_xpaths) if field_xpaths else {}
    out: list[XmlFeedEntry] = []

    if item_xpath.strip():
        for node in _eval_simple_xpath_nodes(root, item_xpath):
            if len(out) >= limit:
                break
            entry = XmlFeedEntry(
                id=_eval_xpath_str(node, str(fmap.get("id") or "guid|id")),
                title=decode_html_entities(_eval_xpath_str(node, str(fmap.get("title") or "title"))),
                url=_eval_xpath_str(node, str(fmap.get("url") or "link/@href|link")),
                author=_eval_xpath_str(node, str(fmap.get("author") or "author/name|author|dc:creator")),
                published_at=_eval_xpath_str(node, str(fmap.get("published_at") or "published|updated|pubDate|dc:date")),
                summary=_truncate_summary(_eval_xpath_str(node, str(fmap.get("excerpt") or "summary|description"))),
                content_html=_eval_xpath_str(node, str(fmap.get("content") or "content:encoded|content|description|summary")),
            )
            if not entry.id:
                entry.id = entry.url or entry.title
            if entry.title or entry.url:
                out.append(entry)
        return out

    root_local = _local_name(root.tag).lower()
    if root_local == "feed":
        for entry_el in _children_by_name(root, "entry"):
            if len(out) >= limit:
                break
            title = decode_html_entities(_first_child_text(entry_el, ("title",)))
            url = _extract_atom_link(entry_el)
            eid = _first_child_text(entry_el, ("id",)) or url
            author = ""
            authors = _children_by_name(entry_el, "author")
            if authors:
                author = _first_child_text(authors[0], ("name",)) or _full_text(authors[0])
            if not author:
                author = _first_child_text(entry_el, ("dc:creator",))
            published_at = _first_child_text(entry_el, ("published", "updated", "dc:date"))
            content_html = _first_child_text(entry_el, ("content", "summary"))
            summary = _truncate_summary(_first_child_text(entry_el, ("summary", "content")))
            cats: list[str] = []
            for cat in _children_by_name(entry_el, "category"):
                term = (cat.attrib.get("term") or "").strip() or _full_text(cat)
                if term:
                    cats.append(term)
            if title or url:
                out.append(
                    XmlFeedEntry(
                        id=eid,
                        title=title,
                        url=url,
                        author=author,
                        published_at=published_at,
                        summary=summary,
                        content_html=content_html,
                        categories=cats,
                    )
                )
        return out

    if root_local == "rss":
        channels = _children_by_name(root, "channel")
        item_parent = channels[0] if channels else root
    elif root_local == "rdf":
        item_parent = root
    else:
        item_parent = root

    for item_el in _children_by_name(item_parent, "item"):
        if len(out) >= limit:
            break
        title = decode_html_entities(_first_child_text(item_el, ("title",)))
        url = _first_child_text(item_el, ("link",))
        eid = _first_child_text(item_el, ("guid", "link"))
        author = strip_html_tags_to_text(_first_child_text(item_el, ("dc:creator", "author")))
        published_at = _first_child_text(item_el, ("pubDate", "dc:date", "published", "updated"))
        content_html = _first_child_text(item_el, ("content:encoded", "description", "summary"))
        summary = _truncate_summary(_first_child_text(item_el, ("description", "summary", "content:encoded")))
        cats = [txt for cat in _children_by_name(item_el, "category") if (txt := _full_text(cat))]
        if title or url:
            out.append(
                XmlFeedEntry(
                    id=eid or url or title,
                    title=title,
                    url=url,
                    author=author,
                    published_at=published_at,
                    summary=summary,
                    content_html=content_html,
                    categories=cats,
                )
            )
    return out


def query_xml_xpath(xml_text: str, xpath_expr: str) -> list[str]:
    """Evaluate a simple XPath-like query against XML text."""
    root = _safe_parse_xml(xml_text)
    if "/@" in xpath_expr:
        elem_path, attr_name = xpath_expr.rsplit("/@", 1)
        return [
            val
            for el in _eval_simple_xpath_nodes(root, elem_path)
            if (val := (el.attrib.get(attr_name.strip()) or "").strip())
        ]
    return [txt for el in _eval_simple_xpath_nodes(root, xpath_expr) if (txt := _full_text(el))]


__all__ = ["XmlFeedEntry", "parse_xml_feed", "query_xml_xpath"]
