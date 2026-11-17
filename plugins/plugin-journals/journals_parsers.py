"""Offline parsers for arXiv Atom, RSS/Atom feeds, bioRxiv, PubMed, Crossref, and HTML."""

from __future__ import annotations

import html as html_mod
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

_DOCTYPE_OR_ENTITY_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_JATS_RE = re.compile(r"</?(?:jats:)?[A-Za-z][^>]*>")


class JournalParseError(ValueError):
    """Raised when an upstream payload cannot be safely parsed."""

    def __init__(self, message: str, *, code: str = "invalid_response") -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class FeedEntry:
    """Normalized paper/preprint/blog record."""

    id: str
    title: str
    url: str = ""
    author: str = ""
    published_at: str = ""
    summary: str = ""
    doi: str = ""
    journal: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FeedDocument:
    """Parsed feed metadata plus its entries."""

    title: str = ""
    site_url: str = ""
    entries: list[FeedEntry] = field(default_factory=list)


def strip_markup(value: str) -> str:
    """Strip HTML/JATS markup and normalize whitespace."""
    text = _JATS_RE.sub(" ", value or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html_mod.unescape(text)).strip()


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


def _first(parent: ET.Element, *names: str) -> ET.Element | None:
    for name in names:
        matches = _children(parent, name)
        if matches:
            return matches[0]
    return None


def _first_text(parent: ET.Element, *names: str) -> str:
    for name in names:
        node = _first(parent, name)
        text = _text(node)
        if text:
            return text
    return ""


