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
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import get_conn, get_settings, init_db, row_to_dict, set_setting
from resolver import DOWNLOADS, HAS_CURL_CFFI, _ffmpeg_path, download_media, find_mpv, launch_mpv, resolve_url
from streams import create_token, get_token

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


class PlayRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    title: Optional[str] = None


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
    if not info.get('stream_url'):
        raise HTTPException(400, 'No playable stream found for this URL')

    token = create_token(
        info['stream_url'],
        info.get('headers'),
        info.get('stream_type', 'progressive'),
        info.get('content_type'),
    )

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
        'play_url': f'/api/proxy/{token}',
        'token': token,
        'source_url': source_url,
        'subtitles': subtitles,
    }


@app.get('/api/status')
def status():
    return {
        'name': 'Nexus Player',
        'version': '2.0.3',
        'lan_ip': _lan_ip(),
        'port': int(os.environ.get('PORT', 8899)),
        'mpv_available': bool(find_mpv()),
        'ffmpeg_available': bool(_ffmpeg_path()),
        'queue_length': len(queue),
        'anti_bot': HAS_CURL_CFFI,
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

    try:
        upstream = await client.send(
            client.build_request('GET', entry['url'], headers=headers),
            stream=True,
        )

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


@app.post('/api/mpv')
def api_mpv(req: PlayRequest):
    try:
        info = resolve_url(req.url, req.format_id)
        url = info.get('stream_url') or req.url
        mpv_path = launch_mpv(url, info.get('title'), info.get('headers'))
        return {'ok': True, 'mpv': mpv_path}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/upload')
async def upload_file(file: UploadFile = File(...)):
    ext = Path(file.filename or 'video.mp4').suffix or '.mp4'
    fid = uuid.uuid4().hex
    dest = UPLOADS / f'{fid}{ext}'
    with open(dest, 'wb') as f:
        shutil.copyfileobj(file.file, f)

    mime, _ = mimetypes.guess_type(dest.name)
    return {
        'id': fid,
        'filename': file.filename,
        'play_url': f'/api/local/{fid}{ext}',
        'mime': mime or 'video/mp4',
        'title': file.filename,
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