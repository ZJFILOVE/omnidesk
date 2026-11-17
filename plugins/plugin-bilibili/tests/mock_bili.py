"""Offline mock Bilibili HTTP server and fake yt-dlp helpers for plugin-bilibili tests."""

from __future__ import annotations

import http.server
import json
import stat
import sys
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MockBiliState:
    """Mutable state controlling offline mock Bilibili HTTP responses."""

    allow_4k: bool = False
    qr_stage: str = "waiting"  # waiting | scanned | expired | confirmed
    nav_logged_in: bool = True
    force_http_status: int | None = None
    force_bili_code: int | None = None
    delay_sec: float = 0.0
    redirect_to: str = ""
    deflate_danmaku: bool = False
    has_subtitles: bool = True
    multi_part: bool = True
    empty_feeds: bool = False
    malformed_json: bool = False
    legacy_paths_only: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)


class MockBiliServer:
    """Context-managed local 127.0.0.1 HTTP server simulating Bilibili APIs."""

    def __init__(self, state: MockBiliState | None = None) -> None:
        self.state = state or MockBiliState()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> MockBiliServer:
        state = self.state

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, payload: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _send_bytes(self, body: bytes, *, content_type: str = "text/xml; charset=utf-8", status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)
                state.requests.append(
                    {
                        "path": path,
                        "query": qs,
                        "cookie": self.headers.get("Cookie", ""),
                        "referer": self.headers.get("Referer", ""),
                    }
                )
                if state.delay_sec > 0:
                    time.sleep(state.delay_sec)
                if state.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", state.redirect_to)
                    self.end_headers()
                    return
                if state.force_http_status is not None:
                    self._send_json({"code": -1, "message": "forced error"}, status=state.force_http_status)
                    return
                if state.malformed_json:
                    self._send_bytes(b"{not-valid-json", content_type="application/json")
                    return
                if state.force_bili_code is not None:
                    self._send_json({"code": state.force_bili_code, "message": f"bili error {state.force_bili_code}"})
                    return

                host_base = f"http://127.0.0.1:{self.server.server_address[1]}"
                if path == "/x/web-interface/nav":
                    if not state.nav_logged_in or not self.headers.get("Cookie"):
                        self._send_json({"code": -101, "message": "账号未登录", "data": {"isLogin": False}})
                    else:
                        self._send_json({
                            "code": 0,
                            "data": {
                                "isLogin": True, "mid": 424242, "uname": "OmniTester",
                                "vipStatus": 1 if state.allow_4k else 0, "vipType": 2 if state.allow_4k else 0,
                                "vip_label": {"text": "年度大会员" if state.allow_4k else ""},
                                "wbi_img": {
                                    "img_url": f"{host_base}/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                                    "sub_url": f"{host_base}/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                                },
                            },
                        })
                    return

                if state.legacy_paths_only and "/wbi/" in path:
                    self._send_json({"code": -404}, status=404)
                    return

                if path in ("/x/web-interface/wbi/index/top/feed/rcmd", "/x/web-interface/index/top/feed/rcmd"):
                    items = [] if state.empty_feeds else [{
                        "id": 10001, "bvid": "BV1xK4y1Z7aa", "cid": 88001,
                        "title": "Wayland + libmpv 硬解实践",
                        "pic": "//i0.hdslb.com/bfs/archive/cover1.jpg", "duration": 1120,
                        "owner": {"mid": 101, "name": "极客图形社"}, "stat": {"view": 54321},
                    }]
                    self._send_json({"code": 0, "data": {"item": items}})
                    return

                if path == "/x/polymer/web-dynamic/v1/feed/all":
                    if not self.headers.get("Cookie"):
                        self._send_json({"code": -101, "message": "账号未登录"})
                        return
                    items = [] if state.empty_feeds else [{
                        "id_str": "dyn-1", "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_author": {"name": "文档排版工坊", "mid": 202},
                            "module_dynamic": {"major": {"archive": {
                                "bvid": "BV1bB4y1N7kp", "aid": "20002", "title": "BabelDOC 学术 PDF 解析",
                                "cover": "https://i0.hdslb.com/bfs/archive/cover2.jpg",
                                "duration_text": "15:40", "desc": "双栏基线重排", "stat": {"play": "12000"},
                            }}},
                        },
                    }]
                    self._send_json({"code": 0, "data": {"items": items}})
                    return

                if path == "/x/space/bangumi/follow/list":
                    items = [] if state.empty_feeds else [{
                        "season_id": 45678, "media_id": 9901, "title": "葬送的芙莉莲",
                        "cover": "//i0.hdslb.com/bfs/bangumi/frieren.jpg", "badge": "会员专享", "evaluate": "魔法与时间之旅",
                        "new_ep": {"id": 778899, "index_show": "全28话", "duration": 1440},
                        "url": "https://www.bilibili.com/bangumi/play/ss45678",
                    }]
                    self._send_json({"code": 0, "data": {"list": items}})
                    return

                if path == "/xlive/web-interface/v1/second/getListByArea":
                    items = (
                        []
                        if state.empty_feeds
                        else [
                            {
                                "roomid": 9001,
                                "uid": 303,
                                "uname": "Linux主播",
                                "title": "Hyprland 桌面调试",
                                "cover": "//i0.hdslb.com/bfs/live/9001.jpg",
                                "online": 4321,
                                "area_name": "科技科普",
                            }
                        ]
                    )
                    self._send_json({"code": 0, "data": {"list": items}})
                    return

                if path in ("/x/web-interface/wbi/search/type", "/x/web-interface/search/type"):
                    kw = (qs.get("keyword") or [""])[0]
                    items = (
                        []
                        if state.empty_feeds or kw == "nothing-here"
                        else [
                            {
                                "bvid": "BV1Q5411W7x1",
                                "aid": 30003,
                                "title": f'<em class="keyword">{kw}</em> 实战教程',
                                "pic": "//i0.hdslb.com/bfs/archive/search1.jpg",
                                "duration": "12:34",
                                "author": "测试UP主",
                                "mid": 404,
                                "play": 9876,
                                "description": "搜索结果摘要",
                            }
                        ]
                    )
                    self._send_json({"code": 0, "data": {"result": items}})
                    return

                if path == "/x/player/pagelist":
                    pages = (
                        [
                            {"cid": 88001, "page": 1, "part": "导论", "duration": 300},
                            {"cid": 88002, "page": 2, "part": "进阶实现", "duration": 420},
                        ]
                        if state.multi_part
                        else [{"cid": 88001, "page": 1, "part": "正片", "duration": 600}]
                    )
                    self._send_json({"code": 0, "data": pages})
                    return

                if path in ("/x/player/wbi/playurl", "/x/player/playurl"):
                    has_4k = state.allow_4k
                    videos = [
                        {"id": 80, "baseUrl": f"{host_base}/cdn/1080.m4s", "codecs": "avc1", "width": 1920, "height": 1080, "frameRate": "60", "bandwidth": 3500000},
                        {"id": 64, "baseUrl": f"{host_base}/cdn/720.m4s", "codecs": "avc1", "width": 1280, "height": 720, "frameRate": "30", "bandwidth": 1500000},
                    ]
                    accept = [80, 64, 32, 16]
                    if has_4k:
                        videos.insert(0, {"id": 120, "baseUrl": f"{host_base}/cdn/4k.m4s", "codecs": "hev1", "width": 3840, "height": 2160, "frameRate": "60", "bandwidth": 12000000})
                        accept.insert(0, 120)
                    req_qn = int((qs.get("qn") or ["80"])[0])
                    actual_qn = req_qn if req_qn in accept else 80
                    self._send_json(
                        {
                            "code": 0,
                            "data": {
                                "quality": actual_qn,
                                "accept_quality": accept,
                                "timelength": 1120000,
                                "dash": {
                                    "video": videos,
                                    "audio": [{"id": 30280, "baseUrl": f"{host_base}/cdn/audio.m4s", "codecs": "mp4a", "bandwidth": 192000}],
                                },
                            },
                        }
                    )
                    return

                if path == "/xlive/web-room/v2/index/getRoomPlayInfo":
                    self._send_json(
                        {
                            "code": 0,
                            "data": {
                                "live_status": 1,
                                "playurl_info": {
                                    "playurl": {
                                        "stream": [
                                            {
                                                "format": [
                                                    {
                                                        "codec": [
                                                            {
                                                                "codec_name": "avc",
                                                                "current_qn": 10000,
                                                                "base_url": "/live-bvc/9001.flv",
                                                                "url_info": [{"host": host_base, "extra": "?sign=abc"}],
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                },
                            },
                        }
                    )
                    return

                if path in ("/x/player/wbi/v2", "/x/player/v2"):
                    subs = (
                        [{"id": 1, "lan": "en-US", "lan_doc": "英语", "subtitle_url": f"{host_base}/mock/sub/en.json"}]
                        if state.has_subtitles
                        else []
                    )
                    self._send_json({"code": 0, "data": {"subtitle": {"subtitles": subs}, "view_points": []}})
                    return

                if path == "/mock/sub/en.json":
                    self._send_json(
                        {
                            "body": [
                                {"from": 0.5, "to": 2.5, "content": "Hello Wayland"},
                                {"from": 3.0, "to": 5.5, "content": "Zero copy pipeline"},
                            ]
                        }
                    )
                    return

                if path == "/x/v1/dm/list.so":
                    xml_str = (
                        '<?xml version="1.0" encoding="UTF-8"?><i>'
                        '<d p="12.500,1,25,16777215,1700000002,0,u2,202">后发先至</d>'
                        '<d p="3.200,5,25,16711680,1700000001,0,u1,201">高能预警</d>'
                        "</i>"
                    )
                    raw = xml_str.encode("utf-8")
                    if state.deflate_danmaku:
                        comp = zlib.compressobj(wbits=-zlib.MAX_WBITS)
                        raw = comp.compress(raw) + comp.flush()
                    self._send_bytes(raw)
                    return

                if path == "/x/passport-login/web/qrcode/generate":
                    self._send_json(
                        {
                            "code": 0,
                            "data": {
                                "url": "https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1&qrcode_key=mockqrkey123456&from=",
                                "qrcode_key": "mockqrkey123456",
                            },
                        }
                    )
                    return

                if path == "/x/passport-login/web/qrcode/poll":
                    stage = state.qr_stage
                    if stage == "waiting":
                        self._send_json({"code": 0, "data": {"code": 86101, "message": "未扫码"}})
                    elif stage == "scanned":
                        self._send_json({"code": 0, "data": {"code": 86090, "message": "已扫码未确认"}})
                    elif stage == "expired":
                        self._send_json({"code": 0, "data": {"code": 86038, "message": "二维码已失效"}})
                    else:
                        self._send_json(
                            {
                                "code": 0,
                                "data": {
                                    "code": 0,
                                    "message": "",
                                    "refresh_token": "mock_refresh_token_999",
                                    "url": "https://www.bilibili.com/?DedeUserID=424242&SESSDATA=secret_sess_abcdef123456&bili_jct=secret_jct_7890",
                                },
                            },
                            headers={
                                "Set-Cookie": "SESSDATA=secret_sess_abcdef123456; Path=/; HttpOnly",
                            },
                        )
                    return

                self._send_json({"code": -404, "message": f"unknown path {path}"}, status=404)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def write_fake_ytdlp(tmp_path: Path, body: str) -> Path:
    """Write an executable fake yt-dlp script into `tmp_path`."""
    script = tmp_path / "fake-yt-dlp"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(stat.S_IRWXU)
    return script
