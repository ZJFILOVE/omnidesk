"""Exercise catalog discovery against a local JSONL server, with no account access."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from text_translation import list_codex_models


SERVER = '''
import json, os, sys, time
from pathlib import Path
Path(__file__ + '.pid').write_text(str(os.getpid()))
def read():
    return json.loads(sys.stdin.readline())
def reply(request, result):
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
request = read()
assert request['method'] == 'initialize'
reply(request, {})
assert read()['method'] == 'initialized'
request = read()
assert request['method'] == 'model/list'
assert request['params']['includeHidden'] is False
print(json.dumps({'method': 'notification', 'params': {}}), flush=True)
reply(request, {'data': [
    {'model': 'first', 'displayName': 'First', 'defaultReasoningEffort': 'medium',
     'supportedReasoningEfforts': [{'reasoningEffort': 'low', 'description': 'Fast'}, {'reasoningEffort': 'medium'}]},
    {'model': 'hidden', 'hidden': True},
    {'model': 'audio-only', 'inputModalities': ['audio']}
], 'nextCursor': 'page2'})
request = read()
assert request['params']['cursor'] == 'page2'
reply(request, {'data': [{'model': 'second', 'description': 'Second model'}], 'nextCursor': None})
time.sleep(30)
'''


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.executable = Path(self.temp.name) / 'codex'
        self.patch = patch('text_translation.codex_executable', return_value=str(self.executable))
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def server(self, code):
        self.executable.write_text('#!' + sys.executable + '\n' + code)
        self.executable.chmod(0o700)

    async def test_handshake_pagination_filtering_and_process_cleanup(self):
        self.server(SERVER)
        result = await list_codex_models()
        self.assertEqual(result['models'], [
            {'model': 'first', 'displayName': 'First', 'description': '', 'defaultReasoningEffort': 'medium',
             'supportedReasoningEfforts': [{'reasoningEffort': 'low', 'description': 'Fast'}, {'reasoningEffort': 'medium'}]},
            {'model': 'second', 'displayName': 'second', 'description': 'Second model',
             'defaultReasoningEffort': None, 'supportedReasoningEfforts': []}])
        pid = int(Path(str(self.executable) + '.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_timeout_stops_unresponsive_server(self):
        self.server('import time\ntime.sleep(30)')
        with self.assertRaisesRegex(RuntimeError, '超时'):
            await list_codex_models(timeout=.1)

    async def test_protocol_error_has_actionable_message(self):
        self.server("import json,sys\nr=json.loads(sys.stdin.readline())\n"
                    "print(json.dumps({'id':r['id'],'error':{'message':'private details'}}),flush=True)")
        with self.assertRaisesRegex(RuntimeError, 'CLI 版本与登录状态'):
            await list_codex_models()


if __name__ == '__main__':
    unittest.main()
