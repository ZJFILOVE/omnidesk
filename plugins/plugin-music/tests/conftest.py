"""Offline test fixtures for `plugin-music`: isolated XDG dirs, loopback HTTP server, dual git repos."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]
for _p in (str(PLUGIN_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from omnidesk_sdk import PluginContext  # noqa: E402
from plugin import MusicPlugin  # noqa: E402


def run_async(coro: Any) -> Any:
    """Execute coroutine synchronously so `pytest -c /dev/null` needs no ini flags."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prevent any test from touching the real user home or XDG directories."""
    home = tmp_path / "home"
    for sub in (".local/share", ".local/state", ".config", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    return home


class FakeAppleMusicState:
    """Configurable loopback state simulating CN Apple Music catalog & library."""

    def __init__(self) -> None:
        self.catalog: dict[str, dict[str, Any]] = {}
        self.playlists: dict[str, dict[str, Any]] = {
            "p.YJXV7dEIeb3GzZX": {
                "id": "p.YJXV7dEIeb3GzZX",
                "name": "ChatGPT Daily 5",
                "tracks": [],
            }
        }
        self.requests: list[tuple[str, str, Any]] = []
        self.fail_append_readback: bool = False
        self.force_catalog_status: int | None = None
        self.retry_after: str = "2"
        self.counter = 0

    def seed_playable(self, song_id: str, *, title: str = "", artist: str = "Artist", playable: bool = True) -> None:
        attrs: dict[str, Any] = {
            "name": title or f"Track {song_id}",
            "artistName": artist,
            "albumName": "Test Album",
            "durationInMillis": 215000,
            "url": f"https://music.apple.com/cn/song/{song_id}",
            "playParams": {"id": song_id, "kind": "song"} if playable else None,
        }
        if not playable:
            attrs.pop("playParams", None)
        self.catalog[song_id] = {"id": song_id, "type": "songs", "attributes": attrs}


def _make_handler(state: FakeAppleMusicState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _send(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            state.requests.append(("GET", parsed.path, parsed.query))
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                self._send(401, {"errors": [{"detail": "missing dev token"}]})
                return

            if parsed.path == "/v1/catalog/cn/songs":
                if state.force_catalog_status is not None:
                    hdrs = {"Retry-After": state.retry_after} if state.force_catalog_status == 429 else {}
                    self._send(state.force_catalog_status, {"error": f"forced {state.force_catalog_status}"}, hdrs)
                    return
                ids_param = parse_qs(parsed.query).get("ids", [""])[0]
                data = [state.catalog[i] for i in ids_param.split(",") if i and i in state.catalog]
                self._send(200, {"data": data})
                return

            if not self.headers.get("Music-User-Token"):
                self._send(401, {"errors": [{"detail": "missing user token"}]})
                return

            if parsed.path == "/v1/me/library/playlists":
                items = [
                    {"id": pid, "type": "library-playlists", "attributes": {"name": pl["name"]}}
                    for pid, pl in state.playlists.items()
                ]
                self._send(200, {"data": items})
                return

            if parsed.path.startswith("/v1/me/library/playlists/"):
                rest = parsed.path.removeprefix("/v1/me/library/playlists/").strip("/").split("/")
                pid = rest[0]
                pl = state.playlists.get(pid)
                if pl is None:
                    self._send(404, {"error": "playlist not found"})
                    return
                if len(rest) == 1:
                    self._send(
                        200,
                        {
                            "data": [
                                {
                                    "id": pid,
                                    "type": "library-playlists",
                                    "attributes": {"name": pl["name"], "canEdit": True},
                                }
                            ]
                        },
                    )
                    return
                if len(rest) == 2 and rest[1] == "tracks":
                    data = [
                        {
                            "id": f"i.{idx}",
                            "type": "library-songs",
                            "attributes": {"playParams": {"id": sid, "catalogId": sid}},
                        }
                        for idx, sid in enumerate(pl["tracks"], start=1)
                    ]
                    self._send(200, {"data": data})
                    return

            self._send(404, {"error": f"unhandled GET {parsed.path}"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            state.requests.append(("POST", parsed.path, body))
            if not self.headers.get("Authorization") or not self.headers.get("Music-User-Token"):
                self._send(401, {"error": "unauthorized"})
                return

            if parsed.path == "/v1/me/library/playlists":
                state.counter += 1
                pid = f"p.ix{state.counter}"
                name = str(body.get("attributes", {}).get("name") or f"Playlist {state.counter}")
                tracks = [
                    str(item.get("id"))
                    for item in body.get("relationships", {}).get("tracks", {}).get("data", [])
                ]
                state.playlists[pid] = {"id": pid, "name": name, "tracks": tracks}
                self._send(
                    201,
                    {
                        "data": [
                            {
                                "id": pid,
                                "type": "library-playlists",
                                "attributes": {"name": name, "canEdit": True},
                            }
                        ]
                    },
                )
                return

            if parsed.path.startswith("/v1/me/library/playlists/") and parsed.path.endswith("/tracks"):
                pid = parsed.path.removeprefix("/v1/me/library/playlists/").removesuffix("/tracks").strip("/")
                pl = state.playlists.get(pid)
                if pl is None:
                    self._send(404, {"error": "playlist not found"})
                    return
                if not state.fail_append_readback:
                    for item in body.get("data", []):
                        sid = str(item.get("id") or "")
                        if sid:
                            pl["tracks"].append(sid)
                self._send(204, {})
                return

            self._send(404, {"error": f"unhandled POST {parsed.path}"})

    return _Handler


@pytest.fixture()
def fake_apple_server() -> tuple[FakeAppleMusicState, str]:
    """Start a local loopback HTTP server mimicking Apple Music endpoints."""
    state = FakeAppleMusicState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def git_commit_all(repo: Path, message: str) -> str:
    """Initialize if needed and create a clean commit in `repo`, returning HEAD sha."""
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-m", message,
        ],
        check=True,
        capture_output=True,
    )
    res = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def write_daily_fixture(
    music_repo: Path,
    vault_repo: Path,
    *,
    day: str,
    tracks: list[dict[str, Any]],
    tx_status: str = "real_published",
    include_qa_run: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """Populate exact production-compatible dual-repo files for `day` and return refs."""
    year, month, _ = day.split("-")
    mfile = music_repo / "recommendations" / year / month / f"{day}.json"
    mfile.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "date": day, "provider": "apple_music", "tracks": tracks}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    mfile.write_bytes(manifest_bytes)

    state_file = music_repo / "state" / "tracks.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    existing_state = (
        json.loads(state_file.read_text(encoding="utf-8"))
        if state_file.is_file()
        else {"schema_version": 1, "recommended": {}}
    )
    recommended = dict(existing_state.get("recommended") or {})
    canonical_ids = [str(t["canonical_id"]) for t in tracks]
    for tr in tracks:
        recommended[tr["canonical_id"]] = {"date": day, "song_id": tr["apple_music"]["song_id"]}
    state_bytes = json.dumps({"schema_version": 1, "recommended": recommended}, ensure_ascii=False, indent=2).encode("utf-8")
    state_file.write_bytes(state_bytes)

    mhead = git_commit_all(music_repo, f"daily {day}") if commit else ""

    v_manifest = vault_repo / "Music" / "Daily" / "manifests" / f"{day}.json"
    v_manifest.parent.mkdir(parents=True, exist_ok=True)
    v_manifest.write_bytes(manifest_bytes)

    v_state = vault_repo / "Music" / "Daily" / "state" / "tracks.json"
    v_state.parent.mkdir(parents=True, exist_ok=True)
    v_state.write_bytes(state_bytes)

    links = "\n".join(f"- {t['apple_music']['url']}" for t in tracks)
    md_file = vault_repo / "Music" / "Daily" / f"{day}.md"
    md_file.parent.mkdir(parents=True, exist_ok=True)
    md_file.write_text(f"# {day}\n{links}\n<!-- AUTO:END -->\n手写随记 {day}\n", encoding="utf-8")

    bundle_sha = hashlib.sha256(f"bundle:{day}".encode("utf-8")).hexdigest()
    tx_rel = f".recommendation/transactions/music/{day}.json"
    tx_file = vault_repo / tx_rel
    tx_file.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "project": "music",
        "date": day,
        "operation_id": f"music:{day}",
        "exposed_resource_ids": canonical_ids,
    }
    if tx_status == "real_published":
        tx_doc: dict[str, Any] = {
            "schema_version": 1,
            "plan": plan,
            "payloads": {
                f"ZJFILOVE/music-daily:recommendations/{year}/{month}/{day}.json": manifest_bytes.decode("utf-8"),
                "ZJFILOVE/music-daily:state/tracks.json": state_bytes.decode("utf-8"),
            },
            "bundle_sha256": bundle_sha,
        }
    else:
        tx_doc = {
            "schema_version": 1,
            "status": tx_status,
            "plan": plan,
            "bundle_sha256": bundle_sha,
        }
    tx_file.write_text(json.dumps(tx_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if include_qa_run:
        qa_file = vault_repo / ".recommendation" / "runs" / f"{day}-music.json"
        qa_file.parent.mkdir(parents=True, exist_ok=True)
        qa_file.write_text(
            json.dumps({
                "project": "music",
                "date": day,
                "operation_id": f"music:{day}",
                "status": "passed_current_publication",
                "bundle": {"path": tx_rel, "bundle_sha256": bundle_sha},
            }),
            encoding="utf-8",
        )

    vhead = git_commit_all(vault_repo, f"tx {day}") if commit else ""
    return {"music_head": mhead, "vault_head": vhead, "bundle_sha256": bundle_sha}


def make_tracks(song_ids: list[str], positions: list[int] | None = None) -> list[dict[str, Any]]:
    """Build manifest track objects from Apple Music song IDs."""
    pos_list = positions or list(range(1, len(song_ids) + 1))
    return [
        {
            "position": pos,
            "canonical_id": f"Artist {idx}::Song {sid}",
            "title": f"Song {sid}",
            "artist": f"Artist {idx}",
            "album": f"Album {idx}",
            "analysis_summary": f"解析摘要 {sid}",
            "listening_cue": f"聆听提示 {sid}",
            "apple_music": {
                "song_id": sid,
                "storefront": "cn",
                "url": f"https://music.apple.com/cn/song/{sid}",
            },
        }
        for idx, (pos, sid) in enumerate(zip(pos_list, song_ids), start=1)
    ]


def build_plugin_context(
    tmp_path: Path,
    *,
    music_repo: Path | None = None,
    vault_repo: Path | None = None,
    api_base: str = "http://127.0.0.1:1",
    dev_token: str = "SECRET_DEV_TOKEN_XYZ",
    user_token: str = "SECRET_USER_TOKEN_ABC",
    cookie: str = "SECRET_COOKIE_123",
    qwen_loader: Any = None,
) -> tuple[MusicPlugin, PluginContext]:
    """Create and load MusicPlugin with isolated tmp_path config and SecretVault."""
    manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        "plugin-music",
        plugin_dir=PLUGIN_ROOT,
        data_dir=tmp_path / "plugin_data",
        config_dir=tmp_path / "plugin_config",
        config_schema=manifest["config_schema"],
        secret_keys=["apple_music_developer_token", "apple_music_user_token", "apple_music_cookie"],
    )
    if music_repo is not None:
        ctx.set_config("music_daily_dir", str(music_repo))
    if vault_repo is not None:
        ctx.set_config("obsidian_vault_dir", str(vault_repo))
    ctx.set_config("apple_music_api_base", api_base)
    if dev_token:
        ctx.set_secret("apple_music_developer_token", dev_token)
    if user_token:
        ctx.set_secret("apple_music_user_token", user_token)
    if cookie:
        ctx.set_secret("apple_music_cookie", cookie)

    plugin = MusicPlugin(qwen_loader=qwen_loader)
    run_async(plugin.on_load(ctx))
    return plugin, ctx
