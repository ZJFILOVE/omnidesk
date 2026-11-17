"""Local-first reading progress and vocabulary sync (`book.sync.v1`)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult

if __package__:
    from .lan_http import LanHttpClient, LanHttpError, validate_lan_url
    from .safety import MAX_SYNC_BYTES, MountValidationError, atomic_write_text_exact, read_text_bounded, redact_secrets
else:
    from lan_http import LanHttpClient, LanHttpError, validate_lan_url
    from safety import MAX_SYNC_BYTES, MountValidationError, atomic_write_text_exact, read_text_bounded, redact_secrets


def _read_sync_file(path: Path) -> dict[str, Any]:
    if not path.exists() or path.is_symlink():
        return {"version": 1, "progress": {}, "vocab": {}}
    try:
        parsed = json.loads(read_text_bounded(path, MAX_SYNC_BYTES))
    except (MountValidationError, json.JSONDecodeError):
        return {"version": 1, "progress": {}, "vocab": {}}
    if not isinstance(parsed, dict):
        return {"version": 1, "progress": {}, "vocab": {}}
    progress = parsed.get("progress") if isinstance(parsed.get("progress"), dict) else {}
    vocab = parsed.get("vocab") if isinstance(parsed.get("vocab"), dict) else {}
    return {"version": 1, "progress": dict(progress), "vocab": dict(vocab)}


def _lww_merge(target: dict[str, dict[str, Any]], entries: Iterable[Mapping[str, Any]], key_field: str) -> int:
    changed = 0
    for item in entries:
        key = str(item.get(key_field) or "").strip()
        if not key:
            continue
        inc_ts = float(item.get("updated_at") or time.time())
        existing = target.get(key)
        cur_ts = float(existing.get("updated_at") or 0.0) if isinstance(existing, Mapping) else -1.0
        if inc_ts >= cur_ts:
            normalized = dict(item)
            normalized[key_field] = key
            normalized["updated_at"] = inc_ts
            target[key] = normalized
            changed += 1
    return changed


def _extract_incoming_entries(req: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    prog: list[Mapping[str, Any]] = []
    if isinstance(req.get("progress"), list):
        prog.extend(p for p in req["progress"] if isinstance(p, Mapping))
    elif isinstance(req.get("progress"), Mapping):
        prog.append(req["progress"])
    elif req.get("book_id") and any(k in req for k in ("current_page", "progress_percent", "cfi", "chapter")):
        prog.append({
            "book_id": str(req["book_id"]), "title": str(req.get("title") or ""),
            "current_page": int(req.get("current_page") or 1), "total_pages": int(req.get("total_pages") or 1),
            "progress_percent": float(req.get("progress_percent") or 0.0), "cfi": str(req.get("cfi") or ""),
            "updated_at": float(req.get("updated_at") or time.time()),
        })

    vocab: list[Mapping[str, Any]] = []
    for k in ("vocab", "vocab_cards"):
        if isinstance(req.get(k), list):
            vocab.extend(v for v in req[k] if isinstance(v, Mapping))
    if isinstance(req.get("word"), str) and req["word"].strip():
        vocab.append({
            "word": req["word"].strip(), "meaning": str(req.get("meaning") or ""),
            "context_sentence": str(req.get("context_sentence") or ""),
            "source_book": str(req.get("source_book") or ""),
            "updated_at": float(req.get("updated_at") or time.time()),
        })
    return prog, vocab


def _persist_sync_state(data_dir: Path, mount_root: Path | None, state: Mapping[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text_exact(data_dir / "book-sync.json", serialized, mode=0o600)
    if mount_root is not None and mount_root.is_dir():
        nas_meta = mount_root / ".omnidesk"
        if not nas_meta.is_symlink():
            try:
                nas_meta.mkdir(parents=True, exist_ok=True, mode=0o755)
                atomic_write_text_exact(nas_meta / "book-sync.json", serialized, mode=0o600)
            except (MountValidationError, OSError):
                pass


async def run_book_sync(
    req: Mapping[str, Any],
    *,
    data_dir: Path,
    mount_root: Path | None,
    companion_url_raw: str,
    companion_token: str | None,
    http_client: LanHttpClient,
    timeout_sec: float,
) -> CapabilityResult:
    """Local-first book progress & vocabulary sync with optional L2 companion server."""
    action = str(req.get("action") or "sync").strip().lower()
    local_state = _read_sync_file(data_dir / "book-sync.json")
    if mount_root is not None:
        nas_state = _read_sync_file(mount_root / ".omnidesk" / "book-sync.json")
        _lww_merge(local_state["progress"], nas_state["progress"].values(), "book_id")
        _lww_merge(local_state["vocab"], nas_state["vocab"].values(), "word")

    is_read_only = action in {"get_local", "list_local", "load", "status", "pull"}
    inc_prog, inc_vocab = ([], []) if is_read_only else _extract_incoming_entries(req)
    changed = _lww_merge(local_state["progress"], inc_prog, "book_id")
    changed += _lww_merge(local_state["vocab"], inc_vocab, "word")
    local_saved = bool(inc_prog or inc_vocab or changed)
    if local_saved:
        _persist_sync_state(data_dir, mount_root, local_state)

    if action in {"get_local", "list_local", "load"}:
        bid = str(req.get("book_id") or "").strip()
        prog_out = {bid: local_state["progress"][bid]} if bid and bid in local_state["progress"] else ({} if bid else local_state["progress"])
        return CapabilityResult.ok({
            "local_saved": local_saved, "remote_synced": False,
            "remote_configured": bool(companion_url_raw.strip()),
            "progress": prog_out, "vocab": local_state["vocab"],
        })

    if action in {"save_local", "save_progress", "save_vocab"}:
        return CapabilityResult.ok({
            "local_saved": True, "remote_synced": False, "remote_configured": bool(companion_url_raw.strip()),
            "progress_count": len(local_state["progress"]), "vocab_count": len(local_state["vocab"]),
            "progress": local_state["progress"], "vocab": local_state["vocab"],
        })

    if action not in {"sync", "push", "pull", "status"}:
        return CapabilityResult.fail(f"不支持的 book.sync.v1 动作: {action!r}", error_code="invalid_argument")

    if not companion_url_raw.strip():
        return CapabilityResult.fail(
            "尚未配置伴随同步服务地址 (companion_sync_url)，仅完成本地存储",
            data={
                "local_saved": local_saved, "remote_synced": False, "remote_configured": False,
                "progress_count": len(local_state["progress"]), "vocab_count": len(local_state["vocab"]),
                "progress": local_state["progress"], "vocab": local_state["vocab"],
            },
            error_code="not_configured",
        )

    try:
        base_url = validate_lan_url(companion_url_raw, label="Companion Sync")
    except LanHttpError as exc:
        return CapabilityResult.fail(str(exc), data={"local_saved": local_saved, "remote_synced": False}, error_code=exc.error_code)

    headers = {"Authorization": f"Bearer {companion_token}"} if companion_token else {}
    secrets = (companion_token,) if companion_token else ()
    try:
        if action == "status":
            remote = await http_client.request_json(
                "GET", f"{base_url}/v1/sync/status", headers=headers, timeout_sec=timeout_sec, secrets=secrets, service_label="Companion Sync"
            )
            return CapabilityResult.ok({"remote_configured": True, "remote_online": True, "remote": remote, "local_saved": local_saved})

        if action == "pull":
            remote = await http_client.request_json(
                "GET", f"{base_url}/v1/sync/progress", headers=headers, timeout_sec=timeout_sec, secrets=secrets, service_label="Companion Sync"
            )
        else:
            remote = await http_client.request_json(
                "POST", f"{base_url}/v1/sync/progress", headers=headers,
                json_body={"progress": list(local_state["progress"].values()), "vocab": list(local_state["vocab"].values())},
                timeout_sec=timeout_sec, secrets=secrets, service_label="Companion Sync",
            )
    except LanHttpError as exc:
        return CapabilityResult.fail(
            redact_secrets(str(exc), secrets),
            data={
                "local_saved": local_saved, "remote_synced": False, "remote_configured": True,
                "http_status": exc.http_status, "progress": local_state["progress"], "vocab": local_state["vocab"],
            },
            error_code=exc.error_code,
        )

    merged_remote = 0
    if isinstance(remote, Mapping):
        if isinstance(remote.get("progress"), list):
            merged_remote += _lww_merge(local_state["progress"], [p for p in remote["progress"] if isinstance(p, Mapping)], "book_id")
        if isinstance(remote.get("vocab"), list):
            merged_remote += _lww_merge(local_state["vocab"], [v for v in remote["vocab"] if isinstance(v, Mapping)], "word")
        if merged_remote > 0:
            _persist_sync_state(data_dir, mount_root, local_state)

    return CapabilityResult.ok({
        "local_saved": local_saved, "remote_synced": True, "remote_configured": True,
        "merged_remote_count": merged_remote, "progress_count": len(local_state["progress"]),
        "vocab_count": len(local_state["vocab"]), "progress": local_state["progress"], "vocab": local_state["vocab"],
    })
