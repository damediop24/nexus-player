import os
import time
from typing import Optional

import httpx

from db import get_settings, set_setting

API_BASE = 'https://api.alldebrid.com'
DEFAULT_API_KEY = 'fFr0hkBueEl7XwmM9OjI'

VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.mpg', '.mpeg',
    '.mpe', '.flv', '.ogv', '.3gp', '.3g2', '.ts', '.m2ts', '.f4v', '.vob',
}

_STATUS_READY = 4
_STATUS_DOWNLOADING = {1, 2, 3, 6}
_STATUS_ERROR = {0, 7, 8, 9}


def get_api_key() -> str:
    env_key = os.environ.get('ALLDEBRID_API_KEY', '').strip()
    if env_key:
        return env_key
    settings = get_settings()
    return (settings.get('alldebrid_api_key') or '').strip() or DEFAULT_API_KEY


def is_configured() -> bool:
    return bool(get_api_key())


def get_status() -> dict:
    key = get_api_key()
    if not key:
        return {'configured': False, 'username': '', 'is_premium': False}
    try:
        user = _api_request('GET', '/v4/user')['user']
        return {
            'configured': True,
            'username': user.get('username') or '',
            'is_premium': bool(user.get('isPremium')),
            'email': user.get('email') or '',
        }
    except Exception as exc:
        return {
            'configured': False,
            'username': '',
            'is_premium': False,
            'error': str(exc),
        }


def _headers() -> dict:
    return {'Authorization': f'Bearer {get_api_key()}'}


def _api_request(method: str, path: str, *, params=None, data=None, files=None, timeout=60.0):
    url = f'{API_BASE}{path}'
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=20.0)) as client:
        resp = client.request(
            method,
            url,
            headers=_headers(),
            params=params,
            data=data,
            files=files,
        )
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f'AllDebrid returned invalid JSON ({resp.status_code})') from exc

    if payload.get('status') != 'success':
        err = payload.get('error') or {}
        code = err.get('code') or 'ALLDEBRID_ERROR'
        message = err.get('message') or 'AllDebrid request failed'
        raise RuntimeError(f'{code}: {message}')

    return payload.get('data') or {}


def _normalize_magnet(source: str) -> str:
    from urllib.parse import unquote
    u = unquote((source or '').strip().lstrip('\ufeff'))
    if len(u) >= 2 and u[0] == u[-1] and u[0] in '"\'':
        u = u[1:-1].strip()
    magnet = u
    if not magnet.lower().startswith('magnet:'):
        magnet = f'magnet:?xt=urn:btih:{magnet}'
    return magnet


def upload_magnet(source: str) -> dict:
    magnet = _normalize_magnet(source)
    data = _api_request('POST', '/v4/magnet/upload', data={'magnets[]': magnet})
    magnets = data.get('magnets') or []
    if not magnets:
        raise RuntimeError('AllDebrid returned no magnet data')

    entry = magnets[0]
    if entry.get('error'):
        err = entry['error']
        raise RuntimeError(err.get('message') or err.get('code') or 'Magnet upload failed')
    if not entry.get('id'):
        raise RuntimeError('AllDebrid did not return a magnet id')
    return entry


def upload_torrent_file(data: bytes, filename: str = 'upload.torrent') -> dict:
    files = {'files[]': (filename, data, 'application/x-bittorrent')}
    result = _api_request('POST', '/v4/magnet/upload/file', files=files, timeout=120.0)
    magnets = result.get('magnets') or result.get('files') or []
    if isinstance(result.get('magnets'), dict):
        return result['magnets']
    if magnets:
        entry = magnets[0]
        if entry.get('error'):
            err = entry['error']
            raise RuntimeError(err.get('message') or err.get('code') or 'Torrent upload failed')
        return entry
    if result.get('id'):
        return result
    raise RuntimeError('AllDebrid did not accept the torrent file')


def get_magnet_status(magnet_id: int) -> dict:
    data = _api_request('GET', '/v4.1/magnet/status', params={'id': magnet_id})
    magnets = data.get('magnets')
    if isinstance(magnets, list):
        for item in magnets:
            if item.get('id') == magnet_id:
                return item
        return magnets[0] if magnets else {}
    if isinstance(magnets, dict):
        return magnets
    return {}


