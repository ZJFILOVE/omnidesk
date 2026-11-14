"""Read only Arr v3 clients. Secrets travel in stdin/headers, never in URLs."""
import hashlib
import json
from pathlib import PurePosixPath
import sys
import urllib.error
import urllib.parse
import urllib.request


def base_url(value):
    p = urllib.parse.urlsplit(value.strip().rstrip('/'))
    if (p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password
            or p.query or p.fragment):
        raise ValueError('服务地址须为 HTTP(S) 地址，不能包含密钥、账号或查询参数。')
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip('/'), '', ''))


def service_id(service):
    if service['kind'] not in {'radarr', 'sonarr'}:
        raise ValueError('未知媒体服务。')
    return service['kind'] + ':' + base_url(service['url'])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('媒体服务重定向了认证请求；请填写最终服务地址。')


class ArrClient:
    def __init__(self, service, key):
        self.base = base_url(service['url'])
        self.kind = service['kind']
        self.key = key
        if not key:
            raise ValueError('请先将此服务的 API Key 保存到系统密钥库。')
        # LAN services must not send their API keys through an environment proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, resource, **query):
        url = self.base + '/api/v3/' + resource
        if query:
            url += '?' + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={'X-Api-Key': self.key, 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=20) as response:
                body = response.read(32 * 1024 * 1024 + 1)
                if len(body) > 32 * 1024 * 1024:
                    raise ValueError('媒体服务响应过大，本次清单未更新。')
                return json.loads(body)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'媒体服务 HTTP {error.code}；请检查 API Key 和服务地址。') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError('媒体服务暂不可达；保留原清单，等待网络恢复。') from None

    def status(self):
        value = self.get('system/status')
        if not isinstance(value, dict) or not value.get('version'):
            raise ValueError('服务返回的状态无效。')
        return {'version': value['version'], 'appName': value.get('appName', self.kind)}

    def inventory(self):
        result = {}
        if self.kind == 'radarr':
            for movie in self._list(self.get('movie')):
                f = movie.get('movieFile')
                if not movie.get('hasFile'):
                    continue
                if not isinstance(f, dict) or not f.get('id'):
                    raise ValueError('电影文件信息不完整；本次清单未更新。')
                item = self._file(f, movie['path'], movie['title'], [])
                result[item['file_id']] = item
        else:
            for series in self._list(self.get('series')):
                sid = series['id']
                episodes = self._list(self.get('episode', seriesId=sid))
                by_file = {}
                for e in episodes:
                    if e.get('hasFile') and e.get('episodeFileId'):
                        by_file.setdefault(e['episodeFileId'], []).append(
                            f"S{int(e['seasonNumber']):02}E{int(e['episodeNumber']):02}")
                # Query files once per series, not once per episode (multi-episode releases).
                for f in self._list(self.get('episodefile', seriesId=sid)):
                    labels = sorted(set(by_file.get(f['id'], [])))
                    item = self._file(f, series['path'], series['title'], labels)
                    result[item['file_id']] = item
        return list(result.values())

    @staticmethod
    def _list(value):
        if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
            raise ValueError('媒体清单格式异常；保留上次清单。')
        return value

    @staticmethod
    def _file(f, parent, title, episodes):
        path = f.get('path') or str(PurePosixPath(parent) / f['relativePath'])
        if not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
            raise ValueError('媒体服务返回了无效文件路径。')
        version = {k: f.get(k) for k in ('id', 'size', 'dateAdded', 'quality', 'mediaInfo',
                                         'sceneName', 'originalFilePath')}
        version['path'] = path
        return {'file_id': str(f['id']), 'path': path, 'title': title,
                'episodes': episodes, 'size': f.get('size'), 'version': digest(version),
                'release': f.get('sceneName') or '', 'original': f.get('originalFilePath') or ''}


async def fetch(service, key, action='inventory'):
    from text_translation import run_process
    code, out, _ = await run_process([sys.executable, __file__],
        data=json.dumps({'service': service, 'key': key, 'action': action}).encode(), timeout=600)
    if code:
        raise RuntimeError('媒体服务请求进程失败；保留原清单。')
    result = json.loads(out)
    if 'error' in result:
        raise RuntimeError(result['error'])
    return result['value']


if __name__ == '__main__':
    data = {}
    try:
        data = json.load(sys.stdin)
        client = ArrClient(data['service'], data['key'])
        print(json.dumps({'value': client.status() if data['action'] == 'status' else client.inventory()}))
    except Exception as error:
        # No arbitrary exception string: JSON/urllib diagnostics can contain a key.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else '媒体服务读取失败。'
        if data.get('key'):
            message = message.replace(data['key'], '[hidden]')
        print(json.dumps({'error': message[:400]}))
