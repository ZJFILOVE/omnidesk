"""RSS/Atom, feed-discovery, and readable HTML article parsing for the blog plugin."""

from __future__ import annotations

import html as html_mod
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

_DOCTYPE_OR_ENTITY_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class BlogParseError(ValueError):
    """Raised when a feed or page cannot be parsed."""

    def __init__(self, message: str, *, code: str = "invalid_response") -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class BlogFeedEntry:
    id: str
    title: str
    url: str = ""
    author: str = ""
    published_at: str = ""
    summary: str = ""
    content_html: str = ""
    tags: list[str] = field(default_factory=list)
    cover_url: str = ""


@dataclass(slots=True)
class BlogFeed:
    title: str = ""
    site_url: str = ""
    entries: list[BlogFeedEntry] = field(default_factory=list)


@dataclass(slots=True)
class BlogArticle:
    title: str
    url: str = ""
    author: str = ""
    published_at: str = ""
    paragraphs: list[tuple[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    cover_url: str = ""
    site_name: str = ""
    summary: str = ""


def strip_markup(value: str) -> str:
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", value or ""))).strip()


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1]


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return _WS_RE.sub(" ", "".join(node.itertext())).strip()


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    want = name.lower()
    return [child for child in parent if _local_name(child.tag).lower() == want]


def _first(parent: ET.Element | None, *names: str) -> ET.Element | None:
    if parent is None:
        return None
    for name in names:
        matches = _children(parent, name)
        if matches:
            return matches[0]
    return None


def _first_text(parent: ET.Element | None, *names: str) -> str:
    for name in names:
        text = _text(_first(parent, name))
        if text:
            return text
    return ""


def _safe_xml(xml_text: str) -> ET.Element:
    cleaned = (xml_text or "").strip()
    if not cleaned:
        raise BlogParseError("Feed 内容为空")
    if _DOCTYPE_OR_ENTITY_RE.search(cleaned):
        raise BlogParseError("安全限制: 拒绝包含 DOCTYPE/ENTITY 的 XML", code="validation_error")
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise BlogParseError(f"Feed XML 解析失败: {exc}") from None


def _atom_link(node: ET.Element) -> str:
    fallback = ""
    for link in _children(node, "link"):
        href = (link.attrib.get("href") or "").strip()
        if not href:
            continue
        rel = (link.attrib.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


def parse_feed(xml_text: str, *, source_url: str = "") -> BlogFeed:
    """Parse RSS 2.0, Atom 1.0, and RDF feeds."""
    root = _safe_xml(xml_text)
    root_name = _local_name(root.tag).lower()
    feed = BlogFeed()
    if root_name == "feed":
        feed.title = strip_markup(_first_text(root, "title"))
        feed.site_url = _atom_link(root) or source_url
        nodes = [node for node in root if _local_name(node.tag).lower() == "entry"]
        is_atom = True
    elif root_name == "rdf":
        channel = _first(root, "channel")
        feed.title = strip_markup(_first_text(channel, "title"))
        feed.site_url = _first_text(channel, "link") or source_url
        nodes = [node for node in root if _local_name(node.tag).lower() == "item"]
        is_atom = False
    else:
        channel = _first(root, "channel") or root
        feed.title = strip_markup(_first_text(channel, "title"))
        feed.site_url = _first_text(channel, "link") or source_url
        nodes = _children(channel, "item")
        is_atom = False

    for node in nodes:
        title = strip_markup(_first_text(node, "title"))
        if not title:
            continue
        url = _atom_link(node) if is_atom else _first_text(node, "link", "guid")
        if not url:
            url = _first_text(node, "guid", "id")
        if url and source_url:
            url = urllib.parse.urljoin(source_url, url)
        summary = strip_markup(_first_text(node, "summary", "description", "encoded", "content"))
        content_html = _first_text(node, "encoded", "content", "description")
        author = _first_text(node, "creator", "author")
        author_node = _first(node, "author")
        if author_node is not None:
            name = _first_text(author_node, "name")
            if name:
                author = name
        published = _first_text(node, "published", "updated", "pubDate", "date")
        tags = [
            strip_markup(_text(category))
            for category in _children(node, "category")
            if strip_markup(_text(category))
        ]
        cover = ""
        for enclosure in _children(node, "enclosure"):
            media_type = (enclosure.attrib.get("type") or "").lower()
            link = (enclosure.attrib.get("url") or "").strip()
            if link and (media_type.startswith("image/") or not cover):
                cover = link
        for media in _children(node, "thumbnail"):
            cover = (media.attrib.get("url") or cover).strip()
        feed.entries.append(
            BlogFeedEntry(
                id=url or _first_text(node, "guid", "id") or title,
                title=title,
                url=url,
                author=author,
                published_at=published,
                summary=summary,
                content_html=content_html,
                tags=tags,
                cover_url=cover,
            )
        )
    return feed


@dataclass(slots=True)
class _DomNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[_DomNode] = field(default_factory=list)


_VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input", "source", "area", "base", "col"})
_DROP_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "iframe"})


