"""Subtitle provider search adapters (ASSRT, SubHD, Custom) and download link resolvers."""

from __future__ import annotations

import html as html_lib
import json
import re
import ssl
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from . import sub_net
except ImportError:
    import sub_net  # type: ignore[no-redef]

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_IMDB_RE = re.compile(r"^tt\d{5,12}$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
_RELEASE_STRIP_RE = re.compile(
    r"(?i)\b(?:2160p|1080p|720p|480p|bluray|bdrip|web-dl|webrip|hdtv|remux|x264|x265|hevc|h\.?264|h\.?265|aac|dts|ddp?\d(?:\.\d)?)\b"
)
_HREF_RE = re.compile(r"""href\s*=\s*["'](https://[^"']+)["']""", re.IGNORECASE)
_LANG_ALIASES: dict[str, set[str]] = {
    "zh": {"zh", "chs", "zh-cn", "zh-hans", "sc", "gb", "简体", "简中", "中文", "双语"},
    "chs": {"zh", "chs", "zh-cn", "zh-hans", "sc", "gb", "简体", "简中", "中文", "双语"},
    "cht": {"cht", "zh-tw", "zh-hk", "zh-hant", "tc", "big5", "繁体", "繁中"},
    "en": {"en", "eng", "english", "英文", "英语", "双语"},
    "eng": {"en", "eng", "english", "英文", "英语", "双语"},
}


@dataclass(frozen=True, slots=True)
class ProviderOpts:
    """Shared request options passed across subtitle provider adapters."""

    timeout: float
    max_bytes: int
    ssl_context: ssl.SSLContext | None = None


def derive_search_params(req: Mapping[str, Any]) -> dict[str, Any]:
    """Derive normalized search query, title, imdb_id, season, episode, and language."""
    raw_query = str(req.get("query") or "").strip()
    title = str(req.get("title") or "").strip()
    imdb_id = str(req.get("imdb_id") or "").strip()
    language = str(req.get("language") or req.get("lang") or "").strip()
    season, episode = req.get("season"), req.get("episode")

    if not title and not raw_query:
        source_name = str(req.get("filename") or req.get("path") or "").strip()
        if source_name:
            stem = Path(source_name).stem
            ep_match = _EPISODE_RE.search(stem)
            if ep_match:
                season = season if season is not None else int(ep_match.group(1))
                episode = episode if episode is not None else int(ep_match.group(2))
                stem = stem[: ep_match.start()]
            else:
                stem = _RELEASE_STRIP_RE.split(stem, maxsplit=1)[0]
            title = re.sub(r"[._]+", " ", stem).strip(" -")

    def _opt_int(val: Any, name: str) -> int | None:
        if val is None or not str(val).strip():
            return None
        try:
            return int(val)
        except (TypeError, ValueError) as exc:
            raise sub_net.SubNetError("invalid_argument", f"参数 {name} 必须为整数: {val!r}") from exc

    season_val = _opt_int(season, "season")
    episode_val = _opt_int(episode, "episode")

    parts: list[str] = []
    if raw_query:
        parts.append(raw_query)
    elif title:
        parts.append(title)
        if season_val is not None and episode_val is not None:
            parts.append(f"S{season_val:02d}E{episode_val:02d}")
        elif season_val is not None:
            parts.append(f"S{season_val:02d}")
    elif imdb_id:
        parts.append(imdb_id)

    return {
        "query": " ".join(p for p in parts if p).strip(),
        "title": title,
        "imdb_id": imdb_id if _IMDB_RE.match(imdb_id) else imdb_id,
        "season": season_val,
        "episode": episode_val,
        "language": language,
    }


def _infer_lang_from_text(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("双语", "中英", "bilingual")):
        return "zh-CN,en"
    if any(k in low for k in ("简体", "chs", "zh-cn", "gb")):
        return "zh-CN"
    if any(k in low for k in ("繁体", "cht", "zh-tw", "big5")):
        return "zh-TW"
    if any(k in low for k in ("eng", "english", "英文")):
        return "en"
    return ""


def _lang_matches(candidate_lang: str, requested_lang: str) -> bool:
    req = requested_lang.strip().lower()
    if not req or not candidate_lang.strip():
        return True
    accepted = _LANG_ALIASES.get(req, {req})
    cand_tokens = {tok.strip().lower() for tok in re.split(r"[,;/|\s]+", candidate_lang) if tok.strip()}
    return bool(cand_tokens & accepted) or any(alias in candidate_lang.lower() for alias in accepted)


def filter_and_rank_results(items: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    """Filter explicit non-matching languages while keeping language-agnostic hits."""
    if not language:
        return items
    filtered = [item for item in items if _lang_matches(str(item.get("language") or ""), language)]
    filtered.sort(key=lambda item: (0 if item.get("language") else 1))
    return filtered


class _SubHDSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._active_href: str | None = None
        self._parts: list[str] = []
        self.items: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if re.fullmatch(r"/(?:a|ar)/[A-Za-z0-9_-]+", href):
            self._active_href = href
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_href is not None:
            text = " ".join("".join(self._parts).split())
            if text:
                self.items.setdefault(self._active_href, []).append(html_lib.unescape(text))
            self._active_href = None


def parse_subhd_html(raw_html: str, base_url: str) -> list[dict[str, Any]]:
    """Extract subtitle search candidates from SubHD HTML (empty list for no results)."""
    parser = _SubHDSearchParser()
    parser.feed(raw_html)
    results: list[dict[str, Any]] = []
    base = base_url.rstrip("/")
    for href, titles in parser.items.items():
        sub_id = href.rsplit("/", 1)[-1]
        if not _SAFE_ID_RE.match(sub_id):
            continue
        title = " · ".join(dict.fromkeys(titles))
        results.append(
            {
                "id": sub_id,
                "provider": "subhd",
                "title": title,
                "language": _infer_lang_from_text(title),
                "format": "",
                "url": f"{base}{href}",
                "download_url": "",
            }
        )
    return results


def parse_assrt_search_json(payload: Any) -> list[dict[str, Any]]:
    """Normalize ASSRT /v1/sub/search JSON response into candidate items."""
    if not isinstance(payload, Mapping):
        raise sub_net.SubNetError("service_offline", "ASSRT 返回格式无效")
    status = payload.get("status")
    if status in (20000, 20001, 401, 403):
        raise sub_net.SubNetError("permission_denied", "ASSRT Token 无效或鉴权失败", http_status=401)
    if status == 30002:
        raise sub_net.SubNetError("rate_limited", "ASSRT 请求频率受限", http_status=429)
    if status != 0:
        raise sub_net.SubNetError("service_offline", f"ASSRT 搜索返回错误状态: {status}")

    subs = (payload.get("sub") or {}).get("subs") or []
    results: list[dict[str, Any]] = []
    for item in subs:
        if not isinstance(item, Mapping) or "id" not in item:
            continue
        sub_id = str(item["id"])
        lang_desc = str((item.get("lang") or {}).get("desc") or "")
        title_parts = [str(item.get(k) or "").strip() for k in ("native_name", "videoname", "subtype")]
        title = " · ".join(p for p in title_parts if p) or f"ASSRT #{sub_id}"
        results.append(
            {
                "id": sub_id,
                "provider": "assrt",
                "title": title,
                "language": lang_desc or _infer_lang_from_text(title),
                "format": str(item.get("subtype") or "").lower(),
                "url": f"https://assrt.net/xml/sub/{sub_id}.xml",
                "download_url": str(item.get("url") or ""),
            }
        )
    return results


async def _provider_get(
    ctx: Any, url: str, *, accept: str, opts: ProviderOpts, bearer_token: str = "", include_cookies: bool = True
) -> tuple[sub_net.HttpBytesResponse, bool, dict[str, str]]:
    parts = sub_net.validate_https_url(url, allow_loopback=True)
    allow_lb = sub_net.is_loopback_host(parts.hostname or "")
    headers: dict[str, str] = {"Accept": accept}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    cookie = await sub_net.resolve_cookie_header(ctx, url) if include_cookies else ""
    if cookie:
        headers["Cookie"] = cookie
    resp = await sub_net.fetch_https_bytes(
        url,
        headers=headers,
        timeout_sec=opts.timeout,
        max_bytes=opts.max_bytes,
        allow_loopback=allow_lb,
        ssl_context=opts.ssl_context,
        secrets=sub_net.collect_secrets(ctx, (bearer_token, cookie)),
    )
    auth_headers = {k: v for k, v in headers.items() if k in {"Authorization", "Cookie"}}
    return resp, allow_lb, auth_headers


def _decode_json_body(body: bytes, label: str) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise sub_net.SubNetError("service_offline", f"{label} JSON 解析失败: {exc}") from None


async def search_provider(
    ctx: Any,
    provider: str,
    params: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    assrt_token: str,
    custom_api_key: str,
    opts: ProviderOpts,
) -> list[dict[str, Any]]:
    """Query one configured subtitle provider (`assrt`, `subhd`, or `custom`)."""
    if provider == "assrt":
        if not assrt_token:
            raise sub_net.SubNetError("not_configured", "ASSRT 需要配置 SecretVault 中的 assrt_token")
        base = str(cfg.get("assrt_api_base") or "https://api.assrt.net").strip().rstrip("/")
        q_dict: dict[str, Any] = {"q": params["query"], "cnt": 15}
        if params.get("imdb_id"):
            q_dict["imdb_id"] = params["imdb_id"]
        resp, _, _ = await _provider_get(
            ctx, f"{base}/v1/sub/search?{urllib.parse.urlencode(q_dict)}", accept="application/json", opts=opts, bearer_token=assrt_token
        )
        return parse_assrt_search_json(_decode_json_body(resp.body, "ASSRT"))

    if provider == "subhd":
        base = str(cfg.get("subhd_base_url") or "https://subhd.tv").strip().rstrip("/")
        resp, _, _ = await _provider_get(
            ctx, f"{base}/search/{urllib.parse.quote(params['query'], safe='')}", accept="text/html,application/json", opts=opts
        )
        text = resp.body.decode("utf-8", errors="replace")
        if "json" in resp.content_type.lower() or text.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(text)
                items = parsed.get("results", []) if isinstance(parsed, Mapping) else parsed
                if isinstance(items, list):
                    return [dict(item, provider="subhd") for item in items if isinstance(item, Mapping)]
            except json.JSONDecodeError:
                pass
        return parse_subhd_html(text, base)

    if provider == "custom":
        base = str(cfg.get("custom_api_base") or "").strip().rstrip("/")
        if not base:
            raise sub_net.SubNetError("not_configured", "尚未配置自定义字幕源 custom_api_base")
        q_dict = {k: v for k, v in params.items() if v is not None and v != ""}
        q_dict["q"] = params["query"]
        resp, _, _ = await _provider_get(
            ctx, f"{base}/search?{urllib.parse.urlencode(q_dict)}", accept="application/json", opts=opts, bearer_token=custom_api_key
        )
        parsed = _decode_json_body(resp.body, "自定义字幕源")
        raw_list = parsed.get("results", []) if isinstance(parsed, Mapping) else parsed
        if not isinstance(raw_list, list):
            raise sub_net.SubNetError("service_offline", "自定义字幕源返回格式无效")
        return [dict(item, provider="custom") for item in raw_list if isinstance(item, Mapping)]

    raise sub_net.SubNetError("invalid_argument", f"未知字幕源 provider: {provider!r}")


async def resolve_provider_download(
    ctx: Any,
    provider: str,
    sub_id: str,
    cfg: Mapping[str, Any],
    *,
    assrt_token: str,
    custom_api_key: str,
    opts: ProviderOpts,
) -> tuple[str, dict[str, str], bool]:
    """Resolve an HTTPS download URL and headers for a provider entry ID."""
    if not _SAFE_ID_RE.match(sub_id):
        raise sub_net.SubNetError("invalid_argument", f"无效字幕条目 ID: {sub_id!r}")

    if provider == "assrt":
        if not assrt_token:
            raise sub_net.SubNetError("not_configured", "ASSRT 下载需要 SecretVault 中的 assrt_token")
        base = str(cfg.get("assrt_api_base") or "https://api.assrt.net").strip().rstrip("/")
        resp, allow_lb, _ = await _provider_get(
            ctx,
            f"{base}/v1/sub/detail?{urllib.parse.urlencode({'id': sub_id})}",
            accept="application/json",
            opts=opts,
            bearer_token=assrt_token,
            include_cookies=False,
        )
        data = _decode_json_body(resp.body, "ASSRT 详情")
        if data.get("status") != 0:
            raise sub_net.SubNetError("service_offline", "ASSRT 详情获取失败")
        for sub in (data.get("sub") or {}).get("subs") or []:
            for item in sub.get("filelist") or []:
                if item.get("url"):
                    return str(item["url"]), {}, allow_lb
            if sub.get("url"):
                return str(sub["url"]), {}, allow_lb
        raise sub_net.SubNetError("not_found", "ASSRT 条目未提供可用下载链接")

    if provider == "subhd":
        base = str(cfg.get("subhd_base_url") or "https://subhd.tv").strip().rstrip("/")
        resp, allow_lb, auth_headers = await _provider_get(
            ctx, f"{base}/a/{sub_id}", accept="text/html,application/json", opts=opts
        )
        text = resp.body.decode("utf-8", errors="replace")
        if text.lstrip().startswith("{"):
            parsed = _decode_json_body(resp.body, "SubHD 详情")
            if parsed.get("download_url") or parsed.get("url"):
                return str(parsed.get("download_url") or parsed.get("url")), auth_headers, allow_lb
        m = _HREF_RE.search(text)
        if not m:
            raise sub_net.SubNetError("not_found", "SubHD 条目页未找到 HTTPS 下载链接")
        return m.group(1), auth_headers, allow_lb

    if provider == "custom":
        base = str(cfg.get("custom_api_base") or "").strip().rstrip("/")
        if not base:
            raise sub_net.SubNetError("not_configured", "尚未配置 custom_api_base")
        parts = sub_net.validate_https_url(base, allow_loopback=True)
        allow_lb = sub_net.is_loopback_host(parts.hostname or "")
        headers = {"Authorization": f"Bearer {custom_api_key}"} if custom_api_key else {}
        cookie = await sub_net.resolve_cookie_header(ctx, base)
        if cookie:
            headers["Cookie"] = cookie
        return f"{base}/download?{urllib.parse.urlencode({'id': sub_id})}", headers, allow_lb

    raise sub_net.SubNetError("invalid_argument", f"下载不支持的 provider: {provider!r}")
