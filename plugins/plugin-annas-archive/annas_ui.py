"""Declarative BookWorkbench UI schema and capability handoff helper for plugin-annas-archive."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

from annas_parser import clean_text, normalize_format


def build_annas_ui_schema(
    *,
    endpoint: str,
    download_root: str,
    last_results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return declarative WorkspaceTab schema for Anna's Archive search."""
    return {
        "type": "Section",
        "id": "annas-search-root",
        "title": "Anna’s Archive · 文件索引检索",
        "children": [
            {
                "type": "KeyValue",
                "id": "annas-status",
                "items": [
                    {"key": "endpoint", "label": "当前站点", "value": endpoint or "(未配置)"},
                    {"key": "download_root", "label": "下载目录", "value": download_root or "(未配置)"},
                ],
            },
            {
                "type": "Form",
                "id": "annas-search-form",
                "fields": [
                    {"name": "query", "label": "书名 / 作者 / MD5 / ISBN", "type": "string", "default": ""},
                    {"name": "format", "label": "文件格式（如 pdf / epub）", "type": "string", "default": ""},
                    {"name": "lang", "label": "语言代码（如 en / zh）", "type": "string", "default": ""},
                    {"name": "sort", "label": "排序（newest / oldest / largest / smallest）", "type": "string", "default": ""},
                ],
            },
            {
                "type": "ActionRow",
                "id": "annas-actions",
                "actions": [{"id": "search", "label": "检索图书", "variant": "primary"}, {"id": "open_browser", "label": "在 Chrome 打开 / 完成验证"}],
            },
            {
                "type": "List",
                "id": "annas-results",
                "title": "最近检索结果",
                "items": [
                    {
                        "id": str(item.get("item_id", "")),
                        "title": str(item.get("title", "")),
                        "subtitle": f"{item.get('author', '')} · {item.get('format', '')} · {item.get('size', '')}",
                    }
                    for item in last_results
                ],
            },
        ],
    }


async def forward_book_handoff(
    ctx: PluginContext | None,
    payload: Mapping[str, Any],
    cap_candidates: tuple[str, ...],
    label: str,
    *,
    default_action: str,
    download_records: Mapping[str, Mapping[str, Any]],
    detail_cache: Mapping[str, Mapping[str, Any]],
) -> CapabilityResult:
    """Forward a book item/file via `ctx.capabilities.call`, failing cleanly if capability is absent."""
    if ctx is None:
        return CapabilityResult.fail(
            f"{label} 能力不可用",
            error_code="capability_unavailable",
            missing_capability=cap_candidates[0],
        )
    matched_cap = next((cap for cap in cap_candidates if ctx.capabilities.has(cap)), None)
    if matched_cap is None:
        return CapabilityResult.fail(
            f"未检测到 {cap_candidates[0]} 能力，请先启用对应插件",
            error_code="capability_unavailable",
            missing_capability=cap_candidates[0],
        )
    item_id = clean_text(payload.get("item_id") or payload.get("id") or payload.get("md5"))
    raw_path = clean_text(payload.get("file_path") or payload.get("local_path") or payload.get("path"))
    if raw_path and ("\x00" in raw_path or ".." in Path(raw_path).parts):
        return CapabilityResult.fail("file_path 包含非法路径穿越字符", error_code="path_safety")
    if not raw_path and item_id in download_records:
        raw_path = str(download_records[item_id].get("file_path") or "")
    if not raw_path and not item_id:
        return CapabilityResult.fail("转发需要提供 file_path 或已下载的 item_id", error_code="invalid_argument")

    forward_req: dict[str, Any] = {
        "action": clean_text(payload.get("downstream_action")) or default_action,
        "source_plugin": "plugin-annas-archive",
        "item_id": item_id,
        "book_id": item_id,
        "file_path": raw_path,
        "format": normalize_format(payload.get("format")) or str(download_records.get(item_id, {}).get("format") or ""),
        "title": clean_text(payload.get("title") or detail_cache.get(item_id, {}).get("title")),
    }
    res = await ctx.capabilities.call(matched_cap, forward_req)
    if not res.ok:
        return CapabilityResult.fail(
            res.error or f"{label} 转发失败",
            data=res.data,
            error_code=str(res.data.get("error_code") or "downstream_failed"),
        )
    return CapabilityResult.ok(
        {
            "forwarded_capability": matched_cap,
            "provider_plugin_id": res.provider_plugin_id,
            "downstream": res.data,
            **forward_req,
        }
    )
