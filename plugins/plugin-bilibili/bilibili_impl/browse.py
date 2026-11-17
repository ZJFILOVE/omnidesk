"""Account feeds and UP spaces, including non-video dynamics and cursor paging."""
from __future__ import annotations

import hashlib
from typing import Any

from omnidesk_sdk import CapabilityResult
from .client import BilibiliApiError
from .media_mappers import map_dynamic_item
from .parsers import normalize_media_url
from .security import cookie_value


async def browse(plugin: Any, req: dict[str, Any]) -> CapabilityResult:
    client = plugin._build_client()
    cookie, _ = await plugin.resolve_cookie()
    action = req["action"]
    page = max(1, min(int(req.get("page") or 1), 100))
    mid = str(req.get("mid") or "")
    if action == "followings":
        if not cookie:
            raise BilibiliApiError("查看关注列表需要登录", error_code="not_configured")
        uid = cookie_value(cookie, "DedeUserID") or str((await client.check_nav(cookie)).get("mid") or "")
        data = (await client.get_json("/x/relation/followings", params={"vmid": uid, "pn": page, "ps": 50, "order": "desc"}, cookie=cookie)).get("data") or {}
        users = [{"id": str(u["mid"]), "name": u.get("uname", ""), "cover_url": normalize_media_url(u.get("face", "")), "summary": u.get("sign", "")} for u in data.get("list", []) if u.get("mid")]
        return CapabilityResult.ok({"users": users, "has_more": page * 50 < int(data.get("total") or 0)})
    if action == "space" and (not mid.isdigit() or int(mid) <= 0):
        return CapabilityResult.fail("UP 主 UID 无效", error_code="invalid_argument")
    if action == "dynamics" and not cookie:
        raise BilibiliApiError("查看关注动态需要登录", error_code="not_configured")
    profile = {}
    if action == "space" and page == 1:
        card = (await client.get_json("/x/web-interface/card", params={"mid": mid}, cookie=cookie)).get("data") or {}
        user = card.get("card") or {}
        profile = {"id": mid, "name": user.get("name", ""), "cover_url": normalize_media_url(user.get("face", "")), "summary": user.get("sign", ""), "followers": card.get("follower", 0)}
    key = (hashlib.sha256(cookie.encode()).hexdigest(), action, mid)
    offsets = plugin._browse_offsets.setdefault(key, {1: ""})
    if page == 1:
        offsets.clear()
        offsets[1] = ""
    if page not in offsets:
        return CapabilityResult.fail("请从第一页顺序加载动态", error_code="invalid_argument")
    params = {"offset": offsets[page], "type": "all", "timezone_offset": -480}
    if action == "space":
        params["host_mid"] = mid
    data = (await client.get_json(f"/x/polymer/web-dynamic/v1/feed/{'space' if action == 'space' else 'all'}", params=params, cookie=cookie)).get("data") or {}
    cards = []
    for row in data.get("items") or []:
        modules = row.get("modules") or {}
        author = modules.get("module_author") or {}
        dynamic = modules.get("module_dynamic") or {}
        major = dynamic.get("major") or {}
        if major.get("type") == "MAJOR_TYPE_BLOCKED" or modules.get("module_ad"):
            continue
        video = map_dynamic_item(row, plugin._default_quality_label(), client.api_base, source_id="plugin-bilibili")
        text = str((dynamic.get("desc") or {}).get("text") or "")
        images = [normalize_media_url(i.get("src", "")) for i in (major.get("draw") or {}).get("items", [])]
        article = major.get("article") or {}
        if article:
            text = "\n".join(filter(None, [text, article.get("title"), article.get("desc")]))
            images += [normalize_media_url(i) for i in article.get("covers", [])]
        orig = row.get("orig") or {}
        if orig:
            original = (orig.get("modules") or {}).get("module_dynamic") or {}
            text += "\n转发：" + str((original.get("desc") or {}).get("text") or "动态")
        cards.append({"id": row.get("id_str", ""), "name": author.get("name", ""), "mid": str(author.get("mid") or ""), "avatar": normalize_media_url(author.get("face", "")), "published": author.get("pub_time", ""), "text": text, "images": images, "video": video.model_dump() if video else None})
    has_more = bool(data.get("has_more") and data.get("offset"))
    if has_more:
        offsets[page + 1] = str(data["offset"])
    return CapabilityResult.ok({"cards": cards, "profile": profile, "has_more": has_more})
