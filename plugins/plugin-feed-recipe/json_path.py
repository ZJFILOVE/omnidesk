"""JSON dot/bracket path lookup, pipe fallbacks, and {{var|filter}} template rendering."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any


def _split_pipe_top_level(expr: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    brace_depth = 0
    i = 0
    n = len(expr)
    while i < n:
        if i + 1 < n and expr[i] == "{" and expr[i + 1] == "{":
            brace_depth += 2
            cur.append("{{")
            i += 2
            continue
        if i + 1 < n and expr[i] == "}" and expr[i + 1] == "}":
            brace_depth = max(0, brace_depth - 2)
            cur.append("}}")
            i += 2
            continue
        if expr[i] == "|" and brace_depth == 0:
            parts.append("".join(cur).strip())
            cur.clear()
            i += 1
            continue
        cur.append(expr[i])
        i += 1
    if cur or parts:
        parts.append("".join(cur).strip())
    return parts


def _contains_template(s: str) -> bool:
    return "{{" in s and "}}" in s


def lookup_json_path(root: Any, path: str) -> Any:
    """Navigate dot/bracket JSON paths like 'data.children[0].data.title' or 'hits'."""
    p = (path or "").strip()
    if not p or p == "." or p == "$":
        return root
    cur = root
    i = 2 if p.startswith("$.") else 0
    n = len(p)

    while i < n and cur is not None:
        if p[i] == ".":
            i += 1
            continue
        if p[i] == "[":
            close = p.find("]", i + 1)
            if close == -1:
                return None
            idx_str = p[i + 1 : close].strip()
            if len(idx_str) >= 2 and (
                (idx_str[0] == "'" and idx_str[-1] == "'") or (idx_str[0] == '"' and idx_str[-1] == '"')
            ):
                key = idx_str[1:-1]
                if not isinstance(cur, Mapping) or key not in cur:
                    return None
                cur = cur[key]
            else:
                if not isinstance(cur, list):
                    return None
                try:
                    idx = int(idx_str)
                except ValueError:
                    return None
                real_idx = len(cur) + idx if idx < 0 else idx
                if real_idx < 0 or real_idx >= len(cur):
                    return None
                cur = cur[real_idx]
            i = close + 1
            continue

        dot = p.find(".", i)
        brk = p.find("[", i)
        candidates = [pos for pos in (dot, brk) if pos != -1]
        next_pos = min(candidates) if candidates else n
        key = p[i:next_pos]
        if not isinstance(cur, Mapping) or key not in cur:
            return None
        cur = cur[key]
        i = next_pos
    return cur


def json_to_str(val: Any) -> str:
    """Convert scalar JSON value to string matching v1 jsonToString semantics."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return val
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return f"{val:.15g}"
    return ""


def json_to_int(val: Any, fallback: int = 0) -> int:
    """Convert JSON number/numeric string prefix to int matching v1 jsonToInt."""
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return fallback
        digits = []
        for idx, ch in enumerate(s):
            if ch.isdigit() or (idx == 0 and ch in "+-"):
                digits.append(ch)
            else:
                break
        num_str = "".join(digits)
        if not num_str or num_str in {"+", "-"}:
            return fallback
        try:
            return int(num_str)
        except ValueError:
            return fallback
    return fallback


def json_to_str_list(val: Any) -> list[str]:
    """Convert JSON array or string into a list of non-empty strings."""
    if isinstance(val, list):
        out: list[str] = []
        for el in val:
            s = json_to_str(el).strip()
            if s:
                out.append(s)
        return out
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    return []


def render_template(tmpl: str, variables: Any) -> str:
    """Expand {{var}}, {{nested.var}}, {{var|url}}, {{var|trim}}, {{var|lower}}."""
    if not tmpl or "{{" not in tmpl:
        return tmpl
    out: list[str] = []
    pos = 0
    n = len(tmpl)
    while pos < n:
        open_idx = tmpl.find("{{", pos)
        if open_idx == -1:
            out.append(tmpl[pos:])
            break
        out.append(tmpl[pos:open_idx])
        close_idx = tmpl.find("}}", open_idx + 2)
        if close_idx == -1:
            out.append(tmpl[open_idx:])
            break

        token = tmpl[open_idx + 2 : close_idx].strip()
        if "|" in token:
            key, flt = token.split("|", 1)
            key = key.strip()
            flt = flt.strip().lower()
        else:
            key, flt = token, ""

        found = lookup_json_path(variables, key)
        val = json_to_str(found) if found is not None else ""
        if flt == "url":
            val = urllib.parse.quote(val, safe="~-._")
        elif flt == "trim":
            val = val.strip()
        elif flt == "lower":
            val = val.lower()
        out.append(val)
        pos = close_idx + 2
    return "".join(out)


def resolve_json_expr(item: Any, expr: str, root_ctx: Any = None) -> Any:
    """Evaluate pipe-separated fallback expressions like 'url|https://.../item?id={{objectID}}'."""
    for alt in _split_pipe_top_level(expr):
        if not alt:
            continue
        if _contains_template(alt):
            rendered = render_template(alt, item)
            if rendered:
                return rendered
            if root_ctx is not None:
                rendered_root = render_template(alt, root_ctx)
                if rendered_root:
                    return rendered_root
            continue

        hit = lookup_json_path(item, alt)
        if hit is not None:
            if isinstance(hit, str) and not hit.strip():
                pass
            else:
                return hit
        if root_ctx is not None:
            hit_root = lookup_json_path(root_ctx, alt)
            if hit_root is not None:
                if isinstance(hit_root, str) and not hit_root.strip():
                    continue
                return hit_root
    return None


__all__ = [
    "json_to_int",
    "json_to_str",
    "json_to_str_list",
    "lookup_json_path",
    "render_template",
    "resolve_json_expr",
]
