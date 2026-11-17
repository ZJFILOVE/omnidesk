"""Shared Markdown, YAML frontmatter, AUTO-marker, and link extraction helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_CIRCLED_MAP = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9}


def parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Split optional YAML frontmatter (`--- ... ---`) into a flat dict and body."""
    normalized = raw.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end_idx = normalized.find("\n---", 4)
    if end_idx == -1:
        return {}, normalized
    fm_block = normalized[4:end_idx]
    after_pos = normalized.find("\n", end_idx + 1)
    body = normalized[after_pos + 1 :] if after_pos != -1 else ""
    kv: dict[str, str] = {}
    cur_list_key = ""
    cur_items: list[str] = []
    for raw_line in fm_block.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and cur_list_key:
            cur_items.append(stripped[2:].strip().strip('"').strip("'"))
            kv[cur_list_key] = ", ".join(cur_items)
            continue
        if ":" in raw_line and not raw_line.startswith((" ", "\t")):
            key, _, val = raw_line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            cur_list_key = ""
            cur_items = []
            if not val:
                cur_list_key = key
            else:
                kv[key] = val
    return kv, body


def strip_auto_comments(body: str) -> str:
    """Remove `<!-- AUTO:START -->` and `<!-- AUTO:END -->` markers from markdown body."""
    return body.replace("<!-- AUTO:START -->", "").replace("<!-- AUTO:END -->", "")


def extract_user_notes_after_auto_end(raw: str) -> str:
    """Extract handwritten user notes preserved after `<!-- AUTO:END -->` or under `## Notes`."""
    normalized = raw.replace("\r\n", "\n")
    marker = "<!-- AUTO:END -->"
    if marker in normalized:
        trailing = normalized.split(marker, 1)[1].strip()
        if not trailing:
            return ""
        return _strip_empty_notes_heading(trailing)
    for hdr in ("\n## My Notes", "\n## Notes", "\n### Notes"):
        pos = normalized.find(hdr)
        if pos != -1:
            trailing = normalized[pos + 1 :].strip()
            return _strip_empty_notes_heading(trailing)
    return ""


def _strip_empty_notes_heading(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip().lower() in {"## notes", "## my notes", "### notes"}:
        body = "\n".join(lines[1:]).strip()
        return body
    return text.strip()


def match_bold_field(line: str, keys: Sequence[str]) -> str | None:
    """Match markdown `- **Key:** val` / `**Key：** val` / `**Key**：val` variants."""
    t = line.strip()
    if t.startswith("- "):
        t = t[2:].strip()
    for key in keys:
        for pat in (f"**{key}:**", f"**{key}**:", f"**{key}：**", f"**{key}**："):
            if t.startswith(pat):
                val = t[len(pat) :].strip()
                if len(val) >= 2 and val.startswith("`") and val.endswith("`"):
                    val = val[1:-1]
                return val
        for pat in (f"**{key}：", f"**{key}:"):
            if t.startswith(pat):
                val = t[len(pat) :].strip()
                if val.endswith("**"):
                    val = val[:-2].strip()
                return val
    return None


def parse_numbered_heading(line: str) -> tuple[int, str] | None:
    """Parse `## 1. Title` or `## 1 Title` into `(rank, title)`."""
    t = line.strip()
    if not t.startswith("## "):
        return None
    rest = t[3:].strip()
    m = re.match(r"^(\d+)(?:\.|\s+)\s*(.+)$", rest)
    if not m:
        return None
    return int(m.group(1)), m.group(2).strip()


def parse_journal_heading(line: str) -> tuple[int, str] | None:
    """Parse `## ① Title` (or `## 1. Title`) into `(rank, title)`."""
    t = line.strip()
    if not t.startswith("## "):
        return None
    rest = t[3:].strip()
    for sym, num in _CIRCLED_MAP.items():
        if rest.startswith(sym):
            after = rest[len(sym) :].strip()
            if "｜" in after:
                prefix, _, candidate = after.partition("｜")
                if len(prefix) < 30 and candidate.strip():
                    after = candidate.strip()
            return num, after
    return parse_numbered_heading(line)


def extract_urls(text: str) -> list[str]:
    """Extract clean HTTP/HTTPS URLs from markdown/text."""
    urls: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        cleaned = raw.rstrip(".,;)")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    return urls


def extract_markdown_link_url(text: str, label_contains: str) -> str:
    """Return URL of first markdown link whose label contains `label_contains`."""
    needle = label_contains.lower()
    for label, url in _MD_LINK_RE.findall(text or ""):
        if needle in label.lower():
            return url.strip()
    return ""


def strip_markdown_links(text: str) -> str:
    """Replace `[label](url)` with `label` for clean readable summaries."""
    return _MD_LINK_RE.sub(r"\1", text or "")
