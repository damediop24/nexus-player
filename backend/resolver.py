import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp

ROOT = Path(__file__).parent.parent
DOWNLOADS = ROOT / 'downloads'
DOWNLOADS.mkdir(exist_ok=True)

BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)

HAS_CURL_CFFI = bool(importlib.util.find_spec('curl_cffi'))


def _ffmpeg_path():
    path = shutil.which('ffmpeg')
    if path:
        return path
    winget = Path.home() / 'AppData' / 'Local' / 'Microsoft' / 'WinGet' / 'Packages'
    candidates = [
        Path(r'C:\ffmpeg\bin\ffmpeg.exe'),
        Path.home() / 'scoop' / 'shims' / 'ffmpeg.exe',
        Path(r'C:\Program Files\MPV Player\ffmpeg.exe'),
    ]
    if winget.exists():
        for ff in winget.glob('**/ffmpeg.exe'):
            candidates.insert(0, ff)
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _detect_browsers():
    found = []
    local = Path.home() / 'AppData' / 'Local'
    checks = [
        ('chrome', local / 'Google' / 'Chrome' / 'User Data'),
        ('edge', local / 'Microsoft' / 'Edge' / 'User Data'),
        ('firefox', Path.home() / 'AppData' / 'Roaming' / 'Mozilla' / 'Firefox' / 'Profiles'),
        ('brave', local / 'BraveSoftware' / 'Brave-Browser' / 'User Data'),
        ('opera', local / 'Opera Software' / 'Opera Stable'),
    ]
    for name, path in checks:
        if path.exists():
            found.append(name)
    return found or ['chrome']


def _build_strategies():
    strategies = [
        {'label': 'default', 'impersonate': None, 'cookies': None},
    ]

    if HAS_CURL_CFFI:
        for target in ('chrome', 'edge', 'firefox'):
            strategies.append({'label': f'impersonate-{target}', 'impersonate': target, 'cookies': None})

    for browser in _detect_browsers():
        strategies.append({'label': f'cookies-{browser}', 'impersonate': None, 'cookies': browser})
        if HAS_CURL_CFFI:
            strategies.append({
                'label': f'cookies+impersonate-{browser}',
                'impersonate': 'chrome',
                'cookies': browser,
            })

    seen = set()
    unique = []
    for s in strategies:
        key = (s['impersonate'], s['cookies'])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def _base_opts(impersonate=None, cookies_browser=None):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 5,
        'fragment_retries': 5,
        'extract_flat': False,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': BROWSER_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Mode': 'navigate',
        },
        'extractor_args': {
            'generic': {'impersonate': [impersonate or 'chrome']},
            'youtube': {'player_client': ['android', 'web', 'tv_embedded']},
        },
    }

    if impersonate and HAS_CURL_CFFI:
        opts['impersonate'] = impersonate

    if cookies_browser:
        try:
            opts['cookiesfrombrowser'] = (cookies_browser,)
        except Exception:
            pass

    ff = _ffmpeg_path()
    if ff:
        opts['ffmpeg_location'] = str(Path(ff).parent)

    return opts


def _is_retriable(exc):
    msg = str(exc).lower()
    return any(x in msg for x in (
        '403', 'forbidden', '429', 'too many requests',
        'unable to download webpage', 'sign in', 'login',
        'confirm your age', 'bot', 'captcha', 'cloudflare',
    ))


