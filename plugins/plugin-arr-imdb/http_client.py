"""Safety validators, secret redaction, metadata helpers, and stdlib HTTP JSON client."""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any

from omnidesk_sdk.stores import redact_secrets

_SAFE_MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,64}\Z")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:api[_-]?key|apikey|token|secret)=)([^&\s]+)")
_HEADER_SECRET_RE = re.compile(r"(?i)(X-Api-Key\s*:\s*)([^\s,;'\"]+)")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_TMDB_GENRE_NAMES: dict[int, str] = {
    12: "Adventure", 14: "Fantasy", 16: "Animation", 18: "Drama", 27: "Horror",
    28: "Action", 35: "Comedy", 36: "History", 37: "Western", 53: "Thriller",
    80: "Crime", 99: "Documentary", 878: "Science Fiction", 9648: "Mystery",
    10402: "Music", 10749: "Romance", 10751: "Family", 10752: "War",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics",
    10770: "TV Movie",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "": "popular",
    "popular": "popular",
    "热门": "popular",
    "trending": "popular",
    "imdb_popular": "popular",
    "movies_popular": "popular",
    "top_rated": "top_rated",
    "top-rated": "top_rated",
    "top250": "top_rated",
    "高分": "top_rated",
    "imdb_top250": "top_rated",
    "upcoming": "upcoming",
    "即将上映": "upcoming",
    "coming_soon": "upcoming",
    "tv_popular": "tv_popular",
    "热门剧集": "tv_popular",
    "series_popular": "tv_popular",
    "tv": "tv_popular",
    "tv_top_rated": "tv_top_rated",
    "高分剧集": "tv_top_rated",
    "series_top_rated": "tv_top_rated",
    "search": "search",
    "搜索": "search",
}


class UpstreamClientError(RuntimeError):
    """Fail-closed upstream or validation error carrying a canonical error_code."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None, duplicate: bool = False) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.duplicate = duplicate
        super().__init__(message)


def scrub_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub secret values, auth headers, and query API keys from diagnostic text."""
    if not text:
        return ""
    cleaned = redact_secrets(text, extra_secrets)
    cleaned = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _HEADER_SECRET_RE.sub(r"\1[REDACTED]", cleaned)
    return cleaned


def validate_http_url(raw_url: Any, *, label: str = "url", empty_code: str = "not_configured") -> str:
    """Validate that a user-configured URL uses http/https with a host and no embedded secrets."""
    if raw_url is None:
        raise UpstreamClientError(empty_code, f"尚未配置 {label}")
    if not isinstance(raw_url, str):
        raise UpstreamClientError("invalid_argument", f"{label} 必须为字符串 URL")
    cleaned = raw_url.strip()
    if not cleaned:
        raise UpstreamClientError(empty_code, f"尚未配置 {label}")
    if any(ch.isspace() or ch == "\x00" for ch in cleaned):
        raise UpstreamClientError("invalid_argument", f"{label} 包含非法空白或控制字符")
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UpstreamClientError("invalid_argument", f"{label} 仅允许 http/https 协议，收到: {scheme or 'empty'}")
    if not parsed.hostname:
        raise UpstreamClientError("invalid_argument", f"{label} 缺少有效主机名")
    if parsed.username is not None or parsed.password is not None:
        raise UpstreamClientError("invalid_argument", f"{label} 不允许内嵌凭据，请使用 SecretVault")
    return cleaned.rstrip("/")