class _DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _DomNode("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _DomNode(tag.lower(), {name.lower(): (value or "") for name, value in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS and len(self.stack) < 256:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _DomNode(tag.lower(), {name.lower(): (value or "") for name, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == name:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        parent = self.stack[-1]
        if parent.children and parent.children[-1].tag == "#text":
            parent.children[-1].text += data
        else:
            parent.children.append(_DomNode("#text", text=data))


def _parse_dom(html_text: str) -> _DomNode:
    builder = _DomBuilder()
    try:
        builder.feed(html_text or "")
        builder.close()
    except Exception:
        pass
    return builder.root


def _walk(node: _DomNode) -> list[_DomNode]:
    out = [node]
    for child in node.children:
        out.extend(_walk(child))
    return out


def _node_text(node: _DomNode) -> str:
    if node.tag == "#text":
        return node.text
    if node.tag in _DROP_TAGS:
        return ""
    return "".join(_node_text(child) for child in node.children)


def discover_feed_links(html_text: str, base_url: str) -> list[str]:
    """Find RSS/Atom `<link rel="alternate">` and obvious feed anchors."""
    root = _parse_dom(html_text)
    found: list[str] = []
    for node in _walk(root):
        if node.tag == "link":
            rel = node.attrs.get("rel", "").lower()
            kind = node.attrs.get("type", "").lower()
            href = node.attrs.get("href", "").strip()
            if href and "alternate" in rel and any(token in kind for token in ("rss", "atom", "xml")):
                found.append(urllib.parse.urljoin(base_url, href))
        elif node.tag == "a":
            href = node.attrs.get("href", "").strip()
            lowered = href.lower()
            if href and any(token in lowered for token in (".rss", ".atom", "feed.xml", "/feed", "/rss", "/atom")):
                found.append(urllib.parse.urljoin(base_url, href))
    return _dedupe_urls(found)


def discover_article_links(html_text: str, base_url: str, *, limit: int = 40) -> list[tuple[str, str]]:
    """Extract plausible article links from a blog homepage without a feed."""
    root = _parse_dom(html_text)
    base_host = urllib.parse.urlsplit(base_url).hostname or ""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in _walk(root):
        if node.tag != "a":
            continue
        href = node.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != base_host:
            continue
        path = parsed.path or "/"
        if path in {"", "/"} or any(
            token in path.lower()
            for token in ("/tag/", "/tags/", "/category/", "/categories/", "/page/", "/about", "/contact", "/feed")
        ):
            continue
        clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
        if clean in seen:
            continue
        title = strip_markup(_node_text(node))
        if len(title) < 4:
            continue
        seen.add(clean)
        results.append((clean, title))
        if len(results) >= limit:
            break
    return results


def parse_html_article(html_text: str, *, url: str = "") -> BlogArticle:
    """Extract a readable article from HTML."""
    root = _parse_dom(html_text)
    nodes = _walk(root)

    def meta(names: tuple[str, ...]) -> str:
        for node in nodes:
            if node.tag != "meta":
                continue
            key = (node.attrs.get("property") or node.attrs.get("name") or "").lower()
            if key in names:
                value = node.attrs.get("content", "").strip()
                if value:
                    return value
        return ""

    title = meta(("og:title", "twitter:title"))
    if not title:
        title = strip_markup(_node_text(next((node for node in nodes if node.tag == "title"), _DomNode("#text"))))
    if not title:
        title = strip_markup(_node_text(next((node for node in nodes if node.tag == "h1"), _DomNode("#text"))))
    author = meta(("author", "article:author", "twitter:creator"))
    if not author:
        for node in nodes:
            if node.attrs.get("rel", "").lower() == "author":
                author = strip_markup(_node_text(node))
                if author:
                    break
    published = meta(("article:published_time", "date", "pubdate", "og:published_time"))
    if not published:
        for node in nodes:
            if node.tag == "time" and node.attrs.get("datetime"):
                published = node.attrs["datetime"].strip()
                break
    cover = meta(("og:image", "twitter:image"))
    site_name = meta(("og:site_name", "application-name"))

    candidates = [node for node in nodes if node.tag in {"article", "main"} or node.attrs.get("itemprop") == "articleBody"]
    if candidates:
        content_root = candidates[0]
    else:
        body = next((node for node in nodes if node.tag == "body"), root)
        content_root = body
        best_score = 0
        for node in nodes:
            if node.tag not in {"div", "section"}:
                continue
            score = sum(len(_node_text(child)) for child in node.children if child.tag == "p")
            if score > best_score:
                content_root, best_score = node, score

    paragraphs: list[tuple[str, str]] = []
    block_tags = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre"}

    def visit(node: _DomNode) -> None:
        if node.tag in _DROP_TAGS:
            return
        if node.tag in block_tags:
            text = strip_markup(_node_text(node))
            if text:
                kind = (
                    "heading"
                    if node.tag.startswith("h")
                    else "code"
                    if node.tag == "pre"
                    else "list"
                    if node.tag == "li"
                    else "quote"
                    if node.tag == "blockquote"
                    else "paragraph"
                )
                paragraphs.append((kind, text))
            return
        for child in node.children:
            visit(child)

    visit(content_root)
    if not paragraphs:
        text = strip_markup(_node_text(content_root))
        if text:
            paragraphs = [("paragraph", text)]
    summary = " ".join(text for kind, text in paragraphs if kind == "paragraph")[:400]
    return BlogArticle(
        title=title or url,
        url=url,
        author=author,
        published_at=published,
        paragraphs=paragraphs,
        cover_url=cover,
        site_name=site_name,
        summary=summary,
    )


def html_title(html_text: str) -> str:
    """Extract a page title without a full article parse."""
    root = _parse_dom(html_text)
    for node in _walk(root):
        if node.tag == "meta" and (node.attrs.get("property") or "").lower() == "og:title":
            value = node.attrs.get("content", "").strip()
            if value:
                return value
    for node in _walk(root):
        if node.tag == "title":
            value = strip_markup(_node_text(node))
            if value:
                return value
    return ""


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        parsed = urllib.parse.urlsplit(url)
        clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
