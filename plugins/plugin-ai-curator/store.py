"""Persistent curation store, ledger/receipt validation, provenance lookup, and SM-2 sync."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookItem, BookSpread, MediaItem, PluginContext, TextArticle, TextFeedItem, VocabCard

if __package__:
    from .models import (
        ALLOWED_DAILY_PROJECTS,
        MAX_PROVENANCE_ITEMS,
        CuratorError,
        HydratedArchive,
        InteractiveSession,
        LedgerReadError,
        RepoPaths,
        atomic_write_json,
        atomic_write_text,
        extract_apple_music_song_id,
        extract_reddit_post_id,
        extract_youtube_video_id,
        is_valid_iso_date,
        normalize_doi,
        normalize_for_dedup,
        read_json_strict,
        read_text_strict,
        safe_child_path,
        safe_identifier,
    )
    from .parsers_common import parse_frontmatter
    from .parsers_streams import parse_jsonl_history_file, parse_music_state_file
else:
    from models import (  # type: ignore[no-redef]
        ALLOWED_DAILY_PROJECTS,
        MAX_PROVENANCE_ITEMS,
        CuratorError,
        HydratedArchive,
        InteractiveSession,
        LedgerReadError,
        RepoPaths,
        atomic_write_json,
        atomic_write_text,
        extract_apple_music_song_id,
        extract_reddit_post_id,
        extract_youtube_video_id,
        is_valid_iso_date,
        normalize_doi,
        normalize_for_dedup,
        read_json_strict,
        read_text_strict,
        safe_child_path,
        safe_identifier,
    )
    from parsers_common import parse_frontmatter  # type: ignore[no-redef]
    from parsers_streams import parse_jsonl_history_file, parse_music_state_file  # type: ignore[no-redef]


def media_identity_key(item: MediaItem) -> str:
    """Stable identity key used for within-session dedup and media item matching."""
    song = str(item.extra.get("apple_music_song_id") or "").strip()
    if song:
        return f"am:{song}"
    vid = str(item.extra.get("video_id") or "").strip()
    if vid:
        return f"video:{vid}"
    if item.play_url:
        return f"url:{item.play_url.rstrip('/')}"
    return f"title:{item.title.strip().lower()}|{item.creator.strip().lower()}"


def text_identity_key(item: TextFeedItem, article: TextArticle | None = None) -> str:
    """Stable identity key for text feed deduplication."""
    doi = item.doi or (article.doi if article else "")
    if doi:
        return f"doi:{normalize_doi(doi)}"
    if item.url:
        return f"url:{item.url.rstrip('/')}"
    return f"title:{item.title.strip().lower()}"


def book_identity_key(item: BookItem) -> str:
    """Stable identity key for book deduplication."""
    if item.id:
        return f"id:{item.id}"
    return f"title:{item.title.strip().lower()}|{item.author.strip().lower()}"


_ASCII_TERM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#/_-]*$")


def _keyword_in_haystack(term: str, hay: str) -> bool:
    """Match CJK terms as substrings and Latin terms on word boundaries."""
    term = term.strip()
    if not term:
        return False
    if _ASCII_TERM_RE.match(term):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
        return re.search(pattern, hay, re.IGNORECASE) is not None
    return term in hay


def media_matches(item: MediaItem, date_filter: str, kw: str) -> bool:
    """Return True if a MediaItem matches optional date and keyword filters."""
    bdate = str(item.business_date or item.extra.get("business_date") or "")
    if date_filter and bdate != date_filter:
        return False
    if not kw:
        return True
    hay = " ".join(
        str(x)
        for x in (
            item.title,
            item.subtitle,
            item.creator,
            item.source_badge,
            item.summary,
            item.listening_cue,
            item.extra.get("artist", ""),
            item.extra.get("album", ""),
            item.extra.get("canonical_id", ""),
            item.extra.get("apple_music_song_id", ""),
            item.extra.get("topic", ""),
            item.extra.get("why_recommended", ""),
            item.extra.get("user_notes", ""),
            bdate,
        )
    ).lower()
    if _keyword_in_haystack(kw, hay):
        return True
    terms = [term for term in kw.split() if term]
    return len(terms) > 1 and any(_keyword_in_haystack(term, hay) for term in terms)


def text_matches(item: TextFeedItem, article: TextArticle | None, date_filter: str, kw: str) -> bool:
    """Return True if a TextFeedItem (+ optional TextArticle body) matches filters."""
    if date_filter and item.published_at != date_filter:
        return False
    if not kw:
        return True
    parts = [
        item.title,
        item.title_zh,
        item.excerpt,
        item.author,
        item.source_name,
        item.published_at,
        item.category,
        item.doi,
        " ".join(item.tags),
    ]
    if article:
        parts.extend([article.summary_zh, article.subtitle, " ".join(p.original for p in article.paragraphs)])
    hay = " ".join(parts).lower()
    if _keyword_in_haystack(kw, hay):
        return True
    terms = [term for term in kw.split() if term]
    return len(terms) > 1 and any(_keyword_in_haystack(term, hay) for term in terms)


def parse_incoming_media_item(raw: Mapping[str, Any], default_mode: str) -> MediaItem:
    """Validate and enrich an incoming MediaItem payload."""
    data = dict(raw)
    if not str(data.get("source_id") or data.get("sourceId") or "").strip():
        data["source_id"] = "plugin-ai-curator"
    if not str(data.get("creator") or "").strip() and isinstance(data.get("subtitle"), str):
        data["creator"] = data["subtitle"]
    extra = dict(data.get("extra")) if isinstance(data.get("extra"), Mapping) else {}
    extra.setdefault("curation_mode", default_mode)
    if not data.get("curation_mode") and not data.get("curationMode"):
        data["curation_mode"] = default_mode
    play_url = str(data.get("play_url") or data.get("playUrl") or "").strip()
    if not extra.get("apple_music_song_id") and play_url:
        if sid := extract_apple_music_song_id(play_url):
            extra["apple_music_song_id"] = sid
    if not extra.get("storefront") and "music.apple.com/" in play_url:
        tail = play_url.split("music.apple.com/", 1)[1]
        sf = tail.split("/", 1)[0]
        if len(sf) == 2 and sf.isalpha():
            extra["storefront"] = sf.lower()
    if not extra.get("video_id") and play_url:
        if vid := extract_youtube_video_id(play_url):
            extra["video_id"] = vid
    data["extra"] = extra
    return MediaItem.model_validate(data)


def parse_incoming_text_item(
    raw: Mapping[str, Any],
    default_category: str,
    *,
    preserve_category: bool = True,
) -> tuple[TextFeedItem, TextArticle]:
    """Validate and enrich an incoming TextFeedItem and paired TextArticle."""
    data = dict(raw)
    if not str(data.get("source_id") or data.get("sourceId") or "").strip():
        data["source_id"] = "plugin-ai-curator"
    if not str(data.get("category") or "").strip() or not preserve_category:
        data["category"] = default_category
    if not str(data.get("source_name") or data.get("sourceName") or "").strip():
        data["source_name"] = "AI 灵感阅读" if data["category"] == "interactive" else "AI 每日精选"
    extra = dict(data.get("extra")) if isinstance(data.get("extra"), Mapping) else {}
    if not str(data.get("published_at") or data.get("publishedAt") or "").strip():
        data["published_at"] = str(extra.get("business_date") or "")
    if not str(data.get("id") or "").strip():
        slug = re_sanitize(str(data.get("title") or "item"))
        data["id"] = f"txt-{slug}"

    why = str(extra.get("why_recommended") or "").strip()
    cue = str(extra.get("listening_or_reading_cue") or "").strip()
    if why and data.get("excerpt") == why:
        data["excerpt"] = ""

    feed = TextFeedItem.model_validate(data)
    if isinstance(raw.get("article"), Mapping):
        art_raw = dict(raw["article"])
        art_raw.setdefault("id", feed.id)
        art_raw.setdefault("title", feed.title)
        art_raw.setdefault("category", feed.category)
        art = TextArticle.model_validate(art_raw)
    else:
        paras = list(data.get("paragraphs")) if isinstance(data.get("paragraphs"), list) else []
        if not paras:
            idx = 1
            if feed.excerpt:
                paras.append({"id": f"p-{idx}", "kind": "paragraph", "original": feed.excerpt})
                idx += 1
            if why:
                paras.append({"id": f"p-{idx}", "kind": "heading", "original": "Why Recommended"})
                paras.append({"id": f"p-{idx + 1}", "kind": "paragraph", "original": why})
                idx += 2
            if cue:
                paras.append({"id": f"p-{idx}", "kind": "heading", "original": "Reading Cue"})
                paras.append({"id": f"p-{idx + 1}", "kind": "quote", "original": cue})
        art = TextArticle(
            id=feed.id,
            source_id=feed.source_id,
            source_name=feed.source_name,
            category=feed.category,
            channel_id=feed.channel_id,
            title=feed.title,
            title_zh=feed.title_zh,
            subtitle=str(data.get("subtitle") or f"{feed.source_name} · {feed.published_at}".strip(" ·")),
            authors=str(data.get("authors") or feed.author),
            published_at=feed.published_at,
            url=feed.url,
            doi=str(data.get("doi") or extra.get("doi") or ""),
            summary_zh=why,
            tags=list(feed.tags),
            paragraphs=paras,
            curation_mode=feed.curation_mode,
            business_date=feed.business_date,
            extra=extra,
        )
    return feed, art


def parse_incoming_book_item(raw: Mapping[str, Any]) -> BookItem:
    """Validate incoming BookItem, coercing legacy `markdown` format to `english_daily`."""
    data = dict(raw)
    if not str(data.get("source_id") or data.get("sourceId") or data.get("sourcePlugin") or "").strip():
        data["source_id"] = "plugin-ai-curator"
    if str(data.get("format") or "").strip().lower() == "markdown":
        extra = dict(data.get("extra")) if isinstance(data.get("extra"), Mapping) else {}
        extra.setdefault("original_format", "markdown")
        data["extra"] = extra
        data["format"] = "english_daily"
    return BookItem.model_validate(data)


def re_sanitize(value: str) -> str:
    """Sanitize arbitrary text into safe ASCII slug."""
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "_" for ch in value).strip("_")
    return cleaned[:64] or "item"


def _has_published_run_receipt(obsidian_dir: Path, project: str, business_date: str, operation_id: str) -> bool:
    if not obsidian_dir.is_dir():
        return False
    tx_runs = obsidian_dir / ".recommendation" / "transaction-runs" / project / business_date
    if tx_runs.is_dir():
        for fp in sorted(tx_runs.glob("*.json")):
            obj = read_json_strict(fp, repo_root=obsidian_dir, error_code="unreadable_ledger")
            if isinstance(obj, dict) and obj.get("operation_id") == operation_id:
                if str(obj.get("status") or "").lower() in {"artifacts_read_back", "published"}:
                    return True
    run_file = obsidian_dir / ".recommendation" / "runs" / f"{business_date}-{project}.json"
    if run_file.is_file():
        obj = read_json_strict(run_file, repo_root=obsidian_dir, error_code="unreadable_ledger")
        if isinstance(obj, dict) and obj.get("operation_id") == operation_id:
            if str(obj.get("phase") or "").lower() == "published":
                return True
    return False


def validate_publication_evidence(
    req: Mapping[str, Any],
    *,
    operation_id: str,
    business_date: str,
    project: str,
    paths: RepoPaths,
) -> dict[str, Any]:
    """Verify upstream PUBLISHED transaction & QA receipt; raise CuratorError(needs_verified_bundle) if missing."""
    raw_ev = req.get("publication_evidence")
    if isinstance(raw_ev, Mapping):
        evidence = dict(raw_ev)
    else:
        evidence = {
            k: req[k]
            for k in ("publication_verified", "receipt", "status", "operation_id", "project", "business_date")
            if k in req
        }
    if not evidence:
        raise CuratorError(
            "needs_verified_bundle",
            "publication_evidence is required; qa_verified alone is not publication proof",
        )
    if evidence.get("status") != "PUBLISHED":
        raise CuratorError("needs_verified_bundle", "publication_evidence.status must be 'PUBLISHED'")
    if evidence.get("publication_verified") is not True:
        raise CuratorError("needs_verified_bundle", "publication_evidence.publication_verified must be true")
    if evidence.get("operation_id") != operation_id:
        raise CuratorError("needs_verified_bundle", f"publication_evidence.operation_id does not match {operation_id}")
    if evidence.get("business_date") != business_date:
        raise CuratorError("needs_verified_bundle", f"publication_evidence.business_date does not match {business_date}")

    ev_proj = str(evidence.get("project") or "").strip()
    if not ev_proj or ev_proj not in ALLOWED_DAILY_PROJECTS or ev_proj != project:
        raise CuratorError("needs_verified_bundle", f"publication_evidence.project must match {project!r}")

    receipt = evidence.get("receipt")
    receipt_ok = False
    receipt_path_str = ""
    expected_sha = ""
    if isinstance(receipt, str) and receipt.strip():
        receipt_ok = True
    elif isinstance(receipt, Mapping):
        for key in ("id", "receipt_id", "path", "bundle_sha256", "manifest_blob", "commit_sha"):
            val = receipt.get(key)
            if isinstance(val, str) and val.strip():
                receipt_ok = True
        receipt_path_str = str(receipt.get("path") or "").strip()
        expected_sha = str(receipt.get("bundle_sha256") or "").strip()

    if not receipt_ok:
        raise CuratorError("needs_verified_bundle", "publication_evidence.receipt must identify a committed artifact")

    # Cross-check on-disk transaction state if repositories exist.
    obs_dir = paths.obsidian_vault_dir
    if obs_dir.is_dir():
        tx_candidate = obs_dir / ".recommendation" / "transactions" / project / f"{business_date}.json"
        if tx_candidate.is_file():
            tx_obj = read_json_strict(tx_candidate, repo_root=obs_dir, error_code="unreadable_ledger")
            if isinstance(tx_obj, dict) and str(tx_obj.get("status") or "").upper() == "PREPARED":
                if not _has_published_run_receipt(obs_dir, project, business_date, operation_id):
                    raise CuratorError(
                        "needs_verified_bundle",
                        f"Transaction {tx_candidate.name} is still PREPARED and not committed as PUBLISHED",
                    )

        if receipt_path_str:
            for root in (obs_dir, paths.music_daily_dir):
                if not root.is_dir():
                    continue
                resolved_root = root.resolve()
                target = (
                    safe_child_path(resolved_root, receipt_path_str)
                    if not Path(receipt_path_str).is_absolute()
                    else Path(receipt_path_str).resolve()
                )
                if target.exists() and target.is_relative_to(resolved_root):
                    rec_obj = read_json_strict(target, repo_root=root, error_code="unreadable_ledger")
                    if isinstance(rec_obj, dict):
                        if str(rec_obj.get("status") or "").upper() == "PREPARED":
                            if not _has_published_run_receipt(obs_dir, project, business_date, operation_id):
                                raise CuratorError(
                                    "needs_verified_bundle",
                                    f"Receipt path {receipt_path_str} points to uncommitted PREPARED bundle",
                                )
                        plan = rec_obj.get("plan") if isinstance(rec_obj.get("plan"), dict) else {}
                        rec_op = plan.get("operation_id") or rec_obj.get("operation_id")
                        if rec_op and rec_op != operation_id:
                            raise CuratorError(
                                "needs_verified_bundle",
                                f"Receipt operation_id {rec_op!r} mismatches {operation_id!r}",
                            )
                        if expected_sha and rec_obj.get("bundle_sha256") and rec_obj["bundle_sha256"] != expected_sha:
                            raise CuratorError("needs_verified_bundle", "Receipt bundle_sha256 mismatch")
                    break

    return evidence


def validate_ledgers_and_dedup(
    paths: RepoPaths,
    archive: HydratedArchive,
    operation_id: str,
    media: Sequence[MediaItem],
    texts: Sequence[TextFeedItem],
    articles: Sequence[TextArticle],
    *,
    obsidian_explicit: bool = False,
    music_explicit: bool = False,
) -> None:
    """Fail-closed validation of permanent ledgers and duplicate guard before daily publish."""
    if obsidian_explicit and not paths.obsidian_vault_dir.is_dir():
        raise CuratorError("unreadable_ledger", f"Configured obsidian_vault_dir missing: {paths.obsidian_vault_dir}")
    if music_explicit and not paths.music_daily_dir.is_dir():
        raise CuratorError("unreadable_ledger", f"Configured music_daily_dir missing: {paths.music_daily_dir}")

    prepared_ids: set[str] = set()
    history_keys: set[str] = set()

    obs_dir = paths.obsidian_vault_dir
    if obs_dir.is_dir():
        rec_dir = obs_dir / ".recommendation"
        if rec_dir.is_dir():
            pol = rec_dir / "policy.json"
            if pol.exists() or pol.is_symlink():
                obj = read_json_strict(pol, repo_root=obs_dir, error_code="unreadable_ledger")
                if not isinstance(obj, dict):
                    raise LedgerReadError(f"Invalid policy.json: {pol}", error_code="unreadable_ledger")

            for stream_name in ("youtube", "reddit", "english", "journal"):
                hp = rec_dir / "history" / f"{stream_name}.jsonl"
                if hp.exists() or hp.is_symlink():
                    for rec in parse_jsonl_history_file(hp, obs_dir, stream_name):
                        if rec.get("video_id"):
                            history_keys.add(f"vid:{rec['video_id']}")
                        if rec.get("post_id"):
                            history_keys.add(f"pid:{rec['post_id']}")
                        if rec.get("doi"):
                            history_keys.add(f"doi:{rec['doi']}")
                        if rec.get("url"):
                            history_keys.add(f"url:{rec['url'].rstrip('/')}")

            tx_dir = rec_dir / "transactions"
            if tx_dir.is_dir():
                for p in sorted(tx_dir.rglob("*.json")):
                    txj = read_json_strict(p, repo_root=obs_dir, error_code="unreadable_ledger")
                    if not isinstance(txj, dict):
                        raise LedgerReadError(f"Invalid transaction JSON: {p}", error_code="unreadable_ledger")
                    plan = txj.get("plan")
                    if isinstance(plan, dict) and plan.get("operation_id") == operation_id:
                        for rid in plan.get("exposed_resource_ids") or ():
                            if isinstance(rid, str):
                                prepared_ids.add(rid)

    mus_dir = paths.music_daily_dir
    if mus_dir.is_dir():
        tracks_path = mus_dir / "state" / "tracks.json"
        if tracks_path.exists() or tracks_path.is_symlink():
            parsed_state = parse_music_state_file(tracks_path, mus_dir)
            for cid in parsed_state["canonical_keys"]:
                history_keys.add(f"cid:{cid}")

    for m in media:
        sid = str(m.extra.get("apple_music_song_id") or "").strip()
        cid = str(m.extra.get("canonical_id") or "").strip()
        vid = str(m.extra.get("video_id") or "").strip()
        if sid and sid not in prepared_ids and f"song:{sid}" not in prepared_ids and sid in archive.song_id_to_date:
            raise CuratorError("duplicate_in_permanent_history", f"Duplicate Apple Music song_id: {sid}")
        if cid and cid not in prepared_ids and (f"cid:{cid}" in history_keys or cid in archive.canonical_id_to_date):
            raise CuratorError("duplicate_in_permanent_history", f"Duplicate canonical_id: {cid}")
        if vid and vid not in prepared_ids and f"youtube:{vid}" not in prepared_ids and (
            f"vid:{vid}" in history_keys or vid in archive.video_id_to_date
        ):
            raise CuratorError("duplicate_in_permanent_history", f"Duplicate YouTube video_id: {vid}")

    for t, a in zip(texts, articles):
        doi = normalize_doi(a.doi or t.doi)
        if doi and doi not in prepared_ids and f"doi:{doi}" not in prepared_ids and (
            f"doi:{doi}" in history_keys or doi in archive.doi_to_date
        ):
            raise CuratorError("duplicate_in_permanent_history", f"Duplicate DOI: {doi}")
        if t.category == "reddit" and t.url:
            pid = extract_reddit_post_id(t.url)
            if pid and pid not in prepared_ids and (f"pid:{pid}" in history_keys or pid in archive.post_id_to_date):
                raise CuratorError("duplicate_in_permanent_history", f"Duplicate Reddit post_id: {pid}")
        norm_url = t.url.rstrip("/")
        if norm_url and norm_url not in prepared_ids and (
            f"url:{norm_url}" in history_keys or norm_url in archive.url_to_date
        ):
            raise CuratorError("duplicate_in_permanent_history", f"Duplicate URL: {t.url}")


def update_markdown_preserving_notes(md_path: Path, auto_block: str, vault_root: Path) -> None:
    """Replace only `<!-- AUTO:START --> ... <!-- AUTO:END -->`, preserving frontmatter & ## Notes."""
    vroot = vault_root.resolve()
    if md_path.is_absolute():
        resolved_md = md_path.resolve()
        if not resolved_md.is_relative_to(vroot):
            raise CuratorError("invalid_markdown_path", "markdown update must stay inside obsidian_vault_dir")
        rel_part = resolved_md.relative_to(vroot).as_posix()
    else:
        rel_part = str(md_path)
    rel = safe_child_path(vroot, rel_part)
    wrapped = f"<!-- AUTO:START -->\n{auto_block.strip()}\n<!-- AUTO:END -->"
    if not rel.exists():
        atomic_write_text(rel, wrapped + "\n\n## Notes\n")
        return
    content = read_text_strict(rel, repo_root=vault_root).replace("\r\n", "\n")
    s_marker, e_marker = "<!-- AUTO:START -->", "<!-- AUTO:END -->"
    s_pos = content.find(s_marker)
    e_pos = content.find(e_marker)
    if s_pos != -1 and e_pos != -1 and e_pos > s_pos:
        new_content = content[:s_pos] + wrapped + content[e_pos + len(e_marker) :]
        atomic_write_text(rel, new_content)
        return

    notes_pos = -1
    for hdr in ("\n## My Notes", "\n## Notes", "\n### Notes"):
        p = content.find(hdr)
        if p != -1:
            notes_pos = p + 1
            break
    suffix = ("\n\n" + content[notes_pos:]) if notes_pos != -1 else "\n\n## Notes\n"
    prefix = ""
    if content.startswith("---\n"):
        end_fm = content.find("\n---", 4)
        if end_fm != -1:
            after_fm = content.find("\n", end_fm + 1)
            if after_fm != -1:
                prefix = content[: after_fm + 1] + "\n"
    atomic_write_text(rel, prefix + wrapped + suffix)


