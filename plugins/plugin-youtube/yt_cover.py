"""Small, credential-free thumbnail fetch through the selected YouTube proxy."""
import asyncio
import base64
import ssl
import urllib.request
from urllib.parse import urlsplit
from omnidesk_sdk import CapabilityResult

_cache: dict[tuple[str, str], str] = {}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

async def fetch_cover(plugin, req):
    url = str(req.get('url') or '')
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == 'https' and parsed.hostname in {'i.ytimg.com', 'i1.ytimg.com', 'i2.ytimg.com', 'i3.ytimg.com', 'i4.ytimg.com'} and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        valid = False
    if not valid:
        return CapabilityResult.fail('无效的 YouTube 封面地址', error_code='invalid_argument')
    proxy = str(plugin.ctx.get_config('proxy') or '')
    key = (proxy, url)
    if key in _cache:
        return CapabilityResult.ok({'data_url': _cache[key]})
    def fetch():
        ssl_ctx = ssl.create_default_context()
        ignore_eof = getattr(ssl, 'OP_IGNORE_UNEXPECTED_EOF', 0)
        if ignore_eof:
            ssl_ctx.options |= ignore_eof
        handlers = [
            urllib.request.ProxyHandler({'https': proxy}) if proxy else urllib.request.ProxyHandler(),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        ]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=6) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            kind = response.headers.get_content_type()
            if len(data) > 2 * 1024 * 1024 or kind not in {'image/jpeg', 'image/png', 'image/webp'}:
                raise ValueError('invalid image')
            return f'data:{kind};base64,' + base64.b64encode(data).decode()
    try:
        if not hasattr(plugin, '_cover_semaphore'):
            plugin._cover_semaphore = asyncio.Semaphore(12)
        async with plugin._cover_semaphore:
            value = await asyncio.to_thread(fetch)
    except (OSError, ValueError):
        return CapabilityResult.fail('YouTube 封面暂不可用', error_code='service_offline')
    if len(_cache) >= 64:
        _cache.pop(next(iter(_cache)))
    _cache[key] = value
    return CapabilityResult.ok({'data_url': value})
