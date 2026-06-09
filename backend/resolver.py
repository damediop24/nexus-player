import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

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
_IMPERSONATE_TARGETS = None

_KVS_VIDEO_URL_PATTERNS = (
    re.compile(r"video_url\s*:\s*'([^']+)'", re.I),
    re.compile(r'video_url\s*:\s*"([^"]+)"', re.I),
    re.compile(r"video_alt_url\s*:\s*'([^']+)'", re.I),
    re.compile(r'video_alt_url\s*:\s*"([^"]+)"', re.I),
)

_KVS_SKIP_HOSTS = (
    'youtube.com', 'youtu.be', 'm.youtube.com', 'music.youtube.com',
    'vimeo.com', 'dailymotion.com', 'twitch.tv', 'facebook.com',
    'instagram.com', 'twitter.com', 'x.com', 'tiktok.com', 'reddit.com',
)

_YOUTUBE_HOSTS = (
    'youtube.com', 'youtu.be', 'm.youtube.com', 'music.youtube.com',
)

_URL_IN_TEXT_RE = re.compile(r'https?://[^\s<>"\']+|magnet:\?[^\s<>"\']+', re.I)

_EROME_MP4_RE = re.compile(r'https?://v\d+\.erome\.com/[^\s"\'<>]+\.mp4', re.I)


class ResolveError(Exception):
    def __init__(self, message, code='resolve_failed', hint=None, retriable=False, site=None):
        self.message = message
        self.code = code
        self.hint = hint
        self.retriable = retriable
        self.site = site
        super().__init__(message)

    def to_dict(self):
        return {
            'error': self.message,
            'code': self.code,
            'hint': self.hint,
            'retriable': self.retriable,
            'site': self.site,
        }