def _extract(url, format_id=None, impersonate=None, cookies_browser=None):
    opts = _base_opts(impersonate, cookies_browser)
    if format_id:
        opts['format'] = format_id
    else:
        opts['format'] = (
            'best[ext=mp4][acodec!=none][vcodec!=none]/'
            'best[ext=webm][acodec!=none][vcodec!=none]/'
            'best[ext=mkv][acodec!=none][vcodec!=none]/'
            'best[ext=mp4]/best[ext=webm]/best[ext=mkv]/best'
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _format_entry(f):
    return {
        'format_id': f.get('format_id'),
        'ext': f.get('ext'),
        'quality': f.get('format_note') or f.get('quality') or '',
        'resolution': f.get('resolution') or ('audio only' if f.get('vcodec') == 'none' else 'unknown'),
        'filesize': f.get('filesize') or f.get('filesize_approx'),
        'vcodec': f.get('vcodec'),
        'acodec': f.get('acodec'),
        'fps': f.get('fps'),
        'tbr': f.get('tbr'),
        'protocol': f.get('protocol'),
        'url': f.get('url'),
        'manifest_url': f.get('manifest_url'),
    }


def _subtitle_entries(info):
    subs = []
    subtitles = info.get('subtitles') or {}
    auto = info.get('automatic_captions') or {}

    for lang, tracks in subtitles.items():
        for t in tracks:
            subs.append({
                'lang': lang,
                'name': t.get('name') or lang,
                'url': t.get('url'),
                'ext': t.get('ext', 'vtt'),
                'auto': False,
            })

    for lang, tracks in auto.items():
        for t in tracks:
            subs.append({
                'lang': lang,
                'name': f'{lang} (auto)',
                'url': t.get('url'),
                'ext': t.get('ext', 'vtt'),
                'auto': True,
            })

    return subs


def _build_response(info, url):
    if info.get('_type') == 'playlist':
        entries = []
        for entry in info.get('entries') or []:
            if not entry:
                continue
            entries.append({
                'id': entry.get('id'),
                'title': entry.get('title'),
                'url': entry.get('url') or entry.get('webpage_url'),
                'thumbnail': entry.get('thumbnail'),
                'duration': entry.get('duration'),
            })
        return {
            'type': 'playlist',
            'title': info.get('title'),
            'entries': entries,
            'url': url,
        }

    formats = []
    for f in info.get('formats') or []:
        if f.get('url') or f.get('manifest_url'):
            formats.append(_format_entry(f))

    def _format_score(f):
        score = 0
        if f.get('acodec') and f['acodec'] != 'none':
            score += 10000
        if f.get('vcodec') and f['vcodec'] != 'none':
            score += 10000
        if f.get('ext') in ('mp4', 'webm', 'mkv'):
            score += 5000
        if (f.get('vcodec') or '').startswith(('avc', 'vp9', 'vp8', 'hvc', 'hev')):
            score += 2000
        proto = f.get('protocol') or ''
        url_l = (f.get('url') or '').lower()
        if 'm3u8' in url_l or 'm3u8' in proto:
            score -= 2000
        if '.mpd' in url_l or 'dash' in proto:
            score -= 1500
        score += (f.get('filesize') or 0) / 1000
        return score

    best = max(formats, key=_format_score) if formats else None

    stream_url = None
    stream_type = 'progressive'
    if best:
        stream_url = best.get('url') or best.get('manifest_url')
        ext = (best.get('ext') or '').lower()
        if ext in ('m3u8', 'mpd') or 'm3u8' in (stream_url or ''):
            stream_type = 'hls' if 'm3u8' in (stream_url or '') or ext == 'm3u8' else 'dash'

    headers = dict(info.get('http_headers') or {})
    if 'User-Agent' not in headers:
        headers['User-Agent'] = BROWSER_UA
    if info.get('webpage_url') and 'Referer' not in headers:
        headers['Referer'] = info['webpage_url']

    return {
        'type': 'video',
        'id': info.get('id'),
        'title': info.get('title') or 'Untitled',
        'url': info.get('webpage_url') or url,
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
        'site': info.get('extractor_key') or info.get('extractor'),
        'description': info.get('description'),
        'uploader': info.get('uploader'),
        'view_count': info.get('view_count'),
        'formats': formats[:40],
        'best_format_id': best['format_id'] if best else None,
        'stream_url': stream_url,
        'stream_type': stream_type,
        'subtitles': _subtitle_entries(info),
        'headers': headers,
    }


_MEDIA_EXTENSIONS = (
    '.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.flv', '.wmv', '.ogv',
    '.3gp', '.ts', '.m3u8', '.mpd', '.mp3', '.m4a', '.aac', '.ogg', '.wav', '.flac',
)

_CDN_DOWNLOAD_HOSTS = (
    'mypikpak.com',
    'pikpak.com',
    'pikpakdrive.com',
)

_EXT_FROM_MIME = {
    'video/mp4': 'mp4',
    'video/webm': 'webm',
    'video/x-matroska': 'mkv',
    'video/quicktime': 'mov',
    'video/x-msvideo': 'avi',
    'video/ogg': 'ogv',
    'audio/mpeg': 'mp3',
    'audio/mp4': 'm4a',
    'audio/aac': 'aac',
    'audio/ogg': 'ogg',
    'audio/wav': 'wav',
    'audio/flac': 'flac',
    'application/vnd.apple.mpegurl': 'm3u8',
    'application/dash+xml': 'mpd',
}

_MIME_FROM_EXT = {
    'mp4': 'video/mp4',
    'webm': 'video/webm',
    'mkv': 'video/x-matroska',
    'mov': 'video/quicktime',
    'avi': 'video/x-msvideo',
    'm4v': 'video/mp4',
    'ogv': 'video/ogg',
    'm3u8': 'application/vnd.apple.mpegurl',
    'mpd': 'application/dash+xml',
    'mp3': 'audio/mpeg',
    'm4a': 'audio/mp4',
    'aac': 'audio/aac',
    'ogg': 'audio/ogg',
    'wav': 'audio/wav',
    'flac': 'audio/flac',
}


def _is_direct_media(url):
    lower = url.lower().split('?')[0]
    return lower.endswith(_MEDIA_EXTENSIONS)


def _looks_like_cdn_download(url):
    lower = url.lower()
    host = urlparse(url).netloc.lower()
    if any(h in host for h in _CDN_DOWNLOAD_HOSTS):
        return True
    if '/download' in lower and ('?' in url or lower.rstrip('/').endswith('/download')):
        return True
    if 'response-content-type=video' in lower or 'content-type=video' in lower:
        return True
    return False


def _referer_for_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if 'mypikpak.com' in host or 'pikpak' in host:
        return 'https://mypikpak.com/'
    if parsed.scheme and parsed.netloc:
        return f'{parsed.scheme}://{parsed.netloc}/'
    return url


def _filename_from_disposition(value):
    if not value:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, re.I)
    if match:
        from urllib.parse import unquote
        return unquote(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', value, re.I)
    return match.group(1) if match else None


def _sniff_media_ext(data: bytes):
    if len(data) >= 12 and data[4:8] == b'ftyp':
        brand = data[8:12].decode('ascii', errors='ignore').lower()
        if brand.startswith('m4'):
            return 'm4a' if brand in ('m4a ', 'm4b ') else 'mp4'
        return 'mp4'
    if data.startswith(b'\x1a\x45\xdf\xa3'):
        return 'mkv'
    if data.startswith(b'\x1f\x8b'):
        return None
    if len(data) >= 4 and data[:4] == b'RIFF' and len(data) >= 12 and data[8:12] == b'WAVE':
        return 'wav'
    if data.startswith(b'ID3') or data[:2] == b'\xff\xfb':
        return 'mp3'
    if data.startswith(b'OggS'):
        return 'ogg'
    if data.startswith(b'fLaC'):
        return 'flac'
    if len(data) >= 4 and data[:4] == b'\x1aE\xdf\xa3':
        return 'webm'
    return None


def _ext_from_content_type(content_type):
    if not content_type:
        return None
    mime = content_type.split(';')[0].strip().lower()
    if mime in _EXT_FROM_MIME:
        return _EXT_FROM_MIME[mime]
    if mime.startswith('video/'):
        return mime.split('/', 1)[1]
    if mime.startswith('audio/'):
        return mime.split('/', 1)[1]
    return None


def _probe_direct_url(url):
    headers = {
        'User-Agent': BROWSER_UA,
        'Accept': '*/*',
        'Referer': _referer_for_url(url),
        'Range': 'bytes=0-511',
    }

    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20.0, read=20.0)) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code not in (200, 206):
                resp = client.head(
                    url,
                    headers={k: v for k, v in headers.items() if k.lower() != 'range'},
                )
                if resp.status_code >= 400:
                    return None
                body = b''
            else:
                body = resp.content

            content_type = (resp.headers.get('content-type') or '').split(';')[0].strip().lower()
            content_length = resp.headers.get('content-length')
            try:
                filesize = int(content_length) if content_length else None
            except ValueError:
                filesize = None

            filename = _filename_from_disposition(resp.headers.get('content-disposition'))
            ext = None
            if filename and '.' in filename:
                ext = filename.rsplit('.', 1)[-1].lower()

            if not ext:
                ext = _ext_from_content_type(content_type)

            if not ext and body:
                ext = _sniff_media_ext(body)

            if not ext and content_type in ('binary/octet-stream', 'application/octet-stream', 'application/download'):
                ext = _sniff_media_ext(body)

            if not ext:
                return None

            if ext == 'm3u8':
                stream_type = 'hls'
            elif ext == 'mpd':
                stream_type = 'dash'
            else:
                stream_type = 'progressive'

            title = filename or urlparse(url).netloc or 'Direct stream'
            mime = _MIME_FROM_EXT.get(ext) or (
                content_type if content_type and content_type != 'binary/octet-stream'
                and content_type != 'application/octet-stream' else None
            )

            return {
                'ext': ext,
                'stream_type': stream_type,
                'title': title,
                'filesize': filesize,
                'content_type': mime or _MIME_FROM_EXT.get(ext, 'video/mp4'),
                'headers': {
                    'User-Agent': BROWSER_UA,
                    'Referer': _referer_for_url(url),
                    'Accept': '*/*',
                },
            }
    except Exception:
        return None


