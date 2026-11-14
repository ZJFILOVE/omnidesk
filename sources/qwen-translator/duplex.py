import asyncio
import base64
import json
from contextlib import suppress

import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import FileResponse

from server import APP_DIR, PRESET_VOICES, WS_URLS, app, build_session, event_id, normalize_event


ALLOWED_ORIGINS = {"http://127.0.0.1:8010", "http://localhost:8010"}
VIRTUAL_SINKS = {"meeting-remote", "meeting-translator"}
session_lock = asyncio.Lock()


async def command(*arguments):
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode().strip() or f"{arguments[0]} failed")
    return stdout.decode()


async def devices():
    sinks, sources, default_sink, default_source = await asyncio.gather(
        command("pactl", "--format=json", "list", "sinks"),
        command("pactl", "--format=json", "list", "sources"),
        command("pactl", "get-default-sink"),
        command("pactl", "get-default-source"),
    )
    return {
        **{
            kind: [
                {"name": item["name"], "description": item.get("description", item["name"]),
                 "mute": item.get("mute", False)}
                for item in json.loads(raw)
            ]
            for kind, raw in (("sinks", sinks), ("sources", sources))
        },
        "default_sink": default_sink.strip(),
        "default_source": default_source.strip(),
    }


@app.get("/duplex")
async def duplex_page():
    return FileResponse(f"{APP_DIR}/static/duplex.html")


@app.get("/api/devices")
async def list_devices():
    return await devices()


@app.get("/api/voices")
async def list_voices():
    return {"voices": sorted(PRESET_VOICES), "default": "Tina"}


@app.post("/api/setup")
async def setup(request: Request):
    if request.headers.get("origin") not in ALLOWED_ORIGINS:
        raise HTTPException(403, "Local page required")
    existing = {item["name"] for item in (await devices())["sinks"]}
    for name in sorted(VIRTUAL_SINKS - existing):
        await command(
            "pactl", "load-module", "module-null-sink", f"sink_name={name}",
            "rate=48000", "channels=2", f"sink_properties=device.description={name}",
        )
    return await devices()


def select_routes(config, available):
    mode = config.get("mode", "inbound")
    if mode not in {"inbound", "outbound", "duplex", "mic_test"}:
        raise ValueError("Invalid mode")
    routes = []
    if mode in {"inbound", "duplex"}:
        routes.append(("inbound", "meeting-remote.monitor", config.get("headphones"), "en", "zh"))
    if mode in {"outbound", "duplex"}:
        routes.append(("outbound", config.get("microphone"), "meeting-translator", "zh", "en"))
    if mode == "mic_test":
        routes.append(("outbound", config.get("microphone"), config.get("headphones"), "zh", "en"))
    source_names = {item["name"] for item in available["sources"]}
    sink_names = {item["name"] for item in available["sinks"]}
    for direction, source, sink, _, _ in routes:
        if source not in source_names or sink not in sink_names:
            raise ValueError(f"{direction}: device missing; refresh devices and create virtual audio")
        if (direction == "inbound" or mode == "mic_test") and sink in VIRTUAL_SINKS:
            raise ValueError("Choose physical headphones for translated playback")
        if direction == "outbound" and source.endswith(".monitor"):
            raise ValueError("Choose a physical microphone, not a monitor")
        if any(item["name"] == source and item["mute"] for item in available["sources"]):
            raise ValueError(f"Microphone is muted: {source}")
    return routes