def _host_key(netloc):
    host = (netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host


def _is_youtube_url(url):
    return _host_key(urlparse(url).netloc) in _YOUTUBE_HOSTS


def _is_erome_host(url):
    host = _host_key(urlparse(url).netloc)
    return host == 'erome.com' or host.endswith('.erome.com')


def _is_erome_page_url(url):
    if not _is_erome_host(url):
        return False
    host = _host_key(urlparse(url).netloc)
    if re.match(r'^v\d+\.', host):
        return False
    return True


def normalize_play_url(url):
    from torrent import normalize_url

    url = normalize_url(url)
    if not url:
        return url

    if not url.startswith(('http://', 'https://', 'magnet:')):
        match = _URL_IN_TEXT_RE.search(url)
        if match:
            url = match.group(0).rstrip('.,;:!?)]}')

    parsed = urlparse(url)
    host = _host_key(parsed.netloc)

    if host in _YOUTUBE_HOSTS or host.endswith('.youtube.com'):
        vid = None
        qs = parse_qs(parsed.query)
        if qs.get('v'):
            vid = qs['v'][0]
        if not vid:
            for pattern in (
                r'^/embed/([\w-]{6,})',
                r'^/shorts/([\w-]{6,})',
                r'^/v/([\w-]{6,})',
                r'^/live/([\w-]{6,})',
            ):
                match = re.match(pattern, parsed.path)
                if match:
                    vid = match.group(1)
                    break
        if not vid and host == 'youtu.be':
            vid = parsed.path.lstrip('/').split('/')[0] or None
        if vid:
            return f'https://www.youtube.com/watch?v={vid}'

    return url


def _cookies_file_path():
    path = os.environ.get('YTDLP_COOKIES_FILE', '').strip()
    if path and Path(path).is_file():
        return path
    for candidate in (ROOT / 'cookies.txt', Path('/data/cookies.txt')):
        if candidate.is_file():
            return str(candidate)
    return None


def _classify_resolve_error(exc, url):
    msg = str(exc)
    lower = msg.lower()
    site = 'youtube' if _is_youtube_url(url) else _host_key(urlparse(url).netloc) or None

    if _is_youtube_url(url):
        if any(x in lower for x in (
            'sign in', 'login', 'confirm your age', 'confirm you', 'not a bot',
            'members only', 'private video', 'not available', 'bot', 'captcha',
        )):
            return ResolveError(
                msg,
                code='youtube_auth_required',
                hint=(
                    'YouTube blocked cloud resolve. On your PC: log into YouTube in Chrome, '
                    'run start-mpv-bridge.vbs, then play again. '
                    'For cloud: upload cookies.txt and set YTDLP_COOKIES_FILE=/data/cookies.txt on Render.'
                ),
                retriable=True,
                site='youtube',
            )
        if any(x in lower for x in ('403', 'forbidden', 'blocked', 'unable to extract')):
            return ResolveError(
                msg,
                code='youtube_blocked',
                hint='YouTube blocked this server. Use the MPV bridge on your PC or add cookies.txt to the server.',
                retriable=True,
                site='youtube',
            )

    if any(x in lower for x in ('unsupported url', 'no suitable extractor', 'no video formats')):
        return ResolveError(
            msg,
            code='unsupported',
            hint='Try a direct .mp4/.m3u8 link, or open the page URL instead of an embed.',
            retriable=False,
            site=site,
        )

    if any(x in lower for x in ('403', 'forbidden', 'blocked this server', 'cloud servers')):
        return ResolveError(
            msg,
            code='site_blocked',
            hint='Site blocked cloud servers. Use MPV bridge or paste a direct media link.',
            retriable=True,
            site=site,
        )

    if any(x in lower for x in ('timed out', 'timeout', 'connection', 'network', 'name or service not known')):
        return ResolveError(
            msg,
            code='network_error',
            hint='Network error — check the URL and try again.',
            retriable=True,
            site=site,
        )

    if _is_stremio_resolver(url):
        url_lower = url.lower()
        hint = (
            'Torrentio/debrid link may be expired, or the cloud server was blocked. '
            'Get a fresh link from Stremio, or use MPV for .mkv/.avi files.'
        )
        if any(x in url_lower for x in ('.mkv', 'x265', 'hevc', '.avi', '.wmv', '.flv')):
            hint = (
                'MKV/AVI/WMV often cannot play in the browser — click MPV to play. '
                'If resolve failed, refresh the Torrentio link or run start-mpv-bridge.vbs on your PC.'
            )
        return ResolveError(
            msg,
            code='stremio_failed',
            hint=hint,
            retriable=True,
            site='stremio',
        )

    return ResolveError(
        msg,
        code='resolve_failed',
        hint='Try another quality, MPV, or a direct stream link (.mp4 / .m3u8).',
        retriable=True,
        site=site,
    )


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


def _available_impersonate_targets():
    global _IMPERSONATE_TARGETS
    if _IMPERSONATE_TARGETS is not None:
        return _IMPERSONATE_TARGETS

    targets = []
    if HAS_CURL_CFFI:
        try:
            result = subprocess.run(
                ['yt-dlp', '--list-impersonate-targets'],
                capture_output=True,
                text=True,
                timeout=20,
            )
            output = (result.stdout or '') + (result.stderr or '')
            for line in output.splitlines():
                if 'unavailable' in line.lower():
                    continue
                match = re.match(r'^(\w+)\s+-\s+curl_cffi', line.strip(), re.I)
                if match:
                    targets.append(match.group(1).lower())
        except Exception:
            pass

    _IMPERSONATE_TARGETS = targets
    return targets


def _pick_impersonate_target(preferred=None):
    if not preferred:
        return None
    available = _available_impersonate_targets()
    preferred = preferred.lower()
    if preferred in available:
        return preferred
    return None


def _browser_cookie_db(profile_root: Path, browser: str):
    if not profile_root.is_dir():
        return None

    candidates = []
    if browser in ('chrome', 'edge', 'brave', 'opera'):
        candidates.extend([
            profile_root / 'Default' / 'Cookies',
            profile_root / 'Default' / 'Network' / 'Cookies',
        ])
        candidates.extend(profile_root.glob('Profile */Cookies'))
        candidates.extend(profile_root.glob('Profile */Network/Cookies'))
    elif browser == 'firefox':
        if profile_root.name.lower() == 'profiles':
            candidates.extend(profile_root.glob('*/cookies.sqlite'))
        else:
            candidates.extend(profile_root.glob('Profiles/*/cookies.sqlite'))

    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def _detect_browsers():
    found = []
    home = Path.home()
    local = home / 'AppData' / 'Local'
    checks = [
        ('chrome', (
            local / 'Google' / 'Chrome' / 'User Data',
            home / '.config' / 'google-chrome',
        )),
        ('edge', (
            local / 'Microsoft' / 'Edge' / 'User Data',
            home / '.config' / 'microsoft-edge',
        )),
        ('firefox', (
            home / 'AppData' / 'Roaming' / 'Mozilla' / 'Firefox' / 'Profiles',
            home / '.mozilla' / 'firefox',
        )),
        ('brave', (
            local / 'BraveSoftware' / 'Brave-Browser' / 'User Data',
            home / '.config' / 'BraveSoftware' / 'Brave-Browser',
        )),
        ('opera', (
            local / 'Opera Software' / 'Opera Stable',
            home / '.config' / 'opera',
        )),
    ]
    for name, paths in checks:
        for path in paths:
            if _browser_cookie_db(path, name):
                found.append(name)
                break
    return found


def _build_strategies():
    strategies = []
    cookies_file = _cookies_file_path()
    if cookies_file:
        strategies.append({'label': 'cookies-file', 'impersonate': None, 'cookies': None, 'cookiefile': cookies_file})

    strategies.append({'label': 'default', 'impersonate': None, 'cookies': None, 'cookiefile': None})

    for target in _available_impersonate_targets():
        strategies.append({'label': f'impersonate-{target}', 'impersonate': target, 'cookies': None, 'cookiefile': None})

    preferred_impersonate = _pick_impersonate_target('chrome')
    for browser in _detect_browsers():
        strategies.append({'label': f'cookies-{browser}', 'impersonate': None, 'cookies': browser, 'cookiefile': None})
        if preferred_impersonate:
            strategies.append({
                'label': f'cookies+impersonate-{browser}',
                'impersonate': preferred_impersonate,
                'cookies': browser,
                'cookiefile': None,
            })

    seen = set()
    unique = []
    for s in strategies:
        key = (s['impersonate'], s['cookies'], s.get('cookiefile'))
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def _base_opts(impersonate=None, cookies_browser=None, cookiefile=None):
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
            'youtube': {
                'player_client': ['ios', 'android', 'mweb', 'web', 'tv_embedded'],
                'player_skip': ['webpage', 'configs'],
            },
            'generic': {'impersonate': []},
        },
    }

    target = _pick_impersonate_target(impersonate)
    if target:
        opts['impersonate'] = target
        opts['extractor_args']['generic'] = {'impersonate': [target]}

    if cookies_browser and cookies_browser in _detect_browsers():
        opts['cookiesfrombrowser'] = (cookies_browser,)

    cookie_path = cookiefile or _cookies_file_path()
    if cookie_path:
        opts['cookiefile'] = cookie_path

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
        'impersonate target', 'is not available', 'flashvars',
        'unable to extract', 'cookies database', 'cookiesfrombrowser',
        'could not find chrome', 'could not find edge', 'could not find firefox',
    ))


