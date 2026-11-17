"""Shared offline test fixtures (local TLS http.server & fake ffsubsync) for plugin-sub-hunter."""

from __future__ import annotations

import http.server
import importlib.util
import ssl
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_sub_hunter_module(submodule: str = "plugin") -> Any:
    """Import a plugin-sub-hunter module under an isolated package namespace."""
    pkg_name = "_omnidesk_test_sub_hunter"
    if pkg_name not in sys.modules:
        pkg = ModuleType(pkg_name)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
    full_name = f"{pkg_name}.{submodule}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PLUGIN_DIR / f"{submodule}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class LocalHttpsServer:
    """In-process TLS HTTP server bound to 127.0.0.1 with ephemeral cert in tmp_path."""

    base_url: str
    ssl_context: ssl.SSLContext
    cert_path: Path
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread
    requests: list[dict[str, Any]]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def start_local_https_server(
    tmp_path: Path,
    responder: Callable[[http.server.BaseHTTPRequestHandler], None],
) -> LocalHttpsServer:
    """Start a local TLS ThreadingHTTPServer in `tmp_path` for completely offline tests."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    if not (cert_path.is_file() and key_path.is_file()):
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
    requests: list[dict[str, Any]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            try:
                responder(self)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                return

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = srv_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    return LocalHttpsServer(
        base_url=f"https://127.0.0.1:{port}",
        ssl_context=client_ctx,
        cert_path=cert_path,
        server=server,
        thread=thread,
        requests=requests,
    )


def write_fake_ffsubsync(tmp_path: Path, body: str, name: str = "fake-ffsubsync") -> Path:
    """Create an executable Python fake ffsubsync script inside `tmp_path`."""
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script


@pytest.fixture
def make_https_server(tmp_path: Path):
    servers: list[LocalHttpsServer] = []

    def _factory(responder: Callable[[http.server.BaseHTTPRequestHandler], None]) -> LocalHttpsServer:
        srv = start_local_https_server(tmp_path, responder)
        servers.append(srv)
        return srv

    yield _factory
    for srv in servers:
        srv.close()
