"""Authenticated mobile subtitle and optional NAS media API. Run --help."""
import argparse
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mobile_jobs import JobError, MODEL, MobileJobs
from mobile_media import MediaLibrary, install_routes, read_config
from mobile_media_jobs import MediaJobs
from mobile_compatible import CompatibleMedia
from video_subtitles import translate_cues

MAX_REQUEST_BYTES = 4 * 1024 * 1024


def read_token(token_file=None):
    token = os.environ.get('MOBILE_API_TOKEN', '').strip()
    if not token:
        path = token_file or os.environ.get('MOBILE_API_TOKEN_FILE')
        if path:
            token = Path(path).expanduser().read_text(encoding='utf-8').strip()
    if len(token) < 32 or len(token) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('Set MOBILE_API_TOKEN or MOBILE_API_TOKEN_FILE to a random token of 32–512 printable ASCII characters.')
    return token


class RequestGuard:
    """Authenticate before reading input; cap streamed/chunked bodies before JSON parsing."""
    def __init__(self, app, *, token, max_bytes):
        self.app, self.token, self.max_bytes = app, token.encode(), max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        auth = headers.get(b'authorization', b'').split(b' ', 1)
        if len(auth) != 2 or auth[0].lower() != b'bearer' or not hmac.compare_digest(auth[1], self.token):
            return await JSONResponse({'detail': '需要有效的 Bearer 令牌。'}, status_code=401,
                                      headers={'WWW-Authenticate': 'Bearer'})(scope, receive, send)
        length = headers.get(b'content-length')
        if length is not None:
            try:
                count = int(length)
                if count < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({'detail': 'Invalid Content-Length.'}, status_code=400)(scope, receive, send)
            if count > self.max_bytes:
                return await JSONResponse({'detail': 'Request exceeds 4 MiB.'}, status_code=413)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > self.max_bytes:
                return await JSONResponse({'detail': 'Request exceeds 4 MiB.'}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        body = b''.join(chunks)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        async def private_send(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).extend([(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')])
            await send(message)

        await self.app(scope, buffered_receive, private_send)


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    client_request_id: str = Field(min_length=36, max_length=36)
    title: str = Field(default='', max_length=500)
    source_url: str = Field(default='', max_length=2048)
    subtitle_srt: str = Field(min_length=1, max_length=2 * 1024 * 1024)

    @field_validator('client_request_id')
    @classmethod
    def valid_uuid(cls, value):
        if str(UUID(value)) != value.lower():
            raise ValueError('client_request_id must be a UUID')
        return value.lower()

    @field_validator('source_url')
    @classmethod
    def valid_source_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('source_url must be an HTTP(S) URL without credentials')
        return value  # Metadata only: never fetched by the service.


def create_app(*, data_dir=None, token=None, token_file=None, translator=translate_cues,
               max_pending=100, max_request_bytes=MAX_REQUEST_BYTES, start_worker=True,
               media_config=None, media_client_factory=None):
    bearer = read_token(token_file) if token is None else token
    if not bearer:
        raise ValueError('A Bearer token is required.')
    directory = data_dir or os.environ.get('MOBILE_API_DATA_DIR') or Path.home() / '.local/share/qwen-mobile'

    @asynccontextmanager
    async def lifespan(app):
        jobs = MobileJobs(directory, translator=translator, max_pending=max_pending)
        app.state.jobs = jobs
        media_jobs = None
        compatible = None
        media = None
        try:
            factory = {'client_factory': media_client_factory} if media_client_factory else {}
            media = MediaLibrary(media_config if media_config is not None else read_config(), Path(directory) / 'media-cache', **factory)
            app.state.media = media
            media_jobs = MediaJobs(Path(directory) / 'media', media, translator=translator, max_pending=max_pending)
            app.state.media_jobs = media_jobs
            compatible = CompatibleMedia(Path(directory) / 'media-compatible', media)
            app.state.compatible_media = compatible
            if start_worker:
                jobs.start()
                media_jobs.start()
                compatible.start()
            yield
        finally:
            if media:
                await media.close()
            if compatible:
                await compatible.close()
            if media_jobs:
                await media_jobs.close()
            await jobs.close()

    app = FastAPI(title='Qwen Mobile Subtitles', version='1', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestGuard, token=bearer, max_bytes=max_request_bytes)

    @app.exception_handler(JobError)
    async def job_error(request, error):
        headers = {'Retry-After': '30'} if error.status == 429 else None
        return JSONResponse({'detail': str(error)}, status_code=error.status, headers=headers)

    @app.get('/health')
    async def health():
        capabilities = ['delete_requests']
        if app.state.media.configured:
            capabilities.extend(['nas_media', 'delete_media_requests', 'compatible_audio'])
        return {'status': 'ok', 'model': MODEL, 'api_version': 1, 'capabilities': capabilities}

    @app.delete('/v1/requests/{client_request_id}')
    async def delete_request(client_request_id: UUID):
        return await app.state.jobs.delete_request(str(client_request_id))

    @app.post('/v1/jobs', status_code=202)
    async def submit(request: Request):
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return JSONResponse({'detail': 'Use application/json.'}, status_code=415)
        try:
            raw = await request.json()
            data = Submission.model_validate(raw)
            return app.state.jobs.submit(data.client_request_id, data.title, data.source_url, data.subtitle_srt)
        except (ValueError, ValidationError, UnicodeError):
            # Pydantic's default response echoes rejected inputs; do not echo subtitle data.
            return JSONResponse({'detail': '无效请求：需要 UUID 任务编号、有效元数据和完整 UTF-8 SRT（最多 2 MiB / 20000 条）。'},
                                status_code=422)

    @app.get('/v1/jobs/{job_id}')
    async def job(job_id: UUID):
        return app.state.jobs.get(str(job_id))

    @app.post('/v1/jobs/{job_id}/cancel')
    async def cancel(job_id: UUID):
        return await app.state.jobs.cancel(str(job_id))

    @app.post('/v1/jobs/{job_id}/retry')
    async def retry(job_id: UUID):
        return app.state.jobs.retry(str(job_id))

    @app.get('/v1/jobs/{job_id}/subtitles/{kind}')
    async def subtitle(job_id: UUID, kind: str):
        return Response(app.state.jobs.subtitle(str(job_id), kind), media_type='application/x-subrip; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{job_id}.{kind}.srt"'})

    install_routes(app)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default=None)
    parser.add_argument('--token-file', default=None)
    parser.add_argument('--max-pending', type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.max_pending <= 1000:
        parser.error('port must be 1–65535; max-pending must be 1–1000')
    import uvicorn
    uvicorn.run(create_app(data_dir=args.data_dir, token_file=args.token_file, max_pending=args.max_pending),
                host=args.host, port=args.port, workers=1, access_log=False,
                limit_concurrency=32, timeout_keep_alive=5)


if __name__ == '__main__':
    main()