def _item_identity_keys(obj: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    ex = obj.get("extra") if isinstance(obj.get("extra"), Mapping) else {}

    def _add(prefix: str, val: Any) -> None:
        if isinstance(val, str) and val.strip():
            keys.append(prefix + val.strip())

    for src in (obj, ex):
        _add("song:", src.get("apple_music_song_id"))
        _add("song:", src.get("song_id"))
        _add("video:", src.get("video_id"))
        _add("video:", src.get("youtube_id"))
        _add("post:", src.get("post_id"))
        cid = src.get("canonical_id")
        if isinstance(cid, str) and cid.strip():
            _add("canonical:", normalize_for_dedup(cid))
        raw_doi = src.get("doi")
        if isinstance(raw_doi, str) and raw_doi.strip():
            _add("doi:", normalize_doi(raw_doi))

    for field in ("url", "play_url"):
        raw_u = str(obj.get(field) or "").strip()
        if not raw_u:
            continue
        _add("song:", extract_apple_music_song_id(raw_u))
        _add("video:", extract_youtube_video_id(raw_u))
        _add("post:", extract_reddit_post_id(raw_u))
        if raw_u.lower().startswith(("https://doi.org/", "http://doi.org/")):
            _add("doi:", normalize_doi(raw_u))
        clean_u = raw_u.split("#", 1)[0].rstrip("/")
        _add("url:", clean_u)

    title = normalize_for_dedup(str(obj.get("title") or ""))
    creator = normalize_for_dedup(str(obj.get("creator") or obj.get("author") or ""))
    if title and creator:
        keys.append(f"work:{creator}::{title}")
    if str(obj.get("kind") or "") != "music" and len(title) >= 16:
        keys.append(f"title:{title}")
    return keys


def build_provenance_index(
    archive: HydratedArchive,
    daily_media: Sequence[MediaItem],
    daily_texts: Sequence[TextFeedItem],
    daily_articles: Mapping[str, TextArticle],
) -> dict[str, str]:
    """Build read-only provenance lookup map from daily + historical archives only (never interactive)."""
    dates: dict[str, str] = {}

    def _remember(key: str, d: str) -> None:
        if not key or not is_valid_iso_date(d):
            return
        prev = dates.get(key)
        if prev is None or d < prev:
            dates[key] = d

    for k, d in archive.song_id_to_date.items():
        _remember(f"song:{k}", d)
    for k, d in archive.video_id_to_date.items():
        _remember(f"video:{k}", d)
    for k, d in archive.post_id_to_date.items():
        _remember(f"post:{k}", d)
    for k, d in archive.doi_to_date.items():
        _remember(f"doi:{k}", d)
    for k, d in archive.canonical_id_to_date.items():
        _remember(f"canonical:{normalize_for_dedup(k)}", d)

    for m in (*archive.media_items, *daily_media):
        bdate = str(m.business_date or m.extra.get("business_date") or "")
        for key in _item_identity_keys(m.model_dump()):
            _remember(key, bdate)
    for t in (*archive.text_items, *daily_texts):
        dump = t.model_dump()
        art = daily_articles.get(t.id) or archive.articles.get(t.id)
        if art and art.doi:
            dump["doi"] = art.doi
        for key in _item_identity_keys(dump):
            _remember(key, str(t.business_date or t.published_at or ""))
    return dates


def lookup_provenance_matches(prov_index: Mapping[str, str], items: Sequence[Any]) -> list[dict[str, str]]:
    """Match candidate items against read-only daily/archive provenance index."""
    if len(items) > MAX_PROVENANCE_ITEMS:
        raise CuratorError("too_many_items", f"lookup_provenance accepts at most {MAX_PROVENANCE_ITEMS} items")
    matches: list[dict[str, str]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        best_date = ""
        for key in _item_identity_keys(raw):
            cand = prov_index.get(key)
            if cand and (not best_date or cand < best_date):
                best_date = cand
        if best_date:
            matches.append(
                {
                    "id": str(raw.get("id") or ""),
                    "source_id": str(raw.get("source_id") or ""),
                    "previously_recommended_on": best_date,
                }
            )
    return matches


def match_item_provenance(prov_index: Mapping[str, str], raw_item: Mapping[str, Any]) -> str | None:
    """Return earliest historical recommendation date for a single item, if matched."""
    best = ""
    for key in _item_identity_keys(raw_item):
        cand = prov_index.get(key)
        if cand and (not best or cand < best):
            best = cand
    return best or None


async def sync_vocab_to_sm2_idempotent(
    ctx: PluginContext,
    cards: Sequence[VocabCard],
    curation_root: Path,
) -> dict[str, Any]:
    """Import vocabulary cards via `vocab.sm2.v1` idempotently without overwriting existing intervals."""
    if not cards or not ctx.capabilities.has("vocab.sm2.v1"):
        return {"enabled": ctx.capabilities.has("vocab.sm2.v1"), "added": 0, "skipped": 0}

    existing_words: set[str] = set()
    list_res = await ctx.capabilities.call("vocab.sm2.v1", {"action": "list", "limit": 10000})
    if list_res.ok and isinstance(list_res.data, Mapping):
        for key in ("cards", "items", "vocab"):
            arr = list_res.data.get(key)
            if isinstance(arr, list):
                for entry in arr:
                    if isinstance(entry, Mapping):
                        w = str(entry.get("word") or "").strip().lower()
                        if w:
                            existing_words.add(w)
                break

    pushed_path = curation_root / "vocab_pushed.json"
    if pushed_path.is_file():
        try:
            pdata = read_json_strict(pushed_path)
            if isinstance(pdata, dict) and isinstance(pdata.get("words"), list):
                for w in pdata["words"]:
                    if isinstance(w, str) and w.strip():
                        existing_words.add(w.strip().lower())
        except LedgerReadError:
            pass

    added = 0
    skipped = 0
    for card in cards:
        lw = card.word.strip().lower()
        if not lw or lw in existing_words:
            skipped += 1
            continue
        res = await ctx.capabilities.call(
            "vocab.sm2.v1",
            {
                "action": "add",
                "word": card.word,
                "lemma": card.lemma,
                "phonetic": card.phonetic,
                "etymology": card.etymology,
                "meaning": card.meaning,
                "context_sentence": card.context_sentence,
                "source_book": card.source_book,
                "source_date": card.source_date,
            },
        )
        if res.ok:
            existing_words.add(lw)
            added += 1

    if added > 0:
        atomic_write_json(pushed_path, {"schema_version": 1, "words": sorted(existing_words)})
    return {"enabled": True, "added": added, "skipped": skipped}


def save_id_list(path: Path, ids: set[str]) -> None:
    """Persist sorted set of item IDs atomically."""
    atomic_write_json(path, {"schema_version": 1, "ids": sorted(ids)})


def load_id_list(path: Path) -> set[str]:
    """Load persisted item ID set safely."""
    if not path.exists():
        return set()
    data = read_json_strict(path, error_code="corrupted_state")
    if isinstance(data, dict) and isinstance(data.get("ids"), list):
        return {str(x) for x in data["ids"] if isinstance(x, str) and x}
    return set()


def load_interactive_session(path: Path, fallback_id: str) -> InteractiveSession:
    """Load and validate an interactive session JSON file."""
    raw = read_json_strict(path, error_code="corrupted_state")
    if not isinstance(raw, dict):
        raise LedgerReadError(f"Interactive session must be a JSON object: {path}", error_code="corrupted_state")
    media = [parse_incoming_media_item(m, "interactive_ephemeral") for m in raw.get("media_items") or [] if isinstance(m, Mapping)]
    texts: list[TextFeedItem] = []
    articles: dict[str, TextArticle] = {}
    for t in raw.get("text_items") or []:
        if isinstance(t, Mapping):
            fi, art = parse_incoming_text_item(t, "interactive", preserve_category=True)
            texts.append(fi)
            articles[art.id] = art
    for aid, art_raw in (raw.get("articles") or {}).items():
        if isinstance(art_raw, Mapping):
            art = TextArticle.model_validate({**art_raw, "id": art_raw.get("id") or aid})
            articles[art.id] = art
    books = [parse_incoming_book_item(b) for b in raw.get("book_items") or [] if isinstance(b, Mapping)]
    spreads: dict[str, BookSpread] = {}
    for bid, sp_raw in (raw.get("book_spreads") or {}).items():
        if isinstance(sp_raw, Mapping):
            sp = BookSpread.model_validate({**sp_raw, "book_id": sp_raw.get("book_id") or bid})
            spreads[sp.book_id] = sp
    return InteractiveSession(
        session_id=str(raw.get("session_id") or fallback_id),
        title=str(raw.get("title") or "AI 灵感推荐"),
        user_prompt=str(raw.get("user_prompt") or ""),
        repeat_policy=str(raw.get("repeat_policy") or "allow_any"),
        media_items=media,
        text_items=texts,
        articles=articles,
        book_items=books,
        book_spreads=spreads,
    )


def save_interactive_session(curation_root: Path, session: InteractiveSession) -> Path:
    """Atomically write interactive session JSON under curation/interactive/sessions/."""
    if not safe_identifier(session.session_id):
        raise CuratorError("invalid_session_id", "Unsafe session_id")
    target = safe_child_path(curation_root, "interactive", "sessions", f"{session.session_id}.json")
    payload = {
        "schema_version": 2,
        "mode": "interactive_ephemeral",
        "session_id": session.session_id,
        "title": session.title,
        "user_prompt": session.user_prompt,
        "repeat_policy": session.repeat_policy,
        "record_permanent_history": False,
        "media_items": [m.model_dump() for m in session.media_items],
        "text_items": [t.model_dump() for t in session.text_items],
        "articles": {k: v.model_dump() for k, v in session.articles.items()},
        "book_items": [b.model_dump() for b in session.book_items],
        "book_spreads": {k: v.model_dump() for k, v in session.book_spreads.items()},
    }
    atomic_write_json(target, payload)
    return target
