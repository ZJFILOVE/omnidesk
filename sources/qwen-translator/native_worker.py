import asyncio
import json
import sys

from credentials import credential, text_credential
from duplex import VIRTUAL_SINKS, command, devices, pipeline, select_routes
from server import PRESET_VOICES


def output(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def prepare():
    existing = {item["name"] for item in (await devices())["sinks"]}
    for name in sorted(VIRTUAL_SINKS - existing):
        await command("pactl", "load-module", "module-null-sink", f"sink_name={name}",
                      "rate=48000", "channels=2", f"sink_properties=device.description={name}")
    from local_subtitles import asr_available
    return {**await devices(), "voices": sorted(PRESET_VOICES), "asr_ready": asr_available()}


async def translate(config, reader):
    if not config.get("api_key", "").strip():
        raise ValueError("请输入 API Key，或从系统密钥库读取。")
    routes = select_routes(config, await devices())
    tasks = []

    async def emit(event):
        output(event)

    async def control():
        await reader.readline()

    try:
        tasks = [asyncio.create_task(pipeline(route, config, emit)) for route in routes]
        tasks.append(asyncio.create_task(control()))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    config = json.loads(await reader.readline())
    action = config.pop("action", "devices")
    if action == "devices":
        output(await prepare())
    elif action in {"lookup", "store", "clear"}:
        output({"key": credential(action, config["region"], config.get("key", ""))})
    elif action in {"text_lookup", "text_store", "text_clear"}:
        output({"key": text_credential(action.removeprefix("text_"), config["text_endpoint"], config.get("key", ""))})
    elif action == "codex_models":
        from text_translation import list_codex_models
        output(await list_codex_models())
    elif action == "antigravity_models":
        from antigravity_session import list_antigravity_models
        output(await list_antigravity_models())
    elif action == "test_translation":
        from text_translation import SubtitleTranslator
        translator = SubtitleTranslator({**config, "subtitle_language": "en-zh"})
        try:
            output(await translator.translate("Welcome to the meeting. We will discuss the project tomorrow."))
        finally:
            await translator.close()
    elif action == "translate":
        if config.get("mode") == "subtitles":
            from local_subtitles import subtitles
            async def emit(event):
                output(event)
            await subtitles(config, reader, emit, devices)
        else:
            await translate(config, reader)
    else:
        raise ValueError("Unknown action")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        output({"error": str(error)})
        raise SystemExit(1)
