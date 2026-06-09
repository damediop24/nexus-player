import asyncio
import importlib.util
import json
import time
from typing import Optional

from db import get_settings, set_setting

HAS_PIKPAK = bool(importlib.util.find_spec('pikpakapi'))

_client = None
_active_jobs: dict[str, dict] = {}
_stream_url_cache: dict[str, dict] = {}


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        pass
    return asyncio.run(coro)


def is_configured() -> bool:
    if not HAS_PIKPAK:
        return False
    s = get_settings()
    return s.get('pikpak_enabled') == 'true' and bool(s.get('pikpak_username')) and bool(s.get('pikpak_password'))


def get_status() -> dict:
    s = get_settings()
    return {
        'available': HAS_PIKPAK,
        'configured': is_configured(),
        'enabled': s.get('pikpak_enabled') == 'true',
        'username': s.get('pikpak_username') or '',
        'has_password': bool(s.get('pikpak_password')),
    }


def configure(username: str, password: str, enabled: bool = True):
    set_setting('pikpak_username', username.strip())
    set_setting('pikpak_password', password)
    set_setting('pikpak_enabled', 'true' if enabled else 'false')
    global _client
    _client = None


def set_enabled(enabled: bool):
    set_setting('pikpak_enabled', 'true' if enabled else 'false')
    global _client
    _client = None


async def _get_client():
    from pikpakapi import PikPakApi

    s = get_settings()
    username = s.get('pikpak_username', '')
    password = s.get('pikpak_password', '')
    if not username or not password:
        raise RuntimeError('PikPak username and password not configured')

    client = PikPakApi(username=username, password=password)
    refresh = s.get('pikpak_refresh_token')
    access = s.get('pikpak_access_token')

    if refresh:
        client.refresh_token = refresh
    if access:
        client.access_token = access

    try:
        if refresh:
            await client.refresh_access_token()
        else:
            await client.login()
            await client.refresh_access_token()
    except Exception:
        await client.login()
        await client.refresh_access_token()

    if client.refresh_token:
        set_setting('pikpak_refresh_token', client.refresh_token)
    if client.access_token:
        set_setting('pikpak_access_token', client.access_token)

    return client


def _get_client_sync():
    global _client
    if _client is None:
        _client = _run(_get_client())
    return _client


def test_login(username: str, password: str) -> dict:
    if not HAS_PIKPAK:
        raise RuntimeError('pikpakapi not installed. Run: pip install pikpakapi')

    async def _test():
        from pikpakapi import PikPakApi
        client = PikPakApi(username=username, password=password)
        await client.login()
        await client.refresh_access_token()
        user = client.get_user_info()
        return {'ok': True, 'user': user.get('name') or user.get('email') or username}

    return _run(_test())


def _extract_stream_url(file_data: dict) -> Optional[str]:
    medias = file_data.get('medias') or []
    candidates = []
    for media in medias:
        link = media.get('link') or {}
        url = link.get('url')
        if url:
            priority = int(media.get('priority') or 0)
            candidates.append((priority, url))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return file_data.get('web_content_link')


def _content_type_for_name(name: str) -> str:
    ext = name.lower().rsplit('.', 1)[-1] if '.' in name.lower() else 'mp4'
    mime = {
        'mp4': 'video/mp4', 'm4v': 'video/mp4', 'mkv': 'video/x-matroska',
        'webm': 'video/webm', 'avi': 'video/x-msvideo', 'mov': 'video/quicktime',
    }
    return mime.get(ext, 'video/mp4')


async def _fresh_stream_async(file_id: str, refresh: bool = False) -> dict:
    if not refresh:
        cached = _stream_url_cache.get(file_id)
        if cached and cached.get('expires', 0) > time.time():
            return cached

    client = await _get_client()
    info = await client.get_download_url(file_id)
    stream_url = _extract_stream_url(info)
    if not stream_url:
        raise RuntimeError('PikPak stream URL not available')

    title = info.get('name') or 'PikPak stream'
    result = {
        'stream_url': stream_url,
        'content_type': _content_type_for_name(title),
        'title': title,
        'size': int(info.get('size') or 0),
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://mypikpak.com/',
            'Accept': '*/*',
        },
        'expires': time.time() + 300,
    }
    _stream_url_cache[file_id] = result
    return result


def get_fresh_stream(file_id: str, refresh: bool = False) -> dict:
    if not is_configured():
        raise RuntimeError('PikPak not configured')
    return _run(_fresh_stream_async(file_id, refresh=refresh))


def _phase_label(phase: str) -> str:
    if not phase:
        return 'unknown'
    return phase.replace('PHASE_TYPE_', '').lower()


def _task_source(task: dict) -> str:
    params = task.get('params') or {}
    if isinstance(params, dict):
        url_obj = params.get('url') or {}
        if isinstance(url_obj, dict):
            return url_obj.get('url') or ''
        if isinstance(url_obj, str):
            return url_obj
    return task.get('url') or ''


