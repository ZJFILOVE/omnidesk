"""Companion reading actions: lookup_term, explain_sentence, clip_jump, and book.sync."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import CapabilityResult

from .vocab import VocabStore, normalize_word

if TYPE_CHECKING:
    from omnidesk_sdk import PluginContext

_PREFIX_ROOTS: tuple[tuple[str, str], ...] = (
    ("trans", "across / through (Latin trans-)"),
    ("inter", "between / among (Latin inter-)"),
    ("super", "above / beyond (Latin super-)"),
    ("infra", "below / beneath (Latin infra-)"),
    ("retro", "backward (Latin retro-)"),
    ("tele", "far / distant (Greek tele-)"),
    ("meta", "beyond / change (Greek meta-)"),
    ("anti", "against (Greek anti-)"),
    ("auto", "self (Greek auto-)"),
    ("hemi", "half (Greek hemi-)"),
    ("poly", "many (Greek poly-)"),
    ("mono", "one / single (Greek mono-)"),
    ("epi", "upon / over / near (Greek epi-)"),
    ("pro", "forward / forth (Latin/Greek pro-)"),
    ("pre", "before (Latin prae-)"),
    ("sub", "under (Latin sub-)"),
    ("con", "together / with (Latin com-)"),
    ("dis", "apart / reversal (Latin dis-)"),
    ("re", "again / back (Latin re-)"),
    ("un", "not / reversal (Old English un-)"),
    ("in", "in / not (Latin in-)"),
)

_SUFFIX_ROOTS: tuple[tuple[str, str], ...] = (
    ("ology", "study of (Greek -logia)"),
    ("metry", "measurement (Greek -metria)"),
    ("graph", "writing / drawing (Greek -graphia)"),
    ("morph", "form / shape (Greek morphe)"),
    ("chron", "time (Greek chronos)"),
    ("tion", "state / action noun (Latin -tio)"),
    ("sion", "state / action noun (Latin -sio)"),
    ("ment", "result / means (Latin -mentum)"),
    ("ness", "quality / state (Old English -nes)"),
    ("able", "capable of (Latin -abilis)"),
    ("ible", "capable of (Latin -ibilis)"),
    ("ous", "full of / characterized by (Latin -osus)"),
    ("ive", "having the nature of (Latin -ivus)"),
    ("ize", "to make / become (Greek -izein)"),
    ("ise", "to make / become (Greek -izein)"),
    ("al", "pertaining to (Latin -alis)"),
    ("ic", "relating to (Greek -ikos)"),
    ("ly", "in the manner of (Old English -lice)"),
)

_CLAUSE_SPLIT_RE = re.compile(r"(?:[;—–]+|--|\s*,\s+(?=(?:which|who|whom|whose|where|when|while|whereas|although|though|because|since|unless|until|yet|but|and|so)\b))", re.IGNORECASE)
_CONNECTORS_RE = re.compile(
    r"\b(?:although|though|whereas|while|because|since|unless|until|which|that|who|whom|whose|however|nevertheless|therefore|moreover|yet|but)\b",
    re.IGNORECASE,
)
_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the", "and", "that", "this", "with", "from", "have", "has", "had", "were", "was",
        "are", "been", "being", "their", "there", "these", "those", "which", "while", "where",
        "when", "what", "would", "could", "should", "about", "after", "before", "into", "over",
        "under", "between", "through", "although", "because", "since", "however", "though",
    }
)


def _analyze_word_morphology(word: str) -> dict[str, Any]:
    affixes: list[str] = []
    stem = word
    for prefix, gloss in _PREFIX_ROOTS:
        if len(stem) > len(prefix) + 2 and stem.startswith(prefix):
            affixes.append(f"{prefix}- ({gloss})")
            stem = stem[len(prefix) :]
            break
    for suffix, gloss in _SUFFIX_ROOTS:
        if len(stem) > len(suffix) + 2 and stem.endswith(suffix):
            affixes.append(f"-{suffix} ({gloss})")
            stem = stem[: -len(suffix)]
            break
    hint = " + ".join(affixes) if affixes else f"base form '{word}'"
    return {"affixes": affixes, "stem_guess": stem, "etymology_hint": hint}


def _extract_json_dict(raw_text: str) -> dict[str, Any] | None:
    text = raw_text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_clips(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("clips", "matches", "items", "results"):
        val = data.get(key)
        if isinstance(val, list):
            return [dict(item) for item in val if isinstance(item, Mapping)]
    single = data.get("clip")
    if isinstance(single, Mapping):
        return [dict(single)]
    return []


async def search_media_clips(
    ctx: PluginContext | None,
    query: str,
    *,
    limit: int = 5,
    auto_open_player: bool = False,
) -> dict[str, Any]:
    """Query optional `media.clip_jump.v1` with graceful offline degradation."""
    clean_q = query.strip()
    if not clean_q:
        return {"available": False, "degraded": True, "clips": [], "query": ""}
    if ctx is None or not ctx.capabilities.has("media.clip_jump.v1"):
        return {
            "available": False,
            "degraded": True,
            "clips": [],
            "query": clean_q,
            "reason": "media.clip_jump.v1 unavailable",
        }
    try:
        res = await ctx.capabilities.call(
            "media.clip_jump.v1",
            {"query": clean_q, "limit": max(1, int(limit)), "auto_open_player": bool(auto_open_player)},
        )
    except Exception as exc:
        return {
            "available": False,
            "degraded": True,
            "clips": [],
            "query": clean_q,
            "reason": str(exc),
        }
    if not res.ok:
        return {
            "available": False,
            "degraded": True,
            "clips": [],
            "query": clean_q,
            "reason": res.error or "media.clip_jump.v1 failed",
        }
    clips = _extract_clips(res.data)
    return {
        "available": True,
        "degraded": False,
        "clips": clips,
        "count": len(clips),
        "query": clean_q,
        "provider_plugin_id": res.provider_plugin_id,
    }


async def sync_with_remote(
    ctx: PluginContext | None,
    *,
    sync_enabled: bool,
    action: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Best-effort invocation of optional `book.sync.v1`; failures never disrupt local state."""
    if not sync_enabled or ctx is None or not ctx.capabilities.has("book.sync.v1"):
        return {"synced": False, "skipped": True, "reason": "sync disabled or book.sync.v1 unavailable"}
    try:
        res = await ctx.capabilities.call("book.sync.v1", {"action": action, **dict(payload)})
    except Exception as exc:
        return {"synced": False, "skipped": False, "error": str(exc)}
    if not res.ok:
        return {"synced": False, "skipped": False, "error": res.error or "book.sync.v1 returned failure"}
    return {"synced": True, "skipped": False, "data": dict(res.data)}


