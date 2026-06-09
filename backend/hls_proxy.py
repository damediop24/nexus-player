import re
from typing import Optional
from urllib.parse import urljoin, urlparse

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


def _proxy_line_url(url: str, manifest_url: str, headers: Optional[dict]) -> str:
    absolute = urljoin(manifest_url, url.strip())
    token = create_token(
        absolute,
        headers,
        stream_type='progressive',
        content_type='video/mp2t' if absolute.lower().split('?')[0].endswith('.ts') else 'application/vnd.apple.mpegurl',
    )
    return f'/api/proxy/{token}'


def rewrite_hls_manifest(body: str, manifest_url: str, headers: Optional[dict] = None) -> str:
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
                    return f'URI="{_proxy_line_url(uri, manifest_url, headers)}"'
                line = re.sub(r'URI="([^"]+)"', repl, stripped)
            out.append(line)
            continue

        out.append(_proxy_line_url(stripped, manifest_url, headers))

    text = '\n'.join(out)
    if not text.endswith('\n'):
        text += '\n'
    return text