def _should_try_kvs_player(url):
    host = _host_key(urlparse(url).netloc)
    if any(host == h or host.endswith('.' + h) for h in _KVS_SKIP_HOSTS):
        return False
    path = urlparse(url).path.lower()
    return bool(re.search(r'/video/\d+', path)) or '/embed/' in path


def _is_likely_kvs_site(url):
    path = urlparse(url).path.lower()
    return bool(re.search(r'/video/\d+', path))


def _resolve_kvs_player(url):
    page_url = url
    parsed = urlparse(url)
    site_origin = f'{parsed.scheme}://{parsed.netloc}/'
    headers = {
        'User-Agent': BROWSER_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': site_origin,
        'Origin': f'{parsed.scheme}://{parsed.netloc}',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }

    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=30.0)) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 403:
            raise RuntimeError('Site blocked this server (403)')
        resp.raise_for_status()
        html = resp.text

    if 'kt_player' not in html.lower() and not any(p.search(html) for p in _KVS_VIDEO_URL_PATTERNS):
        raise RuntimeError('Not a KVS player page')

    stream_url = None
    for pattern in _KVS_VIDEO_URL_PATTERNS:
        match = pattern.search(html)
        if match:
            stream_url = match.group(1).replace('\\/', '/')
            break
    if not stream_url:
        raise RuntimeError('KVS video_url not found')
    if stream_url.startswith('//'):
        stream_url = f'{parsed.scheme}:{stream_url}'
    elif stream_url.startswith('/'):
        stream_url = urljoin(page_url, stream_url)

    title_match = re.search(r'<title>([^<]+)</title>', html, re.I)
    title = title_match.group(1).strip() if title_match else 'Video'
    title = re.sub(r'\s*[-|]\s*[^-|]+$', '', title).strip() or title

    duration_match = re.search(r'video_duration\s*:\s*(\d+)', html, re.I)
    duration = int(duration_match.group(1)) if duration_match else None

    poster_match = (
        re.search(r"poster_url\s*:\s*'([^']+)'", html, re.I)
        or re.search(r'poster_url\s*:\s*"([^"]+)"', html, re.I)
    )
    thumbnail = poster_match.group(1).replace('\\/', '/') if poster_match else None

    probe = _probe_direct_url(stream_url) or {
        'ext': 'mp4',
        'stream_type': 'progressive',
        'title': title,
        'filesize': None,
        'content_type': 'video/mp4',
        'headers': {
            'User-Agent': BROWSER_UA,
            'Referer': page_url,
            'Accept': '*/*',
        },
    }
    probe['title'] = title
    probe['headers']['Referer'] = page_url

    result = _direct_media_response(stream_url, probe)
    result['url'] = page_url
    result['title'] = title
    result['duration'] = duration
    result['thumbnail'] = thumbnail
    result['site'] = urlparse(page_url).netloc
    result['resolved_with'] = 'kvs-player'
    return result