async def close_process(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def translation_session(route, config):
    direction, source, sink, source_language, target_language = route
    voice = config.get(f"{direction}_voice", "Tina")
    clone_mode = {"clone_once": "once", "clone_always": "always"}.get(voice, "off")
    session, _ = build_session({
        "source_language": source_language, "target_language": target_language,
        "region": config.get("region", "mainland"), "audio_enabled": True,
        "voice": "Tina" if clone_mode != "off" else voice,
        "voice_clone_mode": clone_mode,
    })
    return session


async def pipeline(route, config, emit):
    direction, source, sink, source_language, target_language = route
    session = translation_session(route, config)
    processes = []
    tasks = []
    try:
        async with websockets.connect(
            WS_URLS[config.get("region", "mainland")],
            additional_headers={"Authorization": f"Bearer {config['api_key']}"},
            open_timeout=15, max_size=4 * 1024 * 1024,
        ) as upstream:
            await upstream.send(json.dumps({"type": "session.update", "event_id": event_id(direction), "session": session}))
            async with asyncio.timeout(20):
                while True:
                    event = json.loads(await upstream.recv())
                    if event.get("type") == "error":
                        raise RuntimeError(str(event.get("error", event)))
                    if event.get("type") == "session.updated":
                        break
            capture = await asyncio.create_subprocess_exec(
                "parec", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
                "--latency-msec=100", f"--device={source}", f"--client-name=qwen-{direction}-input",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            processes.append(capture)
            playback = await asyncio.create_subprocess_exec(
                "pacat", "--playback", "--raw", "--format=s16le", "--rate=24000", "--channels=1",
                "--latency-msec=100", f"--device={sink}", f"--client-name=qwen-{direction}-output",
                stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            processes.append(playback)
            await emit({"direction": direction, "status": "listening",
                        "source": source, "sink": sink, "voice": session.get("audio", {}).get("output", {}).get("voice", "Tina"),
                        "clone_mode": session.get("voice_clone_options", {}).get("frequency", "off")})

            async def send_audio():
                while True:
                    chunk = await capture.stdout.readexactly(3200)
                    await upstream.send(json.dumps({
                        "type": "input_audio_buffer.append", "event_id": event_id(direction),
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }))

            async def receive_audio():
                async for raw in upstream:
                    event = json.loads(raw)
                    if event.get("type") == "error":
                        raise RuntimeError(str(event.get("error", event)))
                    if event.get("type") == "response.audio.delta":
                        playback.stdin.write(base64.b64decode(event.get("delta", ""), validate=True))
                        await asyncio.wait_for(playback.stdin.drain(), 5)
                    else:
                        await emit({"direction": direction, **normalize_event(event)})
                raise RuntimeError(f"{direction}: API connection closed")

            async def watch_devices():
                while True:
                    await asyncio.sleep(2)
                    current = await devices()
                    if source not in {item["name"] for item in current["sources"]} or sink not in {item["name"] for item in current["sinks"]}:
                        raise RuntimeError(f"{direction}: audio device disconnected")

            async def watch_process(process, label):
                messages = []
                async for line in process.stderr:
                    messages.append(line.decode(errors="replace").strip())
                    messages = messages[-8:]
                exit_code = await process.wait()
                detail = " / ".join(messages) or "No error details"
                raise RuntimeError(f"{direction}: {label} exited ({exit_code}): {detail}")

            tasks = [asyncio.create_task(coroutine) for coroutine in
                     (send_audio(), receive_audio(), watch_process(capture, "capture"),
                      watch_process(playback, "playback"), watch_devices())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError(f"{direction}: audio process stopped")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_process(process) for process in processes))


@app.websocket("/ws/duplex")
async def duplex(websocket: WebSocket):
    if websocket.headers.get("origin") not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    if session_lock.locked():
        await websocket.send_json({"error": "Another translation session is running"})
        await websocket.close()
        return
    async with session_lock:
        tasks = []
        send_lock = asyncio.Lock()

        async def emit(event):
            async with send_lock:
                await websocket.send_json(event)

        try:
            config = await asyncio.wait_for(websocket.receive_json(), 15)
            if not isinstance(config, dict) or not str(config.get("api_key", "")).strip():
                raise ValueError("Enter your DashScope API key")
            routes = select_routes(config, await devices())
            tasks = [asyncio.create_task(pipeline(route, config, emit)) for route in routes]
            tasks.append(asyncio.create_task(websocket.receive()))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as exc:
            with suppress(Exception):
                await emit({"error": str(exc)})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                await websocket.close()
