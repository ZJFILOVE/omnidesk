import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from translation_pool import (TranslationPool, RateLimitedError, QuotaExceededError,
                              check_codex_limit, concurrency, gather_cancel_on_error)
from video_subtitles import Cue, translate_cues


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_sessions_are_bounded_at_four_and_eight(self):
        for size in (4, 8):
            entered = 0
            peak = 0
            ready = asyncio.Event()
            release = asyncio.Event()
            class Session:
                busy = False
                async def translate(self, text):
                    nonlocal entered, peak
                    assert not self.busy
                    self.busy = True
                    entered += 1
                    peak = max(peak, entered)
                    if entered == size:
                        ready.set()
                    await release.wait()
                    entered -= 1
                    self.busy = False
                    return text
                async def close(self): pass
            pool = TranslationPool(Session, size)
            task = asyncio.create_task(gather_cancel_on_error(pool.translate(i) for i in range(16)))
            await asyncio.wait_for(ready.wait(), 1)
            self.assertEqual(peak, size)
            release.set()
            self.assertEqual(await task, list(range(16)))
            await pool.close()

    async def test_quota_stops_waiting_requests_without_retry(self):
        session = AsyncMock()
        session.translate.side_effect = QuotaExceededError('quota')
        pool = TranslationPool(lambda: session, 1)
        results = await asyncio.gather(*(pool.translate('text') for _ in range(4)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, QuotaExceededError) for result in results))
        self.assertEqual(session.translate.await_count, 1)
        await pool.close()

    async def test_rate_limit_retries_with_shared_cooldown(self):
        session = AsyncMock()
        session.translate.side_effect = [RateLimitedError('limit'), 'ok']
        pool = TranslationPool(lambda: session, 1)
        async def sleep(seconds):
            self.assertGreater(seconds, 0)
            pool.blocked_until = 0
        with patch('translation_pool.asyncio.sleep', side_effect=sleep) as wait:
            self.assertEqual(await pool.translate('text'), 'ok')
        self.assertEqual(wait.await_count, 1)
        self.assertEqual(session.translate.await_count, 2)
        await pool.close()

    async def test_failure_cancels_other_tasks(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def blocked():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def fail():
            await started.wait()
            raise RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            await gather_cancel_on_error([blocked(), fail()])
        self.assertTrue(stopped.is_set())

    async def test_video_parallel_completion_preserves_order_and_resume(self):
        ready = asyncio.Event()
        calls = []
        class Translator:
            def __init__(self, config): pass
            async def close(self): pass
            async def translate(self, text):
                ids = [item['id'] for item in json.loads(text)]
                calls.append(ids)
                if len(calls) == 4:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 1)
                await asyncio.sleep(.001 * (3 - ids[0] // 16))
                return {'text': json.dumps([{'id': i, 'text': str(i)} for i in reversed(ids)])}
        cues = [Cue(i * 1000, i * 1000 + 900, str(i)) for i in range(64)]
        with tempfile.TemporaryDirectory() as tmp:
            result = await translate_cues(cues, {'translation_concurrency': 4}, tmp, AsyncMock(), Translator)
            self.assertEqual(result, cues)
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(json.loads(next(Path(tmp).glob('translation-*.json')).read_text())), 64)
            await translate_cues(cues, {}, tmp, AsyncMock(), Translator)
            self.assertEqual(len(calls), 4)

    def test_limit_classification_and_config_bounds(self):
        self.assertEqual(concurrency({}), 4)
        self.assertEqual(concurrency({'translation_concurrency': 99}), 8)
        self.assertEqual(concurrency({'translation_concurrency': 0}), 1)
        with self.assertRaises(RateLimitedError):
            check_codex_limit({'codexErrorInfo': 'rateLimitReached'})
        with self.assertRaises(QuotaExceededError):
            check_codex_limit({'codexErrorInfo': 'usageLimitExceeded'})
