"""Apple Music REST / AMP API client with strict host, redirect, proxy, and secret hygiene."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from omnidesk_sdk.stores import redact_secrets

_ALLOWED_PUBLIC_HOSTS = frozenset({"amp-api.music.apple.com", "api.music.apple.com"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SONG_ID_RE = re.compile(r"^[0-9]{1,24}$")


class AppleMusicApiError(RuntimeError):
    """Structured Apple Music client failure without leaking credentials."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status = status
        self.details = dict(details) if details else {}


class AppleMusicClientProtocol(Protocol):
    """Protocol implemented by both AppleMusicClient and offline test fakes."""

    async def preflight_cn_catalog(self, track_ids: Sequence[str], storefront: str = "cn") -> dict[str, Any]: ...
    async def get_playlist_meta(self, playlist_id: str) -> dict[str, Any]: ...
    async def list_library_playlists(self) -> list[dict[str, Any]]: ...
    async def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]: ...
    async def create_library_playlist(self, name: str, description: str, song_ids: Sequence[str]) -> dict[str, Any]: ...
    async def append_playlist_tracks(self, playlist_id: str, song_ids: Sequence[str]) -> int: ...
    async def verify_readback(self, playlist_id: str, expected_ids: set[str]) -> tuple[list[dict[str, Any]], set[str], list[str]]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so Authorization and Cookie headers never leak."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _validate_base_url(base_url: str) -> tuple[str, bool]:
    raw = (base_url or "https://amp-api.music.apple.com").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and host in _LOOPBACK_HOSTS:
        return raw, True
    if parsed.scheme == "https" and host in _ALLOWED_PUBLIC_HOSTS and not parsed.path:
        return raw, False
    raise AppleMusicApiError("invalid_argument", f"不支持的 Apple Music API 地址: {host or raw}")


def _format_quality_label(audio_traits: Any) -> str:
    traits = set(audio_traits) if isinstance(audio_traits, list) else set()
    parts = ["Hi-Res Lossless"] if "hi-res-lossless" in traits else (["ALAC"] if "lossless" in traits else ["AAC"])
    if "atmos" in traits:
        parts.append("Dolby Atmos")
    return " · ".join(parts)


def extract_catalog_ids(tracks: Sequence[Mapping[str, Any]]) -> set[str]:
    """Extract catalog song IDs from Apple Music library/catalog track nodes."""
    ids: set[str] = set()
    for track in tracks:
        if not isinstance(track, Mapping):
            continue
        if track.get("type") == "songs" and _SONG_ID_RE.match(str(track.get("id") or "")):
            ids.add(str(track["id"]))
        attrs = track.get("attributes")
        if isinstance(attrs, Mapping) and isinstance(attrs.get("playParams"), Mapping):
            params = attrs["playParams"]
            for key in ("catalogId", "id"):
                val = str(params.get(key) or "")
                if _SONG_ID_RE.match(val):
                    ids.add(val)
        rels = track.get("relationships")
        if isinstance(rels, Mapping) and isinstance(rels.get("catalog"), Mapping):
            for item in rels["catalog"].get("data") or ():
                if isinstance(item, Mapping) and item.get("type") == "songs":
                    val = str(item.get("id") or "")
                    if _SONG_ID_RE.match(val):
                        ids.add(val)
    return ids