def _normalize_task(task: dict) -> dict:
    phase = task.get('phase') or task.get('phase_type') or ''
    phase_short = _phase_label(phase)

    progress = task.get('progress')
    if progress is None:
        progress = 100 if phase_short == 'complete' else 0
    progress = float(progress)
    if progress <= 1:
        progress *= 100

    ref = task.get('reference_resource') or {}
    file_obj = ref if ref.get('kind', '').startswith('drive#file') else (ref.get('file') or {})

    name = (
        task.get('name')
        or task.get('file_name')
        or ref.get('name')
        or file_obj.get('name')
        or 'PikPak task'
    )

    file_size = int(
        task.get('file_size')
        or task.get('file_size_byte')
        or ref.get('size')
        or file_obj.get('size')
        or 0
    )

    speed = int(task.get('speed') or task.get('download_speed') or 0)
    file_id = ref.get('id') or file_obj.get('id') or task.get('file_id')

    return {
        'id': task.get('id'),
        'type': 'pikpak',
        'name': name,
        'phase': phase_short,
        'state': phase_short,
        'progress': round(progress, 1),
        'file_size': file_size,
        'download_rate': speed,
        'upload_rate': 0,
        'peers': 0,
        'source': _task_source(task),
        'file_id': file_id,
        'task_id': task.get('id'),
        'error': task.get('message') or task.get('error'),
        'cloud': True,
        'paused': False,
    }


def track_job(task_id: str, file_id: str, source: str, label: str = ''):
    if not task_id:
        return
    _active_jobs[task_id] = {
        'task_id': task_id,
        'file_id': file_id,
        'source': source,
        'label': label,
        'started_at': time.time(),
    }


def _pick_video_file(files: list[dict]) -> Optional[dict]:
    video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.ts'}
    videos = []
    for f in files:
        name = (f.get('name') or '').lower()
        ext = '.' + name.rsplit('.', 1)[-1] if '.' in name else ''
        size = int(f.get('size') or 0)
        if ext in video_exts:
            videos.append((size, f))
    if not videos:
        return None
    return max(videos, key=lambda x: x[0])[1]


async def _resolve_async(source: str, label: str) -> dict:
    from pikpakapi.enums import DownloadStatus

    client = await _get_client()
    result = await client.offline_download(source)
    task = result.get('task') or {}
    file_obj = result.get('file') or {}
    task_id = task.get('id')
    file_id = file_obj.get('id')

    if not file_id:
        raise RuntimeError('PikPak did not return a file id for this torrent')

    track_job(task_id, file_id, source, label)

    deadline = time.time() + 600
    while time.time() < deadline:
        status = await client.get_task_status(task_id, file_id)
        if status == DownloadStatus.done:
            break
        if status == DownloadStatus.error:
            raise RuntimeError('PikPak cloud download failed')
        await asyncio.sleep(2)
    else:
        raise TimeoutError('PikPak download timed out after 10 minutes')

    info = await client.offline_file_info(file_id)
    if info.get('kind') == 'drive#folder':
        children = await client.file_list(parent_id=file_id, size=200)
        pick = _pick_video_file(children.get('files') or [])
        if not pick:
            raise RuntimeError('No playable video found in PikPak torrent folder')
        file_id = pick['id']
        info = await client.get_download_url(file_id)
    else:
        info = await client.get_download_url(file_id)

    stream_url = _extract_stream_url(info)
    if not stream_url:
        raise RuntimeError('PikPak stream URL not available yet')

    title = info.get('name') or label or 'PikPak stream'
    size = int(info.get('size') or 0)
    name_lower = title.lower()
    ext = name_lower.rsplit('.', 1)[-1] if '.' in name_lower else 'mp4'

    return {
        'type': 'video',
        'title': title,
        'url': source,
        'thumbnail': None,
        'duration': None,
        'site': 'pikpak',
        'stream_url': stream_url,
        'stream_type': 'progressive',
        'content_type': 'video/x-matroska' if ext == 'mkv' else 'video/mp4',
        'formats': [{
            'format_id': 'pikpak',
            'ext': ext,
            'quality': 'PikPak cloud',
            'resolution': 'source',
            'filesize': size,
            'url': stream_url,
        }],
        'best_format_id': 'pikpak',
        'subtitles': [],
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://mypikpak.com/',
            'Accept': '*/*',
        },
        'resolved_with': 'pikpak-cloud',
        'pikpak_task_id': task_id,
        'pikpak_file_id': file_id,
    }


async def _list_tasks_async(limit: int = 50) -> list[dict]:
    client = await _get_client()
    phases = [
        'PHASE_TYPE_RUNNING',
        'PHASE_TYPE_PENDING',
        'PHASE_TYPE_COMPLETE',
        'PHASE_TYPE_ERROR',
    ]
    result = await client.offline_list(size=limit, phase=phases)
    tasks = result.get('tasks') or []
    normalized = [_normalize_task(t) for t in tasks]

    seen = {t['id'] for t in normalized if t.get('id')}
    for job in _active_jobs.values():
        tid = job.get('task_id')
        if tid and tid not in seen:
            normalized.insert(0, {
                'id': tid,
                'type': 'pikpak',
                'name': job.get('label') or 'PikPak download',
                'phase': 'running',
                'state': 'running',
                'progress': 0,
                'file_size': 0,
                'download_rate': 0,
                'upload_rate': 0,
                'peers': 0,
                'source': job.get('source') or '',
                'file_id': job.get('file_id'),
                'task_id': tid,
                'error': None,
                'cloud': True,
                'paused': False,
            })
    return normalized


def list_tasks(limit: int = 50) -> list[dict]:
    if not is_configured():
        return []
    return _run(_list_tasks_async(limit))


def resolve_via_pikpak(source: str, label: str = 'PikPak torrent') -> dict:
    if not is_configured():
        raise RuntimeError('PikPak not configured')
    global _client
    _client = None
    return _run(_resolve_async(source, label))


def torrent_bytes_to_magnet(data: bytes) -> str:
    import libtorrent as lt
    info = lt.torrent_info(lt.bdecode(data))
    return lt.magnet_uri(info)