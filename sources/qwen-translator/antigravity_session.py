"""Antigravity CLI login adapter with an independent conversation per request."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from text_translation import run_process, translation_input, translation_instructions
from translation_pool import RateLimitedError, QuotaExceededError


def antigravity_executable():
    executable = shutil.which('agy')
    if not executable:
        path = Path.home() / '.local/bin/agy'
        if path.is_file() and os.access(path, os.X_OK):
            executable = str(path)
    if not executable:
        raise RuntimeError('未找到 Antigravity CLI，请安装 agy 并在终端完成登录。')
    return executable


def login_environment():
    env = dict(os.environ)
    env.pop('GEMINI_API_KEY', None)
    return env


def check_antigravity_error(error):
    value = json.dumps(error).lower()
    if any(key in value for key in ('quota', 'usage_limit', 'usagelimit', 'credit exhausted')):
        raise QuotaExceededError('Antigravity 可用额度已耗尽，请等待额度恢复。')
    if any(key in value for key in ('429', 'resource_exhausted', 'rate_limit', 'ratelimit', 'rate limit', 'too many requests')):
        raise RateLimitedError('Antigravity 请求限流，请稍后重试。')


async def list_antigravity_models(timeout=30):
    with tempfile.TemporaryDirectory(prefix='qwen-agy-models-') as directory:
        try:
            code, output, error = await run_process(
                [antigravity_executable(), 'models'], cwd=directory,
                env=login_environment(), timeout=timeout)
        except TimeoutError:
            raise RuntimeError('读取 Antigravity 模型列表超时，请检查登录和网络后刷新。') from None
    if code:
        raise RuntimeError('Antigravity 无法读取模型列表，请在终端运行 agy 检查登录状态。')
    models = {}
    for line in output.decode('utf-8').splitlines():
        slug, separator, title = line.partition('\t')
        if separator and re.fullmatch(r'[a-zA-Z0-9][\w.:-]*', slug) and title.strip():
            models[slug] = {'model': slug, 'displayName': title.strip()}
    if not models:
        raise RuntimeError('Antigravity 未返回可选模型，请更新 agy 后刷新。')
    return {'models': list(models.values())}


class AntigravitySession:
    def __init__(self, config, timeout=45):
        self.config = dict(config)
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.task = None

    async def translate(self, text, *, instructions=None, output_schema=None):
        async with self.lock:
            self.task = asyncio.current_task()
            try:
                return await self._translate(text, instructions, output_schema)
            except TimeoutError:
                raise RuntimeError('Antigravity 翻译超时，下一段将重新连接。') from None
            finally:
                self.task = None

    async def _translate(self, text, instructions, output_schema):
        model = self.config.get('antigravity_model', '').strip()
        if not model:
            raise ValueError('请刷新模型列表并选择 Antigravity 模型。')
        with tempfile.TemporaryDirectory(prefix='qwen-agy-translation-') as directory:
            agent = Path(directory) / '.agents/agents/qwen-translator/agent.md'
            agent.parent.mkdir(parents=True)
            agent.write_text('---\nname: qwen-translator\ndescription: Text translation only\n'
                             'tools: [finish]\nmainAgent: true\nsubagent: false\n'
                             'mcpServers: []\nskills: []\nplugins: []\n'
                             'commandExecutionPolicy: off\n---\n' +
                             (instructions or translation_instructions(self.config)), encoding='utf-8')
            args = [antigravity_executable(), '--input-format', 'stream-json',
                    '--output-format', 'stream-json', '--disable-slash-commands',
                    '--agent', 'qwen-translator', '--model', model,
                    '--print-timeout', f'{self.timeout}s', '--log-file', os.devnull]
            schema = output_schema or ({'type': 'object', 'properties': {'subtitles': {
                'type': 'array', 'items': {'type': 'object',
                    'properties': {'id': {'type': 'integer'}, 'text': {'type': 'string'}},
                    'required': ['id', 'text'], 'additionalProperties': False}}},
                'required': ['subtitles'], 'additionalProperties': False}
                if self.config.get('video_batch') else {'type': 'object',
                    'properties': {'text': {'type': 'string'}}, 'required': ['text'],
                    'additionalProperties': False})
            args.extend(['--json-schema', json.dumps(schema)])
            prompt = (instructions or translation_instructions(self.config)) + '\n' + (
                text if instructions or self.config.get('video_batch') else translation_input(text, self.config['subtitle_language']))
            if output_schema is None and self.config.get('video_batch'):
                prompt += '\nReturn the translated array inside the subtitles field of the required JSON object.'
            data = (json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False) + '\n').encode()
            code, output, error = await run_process(args, data=data, cwd=directory,
                                                   env=login_environment(), timeout=self.timeout)
        result = None
        try:
            for line in output.splitlines():
                event = json.loads(line)
                if event.get('event') == 'result':
                    if result is not None:
                        raise ValueError('Multiple results')
                    result = event['result']
            if code or not isinstance(result, dict) or result.get('status') != 'SUCCESS':
                check_antigravity_error(result or error.decode('utf-8', errors='replace'))
                raise ValueError('Failed turn')
            response = result.get('response', '')
            if 'structured_output' in result:
                structured = result['structured_output']
                response = structured if isinstance(structured, str) else json.dumps(structured, ensure_ascii=False)
            if output_schema is None and not self.config.get('video_batch'):
                response = json.loads(response)['text']
            elif output_schema is None:
                response = json.dumps(json.loads(response)['subtitles'], ensure_ascii=False)
            if not isinstance(response, str) or not response.strip():
                raise ValueError('Empty translation')
            usage = result.get('usage') or {}
            return {'text': response.strip(), 'usage': {
                target: usage[source] for source, target in (
                    ('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens'),
                    ('cache_read_tokens', 'cached_input_tokens'),
                    ('thinking_tokens', 'reasoning_output_tokens')) if source in usage}}
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError('Antigravity 翻译失败，请检查 agy 版本、模型、登录状态和额度。') from None

    async def close(self):
        task = self.task
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