def list_tasks(limit: int = 50) -> list[dict]:
    if not is_configured():
        return []
    data = _api_request('GET', '/v4.1/magnet/status')
    magnets = data.get('magnets') or []
    if isinstance(magnets, dict):
        magnets = [magnets]
    tasks = [_normalize_task(m) for m in magnets[:limit]]
    return tasks


def _status_progress(status_code: int) -> float:
    if status_code == _STATUS_READY:
        return 100.0
    if status_code in _STATUS_DOWNLOADING:
        return 45.0
    if status_code in _STATUS_ERROR:
        return 0.0
    return 10.0


def _phase_from_code(status_code: int, status_text: str = '') -> str:
    if status_code == _STATUS_READY:
        return 'complete'
    if status_code in _STATUS_ERROR:
        return 'error'
    if status_code in _STATUS_DOWNLOADING:
        return 'running'
    text = (status_text or '').lower()
    if 'ready' in text:
        return 'complete'
    if 'error' in text or 'peer' in text or 'fail' in text:
        return 'error'
    return 'running'


def _normalize_task(magnet: dict) -> dict:
    status_code = int(magnet.get('statusCode') or 0)
    status_text = magnet.get('status') or ''
    phase = _phase_from_code(status_code, status_text)
    progress = _status_progress(status_code)
    if phase == 'running' and status_code == 3:
        progress = 75.0

    return {
        'id': magnet.get('id'),
        'type': 'alldebrid',
        'name': magnet.get('filename') or magnet.get('name') or 'AllDebrid torrent',
        'phase': phase,
        'state': phase,
        'progress': progress,
        'file_size': int(magnet.get('size') or 0),
        'download_rate': 0,
        'upload_rate': 0,
        'peers': 0,
        'source': f'magnet:?xt=urn:btih:{magnet.get("hash")}' if magnet.get('hash') else '',
        'magnet_id': magnet.get('id'),
        'hash': magnet.get('hash'),
        'task_id': magnet.get('id'),
        'error': status_text if phase == 'error' else None,
        'cloud': True,
        'paused': False,
        'status_code': status_code,
    }


def _flatten_alldebrid_files(files: list[dict]) -> list[dict]:
    flat = []
    for item in files:
        nested = item.get('e')
        if nested:
            flat.extend(nested)
        elif item.get('l') or item.get('link'):
            flat.append(item)
    return flat


def _pick_file_entry(
    files: list[dict],
    title_hint: str = '',
    file_index: Optional[int] = None,
) -> Optional[dict]:
    flat = _flatten_alldebrid_files(files)
    if not flat:
        return None

    hint = (title_hint or '').strip()
    if hint:
        hint_lower = hint.lower()
        hint_stem = hint_lower.rsplit('.', 1)[0]
        for item in flat:
            name = (item.get('n') or item.get('filename') or item.get('name') or '').strip()
            name_lower = name.lower()
            if name_lower == hint_lower or name_lower.rsplit('.', 1)[0] == hint_stem:
                return {
                    'name': name,
                    'size': int(item.get('s') or item.get('size') or 0),
                    'link': item.get('l') or item.get('link'),
                }
        for item in flat:
            name = (item.get('n') or item.get('filename') or item.get('name') or '').strip()
            if hint_stem and hint_stem in name.lower():
                return {
                    'name': name,
                    'size': int(item.get('s') or item.get('size') or 0),
                    'link': item.get('l') or item.get('link'),
                }

    if file_index is not None and 0 <= file_index < len(flat):
        item = flat[file_index]
        name = (item.get('n') or item.get('filename') or item.get('name') or '').strip()
        return {
            'name': name,
            'size': int(item.get('s') or item.get('size') or 0),
            'link': item.get('l') or item.get('link'),
        }

    return _pick_video_file(flat)


