"""Offline tests for the live Apple Music chart parser and `media.charts.v1` handler."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import plugin as music_plugin_module
from charts import ChartFetchError, build_chart_url, parse_chart_payload
from conftest import PLUGIN_ROOT, build_plugin_context, run_async


def _payload() -> dict[str, Any]:
    return {
        "feed": {
            "title": "热门歌曲排行",
            "updated": "Tue, 17 Nov 2026 02:58:02 +0000",
            "results": [
                {
                    "id": "6829122414",
                    "name": "Hong Kong (feat. 王菲季候風)",
                    "artistName": "The Weeknd",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/100x100bb.jpg",
                    "url": "https://music.apple.com/cn/album/x/6829122413?i=6829122414",
                    "releaseDate": "2026-10-31",
                },
                {"id": "not-a-song", "name": "Bad row", "artistName": "Nobody"},
            ],
        }
    }


def test_chart_url_and_payload_parser_are_strict() -> None:
    assert build_chart_url("cn", 10) == (
        "https://rss.marketingtools.apple.com/api/v2/cn/music/most-played/10/songs.json"
    )
    with pytest.raises(ChartFetchError):
        build_chart_url("cn", 0)
    with pytest.raises(ChartFetchError):
        build_chart_url("../evil", 5)

    items = parse_chart_payload(_payload(), storefront="cn", limit=5)
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "am-cn-6829122414"
    assert item["title"] == "Hong Kong (feat. 王菲季候風)"
    assert item["creator"] == "The Weeknd"
    assert item["subtitle"] == "The Weeknd · 国区实时榜 #1"
    assert item["extra"]["apple_music_song_id"] == "6829122414"
    assert item["extra"]["chart_rank"] == 1
    assert item["cover_url"].endswith("/600x600bb.jpg")


def test_media_charts_capability_caches_and_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_fetch(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(dict(kwargs))
        return parse_chart_payload(_payload(), storefront="cn", limit=5)

    monkeypatch.setattr(music_plugin_module, "fetch_apple_music_chart", fake_fetch)
    _, ctx = build_plugin_context(tmp_path)

    first = run_async(ctx.capabilities.call("media.charts.v1", {"action": "top_songs", "limit": 5}))
    second = run_async(ctx.capabilities.call("media.charts.v1", {"action": "top_songs", "limit": 5}))
    third = run_async(
        ctx.capabilities.call("media.charts.v1", {"action": "top_songs", "limit": 5, "force_refresh": True})
    )

    assert first.ok and second.ok and third.ok
    assert first.data["cached"] is False
    assert second.data["cached"] is True
    assert third.data["cached"] is False
    assert len(calls) == 2
    assert first.data["items"][0]["extra"]["apple_music_song_id"] == "6829122414"

    bad = run_async(ctx.capabilities.call("media.charts.v1", {"action": "top_songs", "limit": 99}))
    assert not bad.ok
    assert bad.data["error_code"] == "invalid_argument"

    manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert "media.charts.v1" in manifest["provides"]
