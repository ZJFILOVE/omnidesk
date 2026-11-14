"""Bounded independent sessions with shared rate-limit backoff."""
import asyncio


def concurrency(config):
    try:
        return max(1, min(8, int(config.get('translation_concurrency', 4))))
    except (TypeError, ValueError):
        return 4


class RateLimitedError(RuntimeError):
    pass


class QuotaExceededError(RuntimeError):
    pass


def check_codex_limit(error):
    # Inspect provider errors locally; never expose their potentially sensitive text.
    import json
    value = json.dumps(error).lower()
    if any(key in value for key in ('usagelimit', 'usage_limit', 'insufficient_quota', 'quota exceeded')):
        raise QuotaExceededError('Codex 可用额度已耗尽，请等待额度恢复。')
    if any(key in value for key in ('ratelimit', 'rate_limit', 'rate limit', 'too many requests', '429')):
        raise RateLimitedError('Codex 请求限流，请稍后重试。')


class TranslationPool:
    def __init__(self, factory, size):
        self.factory = factory
        self.available = asyncio.Queue()
        for _ in range(size):
            self.available.put_nowait(None)
        self.sessions = []
        self.blocked_until = 0
        self.quota_error = None

    async def translate(self, text, **kwargs):
        session = await self.available.get()
        try:
            if self.quota_error:
                raise self.quota_error
            if session is None:
                session = self.factory()
                self.sessions.append(session)
            for attempt in range(3):
                loop = asyncio.get_running_loop()
                while self.blocked_until > loop.time():
                    await asyncio.sleep(self.blocked_until - loop.time())
                if self.quota_error:
                    raise self.quota_error
                try:
                    return await session.translate(text, **kwargs)
                except QuotaExceededError as error:
                    self.quota_error = error
                    raise
                except RateLimitedError:
                    self.blocked_until = max(self.blocked_until, loop.time() + 5 * 2 ** attempt)
                    if attempt == 2:
                        raise
        finally:
            self.available.put_nowait(session)

    async def close(self):
        await asyncio.gather(*(session.close() for session in self.sessions))
        self.sessions.clear()


async def gather_cancel_on_error(coroutines):
    tasks = [asyncio.create_task(item) for item in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
