"""Bilingual subtitle pre-translation (llm.translate.v1), caching, and speech.asr.v1 fallback."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .yt_http import atomic_write_json
    from .yt_security import (
        YouTubePathSafetyError,
        default_cache_root,
        resolve_safe_cache_dir,
        safe_cache_file,
        validate_lang,
    )
    from .yt_subtitles import download_and_parse_track, extract_subtitle_tracks, select_best_track
else:
    from yt_http import atomic_write_json
    from yt_security import (
        YouTubePathSafetyError,
        default_cache_root,
        resolve_safe_cache_dir,
        safe_cache_file,
        validate_lang,
    )
    from yt_subtitles import download_and_parse_track, extract_subtitle_tracks, select_best_track


def _parse_translation_response(data: Mapping[str, Any], expected_count: int) -> list[str] | None:
    for key in ("translations", "translated_texts", "results"):
        val = data.get(key)
        if isinstance(val, list) and len(val) == expected_count:
            out = [
                str(item.get("translation") or item.get("text") or item.get("translated") or "") if isinstance(item, Mapping) else str(item or "")
                for item in val
            ]
            if all(out):
                return out
    cues_val = data.get("cues")
    if isinstance(cues_val, list) and len(cues_val) == expected_count:
        out = [
            str(item.get("translation") or item.get("translated") or item.get("text") or "")
            for item in cues_val
            if isinstance(item, Mapping)
        ]
        if len(out) == expected_count and all(out):
            return out
    reply = data.get("reply") or data.get("translated")
    if isinstance(reply, str) and reply.strip():
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and len(parsed) == expected_count:
                out = [
                    str(x.get("text") or x.get("translated") or "") if isinstance(x, Mapping) else str(x or "")
                    for x in parsed
                ]
                if all(out):
                    return out
        except json.JSONDecodeError:
            lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
            if len(lines) == expected_count:
                return lines
    return None


async def translate_cues_with_capability(
    ctx: PluginContext, cues: list[dict[str, Any]], *, source_lang: str, target_lang: str, title: str = ""
) -> tuple[list[dict[str, Any]], bool, str]:
    """Translate cues via optional `llm.translate.v1` capability; degrade gracefully if absent or failed."""
    if not cues:
        return cues, False, "no_cues"
    if not ctx.capabilities.has("llm.translate.v1"):
        return cues, False, "llm.translate.v1_unavailable"

    copied = [dict(c) for c in cues]
    for start in range(0, len(copied), 25):
        chunk = copied[start : start + 25]
        texts = [c["original"] for c in chunk]
        req = {
            "action": "translate",
            "domain": "media",
            "context": title,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "texts": texts,
            "cues": [{"id": idx, "text": txt} for idx, txt in enumerate(texts)],
        }
        res = await ctx.capabilities.call("llm.translate.v1", req)
        if not res.ok:
            return cues, False, f"llm.translate.v1_failed:{res.data.get('error_code', 'error')}"
        translated = _parse_translation_response(res.data, len(chunk))
        if translated is None:
            return cues, False, "llm.translate.v1_invalid_response"
        for cue, tr in zip(chunk, translated):
            cue["translation"] = tr.strip()
    return copied, True, ""


async def try_asr_fallback(
    ctx: PluginContext, *, video_id: str, media_path: str = "", audio_path: str = "", lang: str = ""
) -> tuple[list[dict[str, Any]] | None, str]:
    """Attempt offline transcription via optional `speech.asr.v1` when no subtitles exist."""
    if not ctx.capabilities.has("speech.asr.v1"):
        return None, "speech.asr.v1_unavailable"
    action = "transcribe_audio" if audio_path and not media_path else "transcribe_media"
    req: dict[str, Any] = {"action": action, "video_id": video_id}
    if media_path:
        req["media_path"] = media_path
    if audio_path:
        req["audio_path"] = audio_path
    if lang:
        req["lang"] = lang
    res = await ctx.capabilities.call("speech.asr.v1", req)
    if not res.ok:
        return None, f"speech.asr.v1_failed:{res.data.get('error_code', 'error')}"
    raw_cues = res.data.get("cues") or []
    if not isinstance(raw_cues, list) or not raw_cues:
        return None, "speech.asr.v1_empty"
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_cues, 1):
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or item.get("original") or "").strip()
        if not text:
            continue
        start_ms = int(item.get("start_ms") if item.get("start_ms") is not None else float(item.get("start_sec", 0)) * 1000)
        end_ms = int(item.get("end_ms") if item.get("end_ms") is not None else float(item.get("end_sec", start_ms / 1000.0)) * 1000)
        s_ms, e_ms = max(0, start_ms), max(max(0, start_ms), end_ms)
        normalized.append(
            {
                "index": idx,
                "start_ms": s_ms,
                "end_ms": e_ms,
                "start_sec": s_ms / 1000.0,
                "end_sec": e_ms / 1000.0,
                "original": text,
                "translation": "",
                "text": text,
            }
        )
    return (normalized, "") if normalized else (None, "speech.asr.v1_empty")


def resolve_subtitle_cache_path(
    ctx: PluginContext, configured_cache_dir: str, video_id: str, lang: str, target_lang: str
) -> Path:
    """Resolve safe cache path inside configured or default cache directory."""
    root = resolve_safe_cache_dir(configured_cache_dir, default_cache_root(ctx))
    return safe_cache_file(root, "subtitles", f"{video_id}.{validate_lang(lang)}.{validate_lang(target_lang or 'none')}.json")


def read_cached_subtitles(cache_path: Path) -> dict[str, Any] | None:
    """Read cached bilingual subtitle JSON if valid and not a symlink."""
    if cache_path.is_symlink() or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, Mapping) and isinstance(data.get("cues"), list) and data.get("cues"):
        return dict(data)
    return None


async def fetch_subtitles_flow(
    plugin: Any,
    req: Mapping[str, Any],
    vid: str,
) -> CapabilityResult:
    """Execute full subtitle fetch -> parse -> ASR fallback -> bilingual translation & cache flow."""
    ctx: PluginContext = plugin.ctx
    raw_lang = str(req.get("lang") or "").strip()
    raw_target = str(req.get("target_lang") or plugin.target_lang()).strip()
    bilingual = bool(req.get("bilingual", True))
    force_refresh = bool(req.get("force_refresh", False))

    try:
        if raw_lang:
            validate_lang(raw_lang)
        if raw_target:
            validate_lang(raw_target)
        cache_path = resolve_subtitle_cache_path(
            ctx, plugin.cache_dir_config(), vid, raw_lang or "auto", raw_target if bilingual else "mono"
        )
    except (ValueError, YouTubePathSafetyError) as exc:
        code = "path_safety" if isinstance(exc, YouTubePathSafetyError) else "invalid_argument"
        return plugin._fail(str(exc), error_code=code)

    if bilingual and not force_refresh:
        cached = read_cached_subtitles(cache_path)
        if cached is not None and cached.get("translated"):
            return CapabilityResult.ok({**cached, "cached": True, "cache_path": str(cache_path)})

    info, err_res = await plugin._load_video_info_for_subs(req, vid)
    if err_res is not None or info is None:
        return err_res or plugin._fail("无法获取字幕元数据", error_code="service_offline")

    tracks = extract_subtitle_tracks(
        info, allow_loopback_http=plugin.allow_loopback_http, allowed_loopback_origins=plugin.allowed_loopback_origins()
    )
    chosen = select_best_track(tracks, lang=raw_lang)

    cues: list[dict[str, Any]] | None = None
    track_lang, source_type, fmt = raw_lang or "und", "none", ""
    if chosen is not None:
        cues, dl_err = await download_and_parse_track(
            chosen,
            timeout_sec=plugin.timeout_sec(),
            extra_secrets=plugin.secret_values(),
            allow_loopback_http=plugin.allow_loopback_http,
            allowed_loopback_origins=plugin.allowed_loopback_origins(),
        )
        if dl_err is not None or cues is None:
            err_copy = dict(dl_err or {})
            return plugin._fail(
                str(err_copy.pop("error", "下载或解析字幕失败")),
                error_code=str(err_copy.pop("error_code", "service_offline")),
                data=err_copy,
            )
        track_lang, source_type, fmt = str(chosen["lang"]), str(chosen["source_type"]), str(chosen["ext"])
    else:
        cues, asr_reason = await try_asr_fallback(
            ctx, video_id=vid, media_path=str(req.get("media_path") or ""), audio_path=str(req.get("audio_path") or ""), lang=raw_lang
        )
        if cues is None:
            return plugin._fail(
                "该视频无可用官方或自动字幕，且离线语音识别不可用或失败",
                error_code="not_found",
                data={"video_id": vid, "cues": [], "degraded": True, "asr_available": ctx.capabilities.has("speech.asr.v1"), "asr_reason": asr_reason},
            )
        source_type, fmt = "asr", "asr"

    translated, translation_degraded, degraded_reason = False, False, ""
    need_translate = bilingual and bool(raw_target) and track_lang.split("-", 1)[0].lower() != raw_target.split("-", 1)[0].lower()
    if need_translate:
        cues, translated, degraded_reason = await translate_cues_with_capability(
            ctx, cues, source_lang=track_lang, target_lang=raw_target, title=str(info.get("title") or "")
        )
        translation_degraded = not translated

    result_payload: dict[str, Any] = {
        "video_id": vid,
        "lang": track_lang,
        "target_lang": raw_target if bilingual else "",
        "source_type": source_type,
        "format": fmt,
        "bilingual": translated,
        "translated": translated,
        "translation_degraded": translation_degraded,
        "degraded_reason": degraded_reason,
        "cached": False,
        "cues": cues,
        "count": len(cues),
    }
    if translated:
        atomic_write_json(cache_path, result_payload)
        result_payload["cache_path"] = str(cache_path)
    return CapabilityResult.ok(result_payload)
