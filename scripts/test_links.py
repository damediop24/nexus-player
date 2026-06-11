#!/usr/bin/env python3
"""Smoke-test common Nexus Player link types before release."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))

from hls_proxy import is_hls_manifest, rewrite_hls_manifest
from resolver import resolve_url

TESTS = [
    ('xvideos', 'https://www.xvideos.com/video.ooffkpk6f79/48480754/0/my_huge_tits_own_my_stepsons_-_stepmom_fucks_them_my_way_or_no_way_-_vibewithmommy'),
    ('youtube', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'),

]


def test_hls_rewrite():
    sample = '#EXTM3U\n#EXTINF:10.0,\nsegment0.ts\n'
    manifest_url = 'https://cdn.example.com/path/playlist.m3u8?token=abc'
    rewritten = rewrite_hls_manifest(sample, manifest_url, {'Referer': 'https://example.com/'})
    assert '/api/proxy/' in rewritten
    assert 'segment0.ts' not in rewritten.split('\n')[-2]
    print('  hls rewrite: ok')


def test_resolve(label, url):
    try:
        info = resolve_url(url)
    except Exception as exc:
        print(f'  {label}: FAIL resolve — {exc}')
        return False

    stream_url = info.get('stream_url') or ''
    stream_type = info.get('stream_type') or 'progressive'
    site = info.get('site') or info.get('resolved_with') or '?'
    print(f'  {label}: ok — site={site} type={stream_type} best={info.get("best_format_id")}')

    if is_hls_manifest(stream_url, stream_type=stream_type):
        import httpx
        headers = info.get('headers') or {}
        resp = httpx.get(stream_url, headers=headers, timeout=30)
        if resp.status_code >= 400:
            print(f'    manifest fetch failed: {resp.status_code}')
            return False
        rewritten = rewrite_hls_manifest(resp.text, stream_url, headers, url)
        proxy_lines = [ln for ln in rewritten.splitlines() if ln and not ln.startswith('#')]
        if not proxy_lines or not all('/api/proxy/' in ln for ln in proxy_lines[:3]):
            print('    manifest rewrite failed')
            return False
        from streams import get_token
        seg_token = proxy_lines[0].split('/')[-1]
        seg_entry = get_token(seg_token)
        if not seg_entry or not seg_entry.get('refresh_url'):
            print('    segment refresh_url missing')
            return False
        print(f'    hls segments proxied: {len(proxy_lines)}')
    elif stream_url.startswith('http'):
        import httpx
        headers = info.get('headers') or {}
        resp = httpx.head(stream_url, headers=headers, timeout=30, follow_redirects=True)
        if resp.status_code >= 400:
            resp = httpx.get(stream_url, headers=headers, timeout=30, follow_redirects=True)
        if resp.status_code >= 400:
            print(f'    stream probe failed: {resp.status_code}')
            return False
        print(f'    stream probe: {resp.status_code}')
    return True


def main():
    print('Testing HLS rewrite...')
    test_hls_rewrite()

    print('Testing resolvers...')
    ok = True
    for label, url in TESTS:
        if not test_resolve(label, url):
            ok = False

    if not ok:
        sys.exit(1)
    print('All tests passed.')


if __name__ == '__main__':
    main()