async def lookup_term(
    ctx: PluginContext | None,
    vocab_store: VocabStore,
    req: Mapping[str, Any],
    *,
    target_language: str = "zh-CN",
    auto_jump_nas: bool = True,
    sync_enabled: bool = True,
) -> CapabilityResult:
    """Look up term etymology, meaning, context, optional LLM enrichment, and NAS clips."""
    raw_word = req.get("word") if req.get("word") is not None else req.get("term")
    word = normalize_word(raw_word)
    context_sentence = str(req.get("context_sentence") or req.get("context") or "").strip()
    source_book = str(req.get("source_book") or req.get("book_title") or req.get("book_id") or "").strip()
    lang = str(req.get("target_language") or target_language or "zh-CN").strip()

    existing = vocab_store.get_card(word)
    morph = _analyze_word_morphology(word)

    meaning = str((existing or {}).get("meaning") or "")
    etymology = str((existing or {}).get("etymology") or "") or morph["etymology_hint"]
    phonetic = str((existing or {}).get("phonetic") or "")
    pos = str((existing or {}).get("pos") or "")
    if not context_sentence and existing:
        context_sentence = str(existing.get("context_sentence") or "")

    text_only = True
    llm_used = False
    llm_explanation = ""
    served_model = ""

    if ctx is not None and ctx.capabilities.has("llm.chat.v1"):
        prompt = (
            f"Explain the term '{word}' for language learners (target={lang}). "
            f"Return JSON with keys meaning, etymology, phonetic, pos, explanation."
        )
        if context_sentence:
            prompt += f" Context: {context_sentence}"
        try:
            llm_res = await ctx.capabilities.call(
                "llm.chat.v1",
                {"action": "etymology", "domain": "book", "context": context_sentence, "prompt": prompt},
            )
            if llm_res.ok:
                reply = str(llm_res.data.get("reply") or "").strip()
                structured = llm_res.data.get("structured")
                parsed = dict(structured) if isinstance(structured, Mapping) else _extract_json_dict(reply)
                if parsed:
                    meaning = str(parsed.get("meaning") or meaning)
                    etymology = str(parsed.get("etymology") or etymology)
                    phonetic = str(parsed.get("phonetic") or phonetic)
                    pos = str(parsed.get("pos") or pos)
                    llm_explanation = str(parsed.get("explanation") or reply)
                elif reply:
                    llm_explanation = reply
                if parsed or reply:
                    text_only = False
                    llm_used = True
                    served_model = str(llm_res.data.get("served_model") or "")
        except Exception:
            pass

    do_clips = bool(req.get("search_clips", auto_jump_nas))
    clip_info = (
        await search_media_clips(
            ctx,
            word,
            limit=int(req.get("clip_limit", 5) or 5),
            auto_open_player=bool(req.get("auto_open_player", False)),
        )
        if do_clips
        else {"available": False, "degraded": True, "clips": [], "query": word}
    )
    clips = clip_info["clips"]
    nas_clip = clips[0] if clips else ((existing or {}).get("nas_clip") if existing else None)

    vocab_card = existing
    sync_status: dict[str, Any] | None = None
    if bool(req.get("add_to_vocab", False)):
        card_payload = {
            "word": word,
            "meaning": meaning,
            "etymology": etymology,
            "phonetic": phonetic,
            "pos": pos,
            "context_sentence": context_sentence,
            "source_book": source_book,
            "nas_clip": nas_clip,
        }
        vocab_card, _ = vocab_store.add_card(card_payload)
        sync_status = await sync_with_remote(
            ctx,
            sync_enabled=sync_enabled,
            action="sync_vocab",
            payload={"plugin_id": vocab_store._plugin_id, "cards": [vocab_card]},
        )

    return CapabilityResult.ok(
        {
            "word": word,
            "lemma": str((vocab_card or {}).get("lemma") or word),
            "phonetic": phonetic,
            "pos": pos,
            "meaning": meaning,
            "etymology": etymology,
            "affixes": morph["affixes"],
            "context_sentence": context_sentence,
            "source_book": source_book,
            "explanation": llm_explanation or f"{word}: {etymology}",
            "in_vocab": vocab_card is not None,
            "vocab_card": vocab_card,
            "clips": clips,
            "nas_clip": nas_clip,
            "clip_jump_available": bool(clip_info.get("available")),
            "text_only": text_only,
            "llm_used": llm_used,
            "served_model": served_model,
            "target_language": lang,
            "sync_status": sync_status,
        }
    )


