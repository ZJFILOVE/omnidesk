"""Offline mock HTTP server and test fixture helpers for plugin-annas-archive tests."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import threading
import time
import urllib.parse
import zipfile
from contextlib import contextmanager
from typing import Any, Iterator

MD5_SICP = hashlib.md5(b"%PDF-1.7\n% sicp-annas\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n", usedforsecurity=False).hexdigest()
MD5_DDIA = "fedcba9876543210fedcba9876543210"


def make_pdf_bytes(tag: str = "annas") -> bytes:
    return f"%PDF-1.7\n% {tag}\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n".encode("utf-8")


def make_epub_bytes(title: str = "Sample Anna EPUB") -> bytes:
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
    header[0:11] = b"Annas_MOBI_"
    header[60:68] = b"BOOKMOBI"
    return bytes(header) + b"\x00" * 64


def make_zip_bomb_epub_bytes(uncompressed_size: int = 8 * 1024 * 1024) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("OEBPS/bomb.txt", b"Z" * uncompressed_size)
    return buf.getvalue()


class MockAnnasState:
    """Mutable state controlling offline mock Anna's Archive HTTP server responses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mode: str = "normal"
        self.redirect_location: str = ""
        self.slow_delay_sec: float = 0.0
        self.pdf_bytes: bytes = make_pdf_bytes("sicp-annas")
        self.epub_bytes: bytes = make_epub_bytes("Design Patterns")
        self.mobi_bytes: bytes = make_mobi_bytes()
        self.txt_bytes: bytes = "SICP Notes\nOffline Anna's Archive test.\n".encode("utf-8")
        self.bomb_bytes: bytes = make_zip_bomb_epub_bytes()
        self.oversized_declared_len: int = 2 * 1024 * 1024
        self.fast_error: str = ""
        self.detail_extra_links: list[str] = []
        self.partner_html: str | None = None
        self.search_html: str = (
            "<html><body>"
            f'<a href="/md5/{MD5_SICP}" class="flex">'
            '<h3 class="font-bold">Structure and Interpretation of Computer Programs</h3>'
            '<div class="italic">Harold Abelson, Gerald Jay Sussman</div>'
            '<div class="text-xs text-gray-500">English [en] · PDF · 14.2 MB · 1996 · MIT Press</div>'
            "</a>"
            "<!-- "
            f'<a href="/md5/{MD5_DDIA}" class="flex">'
            '<div class="font-bold">Designing Data-Intensive Applications</div>'
            '<div class="italic">Martin Kleppmann</div>'
            '<div class="text-xs text-gray-500">English [en] · EPUB · 9.4 MB · 2017</div>'
            "</a>"
            " -->"
            "</body></html>"
        )


def _send_bytes(handler: http.server.BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@contextmanager
def run_mock_annas_server(state: MockAnnasState) -> Iterator[str]:
    """Start a local 127.0.0.1 HTTP server emulating Anna's Archive search, MD5 detail, and downloads."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            state.requests.append({
                "path": parsed.path,
                "query": parsed.query,
                "cookie": self.headers.get("Cookie", ""),
                "api_key": self.headers.get("X-Api-Key", ""),
            })
            if state.mode == "401":
                _send_bytes(self, 401, "text/plain", b"Unauthorized")
                return
            if state.mode == "500":
                _send_bytes(self, 500, "text/plain", b"Internal Server Error")
                return
            if state.mode == "timeout" and state.slow_delay_sec > 0:
                time.sleep(state.slow_delay_sec)
            if state.mode == "empty" and parsed.path == "/search":
                _send_bytes(self, 200, "text/html; charset=utf-8", b"<html><body><p>No files found.</p></body></html>")
                return
            if parsed.path == "/search":
                _send_bytes(self, 200, "text/html; charset=utf-8", state.search_html.encode("utf-8"))
                return
            if parsed.path == f"/md5/{MD5_SICP}":
                html = (
                    "<html><body>"
                    "<h1>Structure and Interpretation of Computer Programs</h1>"
                    '<div class="italic">Harold Abelson, Gerald Jay Sussman</div>'
                    '<div class="text-sm text-gray-500">English [en] · PDF · 14.2 MB · 1996</div>'
                    '<div class="js-md5-top-box-description">Classic computer science textbook.</div>'
                    f'<a href="/slow_download/{MD5_SICP}/0/0">Slow Partner Server (PDF)</a>'
                    f'<a href="/dl/{MD5_SICP}/epub">Mirror Server (EPUB)</a>'
                    "</body></html>"
                )
                html += "".join(f'<a href="{href}" download>Test file</a>' for href in state.detail_extra_links)
                _send_bytes(self, 200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            if parsed.path.startswith("/md5/"):
                _send_bytes(self, 404, "text/html", b"<h1>MD5 Not Found</h1>")
                return
            if parsed.path == "/dyn/api/fast_download.json":
                if state.fast_error:
                    _send_bytes(self, 400, "application/json", json.dumps({"error": state.fast_error}).encode("utf-8"))
                    return
                _send_bytes(self, 200, "application/json", json.dumps({"download_url": f"/dl/{MD5_SICP}/pdf"}).encode("utf-8"))
                return
            if parsed.path == f"/slow_download/{MD5_SICP}/0/0":
                html = state.partner_html or f'<html><body><a class="js-download-link" href="/dl/{MD5_SICP}/pdf">Download now</a></body></html>'
                _send_bytes(self, 200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            if parsed.path == "/dl/redirect-unsafe":
                self.send_response(302)
                self.send_header("Location", state.redirect_location)
                self.end_headers()
                return
            if parsed.path == "/dl/redirect-ok":
                self.send_response(302)
                self.send_header("Location", f"/dl/{MD5_SICP}/pdf")
                self.end_headers()
                return
            if parsed.path == f"/dl/{MD5_SICP}/pdf":
                _send_bytes(self, 200, "application/pdf", state.pdf_bytes)
                return
            if parsed.path == f"/dl/{MD5_SICP}/epub":
                _send_bytes(self, 200, "application/epub+zip", state.epub_bytes)
                return
            if parsed.path == f"/dl/{MD5_SICP}/mobi":
                _send_bytes(self, 200, "application/x-mobipocket-ebook", state.mobi_bytes)
                return
            if parsed.path == f"/dl/{MD5_SICP}/txt":
                _send_bytes(self, 200, "text/plain; charset=utf-8", state.txt_bytes)
                return
            if parsed.path == "/dl/wrong-magic":
                _send_bytes(self, 200, "text/html", b"<!doctype html><html><body>Cloudflare check</body></html>")
                return
            if parsed.path == "/dl/zip-bomb":
                _send_bytes(self, 200, "application/epub+zip", state.bomb_bytes)
                return
            if parsed.path == "/dl/oversized-header":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(state.oversized_declared_len))
                self.end_headers()
                return
            if parsed.path == "/dl/oversized-stream":
                big = b"%PDF-1.7\n" + (b"Y" * (2 * 1024 * 1024))
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(big)
                return
            if parsed.path == "/dl/timeout-mid":
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
