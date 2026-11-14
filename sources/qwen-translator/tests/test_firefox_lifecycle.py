"""Install a real extension and exercise event-page idle shutdown (no model)."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('QWEN_FIREFOX_TESTS') == '1', 'Optional real Firefox lifecycle test')
class EventPageTests(unittest.TestCase):
    def test_slow_translation_survives_idle_then_background_can_sleep(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body><p>Hello world</p></body></html>')

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                # Exceeds two idle periods, including any grace given to listeners.
                time.sleep(5)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                result = {'segments': [{'id': part['id'], 'slots': {'0': '测试译文'}}
                                       for part in payload['segments']]}
                try:
                    self.wfile.write(json.dumps(result).encode())
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='qwen-event-page-') as directory:
            profile = Path(directory)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            (profile / 'user.js').write_text(
                f'user_pref("marionette.port", {port});\n'
                'user_pref("extensions.background.idle.timeout", 1500);\n'
                'user_pref("browser.shell.checkDefaultBrowser", false);\n')
            # Scale the heartbeat and idle clocks together, preserving the real
            # browser transport and lifecycle. Only the model server is simulated.
            with zipfile.ZipFile(profile / 'addon.xpi', 'w') as archive:
                for path in (ROOT / 'firefox-extension').iterdir():
                    if not path.is_file():
                        continue
                    data = path.read_bytes()
                    if path.name == 'background.js':
                        data = data.replace(b'http://127.0.0.1:8766', endpoint.encode())
                        self.assertIn(b'}, 10000);', data)
                        data = data.replace(b'}, 10000);', b'}, 200);')
                    archive.writestr(path.name, data)
            executable = sorted((ROOT / 'runtime/browsers').glob('firefox-*/firefox/firefox'))[-1]
            with (profile / 'browser.log').open('w') as log:
                process = subprocess.Popen([str(executable), '-headless', '-no-remote',
                    '-profile', directory, '-marionette', '--remote-allow-system-access'],
                    stdout=log, stderr=log)
                connection = None
                try:
                    deadline = time.monotonic() + 15
                    while connection is None and time.monotonic() < deadline:
                        try:
                            connection = socket.create_connection(('127.0.0.1', port), timeout=1)
                        except OSError:
                            time.sleep(.1)
                    self.assertIsNotNone(connection, 'Marionette did not start')
                    connection.settimeout(20)

                    def receive():
                        header = b''
                        while not header.endswith(b':'):
                            byte = connection.recv(1)
                            if not byte:
                                raise EOFError('Marionette closed')
                            header += byte
                        size = int(header[:-1])
                        body = b''
                        while len(body) < size:
                            chunk = connection.recv(size - len(body))
                            if not chunk:
                                raise EOFError('Truncated Marionette response')
                            body += chunk
                        return json.loads(body)

                    receive()  # Protocol handshake.
                    sequence = 0

                    def command(name, arguments=None):
                        nonlocal sequence
                        sequence += 1
                        body = json.dumps([0, sequence, name, arguments or {}]).encode()
                        connection.sendall(str(len(body)).encode() + b':' + body)
                        response = receive()
                        self.assertIsNone(response[2], str(response[2]))
                        return response[3]

                    def script(source, asynchronous=False):
                        return command('WebDriver:ExecuteAsyncScript' if asynchronous else 'WebDriver:ExecuteScript',
                                       {'script': source, 'args': [], 'newSandbox': True, 'sandbox': 'default'})['value']

                    command('WebDriver:NewSession')
                    command('Addon:Install', {'path': str(profile / 'addon.xpi'), 'temporary': True})
                    command('Marionette:SetContext', {'value': 'chrome'})
                    extension = ('const {ExtensionParent}=ChromeUtils.importESModule('
                                 '"resource://gre/modules/ExtensionParent.sys.mjs");'
                                 'const e=ExtensionParent.GlobalManager.getExtension("qwen-web-translator@local.zhu");')
                    url = script(extension + 'return e.baseURI.spec;')
                    command('Marionette:SetContext', {'value': 'content'})
                    command('WebDriver:Navigate', {'url': url + 'options.html'})
                    result = script('''
                      const done=arguments[arguments.length-1], b=window.wrappedJSObject.browser;
                      (async()=>{
                        await b.storage.local.set({token:'fixture-token'});
                        const tab=await b.tabs.create({url:ENDPOINT,active:false});
                        await new Promise(r=>setTimeout(r,500));
                        return b.scripting.executeScript({target:{tabId:tab.id},func:async()=>{
                          try{return await browser.runtime.sendMessage({type:'qwen:translate',payload:{
                            target:'zh-CN',segments:[{id:'s1',html:'<t id="0">Hello world</t>'}]}});}
                          catch(e){return {error:String(e)};}
                        }});
                      })().then(x=>done(JSON.stringify(x)),e=>done(JSON.stringify({error:String(e)})));
                    '''.replace('ENDPOINT', json.dumps(endpoint)), asynchronous=True)
                    translated = json.loads(result)[0]['result']
                    self.assertNotIn('error', translated)
                    self.assertEqual(translated['segments'][0]['slots']['0'], '测试译文')
                    command('Marionette:SetContext', {'value': 'chrome'})
                    # Reading internal state does not wake the extension or attach
                    # DevTools (which would mask the idle bug).
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        if script(extension + 'return e.backgroundState;') == 'stopped':
                            break
                        time.sleep(.3)
                    else:
                        self.fail('Background stayed awake after translation finished')
                finally:
                    if connection:
                        connection.close()
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
