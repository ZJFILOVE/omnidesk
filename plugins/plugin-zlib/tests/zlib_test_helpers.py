"""Offline mock HTTP server and test fixture helpers for plugin-zlib tests."""

from __future__ import annotations

import http.server
import io
import json
import threading
import time
import urllib.parse
import zipfile
from contextlib import contextmanager
from typing import Any, Iterator


def make_pdf_bytes(tag: str = "sample") -> bytes:
    return f"%PDF-1.7\n% {tag}\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n".encode("utf-8")


def make_epub_bytes(title: str = "Sample EPUB") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles><rootfile full-path=\"OEBPS/content.opf\" media-type=\"application/oebps-package+xml\"/></rootfiles></container>",
        )
        zf.writestr("OEBPS/chapter1.xhtml", f"<html><body><h1>{title}</h1></body></html>")
    return buf.getvalue()


def make_mobi_bytes() -> bytes:
    header = bytearray(80)
    header[0:11] = b"Sample_Book"
    header[60:68] = b"BOOKMOBI"
    return bytes(header) + b"\x00" * 64


def make_zip_bomb_epub_bytes(uncompressed_size: int = 8 * 1024 * 1024) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("OEBPS/bomb.txt", b"A" * uncompressed_size)
    return buf.getvalue()


class MockZlibState:
    """Mutable state controlling offline mock Z-Library HTTP server responses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mode: str = "normal"
        self.redirect_location: str = ""
        self.slow_delay_sec: float = 0.0
        self.pdf_bytes: bytes = make_pdf_bytes("zlib")
        self.epub_bytes: bytes = make_epub_bytes("Crafting Interpreters")
        self.mobi_bytes: bytes = make_mobi_bytes()
        self.txt_bytes: bytes = "Chapter 1\nHello Z-Library offline test.\n".encode("utf-8")
        self.bomb_bytes: bytes = make_zip_bomb_epub_bytes()
        self.oversized_declared_len: int = 2 * 1024 * 1024
        self.search_html: str = (
            "<html><body>"
            '<z-bookcard id="101" hash="abc101" author="Robert Nystrom" year="2021" '
            'language="English" extension="epub" filesize="8.6 MB" '
            'href="/book/101/abc101/crafting-interpreters.html" download="/dl/101/abc101">'
            '<div slot="title">Crafting Interpreters</div>'
            "</z-bookcard>"
            '<div class="resItemBox"><h3 itemprop="name">'
            '<a href="/book/202/def202/sicp.html">Structure and Interpretation</a></h3>'
            '<div class="authors"><a itemprop="author">Harold Abelson</a></div>'
            '<div class="property_year"><div class="property_value">1996</div></div>'
            '<div class="property_language"><div class="property_value">English</div></div>'
            '<div class="property__file"><div class="property_value">PDF, 14.2 MB</div></div>'
            '<a href="/dl/202/def202">Download</a></div>'
            "</body></html>"
        )


def _send_bytes(handler: http.server.BaseHTTPRequestHandler, status: int, content_type: str, body: bytes, *, omit_length: bool = False) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    if not omit_length:
        handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@contextmanager
def run_mock_zlib_server(state: MockZlibState) -> Iterator[str]:
    """Start a local 127.0.0.1 HTTP server emulating Z-Library search, detail, and download."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            assert self.path == "/eapi/book/search"
            assert self.headers.get("Content-Type") == "application/x-www-form-urlencoded"
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            assert "message=" in body
            self.path += "?" + body
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            state.requests.append({
                "path": parsed.path,
                "query": parsed.query,
                "cookie": self.headers.get("Cookie", ""),
                "remix_key": self.headers.get("remix-userkey", ""),
                "remix_userid": self.headers.get("remix-userid", ""),
            })
            if state.mode == "401":
                _send_bytes(self, 401, "text/plain", b"Unauthorized")
                return
            if state.mode == "500":
                _send_bytes(self, 500, "text/plain", b"Internal Server Error")
                return
            if state.mode == "timeout" and state.slow_delay_sec > 0:
                time.sleep(state.slow_delay_sec)
            if state.mode == "empty" and (parsed.path.startswith("/s/") or parsed.path == "/eapi/book/search"):
                _send_bytes(self, 200, "text/html; charset=utf-8", b"<html><body><p>Nothing found</p></body></html>")
                return
            if parsed.path.startswith("/s/"):
                _send_bytes(self, 200, "text/html; charset=utf-8", state.search_html.encode("utf-8"))
                return
            if parsed.path == "/eapi/book/search":
                payload = {
                    "success": 1,
                    "books": [{
                        "id": 303, "hash": "eapi303", "title": "Designing Data-Intensive Applications",
                        "author": "Martin Kleppmann", "year": "2017", "extension": "pdf",
                        "filesizeString": "22.4 MB", "language": "English",
                        "href": "/book/303/eapi303/ddia.html", "dl": "/dl/303/eapi303",
                    }],
                }
                _send_bytes(self, 200, "application/json", json.dumps(payload).encode("utf-8"))
                return
            if parsed.path.startswith("/book/101/"):
                html = (
                    "<html><body><h1>Crafting Interpreters</h1>"
                    '<span itemprop="author">Robert Nystrom</span>'
                    '<div id="bookDescriptionBox">A handbook for making programming languages.</div>'
                    '<div class="property_year"><div class="property_value">2021</div></div>'
                    '<div class="property_language"><div class="property_value">English</div></div>'
                    '<div class="property__file"><div class="property_value">EPUB, 8.6 MB</div></div>'
                    '<a class="addDownloadedBook" href="/dl/101/abc101">Download EPUB</a>'
                    '<a class="addDownloadedBook" href="/dl/101/pdf101">Download PDF</a>'
                    "</body></html>"
                )
                _send_bytes(self, 200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            if parsed.path.startswith("/book/unknown"):
                _send_bytes(self, 404, "text/html", b"<h1>Not Found</h1>")
                return
            if parsed.path == "/dl/101/redirect-unsafe":
                self.send_response(302)
                self.send_header("Location", state.redirect_location)
                self.end_headers()
                return
            if parsed.path == "/dl/101/redirect-ok":
                self.send_response(302)
                self.send_header("Location", "/dl/101/pdf101")
                self.end_headers()
                return
            if parsed.path == "/dl/101/abc101":
                _send_bytes(self, 200, "application/epub+zip", state.epub_bytes)
                return
            if parsed.path == "/dl/101/pdf101":
                _send_bytes(self, 200, "application/pdf", state.pdf_bytes)
                return
            if parsed.path == "/dl/101/mobi":
                _send_bytes(self, 200, "application/x-mobipocket-ebook", state.mobi_bytes)
                return
            if parsed.path == "/dl/101/txt":
                _send_bytes(self, 200, "text/plain; charset=utf-8", state.txt_bytes)
                return
            if parsed.path == "/dl/101/eapi-json":
                _send_bytes(self, 200, "application/json", json.dumps({"success": 1, "file": {"downloadLink": "/dl/101/pdf101"}}).encode("utf-8"))
                return
            if parsed.path == "/dl/101/wrong-magic":
                _send_bytes(self, 200, "text/html", b"<!doctype html><html><body>Login required</body></html>")
                return
            if parsed.path == "/dl/101/zip-bomb":
                _send_bytes(self, 200, "application/epub+zip", state.bomb_bytes)
                return
            if parsed.path == "/dl/101/oversized-header":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(state.oversized_declared_len))
                self.end_headers()
                return
            if parsed.path == "/dl/101/oversized-stream":
                big = b"%PDF-1.7\n" + (b"X" * (2 * 1024 * 1024))
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(big)
                return
            if parsed.path == "/dl/101/timeout-mid":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", "4096")
                self.end_headers()
                self.wfile.write(b"%PDF-1.7\n")
                self.wfile.flush()
                time.sleep(max(0.5, state.slow_delay_sec))
                return
            _send_bytes(self, 404, "text/plain", b"Not Found")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
