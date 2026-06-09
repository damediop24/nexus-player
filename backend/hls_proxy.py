import re
from typing import Optional
from urllib.parse import urljoin

from streams import create_token


def is_hls_manifest(url: str, content_type: Optional[str] = None, stream_type: Optional[str] = None) -> bool:
    lower = (url or '').lower().split('?')[0]
    if lower.endswith('.m3u8') or '/hls-' in lower:
        return True
    if stream_type == 'hls':
        return True
    if content_type:
        base = content_type.split(';')[0].strip().lower()
        if base in ('application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl'):
            return True
    return False


def _proxy_line_url(
    url: str,
    manifest_url: str,
    headers: Optional[dict] = None,
    refresh_url: Optional[str] = None,
) -> str:
    absolute = urljoin(manifest_url, url.strip())
    token = create_token(
        absolute,
        headers,
        stream_type='progressive',
        content_type='video/mp2t' if absolute.lower().split('?')[0].endswith('.ts') else 'application/vnd.apple.mpegurl',
        refresh_url=refresh_url,
    )
    return f'/api/proxy/{token}'


def is_hls_segment_url(url: str) -> bool:
    lower = (url or '').lower().split('?')[0]
    return lower.endswith('.ts') or lower.endswith('.m4s')


def refresh_hls_segment(entry: dict) -> bool:
    refresh_url = entry.get('refresh_url')
    old_url = entry.get('url') or ''
    if not refresh_url or not is_hls_segment_url(old_url):
        return False

    filename = old_url.rsplit('/', 1)[-1].split('?')[0]
    if not filename:
        return False

    from resolver import resolve_url
    import httpx

    try:
        refreshed = resolve_url(refresh_url)
        manifest_url = refreshed.get('stream_url')
        if not manifest_url:
            return False
        headers = dict(refreshed.get('headers') or {})
        if 'User-Agent' not in headers:
            headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        resp = httpx.get(manifest_url, headers=headers, timeout=30)
        if resp.status_code >= 400:
            return False
        entry['url'] = urljoin(manifest_url, filename)
        entry['headers'] = headers
        return True
    except Exception:
        return False


def rewrite_hls_manifest(
    body: str,
    manifest_url: str,
    headers: Optional[dict] = None,
    refresh_url: Optional[str] = None,
) -> str:
    lines = body.replace('\r\n', '\n').split('\n')
    out = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue

        if stripped.startswith('#'):
            if 'URI="' in stripped:
                def repl(match):
                    uri = match.group(1)
                    return f'URI="{_proxy_line_url(uri, manifest_url, headers, refresh_url)}"'
                line = re.sub(r'URI="([^"]+)"', repl, stripped)
            out.append(line)
            continue

        out.append(_proxy_line_url(stripped, manifest_url, headers, refresh_url))

    text = '\n'.join(out)
    if not text.endswith('\n'):
        text += '\n'
    return text