def validate_optional_http_url(raw_url: Any) -> str:
    """Return validated http/https URL or empty string when blank/invalid."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        return ""
    try:
        return validate_http_url(raw_url, label="media_url")
    except UpstreamClientError:
        return ""


def validate_media_id(raw_id: Any, *, field: str = "item_id") -> str:
    """Validate a media identifier against path-traversal and URL-injection characters."""
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
        return str(raw_id)
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise UpstreamClientError("invalid_argument", f"缺少有效的 {field}")
    cleaned = raw_id.strip()
    lower = cleaned.lower()
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned or "\x00" in cleaned or "%2e" in lower or "%2f" in lower or "%5c" in lower:
        raise UpstreamClientError("path_safety", f"{field} 包含非法路径穿越字符")
    if not _SAFE_MEDIA_ID_RE.match(cleaned):
        raise UpstreamClientError("invalid_argument", f"{field} 格式无效")
    return cleaned


def validate_remote_path(raw_path: Any, *, field: str = "root_folder_path") -> str:
    """Validate a remote Arr root folder path against traversal segments."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise UpstreamClientError("invalid_argument", f"缺少有效的 {field}")
    cleaned = raw_path.strip()
    if "\x00" in cleaned or "\r" in cleaned or "\n" in cleaned:
        raise UpstreamClientError("path_safety", f"{field} 包含非法控制字符")
    parts = [p for p in re.split(r"[\\/]+", cleaned) if p]
    if ".." in parts:
        raise UpstreamClientError("path_safety", f"{field} 不可包含路径穿越片段 '..'")
    if not (cleaned.startswith("/") or _WINDOWS_DRIVE_RE.match(cleaned)):
        raise UpstreamClientError("invalid_argument", f"{field} 必须是绝对路径")
    return cleaned


def normalize_category(category: str, query: str = "") -> str:
    """Map user/alias category strings to canonical IMDb/TMDB category keys."""
    raw = (category or "").strip()
    if not raw and query.strip():
        return "search"
    key = raw.lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    if raw in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[raw]
    raise UpstreamClientError("invalid_argument", f"不支持的媒体分类: {category!r}")


def join_url(base_url: str, path: str, query: Mapping[str, Any] | None = None) -> str:
    """Join base URL, path, and non-empty query parameters."""
    clean_base = base_url.rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    if not query:
        return f"{clean_base}{clean_path}"
    filtered = {k: v for k, v in query.items() if v is not None and str(v) != ""}
    return f"{clean_base}{clean_path}?{urllib.parse.urlencode(filtered)}" if filtered else f"{clean_base}{clean_path}"


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_year(item: Mapping[str, Any]) -> int | None:
    for key in ("year", "startYear", "releaseYear"):
        if (val := as_int(item.get(key))) is not None and 1880 <= val <= 2100:
            return val
    for key in ("release_date", "first_air_date", "releaseDate"):
        raw = str(item.get(key) or "").strip()
        if len(raw) >= 4 and raw[:4].isdigit():
            return int(raw[:4])
    return None


def extract_genres(item: Mapping[str, Any]) -> list[str]:
    genres: list[str] = []
    raw_genres = item.get("genres")
    if isinstance(raw_genres, list):
        for g in raw_genres:
            if isinstance(g, str) and g.strip():
                genres.append(g.strip())
            elif isinstance(g, Mapping) and str(g.get("name") or "").strip():
                genres.append(str(g["name"]).strip())
    if not genres and isinstance(item.get("genre_ids"), list):
        for gid in item["genre_ids"]:
            if isinstance(gid, int) and gid in _TMDB_GENRE_NAMES:
                genres.append(_TMDB_GENRE_NAMES[gid])
    return genres


def extract_poster_url(item: Mapping[str, Any]) -> str:
    for key in ("poster_url", "posterUrl", "cover_url", "coverUrl", "poster", "image"):
        raw = item.get(key)
        if isinstance(raw, str) and (valid := validate_optional_http_url(raw)):
            return valid
        if isinstance(raw, Mapping) and (valid := validate_optional_http_url(raw.get("url"))):
            return valid
    primary_image = item.get("primaryImage")
    if isinstance(primary_image, Mapping) and (valid := validate_optional_http_url(primary_image.get("url"))):
        return valid
    poster_path = item.get("poster_path")
    if isinstance(poster_path, str) and poster_path.startswith("/") and ".." not in poster_path:
        return f"https://image.tmdb.org/t/p/w500{poster_path}"
    for img in item.get("images") or ():
        if isinstance(img, Mapping) and str(img.get("coverType") or "").lower() == "poster":
            if valid := validate_optional_http_url(img.get("remoteUrl") or img.get("url")):
                return valid
    return ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Forbid following HTTP 3xx redirects so secrets/cookies are never leaked."""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Mapping[str, str], newurl: str
    ) -> None:
        return None


def _build_safe_opener() -> urllib.request.OpenerDirector:
    """Construct a urllib opener with trust_env=False (no proxies) and follow_redirects=False."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler({}),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _NoRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


