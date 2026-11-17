"""Anna's Archive HTML, based on official aarecord and partner-download templates.

A small tree preserves sibling metadata, nested text and HTML void elements.
Source: software.annas-archive.gl/AnnaArchivist/annas-archive (2026-11-17).
"""
from __future__ import annotations

import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

_MD5_HREF_RE = re.compile(r'^/md5/([a-fA-F0-9]{32})/?$')
_FORMAT_TOKEN_RE = re.compile(r'\b(pdf|epub|mobi|azw3|txt|djvu|fb2|cbr|cbz)\b', re.I)
_SIZE_TOKEN_RE = re.compile(r'\b(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|KiB|MiB|GiB))\b', re.I)
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def clean_html_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def parse_meta_badges(text: str) -> dict[str, str]:
    fmt, size = _FORMAT_TOKEN_RE.search(text), _SIZE_TOKEN_RE.search(text)
    year = re.search(r'\b(1[5-9]\d{2}|20\d{2})\b', text)
    lang = re.search(r'([\w -]+)\s*\[([a-z]{2,3}(?:-[A-Za-z]+)?)\]', text)
    if not lang:
        lang = re.search(r'\b(English|Chinese|Japanese|French|German|Spanish|Russian|中文)\b', text, re.I)
    return {'language': clean_html_text(lang.group(1)) if lang else '',
            'format': fmt.group(1).lower() if fmt else '', 'size': size.group(1) if size else '',
            'year': year.group(1) if year else ''}


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {'script', 'style'} or 'select-none' in (self.attrs.get('class') or ''):
            return ''
        return clean_html_text(' '.join(child.text() if isinstance(child, Node) else child for child in self.children))

    def has(self, token):
        return token in (self.attrs.get('class') or '')


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)

    def handle_comment(self, data):
        # Anna wraps deferred result cards in comments. Never execute their JS.
        if '/md5/' in data and '<' in data:
            child = Document()
            child.feed(data)
            for node in child.root.children:
                if isinstance(node, Node):
                    node.parent = self.current
                self.current.children.append(node)


def md5_link(node):
    if node.tag != 'a':
        return ''
    match = _MD5_HREF_RE.fullmatch(urllib.parse.urlsplit(node.attrs.get('href') or '').path)
    return match.group(1).lower() if match else ''


def first_text(root, predicate):
    return next((n.text() for n in root.walk() if predicate(n) and n.text()), '')


def metadata(root):
    lines = [n.text() for n in root.walk() if n.has('text-gray-500') or n.has('text-gray-800') or n.has('dark:text-slate-400')]
    return parse_meta_badges(' · '.join(lines))


class AnnasSearchHTMLParser(Document):
    @property
    def items(self):
        found = {}
        for link in self.root.walk():
            md5 = md5_link(link)
            if not md5:
                continue
            root = link
            # New cards have separate cover/title/author links; old cards wrap everything.
            while root.parent is not None and root.parent.tag not in ('body', 'html', ''):
                parent = root.parent
                ids = {md5_link(n) for n in parent.walk()} - {''}
                if ids != {md5}:
                    break
                root = parent
                if root.has('border-b') or root.tag == 'tr':
                    break
            title = first_text(root, lambda n: n.tag == 'h3' or (md5_link(n) == md5 and (n.has('font-semibold') or n.has('font-bold'))))
            title = title or link.attrs.get('data-title') or first_text(root, lambda n: n.has('font-bold')) or link.text()
            if not title:
                continue
            author = first_text(root, lambda n: n.has('italic') or n.attrs.get('itemprop') == 'author' or (n.tag == 'a' and any(c.has('mdi--user-edit') for c in n.walk())))
            publisher = first_text(root, lambda n: n.tag == 'a' and any(c.has('mdi--company') for c in n.walk()))
            entry = {'md5': md5, 'href': link.attrs.get('href'), 'title': title, 'author': author, 'publisher': publisher, **metadata(root)}
            for field in ('title', 'author', 'format', 'size', 'year', 'language'):
                entry[field] = link.attrs.get('data-' + field) or entry.get(field, '')
            previous = found.get(md5)
            if previous is None or sum(bool(v) for v in entry.values()) > sum(bool(v) for v in previous.values()):
                found[md5] = entry
        return list(found.values())


