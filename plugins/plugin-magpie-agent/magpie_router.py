"""Magpie CLI output parsers, route spec normalization, and group cycle checks."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from typing import Any

try:
    from .magpie_common import DEFAULT_GROUP_ID, strip_plugin_log_lines
except ImportError:
    from magpie_common import DEFAULT_GROUP_ID, strip_plugin_log_lines  # type: ignore[no-redef]

SUPPORTED_ROUTER_ACTIONS = (
    "current_route",
    "gateway_status",
    "list_harnesses",
    "list_models",
    "list_groups",
    "list_quotas",
    "set_route",
)
VALID_ROUTING_STRATEGIES = frozenset(
    {"smart", "order", "rotate", "usage", "pace", "weight", "manual", "fast", "cheap", "drain", "expiring"}
)
VALID_STAYS_POLICIES = frozenset({"auto", "session", "turn", "off", "strict"})

_EFFORT_TOKEN_RE = r"(?:none|minimal|low|medium|high|xhigh|max|ultra)"
_EFFORTS_RE = re.compile(rf"^{_EFFORT_TOKEN_RE}(?:/{_EFFORT_TOKEN_RE})*$")
_VALID_EFFORT_TOKENS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_VALID_EFFORT_ARGS = _VALID_EFFORT_TOKENS | {"", "agent", "auto"}
_MODEL_HEAD_RE = re.compile(r"^[A-Za-z0-9._-]+/\S+$")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:@/#-]+$")
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RESERVED_HARNESS_IDS = frozenset(
    {
        "claude", "cc", "codex", "gemini", "opencode", "oc", "mimocode", "deveco",
        "pi", "goose", "cursor", "zed", "copilot", "crush", "aside", "antigravity",
        "antigravity-cli",
    }
)


def parse_harnesses(stdout: str) -> list[dict[str, Any]]:
    """Parse `magpie ls` harness lines."""
    harnesses: list[dict[str, Any]] = []
    for raw_line in strip_plugin_log_lines(stdout).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
        name, rest = parts[0], parts[1:]
        config_path = ""
        if rest and (rest[-1].startswith(("~/", "/", "./")) or rest[-1].endswith((".json", ".toml", ".yml", ".yaml"))):
            config_path = rest.pop()
        attrs: dict[str, str] = {}
        status = ""
        for segment in rest:
            for token in (t.strip() for t in segment.split("·") if t.strip()):
                matched = False
                for key, prefix in (("effort", "effort "), ("thinking", "thinking "), ("sign_in", "sign-in "), ("provider", "provider ")):
                    if token.startswith(prefix):
                        attrs[key] = token.removeprefix(prefix).strip()
                        matched = True
                        break
                if not matched and not status:
                    status = token
        harnesses.append({
            "name": name,
            "harness_id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "status": status,
            "model": status if "/" in status else "",
            "effort": attrs.get("effort", ""),
            "thinking": attrs.get("thinking", ""),
            "sign_in": attrs.get("sign_in", ""),
            "provider": attrs.get("provider", ""),
            "config_path": config_path,
            "configured": (status not in {"", "—", "-"}) or (attrs.get("provider", "") not in {"", "—", "-"}),
        })
    return harnesses


def parse_models(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse `magpie models` grouped output."""
    models: list[dict[str, Any]] = []
    groups: list[str] = []
    current_group = ""
    for raw_line in strip_plugin_log_lines(stdout).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
        head, tail = parts[0], parts[1:]
        if not _MODEL_HEAD_RE.match(head) or head.startswith(("http:", "https:")):
            current_group = line
            if current_group not in groups:
                groups.append(current_group)
            continue
        provider, _, slug = head.partition("/")
        efforts = tail.pop().split("/") if tail and _EFFORTS_RE.match(tail[-1]) else []
        models.append({
            "id": head,
            "provider": provider,
            "slug": slug,
            "name": " ".join(tail).strip() or slug,
            "group": current_group,
            "efforts": efforts,
            "is_group": provider in {"group", "auto"},
        })
    return models, groups


def parse_groups_list(stdout: str) -> list[dict[str, Any]]:
    """Parse `magpie groups` summary rows for cycle checks and inspection."""
    groups: list[dict[str, Any]] = []
    for raw_line in strip_plugin_log_lines(stdout).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cols = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
        ref_idx = next((i for i, col in enumerate(cols) if col.startswith(("group/", "auto/"))), -1)
        if ref_idx < 0:
            continue
        ref = cols[ref_idx]
        members: list[str] = []
        if len(cols) > ref_idx + 2:
            raw_members = cols[ref_idx + 2]
            sep = " → " if " → " in raw_members else " · "
            for part in raw_members.split(sep):
                cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", part.replace("●", "")).strip()
                if cleaned:
                    members.append(cleaned)
        groups.append({
            "ref": ref,
            "id": ref.split("/", 1)[1],
            "name": cols[0] if ref_idx > 0 else ref.split("/", 1)[1],
            "members": members,
        })
    return groups


