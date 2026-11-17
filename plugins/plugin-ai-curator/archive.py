"""Dual-repo Git mirror discovery, safe sync, and incremental archive rehydration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookPageBlock, MediaItem, PluginContext, TextArticle, TextFeedItem, VocabCard

if __package__:
    from .models import (
        DEFAULT_MUSIC_REMOTE,
        DEFAULT_OBSIDIAN_REMOTE,
        HISTORY_START_DATE,
        CuratorError,
        DateStats,
        EnglishDayBundle,
        EnglishNoteEntry,
        HydratedArchive,
        LedgerReadError,
        RepoPaths,
        RepoSyncStatus,
        atomic_write_json,
        git_blob_sha1,
        is_valid_iso_date,
        normalize_for_dedup,
        read_bytes_strict,
        read_json_strict,
        read_text_strict,
        record_provenance,
    )
    from .parsers_english import ENGLISH_SUBDIRS, parse_english_file
    from .parsers_streams import (
        extract_date_from_stem,
        parse_journal_daily_file,
        parse_jsonl_history_file,
        parse_music_manifest_file,
        parse_music_note_file,
        parse_music_state_file,
        parse_news_daily_file,
        parse_reddit_daily_file,
        parse_youtube_daily_file,
    )
else:
    from models import (  # type: ignore[no-redef]
        DEFAULT_MUSIC_REMOTE,
        DEFAULT_OBSIDIAN_REMOTE,
        HISTORY_START_DATE,
        CuratorError,
        DateStats,
        EnglishDayBundle,
        EnglishNoteEntry,
        HydratedArchive,
        LedgerReadError,
        RepoPaths,
        RepoSyncStatus,
        atomic_write_json,
        git_blob_sha1,
        is_valid_iso_date,
        normalize_for_dedup,
        read_bytes_strict,
        read_json_strict,
        read_text_strict,
        record_provenance,
    )
    from parsers_english import ENGLISH_SUBDIRS, parse_english_file  # type: ignore[no-redef]
    from parsers_streams import (  # type: ignore[no-redef]
        extract_date_from_stem,
        parse_journal_daily_file,
        parse_jsonl_history_file,
        parse_music_manifest_file,
        parse_music_note_file,
        parse_music_state_file,
        parse_news_daily_file,
        parse_reddit_daily_file,
        parse_youtube_daily_file,
    )


def resolve_curation_root(ctx: PluginContext, req: Mapping[str, Any] | None = None) -> Path:
    """Resolve curation storage root honoring request/config, PluginManager XDG paths, or env."""
    if req:
        for key in ("curation_root", "curation_dir"):
            raw = str(req.get(key) or "").strip()
            if raw:
                return Path(raw).expanduser()
    cfg_root = str(ctx.get_config("curation_root", "") or "").strip()
    if cfg_root:
        return Path(cfg_root).expanduser()
    # PluginManager sets data_dir = <XDG_DATA_HOME>/omnidesk/plugin-data/<plugin_id>.
    if ctx.data_dir.parent.name == "plugin-data" and ctx.data_dir.name == ctx.plugin_id:
        return ctx.data_dir.parent.parent / "curation"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        expanded = Path(xdg_data).expanduser()
        if expanded.is_absolute():
            return expanded / "omnidesk" / "curation"
    if ctx.data_dir != ctx.plugin_dir:
        return ctx.data_dir / "curation"
    return Path.home() / ".local" / "share" / "omnidesk" / "curation"


def _workspace_sibling_candidates(ctx: PluginContext, repo_name: str) -> Path | None:
    pdir = ctx.plugin_dir.resolve()
    if pdir.parent.name != "plugins":
        return None
    roots: list[Path] = []
    if len(pdir.parents) >= 3:
        roots.append(pdir.parents[2])  # sibling of workspace root (e.g. ~/Projects/obsidian-vault)
    if len(pdir.parents) >= 2:
        roots.append(pdir.parents[1])  # directly inside workspace root
    for root in roots:
        cand = root / repo_name
        if (cand / ".git").exists():
            return cand
    return None


def resolve_repo_paths(ctx: PluginContext, req: Mapping[str, Any] | None = None) -> RepoPaths:
    """Discover obsidian-vault and music-daily via config -> workspace sibling -> managed XDG."""
    curation_root = resolve_curation_root(ctx, req)

    def _pick(req_key: str, cfg_key: str, repo_name: str) -> Path:
        if req and (raw := str(req.get(req_key) or "").strip()):
            return Path(raw).expanduser()
        if cfg := str(ctx.get_config(cfg_key, "") or "").strip():
            return Path(cfg).expanduser()
        if sibling := _workspace_sibling_candidates(ctx, repo_name):
            return sibling
        return curation_root / "repos" / repo_name

    return RepoPaths(
        obsidian_vault_dir=_pick("obsidian_vault_dir", "obsidian_vault_dir", "obsidian-vault"),
        music_daily_dir=_pick("music_daily_dir", "music_daily_dir", "music-daily"),
        curation_root=curation_root,
    )


def _validate_git_remote_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned or cleaned.startswith("-") or cleaned.lower().startswith("ext::"):
        raise CuratorError("invalid_argument", f"Unsafe git remote URL: {url!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise CuratorError("invalid_argument", "Control characters forbidden in git remote URL")
    return cleaned


async def sync_repo(
    ctx: PluginContext,
    repo_name: str,
    repo_dir: Path,
    remote_url: str,
    *,
    pull_remote: bool,
    clone_if_missing: bool,
    branch: str = "main",
) -> RepoSyncStatus:
    """Safely clone if missing or fast-forward only when git working tree is clean."""
    st = RepoSyncStatus(name=repo_name, path=str(repo_dir))
    safe_url = _validate_git_remote_url(remote_url)

    if repo_dir.is_symlink():
        raise CuratorError("path_safety", f"Refusing symlinked repository path: {repo_dir}")

    if not repo_dir.exists() or (repo_dir.is_dir() and not (repo_dir / ".git").exists() and not any(repo_dir.iterdir())):
        if not clone_if_missing:
            st.status_detail = "missing_local_repo"
            return st
        repo_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        clone_res = await ctx.exec_subprocess(
            ["git", "clone", "--branch", branch, safe_url, str(repo_dir)],
            timeout_sec=120.0,
        )
        if not clone_res.ok:
            st.error = (clone_res.stderr or clone_res.stdout or "git clone failed").strip()
            st.status_detail = "git_clone_failed"
            return st
        st.cloned = True

    st.exists = repo_dir.exists()
    st.is_git_repo = (repo_dir / ".git").exists()
    if not st.is_git_repo:
        st.status_detail = "non_git_directory"
        return st

    head_res = await ctx.exec_subprocess(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_dir, timeout_sec=10.0)
    if head_res.ok:
        st.head_sha = head_res.stdout.strip()

    remote_res = await ctx.exec_subprocess(["git", "remote", "get-url", "origin"], cwd=repo_dir, timeout_sec=10.0)
    st.has_remote = remote_res.ok and bool(remote_res.stdout.strip())

    porcelain_res = await ctx.exec_subprocess(["git", "status", "--porcelain"], cwd=repo_dir, timeout_sec=15.0)
    if not porcelain_res.ok:
        st.error = (porcelain_res.stderr or "git status failed").strip()
        st.status_detail = "git_status_failed"
        return st
    if porcelain_res.stdout.strip():
        st.dirty = True
        st.dirty_worktree_preserved = True
        st.status_detail = "dirty_worktree_preserved"
        return st

    if not pull_remote or not st.has_remote:
        st.status_detail = "local_clean" if pull_remote else "pull_disabled"
        return st

    fetch_res = await ctx.exec_subprocess(["git", "fetch", "--", "origin", branch], cwd=repo_dir, timeout_sec=60.0)
    if not fetch_res.ok:
        st.error = (fetch_res.stderr or fetch_res.stdout or "git fetch failed").strip()
        st.status_detail = "git_fetch_failed"
        return st

    remote_head = await ctx.exec_subprocess(
        ["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=repo_dir, timeout_sec=10.0
    )
    if remote_head.ok:
        st.remote_head_sha = remote_head.stdout.strip()

    merge_res = await ctx.exec_subprocess(
        ["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo_dir, timeout_sec=30.0
    )
    if not merge_res.ok:
        st.error = (merge_res.stderr or merge_res.stdout or "git merge --ff-only failed").strip()
        st.status_detail = "git_ff_only_failed"
        return st

    after_res = await ctx.exec_subprocess(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_dir, timeout_sec=10.0)
    new_sha = after_res.stdout.strip() if after_res.ok else st.head_sha
    st.pulled = bool(st.head_sha and new_sha and new_sha != st.head_sha)
    st.head_sha = new_sha
    st.status_detail = "pulled" if st.pulled else "up_to_date"
    return st


def _scan_source_files(obsidian_dir: Path, music_dir: Path) -> list[dict[str, Any]]:
    """Collect candidate source files across music-daily and obsidian-vault."""
    entries: list[dict[str, Any]] = []

    def _add(repo_key: str, repo_root: Path, path: Path, kind: str, meta: dict[str, Any] | None = None) -> None:
        if path.is_symlink():
            try:
                path.resolve().relative_to(repo_root.resolve())
            except ValueError as exc:
                raise LedgerReadError(f"Symlink escapes repository: {path}") from exc
        try:
            st = path.stat()
        except OSError as exc:
            raise LedgerReadError(f"Unreadable file {path}: {exc}") from exc
        rel = path.relative_to(repo_root).as_posix()
        entries.append(
            {
                "key": f"{repo_key}:{rel}",
                "repo": repo_key,
                "rel_path": rel,
                "abs_path": path,
                "repo_root": repo_root,
                "kind": kind,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "meta": meta or {},
            }
        )

    if music_dir.exists():
        recs = music_dir / "recommendations"
        if recs.is_dir():
            for p in sorted(recs.rglob("*.json")):
                if p.is_file() and extract_date_from_stem(p):
                    _add("music_daily", music_dir, p, "music_manifest", {"priority": 1})
        tracks = music_dir / "state" / "tracks.json"
        if tracks.exists() or tracks.is_symlink():
            _add("music_daily", music_dir, tracks, "music_state")

    if obsidian_dir.exists():
        obs_music = obsidian_dir / "Music" / "Daily"
        if obs_music.is_dir():
            for p in sorted(obs_music.glob("*.md")):
                if p.is_file() and extract_date_from_stem(p):
                    _add("obsidian_vault", obsidian_dir, p, "music_note")
        obs_man = obs_music / "manifests"
        if obs_man.is_dir():
            for p in sorted(obs_man.glob("*.json")):
                if p.is_file() and extract_date_from_stem(p):
                    _add("obsidian_vault", obsidian_dir, p, "music_manifest", {"priority": 2})
        obs_tracks = obs_music / "state" / "tracks.json"
        if obs_tracks.exists() or obs_tracks.is_symlink():
            _add("obsidian_vault", obsidian_dir, obs_tracks, "music_state")

        for rel_dir, kind in (
            ("Resources/YouTube/Daily", "youtube"),
            ("News/Daily", "news"),
            ("Resources/Reddit/Daily", "reddit"),
            ("00-inbox/Journal", "journal"),
        ):
            d = obsidian_dir / rel_dir
            if d.is_dir():
                for p in sorted(d.glob("*.md")):
                    if p.is_file() and extract_date_from_stem(p):
                        _add("obsidian_vault", obsidian_dir, p, kind)

        j_idx = obsidian_dir / "00-inbox" / "Journal" / "Index.md"
        if j_idx.exists() or j_idx.is_symlink():
            _add("obsidian_vault", obsidian_dir, j_idx, "journal_index")

        for sub_rel, comp, label in ENGLISH_SUBDIRS:
            d = obsidian_dir / "50-courses" / "English" / sub_rel
            if d.is_dir():
                for p in sorted(d.glob("*.md")):
                    if p.is_file() and extract_date_from_stem(p):
                        _add(
                            "obsidian_vault",
                            obsidian_dir,
                            p,
                            "english",
                            {"sub_rel": sub_rel, "component": comp, "label": label},
                        )

        for stream_name in ("youtube", "reddit", "journal", "english"):
            hp = obsidian_dir / ".recommendation" / "history" / f"{stream_name}.jsonl"
            if hp.exists() or hp.is_symlink():
                _add("obsidian_vault", obsidian_dir, hp, "jsonl_history", {"stream": stream_name})

        pol = obsidian_dir / ".recommendation" / "policy.json"
        if pol.exists() or pol.is_symlink():
            _add("obsidian_vault", obsidian_dir, pol, "policy")

        tx_dir = obsidian_dir / ".recommendation" / "transactions"
        if tx_dir.is_dir():
            for p in sorted(tx_dir.rglob("*.json")):
                if p.is_file() or p.is_symlink():
                    _add("obsidian_vault", obsidian_dir, p, "transaction")

    entries.sort(key=lambda e: e["key"])
    return entries


def _parse_single_entry(entry: dict[str, Any]) -> dict[str, Any]:
    kind = entry["kind"]
    path: Path = entry["abs_path"]
    root: Path = entry["repo_root"]
    meta: dict[str, Any] = entry["meta"]

    if kind == "music_note":
        return {"kind": kind, "date": path.stem, "user_notes": parse_music_note_file(path, root)}
    if kind == "music_manifest":
        parsed = parse_music_manifest_file(path, root)
        return {
            "kind": kind,
            "date": parsed["date"],
            "priority": meta.get("priority", 2),
            "items": [m.model_dump() for m in parsed["items"]],
            "anomaly": parsed["anomaly"],
        }
    if kind == "music_state":
        return {"kind": kind, **parse_music_state_file(path, root)}
    if kind == "youtube":
        parsed = parse_youtube_daily_file(path, root)
        return {"kind": kind, "date": parsed["date"], "items": [m.model_dump() for m in parsed["items"]]}
    if kind == "news":
        parsed = parse_news_daily_file(path, root)
        return {
            "kind": kind,
            "date": parsed["date"],
            "feed_items": [f.model_dump() for f in parsed["feed_items"]],
            "articles": {k: v.model_dump() for k, v in parsed["articles"].items()},
            "story_count": parsed["story_count"],
        }
    if kind == "reddit":
        parsed = parse_reddit_daily_file(path, root)
        return {
            "kind": kind,
            "date": parsed["date"],
            "feed_items": [f.model_dump() for f in parsed["feed_items"]],
            "articles": {k: v.model_dump() for k, v in parsed["articles"].items()},
        }
    if kind == "journal":
        parsed = parse_journal_daily_file(path, root)
        return {
            "kind": kind,
            "date": parsed["date"],
            "feed_items": [f.model_dump() for f in parsed["feed_items"]],
            "articles": {k: v.model_dump() for k, v in parsed["articles"].items()},
        }
    if kind == "journal_index":
        _ = read_text_strict(path, repo_root=root)
        return {"kind": kind}
    if kind == "english":
        parsed = parse_english_file(
            path,
            root,
            sub_rel=meta["sub_rel"],
            component=meta["component"],
            label=meta["label"],
        )
        note: EnglishNoteEntry = parsed["note"]
        return {
            "kind": kind,
            "date": parsed["date"],
            "component": parsed["component"],
            "note": {
                "component": note.component,
                "date": note.date,
                "title": note.title,
                "rel_path": note.rel_path,
                "blocks": [b.model_dump() for b in note.blocks],
                "vocab_cards": [v.model_dump() for v in note.vocab_cards],
            },
            "feed_item": parsed["feed_item"].model_dump(),
            "article": parsed["article"].model_dump(),
            "vocab_cards": [v.model_dump() for v in parsed["vocab_cards"]],
        }
    if kind == "jsonl_history":
        return {"kind": kind, "stream": meta["stream"], "records": parse_jsonl_history_file(path, root, meta["stream"])}
    if kind == "policy":
        obj = read_json_strict(path, repo_root=root, error_code="unreadable_ledger")
        if not isinstance(obj, dict):
            raise LedgerReadError(f"Invalid policy.json object: {path}", error_code="unreadable_ledger")
        return {"kind": kind}
    if kind == "transaction":
        obj = read_json_strict(path, repo_root=root, error_code="unreadable_ledger")
        if not isinstance(obj, dict):
            raise LedgerReadError(f"Invalid transaction JSON object: {path}", error_code="unreadable_ledger")
        return {"kind": kind}
    raise LedgerReadError(f"Unknown source kind: {kind}")


def _assemble_archive(per_file_cache: Mapping[str, dict[str, Any]]) -> HydratedArchive:
    """Assemble full HydratedArchive deterministically from parsed per-file cache."""
    out = HydratedArchive()
    notes_by_date: dict[str, str] = {}
    music_by_date: dict[str, tuple[int, dict[str, Any]]] = {}

    for rec in per_file_cache.values():
        if rec.get("kind") == "music_note" and rec.get("user_notes"):
            notes_by_date[rec["date"]] = rec["user_notes"]

    for rec in per_file_cache.values():
        kind = rec.get("kind")
        if kind == "music_manifest":
            d = rec["date"]
            prio = int(rec.get("priority", 2))
            prev = music_by_date.get(d)
            if prev is None or prio < prev[0]:
                music_by_date[d] = (prio, rec)
        elif kind == "music_state":
            for cid, d in (rec.get("canonical_dates") or {}).items():
                record_provenance(out.canonical_id_to_date, cid, d)
        elif kind == "youtube":
            for raw_item in rec.get("items", []):
                out.media_items.append(MediaItem.model_validate(raw_item))
                out.youtube_videos_hydrated += 1
                out.date_stats.setdefault(rec["date"], DateStats()).youtube += 1
        elif kind == "news":
            if rec.get("feed_items"):
                out.news_days_hydrated += 1
            for raw_fi in rec.get("feed_items", []):
                out.text_items.append(TextFeedItem.model_validate(raw_fi))
            for aid, raw_art in rec.get("articles", {}).items():
                out.articles[aid] = TextArticle.model_validate(raw_art)
            cnt = int(rec.get("story_count", 0))
            out.news_stories_hydrated += cnt
            if cnt > 0:
                out.date_stats.setdefault(rec["date"], DateStats()).news += cnt
        elif kind == "reddit":
            fis = rec.get("feed_items", [])
            if fis:
                out.reddit_days_hydrated += 1
            for raw_fi in fis:
                out.text_items.append(TextFeedItem.model_validate(raw_fi))
            for aid, raw_art in rec.get("articles", {}).items():
                out.articles[aid] = TextArticle.model_validate(raw_art)
            out.reddit_posts_hydrated += len(fis)
            if fis:
                out.date_stats.setdefault(rec["date"], DateStats()).reddit += len(fis)
        elif kind == "journal":
            fis = rec.get("feed_items", [])
            if fis:
                out.journal_days_hydrated += 1
            for raw_fi in fis:
                out.text_items.append(TextFeedItem.model_validate(raw_fi))
            for aid, raw_art in rec.get("articles", {}).items():
                out.articles[aid] = TextArticle.model_validate(raw_art)
            out.journal_papers_hydrated += len(fis)
            if fis:
                out.date_stats.setdefault(rec["date"], DateStats()).journal += len(fis)
        elif kind == "jsonl_history":
            for r in rec.get("records", []):
                d = r.get("date", "")
                if not is_valid_iso_date(d):
                    continue
                if r.get("video_id"):
                    record_provenance(out.video_id_to_date, r["video_id"], d)
                if r.get("post_id"):
                    record_provenance(out.post_id_to_date, r["post_id"], d)
                if r.get("doi"):
                    record_provenance(out.doi_to_date, r["doi"], d)
                if r.get("url"):
                    record_provenance(out.url_to_date, r["url"].rstrip("/"), d)
                if r.get("norm_title"):
                    record_provenance(out.norm_title_to_date, r["norm_title"], d)

    for d in sorted(music_by_date.keys(), reverse=True):
        _, rec = music_by_date[d]
        notes = notes_by_date.get(d, "")
        for raw_item in rec.get("items", []):
            item = MediaItem.model_validate(raw_item)
            if notes and not item.extra.get("user_notes"):
                item.extra["user_notes"] = notes
            out.media_items.append(item)
            out.music_tracks_hydrated += 1
            out.date_stats.setdefault(d, DateStats()).music += 1
        if rec.get("anomaly"):
            out.history_anomalies.append(rec["anomaly"])

    # Assemble English notes & deduplicate VocabCards by lowercase word.
    seen_vocab: set[str] = set()
    english_recs = [r for r in per_file_cache.values() if r.get("kind") == "english"]
    english_recs.sort(key=lambda r: (r["date"], r["component"]), reverse=True)
    for rec in english_recs:
        d = rec["date"]
        nraw = rec["note"]
        note = EnglishNoteEntry(
            component=nraw["component"],
            date=nraw["date"],
            title=nraw["title"],
            rel_path=nraw["rel_path"],
            blocks=[BookPageBlock.model_validate(b) for b in nraw.get("blocks", [])],
            vocab_cards=[VocabCard.model_validate(v) for v in nraw.get("vocab_cards", [])],
        )
        bundle = out.english_days.setdefault(d, EnglishDayBundle(date=d))
        bundle.notes.append(note)
        out.text_items.append(TextFeedItem.model_validate(rec["feed_item"]))
        art = TextArticle.model_validate(rec["article"])
        out.articles[art.id] = art
        out.english_notes_hydrated += 1
        out.date_stats.setdefault(d, DateStats()).english += 1
        for raw_vc in rec.get("vocab_cards", []):
            vc = VocabCard.model_validate(raw_vc)
            key = vc.word.strip().lower()
            if key and key not in seen_vocab:
                seen_vocab.add(key)
                out.vocab_cards.append(vc)
                out.vocab_cards_hydrated += 1

    out.media_items.sort(
        key=lambda m: (
            str(m.business_date or m.extra.get("business_date") or ""),
            -int(m.extra.get("priority_rank", 999)),
        ),
        reverse=True,
    )
    out.text_items.sort(key=lambda t: (t.published_at, t.id), reverse=True)
    out.history_anomalies.sort(key=lambda a: a.get("date", ""), reverse=True)
    out.rebuild_indices()
    return out


class ArchiveRehydrator:
    """Incremental archive rehydrator backed by rehydrate_manifest.json and hydrated_index.json."""

    @staticmethod
    def load_cached_archive(curation_root: Path) -> HydratedArchive | None:
        """Load cached archive from hydrated_index.json without touching upstream repos."""
        idx_path = curation_root / "hydrated_index.json"
        if not idx_path.exists():
            return None
        raw = read_json_strict(idx_path, error_code="corrupted_cache")
        if not isinstance(raw, dict) or raw.get("schema_version") != 4:
            return None
        per_file = raw.get("per_file_cache")
        if not isinstance(per_file, dict):
            return None
        return _assemble_archive(per_file)

    @staticmethod
    def rehydrate(
        paths: RepoPaths,
        *,
        force_full: bool = False,
    ) -> tuple[HydratedArchive, dict[str, Any]]:
        """Incrementally rehydrate upstream repositories; fail closed on unreadable/corrupt input."""
        current_entries = _scan_source_files(paths.obsidian_vault_dir, paths.music_daily_dir)
        manifest_path = paths.curation_root / "rehydrate_manifest.json"
        index_path = paths.curation_root / "hydrated_index.json"

        prev_manifest_files: dict[str, dict[str, Any]] = {}
        prev_cache: dict[str, dict[str, Any]] = {}
        can_reuse = False

        if not force_full and manifest_path.is_file() and index_path.is_file():
            try:
                m_raw = json.loads(read_text_strict(manifest_path))
                i_raw = json.loads(read_text_strict(index_path))
                if (
                    isinstance(m_raw, dict)
                    and m_raw.get("schema_version") == 3
                    and m_raw.get("obsidian_vault_dir") == str(paths.obsidian_vault_dir)
                    and m_raw.get("music_daily_dir") == str(paths.music_daily_dir)
                    and isinstance(m_raw.get("files"), list)
                    and isinstance(i_raw, dict)
                    and i_raw.get("schema_version") == 4
                    and isinstance(i_raw.get("per_file_cache"), dict)
                ):
                    prev_manifest_files = {
                        f["key"]: f for f in m_raw["files"] if isinstance(f, dict) and isinstance(f.get("key"), str)
                    }
                    prev_cache = i_raw["per_file_cache"]
                    can_reuse = True
            except (LedgerReadError, json.JSONDecodeError, KeyError, TypeError):
                can_reuse = False

        new_cache: dict[str, dict[str, Any]] = {}
        new_manifest_files: list[dict[str, Any]] = []
        reparsed_files = 0
        reused_files = 0

        for entry in current_entries:
            key = entry["key"]
            prev_meta = prev_manifest_files.get(key) if can_reuse else None
            if (
                prev_meta is not None
                and key in prev_cache
                and prev_meta.get("size") == entry["size"]
                and prev_meta.get("mtime_ns") == entry["mtime_ns"]
            ):
                new_cache[key] = prev_cache[key]
                new_manifest_files.append(prev_meta)
                reused_files += 1
                continue

            raw_bytes = read_bytes_strict(entry["abs_path"], repo_root=entry["repo_root"])
            blob_sha = git_blob_sha1(raw_bytes)
            if prev_meta is not None and key in prev_cache and prev_meta.get("blob_sha") == blob_sha:
                new_cache[key] = prev_cache[key]
                reused_files += 1
            else:
                new_cache[key] = _parse_single_entry(entry)
                reparsed_files += 1

            new_manifest_files.append(
                {
                    "key": key,
                    "repo": entry["repo"],
                    "rel_path": entry["rel_path"],
                    "size": entry["size"],
                    "mtime_ns": entry["mtime_ns"],
                    "blob_sha": blob_sha,
                }
            )

        exact_cache_hit = (
            can_reuse
            and reparsed_files == 0
            and len(new_manifest_files) == len(prev_manifest_files)
            and all(m == prev_manifest_files.get(m["key"]) for m in new_manifest_files)
        )
        archive = _assemble_archive(new_cache)

        if not exact_cache_hit:
            atomic_write_json(index_path, {"schema_version": 4, "per_file_cache": new_cache})
            atomic_write_json(
                manifest_path,
                {
                    "schema_version": 3,
                    "obsidian_vault_dir": str(paths.obsidian_vault_dir),
                    "music_daily_dir": str(paths.music_daily_dir),
                    "files": new_manifest_files,
                },
            )

        stats = {
            "cache_hit": exact_cache_hit,
            "reparsed_files": reparsed_files,
            "reused_files": reused_files,
            "manifest_file_count": len(new_manifest_files),
        }
        return archive, stats