def _direct_media_response(url, probe=None):
    if probe:
        ext = probe['ext']
        stream_type = probe['stream_type']
        title = probe['title']
        filesize = probe.get('filesize')
        headers = probe.get('headers') or {'User-Agent': BROWSER_UA, 'Referer': _referer_for_url(url)}
        content_type = probe.get('content_type')
    else:
        ext = url.lower().split('?')[0].rsplit('.', 1)[-1]
        stream_type = 'hls' if ext == 'm3u8' else 'dash' if ext == 'mpd' else 'progressive'
        title = url.split('/')[-1].split('?')[0] or 'Direct stream'
        filesize = None
        headers = {'User-Agent': BROWSER_UA, 'Referer': _referer_for_url(url)}
        content_type = _MIME_FROM_EXT.get(ext)

    host = urlparse(url).netloc.lower()
    site = 'pikpak' if 'pikpak' in host else 'direct'

    return {
        'type': 'video',
        'title': title,
        'url': url,
        'thumbnail': None,
        'duration': None,
        'site': site,
        'formats': [{
            'format_id': 'direct',
            'ext': ext,
            'quality': 'direct',
            'resolution': 'source',
            'filesize': filesize,
            'url': url,
        }],
        'best_format_id': 'direct',
        'stream_url': url,
        'stream_type': stream_type,
        'content_type': content_type,
        'subtitles': [],
        'headers': headers,
    }


