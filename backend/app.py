import asyncio
import json
import mimetypes
import os
import shutil
import socket
import threading
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from db import get_conn, get_settings, init_db, row_to_dict, set_setting
from resolver import DOWNLOADS, HAS_CURL_CFFI, _ffmpeg_path, download_media, find_mpv, launch_mpv, resolve_url
from streams import create_token, get_token
from torrent import HAS_LIBTORRENT, get_manager, is_magnet, is_torrent_bytes, normalize_url, parse_range_header
from pikpak import HAS_PIKPAK, configure as pikpak_configure, get_fresh_stream, get_status as pikpak_status, is_configured as pikpak_ready, list_tasks as pikpak_list_tasks, resolve_via_pikpak, set_enabled as pikpak_set_enabled, test_login as pikpak_test_login, torrent_bytes_to_magnet

ROOT = Path(__file__).parent.parent
PUBLIC = ROOT / 'public'
UPLOADS = ROOT / 'uploads'
UPLOADS.mkdir(exist_ok=True)

init_db()

app = FastAPI(title='Nexus Player', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

queue: list[dict] = []
ws_clients: list[WebSocket] = []
download_jobs: dict[int, dict] = {}


class ResolveRequest(BaseModel):
    url: str
    format_id: Optional[str] = None

    @field_validator('url', mode='before')
    @classmethod
    def _normalize_resolve_url(cls, value):
        return normalize_url(value) if value else value


class PlayRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    title: Optional[str] = None

    @field_validator('url', mode='before')
    @classmethod
    def _normalize_play_url(cls, value):
        return normalize_url(value) if value else value


class LocalPlayRequest(BaseModel):
    source_url: str
    stream_url: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    site: Optional[str] = None
    headers: Optional[dict] = None
    stream_type: str = 'progressive'
    content_type: Optional[str] = None
    resolved_with: str = 'local-bridge'


class ProgressRequest(BaseModel):
    url: str
    position: float
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[float] = 0
    site: Optional[str] = None
    format_id: Optional[str] = None


class QueueItem(BaseModel):
    url: str
    title: Optional[str] = None
    format_id: Optional[str] = None


class FavoriteRequest(BaseModel):
    url: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    site: Optional[str] = None


class LibrarySyncRequest(BaseModel):
    history: list[dict] = []
    favorites: list[dict] = []


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None


class BatchRequest(BaseModel):
    urls: str


class SettingUpdate(BaseModel):
    key: str
    value: str


class PlaylistCreate(BaseModel):
    name: str


class PlaylistItemAdd(BaseModel):
    url: str
    title: Optional[str] = None


async def broadcast(event: str, data: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json({'event': event, 'data': data})
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _make_play_response(info: dict, source_url: str):
    if info.get('pikpak_file_id'):
        play_url = f'/api/pikpak/stream/{info["pikpak_file_id"]}'
    elif info.get('play_url'):
        play_url = info['play_url']
    elif info.get('stream_url', '').startswith('/api/'):
        play_url = info['stream_url']
    elif info.get('stream_url'):
        refresh_url = source_url if info.get('resolved_with') == 'kvs-player' else None
        token = create_token(
            info['stream_url'],
            info.get('headers'),
            info.get('stream_type', 'progressive'),
            info.get('content_type'),
            refresh_url=refresh_url,
        )
        play_url = f'/api/proxy/{token}'
    else:
        raise HTTPException(400, 'No playable stream found for this URL')

    if play_url.startswith('/api/proxy/'):
        token = play_url.split('/')[-1]
    else:
        token = None

    conn = get_conn()
    conn.execute(
        '''INSERT INTO history (url, title, thumbnail, duration, site, format_id)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (
            source_url,
            info.get('title'),
            info.get('thumbnail'),
            info.get('duration') or 0,
            info.get('site'),
            info.get('best_format_id'),
        ),
    )
    conn.commit()
    conn.close()

    subtitles = []
    for sub in info.get('subtitles') or []:
        s = dict(sub)
        if s.get('url'):
            s['proxy_url'] = f'/api/proxy/{create_token(s["url"], info.get("headers"))}'
        subtitles.append(s)

    return {
        **info,
        'play_url': play_url,
        'token': token,
        'source_url': source_url,
        'subtitles': subtitles,
    }


@app.get('/api/status')
def status():
    return {
        'name': 'Nexus Player',
        'version': '2.4.0',
        'torrent_available': HAS_LIBTORRENT,
        'pikpak': pikpak_status(),
        'lan_ip': _lan_ip(),
        'port': int(os.environ.get('PORT', 8899)),
        'mpv_available': True,
        'mpv_server': bool(find_mpv()),
        'mpv_client': True,
        'ffmpeg_available': bool(_ffmpeg_path()),
        'queue_length': len(queue),
        'anti_bot': bool(__import__('resolver')._available_impersonate_targets()),
        'browsers_detected': __import__('resolver')._detect_browsers(),
    }


@app.post('/api/resolve')
def api_resolve(req: ResolveRequest):
    try:
        return resolve_url(req.url, req.format_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/play')
def api_play(req: PlayRequest):
    try:
        info = resolve_url(req.url, req.format_id)
        if info.get('type') == 'playlist':
            for entry in info.get('entries', []):
                queue.append({
                    'id': str(uuid.uuid4()),
                    'url': entry['url'],
                    'title': entry.get('title'),
                })
            return {'type': 'playlist', 'queued': len(info.get('entries', [])), 'entries': info.get('entries')}

        return _make_play_response(info, req.url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/play/local')
def api_play_local(req: LocalPlayRequest):
    try:
        info = {
            'type': 'video',
            'title': req.title or 'Video',
            'url': req.source_url,
            'thumbnail': req.thumbnail,
            'duration': req.duration,
            'site': req.site,
            'stream_url': req.stream_url,
            'stream_type': req.stream_type,
            'content_type': req.content_type,
            'headers': req.headers or {},
            'resolved_with': req.resolved_with,
            'formats': [{
                'format_id': 'direct',
                'ext': req.stream_type if req.stream_type != 'progressive' else 'mp4',
                'quality': 'direct',
                'resolution': 'source',
                'url': req.stream_url,
            }],
            'best_format_id': 'direct',
            'subtitles': [],
        }
        return _make_play_response(info, req.source_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/batch')
def api_batch(req: BatchRequest):
    lines = [l.strip() for l in req.urls.splitlines() if l.strip()]
    results = []
    for url in lines:
        try:
            info = resolve_url(url)
            results.append({'url': url, 'ok': True, 'title': info.get('title'), 'type': info.get('type')})
            if info.get('type') != 'playlist':
                queue.append({'id': str(uuid.uuid4()), 'url': url, 'title': info.get('title')})
            else:
                for entry in info.get('entries', []):
                    queue.append({'id': str(uuid.uuid4()), 'url': entry['url'], 'title': entry.get('title')})
        except Exception as e:
            results.append({'url': url, 'ok': False, 'error': str(e)})
    return {'results': results, 'queued': len(queue)}


@app.get('/api/proxy/{token}')
@app.head('/api/proxy/{token}')
async def proxy_stream(token: str, request: Request):
    entry = get_token(token)
    if not entry:
        raise HTTPException(404, 'Stream expired or not found')

    headers = dict(entry.get('headers') or {})
    range_header = request.headers.get('range')
    if range_header:
        headers['Range'] = range_header
    if not any(k.lower() == 'user-agent' for k in headers):
        headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0, read=120.0))

    async def fetch_upstream():
        return await client.send(
            client.build_request('GET', entry['url'], headers=headers),
            stream=True,
        )

    try:
        upstream = await fetch_upstream()

        if upstream.status_code in (401, 403) and entry.get('refresh_url'):
            await upstream.aclose()
            from resolver import _resolve_kvs_player
            refreshed = _resolve_kvs_player(entry['refresh_url'])
            entry['url'] = refreshed['stream_url']
            entry['headers'] = refreshed.get('headers') or {}
            headers.clear()
            headers.update(entry['headers'])
            if range_header:
                headers['Range'] = range_header
            if not any(k.lower() == 'user-agent' for k in headers):
                headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            upstream = await fetch_upstream()

        out_headers = {
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges, Content-Type',
        }
        for key in ('content-type', 'content-length', 'content-range'):
            if key in upstream.headers:
                out_headers[key] = upstream.headers[key]

        media_type = upstream.headers.get('content-type')
        generic_types = {'binary/octet-stream', 'application/octet-stream', 'application/download'}
        if media_type:
            base_mime = media_type.split(';')[0].strip().lower()
            if base_mime in generic_types and entry.get('content_type'):
                media_type = entry['content_type']
                out_headers['content-type'] = media_type
        elif entry.get('content_type'):
            media_type = entry['content_type']
            out_headers['content-type'] = media_type
        else:
            media_type = 'application/vnd.apple.mpegurl' if entry.get('stream_type') == 'hls' else 'video/mp4'

        if request.method == 'HEAD':
            await upstream.aclose()
            await client.aclose()
            from starlette.responses import Response
            return Response(status_code=upstream.status_code, headers=out_headers)

        async def stream():
            try:
                async for chunk in upstream.aiter_bytes(65536):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=out_headers,
        )
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, f'Stream error: {exc}')


@app.get('/api/history')
def api_history():
    conn = get_conn()
    rows = conn.execute('SELECT * FROM history ORDER BY played_at DESC LIMIT 200').fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


@app.delete('/api/history')
def clear_history():
    conn = get_conn()
    conn.execute('DELETE FROM history')
    conn.commit()
    conn.close()
    return {'ok': True}


@app.post('/api/progress')
def save_progress(req: ProgressRequest):
    conn = get_conn()
    conn.execute(
        '''UPDATE history SET position = ?, played_at = datetime('now')
           WHERE id = (SELECT id FROM history WHERE url = ? ORDER BY played_at DESC LIMIT 1)''',
        (req.position, req.url),
    )
    conn.commit()
    conn.close()
    return {'ok': True}


@app.get('/api/favorites')
def api_favorites():
    conn = get_conn()
    rows = conn.execute('SELECT * FROM favorites ORDER BY added_at DESC').fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


@app.post('/api/favorites')
def add_favorite(req: FavoriteRequest):
    conn = get_conn()
    try:
        conn.execute(
            'INSERT INTO favorites (url, title, thumbnail, site) VALUES (?, ?, ?, ?)',
            (req.url, req.title, req.thumbnail, req.site),
        )
        conn.commit()
    except Exception:
        raise HTTPException(409, 'Already in favorites')
    finally:
        conn.close()
    return {'ok': True}


@app.delete('/api/favorites/{fav_id}')
def remove_favorite(fav_id: int):
    conn = get_conn()
    conn.execute('DELETE FROM favorites WHERE id = ?', (fav_id,))
    conn.commit()
    conn.close()
    return {'ok': True}


@app.post('/api/library/sync')
def library_sync(req: LibrarySyncRequest):
    conn = get_conn()

    for item in req.history:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        row = conn.execute(
            'SELECT id FROM history WHERE url = ? ORDER BY played_at DESC LIMIT 1',
            (url,),
        ).fetchone()
        if row:
            conn.execute(
                '''UPDATE history SET title = COALESCE(?, title), thumbnail = COALESCE(?, thumbnail),
                   duration = COALESCE(?, duration), site = COALESCE(?, site),
                   format_id = COALESCE(?, format_id), position = COALESCE(?, position),
                   played_at = COALESCE(?, played_at)
                   WHERE id = ?''',
                (
                    item.get('title'),
                    item.get('thumbnail'),
                    item.get('duration'),
                    item.get('site'),
                    item.get('format_id'),
                    item.get('position'),
                    item.get('played_at'),
                    row['id'],
                ),
            )
        else:
            conn.execute(
                '''INSERT INTO history (url, title, thumbnail, duration, site, format_id, position, played_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))''',
                (
                    url,
                    item.get('title'),
                    item.get('thumbnail'),
                    item.get('duration') or 0,
                    item.get('site'),
                    item.get('format_id'),
                    item.get('position') or 0,
                    item.get('played_at'),
                ),
            )

    for item in req.favorites:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        try:
            conn.execute(
                'INSERT INTO favorites (url, title, thumbnail, site, added_at) VALUES (?, ?, ?, ?, COALESCE(?, datetime(\'now\')))',
                (url, item.get('title'), item.get('thumbnail'), item.get('site'), item.get('added_at')),
            )
        except Exception:
            pass

    conn.commit()
    history = conn.execute('SELECT * FROM history ORDER BY played_at DESC LIMIT 300').fetchall()
    favorites = conn.execute('SELECT * FROM favorites ORDER BY added_at DESC').fetchall()
    conn.close()
    return {
        'history': [row_to_dict(r) for r in history],
        'favorites': [row_to_dict(r) for r in favorites],
    }


@app.get('/api/playlists')
def api_playlists():
    conn = get_conn()
    playlists = conn.execute('SELECT * FROM playlists ORDER BY created_at DESC').fetchall()
    result = []
    for p in playlists:
        items = conn.execute(
            'SELECT * FROM playlist_items WHERE playlist_id = ? ORDER BY position',
            (p['id'],),
        ).fetchall()
        d = row_to_dict(p)
        d['items'] = [row_to_dict(i) for i in items]
        result.append(d)
    conn.close()
    return result


@app.post('/api/playlists')
def create_playlist(req: PlaylistCreate):
    conn = get_conn()
    cur = conn.execute('INSERT INTO playlists (name) VALUES (?)', (req.name,))
    pid = cur.lastrowid
    row = conn.execute('SELECT * FROM playlists WHERE id = ?', (pid,)).fetchone()
    conn.commit()
    conn.close()
    return row_to_dict(row)


@app.post('/api/playlists/{playlist_id}/items')
def add_playlist_item(playlist_id: int, req: PlaylistItemAdd):
    conn = get_conn()
    count = conn.execute(
        'SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?',
        (playlist_id,),
    ).fetchone()[0]
    conn.execute(
        'INSERT INTO playlist_items (playlist_id, url, title, position) VALUES (?, ?, ?, ?)',
        (playlist_id, req.url, req.title, count),
    )
    conn.commit()
    conn.close()
    return {'ok': True}


@app.delete('/api/playlists/{playlist_id}/items/{item_id}')
def remove_playlist_item(playlist_id: int, item_id: int):
    conn = get_conn()
    conn.execute(
        'DELETE FROM playlist_items WHERE id = ? AND playlist_id = ?',
        (item_id, playlist_id),
    )
    conn.commit()
    conn.close()
    return {'ok': True}


@app.get('/api/queue')
def get_queue():
    return queue


@app.post('/api/queue')
def add_queue(item: QueueItem):
    entry = {'id': str(uuid.uuid4()), **item.model_dump()}
    queue.append(entry)
    return entry


@app.delete('/api/queue/{item_id}')
def remove_queue(item_id: str):
    global queue
    queue = [q for q in queue if q['id'] != item_id]
    return {'ok': True}


@app.delete('/api/queue')
def clear_queue():
    global queue
    queue = []
    return {'ok': True}


@app.post('/api/download')
def start_download(req: DownloadRequest):
    conn = get_conn()
    cur = conn.execute(
        'INSERT INTO downloads (url, status) VALUES (?, ?)',
        (req.url, 'downloading'),
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()

    def run():
        def on_progress(pct, filename):
            download_jobs[job_id] = {'progress': pct, 'filename': filename}
            asyncio.run(broadcast('download_progress', {'id': job_id, 'progress': pct}))

        try:
            result = download_media(req.url, req.format_id, on_progress)
            conn = get_conn()
            conn.execute(
                '''UPDATE downloads SET status = ?, progress = 100, filepath = ?, title = ?, finished_at = datetime('now')
                   WHERE id = ?''',
                ('completed', result['filepath'], result['title'], job_id),
            )
            conn.commit()
            conn.close()
            asyncio.run(broadcast('download_complete', {'id': job_id, 'filepath': result['filepath']}))
        except Exception as e:
            conn = get_conn()
            conn.execute(
                'UPDATE downloads SET status = ?, error = ? WHERE id = ?',
                ('failed', str(e), job_id),
            )
            conn.commit()
            conn.close()
            asyncio.run(broadcast('download_failed', {'id': job_id, 'error': str(e)}))

    threading.Thread(target=run, daemon=True).start()
    return {'id': job_id, 'status': 'downloading'}


@app.get('/api/downloads')
def list_downloads():
    conn = get_conn()
    rows = conn.execute('SELECT * FROM downloads ORDER BY created_at DESC LIMIT 50').fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


MEDIA_EXTS = {
    '.mp4', '.webm', '.mkv', '.avi', '.mov', '.m4v', '.flv', '.wmv', '.ogv', '.3gp',
    '.mp3', '.m4a', '.aac', '.ogg', '.wav', '.flac', '.opus', '.ts', '.m2ts',
}


@app.get('/api/library')
def api_library():
    files = []
    for folder in (DOWNLOADS, UPLOADS):
        if not folder.exists():
            continue
        for f in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not f.is_file() or f.suffix.lower() not in MEDIA_EXTS:
                continue
            stat = f.stat()
            files.append({
                'title': f.stem,
                'filename': f.name,
                'play_url': f'/api/local/{f.name}',
                'size': f'{stat.st_size / 1024 / 1024:.1f} MB',
                'folder': folder.name,
                'modified': stat.st_mtime,
            })
    return files


def _absolute_url(request: Request, path: str) -> str:
    if not path:
        return path
    if path.startswith('http://') or path.startswith('https://'):
        return path
    base = str(request.base_url).rstrip('/')
    if not path.startswith('/'):
        path = '/' + path
    return base + path


@app.post('/api/mpv')
def api_mpv(req: PlayRequest, request: Request, server: bool = False):
    try:
        info = resolve_url(req.url, req.format_id)
        play = _make_play_response(info, req.url)
        play_url = _absolute_url(request, play['play_url'])
        title = play.get('title') or req.title

        if server and find_mpv():
            port = int(os.environ.get('PORT', 8899))
            local_url = play['play_url']
            if local_url.startswith('/'):
                local_url = f'http://127.0.0.1:{port}{local_url}'
            launch_mpv(local_url, title, info.get('headers'))
            return {'ok': True, 'mode': 'server', 'play_url': play_url, 'title': title}

        return {
            'ok': True,
            'mode': 'client',
            'play_url': play_url,
            'title': title,
            'mpv_protocol': f'mpv://{play_url}',
            'command': f'mpv "{play_url}"',
        }
    except Exception as e:
        raise HTTPException(400, str(e))


class PikPakConfigRequest(BaseModel):
    username: str
    password: str
    enabled: bool = True


class PikPakEnableRequest(BaseModel):
    enabled: bool


class TorrentAddRequest(BaseModel):
    magnet: Optional[str] = None


class TorrentPlayRequest(BaseModel):
    file_index: Optional[int] = None


class TorrentPriorityRequest(BaseModel):
    file_index: int
    priority: int = 7


@app.get('/api/pikpak/status')
def api_pikpak_status():
    return pikpak_status()


@app.post('/api/pikpak/configure')
def api_pikpak_configure(req: PikPakConfigRequest):
    if not HAS_PIKPAK:
        raise HTTPException(400, 'pikpakapi not installed')
    try:
        pikpak_test_login(req.username, req.password)
        pikpak_configure(req.username, req.password, req.enabled)
        return {'ok': True, **pikpak_status()}
    except Exception as e:
        raise HTTPException(400, f'PikPak login failed: {e}')


@app.post('/api/pikpak/enable')
def api_pikpak_enable(req: PikPakEnableRequest):
    pikpak_set_enabled(req.enabled)
    return {'ok': True, **pikpak_status()}


@app.get('/api/pikpak/stream/{file_id}')
@app.head('/api/pikpak/stream/{file_id}')
async def api_pikpak_stream(file_id: str, request: Request):
    if not pikpak_ready():
        raise HTTPException(400, 'PikPak not configured')

    refresh = request.query_params.get('refresh') == '1'
    try:
        entry = get_fresh_stream(file_id, refresh=refresh)
    except Exception as e:
        raise HTTPException(502, f'PikPak stream: {e}')

    headers = dict(entry.get('headers') or {})
    range_header = request.headers.get('range')
    if range_header:
        headers['Range'] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0, read=120.0))

    async def proxy_upstream(stream_info: dict, retry: bool = True):
        upstream = await client.send(
            client.build_request('GET', stream_info['stream_url'], headers=headers),
            stream=True,
        )
        if upstream.status_code in (401, 403, 404) and retry:
            await upstream.aclose()
            stream_info = get_fresh_stream(file_id, refresh=True)
            return await proxy_upstream(stream_info, retry=False)
        return upstream, stream_info

    try:
        upstream, entry = await proxy_upstream(entry)

        out_headers = {
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges, Content-Type',
        }
        for key in ('content-type', 'content-length', 'content-range'):
            if key in upstream.headers:
                out_headers[key] = upstream.headers[key]

        media_type = upstream.headers.get('content-type') or entry.get('content_type') or 'video/mp4'
        base_mime = media_type.split(';')[0].strip().lower()
        if base_mime in {'binary/octet-stream', 'application/octet-stream', 'application/json', 'text/plain'}:
            media_type = entry.get('content_type') or 'video/mp4'
            out_headers['content-type'] = media_type

        if upstream.status_code >= 400:
            body = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            snippet = body[:200].decode('utf-8', errors='replace')
            raise HTTPException(upstream.status_code, f'PikPak CDN error: {snippet}')

        if not range_header:
            content_length = upstream.headers.get('content-length')
            try:
                if content_length and int(content_length) < 4096:
                    body = await upstream.aread()
                    await upstream.aclose()
                    await client.aclose()
                    snippet = body[:200].decode('utf-8', errors='replace')
                    raise HTTPException(502, f'PikPak returned invalid stream ({content_length} bytes): {snippet}')
            except ValueError:
                pass

        if request.method == 'HEAD':
            await upstream.aclose()
            await client.aclose()
            from starlette.responses import Response
            return Response(status_code=upstream.status_code, headers=out_headers)

        async def stream():
            try:
                async for chunk in upstream.aiter_bytes(65536):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=out_headers,
        )
    except HTTPException:
        raise
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, f'PikPak stream error: {exc}')


@app.get('/api/pikpak/tasks')
def api_pikpak_tasks(limit: int = 50):
    if not HAS_PIKPAK:
        return []
    if not pikpak_ready():
        return []
    try:
        return pikpak_list_tasks(limit=min(limit, 100))
    except Exception as e:
        raise HTTPException(400, f'PikPak tasks: {e}')


@app.get('/api/torrent')
def api_torrent_list():
    if not HAS_LIBTORRENT:
        return []
    return get_manager().list_all()


@app.post('/api/torrent/add')
async def api_torrent_add(magnet: Optional[str] = Form(None), file: UploadFile = File(None)):
    if magnet:
        magnet = normalize_url(magnet)

    if magnet and is_magnet(magnet):
        if pikpak_ready() and HAS_PIKPAK:
            try:
                info = resolve_via_pikpak(magnet, 'Magnet link')
                play = _make_play_response(info, magnet)
                return {'type': 'pikpak', 'pikpak': True, 'files': [], **play}
            except Exception as e:
                if not HAS_LIBTORRENT:
                    raise HTTPException(400, f'PikPak failed: {e}')
        if not HAS_LIBTORRENT:
            raise HTTPException(400, 'Torrent support requires PikPak login or libtorrent')
        mgr = get_manager()
        tid = mgr.add_magnet(magnet)
        mgr.wait_metadata(tid)
        return {'id': tid, **mgr.status(tid), 'files': mgr.list_files(tid)}

    if not HAS_LIBTORRENT:
        raise HTTPException(400, 'Torrent support requires PikPak login or libtorrent')

    mgr = get_manager()

    if file and file.filename:
        data = await file.read()
        if is_torrent_bytes(data) or (file.filename or '').lower().endswith('.torrent'):
            tid = mgr.add_torrent_data(data, file.filename)
            mgr.wait_metadata(tid)
            return {'id': tid, **mgr.status(tid), 'files': mgr.list_files(tid)}

    raise HTTPException(400, 'Provide a magnet link or .torrent file')


@app.get('/api/torrent/{tid}')
def api_torrent_status(tid: str):
    if not HAS_LIBTORRENT:
        raise HTTPException(400, 'Torrent support not available')
    try:
        return get_manager().status(tid)
    except KeyError:
        raise HTTPException(404, 'Torrent not found')


@app.get('/api/torrent/{tid}/files')
def api_torrent_files(tid: str):
    if not HAS_LIBTORRENT:
        raise HTTPException(400, 'Torrent support not available')
    try:
        return get_manager().list_files(tid)
    except KeyError:
        raise HTTPException(404, 'Torrent not found')


@app.post('/api/torrent/{tid}/play')
def api_torrent_play(tid: str, req: TorrentPlayRequest):
    if not HAS_LIBTORRENT:
        raise HTTPException(400, 'Torrent support not available')
    try:
        mgr = get_manager()
        entry = mgr.get(tid)
        if not entry:
            raise KeyError(tid)
        source = entry.get('source', f'torrent:{tid}')
        idx = req.file_index if req.file_index is not None else mgr.pick_best_video(tid)
        mgr.prioritize_file(tid, idx)
        files = mgr.list_files(tid)
        file_info = next(f for f in files if f['index'] == idx)
        info = {
            'type': 'video',
            'title': f"{mgr.status(tid)['name']} — {file_info['name']}",
            'site': 'torrent',
            'torrent_id': tid,
            'file_index': idx,
            'stream_url': f'/api/torrent/{tid}/stream/{idx}',
            'stream_type': 'progressive',
            'content_type': mgr.mime_for_file(tid, idx),
            'formats': [{
                'format_id': str(f['index']),
                'ext': f['ext'],
                'quality': f['name'],
                'resolution': 'source',
                'filesize': f['size'],
            } for f in files if f['is_video']],
            'best_format_id': str(idx),
            'subtitles': [],
            'headers': {},
            'play_url': f'/api/torrent/{tid}/stream/{idx}',
        }
        return _make_play_response(info, source)
    except KeyError:
        raise HTTPException(404, 'Torrent not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/torrent/{tid}/pause')
def api_torrent_pause(tid: str):
    get_manager().pause(tid)
    return {'ok': True}


@app.post('/api/torrent/{tid}/resume')
def api_torrent_resume(tid: str):
    get_manager().resume(tid)
    return {'ok': True}


@app.delete('/api/torrent/{tid}')
def api_torrent_remove(tid: str, delete_files: bool = False):
    get_manager().remove(tid, delete_files=delete_files)
    return {'ok': True}


@app.post('/api/torrent/{tid}/priority')
def api_torrent_priority(tid: str, req: TorrentPriorityRequest):
    get_manager().set_file_priority(tid, req.file_index, req.priority)
    return {'ok': True}


@app.get('/api/torrent/{tid}/stream/{file_index}')
@app.head('/api/torrent/{tid}/stream/{file_index}')
async def api_torrent_stream(tid: str, file_index: int, request: Request):
    if not HAS_LIBTORRENT:
        raise HTTPException(400, 'Torrent support not available')

    mgr = get_manager()
    try:
        files = mgr.list_files(tid)
        file_info = next(f for f in files if f['index'] == file_index)
        size = file_info['size']
    except (KeyError, StopIteration):
        raise HTTPException(404, 'Torrent file not found')

    range_header = request.headers.get('range')
    start, end = parse_range_header(range_header, size)
    length = end - start + 1

    loop = asyncio.get_event_loop()
    ready = await loop.run_in_executor(None, mgr.ensure_range, tid, file_index, start, end)
    if not ready:
        raise HTTPException(503, 'Torrent buffering — try again in a few seconds')

    path = mgr.file_path(tid, file_index)
    if not path.exists():
        raise HTTPException(503, 'Torrent file not on disk yet')

    media_type = mgr.mime_for_file(tid, file_index)
    headers = {
        'Accept-Ranges': 'bytes',
        'Content-Length': str(length),
        'Access-Control-Allow-Origin': '*',
        'Content-Type': media_type,
    }
    status_code = 200
    if range_header:
        status_code = 206
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'

    if request.method == 'HEAD':
        from starlette.responses import Response
        return Response(status_code=status_code, headers=headers)

    def iter_file():
        with open(path, 'rb') as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iter_file(), status_code=status_code, media_type=media_type, headers=headers)


@app.post('/api/upload')
async def upload_file(file: UploadFile = File(...)):
    raw = await file.read()
    filename = file.filename or 'upload'

    if filename.lower().endswith('.torrent') or is_torrent_bytes(raw):
        if pikpak_ready() and HAS_PIKPAK:
            try:
                magnet = torrent_bytes_to_magnet(raw)
                info = resolve_via_pikpak(magnet, filename)
                play = _make_play_response(info, magnet)
                return {'type': 'pikpak', 'filename': filename, **play}
            except Exception as e:
                if not HAS_LIBTORRENT:
                    raise HTTPException(400, f'PikPak failed: {e}')
        if HAS_LIBTORRENT:
            mgr = get_manager()
            info = mgr.resolve_torrent_file(raw, filename)
            return {
                'type': 'torrent',
                'torrent_id': info['torrent_id'],
                'filename': filename,
                'title': info['title'],
                'play_url': info['play_url'],
                'files': mgr.list_files(info['torrent_id']),
                **info,
            }
        raise HTTPException(400, 'Torrent support requires libtorrent or PikPak login')

    ext = Path(filename).suffix or '.mp4'
    fid = uuid.uuid4().hex
    dest = UPLOADS / f'{fid}{ext}'
    with open(dest, 'wb') as f:
        f.write(raw)

    mime, _ = mimetypes.guess_type(dest.name)
    return {
        'id': fid,
        'filename': filename,
        'play_url': f'/api/local/{fid}{ext}',
        'mime': mime or 'video/mp4',
        'title': filename,
    }


@app.get('/api/local/{filename}')
def serve_local(filename: str):
    path = UPLOADS / filename
    if not path.exists():
        path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get('/api/settings')
def api_settings():
    return get_settings()


@app.put('/api/settings')
def update_settings(req: SettingUpdate):
    set_setting(req.key, req.value)
    return {'ok': True}


@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get('cmd'):
                await broadcast('remote', msg)
                dead = []
                for client in ws_clients:
                    if client is ws:
                        continue
                    try:
                        await client.send_json(msg)
                    except Exception:
                        dead.append(client)
                for client in dead:
                    if client in ws_clients:
                        ws_clients.remove(client)
    except WebSocketDisconnect:
        if ws in ws_clients:
            ws_clients.remove(ws)


app.mount('/', StaticFiles(directory=str(PUBLIC), html=True), name='static')


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8899))
    print(f'Nexus Player running at http://localhost:{port}')
    print(f'LAN access: http://{_lan_ip()}:{port}')
    uvicorn.run(app, host='0.0.0.0', port=port)