def _pick_video_file(files: list[dict]) -> Optional[dict]:
    videos = []
    for item in _flatten_alldebrid_files(files):
        name = (item.get('n') or item.get('filename') or item.get('name') or '').strip()
        if not name:
            continue
        ext = '.' + name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        size = int(item.get('s') or item.get('size') or 0)
        if ext in VIDEO_EXTS:
            videos.append((size, item, name))
    if not videos:
        return None
    _, pick, name = max(videos, key=lambda row: row[0])
    return {'name': name, 'size': int(pick.get('s') or pick.get('size') or 0), 'link': pick.get('l') or pick.get('link')}


def _collect_files(magnet: dict) -> list[dict]:
    files = magnet.get('files') or []
    if files:
        return files
    data = _api_request('GET', '/v4/magnet/files', params={'id': magnet.get('id')})
    magnets = data.get('magnets')
    if isinstance(magnets, dict):
        return magnets.get('files') or []
    if isinstance(magnets, list) and magnets:
        return magnets[0].get('files') or []
    return []


def unlock_link(link: str) -> dict:
    return _api_request('POST', '/v4/link/unlock', data={'link': link})


def _content_type_for_name(name: str) -> str:
    ext = name.lower().rsplit('.', 1)[-1] if '.' in name.lower() else 'mp4'
    mime = {
        'mp4': 'video/mp4', 'm4v': 'video/mp4', 'mkv': 'video/x-matroska',
        'webm': 'video/webm', 'avi': 'video/x-msvideo', 'mov': 'video/quicktime',
        'wmv': 'video/x-ms-wmv', 'flv': 'video/x-flv', 'mpeg': 'video/mpeg',
        'mpg': 'video/mpeg', 'ts': 'video/mp2t', 'm2ts': 'video/mp2t',
    }
    return mime.get(ext, 'video/mp4')





def parse_torrentio_url(url: str) -> Optional[dict]:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    host = (parsed.netloc or '').lower()
    if 'strem.fun' not in host:
        return None

    parts = [p for p in parsed.path.split('/') if p]
    if len(parts) < 5 or parts[0] != 'resolve':
        return None

    provider = parts[1].lower()
    if provider != 'alldebrid':
        return None

    title = unquote(parts[4]) if len(parts) > 4 else ''
    file_index = None
    if len(parts) > 5 and parts[5].isdigit():
        file_index = int(parts[5])
    if len(parts) > 6 and not title:
        title = unquote(parts[6])

    return {
        'provider': provider,
        'hash': parts[3].lower(),
        'title': title,
        'file_index': file_index,
    }


def resolve_torrentio_url(url: str) -> Optional[dict]:
    parsed = parse_torrentio_url(url)
    if not parsed or not is_configured():
        return None

    magnet = f'magnet:?xt=urn:btih:{parsed["hash"]}'
    uploaded = upload_magnet(magnet)
    magnet_id = int(uploaded['id'])
    if not uploaded.get('ready'):
        magnet = wait_for_magnet(magnet_id, timeout=300.0)
    else:
        magnet = get_magnet_status(magnet_id)

    files = _collect_files(magnet)
    pick = _pick_file_entry(files, parsed.get('title') or '', parsed.get('file_index'))
    if not pick or not pick.get('link'):
        return None

    unlocked = unlock_link(pick['link'])
    stream_url = unlocked.get('link')
    if not stream_url:
        return None

    title = unlocked.get('filename') or pick['name'] or parsed.get('title') or 'Stremio stream'
    size = int(unlocked.get('filesize') or pick.get('size') or 0)
    ext = title.lower().rsplit('.', 1)[-1] if '.' in title.lower() else 'mp4'

    return {
        'stream_url': stream_url,
        'title': title,
        'filesize': size,
        'ext': ext,
        'content_type': _content_type_for_name(title),
        'requires_mpv': False,
    }


