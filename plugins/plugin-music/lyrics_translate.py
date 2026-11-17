"""Batch lyric translation through the optional `llm.translate.v1` capability."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omnidesk_sdk import PluginContext

_BATCH_LINES = 40


def _translation_text(result: Any) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    for key in ("translation", "reply", "translated"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def translate_lyric_lines(
    ctx: PluginContext,
    texts: Sequence[str],
    *,
    title: str = "",
) -> tuple[list[str] | None, str]:
    """Translate non-empty lyric lines while preserving the one-line-per-cue layout."""
    cleaned = [text.strip() for text in texts if text and text.strip()]
    if not cleaned:
        return [], ""
    if not ctx.capabilities.has("llm.translate.v1"):
        return None, "llm.translate.v1_unavailable"

    translated: list[str] = []
    for start in range(0, len(cleaned), _BATCH_LINES):
        chunk = cleaned[start : start + _BATCH_LINES]
        request = {
            "action": "translate",
            "domain": "media",
            "context": (
                f"歌词《{title}》。逐行翻译为简体中文，严格保持输入行数、顺序和换行，"
                "不要合并或增加行，不要输出解释。"
            ),
            "source_lang": "auto",
            "target_lang": "zh-CN",
            "text": "\n".join(chunk),
            "preserve_formatting": True,
            "temperature": 0.1,
        }
        result = await ctx.capabilities.call("llm.translate.v1", request)
        if not result.ok:
            code = str((result.data or {}).get("error_code") or "error")
            return None, f"llm.translate.v1_failed:{code}"
        raw = _translation_text(result)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(lines) != len(chunk):
            return None, "llm.translate.v1_line_count_mismatch"
        translated.extend(lines)
    return translated, ""