def _extract(url, format_id=None, impersonate=None, cookies_browser=None, cookiefile=None):
    opts = _base_opts(impersonate, cookies_browser, cookiefile)
    if format_id:
        opts['format'] = format_id
    else:
        opts['format'] = (
            'bestvideo[height<=2160][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a]/'
            'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=2160]+bestaudio/'
            'best[height<=2160][ext=mp4][acodec!=none][vcodec!=none]/'
            'best[height<=2160][ext=webm][acodec!=none][vcodec!=none]/'
            'best[height<=2160]/best'
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

    def _format_height(f):
        height = f.get('height') or 0
        if height:
            return int(height)
        res = str(f.get('resolution') or '')
        if 'x' in res:
            try:
                return int(res.split('x', 1)[1])
            except ValueError:
                pass
        match = re.search(r'(\d{3,4})p', str(f.get('quality') or ''), re.I)
        if match:
            return int(match.group(1))
        return 0

    def _format_score(f):
        score = 0
        if f.get('acodec') and f['acodec'] != 'none':
            score += 10000
        if f.get('vcodec') and f['vcodec'] != 'none':
            score += 10000
        height = _format_height(f)
        score += min(height, 2160) * 50
        if f.get('ext') in ('mp4', 'webm'):
            score += 5000
        elif f.get('ext') == 'mkv':
            score += 2500
        vcodec = (f.get('vcodec') or '').lower()
        if vcodec.startswith(('avc', 'vp9', 'vp8')):
            score += 3000
        elif vcodec.startswith(('hvc', 'hev')):
            score += 1000
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
    '.webm', '.mkv', '.flv', '.vob', '.ogv', '.ogg', '.gifv', '.mng', '.mov', '.avi',
    '.qt', '.wmv', '.yuv', '.rm', '.asf', '.amv', '.mp4', '.m4p', '.m4v', '.mpg',
    '.mp2', '.mpeg', '.mpe', '.svi', '.3gp', '.3g2', '.mxf', '.roq', '.nsv', '.f4v',
    '.f4p', '.f4a', '.f4b', '.mod',
    '.ts', '.m3u8', '.mpd',
    '.mp3', '.m4a', '.aac', '.wav', '.flac',
)