def _safe_xml(xml_text: str) -> ET.Element:
    cleaned = (xml_text or "").strip()
    if not cleaned:
        raise JournalParseError("上游 XML 内容为空")
    if _DOCTYPE_OR_ENTITY_RE.search(cleaned):
        raise JournalParseError("安全限制: 拒绝包含 DOCTYPE/ENTITY 的 XML", code="validation_error")
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise JournalParseError(f"XML 解析失败: {exc}") from None


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for link in _children(entry, "link"):
        href = (link.attrib.get("href") or "").strip()
        if not href:
            continue
        rel = (link.attrib.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def _entry_tags(entry: ET.Element) -> list[str]:
    tags: list[str] = []
    for category in _children(entry, "category"):
        value = (category.attrib.get("term") or category.attrib.get("label") or _text(category)).strip()
        if value and value not in tags:
            tags.append(value)
    return tags


def parse_arxiv(xml_text: str, *, max_results: int = 20) -> FeedDocument:
    """Parse the arXiv export Atom response."""
    root = _safe_xml(xml_text)
    doc = FeedDocument(title="arXiv", site_url="https://arxiv.org")
    for entry in [node for node in root.iter() if _local_name(node.tag).lower() == "entry"][:max_results]:
        raw_id = _first_text(entry, "id")
        title = strip_markup(_first_text(entry, "title"))
        if not title:
            continue
        match = re.search(r"arxiv\.org/abs/([^/\s]+?)(?:v\d+)?$", raw_id)
        arxiv_id = match.group(1) if match else raw_id.rsplit("/", 1)[-1]
        doi = _first_text(entry, "doi")
        authors = [
            _first_text(author, "name")
            for author in _children(entry, "author")
            if _first_text(author, "name")
        ]
        published = _first_text(entry, "published", "updated")
        summary = strip_markup(_first_text(entry, "summary"))
        url = _atom_link(entry) or f"https://arxiv.org/abs/{arxiv_id}"
        doc.entries.append(
            FeedEntry(
                id=f"arxiv:{arxiv_id}",
                title=title,
                url=url,
                author=", ".join(authors),
                published_at=published,
                summary=summary,
                doi=doi,
                journal="arXiv",
                tags=_entry_tags(entry),
                source="arxiv",
                extra={"arxiv_id": arxiv_id, "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}"},
            )
        )
    return doc


def parse_rss(xml_text: str, *, source: str = "rss", journal: str = "") -> FeedDocument:
    """Parse RSS 2.0, Atom 1.0, or RDF feed documents."""
    root = _safe_xml(xml_text)
    root_name = _local_name(root.tag).lower()
    doc = FeedDocument()
    if root_name == "feed":
        doc.title = strip_markup(_first_text(root, "title"))
        doc.site_url = _atom_link(root)
        nodes = [node for node in root if _local_name(node.tag).lower() == "entry"]
    elif root_name == "rdf":
        channel = _first(root, "channel")
        doc.title = strip_markup(_first_text(channel, "title")) if channel is not None else ""
        doc.site_url = _first_text(channel, "link") if channel is not None else ""
        nodes = [node for node in root if _local_name(node.tag).lower() == "item"]
    else:
        channel = _first(root, "channel") or root
        doc.title = strip_markup(_first_text(channel, "title"))
        doc.site_url = _first_text(channel, "link")
        nodes = _children(channel, "item")

    for node in nodes:
        title = strip_markup(_first_text(node, "title"))
        if not title:
            continue
        link = _atom_link(node) or _first_text(node, "link", "guid")
        raw_id = _first_text(node, "guid", "id") or link
        summary = strip_markup(
            _first_text(node, "description", "summary", "encoded", "content")
        )
        doi = _first_text(node, "doi")
        if not doi:
            match = re.search(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b", f"{link} {raw_id}")
            doi = match.group(1) if match else ""
        authors = _first_text(node, "creator", "author")
        if isinstance(_first(node, "author"), ET.Element):
            author_node = _first(node, "author")
            if author_node is not None:
                authors = authors or _first_text(author_node, "name")
        published = _first_text(node, "pubDate", "published", "updated", "date")
        tags = [
            strip_markup(_text(category))
            for category in _children(node, "category")
            if strip_markup(_text(category))
        ]
        doc.entries.append(
            FeedEntry(
                id=f"doi:{doi}" if doi else link or raw_id,
                title=title,
                url=link,
                author=authors,
                published_at=published,
                summary=summary,
                doi=doi,
                journal=journal or doc.title,
                tags=tags,
                source=source,
            )
        )
    return doc


def parse_biorxiv(payload: str, *, server: str = "biorxiv") -> FeedDocument:
    """Parse the Cold Spring Harbor bioRxiv/medRxiv details JSON response."""
    try:
        doc = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise JournalParseError(f"bioRxiv JSON 解析失败: {exc}") from None
    if not isinstance(doc, Mapping):
        raise JournalParseError("bioRxiv 响应格式无效")
    collection = doc.get("collection")
    if not isinstance(collection, list):
        raise JournalParseError("bioRxiv 响应缺少 collection 数组")
    out = FeedDocument(title=server, site_url=f"https://www.{server}.org")
    for item in collection:
        if not isinstance(item, Mapping):
            continue
        doi = str(item.get("doi") or "").strip()
        title = strip_markup(str(item.get("title") or ""))
        if not title:
            continue
        version = str(item.get("version") or "").strip()
        url = f"https://www.{server}.org/content/{doi}v{version}" if doi else ""
        out.entries.append(
            FeedEntry(
                id=f"doi:{doi}" if doi else url,
                title=title,
                url=url,
                author=str(item.get("authors") or "").strip(),
                published_at=str(item.get("date") or "").strip(),
                summary=strip_markup(str(item.get("abstract") or "")),
                doi=doi,
                journal=str(item.get("category") or server).strip(),
                tags=[str(item.get("category") or "").strip()] if item.get("category") else [],
                source=server,
                extra={"version": version, "type": item.get("type")},
            )
        )
    return out


def parse_pubmed(xml_text: str) -> list[FeedEntry]:
    """Parse PubMed EFetch XML into normalized entries with structured abstracts."""
    root = _safe_xml(xml_text)
    entries: list[FeedEntry] = []
    for article_node in [node for node in root.iter() if _local_name(node.tag) == "PubmedArticle"]:
        citation = _first(article_node, "MedlineCitation")
        if citation is None:
            continue
        pmid = _first_text(citation, "PMID")
        article = _first(citation, "Article")
        if article is None:
            continue
        title = strip_markup(_first_text(article, "ArticleTitle"))
        if not title:
            continue
        journal_node = _first(article, "Journal")
        journal = ""
        published = ""
        if journal_node is not None:
            journal = strip_markup(_first_text(journal_node, "Title", "ISOAbbreviation"))
            issue = _first(journal_node, "JournalIssue")
            pub_date = _first(issue, "PubDate") if issue is not None else None
            if pub_date is not None:
                year = _first_text(pub_date, "Year")
                month = _first_text(pub_date, "Month")
                day = _first_text(pub_date, "Day")
                published = "-".join(part for part in (year, month, day) if part) or _first_text(
                    pub_date, "MedlineDate"
                )
        authors: list[str] = []
        author_list = _first(article, "AuthorList")
        if author_list is not None:
            for author in _children(author_list, "Author"):
                collective = _first_text(author, "CollectiveName")
                if collective:
                    authors.append(collective)
                    continue
                last = _first_text(author, "LastName")
                fore = _first_text(author, "ForeName", "Initials")
                name = " ".join(part for part in (fore, last) if part)
                if name:
                    authors.append(name)
        abstract_parts: list[str] = []
        abstract = _first(article, "Abstract")
        if abstract is not None:
            for segment in _children(abstract, "AbstractText"):
                label = (segment.attrib.get("Label") or "").strip()
                text = strip_markup(_text(segment))
                if text:
                    abstract_parts.append(f"{label}: {text}" if label else text)
        doi = ""
        article_ids = _first(article_node, "ArticleIdList")
        if article_ids is not None:
            for article_id in _children(article_ids, "ArticleId"):
                if (article_id.attrib.get("IdType") or "").lower() == "doi":
                    doi = _text(article_id)
                    break
        if not doi:
            for eloc in _children(article, "ELocationID"):
                if (eloc.attrib.get("EIdType") or "").lower() == "doi":
                    doi = _text(eloc)
                    break
        tags = []
        keyword_list = _first(citation, "KeywordList")
        if keyword_list is not None:
            tags = [strip_markup(_text(node)) for node in _children(keyword_list, "Keyword")]
        entries.append(
            FeedEntry(
                id=f"pubmed:{pmid}",
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                author=", ".join(authors),
                published_at=published,
                summary="\n\n".join(abstract_parts),
                doi=doi,
                journal=journal,
                tags=[tag for tag in tags if tag],
                source="pubmed",
                extra={"pmid": pmid},
            )
        )
    return entries


def parse_crossref(payload: str, *, identifier: str = "") -> FeedEntry:
    """Parse a Crossref work payload into one normalized paper record."""
    try:
        doc = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise JournalParseError(f"Crossref JSON 解析失败: {exc}") from None
    message = doc.get("message") if isinstance(doc, Mapping) else None
    if not isinstance(message, Mapping):
        raise JournalParseError("Crossref 响应缺少 message 对象")

    def _joined(value: Any) -> str:
        if isinstance(value, list):
            return " ".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "").strip()

    authors: list[str] = []
    raw_authors = message.get("author")
    if isinstance(raw_authors, list):
        for item in raw_authors:
            if not isinstance(item, Mapping):
                continue
            name = " ".join(
                part for part in (str(item.get("given") or "").strip(), str(item.get("family") or "").strip()) if part
            ) or str(item.get("name") or "").strip()
            if name:
                authors.append(name)
    title = strip_markup(_joined(message.get("title")))
    doi = str(message.get("DOI") or identifier.replace("doi:", "")).strip()
    if not title:
        raise JournalParseError("Crossref 响应缺少标题")
    date_parts = message.get("published", {}).get("date-parts") if isinstance(message.get("published"), Mapping) else None
    published = ""
    if isinstance(date_parts, Sequence) and date_parts and isinstance(date_parts[0], Sequence):
        published = "-".join(str(part) for part in date_parts[0] if part not in (None, ""))
    journal = _joined(message.get("container-title")) or _joined(message.get("publisher"))
    subjects = message.get("subject")
    tags = [str(item).strip() for item in subjects if str(item).strip()] if isinstance(subjects, list) else []
    return FeedEntry(
        id=f"doi:{doi}" if doi else identifier,
        title=title,
        url=str(message.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
        author=", ".join(authors),
        published_at=published,
        summary=strip_markup(str(message.get("abstract") or "")),
        doi=doi,
        journal=journal,
        tags=tags,
        source="crossref",
        extra={
            "type": message.get("type"),
            "publisher": message.get("publisher"),
            "volume": message.get("volume"),
            "issue": message.get("issue"),
            "page": message.get("page"),
            "citation_count": message.get("is-referenced-by-count", 0),
        },
    )


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
        node = _DomNode(
            tag.lower(),
            {name.lower(): (value or "") for name, value in attrs},
        )
        self.stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS and len(self.stack) < 256:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS and len(self.stack) > 1:
            self.stack.pop()

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


def _page_paragraphs(html_text: str) -> tuple[str, list[tuple[str, str]], str]:
    builder = _DomBuilder()
    try:
        builder.feed(html_text or "")
        builder.close()
    except Exception:
        pass
    nodes = _walk(builder.root)
    title = ""
    for node in nodes:
        if node.tag == "meta" and node.attrs.get("property", "").lower() in {"og:title", "twitter:title"}:
            title = node.attrs.get("content", "").strip()
            if title:
                break
    if not title:
        for node in nodes:
            if node.tag == "title":
                title = strip_markup(_node_text(node))
                break
    if not title:
        for node in nodes:
            if node.tag == "h1":
                title = strip_markup(_node_text(node))
                break

    cover = ""
    for node in nodes:
        if node.tag == "meta" and node.attrs.get("property", "").lower() in {"og:image", "twitter:image"}:
            cover = node.attrs.get("content", "").strip()
            if cover:
                break

    roots = [node for node in nodes if node.tag in {"article", "main"}]
    body = next((node for node in nodes if node.tag == "body"), builder.root)
    if not roots:
        best = body
        best_score = 0
        for node in nodes:
            if node.tag not in {"div", "section"}:
                continue
            score = sum(len(_node_text(child)) for child in node.children if child.tag == "p")
            if score > best_score:
                best, best_score = node, score
        roots = [best]
    root = roots[0]

    paragraphs: list[tuple[str, str]] = []
    block_tags = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre"}

    def visit(node: _DomNode) -> None:
        if node.tag in _DROP_TAGS:
            return
        if node.tag in block_tags:
            text = strip_markup(_node_text(node))
            if text:
                kind = "heading" if node.tag.startswith("h") else "code" if node.tag == "pre" else "list" if node.tag == "li" else "quote" if node.tag == "blockquote" else "paragraph"
                paragraphs.append((kind, text))
            return
        for child in node.children:
            visit(child)

    visit(root)
    if not paragraphs:
        text = strip_markup(_node_text(root))
        if text:
            paragraphs = [("paragraph", text)]
    return title, paragraphs, cover


def parse_html_article(html_text: str, *, url: str = "") -> FeedEntry:
    """Extract a readable article record from an HTML page."""
    title, paragraphs, cover = _page_paragraphs(html_text)
    if not title and url:
        title = url
    return FeedEntry(
        id=url or title,
        title=title,
        url=url,
        summary="\n\n".join(text for _, text in paragraphs[:3]),
        source="web",
        extra={"paragraphs": paragraphs, "cover_url": cover},
    )
