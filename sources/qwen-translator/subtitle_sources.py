"""ASSRT API and SubHD public search. Downloads never execute archive contents."""
import gzip
import html
from html.parser import HTMLParser
import http.cookiejar
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from video_subtitles import decode

LIMIT = 16 * 1024 * 1024
TEXT_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt'}


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authenticated APIs must not forward their credentials on redirects.
        if req.has_header('Authorization'):
            raise RuntimeError('字幕服务重定向了认证请求，已停止。')
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('字幕下载地址无效。')
    import ipaddress
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        if parts.hostname.lower() == 'localhost' or '.' not in parts.hostname:
            raise ValueError('字幕服务返回了本地地址。')
    else:
        if not address.is_global:
            raise ValueError('字幕服务返回了本地地址。')
    return url


class WebClient:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoCredentialRedirect(),
                            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, url, *, headers=None, payload=None):
        validate_url(url)
        base = {'User-Agent': 'QwenTranslator/1.0 (Linux; subtitle client)'}
        base.update(headers or {})
        if payload is not None:
            base['Content-Type'] = 'application/json; charset=utf-8'
        request = urllib.request.Request(url, headers=base,
                    data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with self.opener.open(request, timeout=25) as response:
                result = response.read(LIMIT + 1)
                if len(result) > LIMIT:
                    raise ValueError('字幕文件超过 16 MB，请在网站选择单集文本字幕。')
                return result
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'字幕网站返回 HTTP {error.code}；可打开原网页处理登录或验证。') from None
        except urllib.error.URLError:
            raise RuntimeError('无法连接字幕网站，请检查代理与网络。') from None

    async def arequest(self, url, *, headers=None, payload=None):
        # urllib can block in DNS or a slow response after its coroutine is
        # cancelled. A private helper makes the whole request cancellable, and
        # keeps credentials in stdin rather than process arguments.
        from text_translation import run_process
        request = json.dumps({'url': url, 'headers': headers, 'payload': payload}).encode()
        try:
            code, result, error = await run_process(
                [sys.executable, __file__, '--request'], data=request, timeout=30)
        except TimeoutError:
            raise RuntimeError('字幕网站请求超时，请检查代理与网络。') from None
        if code:
            raise RuntimeError(error.decode(errors='replace').strip() or '字幕网站请求失败。')
        return result


def episode(name):
    match = re.search(r'(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b', name)
    return tuple(map(int, match.groups())) if match else None


def search_name(filename):
    stem = Path(filename).stem
    match = re.search(r'(?i)\bS\d{1,2}[ ._-]*E\d{1,3}\b', stem)
    if match:
        stem = stem[:match.end()]
    else:
        stem = re.split(r'(?i)\b(?:2160p|1080p|720p|bluray|web-dl|webrip)\b', stem)[0]
    return re.sub(r'[._]+', ' ', stem).strip()


