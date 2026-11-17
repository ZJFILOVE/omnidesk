"""Declarative Bilibili workspace tab schema."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plugin import BilibiliPlugin

_QUALITIES = ["auto", "4k", "1080p60", "1080p+", "1080p", "720p", "480p", "360p"]
_CODECS = [
    {"value": "hevc", "label": "HEVC (推荐)"},
    {"value": "avc", "label": "AVC / H.264"},
    {"value": "av1", "label": "AV1"},
]


def build_ui_schema(plugin: BilibiliPlugin) -> dict[str, Any]:
    """Build a live schema with auth state, QR image, import form, and quality controls."""
    ctx = plugin.ctx
    cfg = plugin._cfg()
    web_configured = bool(ctx and ctx.get_secret("cookie_header"))
    android_token = bool(ctx and ctx.get_secret("android_access_token"))
    android_mid = str(ctx.get_secret("android_mid") or "") if ctx else ""
    expires_at = str(ctx.get_secret("android_expires_at") or "") if ctx else ""
    qr = plugin._last_qr if isinstance(plugin._last_qr, dict) else {}
    children: list[dict[str, Any]] = [
        {
            "type": "Markdown",
            "id": "bilibili-intro",
            "content": (
                "Web 扫码用于推荐/动态/历史；**TV/Android 扫码**或导入 Wiliwili "
                "会话用于 `PlayViewUnite` 高画质兜底。播放仍在桌面 libmpv。"
            ),
        },
        {
            "type": "KeyValue",
            "id": "bilibili-status",
            "items": [
                {"key": "web_cookie", "label": "Web 登录", "value": "已配置" if web_configured else "未配置"},
                {
                    "key": "android_token",
                    "label": "Android/TV 取流",
                    "value": "已授权" if android_token else "未授权",
                },
                {"key": "android_mid", "label": "Android UID", "value": android_mid or "(none)"},
                {"key": "android_expires_at", "label": "Token 过期时间", "value": expires_at or "(unknown)"},
                {"key": "quality", "label": "默认画质", "value": plugin._default_quality_label()},
                {"key": "unlock_hd", "label": "高清兜底", "value": "开启" if _bool(cfg.get("unlock_hd"), True) else "关闭"},
                {"key": "preferred_codec", "label": "优先编码", "value": str(cfg.get("preferred_codec") or "hevc")},
            ],
        },
        {
            "type": "Form",
            "id": "bilibili-settings-form",
            "fields": [
                {
                    "name": "quality",
                    "label": "默认播放画质",
                    "type": "select",
                    "default": plugin._default_quality_label(),
                    "options": _QUALITIES,
                },
                {
                    "name": "preferred_codec",
                    "label": "优先视频编码",
                    "type": "select",
                    "default": str(cfg.get("preferred_codec") or "hevc"),
                    "options": _CODECS,
                },
                {
                    "name": "unlock_hd",
                    "label": "Web 画质不足时尝试 Android/TV 取流",
                    "type": "bool",
                    "default": _bool(cfg.get("unlock_hd"), True),
                },
            ],
        },
        {
            "type": "ActionRow",
            "id": "bilibili-settings-actions",
            "actions": [{"id": "set_settings", "label": "保存播放设置", "variant": "primary"}],
        },
        {
            "type": "ActionRow",
            "id": "bilibili-auth-actions",
            "actions": [
                {"id": "qr_login_start", "label": "Web 扫码登录", "variant": "primary"},
                {"id": "android_qr_login_start", "label": "TV/Android 4K 授权", "variant": "primary"},
                {"id": "qr_login_poll", "label": "检查 Web 扫码", "variant": "secondary"},
                {"id": "android_qr_login_poll", "label": "检查 Android 扫码", "variant": "secondary"},
                {"id": "status", "label": "校验登录态", "variant": "secondary"},
                {"id": "logout", "label": "退出并清除凭据", "variant": "secondary"},
            ],
        },
        {
            "type": "Form",
            "id": "bilibili-wiliwili-form",
            "fields": [
                {
                    "name": "wiliwili_dir",
                    "label": "Wiliwili 配置目录",
                    "type": "string",
                    "default": str(cfg.get("wiliwili_dir") or "~/.config/wiliwili"),
                }
            ],
        },
        {
            "type": "ActionRow",
            "id": "bilibili-import-actions",
            "actions": [
                {"id": "import_wiliwili_session", "label": "导入 Wiliwili 会话", "variant": "secondary"}
            ],
        },
    ]
    if qr.get("qr_png_data_uri"):
        mode = "TV/Android" if qr.get("mode") == "android" else "Web"
        children.extend(
            [
                {"type": "Markdown", "id": "bilibili-qr-title", "content": f"### {mode} 登录二维码"},
                {
                    "type": "Image",
                    "id": "bilibili-qr-image",
                    "src": str(qr["qr_png_data_uri"]),
                    "alt": f"Bilibili {mode} 登录二维码",
                    "width": 220,
                },
                {
                    "type": "Markdown",
                    "id": "bilibili-qr-url",
                    "content": f"扫码地址：`{qr.get('qr_url') or ''}`",
                },
            ]
        )
    if plugin._tab_status:
        children.append(
            {
                "type": "Markdown",
                "id": "bilibili-action-status",
                "content": plugin._tab_status,
            }
        )
    children.append(
        {
            "type": "List",
            "id": "bilibili-categories",
            "title": "已迁移分类",
            "items": [
                {"id": category, "title": category, "subtitle": "media.source.v1"}
                for category in (plugin._media_source.categories() if plugin._media_source else [])
            ],
        }
    )
    return {
        "type": "Section",
        "id": "bilibili-workspace-root",
        "title": "Bilibili 登录与播放",
        "description": "Web/Android 双登录、高清取流、画质与编码切换",
        "children": children,
    }


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default