class SafeHttpClient:
    """Stdlib HTTP JSON client enforcing trust_env=False, follow_redirects=False, and timeouts."""

    trust_env: bool = False
    follow_redirects: bool = False

    def __init__(self, *, timeout_sec: float = 10.0) -> None:
        self.timeout_sec = max(0.05, min(float(timeout_sec), 120.0))

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        secrets: Iterable[str] = (),
        timeout_sec: float | None = None,
    ) -> tuple[int, Any]:
        """Execute an HTTP request off-thread and parse JSON fail-closed."""
        validated_url = validate_http_url(url, label="request_url", empty_code="invalid_argument")
        effective_timeout = max(0.05, min(float(timeout_sec if timeout_sec is not None else self.timeout_sec), 120.0))
        secret_list = [s for s in secrets if isinstance(s, str) and s]
        req_headers = {"Accept": "application/json", **(dict(headers) if headers else {})}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(dict(json_body), ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")

        def _do_sync() -> tuple[int, Any]:
            opener = _build_safe_opener()
            req = urllib.request.Request(validated_url, data=data, headers=req_headers, method=method.upper())
            try:
                with opener.open(req, timeout=effective_timeout) as resp:
                    status = int(getattr(resp, "status", 200))
                    raw = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    raw_err = exc.read(2048).decode("utf-8", errors="replace")
                except Exception:
                    raw_err = ""
                finally:
                    exc.close()
                raise self._map_http_error(status, raw_err, secret_list) from None
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                    raise UpstreamClientError("timeout", "上游服务请求超时") from None
                raise UpstreamClientError("service_offline", f"上游服务不可达: {scrub_secrets(str(reason), secret_list)}") from None
            except (TimeoutError, socket.timeout):
                raise UpstreamClientError("timeout", "上游服务请求超时") from None
            except OSError as exc:
                raise UpstreamClientError("service_offline", f"上游网络异常: {scrub_secrets(str(exc), secret_list)}") from None

            if not raw.strip():
                return status, {}
            try:
                return status, json.loads(raw)
            except json.JSONDecodeError as exc:
                raise UpstreamClientError("upstream_error", f"上游返回非 JSON 响应: {exc.msg}") from None

        try:
            return await asyncio.wait_for(asyncio.to_thread(_do_sync), timeout=effective_timeout + 1.0)
        except TimeoutError:
            raise UpstreamClientError("timeout", "上游服务请求超时") from None

    @staticmethod
    def _map_http_error(status: int, raw_body: str, secrets: Iterable[str]) -> UpstreamClientError:
        safe_body = scrub_secrets(raw_body.strip(), secrets)
        if 300 <= status < 400:
            return UpstreamClientError("redirect_blocked", f"上游返回重定向 HTTP {status}（已禁止自动跳转）", status_code=status)
        if status in {401, 403}:
            return UpstreamClientError("unauthorized", f"上游认证失败（HTTP {status}），请检查 SecretVault 凭据", status_code=status)
        if status == 404:
            return UpstreamClientError("not_found", "上游资源不存在（HTTP 404）", status_code=status)
        if status == 409:
            return UpstreamClientError("conflict", f"资源已存在或冲突（HTTP 409）: {safe_body[:200]}", status_code=status, duplicate=True)
        if status == 400 and any(
            marker in safe_body.lower()
            for marker in ("movieexistsvalidator", "seriesexistsvalidator", "already been added", "already exists")
        ):
            return UpstreamClientError("conflict", f"条目已存在于库中（重复添加）: {safe_body[:200]}", status_code=status, duplicate=True)
        if status == 429:
            return UpstreamClientError("rate_limited", "上游触发限流（HTTP 429）", status_code=status)
        if status >= 500:
            return UpstreamClientError("upstream_error", f"上游服务错误（HTTP {status}）", status_code=status)
        return UpstreamClientError("upstream_error", f"上游请求失败（HTTP {status}）: {safe_body[:200]}", status_code=status)
