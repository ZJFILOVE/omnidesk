"""Bilingual article & comment-tree translator via llm.translate.v1 / llm.chat.v1 with SHA-256 disk cache."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, CommentNode, PluginContext, TextArticle

if __package__:
    from .security import FeedRecipeError, atomic_write_text, validate_safe_path
else:
    from security import FeedRecipeError, atomic_write_text, validate_safe_path

CapHasFn = Callable[[str], bool]
CapCallFn = Callable[[str, dict[str, Any]], Any]
_MAX_MEM_CACHE = 512
_COMMENT_TRANSLATE_BUDGET = 32


def is_already_target_language(text: str, target_lang: str) -> bool:
    """Return True if text is already predominantly in target Chinese language."""
    if not target_lang.strip().lower().startswith("zh"):
        return False
    cjk = 0
    ascii_alpha = 0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            cjk += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            ascii_alpha += 1
    return cjk >= 6 and cjk > (ascii_alpha // 2)


class Translator:
    """SHA-256 cached translator skipping code/formula blocks and capping comment depth."""

    def __init__(self, ctx: PluginContext | None = None) -> None:
        self._ctx = ctx
        self._custom_has: CapHasFn | None = None
        self._custom_call: CapCallFn | None = None
        self.cache_dir: Path | None = None
        self.enabled: bool = True
        self.target_lang: str = "zh-CN"
        self._mem_cache: dict[str, str] = {}

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def set_custom_transport(self, has_cap: CapHasFn | None, call_cap: CapCallFn | None) -> None:
        self._custom_has = has_cap
        self._custom_call = call_cap

    def configure(self, cache_dir: str | Path | None, enabled: bool, target_lang: str) -> None:
        self.enabled = bool(enabled)
        self.target_lang = (target_lang or "zh-CN").strip() or "zh-CN"
        self.cache_dir = None
        if cache_dir:
            try:
                resolved = validate_safe_path(cache_dir, check_symlinks=True)
                resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.cache_dir = resolved
            except (FeedRecipeError, OSError):
                self.cache_dir = None

    @staticmethod
    def compute_cache_key(target_lang: str, text: str) -> str:
        payload = f"{(target_lang or 'zh-CN').strip().lower()}\n{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def cache_size(self) -> int:
        return len(self._mem_cache)

    def _has_cap(self, cap_id: str) -> bool:
        if self._custom_has is not None:
            return bool(self._custom_has(cap_id))
        if self._ctx is not None:
            return bool(self._ctx.capabilities.has(cap_id))
        return False

    async def _call_cap(self, cap_id: str, request: dict[str, Any]) -> CapabilityResult:
        if self._custom_call is not None:
            out = self._custom_call(cap_id, request)
            return await out if inspect.isawaitable(out) else out
        if self._ctx is not None:
            return await self._ctx.capabilities.call(cap_id, request)
        return CapabilityResult.fail("No context", error_code="service_offline")

    def is_capability_available(self) -> bool:
        return self._has_cap("llm.translate.v1") or self._has_cap("llm.chat.v1")

    def _put_mem(self, key: str, translated: str) -> None:
        if len(self._mem_cache) >= _MAX_MEM_CACHE and key not in self._mem_cache:
            self._mem_cache.pop(next(iter(self._mem_cache)))
        self._mem_cache[key] = translated

    def _lookup_disk_cache(self, key: str) -> str:
        if key in self._mem_cache:
            return self._mem_cache[key]
        if self.cache_dir is None:
            return ""
        try:
            path = validate_safe_path(f"{key}.json", base_dir=self.cache_dir, check_symlinks=True)
        except FeedRecipeError:
            return ""
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(data, Mapping):
            return ""
        tr = str(data.get("translated") or "").strip()
        if tr:
            self._put_mem(key, tr)
        return tr

    def _write_disk_cache(self, key: str, source: str, translated: str) -> None:
        if not translated:
            return
        self._put_mem(key, translated)
        if self.cache_dir is None:
            return
        try:
            dest = validate_safe_path(f"{key}.json", base_dir=self.cache_dir, check_symlinks=True)
            payload = json.dumps(
                {"key": key, "source_len": len(source), "translated": translated},
                ensure_ascii=False,
            )
            atomic_write_text(dest, payload, file_mode=0o600)
        except (FeedRecipeError, OSError):
            pass

    async def _invoke_llm_translation(self, text: str, target_lang: str) -> str:
        if self._has_cap("llm.translate.v1"):
            res = await self._call_cap(
                "llm.translate.v1",
                {
                    "action": "translate",
                    "text": text,
                    "target_lang": target_lang,
                    "source_lang": "auto",
                    "preserve_terms": True,
                },
            )
            if res.ok and isinstance(res.data, Mapping):
                for key in ("translated", "translation", "reply"):
                    val = str(res.data.get(key) or "").strip()
                    if val:
                        return val

        if self._has_cap("llm.chat.v1"):
            res = await self._call_cap(
                "llm.chat.v1",
                {
                    "action": "complete",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"Translate accurately to {target_lang}. "
                                "Preserve technical terms, identifiers, and formatting. Output only the translation."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.2,
                },
            )
            if res.ok and isinstance(res.data, Mapping):
                for key in ("content", "reply", "translated", "translation"):
                    val = str(res.data.get(key) or "").strip()
                    if val:
                        return val
        return ""

    async def translate_text(self, text: str, target_lang: str = "") -> str:
        clean = (text or "").strip()
        if not clean or not self.enabled:
            return ""
        lang = (target_lang or self.target_lang).strip() or "zh-CN"
        if is_already_target_language(clean, lang):
            return ""

        key = self.compute_cache_key(lang, clean)
        cached = self._lookup_disk_cache(key)
        if cached:
            return cached
        if not self.is_capability_available():
            return ""
        translated = await self._invoke_llm_translation(clean, lang)
        if translated:
            self._write_disk_cache(key, clean, translated)
        return translated

    async def _translate_comments_recursive(
        self,
        nodes: list[CommentNode],
        target_lang: str,
        budget: list[int],
    ) -> bool:
        any_tr = False
        for node in nodes:
            if budget[0] <= 0:
                break
            if node.translated_body:
                any_tr = True
            elif node.body:
                tr = await self.translate_text(node.body, target_lang)
                if tr:
                    node.translated_body = tr
                    any_tr = True
                budget[0] -= 1
            if node.replies and budget[0] > 0:
                if await self._translate_comments_recursive(node.replies, target_lang, budget):
                    any_tr = True
        return any_tr

    async def translate_article(
        self,
        article: TextArticle,
        target_lang: str = "",
        *,
        include_comments: bool = True,
    ) -> bool:
        """Translate article paragraphs (skipping code & formula blocks) and comment tree."""
        any_translated = False
        for para in article.paragraphs:
            if para.kind in {"code", "formula"}:
                continue
            if para.translated:
                any_translated = True
                continue
            tr = await self.translate_text(para.original, target_lang)
            if tr:
                para.translated = tr
                any_translated = True

        if include_comments and article.comments:
            budget = [_COMMENT_TRANSLATE_BUDGET]
            if await self._translate_comments_recursive(article.comments, target_lang, budget):
                any_translated = True
        return any_translated


__all__ = ["Translator", "is_already_target_language"]
