"""Dual-repo committed PUBLISHED transaction, QA receipt, and anomaly verification."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext

if __package__:
    from .state_store import GateError, is_valid_iso_date
else:
    from state_store import GateError, is_valid_iso_date

_SONG_ID_RE = re.compile(r"^[0-9]{1,24}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_VALID_QA_STATUSES = frozenset({"passed_current_publication", "PUBLISHED", "passed"})


@dataclass(frozen=True, slots=True)
class AuthorizedDay:
    """Verified daily manifest and publication proof for one business date."""

    business_date: str
    operation_id: str
    manifest_rel_path: str
    song_ids: list[str]
    canonical_ids: list[str]
    tracks: list[dict[str, Any]]
    music_head: str
    vault_head: str
    bundle_sha256: str = ""
    anomaly: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DailyVerificationReport:
    """Result of verifying dual-repo committed manifests and PUBLISHED bundles."""

    days: list[AuthorizedDay]
    target_song_ids: list[str]
    history_anomalies: list[dict[str, Any]] = field(default_factory=list)


def git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw).hexdigest()


def _is_canonical_song_url(url: str, song_id: str) -> bool:
    """Accept both compact and slugged Apple Music song URLs for one catalog id."""
    if not song_id:
        return False
    return bool(
        re.fullmatch(
            rf"https://music\.apple\.com/cn/song/(?:[^/\s]+/)?{re.escape(song_id)}",
            url,
        )
    )


async def _run_git(ctx: PluginContext, repo: Path, *args: str) -> bytes:
    res = await ctx.exec_subprocess(["git", "-C", str(repo), *args], timeout_sec=10.0)
    if not res.ok:
        detail = (res.stderr or res.stdout).strip()[:200]
        raise GateError("needs_verified_bundle", f"Git 校验失败 ({repo.name}): {detail or 'command failed'}")
    return res.stdout_bytes


async def verify_clean_git_repo(ctx: PluginContext, repo: Path | None, label: str) -> str:
    if repo is None or not repo.is_dir() or not (repo / ".git").exists():
        raise GateError("needs_verified_bundle", f"缺少已提交的本地 Git 仓库: {label}")
    head = (await _run_git(ctx, repo, "rev-parse", "HEAD")).decode("utf-8", errors="replace").strip()
    if not _HEX40_RE.match(head):
        raise GateError("needs_verified_bundle", f"无效的 Git HEAD: {label}")
    shallow = (await _run_git(ctx, repo, "rev-parse", "--is-shallow-repository")).decode("utf-8", errors="replace").strip()
    if shallow != "false":
        raise GateError("needs_verified_bundle", f"浅克隆仓库无法授权每日写回: {label}")
    porcelain = (await _run_git(ctx, repo, "status", "--porcelain", "--untracked-files=all")).decode("utf-8", errors="replace").strip()
    if porcelain:
        raise GateError("needs_verified_bundle", f"仓库存在未提交改动，拒绝授权写回: {label}")
    return head


async def read_committed_bytes(ctx: PluginContext, repo: Path, head: str, rel_path: str) -> bytes:
    disk_path = repo / rel_path
    if disk_path.is_symlink() or not disk_path.is_file():
        raise GateError("needs_verified_bundle", f"缺少已提交产物: {rel_path}")
    disk_bytes = disk_path.read_bytes()
    if await _run_git(ctx, repo, "show", f"{head}:{rel_path}") != disk_bytes:
        raise GateError("needs_verified_bundle", f"工作区文件与已提交快照不一致: {rel_path}")
    return disk_bytes


def parse_music_manifest(
    raw: bytes, expected_date: str
) -> tuple[list[str], list[str], list[dict[str, Any]], dict[str, Any] | None]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("needs_verified_bundle", f"每日清单 JSON 无效 ({expected_date})") from exc
    if not isinstance(doc, Mapping) or doc.get("schema_version") != 1 or doc.get("provider") != "apple_music":
        raise GateError("needs_verified_bundle", f"每日清单格式无效 ({expected_date})")
    if doc.get("date") != expected_date or not isinstance(doc.get("tracks"), list):
        raise GateError("needs_verified_bundle", f"每日清单日期或 tracks 无效 ({expected_date})")

    tracks_raw = doc["tracks"]
    is_2026_11_06 = expected_date == "2026-11-06" and len(tracks_raw) == 4
    if len(tracks_raw) != 5 and not is_2026_11_06:
        raise GateError("needs_verified_bundle", f"每日清单曲目数必须为 5 首 ({expected_date}: 实际 {len(tracks_raw)})")

    song_ids: list[str] = []
    canonical_ids: list[str] = []
    tracks: list[dict[str, Any]] = []
    seen_songs: set[str] = set()
    seen_canonicals: set[str] = set()
    last_pos = 0

    for idx, item in enumerate(tracks_raw):
        if not isinstance(item, Mapping):
            raise GateError("needs_verified_bundle", f"曲目项格式错误 ({expected_date})")
        pos = item.get("position")
        if type(pos) is not int or (not is_2026_11_06 and pos != idx + 1) or (is_2026_11_06 and not (last_pos < pos <= 5)):
            raise GateError("needs_verified_bundle", f"曲目顺序异常 ({expected_date})")
        last_pos = pos
        am = item.get("apple_music")
        if not isinstance(am, Mapping):
            raise GateError("needs_verified_bundle", f"缺少 apple_music 字段 ({expected_date})")
        sid = str(am.get("song_id") or "")
        sf = str(am.get("storefront") or "").lower()
        url = str(am.get("url") or "")
        title = str(item.get("title") or "").strip()
        artist = str(item.get("artist") or "").strip()
        canonical = str(item.get("canonical_id") or "").strip()
        if (
            not _SONG_ID_RE.match(sid)
            or sf != "cn"
            or not _is_canonical_song_url(url, sid)
            or not title
            or not artist
            or canonical != f"{artist}::{title}"
            or not str(item.get("analysis_summary") or "").strip()
            or not str(item.get("listening_cue") or "").strip()
            or sid in seen_songs
            or canonical in seen_canonicals
        ):
            raise GateError("needs_verified_bundle", f"曲目元数据或国区标识校验失败 ({expected_date})")
        seen_songs.add(sid)
        seen_canonicals.add(canonical)
        song_ids.append(sid)
        canonical_ids.append(canonical)
        tracks.append(dict(item))

    anomaly = (
        {
            "date": "2026-11-06",
            "expected_count": 5,
            "actual_count": 4,
            "song_ids": list(song_ids),
            "reason": "known_historical_anomaly_4_tracks_no_synthetic_5th",
        }
        if is_2026_11_06
        else None
    )
    return song_ids, canonical_ids, tracks, anomaly


def _validate_publication_evidence(
    evidence: Any, business_date: str, operation_id: str, tx_rel: str, bundle_sha: str
) -> None:
    if not isinstance(evidence, Mapping):
        raise GateError("needs_verified_bundle", "publication_evidence 必须为对象")
    if evidence.get("status") != "PUBLISHED" or evidence.get("publication_verified") is not True:
        raise GateError("needs_verified_bundle", "publication_evidence 未证实 PUBLISHED 状态")
    if evidence.get("project") != "music" or evidence.get("business_date") != business_date or evidence.get("operation_id") != operation_id:
        raise GateError("needs_verified_bundle", "publication_evidence 与 operation_id/business_date 不匹配")
    receipt = evidence.get("receipt")
    if isinstance(receipt, str):
        if not receipt.strip():
            raise GateError("needs_verified_bundle", "publication_evidence 缺少 receipt")
    elif isinstance(receipt, Mapping):
        rpath, rsha = str(receipt.get("path") or ""), str(receipt.get("bundle_sha256") or "")
        if (rpath and rpath != tx_rel) or (rsha and bundle_sha and rsha != bundle_sha):
            raise GateError("needs_verified_bundle", "publication_evidence receipt 与事务不匹配")
    else:
        raise GateError("needs_verified_bundle", "publication_evidence receipt 格式无效")


async def _verify_single_day_bundle(
    ctx: PluginContext,
    music_repo: Path,
    vault_repo: Path,
    music_head: str,
    vault_head: str,
    business_date: str,
    music_state_raw: bytes,
    recommended_map: Mapping[str, Any],
    *,
    publication_evidence: Any = None,
) -> AuthorizedDay:
    op_id = f"music:{business_date}"
    manifest_rel = f"recommendations/{business_date[:4]}/{business_date[5:7]}/{business_date}.json"
    manifest_raw = await read_committed_bytes(ctx, music_repo, music_head, manifest_rel)
    song_ids, canonical_ids, tracks, anomaly = parse_music_manifest(manifest_raw, business_date)
    for cid in canonical_ids:
        if cid not in recommended_map:
            raise GateError("needs_verified_bundle", f"state/tracks.json 缺少永久曲目记录: {cid}")

    vault_manifest_raw = await read_committed_bytes(ctx, vault_repo, vault_head, f"Music/Daily/manifests/{business_date}.json")
    vault_state_raw = await read_committed_bytes(ctx, vault_repo, vault_head, "Music/Daily/state/tracks.json")
    note_raw = await read_committed_bytes(ctx, vault_repo, vault_head, f"Music/Daily/{business_date}.md")
    if vault_manifest_raw != manifest_raw or vault_state_raw != music_state_raw:
        raise GateError("needs_verified_bundle", f"双仓镜像不一致 ({business_date})")
    note_text = note_raw.decode("utf-8", errors="replace")
    if any(f"https://music.apple.com/cn/song/{sid}" not in note_text for sid in song_ids):
        raise GateError("needs_verified_bundle", f"Obsidian 笔记缺少歌曲链接 ({business_date})")

    tx_rel = f".recommendation/transactions/music/{business_date}.json"
    qa_rel = f".recommendation/runs/{business_date}-music.json"
    tx_raw = await read_committed_bytes(ctx, vault_repo, vault_head, tx_rel)
    qa_raw = await read_committed_bytes(ctx, vault_repo, vault_head, qa_rel)
    try:
        tx = json.loads(tx_raw.decode("utf-8"))
        qa = json.loads(qa_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("needs_verified_bundle", f"事务或 QA 收据 JSON 解析失败 ({business_date})") from exc
    if not isinstance(tx, Mapping) or not isinstance(qa, Mapping):
        raise GateError("needs_verified_bundle", f"事务或 QA 收据格式无效 ({business_date})")

    if (tx_status := tx.get("status")) is not None and tx_status != "PUBLISHED":
        raise GateError("needs_verified_bundle", f"事务状态不是 PUBLISHED ({business_date}: {tx_status})")

    plan = tx.get("plan") if isinstance(tx.get("plan"), Mapping) else tx
    if plan.get("project") != "music" or (plan.get("date") or plan.get("business_date")) != business_date or plan.get("operation_id") != op_id:
        raise GateError("needs_verified_bundle", f"事务 operation_id/business_date 不匹配 ({business_date})")
    if isinstance(plan.get("exposed_resource_ids"), list) and plan["exposed_resource_ids"] != canonical_ids:
        raise GateError("needs_verified_bundle", f"事务 exposed_resource_ids 与清单不一致 ({business_date})")

    if (
        qa.get("project") != "music"
        or (qa.get("date") or qa.get("business_date")) != business_date
        or qa.get("operation_id") != op_id
        or qa.get("status") not in _VALID_QA_STATUSES
        or qa.get("not_original_publication_success_receipt") is True
    ):
        raise GateError("needs_verified_bundle", f"缺少或未通过对应 QA 收据 ({business_date})")

    tx_sha = str(tx.get("bundle_sha256") or "")
    qa_bundle = qa.get("bundle") if isinstance(qa.get("bundle"), Mapping) else {}
    if qa_bundle:
        if qa_bundle.get("path") not in (None, tx_rel):
            raise GateError("needs_verified_bundle", f"QA 收据绑定的事务路径不匹配 ({business_date})")
        if tx_sha and qa_bundle.get("bundle_sha256") and qa_bundle.get("bundle_sha256") != tx_sha:
            raise GateError("needs_verified_bundle", f"QA 收据 bundle_sha256 与事务不匹配 ({business_date})")

    expected_blobs = {
        f"ZJFILOVE/music-daily:{manifest_rel}": manifest_raw,
        "ZJFILOVE/music-daily:state/tracks.json": music_state_raw,
        f"ZJFILOVE/obsidian-vault:Music/Daily/{business_date}.md": note_raw,
        f"ZJFILOVE/obsidian-vault:Music/Daily/manifests/{business_date}.json": vault_manifest_raw,
        "ZJFILOVE/obsidian-vault:Music/Daily/state/tracks.json": vault_state_raw,
    }
    if isinstance(tx.get("payloads"), Mapping):
        for key, raw_bytes in expected_blobs.items():
            if key in tx["payloads"] and tx["payloads"][key] != raw_bytes.decode("utf-8", errors="replace"):
                raise GateError("needs_verified_bundle", f"事务载荷与已提交产物不一致: {key}")
    if isinstance(qa.get("readbacks"), Mapping):
        for key, rb in qa["readbacks"].items():
            if key in expected_blobs and isinstance(rb, Mapping):
                rb_sha = str(rb.get("sha") or "")
                if _HEX40_RE.match(rb_sha) and rb_sha != git_blob_sha(expected_blobs[key]):
                    raise GateError("needs_verified_bundle", f"QA 读回哈希不一致: {key}")
    if publication_evidence is not None:
        _validate_publication_evidence(publication_evidence, business_date, op_id, tx_rel, tx_sha)

    return AuthorizedDay(
        business_date=business_date, operation_id=op_id, manifest_rel_path=manifest_rel,
        song_ids=song_ids, canonical_ids=canonical_ids, tracks=tracks,
        music_head=music_head, vault_head=vault_head, bundle_sha256=tx_sha, anomaly=anomaly,
    )


async def scan_repo_anomalies(ctx: PluginContext, music_repo: Path, music_head: str) -> tuple[list[str], list[dict[str, Any]]]:
    ls_raw = await _run_git(ctx, music_repo, "ls-tree", "-r", "--name-only", music_head, "recommendations")
    dates: list[str] = []
    anomalies: list[dict[str, Any]] = []
    for rel in sorted(line.strip() for line in ls_raw.decode("utf-8", errors="replace").splitlines() if line.strip()):
        if not re.fullmatch(r"recommendations/\d{4}/\d{2}/\d{4}-\d{2}-\d{2}\.json", rel):
            continue
        day = Path(rel).stem
        if not is_valid_iso_date(day):
            continue
        raw = await read_committed_bytes(ctx, music_repo, music_head, rel)
        try:
            _, _, _, anomaly = parse_music_manifest(raw, day)
        except GateError as exc:
            dates.append(day)
            anomalies.append({
                "date": day,
                "reason": "manifest_invalid",
                "error_code": exc.error_code,
                "error": exc.message,
            })
            continue
        dates.append(day)
        if anomaly is not None:
            anomalies.append(anomaly)
    return dates, anomalies


async def verify_daily_authorization(
    ctx: PluginContext,
    *,
    music_repo: Path | None,
    vault_repo: Path | None,
    business_date: str = "",
    operation_id: str = "",
    sync_all_rehydrated: bool = False,
    req_track_ids: Sequence[str] | None = None,
    publication_evidence: Any = None,
) -> DailyVerificationReport:
    """Verify committed dual-repo PUBLISHED transaction and QA receipt before daily sync."""
    music_head = await verify_clean_git_repo(ctx, music_repo, "music-daily")
    vault_head = await verify_clean_git_repo(ctx, vault_repo, "obsidian-vault")
    assert music_repo is not None and vault_repo is not None

    state_raw = await read_committed_bytes(ctx, music_repo, music_head, "state/tracks.json")
    try:
        state_json = json.loads(state_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("needs_verified_bundle", "state/tracks.json 解析失败") from exc
    recommended = state_json.get("recommended") if isinstance(state_json, Mapping) else None
    if not isinstance(recommended, Mapping):
        raise GateError("needs_verified_bundle", "state/tracks.json 缺少 recommended 账本")

    dates, anomalies = await scan_repo_anomalies(ctx, music_repo, music_head)
    if not dates:
        raise GateError("needs_verified_bundle", "music-daily 中没有已提交的每日清单")

    if not sync_all_rehydrated:
        if not is_valid_iso_date(business_date):
            raise GateError("invalid_argument", "单日同步必须提供合法的 business_date (YYYY-MM-DD)")
        expected_op = f"music:{business_date}"
        if operation_id and operation_id != expected_op:
            raise GateError("needs_verified_bundle", f"operation_id 必须匹配 {expected_op}")
        try:
            day_obj = await _verify_single_day_bundle(
                ctx, music_repo, vault_repo, music_head, vault_head, business_date, state_raw, recommended,
                publication_evidence=publication_evidence,
            )
        except GateError as exc:
            if anomalies:
                exc.extra.setdefault("history_anomalies", anomalies)
            raise
        if req_track_ids is not None and [str(x).strip() for x in req_track_ids] != day_obj.song_ids:
            raise GateError("needs_verified_bundle", f"请求 track_ids 与 {business_date} 已提交清单不一致")
        return DailyVerificationReport(days=[day_obj], target_song_ids=list(day_obj.song_ids), history_anomalies=anomalies)

    verified_days: list[AuthorizedDay] = []
    for day in reversed(dates):
        try:
            verified_days.append(await _verify_single_day_bundle(
                ctx, music_repo, vault_repo, music_head, vault_head, day, state_raw, recommended
            ))
        except GateError:
            continue
    if not verified_days:
        raise GateError("needs_verified_bundle", "双仓中未找到任何已提交 PUBLISHED 事务与 QA 收据", {"history_anomalies": anomalies})
    targets = list(dict.fromkeys(sid for d in verified_days for sid in d.song_ids))
    return DailyVerificationReport(days=verified_days, target_song_ids=targets, history_anomalies=anomalies)
