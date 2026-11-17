"""Idempotent v1 -> v2 plugin configuration import.

v1 and v2 share ``~/.config/omnidesk``; v1 stored a flat mix of plain keys and
secrets, while v2 uses ``ConfigStore`` plus an isolated ``SecretVault``. This
module only fills v2 keys that are missing, never overwrites an existing v2
value, and keeps all secret values out of reports and logs.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ConfigStore, ensure_private_dir
from .paths import OmnideskPaths
from .vault import SecretVault

# Renamed plain keys. Values are copied verbatim unless a special transform below
# handles the plugin.
LEGACY_KEY_MAP: dict[str, dict[str, str]] = {
    "plugin-annas-archive": {
        "download_dir": "download_root",
        "mirror": "base_url",
    },
    "plugin-arr-imdb": {
        "radarr_default_root_folder": "root_folder_path",
    },
    "plugin-asr-funasr": {
        "runtime_bin": "funasr_bin",
        "backend": "device",
    },
    "plugin-ebook": {
        "library_dirs": "library_roots",
        "auto_sync_nas": "sync_enabled",
    },
    "plugin-magpie-agent": {"route_group_id": "default_group"},
    "plugin-pdf-babeldoc": {
        "library_dirs": "pdf_library_roots",
        "lang_out": "target_language",
    },
    "plugin-sponsorblock": {
        "youtube_base_url": "youtube_api_base",
        "bilibili_base_url": "bilibili_api_base",
    },
    "plugin-sub-hunter": {"max_archive_mb": "max_download_mb"},
    "plugin-youtube": {
        "subscriptions_channels": "channel_ids",
        "bilingual_target_lang": "target_lang",
    },
    "plugin-zlib": {
        "domain": "base_url",
        "download_dir": "download_root",
    },
}

_BILIBILI_QUALITY = {
    "0": "auto",
    "127": "4k",
    "126": "4k",
    "125": "1080p+",
    "120": "4k",
    "116": "1080p60",
    "80": "1080p",
    "64": "720p",
}
_BILIBILI_CODEC = {"12": "hevc", "7": "avc", "13": "av1"}
_YOUTUBE_QUALITY = {
    "0": "best",
    "2160": "2160p",
    "1440": "1440p",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
}


def _is_missing(cfg: Mapping[str, Any], key: str) -> bool:
    return key not in cfg or cfg.get(key) is None or cfg.get(key) == ""


def _url_base(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text
    return f"{parts.scheme}://{parts.netloc}"


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _timeout_seconds(raw: Any) -> int | float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    seconds = value / 1000.0
    return int(seconds) if seconds.is_integer() else seconds


def _candidate_babeldoc_bin() -> str:
    for candidate in (
        Path("/home/zhu/Projects/qwen-translator/runtime/pdf-venv/bin/babeldoc"),
        Path("/home/zhu/.local/bin/babeldoc"),
    ):
        if candidate.is_file():
            return str(candidate)
    return ""


def _compose_bilibili_cookie(raw_cfg: Mapping[str, Any], vault: SecretVault) -> str:
    pairs: list[tuple[str, str]] = []
    for cookie_name, secret_name in (
        ("SESSDATA", "web_sessdata"),
        ("bili_jct", "web_bili_jct"),
        ("DedeUserID", "web_dede_userid"),
        ("buvid3", "web_buvid3"),
    ):
        value = vault.get("plugin-bilibili", secret_name) or ""
        if not value and cookie_name == "DedeUserID":
            value = str(raw_cfg.get("web_dede_userid") or "")
        value = value.strip()
        if value:
            pairs.append((cookie_name, value))
    return "; ".join(f"{name}={value}" for name, value in pairs)


def _plan_plain(
    plugin_id: str,
    raw_cfg: dict[str, Any],
    *,
    paths: OmnideskPaths | None,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Return the merged config, ``{new_key: source}``, and derived key names."""
    cfg = dict(raw_cfg)
    sources: dict[str, str] = {}
    derived: list[str] = []

    for old_key, new_key in LEGACY_KEY_MAP.get(plugin_id, {}).items():
        if old_key in cfg and _is_missing(cfg, new_key):
            cfg[new_key] = cfg[old_key]
            sources[new_key] = old_key

    if plugin_id == "plugin-ai-curator":
        v1_keys = ("auto_pull_remote", "auto_rehydrate_on_load")
        if any(key in cfg for key in v1_keys) and _is_missing(cfg, "sync_remote"):
            cfg["sync_remote"] = any(_truthy(cfg.get(key)) for key in v1_keys)
            derived.append("sync_remote")

    if plugin_id == "plugin-arr-imdb" and _is_missing(cfg, "quality_profile"):
        try:
            profile_id = int(cfg.get("radarr_default_profile_id") or 0)
        except (TypeError, ValueError):
            profile_id = 0
        if profile_id > 0:
            cfg["quality_profile"] = str(profile_id)
            derived.append("quality_profile")

    if plugin_id == "plugin-asr-funasr" and _is_missing(cfg, "model_dir"):
        model_path = str(cfg.get("model_path") or "").strip()
        if model_path:
            cfg["model_dir"] = str(Path(model_path).expanduser().parent)
            derived.append("model_dir")

    if plugin_id in {"plugin-annas-archive", "plugin-zlib"} and "base_url" in cfg:
        cfg["base_url"] = _url_base(cfg["base_url"])

    if plugin_id == "plugin-bilibili":
        if _is_missing(cfg, "quality"):
            quality = _BILIBILI_QUALITY.get(str(cfg.get("default_quality") or "").strip())
            if quality:
                cfg["quality"] = quality
                derived.append("quality")
        codec = _BILIBILI_CODEC.get(str(cfg.get("preferred_codec") or "").strip())
        if codec:
            cfg["preferred_codec"] = codec
            derived.append("preferred_codec")

    if plugin_id == "plugin-pdf-babeldoc":
        if _is_missing(cfg, "babeldoc_bin"):
            babeldoc_bin = _candidate_babeldoc_bin()
            if babeldoc_bin:
                cfg["babeldoc_bin"] = babeldoc_bin
                derived.append("babeldoc_bin")
        if _is_missing(cfg, "output_root") and paths is not None:
            cfg["output_root"] = str(paths.data_dir / "plugin-data" / plugin_id / "output")
            derived.append("output_root")

    if plugin_id == "plugin-sponsorblock":
        for base_key in ("youtube_api_base", "bilibili_api_base"):
            if base_key in cfg:
                cfg[base_key] = _url_base(cfg[base_key])
        if _is_missing(cfg, "enabled"):
            flags = [cfg.get("enable_youtube"), cfg.get("enable_bilibili")]
            if any(flag is not None for flag in flags):
                cfg["enabled"] = any(_truthy(flag) for flag in flags)
                derived.append("enabled")
        if _is_missing(cfg, "request_timeout_sec") and "timeout_ms" in cfg:
            timeout = _timeout_seconds(cfg.get("timeout_ms"))
            if timeout is not None:
                cfg["request_timeout_sec"] = timeout
                derived.append("request_timeout_sec")

    if plugin_id == "plugin-youtube":
        if _is_missing(cfg, "quality"):
            quality = _YOUTUBE_QUALITY.get(str(cfg.get("default_quality") or "").strip())
            if quality:
                cfg["quality"] = quality
                derived.append("quality")

    return cfg, sources, derived


