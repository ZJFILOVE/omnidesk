"""Optional real layout engine checks; text responses are local fixtures."""
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pdf_translation


@unittest.skipUnless(os.environ.get('QWEN_PDF_ENGINE_TESTS') == '1', 'optional BabelDOC runtime test')
class PdfEngineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import pymupdf
        cls.pdf = pymupdf
        logging.disable(logging.CRITICAL)
        pdf_translation.initialize_engine()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'source.pdf'
        with self.pdf.open() as doc:
            for n in (1, 2):
                page = doc.new_page()
                page.insert_text((60,60), f'Scientific document page {n}', fontsize=16)
                for row in range(8):
                    page.insert_textbox((60,100 + row * 75,500,170 + row * 75),
                        'The experiment measures temperature under controlled conditions. '
                        'Preserve the original document and translate the text. '
                        'Measurements should be checked carefully before drawing scientific conclusions.', fontsize=12)
            doc.save(self.path)
        self.config = {'source': str(self.path), 'text_provider': 'codex',
                       'codex_model': 'engine-test-' + self.root.name, 'translation_concurrency': 2}
        p = patch.object(pdf_translation, 'CACHE', self.root / 'cache')
        p.start()
        self.addCleanup(p.stop)
        self.emit = AsyncMock()

    async def translate_stub(self, text, **kwargs):
        if '## Here is the input:' in text:
            items = json.loads(text.rsplit('## Here is the input:', 1)[-1])
            value = json.dumps([{'id': item['id'], 'output': '译文：' + item['input']} for item in items], ensure_ascii=False)
        elif 'Now translate the following text:' in text:
            value = '译文：' + text.rsplit('Now translate the following text:', 1)[-1].strip()
        else:
            value = '译文：' + json.loads(text.rsplit('Excerpt: ', 1)[-1])
        return {'text': json.dumps({'text': value}, ensure_ascii=False)}

    async def test_selected_page_and_alternating_output(self):
        pool = AsyncMock()
        pool.translate.side_effect = self.translate_stub
        original = self.path.read_bytes()
        result = await pdf_translation.translate_pdf({**self.config, 'pages': '2', 'alternating': True}, self.emit, pool=pool)
        with self.pdf.open(result['mono']) as doc:
            self.assertEqual(doc.page_count, 2)
            self.assertNotIn('译文', doc[0].get_text())
            self.assertIn('译文', doc[1].get_text())
        with self.pdf.open(result['dual']) as doc:
            self.assertEqual(doc.page_count, 4)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(pool.translate.called)

    async def test_engine_fallback_cannot_hide_provider_failure(self):
        pool = AsyncMock()
        pool.translate.side_effect = RuntimeError('model unavailable')
        with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
            await pdf_translation.translate_pdf(self.config, self.emit, pool=pool)
        self.assertFalse(list((self.root / 'cache').glob('*/result.json')))
        self.assertFalse(list((self.root / 'cache').glob('*/mono.pdf')))

    async def test_cancellation_releases_model_and_work_directory(self):
        entered = asyncio.Event()
        async def block(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        pool = AsyncMock()
        pool.translate.side_effect = block
        task = asyncio.create_task(pdf_translation.translate_pdf(self.config, self.emit, pool=pool))
        try:
            waiter = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait([waiter, task], timeout=40, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                self.fail('Engine completed without making a model request')
            self.assertTrue(waiter.done(), 'Engine did not reach translation')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            self.assertFalse(list((self.root / 'cache').iterdir()))
            self.assertTrue(pool.close.called)
        finally:
            waiter.cancel()
            task.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)

    async def test_scanned_encrypted_and_out_of_range_fail_before_model(self):
        pool = AsyncMock()
        with self.assertRaisesRegex(ValueError, '超出范围'):
            await pdf_translation.translate_pdf({**self.config, 'pages': '3'}, self.emit, pool=pool)
        scan = self.root / 'scan.pdf'
        with self.pdf.open() as doc:
            page = doc.new_page()
            page.draw_rect((10,10,200,200), color=(0,0,0))
            doc.save(scan)
        with self.assertRaisesRegex(ValueError, 'OCR'):
            await pdf_translation.translate_pdf({**self.config, 'source': str(scan)}, self.emit, pool=pool)
        encrypted = self.root / 'encrypted.pdf'
        with self.pdf.open(self.path) as doc:
            doc.save(encrypted, encryption=self.pdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='password')
        with self.assertRaisesRegex(ValueError, '加密'):
            await pdf_translation.translate_pdf({**self.config, 'source': str(encrypted)}, self.emit, pool=pool)
        pool.translate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