def wait_for_magnet(magnet_id: int, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_magnet_status(magnet_id)
        code = int(last.get('statusCode') or 0)
        if code == _STATUS_READY:
            return last
        if code in _STATUS_ERROR:
            raise RuntimeError(last.get('status') or 'AllDebrid magnet failed')
        time.sleep(3)
    raise TimeoutError(f'AllDebrid download timed out ({last.get("status") if last else "no status"})')


def resolve_magnet(source: str, label: str = 'Magnet link') -> dict:
    uploaded = upload_magnet(source)
    magnet_id = int(uploaded['id'])
    if not uploaded.get('ready'):
        magnet = wait_for_magnet(magnet_id)
    else:
        magnet = get_magnet_status(magnet_id)

    files = _collect_files(magnet)
    pick = _pick_video_file(files)
    if not pick or not pick.get('link'):
        if len(files) == 1:
            only = files[0]
            pick = {
                'name': only.get('n') or only.get('filename') or label,
                'size': int(only.get('s') or only.get('size') or 0),
                'link': only.get('l') or only.get('link'),
            }
    if not pick or not pick.get('link'):
        raise RuntimeError('No playable video found in AllDebrid torrent')

    unlocked = unlock_link(pick['link'])
    stream_url = unlocked.get('link')
    if not stream_url:
        raise RuntimeError('AllDebrid did not return a download link')

    title = unlocked.get('filename') or pick['name'] or label
    size = int(unlocked.get('filesize') or pick.get('size') or 0)
    ext = title.lower().rsplit('.', 1)[-1] if '.' in title.lower() else 'mp4'

    return {
        'type': 'video',
        'title': title,
        'url': source,
        'thumbnail': None,
        'duration': None,
        'site': 'alldebrid',
        'stream_url': stream_url,
        'stream_type': 'progressive',
        'content_type': _content_type_for_name(title),
        'formats': [{
            'format_id': 'alldebrid',
            'ext': ext,
            'quality': 'AllDebrid',
            'resolution': 'source',
            'filesize': size,
            'url': stream_url,
        }],
        'best_format_id': 'alldebrid',
        'subtitles': [],
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://alldebrid.com/',
            'Accept': '*/*',
        },
        'resolved_with': 'alldebrid-cloud',
        'alldebrid_magnet_id': magnet_id,
        'requires_mpv': False,
    }


def resolve_torrent_bytes(data: bytes, filename: str = 'upload.torrent') -> dict:
    uploaded = upload_torrent_file(data, filename)
    magnet_id = int(uploaded['id'])
    if not uploaded.get('ready'):
        magnet = wait_for_magnet(magnet_id)
    else:
        magnet = get_magnet_status(magnet_id)

    source = f'magnet:?xt=urn:btih:{magnet.get("hash") or uploaded.get("hash")}'
    files = _collect_files(magnet)
    pick = _pick_video_file(files)
    if not pick or not pick.get('link'):
        if len(files) == 1:
            only = files[0]
            pick = {
                'name': only.get('n') or filename,
                'size': int(only.get('s') or 0),
                'link': only.get('l') or only.get('link'),
            }
    if not pick or not pick.get('link'):
        raise RuntimeError('No playable video found in torrent')

    unlocked = unlock_link(pick['link'])
    stream_url = unlocked.get('link')
    if not stream_url:
        raise RuntimeError('AllDebrid did not return a download link')

    title = unlocked.get('filename') or pick['name'] or filename
    size = int(unlocked.get('filesize') or pick.get('size') or 0)
    ext = title.lower().rsplit('.', 1)[-1] if '.' in title.lower() else 'mp4'

    return {
        'type': 'video',
        'title': title,
        'url': source or filename,
        'thumbnail': None,
        'duration': None,
        'site': 'alldebrid',
        'stream_url': stream_url,
        'stream_type': 'progressive',
        'content_type': _content_type_for_name(title),
        'formats': [{
            'format_id': 'alldebrid',
            'ext': ext,
            'quality': 'AllDebrid',
            'resolution': 'source',
            'filesize': size,
            'url': stream_url,
        }],
        'best_format_id': 'alldebrid',
        'subtitles': [],
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://alldebrid.com/',
            'Accept': '*/*',
        },
        'resolved_with': 'alldebrid-cloud',
        'alldebrid_magnet_id': magnet_id,
        'requires_mpv': False,
    }