def _plan_secrets(
    plugin_id: str,
    raw_cfg: Mapping[str, Any],
    vault: SecretVault,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return ``(new_secrets, renamed_existing_secrets, plain_keys_to_remove)``."""
    new_secrets: dict[str, str] = {}
    renamed: dict[str, str] = {}
    remove_plain: set[str] = set()

    if plugin_id == "plugin-bilibili" and not vault.has(plugin_id, "cookie_header"):
        cookie = _compose_bilibili_cookie(raw_cfg, vault)
        if cookie:
            new_secrets["cookie_header"] = cookie

    if plugin_id == "plugin-zlib":
        remix_userid = str(raw_cfg.get("remix_userid") or "").strip()
        if remix_userid and not vault.has(plugin_id, "remix_userid"):
            new_secrets["remix_userid"] = remix_userid
            remove_plain.add("remix_userid")
        remix_userkey = vault.get(plugin_id, "remix_userkey")
        if remix_userkey and not vault.has(plugin_id, "api_key"):
            renamed["api_key"] = remix_userkey

    for old_key, new_key in (
        ("youtube_api_key", "api_key"),
        ("media_user_token", "apple_music_user_token"),
    ):
        value = vault.get(plugin_id, old_key)
        if value and not vault.has(plugin_id, new_key):
            renamed[new_key] = value

    return new_secrets, renamed, remove_plain


def _backup_config(config_dir: Path, backup_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_root / f"v1-import-{stamp}"
    ensure_private_dir(target)
    for source in (config_dir / "plugins", config_dir / "vault.json"):
        if source.is_dir():
            shutil.copytree(source, target / source.name)
        elif source.is_file():
            shutil.copy2(source, target / source.name)
    return target


def import_v1_config(
    store: ConfigStore,
    vault: SecretVault,
    *,
    paths: OmnideskPaths | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Import v1 keys/secrets into v2 storage and return a secret-free report."""
    plugin_ids = sorted(set(store.list_plugins()) | set(vault.list_plugins()))
    plans: list[dict[str, Any]] = []

    for plugin_id in plugin_ids:
        raw_cfg = store.read(plugin_id, strip_known_secrets=False)
        merged_cfg, sources, derived = _plan_plain(plugin_id, raw_cfg, paths=paths)
        new_secrets, renamed, remove_plain = _plan_secrets(plugin_id, raw_cfg, vault)
        for key in remove_plain:
            merged_cfg.pop(key, None)

        plain_changed = merged_cfg != raw_cfg
        secrets_changed = bool(new_secrets or renamed)
        if not plain_changed and not secrets_changed:
            continue

        plans.append(
            {
                "plugin_id": plugin_id,
                "config": merged_cfg,
                "sources": sources,
                "derived": derived,
                "new_secrets": new_secrets,
                "renamed_secrets": renamed,
                "removed_plain": sorted(remove_plain),
            }
        )

    backup_path: Path | None = None
    if plans and not dry_run:
        if backup and paths is not None:
            backup_path = _backup_config(paths.config_dir, paths.data_dir / "backups")
        for plan in plans:
            plugin_id = plan["plugin_id"]
            if plan["new_secrets"]:
                vault.set_many(plugin_id, plan["new_secrets"])
            if plan["renamed_secrets"]:
                vault.set_many(plugin_id, plan["renamed_secrets"])
            store.write(plugin_id, plan["config"], strip_known_secrets=True)
            output_root = plan["config"].get("output_root")
            if plugin_id == "plugin-pdf-babeldoc" and output_root:
                ensure_private_dir(Path(str(output_root)))

    report_plugins = [
        {
            "plugin_id": plan["plugin_id"],
            "config_keys": {
                **plan["sources"],
                **{key: "derived" for key in plan["derived"]},
            },
            "secret_keys": sorted(
                set(plan["new_secrets"]) | set(plan["renamed_secrets"])
            ),
            "removed_plain_keys": plan["removed_plain"],
        }
        for plan in plans
    ]
    return {
        "ok": True,
        "dry_run": dry_run,
        "backup_dir": str(backup_path) if backup_path else None,
        "plugins": report_plugins,
        "changed_count": len(report_plugins),
    }


__all__ = ["LEGACY_KEY_MAP", "import_v1_config"]
