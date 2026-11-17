"""Ordered translation batches with bounded concurrency and shared backoff.

Adapted from qwen-translator/translation_pool.py: independent calls, shared
rate-limit cooldown, and cancellation of pending work on a terminal failure.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping

from omnidesk_sdk import CapabilityResult


class TranslationFailed(Exception):
    def __init__(self, result):
        self.result = result


async def translate_with_pool(req, *, translate, normalize):
    is_batch, segments, error = normalize(req)
    if error:
        return CapabilityResult.fail(error, error_code="invalid_argument")
    concurrency = req.get("concurrency", 4)
    batch_size = req.get("batch_size", 4)
    for value, upper, label in ((concurrency, 8, "并发数"), (batch_size, 32, "每批段数")):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            return CapabilityResult.fail(f"{label}须为 1–{upper} 的整数", error_code="invalid_argument")
    models = req.get("models", [])
    if not isinstance(models, list) or len(models) > 16 or any(not isinstance(m, str) or not m.strip() or len(m) > 200 for m in models):
        return CapabilityResult.fail("翻译模型须为最多 16 个非空模型 ID", error_code="invalid_argument")
    if len(segments) > 4096:
        return CapabilityResult.fail("一次最多翻译 4096 段，请分段提交", error_code="invalid_argument")

    slots = asyncio.Semaphore(concurrency)
    blocked_until = 0.0
    terminal_failure = None

    async def run(start, batch_index):
        nonlocal blocked_until, terminal_failure
        async with slots:
            if terminal_failure is not None:
                raise TranslationFailed(terminal_failure)
            payload = {k: v for k, v in req.items() if k not in {"text", "texts", "segments", "items", "models", "concurrency", "batch_size"}}
            chunk = [dict(seg) for seg in segments[start:start + batch_size]]
            # Preserve context across batch boundaries without moving translations.
            if start and not chunk[0].get("context_before"):
                chunk[0]["context_before"] = segments[start - 1]["text"]
            end = start + len(chunk)
            if end < len(segments) and not chunk[-1].get("context_after"):
                chunk[-1]["context_after"] = segments[end]["text"]
            if is_batch:
                payload["segments"] = chunk
            else:
                payload["text"] = segments[0]["text"]
            if models and not req.get("model") and not req.get("route_override"):
                payload["model"] = models[batch_index % len(models)].strip()
            for attempt in range(3):
                loop = asyncio.get_running_loop()
                while blocked_until > loop.time():
                    await asyncio.sleep(blocked_until - loop.time())
                result = await translate(payload)
                if result.ok:
                    return result
                code = (result.data or {}).get("error_code")
                if code != "rate_limited" or attempt == 2:
                    terminal_failure = result
                    raise TranslationFailed(result)
                blocked_until = max(blocked_until, loop.time() + 2 ** attempt)

    tasks = [asyncio.create_task(run(start, i)) for i, start in enumerate(range(0, len(segments), batch_size))]
    try:
        completed = await asyncio.gather(*tasks)
    except TranslationFailed as exc:
        return exc.result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if not is_batch:
        return completed[0]
    results, served_models, glossary = [], [], []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for result in completed:
        data = result.data
        model = str(data.get("served_model") or data.get("model") or "")
        if model and model not in served_models:
            served_models.append(model)
        for item in data.get("results", []):
            index = len(results)
            results.append({**item, "index": index, "id": segments[index]["id"], "served_model": model})
        for item in data.get("glossary_applied", []):
            if item not in glossary:
                glossary.append(item)
        if isinstance(data.get("usage"), Mapping):
            for key in usage:
                value = data["usage"].get(key, 0)
                if isinstance(value, int):
                    usage[key] += value
    if len(results) != len(segments):
        return CapabilityResult.fail("翻译返回段数不匹配，请重试", error_code="parse_error")
    return CapabilityResult.ok({
        **completed[0].data, "results": results, "translations": [r["translation"] for r in results],
        "count": len(results), "usage": usage, "served_models": served_models,
        "served_model": served_models[0] if len(served_models) == 1 else "",
        "model": served_models[0] if len(served_models) == 1 else "",
        "glossary_applied": glossary, "concurrency": concurrency, "batch_size": batch_size,
    })
