"""A session-owned Codex service; each subtitle gets a fresh ephemeral thread."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import tomllib
from translation_pool import check_codex_limit

from text_translation import (translation_instructions, codex_executable, translation_input,
                              terminate_process_group)


class CodexSession:
    def __init__(self, config, timeout=45):
        self.config = dict(config)
        self.timeout = timeout
        self.process = None
        self.directory = None
        self.request_id = 0
        self.thread_id = None
        self.result = None
        self.completed = False
        self.lock = asyncio.Lock()

    async def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
        await self.process.stdin.drain()

    async def receive(self):
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError('Codex 翻译连接已断开，下一段将重新连接。')
        message = json.loads(line)
        if 'method' in message and 'id' in message:
            # Translation never grants permission to execute tools or answer prompts.
            await self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Tools are disabled'}})
        params = message.get('params') or {}
        if self.thread_id and params.get('threadId') == self.thread_id:
            if message.get('method') == 'item/completed':
                item = params.get('item', {})
                if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                    self.result['text'] = item.get('text', '')
            elif message.get('method') == 'thread/tokenUsage/updated':
                usage = params.get('tokenUsage', {}).get('last', {})
                self.result['usage'] = {target: usage[source] for source, target in (
                    ('inputTokens', 'input_tokens'), ('cachedInputTokens', 'cached_input_tokens'),
                    ('cacheWriteInputTokens', 'cache_write_input_tokens'),
                    ('outputTokens', 'output_tokens'), ('reasoningOutputTokens', 'reasoning_output_tokens'),
                ) if source in usage}
            elif message.get('method') == 'turn/completed':
                if params.get('turn', {}).get('status') != 'completed':
                    check_codex_limit(params.get('turn', {}).get('error', {}))
                    raise RuntimeError('Codex 未完成翻译，请检查模型、登录状态和额度。')
                self.completed = True
        return message

    async def request(self, method, params):
        self.request_id += 1
        request_id = self.request_id
        await self.send({'id': request_id, 'method': method, 'params': params})
        while True:
            message = await self.receive()
            if message.get('id') != request_id or 'method' in message:
                continue
            if 'error' in message:
                check_codex_limit(message['error'])
                raise RuntimeError('Codex 请求失败，请检查 CLI 版本、模型、登录状态和额度。')
            return message['result']

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        await self.close()
        self.directory = tempfile.TemporaryDirectory(prefix='qwen-codex-service-')
        env = dict(os.environ)
        for name in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
            env.pop(name, None)
        overrides = {'web_search': 'disabled', 'project_doc_max_bytes': 0,
                     'developer_instructions': '', 'model_provider': 'openai',
                     'features.skip_host_skill_discovery': True}
        for feature in ('shell_tool', 'apps', 'plugins', 'multi_agent', 'browser_use', 'computer_use',
                        'image_generation', 'hooks', 'memories', 'skill_search'):
            overrides['features.' + feature] = False
        # App Server has no --ignore-user-config. Disable configured MCPs by name.
        config_path = Path(env.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
        if config_path.is_file():
            for name in tomllib.loads(config_path.read_text()).get('mcp_servers', {}):
                # CLI override paths use literal dot-separated keys, not TOML
                # quoted key syntax. Quotes create a second, invalid MCP entry.
                overrides['mcp_servers.' + name + '.enabled'] = False
        args = [codex_executable(), 'app-server', '--listen', 'stdio://']
        for key, value in overrides.items():
            args.extend(['-c', key + '=' + json.dumps(value)])
        self.process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=self.directory.name, env=env,
            start_new_session=True, limit=4 * 1024 * 1024,
        )
        await self.request('initialize', {'clientInfo': {
            'name': 'qwen_subtitles', 'title': 'Qwen Subtitles', 'version': '1.0'},
            'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized', 'params': {}})

    async def translate(self, text, *, instructions=None, output_schema=None):
        async with self.lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self.start()
                    self.result = {'text': '', 'usage': {}}
                    self.completed = False
                    result = await self.request('thread/start', {
                        'model': self.config.get('codex_model') or 'gpt-5.6-luna',
                        'cwd': self.directory.name, 'ephemeral': True,
                        'approvalPolicy': 'never', 'sandbox': 'read-only',
                        'serviceTier': 'default',
                        'baseInstructions': instructions or translation_instructions(self.config), 'developerInstructions': '',
                        'personality': 'none', 'environments': [],
                    })
                    if result['thread'].get('ephemeral') is not True:
                        raise RuntimeError('Codex 未建立临时会话，已停止本次字幕请求。')
                    self.thread_id = result['thread']['id']
                    params = {'threadId': self.thread_id, 'input': [{'type': 'text',
                              'text': text if instructions else translation_input(text, self.config['subtitle_language'])}]}
                    if output_schema is not None:
                        params['outputSchema'] = output_schema
                    effort = self.config.get('codex_reasoning_effort', 'low')
                    if effort and effort != 'default':
                        params['effort'] = effort
                    await self.request('turn/start', params)
                    while not self.completed:
                        await self.receive()
                    if not self.result['text'].strip():
                        raise RuntimeError('Codex 未返回译文。')
                    self.result['text'] = self.result['text'].strip()
                    result = self.result
                    try:
                        async with asyncio.timeout(3):
                            await self.request('thread/unsubscribe', {'threadId': self.thread_id})
                    except Exception:
                        # A completed translation must not be retried for cleanup errors.
                        await self.close()
                    self.thread_id = None
                    self.result = None
                    return result
            except BaseException as error:
                await self.close()
                if isinstance(error, TimeoutError):
                    raise RuntimeError('Codex 翻译超时，下一段将重新连接。') from None
                raise

    async def close(self):
        process, self.process = self.process, None
        self.thread_id = None
        try:
            if process:
                await terminate_process_group(process)
        finally:
            if self.directory:
                self.directory.cleanup()
                self.directory = None
