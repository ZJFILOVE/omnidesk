"""Offline tests for the bundled Qwen Chrome MV3 extension and loopback translate bridge."""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from omnidesk_sdk import CapabilityResult, PluginContext

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from translate_bridge import LoopbackTranslateBridge, extract_fragment_slots  # noqa: E402


def test_qwen_chrome_extension_manifest_and_files() -> None:
    ext_dir = PLUGIN_DIR / "resources" / "qwen-translator-extension"
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert "browser_specific_settings" not in manifest
    assert manifest["background"]["service_worker"] == "sw.js"
    assert (ext_dir / "sw.js").is_file()
    assert (ext_dir / "hotkey.js").is_file()
    assert (ext_dir / "background.js").is_file()
    assert (ext_dir / "content.js").is_file()
    assert manifest["commands"]["translate-page"]["suggested_key"]["default"] == "Alt+Shift+T"

    # Ensure the minimal bridge skeleton does not steal Alt+Shift+T.
    bridge_manifest = json.loads(
        (PLUGIN_DIR / "resources" / "mv3-extension" / "manifest.json").read_text(encoding="utf-8")
    )
    assert "commands" not in bridge_manifest


def test_extract_fragment_slots_validates_inline_structure() -> None:
    slots = extract_fragment_slots('<t id="0">Hello </t><a><t id="1">world</t></a>')
    assert slots == {"0": "Hello ", "1": "world"}
    with pytest.raises(ValueError):
        extract_fragment_slots("<script>alert(1)</script>")


@pytest.mark.asyncio
async def test_loopback_translate_bridge_auth_health_and_translate(tmp_path: Path) -> None:
    ctx = PluginContext("plugin-chrome-web", plugin_dir=PLUGIN_DIR, data_dir=tmp_path / "data", config_dir=tmp_path / "cfg")
    bridge = LoopbackTranslateBridge(ctx)
    endpoint, token = await bridge.ensure_started()
    try:
        # Without llm.translate.v1 registered -> 503
        req_503 = urllib.request.Request(f"{endpoint}/health", headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(urllib.error.HTTPError) as exc_503:
            await asyncio.to_thread(urllib.request.urlopen, req_503, None, 3)
        assert exc_503.value.code == 503

        # Wrong token -> 401
        req_401 = urllib.request.Request(f"{endpoint}/health", headers={"Authorization": "Bearer wrong-token"})
        with pytest.raises(urllib.error.HTTPError) as exc_401:
            await asyncio.to_thread(urllib.request.urlopen, req_401, None, 3)
        assert exc_401.value.code == 401

        async def _fake_translate(req: dict[str, Any]) -> CapabilityResult:
            segs = req["segments"]
            out = []
            for seg in segs:
                txt = seg["text"]
                if txt == "Hello world":
                    out.append({"translated_text": "你好世界"})
                else:
                    out.append({"translated_text": '<t id="0">你好 </t><a><t id="1">世界</t></a>'})
            return CapabilityResult.ok({"translations": out})

        ctx.capabilities.register("llm.translate.v1", _fake_translate, plugin_id="plugin-magpie-agent")

        req_ok = urllib.request.Request(f"{endpoint}/health", headers={"Authorization": f"Bearer {token}"})
        resp_ok = await asyncio.to_thread(urllib.request.urlopen, req_ok, None, 3)
        health_body = json.loads(resp_ok.read().decode("utf-8"))
        assert health_body["ok"] is True

        payload = {
            "target": "zh-CN",
            "title": "Sample",
            "context": "",
            "segments": [
                {"id": "s0", "html": '<t id="0">Hello world</t>'},
                {"id": "s1", "html": '<t id="0">Hello </t><a><t id="1">world</t></a>'},
            ],
        }
        req_tr = urllib.request.Request(
            f"{endpoint}/translate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        resp_tr = await asyncio.to_thread(urllib.request.urlopen, req_tr, None, 3)
        tr_body = json.loads(resp_tr.read().decode("utf-8"))
        assert tr_body["failedSegments"] == []
        assert tr_body["segments"] == [
            {"id": "s0", "slots": {"0": "你好世界"}},
            {"id": "s1", "slots": {"0": "你好 ", "1": "世界"}},
        ]
    finally:
        await bridge.stop()
        await ctx.acleanup()