def extract_ordered_catalog_ids(tracks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Extract unambiguous ordered catalog song IDs from track readback."""
    ordered: list[str] = []
    for track in tracks:
        ids = extract_catalog_ids([track])
        if len(ids) != 1:
            raise AppleMusicApiError("readback_verification_failed", "歌单读回条目缺少唯一目录曲目 ID")
        ordered.append(next(iter(ids)))
    return ordered


@dataclass(frozen=True, slots=True)
class AppleMusicCredentials:
    """In-memory Apple Music credentials sourced exclusively from SecretVault."""

    developer_token: str = ""
    user_token: str = ""
    cookie: str = ""

    @property
    def has_catalog_auth(self) -> bool:
        return bool(self.developer_token.strip())

    @property
    def has_write_auth(self) -> bool:
        return bool(self.developer_token.strip() and self.user_token.strip())

    def secret_values(self) -> tuple[str, ...]:
        return tuple(v for v in (self.developer_token, self.user_token, self.cookie) if v)


class AppleMusicClient:
    """Thread-safe async wrapper around Apple Music Catalog and Library HTTP APIs."""

    def __init__(
        self,
        credentials: AppleMusicCredentials,
        *,
        base_url: str = "https://amp-api.music.apple.com",
        proxy: str = "",
        timeout_sec: float = 12.0,
        readback_attempts: int = 3,
        readback_delay_sec: float = 0.15,
    ) -> None:
        self._creds = credentials
        self._base_url, self._is_loopback = _validate_base_url(base_url)
        self._timeout = max(1.0, float(timeout_sec))
        self._readback_attempts = max(1, int(readback_attempts))
        self._readback_delay = max(0.0, float(readback_delay_sec))
        proxy_clean = (proxy or "").strip()
        proxy_handler = urllib.request.ProxyHandler({}) if (self._is_loopback or not proxy_clean) else urllib.request.ProxyHandler({"http": proxy_clean, "https": proxy_clean})
        self._opener = urllib.request.build_opener(proxy_handler, _NoRedirectHandler())

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._creds.secret_values())

    def _request_sync(self, path: str, method: str = "GET", body: Any = None) -> tuple[int, dict[str, Any]]:
        if not path.startswith("/v1/") or ".." in path or any(c in path for c in "\r\n#\\"):
            raise AppleMusicApiError("invalid_argument", "非法的 Apple Music API 路径")
        if not self._creds.has_catalog_auth:
            raise AppleMusicApiError("not_configured", "尚未配置 Apple Music Developer Token")
        if path.startswith("/v1/me/") and not self._creds.has_write_auth:
            raise AppleMusicApiError("not_configured", "尚未配置 Apple Music Music-User-Token")

        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._creds.developer_token.strip()}",
            "Origin": "https://music.apple.com",
            "Accept": "application/json",
        }
        if method == "POST" or body is not None:
            headers["Content-Type"] = "application/json"
        if mut := self._creds.user_token.strip():
            headers["Music-User-Token"] = mut
            headers["Media-User-Token"] = mut
            headers["Cookie"] = self._creds.cookie.strip() or f"media-user-token={mut}"
        elif ck := self._creds.cookie.strip():
            headers["Cookie"] = ck

        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._base_url + path, headers=headers, data=data, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(parsed, dict):
                    raise AppleMusicApiError("service_offline", "Apple Music 响应格式无效")
                return resp.status, parsed
        except urllib.error.HTTPError as err:
            code = err.code
            if code == 429:
                raise AppleMusicApiError("rate_limited", "Apple Music API 触发限流 (HTTP 429)", status=code) from None
            if code in (401, 403):
                raise AppleMusicApiError("service_offline", f"Apple Music 鉴权被拒绝 (HTTP {code})", status=code) from None
            raise AppleMusicApiError("service_offline", f"Apple Music 上游错误 (HTTP {code})", status=code) from None
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppleMusicApiError("service_offline", f"Apple Music 上游不可达: {self._redact(str(exc) or type(exc).__name__)}") from None

    async def request(self, path: str, method: str = "GET", body: Any = None) -> tuple[int, dict[str, Any]]:
        return await asyncio.to_thread(self._request_sync, path, method, body)

    def _collection_sync(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        visited: set[str] = set()
        next_path: str | None = path
        while next_path:
            if next_path in visited:
                raise AppleMusicApiError("service_offline", "Apple Music 分页返回重复游标")
            visited.add(next_path)
            _, payload = self._request_sync(next_path)
            if payload.get("errors") or not isinstance(payload.get("data"), list):
                raise AppleMusicApiError("service_offline", "Apple Music 集合响应格式无效")
            items.extend(node for node in payload["data"] if isinstance(node, dict))
            raw_next = payload.get("next")
            next_path = str(raw_next) if isinstance(raw_next, str) and raw_next else None
        return items

    async def preflight_cn_catalog(self, track_ids: Sequence[str], storefront: str = "cn") -> dict[str, Any]:
        sf = (storefront or "").strip().lower()
        if sf != "cn":
            raise AppleMusicApiError("invalid_argument", "国区预检仅支持 storefront='cn'")
        ids = [str(tid).strip() for tid in track_ids]
        if not ids or any(not _SONG_ID_RE.match(tid) for tid in ids):
            raise AppleMusicApiError("invalid_argument", "track_ids 必须是非空的数字歌曲 ID 列表")

        deduped = list(dict.fromkeys(ids))
        returned_nodes: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(deduped), 100):
            query = urllib.parse.urlencode({"ids": ",".join(deduped[offset : offset + 100])})
            _, body = await self.request(f"/v1/catalog/{sf}/songs?{query}")
            if body.get("errors") or not isinstance(body.get("data"), list):
                raise AppleMusicApiError("service_offline", "Apple Music 国区目录响应异常")
            for node in body["data"]:
                if isinstance(node, dict) and node.get("id"):
                    returned_nodes[str(node["id"])] = node

        verified_ids: list[str] = []
        unavailable: list[str] = []
        not_found: list[str] = []
        unplayable_ids: list[str] = []
        tracks: list[dict[str, Any]] = []
        for sid in deduped:
            node = returned_nodes.get(sid)
            if node is None:
                not_found.append(sid)
                unplayable_ids.append(sid)
                continue
            attrs = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
            play_params = attrs.get("playParams") if isinstance(attrs.get("playParams"), Mapping) else None
            title = str(attrs.get("name") or "").strip()
            playable = (
                node.get("type") == "songs"
                and play_params is not None
                and str(play_params.get("id") or "") == sid
                and play_params.get("kind") == "song"
                and attrs.get("isStreamable", True) is not False
                and bool(title)
            )
            if not playable:
                unavailable.append(sid)
                unplayable_ids.append(sid)
                continue
            raw_art = str((attrs.get("artwork") or {}).get("url") or "") if isinstance(attrs.get("artwork"), Mapping) else ""
            traits = attrs.get("audioTraits") if isinstance(attrs.get("audioTraits"), list) else []
            verified_ids.append(sid)
            tracks.append({
                "id": sid, "title": title, "artist": str(attrs.get("artistName") or ""),
                "album": str(attrs.get("albumName") or ""), "storefront": sf,
                "duration_sec": float(attrs.get("durationInMillis", 0) or 0) / 1000.0,
                "has_lyrics": bool(attrs.get("hasLyrics", False)),
                "cover_url": raw_art.replace("{w}", "500").replace("{h}", "500") if raw_art else "",
                "quality_label": _format_quality_label(traits),
                "url": f"https://music.apple.com/{sf}/song/{sid}",
            })
        return {
            "storefront": sf, "requested_count": len(deduped), "verified_count": len(verified_ids),
            "all_playable": len(verified_ids) == len(deduped) and not unplayable_ids,
            "verified_ids": verified_ids, "unavailable": unavailable, "not_found": not_found,
            "unplayable_ids": unplayable_ids, "tracks": tracks, "error": "",
            "verification_source": "apple_music_live_catalog",
        }

    async def get_playlist_meta(self, playlist_id: str) -> dict[str, Any]:
        pid = (playlist_id or "").strip()
        if not _PLAYLIST_ID_RE.match(pid):
            raise AppleMusicApiError("invalid_argument", "无效的 Apple Music 歌单 ID")
        _, body = await self.request(f"/v1/me/library/playlists/{pid}")
        data = body.get("data")
        if body.get("errors") or not isinstance(data, list) or len(data) != 1:
            raise AppleMusicApiError("service_offline", "无法核验 Apple Music 歌单元数据")
        node = data[0]
        attrs = node.get("attributes") if isinstance(node, Mapping) else None
        if (
            not isinstance(node, Mapping) or node.get("id") != pid or node.get("type") != "library-playlists"
            or not isinstance(attrs, Mapping) or not str(attrs.get("name") or "").strip()
            or attrs.get("canEdit") is False
        ):
            raise AppleMusicApiError("service_offline", "Apple Music 歌单不可编辑或元数据异常")
        return {"id": pid, "name": str(attrs["name"]), "canEdit": bool(attrs.get("canEdit", True))}

    async def list_library_playlists(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._collection_sync, "/v1/me/library/playlists?limit=100")

    async def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        pid = (playlist_id or "").strip()
        if not _PLAYLIST_ID_RE.match(pid):
            raise AppleMusicApiError("invalid_argument", "无效的 Apple Music 歌单 ID")
        return await asyncio.to_thread(self._collection_sync, f"/v1/me/library/playlists/{pid}/tracks?limit=100&include=catalog")

    async def create_library_playlist(self, name: str, description: str, song_ids: Sequence[str]) -> dict[str, Any]:
        payload = {
            "attributes": {"name": name, "description": description},
            "relationships": {"tracks": {"data": [{"id": str(sid), "type": "songs"} for sid in song_ids]}},
        }
        status, body = await self.request("/v1/me/library/playlists", method="POST", body=payload)
        data = body.get("data")
        if status not in (200, 201) or body.get("errors") or not isinstance(data, list) or not data:
            raise AppleMusicApiError("service_offline", "创建 Apple Music 歌单失败")
        pid = str(data[0].get("id") or "")
        if not pid:
            raise AppleMusicApiError("service_offline", "创建歌单未返回 playlist_id")
        return {"id": pid, "name": str(data[0].get("attributes", {}).get("name") or name)}

    async def append_playlist_tracks(self, playlist_id: str, song_ids: Sequence[str]) -> int:
        pid = (playlist_id or "").strip()
        if not _PLAYLIST_ID_RE.match(pid):
            raise AppleMusicApiError("invalid_argument", "无效的 Apple Music 歌单 ID")
        if not song_ids:
            return 204
        body = {"data": [{"id": str(sid), "type": "songs"} for sid in song_ids]}
        status, resp = await self.request(f"/v1/me/library/playlists/{pid}/tracks", method="POST", body=body)
        if status not in (200, 201, 202, 204) or resp.get("errors"):
            raise AppleMusicApiError("service_offline", f"追加歌单曲目失败 (HTTP {status})", status=status)
        return status

    async def verify_readback(self, playlist_id: str, expected_ids: set[str]) -> tuple[list[dict[str, Any]], set[str], list[str]]:
        last_error: AppleMusicApiError | None = None
        for attempt in range(self._readback_attempts):
            try:
                tracks = await self.get_playlist_tracks(playlist_id)
                present = extract_catalog_ids(tracks)
                if expected_ids <= present:
                    return tracks, present, extract_ordered_catalog_ids(tracks)
                last_error = AppleMusicApiError(
                    "readback_verification_failed",
                    "歌单写回后读回校验缺少目标曲目",
                    details={"missing_ids": sorted(expected_ids - present)},
                )
            except AppleMusicApiError as exc:
                last_error = exc
                if exc.status not in (None, 404, 429, 500, 502, 503, 504):
                    raise
            if attempt + 1 < self._readback_attempts and self._readback_delay > 0:
                await asyncio.sleep(self._readback_delay)
        assert last_error is not None
        raise last_error