_CDN_DOWNLOAD_HOSTS = (
    'mypikpak.com',
    'pikpak.com',
    'pikpakdrive.com',
    'debrid.it',
    'alldebrid.com',
    'real-debrid.com',
)

_STREMIO_RESOLVER_MARKERS = (
    'torrentio.strem.fun',
    'strem.fun',
)

_EXT_FROM_MIME = {
    'video/mp4': 'mp4',
    'video/webm': 'webm',
    'video/x-matroska': 'mkv',
    'video/quicktime': 'mov',
    'video/x-msvideo': 'avi',
    'video/x-flv': 'flv',
    'video/x-f4v': 'f4v',
    'video/x-ms-wmv': 'wmv',
    'video/x-ms-asf': 'asf',
    'video/mpeg': 'mpeg',
    'video/3gpp': '3gp',
    'video/3gpp2': '3g2',
    'video/ogg': 'ogv',
    'video/dvd': 'vob',
    'video/x-amv': 'amv',
    'video/x-svi': 'svi',
    'application/vnd.rn-realmedia': 'rm',
    'application/mxf': 'mxf',
    'application/x-nsv': 'nsv',
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
    'qt': 'video/quicktime',
    'avi': 'video/x-msvideo',
    'flv': 'video/x-flv',
    'f4v': 'video/x-f4v',
    'f4p': 'video/x-f4v',
    'f4a': 'audio/mp4',
    'f4b': 'audio/mp4',
    'wmv': 'video/x-ms-wmv',
    'asf': 'video/x-ms-asf',
    'm4v': 'video/mp4',
    'm4p': 'video/mp4',
    'mpg': 'video/mpeg',
    'mpeg': 'video/mpeg',
    'mpe': 'video/mpeg',
    'mp2': 'video/mpeg',
    'mod': 'video/mpeg',
    'vob': 'video/dvd',
    'ogv': 'video/ogg',
    'ogg': 'video/ogg',
    'gifv': 'video/mp4',
    'mng': 'video/mpeg',
    'yuv': 'video/raw',
    'rm': 'application/vnd.rn-realmedia',
    'amv': 'video/x-amv',
    'svi': 'video/x-svi',
    '3gp': 'video/3gpp',
    '3g2': 'video/3gpp2',
    'mxf': 'application/mxf',
    'roq': 'video/x-idsoftware-quake',
    'nsv': 'application/x-nsv',
    'm3u8': 'application/vnd.apple.mpegurl',
    'mpd': 'application/dash+xml',
    'mp3': 'audio/mpeg',
    'm4a': 'audio/mp4',
    'aac': 'audio/aac',
    'wav': 'audio/wav',
    'flac': 'audio/flac',
}