def parse_group_detail(stdout: str, group_id: str, default_routing: str) -> dict[str, Any]:
    """Parse `magpie group <id>` detail output (header + columnar sections)."""
    routing, stays, members, fallback_members = default_routing, "turn", [], []
    explicit_routing, explicit_stays = False, False
    own_group = f"group/{group_id}"
    in_models = False
    for raw_line in strip_plugin_log_lines(stdout).splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        for match in re.finditer(r"\b(routing|stays)\s*[=:]\s*([A-Za-z0-9_-]+)", stripped):
            if match.group(1) == "routing":
                routing, explicit_routing = match.group(2), True
            else:
                stays, explicit_stays = match.group(2), True
        cols = [p.strip() for p in re.split(r"\s{2,}", stripped) if p.strip()]
        is_key_row = raw_line.startswith("  ") and not raw_line.startswith("    ") and bool(cols)
        if is_key_row:
            key = cols[0]
            if key == "routing" and len(cols) >= 2:
                in_models, routing, explicit_routing = False, cols[1].split()[0], True
                continue
            if key == "stays" and len(cols) >= 2:
                in_models, stays, explicit_stays = False, cols[1].split()[0], True
                continue
            if key == "models":
                in_models = True
                stripped = "  ".join(cols[1:])
            elif key in {"rules", "levels", "classifier", "context", "compact", "family", "note", "effort", "used by"}:
                in_models = False
                continue
        if in_models:
            m_num = re.match(r"^(?:\d+\s+)?(?:●\s*)?([A-Za-z0-9._-]+/[A-Za-z0-9._:@/+_#-]+)", stripped)
            if m_num and m_num.group(1) != own_group and m_num.group(1) not in members:
                members.append(m_num.group(1))
        elif len(cols) == 1:
            candidate = cols[0].lstrip("● ").strip()
            if _MEMBER_RE.match(candidate) and candidate != own_group:
                if not candidate.startswith(("http:", "https:")) and candidate not in fallback_members:
                    fallback_members.append(candidate)
    resolved = members or fallback_members
    primary = resolved[0] if resolved else ""
    return {
        "routing": routing,
        "stays": stays,
        "primary_member": primary,
        "harness_provider": primary.partition("/")[0] if "/" in primary else "",
        "fallbacks": resolved[1:],
        "members": resolved,
        "explicit_routing": explicit_routing,
        "explicit_stays": explicit_stays,
    }


def normalize_quotas(raw: Any) -> list[dict[str, Any]]:
    """Normalize Magpie gateway/CLI quota payload into canonical structure."""
    entries = raw.get("quotas", raw.get("data", [])) if isinstance(raw, Mapping) else raw
    if not isinstance(entries, list):
        raise ValueError("quotas payload must be a list")
    normalized: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        windows = [
            {
                "name": str(w.get("name", "")),
                "used_pct": w.get("used"),
                "remaining_pct": w.get("remaining"),
                "resets_at": str(w.get("resetsAt") or w.get("resets_at") or ""),
                "unlimited": bool(w.get("unlimited", False)),
                "display": str(w.get("display", "")),
                "amount": w.get("amount"),
                "limit": w.get("limit"),
                "unit": str(w.get("unit", "")),
            }
            for w in (item.get("windows") or []) if isinstance(w, Mapping)
        ]
        normalized.append({
            "provider": str(item.get("provider", "")),
            "name": str(item.get("name", "")),
            "user": str(item.get("user", "")),
            "plan": str(item.get("plan", "")),
            "kind": str(item.get("kind", "")),
            "balance": item.get("balance"),
            "error": str(item.get("error", "")),
            "as_of": str(item.get("asOf") or item.get("readAt") or ""),
            "last_served_at": str(item.get("lastServedAt") or ""),
            "last": bool(item.get("last", False)),
            "windows": windows,
        })
    return normalized


def validate_dedicated_group_id(raw_group: str | None, default_group: str) -> tuple[str, str | None]:
    """Validate and normalize group ID; restricts mutations to OmniDesk's dedicated group."""
    cleaned = str(raw_group or default_group).strip()
    if cleaned.startswith("group/"):
        cleaned = cleaned[len("group/"):]
    if not cleaned or ".." in cleaned or not _GROUP_ID_RE.match(cleaned):
        return "", f"非法的路由组 ID: {raw_group!r}"
    if cleaned.lower() in _RESERVED_HARNESS_IDS:
        return "", f"禁止修改终端 Harness 全局绑定: {cleaned!r}"
    expected = default_group.removeprefix("group/").strip() or DEFAULT_GROUP_ID
    if cleaned != expected:
        return "", f"set_route 仅允许写入 OmniDesk 专属组 group/{expected}，禁止改动 {cleaned!r}"
    return cleaned, None