def _segment_sentence(sentence: str) -> dict[str, Any]:
    raw_clauses = [part.strip(" ,;") for part in _CLAUSE_SPLIT_RE.split(sentence) if part and part.strip(" ,;")]
    if not raw_clauses:
        raw_clauses = [sentence.strip()]
    connectors = list(dict.fromkeys(m.group(0).lower() for m in _CONNECTORS_RE.finditer(sentence)))
    tokens = _WORD_TOKEN_RE.findall(sentence)
    key_terms: list[str] = []
    seen_terms: set[str] = set()
    for tok in tokens:
        low = tok.lower()
        if len(low) >= 6 and low not in _STOP_WORDS and low not in seen_terms:
            seen_terms.add(low)
            key_terms.append(low)
    return {
        "clauses": raw_clauses,
        "clause_count": len(raw_clauses),
        "word_count": len(tokens),
        "connectors": connectors,
        "key_terms": key_terms[:10],
    }


async def explain_sentence(
    ctx: PluginContext | None,
    req: Mapping[str, Any],
    *,
    target_language: str = "zh-CN",
) -> CapabilityResult:
    """Break down complex sentences; uses optional `llm.chat.v1` or degrades to text_only=True."""
    raw_sentence = req.get("sentence") if req.get("sentence") is not None else req.get("text")
    if not isinstance(raw_sentence, str) or not raw_sentence.strip():
        return CapabilityResult.fail("sentence must be a non-empty string", error_code="invalid_argument")
    sentence = " ".join(raw_sentence.strip().split())
    context = str(req.get("context") or "").strip()
    lang = str(req.get("target_language") or target_language or "zh-CN").strip()

    structure = _segment_sentence(sentence)
    offline_summary = (
        f"[Offline Structural Breakdown | {structure['clause_count']} clause(s), "
        f"{structure['word_count']} words] "
        + " | ".join(f"({idx}) {c}" for idx, c in enumerate(structure["clauses"], start=1))
    )

    if ctx is not None and ctx.capabilities.has("llm.chat.v1"):
        prompt = (
            f"Break down the syntax, clauses, and meaning of this sentence (target={lang}): "
            f"{sentence}"
        )
        try:
            llm_res = await ctx.capabilities.call(
                "llm.chat.v1",
                {"action": "clause_parse", "domain": "book", "context": context or sentence, "prompt": prompt},
            )
            if llm_res.ok:
                reply = str(llm_res.data.get("reply") or "").strip()
                structured = llm_res.data.get("structured")
                if reply or structured:
                    return CapabilityResult.ok(
                        {
                            "sentence": sentence,
                            "analysis": reply or json.dumps(structured, ensure_ascii=False),
                            "structured": structured,
                            **structure,
                            "text_only": False,
                            "llm_used": True,
                            "served_model": str(llm_res.data.get("served_model") or ""),
                            "target_language": lang,
                        }
                    )
        except Exception:
            pass

    return CapabilityResult.ok(
        {
            "sentence": sentence,
            "analysis": offline_summary,
            "structured": None,
            **structure,
            "text_only": True,
            "llm_used": False,
            "degraded_reason": "llm.chat.v1 unavailable",
            "target_language": lang,
        }
    )
