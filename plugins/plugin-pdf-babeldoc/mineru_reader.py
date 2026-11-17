"""MinerU selected-page bridge, compatible with the courseware API wrapper.

Never uploads during ordinary book/Agent reads. JSON is preferred; Markdown
fallback is only accepted for the single page actually submitted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.parse

from omnidesk_sdk import BookPageBlock

MAX_RESULT = 24 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def contained_file(root: Path, relative: str) -> Path:
    value = urllib.parse.unquote(relative)
    if not value or '\\' in value or urllib.parse.urlsplit(value).scheme or Path(value).is_absolute():
        raise ValueError('MinerU 图片必须是结果目录内的相对路径')
    path = root / value
    if '..' in Path(value).parts or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError('MinerU 图片路径不安全')
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('MinerU 图片不存在或路径越界')
    return path


def image_data(root: Path, relative: str) -> str:
    path = contained_file(root, relative)
    if path.stat().st_size > MAX_IMAGE:
        raise ValueError('MinerU 单张图片超过 8 MiB')
    raw = path.read_bytes()
    mime = ('image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else
            'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else
            'image/webp' if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' else '')
    if not mime:
        raise ValueError('MinerU 图片格式不支持，需 PNG / JPEG / WebP')
    return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')


def text_value(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(filter(None, (text_value(v) for v in value)))
    if isinstance(value, dict):
        return '\n'.join(filter(None, (text_value(value.get(k)) for k in ('text', 'content', 'list_items', 'code_body'))))
    return ''


def json_blocks(raw, root: Path, page: int, subset_index: int = 0) -> tuple[list[dict], list[dict]]:
    if not isinstance(raw, list):
        return [], []
    if raw and all(isinstance(p, list) for p in raw):
        raw = [{**b, 'page_idx': i} for i, blocks in enumerate(raw) for b in blocks if isinstance(b, dict)]
    blocks, locations = [], []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict) or item.get('page_idx') != subset_index:
            continue
        kind = item.get('type', 'text')
        if kind in {'header', 'footer', 'page_number'}:
            continue
        block_id = f'mineru-p{page}-b{idx}'
        content = text_value(item.get('text') or item.get('content') or item.get('list_items') or item.get('code_body'))
        if kind == 'equation' and content and not content.strip().startswith('$$'):
            content = '$$\n' + content + '\n$$'
        if item.get('img_path'):
            image = image_data(root, item['img_path'])
            blocks.append(dict(id=block_id + '-image', kind='image', original='', image_url=image, translated_image_url=image))
        # Tables retain the original crop when present and their machine-readable
        # representation as text for the Agent. Captions are independent blocks.
        if item.get('table_body'):
            content = text_value(item['table_body'])
        if content.strip():
            block_kind = 'formula' if kind == 'equation' else 'heading' if item.get('text_level') else 'paragraph'
            blocks.append(dict(id=block_id, kind=block_kind, original=content))
        for key in ('image_caption', 'image_footnote', 'table_caption', 'table_footnote', 'code_caption'):
            caption = text_value(item.get(key))
            if caption.strip():
                blocks.append(dict(id=f'{block_id}-{key}', kind='caption', original=caption))
        locations.append({'id': block_id, 'page': page, 'bbox': item.get('bbox'), 'source_type': kind})
    return blocks, locations


def markdown_blocks(markdown: str, root: Path, page: int) -> list[dict]:
    # MinerU emits inline image links; also accept CommonMark reference images.
    references = dict(re.findall(r'^\s{0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?.*$', markdown, re.M))
    markdown = re.sub(r'^\s{0,3}\[[^\]]+\]:\s*\S+.*$', '', markdown, flags=re.M)
    pattern = re.compile(r'!\[([^\]]*)\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\)|!\[([^\]]*)\]\[([^\]]*)\]|<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>', re.I)
    blocks = []
    def add_text(value):
        for paragraph in re.split(r'\n\s*\n', value.strip()):
            if paragraph.strip():
                blocks.append(dict(id=f'mineru-p{page}-md{len(blocks)}', kind='paragraph', original=paragraph.strip()))
    previous = 0
    for match in pattern.finditer(markdown):
        add_text(markdown[previous:match.start()])
        alt = match[1] if match[1] is not None else match[3] or ''
        ref = match[2] or match[5] or references.get(match[4] or alt)
        if not ref:
            raise ValueError('MinerU Markdown 图片引用缺少定义')
        image = image_data(root, ref.strip('<>'))
        blocks.append(dict(id=f'mineru-p{page}-md{len(blocks)}', kind='image', original='', image_url=image, translated_image_url=image))
        if alt:
            blocks.append(dict(id=f'mineru-p{page}-md{len(blocks)}', kind='caption', original=alt))
        previous = match.end()
    add_text(markdown[previous:])
    return blocks


def parse_result(root: Path, page: int) -> dict:
    files = sorted(root.rglob('*_content_list.json'))
    if (root / 'content_list.json').is_file():
        files.append(root / 'content_list.json')
    for path in files:
        checked = contained_file(root, str(path.relative_to(root)))
        if checked.stat().st_size > MAX_RESULT:
            raise ValueError('MinerU JSON 超过大小限制')
        try:
            raw = json.loads(checked.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            continue
        blocks, locations = json_blocks(raw, checked.parent, page)
        if any(b.get('original', '').strip() for b in blocks):
            return {'format': 'content_list', 'blocks': blocks, 'locations': locations}
    candidates = sorted(root.rglob('*.md'))
    if len(candidates) != 1:
        raise ValueError('MinerU 没有可用正文 JSON 或唯一 Markdown，原始结果已保留')
    path = contained_file(root, str(candidates[0].relative_to(root)))
    if path.stat().st_size > MAX_RESULT:
        raise ValueError('MinerU Markdown 超过大小限制')
    blocks = markdown_blocks(path.read_text(encoding='utf-8-sig'), path.parent, page)
    if not any(b.get('original', '').strip() for b in blocks):
        raise ValueError('MinerU 没有返回可翻译的正文')
    return {'format': 'markdown', 'blocks': blocks, 'locations': [], 'page_mapping': 'single_uploaded_page'}


class MineruReader:
    def __init__(self, data_dir: Path, get_config, get_token=lambda: ''):
        self.root = data_dir / 'mineru'
        self.config = get_config
        self.get_token = get_token
        self.locks = {}

    def directory(self, source: Path, page: int) -> Path:
        return self.root / source_hash(source) / str(page)

    def cached(self, source: Path, page: int):
        path = self.directory(source, page) / 'page.json'
        if not path.is_file():
            return None
        try:
            if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
                return None
            value = json.loads(path.read_text())
            if value.get('source_sha256') != source_hash(source) or value.get('page') != page:
                return None
            value['blocks'] = [BookPageBlock.model_validate(b) for b in value['blocks']]
            return value
        except (ValueError, KeyError, TypeError):
            return None

    async def parse(self, source: Path, page: int, *, upload: bool):
        key = await asyncio.to_thread(self.directory, source, page)
        async with self.locks.setdefault(str(key), asyncio.Lock()):
            cached = await asyncio.to_thread(self.cached, source, page)
            if cached:
                return cached
            if not upload:
                raise ValueError('当前页尚未解析；请通过“上传当前页至 MinerU”开始扫描解析')
            cfg = self.config()
            wrapper = Path(cfg.get('mineru_wrapper') or Path.home() / 'Documents/GLM-OCR/.agent/skills/mineru-document-parser/scripts/mineru_api.py').expanduser()
            python = str(Path(cfg.get('mineru_python') or Path.home() / 'Projects/qwen-translator/runtime/pdf-venv/bin/python').expanduser())
            if not wrapper.is_file() or not Path(python).is_file():
                raise ValueError('请配置 MinerU 适配器与含 PyMuPDF 的 Python 路径')
            key.mkdir(parents=True, exist_ok=True, mode=0o700)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            token_file = str(Path(cfg.get('mineru_token_file') or Path.home() / 'Documents/GLM-OCR/.mineru/token').expanduser())
            token = self.get_token()
            if not token and not Path(token_file).is_file():
                raise ValueError('请在 MinerU 设置中保存 API Token 或配置已有 Token 文件路径')
            env.pop('MINERU_API_TOKEN', None)
            env.pop('MINERU_TOKEN', None)
            env['MINERU_TOKEN_FILE'] = token_file
            if token:
                env['MINERU_API_TOKEN'] = token

            async def run(args, allowed=(0,)):
                process = await asyncio.create_subprocess_exec(python, *map(str, args), env=env,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    code = await asyncio.wait_for(process.wait(), 180)
                    if code not in allowed:
                        raise RuntimeError('MinerU 请求失败，请检查账号、额度及网络；私有任务回执已保留')
                    return code
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()

            task = key / 'task'
            if not (task / 'task.json').exists():
                subset = key / 'selected-page.pdf'
                await run(['-c', 'import pymupdf as f,sys; s=f.open(sys.argv[1]); t=f.open(); p=int(sys.argv[3])-1; t.insert_pdf(s,from_page=p,to_page=p); t.save(sys.argv[2]); t.close(); s.close()', source, subset, page])
                await run([wrapper, 'submit', '--mode', 'standard', '--file', subset, '--pages', '1', '--model', 'vlm', '--output', task])
            async with asyncio.timeout(900):
                while await run([wrapper, 'poll', '--output', task], (0, 3)) == 3:
                    await asyncio.sleep(3)
            if not (task / 'download.json').is_file():
                await run([wrapper, 'download', '--output', task])
            value = await asyncio.to_thread(parse_result, task / 'result', page)
            value.update(source_sha256=key.parent.name, page=page, parser='mineru')
            if await asyncio.to_thread(source_hash, source) != key.parent.name:
                raise ValueError('源 PDF 在解析期间发生变化，请重新打开')
            encoded = json.dumps(value, ensure_ascii=False)
            if len(encoded.encode()) > 64 * 1024 * 1024:
                raise ValueError('MinerU 当前页输出过大')
            temporary = key / 'page.tmp'
            temporary.write_text(encoded)
            temporary.chmod(0o600)
            temporary.replace(key / 'page.json')
            return await asyncio.to_thread(self.cached, source, page)
