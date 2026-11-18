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
    if action == 'open_api_console':
        if not ctx.capabilities.has('web.embed.v1'):
            return CapabilityResult.fail('请启用 Chrome 插件', error_code='not_configured')
        return await ctx.capabilities.call(
            'web.embed.v1',
            {'action': 'open', 'url': 'https://console.cloud.google.com/apis/credentials'},
        )
    cookie, source = await resolve_cookie(ctx)
    has_session = session_cookie_present(cookie)
    if hasattr(plugin, '_cookie_logged_in'):
        plugin._cookie_logged_in = has_session
    if hasattr(plugin, '_cookie_source'):
        plugin._cookie_source = source
    return CapabilityResult.ok({
        'cookie_configured': bool(cookie),
        'cookie_source': source,
        'session_cookie_present': has_session,
        'oauth_configured': bool(plugin.oauth_token()),
        'status': 'cookie_available' if has_session else 'guest',
        'message': (
            f'已通过 {source} 读取 YouTube 登录 Cookie（可用于播放、订阅、稍后再看与推荐）。'
            'Chrome Cookie 中不包含 Data API Key / OAuth Token，这两项为可选 Google Cloud 开发者凭证。'
            if has_session
            else '未检测到 YouTube 登录 Cookie，请在 Chrome 登录 YouTube 后再点击刷新登录状态'
        ),
    })
