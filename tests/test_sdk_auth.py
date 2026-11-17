"""Security-focused tests for SDK cookie/session resolution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnidesk_sdk import (
    CookieResolutionError,
    CookieSessionResolver,
    SessionCookie,
)
from omnidesk_sdk.auth import (
    filter_cookies,
    parse_cookie_header,
    parse_json_cookies,
    parse_netscape_cookies,
)


def test_netscape_parser_supports_httponly_and_chromium_expiry() -> None:
    chromium_expiry = int((11_644_473_600 + 1_234_567) * 1_000_000)
    text = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t"
        f"{chromium_expiry}\tsession\thttp-only-value\n"
        "example.com\tFALSE\t/feed\tFALSE\t0\ttheme\tdark\n"
    )

    cookies = parse_netscape_cookies(text)

    assert [cookie.name for cookie in cookies] == ["session", "theme"]
    assert cookies[0].domain == ".example.com"
    assert cookies[0].http_only is True
    assert cookies[0].secure is True
    assert cookies[0].expires_at == pytest.approx(1_234_567)
    assert cookies[1].value == "dark"


def test_json_cookie_parser_accepts_export_shapes() -> None:
    payload = {
        "cookies": [
            {
                "name": "sid",
                "value": "abc",
                "domain": ".example.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expirationDate": 2_000,
            }
        ]
    }

    cookies = parse_json_cookies(json.dumps(payload))

    assert cookies == [
        SessionCookie(
            name="sid",
            value="abc",
            domain=".example.com",
            path="/",
            secure=True,
            expires_at=2_000,
            http_only=True,
        )
    ]

    with pytest.raises(CookieResolutionError) as exc_info:
        parse_json_cookies('{"not_cookies": []}')
    assert exc_info.value.code == "invalid_response"


def test_cookie_filter_matches_domain_path_expiry_and_secure_transport() -> None:
    cookies = [
        SessionCookie("root", "1", ".example.com", "/", expires_at=2_000),
        SessionCookie("feed", "2", "example.com", "/feed/", expires_at=2_000),
        SessionCookie("wrong-prefix", "3", "example.com", "/fe", expires_at=2_000),
        SessionCookie("expired", "4", "example.com", "/", expires_at=900),
        SessionCookie("secure-only", "5", "example.com", "/", secure=True, expires_at=2_000),
        SessionCookie("other-domain", "6", "evil.test", "/", expires_at=2_000),
    ]

    matched = filter_cookies(cookies, "https://sub.example.com/feed/item", now=1_000)

    assert [cookie.name for cookie in matched] == ["feed", "root", "secure-only"]
    assert [cookie.name for cookie in filter_cookies(cookies, "http://example.com/", now=1_000)] == ["root"]


def test_cookie_header_parser_is_scoped_to_request_domain() -> None:
    cookies = parse_cookie_header(" sid=abc; theme=dark ; malformed ", domain="example.com", path="/feed")

    assert [(cookie.name, cookie.value) for cookie in cookies] == [("sid", "abc"), ("theme", "dark")]
    assert all(cookie.domain == "example.com" and cookie.path == "/feed" for cookie in cookies)


@pytest.mark.asyncio
async def test_resolver_prefers_capability_and_falls_back_to_vault(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "example.com\tFALSE\t/\tFALSE\t0\tsid\tfrom-file\n"
        "example.com\tFALSE\t/\tFALSE\t0\tfallback\tfrom-file\n",
        encoding="utf-8",
    )

    async def capability(capability_id: str, request: dict[str, object]) -> SimpleNamespace:
        assert capability_id == "web.cookies.v1"
        assert request["url"] == "https://example.com/"
        return SimpleNamespace(
            ok=True,
            data={
                "cookies": [
                    {"name": "sid", "value": "from-capability", "domain": ".example.com"},
                ]
            },
        )

    resolver = CookieSessionResolver(capability=capability, cookie_file=cookie_file)

    cookies = await resolver.resolve("https://example.com/")

    assert cookies == {"sid": "from-capability", "fallback": "from-file"}


@pytest.mark.asyncio
async def test_resolver_reads_secret_vault_cookie_header() -> None:
    class Vault:
        def get_secret(self, key: str) -> str:
            assert key == "reddit"
            return "reddit_session=secret; token=second"

    resolver = CookieSessionResolver(secret_vault=Vault(), secret_key="reddit")

    cookies = await resolver.resolve("https://www.reddit.com/r/python/")

    assert cookies == {"reddit_session": "secret", "token": "second"}


@pytest.mark.asyncio
async def test_resolver_fails_closed_without_leaking_cookie_values(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("evil.test\tFALSE\t/\tFALSE\t0\tsid\tsuper-secret-value\n", encoding="utf-8")
    resolver = CookieSessionResolver(cookie_file=cookie_file)

    with pytest.raises(CookieResolutionError) as exc_info:
        await resolver.resolve("https://example.com/")

    assert exc_info.value.code == "auth_required"
    assert "super-secret-value" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_resolver_propagates_capability_error_code() -> None:
    async def failing_capability(capability_id: str, request: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(ok=False, data={"error_code": "login_required"})

    resolver = CookieSessionResolver(capability=failing_capability)

    with pytest.raises(CookieResolutionError) as exc_info:
        await resolver.resolve("https://example.com/")

    assert exc_info.value.code == "login_required"


@pytest.mark.asyncio
async def test_resolver_rejects_non_http_urls() -> None:
    resolver = CookieSessionResolver()

    with pytest.raises(CookieResolutionError) as exc_info:
        await resolver.resolve("file:///etc/passwd")

    assert exc_info.value.code == "invalid_argument"