class AnnasDetailHTMLParser(Document):
    @property
    def meta(self):
        title = first_text(self.root, lambda n: n.has('text-2xl') and n.has('font-semibold')) or first_text(self.root, lambda n: n.tag == 'h1' or n.has('text-3xl'))
        if not title:
            title = next((n.attrs.get('content', '') for n in self.root.walk() if n.tag == 'meta' and n.attrs.get('property') == 'og:title'), '')
        author = first_text(self.root, lambda n: n.has('italic') or n.attrs.get('itemprop') == 'author' or (n.tag == 'a' and any(c.has('mdi--user-edit') for c in n.walk())))
        publisher = first_text(self.root, lambda n: n.tag == 'a' and any(c.has('mdi--company') for c in n.walk()))
        links, seen = [], set()
        for n in self.root.walk():
            href = n.attrs.get('href') or ''
            if n.tag != 'a' or not href or href in seen:
                continue
            path = urllib.parse.urlsplit(href).path
            match = re.fullmatch(r'/(fast_download|slow_download)/([a-fA-F0-9]{32})/(\d+)/(\d+)/?', path)
            if match:
                kind = 'fast' if match.group(1) == 'fast_download' else 'slow'
                links.append({'kind': kind, 'download_token': href, 'label': n.text() or kind,
                              'md5': match.group(2).lower(), 'path_index': int(match.group(3)), 'domain_index': int(match.group(4))})
            elif path.startswith('/dl/') or 'download' in n.attrs:
                links.append({'kind': 'direct', 'download_token': href, 'label': n.text() or '文件链接'})
            elif 'nofollow' in (n.attrs.get('rel') or '').split() or n.has('js-download-link'):
                links.append({'kind': 'external', 'download_token': href, 'label': n.text() or '外部来源'})
            seen.add(href)
        badges = metadata(self.root)
        return {'title': title, 'author': author, 'publisher': publisher, **badges,
                'description': first_text(self.root, lambda n: n.has('js-md5-top-box-description') or n.has('book-description')),
                'download_options': links}


def page_state(body: bytes) -> dict:
    text = body.decode('utf-8', errors='replace')
    doc = Document(); doc.feed(text)
    heading = first_text(doc.root, lambda n: n.tag in {'title', 'h1', 'h2'}).lower()
    if any(marker in text.lower() for marker in ('cf-chl-', 'cf-turnstile', 'challenges.cloudflare.com', 'just a moment', 'ddos-guard')) or any(marker in heading for marker in ('verify you are human', 'browser verification', '浏览器验证')):
        return {'state': 'verification_required', 'message': 'Anna 页面要求浏览器验证，请在 Chrome 完成后重试'}
    wait = re.search(r'\b(?:let|var|const)\s+waitSeconds\s*=\s*(\d+)', text)
    if wait and int(wait.group(1)) > 0:
        return {'state': 'waiting', 'wait_seconds': int(wait.group(1)), 'message': '慢速下载正在排队，请等待倒计时结束后重试'}
    return {'state': 'ready'}


class SlowDownloadLinkParser(Document):
    @property
    def links(self):
        links = []
        for n in self.root.walk():
            href = n.attrs.get('href') or ''
            path = urllib.parse.urlsplit(href).path
            label = n.text().lower()
            if n.tag == 'a' and href and not path.startswith(('/md5/', '/search', '/donate', '/faq')) and (
                'download' in n.attrs or n.has('js-download-link') or 'download' in label or '下载' in label
            ):
                links.append(href)
            # Official partner pages sometimes expose a copy-only URL.
            onclick = n.attrs.get('onclick') or ''
            match = re.search(r'clipboard\.writeText\(("(?:[^"\\]|\\.)*")\)', onclick)
            if match:
                value = json.loads(match.group(1))
                if value.startswith(('https://', 'http://')):
                    links.append(value)
        return list(dict.fromkeys(links))