class SubHDSearch(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = None
        self.parts = []
        self.items = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and re.fullmatch(r'/a/[A-Za-z0-9]+', attrs.get('href', '')):
            self.active = attrs['href']
            self.parts = []

    def handle_data(self, text):
        if self.active:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.active:
            title = ' '.join(''.join(self.parts).split())
            if title:
                self.items.setdefault(self.active, []).append(title)
            self.active = None


async def search(provider, query, token=''):
    web = WebClient()
    if provider == 'subhd':
        raw = await web.arequest('https://subhd.tv/search/' + urllib.parse.quote(query, safe=''))
        parser = SubHDSearch()
        parser.feed(decode(raw))
        if not parser.items and ('搜索结果' not in decode(raw)):
            raise RuntimeError('SubHD 页面结构变化或需要浏览器验证，请打开网站搜索。')
        return [{'provider': 'subhd', 'id': path.rsplit('/', 1)[-1],
                 'title': ' · '.join(dict.fromkeys(titles)), 'url': 'https://subhd.tv' + path}
                for path, titles in parser.items.items()]
    if provider != 'assrt':
        raise ValueError('未知字幕网站。')
    if not token:
        raise ValueError('ASSRT 需要个人 Token，请在本页填写，或使用 SubHD 搜索。')
    url = 'https://api.assrt.net/v1/sub/search?' + urllib.parse.urlencode({'q': query, 'cnt': 15})
    data = json.loads(await web.arequest(url, headers={'Authorization': 'Bearer ' + token}))
    if data.get('status') != 0:
        raise RuntimeError('ASSRT 搜索失败，请检查 Token 与请求额度。')
    return [{'provider': 'assrt', 'id': str(item['id']),
             'title': ' · '.join(str(item.get(k, '')) for k in ('native_name', 'videoname', 'subtype'))
                      + ' · ' + item.get('lang', {}).get('desc', ''),
             'url': 'https://assrt.net/xml/sub/' + str(item['id']) + '.xml'}
            for item in data.get('sub', {}).get('subs', [])]


def subtitle_files(data, filename):
    """Return text files in memory with bounded decompression and no path extraction."""
    stream = io.BytesIO(data)
    if zipfile.is_zipfile(stream):
        result = []
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if len(members) > 500 or sum(i.file_size for i in members) > LIMIT:
                raise ValueError('字幕压缩包过大，请选择单集字幕。')
            for member in members:
                if not member.is_dir() and Path(member.filename).suffix.lower() in TEXT_EXTENSIONS:
                    result.append((Path(member.filename).name, archive.read(member)))
        return result
    if data[:2] == b'\x1f\x8b':
        with gzip.GzipFile(fileobj=stream) as zipped:
            data = zipped.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('解压后的字幕过大。')
        filename = filename.removesuffix('.gz')
    if data.startswith(b'Rar!'):
        raise ValueError('该条目是 RAR 压缩包；请解压后导入 SRT/ASS，或选择其他条目。')
    if Path(filename).suffix.lower() not in TEXT_EXTENSIONS:
        raise ValueError('下载内容不是支持的文本字幕，请从网站下载 SRT 或 ASS 后导入。')
    return [(Path(filename).name, data)]


async def download(candidate, directory, token=''):
    if candidate['provider'] == 'subhd':
        if not re.fullmatch('[A-Za-z0-9]+', candidate['id']):
            raise ValueError('SubHD 条目编号无效。')
        from subhd_browser import fetch
        Path(directory).mkdir(parents=True, exist_ok=True)
        return await fetch(candidate['id'], directory)
    web = WebClient()
    links = []
    if candidate['provider'] == 'assrt':
        if not token:
            raise ValueError('请填写 ASSRT Token。')
        url = 'https://api.assrt.net/v1/sub/detail?' + urllib.parse.urlencode({'id': candidate['id']})
        data = json.loads(await web.arequest(url, headers={'Authorization': 'Bearer ' + token}))
        if data.get('status') != 0:
            raise RuntimeError('ASSRT 下载信息获取失败，请检查 Token 与请求额度。')
        for entry in data.get('sub', {}).get('subs', []):
            for item in entry.get('filelist', []):
                if Path(item.get('f', '')).suffix.lower() in TEXT_EXTENSIONS:
                    links.append((item['url'], item['f']))
    else:
        raise ValueError('未知字幕网站。')
    if not links:
        raise RuntimeError('没有可直接下载的文本字幕；网站可能需要验证，请打开条目网页下载后导入。')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for url, name in links[:30]:
        raw = await web.arequest(url)
        for filename, data in subtitle_files(raw, name):
            # Server-provided filenames are labels only; generated paths cannot traverse.
            path = root / f'{len(files):03}-{Path(filename).name}'
            path.write_bytes(data)
            files.append({'path': str(path), 'title': filename})
    return files


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        result = WebClient().request(**request)
        sys.stdout.buffer.write(result)
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        # Exceptions may contain a signed URL or service credentials.
        print('字幕网站请求失败，请检查代理与网络。', file=sys.stderr)
        raise SystemExit(1)
