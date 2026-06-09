import importlib.util
import shutil
import subprocess
from pathlib import Path

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


def _is_direct_media(url):
    lower = url.lower().split('?')[0]
    return lower.endswith((
        '.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.flv', '.wmv', '.ogv',
        '.3gp', '.ts', '.m3u8', '.mpd', '.mp3', '.m4a', '.aac', '.ogg', '.wav', '.flac',
    ))


def _direct_media_response(url):
    ext = url.lower().split('?')[0].rsplit('.', 1)[-1]
    stream_type = 'hls' if ext == 'm3u8' else 'dash' if ext == 'mpd' else 'progressive'
    return {
        'type': 'video',
        'title': url.split('/')[-1].split('?')[0] or 'Direct stream',
        'url': url,
        'thumbnail': None,
        'duration': None,
        'site': 'direct',
        'formats': [{'format_id': 'direct', 'ext': ext, 'quality': 'direct', 'resolution': 'source', 'url': url}],
        'best_format_id': 'direct',
        'stream_url': url,
        'stream_type': stream_type,
        'subtitles': [],
        'headers': {'User-Agent': BROWSER_UA, 'Referer': url},
    }


def resolve_url(url, format_id=None):
    if _is_direct_media(url):
        return _direct_media_response(url)

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