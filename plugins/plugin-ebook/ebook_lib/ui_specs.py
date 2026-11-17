"""Declarative JSON Schema builders for BookWorkbench tab and F8 Companion Dock."""

from __future__ import annotations

from typing import Any


def build_workspace_schema(*, roots_count: int, due_count: int, target_language: str) -> dict[str, Any]:
    return {
        "type": "Section",
        "id": "ebook-studio-root",
        "title": "Ebook & SM-2 Vocabulary Studio",
        "children": [
            {
                "type": "KeyValue",
                "id": "ebook-studio-status",
                "items": [
                    {"key": "library_roots", "label": "已配置书库目录", "value": str(roots_count)},
                    {"key": "due_vocab", "label": "待复习生词", "value": str(due_count)},
                    {"key": "target_language", "label": "目标语言", "value": target_language},
                ],
            }
        ],
    }


def build_dock_schema() -> dict[str, Any]:
    return {
        "type": "Section",
        "id": "ebook-dock-root",
        "title": "划词词源与长难句精读",
        "children": [
            {
                "type": "Form",
                "id": "ebook-dock-form",
                "fields": [
                    {"name": "word", "label": "词元", "type": "string", "default": ""},
                    {"name": "sentence", "label": "长难句", "type": "string", "default": ""},
                ],
            },
            {
                "type": "ActionRow",
                "id": "ebook-dock-actions",
                "actions": [
                    {"id": "lookup_term", "label": "查词源", "variant": "primary"},
                    {"id": "explain_sentence", "label": "拆解长难句", "variant": "secondary"},
                ],
            },
        ],
    }
