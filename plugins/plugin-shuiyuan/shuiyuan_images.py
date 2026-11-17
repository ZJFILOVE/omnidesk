"""Authenticated, bounded retrieval for Shuiyuan attachments and WeChat cards."""
import base64
from urllib.parse import urlsplit

from omnidesk_sdk import CapabilityResult

if __package__:
    from .shuiyuan_http import HttpRequest
    from .shuiyuan_security import is_same_origin, validate_https_url
else:
    from shuiyuan_http import HttpRequest
    from shuiyuan_security import is_same_origin, validate_https_url


async def fetch_image(plugin, url):
    url = validate_https_url(url)
    host = urlsplit(url).hostname or ''
    own = is_same_origin(url, plugin.client.base_url)
    # Public card images need no forum credentials; do not create an open proxy.
    if not own and host not in {'shuiyuan.s3.jcloud.sjtu.edu.cn', 'mmbiz.qpic.cn', 'mmbiz.qlogo.cn'}:
        return CapabilityResult.fail('不支持的水源图片来源', error_code='permission_denied')
    auth = await plugin.auth.resolve_cookies() if own else None
    response = await plugin.client._http.request(HttpRequest(
        url=url, binary=True, max_bytes=8 * 1024 * 1024,
        cookie_header=auth.cookie_header if auth else '',
        headers={'Accept': 'image/*', 'Referer': plugin.client.base_url + '/' if own else 'https://mp.weixin.qq.com/'},
    ), extra_secrets=plugin.auth.secrets_for_redaction())
    mime = response.headers.get('content-type', '').split(';')[0].lower().strip()
    if not response.is_success or mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'} or not response.body_bytes:
        return CapabilityResult.fail('水源图片获取失败，请检查登录状态或原图是否仍可用', error_code=response.error_code or 'invalid_response')
    return CapabilityResult.ok({'data_url': f'data:{mime};base64,' + base64.b64encode(response.body_bytes).decode('ascii')})