def resolve_url(url, format_id=None):
    if _is_direct_media(url):
        return _direct_media_response(url)

    if _looks_like_cdn_download(url):
        probe = _probe_direct_url(url)
        if probe:
            return _direct_media_response(url, probe)

    last_error = None

    for strategy in _build_strategies():
        try:
            info = _extract(
                url,
                format_id,
                impersonate=strategy['impersonate'],
                cookies_browser=strategy['cookies'],
            )
            result = _build_response(info, url)
            if strategy['label'] != 'default':
                result['resolved_with'] = strategy['label']
            return result
        except Exception as exc:
            last_error = exc
            if not _is_retriable(exc):
                raise
            continue

    hint = (
        'Site blocked the request (403). Try: paste a direct video link (.mp4/.m3u8), '
        'use the MPV button, or make sure you are logged into the site in Chrome/Edge.'
    )
    raise RuntimeError(f'{last_error}  —  {hint}')


def download_media(url, format_id=None, on_progress=None):
    outtmpl = str(DOWNLOADS / '%(title).200B [%(id)s].%(ext)s')

    def hook(d):
        if on_progress and d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes') or 0
            pct = (downloaded / total * 100) if total else 0
            on_progress(pct, d.get('filename', ''))
        elif on_progress and d.get('status') == 'finished':
            on_progress(100, d.get('filename', ''))

    last_error = None
    for strategy in _build_strategies():
        try:
            opts = _base_opts(strategy['impersonate'], strategy['cookies'])
            opts.update({
                'outtmpl': outtmpl,
                'progress_hooks': [hook],
                'format': format_id or 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best/best',
                'merge_output_format': 'mp4',
                'writethumbnail': True,
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
            return {
                'title': info.get('title'),
                'filepath': filepath,
                'thumbnail': info.get('thumbnail'),
            }
        except Exception as exc:
            last_error = exc
            if not _is_retriable(exc):
                raise
            continue

    raise RuntimeError(str(last_error))


def find_mpv():
    path = shutil.which('mpv')
    if path:
        return path
    candidates = [
        Path(r'C:\Program Files\MPV Player\mpv.exe'),
        Path(r'C:\mpv\mpv\mpv.exe'),
        Path(r'C:\Program Files\mpv\mpv.exe'),
        Path(r'C:\Program Files (x86)\mpv\mpv.exe'),
        Path.home() / 'AppData' / 'Local' / 'Microsoft' / 'WinGet' / 'Links' / 'mpv.exe',
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def launch_mpv(url, title=None, headers=None):
    mpv = find_mpv()
    if not mpv:
        raise FileNotFoundError('MPV not found')

    args = [mpv, '--force-window=immediate', f'--title={title or "Nexus Player"}']

    if headers:
        if headers.get('User-Agent'):
            args.append(f'--user-agent={headers["User-Agent"]}')
        if headers.get('Referer'):
            args.append(f'--referrer={headers["Referer"]}')

    args.append(url)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return mpv