"""Optional real converter coverage; run with OMNIDESK_TEST_CALIBRE_BIN configured."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import build_minimal_epub
from ebook_lib.parsers import convert_and_parse_mobi

BIN = os.environ.get('OMNIDESK_TEST_CALIBRE_BIN', '')
pytestmark = pytest.mark.skipif(not BIN, reason='Real Calibre executable not requested')

@pytest.mark.asyncio
@pytest.mark.parametrize('fmt', ['mobi', 'azw3', 'fb2', 'docx', 'rtf', 'htmlz'])
async def test_real_format_roundtrip(tmp_path: Path, fmt: str):
    source = build_minimal_epub(tmp_path / 'input.epub')
    target = tmp_path / f'book.{fmt}'
    env = {**os.environ, 'CALIBRE_CONFIG_DIRECTORY': str(tmp_path / 'calibre-config'), 'QT_QPA_PLATFORM': 'offscreen'}
    async def run(argv, **kwargs):
        proc = await asyncio.create_subprocess_exec(*argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), kwargs.get('timeout_sec', 90))
        return SimpleNamespace(ok=proc.returncode == 0, stdout=out.decode(errors='replace'))
    converted = await run([BIN, str(source), str(target)])
    assert converted.ok, converted.stdout[-1200:]
    parsed = await convert_and_parse_mobi(target, fmt, BIN, run, page_char_budget=200)
    text = ' '.join(block.original for page in parsed.pages for block in page.blocks)
    assert 'ephemeral design' in text and 'Telemetry' in text
    assert len(parsed.pages) > 1
    assert parsed.book_format == fmt
