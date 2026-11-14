import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from server import (
    BridgeError, BridgeServer, CodexEngine, MODEL, Settings,
    make_command, parse_result, validate_request,
)

TOKEN = "test-local-token-" + "a" * 48
MESSAGES = [{"role": "system", "content": "只输出中文"}, {"role": "user", "content": "概括字幕"}]


class FakeEngine:
    def __init__(self):
        self.calls = 0
        self.error = None

    def run(self, messages, cancelled):
        self.calls += 1
        if self.error:
            raise self.error
        return {"content": "字幕总结", "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.server = BridgeServer(Settings("/unused", "/tmp", TOKEN, port=0), self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method="POST", path="/v1/chat/completions", body=None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        data = json.dumps(body or {"model": MODEL, "messages": MESSAGES}) if method == "POST" else None
        merged = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", **headers}
        conn.request(method, path, data, merged)
        response = conn.getresponse()
        status, content_type, text = response.status, response.getheader("Content-Type"), response.read().decode()
        conn.close()
        return status, content_type, text

    def test_unauthorized_never_invokes_codex_or_leaks_token(self):
        status, _, text = self.request(Authorization="Bearer wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.engine.calls, 0)
        self.assertNotIn(TOKEN, text)

    def test_blocks_web_page_origin_and_rebound_host(self):
        self.assertEqual(self.request(Origin="https://www.bilibili.com")[0], 403)
        self.assertEqual(self.request(Host="attacker.example")[0], 403)
        self.assertEqual(self.engine.calls, 0)

    def test_extension_origin_can_use_authenticated_models(self):
        status, _, text = self.request("GET", "/v1/models", Origin="moz-extension://trial")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["data"][0]["id"], MODEL)
        self.assertEqual(self.engine.calls, 0)

    def test_wrong_model_and_tool_calls_rejected(self):
        self.assertEqual(self.request(body={"model": "other", "messages": MESSAGES})[0], 400)
        self.assertEqual(self.request(body={"model": MODEL, "messages": MESSAGES, "tools": [{}]})[0], 400)
        self.assertEqual(self.engine.calls, 0)

    def test_standard_reply(self):
        status, _, text = self.request()
        self.assertEqual(status, 200)
        result = json.loads(text)
        self.assertEqual(result["choices"][0]["message"]["content"], "字幕总结")
        self.assertEqual(result["model"], MODEL)

    def test_sse_reply_and_failure_both_terminate(self):
        body = {"model": MODEL, "messages": MESSAGES, "stream": True}
        status, kind, text = self.request(body=body)
        self.assertEqual(status, 200)
        self.assertTrue(kind.startswith("text/event-stream"))
        self.assertTrue(text.endswith("data: [DONE]\n\n"))
        self.assertIn("字幕总结", text)
        self.engine.error = BridgeError(504, "codex_timeout", "超时")
        status, _, text = self.request(body=body)
        self.assertEqual(status, 200)
        self.assertIn('"error"', text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_embeddings_explicitly_unsupported(self):
        self.assertEqual(self.request(path="/v1/embeddings")[0], 501)
        self.assertEqual(self.engine.calls, 0)

    def test_busy_has_no_extra_model_call(self):
        self.server.slots.acquire()
        self.server.slots.acquire()
        try:
            self.assertEqual(self.request(body={"model": MODEL, "messages": MESSAGES, "stream": True})[0], 429)
            self.assertEqual(self.engine.calls, 0)
        finally:
            self.server.slots.release()
            self.server.slots.release()


class CliTest(unittest.TestCase):
    def test_command_is_text_only_and_preserves_roles(self):
        args, prompt = make_command(Settings("/codex", "/empty", TOKEN), MESSAGES)
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("read-only", args)
        self.assertEqual(args[args.index("--model") + 1], MODEL)
        self.assertIn("shell_tool", args)
        self.assertIn("skip_host_skill_discovery", args)
        history = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(history, [MESSAGES[1]])
        self.assertTrue(any(arg.startswith("developer_instructions=") and "只输出中文" in arg for arg in args))
        self.assertNotIn(TOKEN, " ".join(args))

    def test_jsonl_extracts_final_messages_and_usage(self):
        text = "\n".join(json.dumps(event) for event in [
            {"type": "item.completed", "item": {"type": "reasoning", "text": "private"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
            {"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}},
        ])
        self.assertEqual(parse_result(text, "", 0), {
            "content": "final", "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        })

    def test_raw_upstream_failures_are_never_forwarded(self):
        with self.assertRaises(BridgeError) as caught:
            parse_result('', 'authentication: secret=' + TOKEN, 1)
        self.assertEqual(caught.exception.code, "codex_login")
        self.assertNotIn(TOKEN, caught.exception.message)

    def test_timeout_and_cancel_terminate_own_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            script = root / "fake-codex"
            script.write_text("#!/usr/sbin/python3\nimport os,time\nfrom pathlib import Path\n"
                              f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
            script.chmod(0o700)
            engine = CodexEngine(Settings(str(script), directory, TOKEN, timeout_seconds=0.3))
            with self.assertRaises(BridgeError) as caught:
                engine.run(MESSAGES, threading.Event())
            self.assertEqual(caught.exception.code, "codex_timeout")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            pid_file.unlink()
            cancellation = threading.Event()
            errors = []
            def run():
                try:
                    CodexEngine(Settings(str(script), directory, TOKEN)).run(MESSAGES, cancellation)
                except BridgeError as error:
                    errors.append(error.code)
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                cancellation.wait(0.01)
            self.assertTrue(pid_file.exists())
            cancellation.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["cancelled"])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_malformed_messages_rejected(self):
        for bad in ([], [{"role": "tool", "content": "x"}], [{"role": "user", "content": []}],
                    [{"role": "assistant", "content": "x"}]):
            with self.assertRaises(BridgeError):
                validate_request({"model": MODEL, "messages": bad}, MODEL)


if __name__ == "__main__":
    unittest.main()
