"""Shared browser/Vault login material for status and yt-dlp playback."""
import http.cookies
from omnidesk_sdk import CapabilityResult


def session_cookie_present(cookie):
    parsed = http.cookies.SimpleCookie()
    try:
        parsed.load(cookie)
    except http.cookies.CookieError:
        return False
    return any(key in parsed for key in ('SID', '__Secure-1PSID', '__Secure-3PSID', 'LOGIN_INFO'))


async def resolve_cookie(ctx):
    # Prefer a newly verified browser session over an older pasted cookie.
    browser_cookie = ''
    if ctx.capabilities.has('web.cookies.v1'):
        result = await ctx.capabilities.call('web.cookies.v1', {'action': 'get_cookies', 'url': 'https://www.youtube.com/', 'domain': 'youtube.com', 'include_subdomains': True})
        if result.ok and result.data.get('cookie_header'):
            browser_cookie = str(result.data['cookie_header'])
            if session_cookie_present(browser_cookie):
                return browser_cookie, 'web.cookies.v1'
    cookie = str(ctx.get_secret('cookie_header') or '')
    if cookie:
        return cookie, 'vault'
    return browser_cookie, 'web.cookies.v1' if browser_cookie else 'none'


async def auth_action(plugin, action):
    ctx = plugin.ctx
    if action == 'open_login':
        if not ctx.capabilities.has('web.embed.v1'):
            return CapabilityResult.fail('请启用 Chrome 插件', error_code='not_configured')
        return await ctx.capabilities.call('web.embed.v1', {'action': 'open', 'url': 'https://www.youtube.com/'})
    cookie, source = await resolve_cookie(ctx)
    has_session = session_cookie_present(cookie)
    return CapabilityResult.ok({'cookie_configured': bool(cookie), 'cookie_source': source,
        'session_cookie_present': has_session, 'oauth_configured': bool(plugin.oauth_token()),
        'status': 'cookie_available' if has_session else 'guest',
        'message': '已读取浏览器登录 Cookie；实际可用性以播放解析结果为准' if has_session else '请在 Chrome 登录 YouTube，再返回刷新登录状态'})