def build_member_spec(
    model_or_spec: str,
    effort: str = "",
    fast: bool = False,
    account: str = "",
) -> tuple[str, str | None]:
    """Validate and normalize `<provider>/<model>[:effort][:fast][#account]` or `group/<id>`."""
    raw = str(model_or_spec or "").strip()
    if not raw:
        return "", "set_route 需要非空的 model_id"
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw) or "," in raw or "=" in raw or raw.startswith("-"):
        return "", f"模型标识包含非法字符: {raw!r}"
    eff_acct = str(account or "").strip()
    if eff_acct and (any(ch.isspace() for ch in eff_acct) or "," in eff_acct or "#" in eff_acct):
        return "", f"非法的 account 选择器: {eff_acct!r}"

    base = raw
    if "#" in base:
        base, embedded_acct = base.split("#", 1)
        if not embedded_acct:
            return "", "模型标识中的 #account 后缀不能为空"
        if not eff_acct:
            eff_acct = embedded_acct

    if base.startswith(("group/", "auto/")):
        if eff_acct or fast or (effort and effort not in {"auto", "agent"}):
            return "", "嵌套 group/ 或 auto/ 引用不支持 :effort、:fast 或 #account 后缀"
        if "/" not in base or not base.split("/", 1)[1]:
            return "", f"非法的路由组引用: {raw!r}"
        return base, None

    embedded_fast = False
    if base.endswith(":fast") and len(base) > 5:
        embedded_fast = True
        base = base[:-5]

    embedded_effort = ""
    if ":" in base:
        prefix, suffix = base.rsplit(":", 1)
        if suffix in _VALID_EFFORT_TOKENS:
            embedded_effort = suffix
            base = prefix

    eff_arg = str(effort or "").strip()
    if eff_arg not in _VALID_EFFORT_ARGS:
        return "", f"不支持的推理档位 effort={eff_arg!r}"
    final_effort = "" if eff_arg in {"auto", "agent"} else (eff_arg or embedded_effort)

    if not base or "/" not in base or base.startswith("/") or base.endswith("/"):
        return "", f"无效的 provider/model 标识: {raw!r}"
    provider, _, slug = base.partition("/")
    if not provider or not slug:
        return "", f"无效的 provider/model 标识: {raw!r}"

    out = base
    if final_effort:
        out += f":{final_effort}"
    if fast or embedded_fast:
        out += ":fast"
    if eff_acct:
        out += f"#{eff_acct}"
    return out, None


def detect_group_cycle(
    target_group_id: str,
    members: list[str],
    existing_groups: list[Mapping[str, Any]],
) -> str | None:
    """Detect direct/indirect group self-reference or nesting deeper than Magpie's limit (8)."""
    target_ref = f"group/{target_group_id}"
    auto_ref = f"auto/{target_group_id}"
    adj: dict[str, list[str]] = {}
    for g in existing_groups:
        if g.get("id") == target_group_id:
            continue
        children: list[str] = []
        for m in g.get("members") or ():
            clean = str(m).split("#", 1)[0]
            if clean.startswith(("group/", "auto/")):
                children.append(clean.split(":", 1)[0])
        if g.get("ref"):
            adj[str(g["ref"])] = children

    queue: deque[tuple[str, int, str]] = deque()
    visited: set[str] = set()
    for m in members:
        clean = m.split("#", 1)[0]
        if not clean.startswith(("group/", "auto/")):
            continue
        clean = clean.split(":", 1)[0]
        if clean in {target_ref, auto_ref}:
            return f"{target_ref} -> {clean}"
        if clean not in visited:
            visited.add(clean)
            queue.append((clean, 1, f"{target_ref} -> {clean}"))

    while queue:
        ref, depth, path = queue.popleft()
        if depth > 8:
            return f"路由组嵌套超过 Magpie 最大深度 (8): {path}"
        for nxt in adj.get(ref, ()):
            if nxt in {target_ref, auto_ref}:
                return f"{path} -> {nxt}"
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, depth + 1, f"{path} -> {nxt}"))
    return None


def parse_fallbacks(raw: Any) -> tuple[list[str], str | None]:
    """Normalize fallbacks list or CSV into validated member specs."""
    if raw is None:
        return [], None
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        items = [str(item).strip() for item in raw if isinstance(item, str) and item.strip()]
    else:
        return [], "fallbacks 必须为字符串数组或逗号分隔字符串"
    out: list[str] = []
    for item in items:
        spec, err = build_member_spec(item)
        if err:
            return [], f"fallback 参数无效: {err}"
        if spec not in out:
            out.append(spec)
    return out, None