def _is_stremio_resolver(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if '/resolve/' not in path:
        return False
    return any(marker in host for marker in _STREMIO_RESOLVER_MARKERS) or host.endswith('.strem.fun')


def _title_from_stremio_url(url):
    from urllib.parse import unquote

    for part in reversed(urlparse(url).path.split('/')):
        if not part:
            continue
        decoded = unquote(part)
        lower = decoded.lower()
        if any(lower.endswith(ext) for ext in _MEDIA_EXTENSIONS):
            return decoded
    return 'Stremio stream'


def _stremio_request_headers(referer=None):
    return {
        'User-Agent': BROWSER_UA,
        'Accept': '*/*',
        'Referer': referer or 'https://torrentio.strem.fun/',
    }


def _url_from_stremio_body(resp):
    content_type = (resp.headers.get('content-type') or '').lower()
    body = resp.text or ''
    if 'json' not in content_type and not body.lstrip().startswith(('{', '[')):
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if isinstance(data, str) and data.startswith('http'):
        return data
    if not isinstance(data, dict):
        return None
    for key in ('url', 'download', 'link', 'href', 'stream'):
        val = data.get(key)
        if isinstance(val, str) and val.startswith('http'):
            return val
    return None


def _resolve_stremio_url(url):
    headers = _stremio_request_headers()
    max_hops = 10
    timeout = httpx.Timeout(20.0, connect=10.0)

    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            current = url
            for _ in range(max_hops):
                with client.stream('GET', current, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get('location')
                        resp.close()
                        if not location:
                            return None
                        current = urljoin(current, location)
                        parsed = urlparse(current)
                        if parsed.scheme and parsed.netloc:
                            headers['Referer'] = f'{parsed.scheme}://{parsed.netloc}/'
                        continue
                    if resp.status_code in (200, 206):
                        if 'strem.fun' in (urlparse(current).netloc or '').lower():
                            resolved = _url_from_stremio_body(resp)
                            if resolved:
                                return resolved
                        return current
                    resp.close()
                    return None
    except Exception:
        pass

    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            with client.stream('GET', url, headers=_stremio_request_headers()) as resp:
                if resp.status_code in (200, 206):
                    final = str(resp.url)
                    if final.startswith('http') and final != url:
                        return final
    except Exception:
        return None
    return None


def _requires_mpv_playback(title='', url='', ext=''):
    text = f'{title} {url}'.lower()
    ext_l = (ext or '').lower().lstrip('.')
    if any(token in text for token in ('x265', 'hevc', 'h265', 'h.265', '10bit', 'hdr10', 'dolby vision')):
        return True
    if ext_l in ('mkv', 'avi', 'wmv', 'flv', 'vob', 'rm', 'rmvb', 'ts', 'm2ts'):
        return True
    if ext_l == 'mkv' or text.endswith('.mkv'):
        return True
    return False


def _resolve_stremio_stream(url):
    title = _title_from_stremio_url(url)
    final_url = None
    torrentio_meta = None

    try:
        from alldebrid import is_configured, resolve_torrentio_url
        if is_configured():
            torrentio_meta = resolve_torrentio_url(url)
            if torrentio_meta:
                final_url = torrentio_meta['stream_url']
                title = torrentio_meta.get('title') or title
    except Exception:
        pass

    if not final_url:
        final_url = _resolve_stremio_url(url)
    if not final_url:
        raise RuntimeError(
            'Stremio resolver failed. The link may be expired or the debrid service is unavailable.'
        )
    path_lower = final_url.lower().split('?')[0]
    ext = path_lower.rsplit('.', 1)[-1] if '.' in path_lower else 'mp4'
    probe = {
        'ext': ext,
        'stream_type': 'progressive',
        'title': title,
        'filesize': None,
        'content_type': _MIME_FROM_EXT.get(ext, 'video/mp4'),
        'headers': {
            'User-Agent': BROWSER_UA,
            'Referer': _referer_for_url(final_url),
            'Accept': '*/*',
            'Origin': urlparse(final_url).scheme + '://' + urlparse(final_url).netloc,
        },
    }

    result = _direct_media_response(final_url, probe)
    result['site'] = 'stremio'
    result['url'] = url
    result['title'] = title
    if torrentio_meta:
        result['requires_mpv'] = torrentio_meta.get('requires_mpv', _requires_mpv_playback(title, url, ext))
        if torrentio_meta.get('filesize'):
            result['formats'][0]['filesize'] = torrentio_meta['filesize']
    else:
        result['requires_mpv'] = _requires_mpv_playback(title, url, ext)
    return result


def _is_direct_media(url):
    if _is_stremio_resolver(url):
        return False
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
    if 'erome.com' in host:
        return 'https://www.erome.com/'
    if parsed.scheme and parsed.netloc:
        return f'{parsed.scheme}://{parsed.netloc}/'
    return url


def _resolve_erome(url):
    headers = {
        'User-Agent': BROWSER_UA,
        'Referer': 'https://www.erome.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=30.0)) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 403:
            raise RuntimeError('Erome blocked this server (403)')
        resp.raise_for_status()
        html = resp.text

    title_match = re.search(r'<title>([^<]+)</title>', html, re.I)
    title = title_match.group(1).strip() if title_match else 'Erome'
    title = re.sub(r'\s*[-|]\s*Erome.*$', '', title, flags=re.I).strip() or 'Erome'

    mp4s = []
    seen = set()
    for match in _EROME_MP4_RE.findall(html):
        clean = match.rstrip('",\'')
        if clean not in seen:
            seen.add(clean)
            mp4s.append(clean)

    if not mp4s:
        raise ResolveError(
            'No videos found on this Erome page',
            code='no_stream',
            hint='Open an album page (erome.com/a/…) that contains videos.',
            retriable=False,
            site='erome.com',
        )

    stream_headers = {
        'User-Agent': BROWSER_UA,
        'Referer': url,
        'Accept': '*/*',
    }

    if len(mp4s) == 1:
        result = _direct_media_response(mp4s[0], {
            'ext': 'mp4',
            'stream_type': 'progressive',
            'title': title,
            'filesize': None,
            'content_type': 'video/mp4',
            'headers': stream_headers,
        })
        result['url'] = url
        result['title'] = title
        result['site'] = 'erome'
        result['resolved_with'] = 'erome-scraper'
        return result

    entries = []
    for i, stream_url in enumerate(mp4s):
        entries.append({
            'id': str(i),
            'title': f'{title} ({i + 1})',
            'url': stream_url,
            'thumbnail': None,
            'duration': None,
        })
    return {
        'type': 'playlist',
        'title': title,
        'entries': entries,
        'url': url,
        'resolved_with': 'erome-scraper',
    }


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
    if data.startswith(b'FLV\x01'):
        return 'flv'
    if len(data) >= 12 and data[:4] == b'RIFF':
        if data[8:12] == b'AVI ':
            return 'avi'
        if data[8:12] == b'WAVE':
            return 'wav'
    if len(data) >= 4 and data[:3] == b'\x00\x00\x01':
        return 'mpeg'
    if len(data) >= 4 and data[:4] == b'\x30\x26\xb2\x75':
        return 'wmv'
    if data.startswith(b'\x1f\x8b'):
        return None
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
        'requires_mpv': _requires_mpv_playback(title, url, ext),
    }


def _cdn_download_fallback(url):
    host = urlparse(url).netloc.lower()
    title = host or 'CDN download'
    return {
        'ext': 'mp4',
        'stream_type': 'progressive',
        'title': title,
        'filesize': None,
        'content_type': 'video/mp4',
        'headers': {
            'User-Agent': BROWSER_UA,
            'Referer': _referer_for_url(url),
            'Accept': '*/*',
        },
    }


def _resolve_magnet(url, format_id=None):
    from torrent import HAS_LIBTORRENT, get_manager

    alldebrid_error = None
    try:
        from alldebrid import is_configured as alldebrid_ready, resolve_magnet as resolve_via_alldebrid
        if alldebrid_ready():
            try:
                return resolve_via_alldebrid(url, 'Magnet link')
            except Exception as exc:
                alldebrid_error = str(exc)
    except Exception as exc:
        alldebrid_error = str(exc)

    if HAS_LIBTORRENT:
        try:
            mgr = get_manager()
            idx = int(format_id) if format_id is not None and str(format_id).isdigit() else None
            return mgr.resolve_for_play(url, idx)
        except Exception as exc:
            if alldebrid_error:
                raise RuntimeError(
                    f'AllDebrid failed: {alldebrid_error}. Local torrent failed: {exc}'
                ) from exc
            raise

    if alldebrid_error:
        raise RuntimeError(f'AllDebrid failed: {alldebrid_error}')
    raise RuntimeError('Magnet links need AllDebrid or libtorrent installed.')


def resolve_url(url, format_id=None):
    from torrent import is_magnet

    url = normalize_play_url(url)
    if not url:
        raise ResolveError('No URL provided', code='invalid_url', hint='Paste a link or magnet.', retriable=False)

    if is_magnet(url):
        try:
            return _resolve_magnet(url, format_id)
        except Exception as exc:
            raise _classify_resolve_error(exc, url) from exc

    if _is_stremio_resolver(url):
        try:
            return _resolve_stremio_stream(url)
        except Exception as exc:
            raise _classify_resolve_error(exc, url) from exc

    if _is_direct_media(url):
        probe = _probe_direct_url(url) if _is_erome_host(url) else None
        return _direct_media_response(url, probe)

    if _is_erome_page_url(url):
        try:
            return _resolve_erome(url)
        except ResolveError:
            raise
        except Exception as exc:
            if not _is_retriable(exc):
                raise _classify_resolve_error(exc, url) from exc

    if _looks_like_cdn_download(url):
        probe = _probe_direct_url(url)
        if probe:
            return _direct_media_response(url, probe)
        return _direct_media_response(url, _cdn_download_fallback(url))

    if _should_try_kvs_player(url):
        try:
            return _resolve_kvs_player(url)
        except Exception as kvs_error:
            if _is_likely_kvs_site(url):
                raise ResolveError(
                    str(kvs_error),
                    code='kvs_failed',
                    hint='This site embeds video that cloud servers cannot scrape. Try MPV bridge or a direct link.',
                    retriable=True,
                    site=_host_key(urlparse(url).netloc),
                ) from kvs_error

    last_error = None
    for strategy in _build_strategies():
        try:
            info = _extract(
                url,
                format_id,
                impersonate=strategy['impersonate'],
                cookies_browser=strategy['cookies'],
                cookiefile=strategy.get('cookiefile'),
            )
            result = _build_response(info, url)
            if result.get('type') == 'playlist' and result.get('entries'):
                if strategy['label'] != 'default':
                    result['resolved_with'] = strategy['label']
                return result
            if not result.get('stream_url'):
                if _looks_like_cdn_download(url):
                    return _direct_media_response(url, _cdn_download_fallback(url))
                raise ResolveError(
                    'No playable stream found',
                    code='no_stream',
                    hint='Try another quality from the menu, or open in MPV.',
                    retriable=True,
                    site=_host_key(urlparse(url).netloc),
                )
            if strategy['label'] != 'default':
                result['resolved_with'] = strategy['label']
            return result
        except ResolveError:
            raise
        except Exception as exc:
            last_error = exc
            if not _is_retriable(exc):
                if _looks_like_cdn_download(url):
                    return _direct_media_response(url, _cdn_download_fallback(url))
                raise _classify_resolve_error(exc, url) from exc
            continue

    if _looks_like_cdn_download(url):
        return _direct_media_response(url, _cdn_download_fallback(url))

    raise _classify_resolve_error(last_error or RuntimeError('All resolve strategies failed'), url)


def download_media(url, format_id=None, on_progress=None):
    from torrent import is_magnet, normalize_url

    url = normalize_url(url)
    if is_magnet(url):
        raise RuntimeError('Magnet downloads are not supported. Play the torrent instead.')

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
            opts = _base_opts(strategy['impersonate'], strategy['cookies'], strategy.get('cookiefile'))
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