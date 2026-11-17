"""Offline local http.server fixtures simulating IMDb/TMDB, Radarr, Sonarr, and redirect/timeout traps."""

from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockServerState:
    """Mutable state inspected and configured by offline unit tests."""

    mode: str = "ok"  # ok | empty | unauthorized | conflict_post | conflict_400 | redirect | timeout | server_error
    delay_sec: float = 0.0
    expected_api_key: str = ""
    redirect_target: str = ""
    requests: list[tuple[str, str, dict[str, str], Any]] = field(default_factory=list)
    existing_movies: list[dict[str, Any]] = field(default_factory=list)
    existing_series: list[dict[str, Any]] = field(default_factory=list)
    trap_hits: int = 0


class _RouterHandler(http.server.BaseHTTPRequestHandler):
    state: MockServerState
    kind: str

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _record(self, method: str, body: Any = None) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.state.requests.append((method, self.path, headers, body))

    def _check_common_faults(self, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path == "/redirect-trap":
            self.state.trap_hits += 1
            self._send_json(200, {"trapped": True})
            return True
        if self.state.delay_sec > 0:
            time.sleep(self.state.delay_sec)
        if self.state.mode == "redirect":
            target = self.state.redirect_target or "/redirect-trap"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if self.state.mode == "unauthorized":
            secret = self.state.expected_api_key or self.headers.get("X-Api-Key", "")
            self._send_json(401, {"message": f"Invalid X-Api-Key: {secret} api_key={secret}"})
            return True
        if self.state.mode == "server_error":
            self._send_json(502, {"error": "Bad Gateway"})
            return True
        if self.state.expected_api_key:
            hdr_key = self.headers.get("X-Api-Key", "")
            auth = self.headers.get("Authorization", "")
            qs = urllib.parse.parse_qs(parsed.query)
            q_key = (qs.get("api_key") or [""])[0]
            if self.state.expected_api_key not in {hdr_key, q_key, auth.removeprefix("Bearer ").strip()}:
                self._send_json(401, {"message": f"Unauthorized key={self.state.expected_api_key}"})
                return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        self._record("GET")
        parsed = urllib.parse.urlparse(self.path)
        if self._check_common_faults(parsed):
            return
        if self.kind == "imdb":
            self._handle_imdb_get(parsed)
        elif self.kind == "tmdb":
            self._handle_tmdb_get(parsed)
        elif self.kind == "radarr":
            self._handle_radarr_get(parsed)
        elif self.kind == "sonarr":
            self._handle_sonarr_get(parsed)
        else:
            self._send_json(404, {"error": "unknown server kind"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        self._record("POST", body)
        parsed = urllib.parse.urlparse(self.path)
        if self._check_common_faults(parsed):
            return
        if self.state.mode == "conflict_post":
            self._send_json(409, {"error": "Already exists in library"})
            return
        if self.state.mode == "conflict_400":
            self._send_json(400, [{"propertyName": "TmdbId", "errorMessage": "This movie has already been added", "errorCode": "MovieExistsValidator"}])
            return
        if self.kind == "radarr" and parsed.path == "/api/v3/movie":
            created = {**(body or {}), "id": 101, "hasFile": False, "status": "announced"}
            self.state.existing_movies.append(created)
            self._send_json(201, created)
            return
        if self.kind == "sonarr" and parsed.path == "/api/v3/series":
            created = {**(body or {}), "id": 202, "status": "continuing"}
            self.state.existing_series.append(created)
            self._send_json(201, created)
            return
        self._send_json(404, {"error": "not found"})

    def _handle_imdb_get(self, parsed: urllib.parse.ParseResult) -> None:
        if self.state.mode == "empty":
            self._send_json(200, {"items": []})
            return
        if parsed.path in {"/charts/popular", "/charts/top_rated", "/charts/upcoming", "/charts/tv_popular", "/charts/tv_top_rated"}:
            cat = parsed.path.rsplit("/", 1)[-1]
            self._send_json(
                200,
                {
                    "items": [
                        {
                            "imdb_id": "tt1375666",
                            "tmdb_id": 27205,
                            "title": "Inception",
                            "year": 2010,
                            "rating": 8.8,
                            "poster_url": "https://posters.example/inception.jpg",
                            "plot": f"A thief enters dreams ({cat}).",
                            "genres": ["Action", "Sci-Fi"],
                            "directors": ["Christopher Nolan"],
                            "media_type": "tv" if cat.startswith("tv_") else "movie",
                        }
                    ]
                },
            )
            return
        if parsed.path == "/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0]
            if q == "nonexistent":
                self._send_json(200, {"results": []})
                return
            self._send_json(200, {"results": [{"id": "tt0903747", "title": "Breaking Bad", "year": 2008, "rating": 9.5, "genres": ["Crime", "Drama"], "media_type": "tv"}]})
            return
        if parsed.path == "/title/tt1375666":
            self._send_json(
                200,
                {
                    "imdb_id": "tt1375666",
                    "tmdb_id": 27205,
                    "title": "Inception",
                    "year": 2010,
                    "directors": ["Christopher Nolan"],
                    "cast": ["Leonardo DiCaprio", "Joseph Gordon-Levitt"],
                    "runtime_minutes": 148,
                    "genres": ["Action", "Sci-Fi", "Thriller"],
                    "rating": 8.8,
                    "overview": "A thief who steals corporate secrets through dream-sharing technology.",
                    "poster_url": "https://posters.example/inception.jpg",
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def _handle_tmdb_get(self, parsed: urllib.parse.ParseResult) -> None:
        if self.state.mode == "empty":
            self._send_json(200, {"results": []})
            return
        if parsed.path == "/movie/popular":
            self._send_json(
                200,
                {
                    "page": 1,
                    "results": [
                        {
                            "id": 157336,
                            "title": "Interstellar",
                            "release_date": "2014-11-05",
                            "vote_average": 8.4,
                            "poster_path": "/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg",
                            "overview": "Explorers travel through a wormhole in space.",
                            "genre_ids": [12, 18, 878],
                        }
                    ],
                },
            )
            return
        if parsed.path == "/find/tt0816692":
            self._send_json(200, {"movie_results": [{"id": 157336}], "tv_results": []})
            return
        if parsed.path == "/find/tt9999999":
            self._send_json(200, {"movie_results": [], "tv_results": []})
            return
        if parsed.path == "/movie/157336":
            self._send_json(
                200,
                {
                    "id": 157336,
                    "title": "Interstellar",
                    "release_date": "2014-11-05",
                    "vote_average": 8.4,
                    "runtime": 169,
                    "overview": "Explorers travel through a wormhole in space.",
                    "poster_path": "/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg",
                    "genres": [{"id": 12, "name": "Adventure"}, {"id": 878, "name": "Science Fiction"}],
                    "external_ids": {"imdb_id": "tt0816692"},
                    "credits": {
                        "crew": [{"job": "Director", "name": "Christopher Nolan"}],
                        "cast": [{"name": "Matthew McConaughey"}, {"name": "Anne Hathaway"}],
                    },
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def _handle_radarr_get(self, parsed: urllib.parse.ParseResult) -> None:
        if parsed.path == "/api/v3/movie/lookup":
            if self.state.mode == "empty":
                self._send_json(200, [])
                return
            self._send_json(
                200,
                [
                    {
                        "title": "Inception",
                        "year": 2010,
                        "tmdbId": 27205,
                        "imdbId": "tt1375666",
                        "titleSlug": "inception-27205",
                        "overview": "Dream heist.",
                        "status": "released",
                        "monitored": False,
                        "hasFile": False,
                    }
                ],
            )
            return
        if parsed.path == "/api/v3/movie":
            self._send_json(200, self.state.existing_movies)
            return
        if parsed.path == "/api/v3/qualityprofile":
            self._send_json(200, [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}])
            return
        if parsed.path == "/api/v3/rootfolder":
            self._send_json(200, [{"id": 1, "path": "/movies"}])
            return
        if parsed.path == "/api/v3/queue":
            if self.state.mode == "empty":
                self._send_json(200, {"page": 1, "pageSize": 20, "totalRecords": 0, "records": []})
                return
            self._send_json(
                200,
                {
                    "page": 1,
                    "pageSize": 20,
                    "totalRecords": 1,
                    "records": [
                        {
                            "id": 77,
                            "movieId": 101,
                            "title": "Inception.2010.1080p.BluRay",
                            "status": "downloading",
                            "trackedDownloadStatus": "ok",
                            "trackedDownloadState": "downloading",
                            "protocol": "torrent",
                            "downloadClient": "qBittorrent",
                            "size": 1000,
                            "sizeleft": 250,
                            "timeleft": "00:05:00",
                            "movie": {"id": 101, "title": "Inception"},
                            "statusMessages": [{"title": "Release", "messages": ["Downloading"]}],
                        }
                    ],
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def _handle_sonarr_get(self, parsed: urllib.parse.ParseResult) -> None:
        if parsed.path == "/api/v3/series/lookup":
            if self.state.mode == "empty":
                self._send_json(200, [])
                return
            self._send_json(
                200,
                [
                    {
                        "title": "Breaking Bad",
                        "year": 2008,
                        "tvdbId": 81189,
                        "imdbId": "tt0903747",
                        "titleSlug": "breaking-bad",
                        "overview": "Chemistry teacher turns to crime.",
                        "status": "ended",
                        "seasons": [{"seasonNumber": 1, "monitored": True}],
                    }
                ],
            )
            return
        if parsed.path == "/api/v3/series":
            self._send_json(200, self.state.existing_series)
            return
        if parsed.path == "/api/v3/qualityprofile":
            self._send_json(200, [{"id": 6, "name": "HD-1080p"}])
            return
        if parsed.path == "/api/v3/rootfolder":
            self._send_json(200, [{"id": 1, "path": "/tv"}])
            return
        if parsed.path == "/api/v3/queue":
            self._send_json(
                200,
                {
                    "page": 1,
                    "pageSize": 20,
                    "totalRecords": 1,
                    "records": [
                        {
                            "id": 88,
                            "seriesId": 202,
                            "episodeId": 9001,
                            "title": "Breaking.Bad.S01E01.1080p",
                            "status": "downloading",
                            "trackedDownloadStatus": "ok",
                            "trackedDownloadState": "downloading",
                            "protocol": "usenet",
                            "downloadClient": "SABnzbd",
                            "size": 500,
                            "sizeleft": 100,
                            "series": {"id": 202, "title": "Breaking Bad"},
                            "episode": {"id": 9001, "seasonNumber": 1, "episodeNumber": 1},
                        }
                    ],
                },
            )
            return
        self._send_json(404, {"error": "not found"})


class RunningMockServer:
    """Context manager launching a local 127.0.0.1 ThreadingHTTPServer."""

    def __init__(self, kind: str, state: MockServerState | None = None) -> None:
        self.kind = kind
        self.state = state or MockServerState()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> RunningMockServer:
        handler_cls = type(
            f"_Bound{self.kind.title()}Handler",
            (_RouterHandler,),
            {"state": self.state, "kind": self.kind},
        )
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
