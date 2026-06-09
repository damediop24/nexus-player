#!/usr/bin/env python3
"""Local MPV bridge — lets the Nexus web UI launch MPV on your PC without .bat files."""
import json
import os
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get('NEXUS_MPV_BRIDGE_PORT', '9340'))
DEFAULT_MPV = os.environ.get('MPV_PATH', r'C:\mpv\mpv\mpv.exe')
PROTOCOL_FLAG = Path.home() / '.nexus-mpv-protocol-registered'


def ensure_protocol_handler(mpv_path: str):
    if PROTOCOL_FLAG.exists() or not os.path.isfile(mpv_path):
        return
    try:
        subprocess.run(
            [mpv_path, '--register-protocol-handler'],
            check=True,
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PROTOCOL_FLAG.write_text('ok', encoding='utf-8')
        print('[mpv-bridge] Registered mpv:// protocol handler')
    except Exception as exc:
        print(f'[mpv-bridge] Protocol registration skipped: {exc}')


class Handler(BaseHTTPRequestHandler):
    server_version = 'NexusMpvBridge/1.0'

    def log_message(self, fmt, *args):
        print(f'[mpv-bridge] {self.address_string()} {fmt % args}')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _resolve_page(self, url: str):
        root = Path(__file__).parent
        backend = root / 'backend'
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from resolver import resolve_url

        result = resolve_url(url)
        stream_url = result.get('stream_url')
        if not stream_url:
            raise RuntimeError('No stream URL found for this page')
        return {
            'ok': True,
            'source_url': url,
            'stream_url': stream_url,
            'title': result.get('title'),
            'thumbnail': result.get('thumbnail'),
            'duration': result.get('duration'),
            'site': result.get('site'),
            'headers': result.get('headers') or {},
            'stream_type': result.get('stream_type', 'progressive'),
            'content_type': result.get('content_type'),
            'resolved_with': result.get('resolved_with', 'local-bridge'),
        }

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/health':
            self._json({'ok': True, 'port': PORT, 'mpv': DEFAULT_MPV, 'resolve': True})
            return
        if parsed.path == '/resolve':
            params = urllib.parse.parse_qs(parsed.query)
            url = (params.get('url') or [''])[0]
            if not url:
                self._json({'error': 'missing url parameter'}, 400)
                return
            try:
                self._json(self._resolve_page(url))
            except Exception as exc:
                self._json({'error': str(exc)}, 500)
            return
        if parsed.path in ('/launch', '/launch-form'):
            params = urllib.parse.parse_qs(parsed.query)
            url = (params.get('url') or [''])[0]
            mpv = urllib.parse.unquote((params.get('mpv') or [DEFAULT_MPV])[0])
            title = urllib.parse.unquote((params.get('title') or ['Nexus Player'])[0])
            referer = urllib.parse.unquote((params.get('referer') or [''])[0])
            if not url:
                self._html('Missing url parameter', 400)
                return
            try:
                self._exec_launch(url, mpv, title, referer or None)
                self._html(
                    '<!doctype html><html><head><meta charset="utf-8"><title>MPV</title></head>'
                    '<body style="font-family:sans-serif;padding:1rem">'
                    '<p>MPV launched.</p><script>setTimeout(()=>window.close(),400)</script></body></html>'
                )
            except FileNotFoundError:
                self._html(f'<p>MPV not found: {mpv}</p>', 404)
            except Exception as exc:
                self._html(f'<p>Error: {exc}</p>', 500)
            return
        self._html('<p>Nexus MPV Bridge is running.</p>', 200)

    def do_POST(self):
        if self.path == '/resolve':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            try:
                data = json.loads(raw.decode('utf-8'))
            except json.JSONDecodeError:
                self._json({'error': 'invalid json'}, 400)
                return
            url = data.get('url', '')
            if not url:
                self._json({'error': 'missing url'}, 400)
                return
            try:
                self._json(self._resolve_page(url))
            except Exception as exc:
                self._json({'error': str(exc)}, 500)
            return
        if self.path not in ('/launch', '/launch-form'):
            self._json({'error': 'not found'}, 404)
            return
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        content_type = self.headers.get('Content-Type', '')
        if 'application/x-www-form-urlencoded' in content_type:
            params = urllib.parse.parse_qs(raw.decode('utf-8'))
            data = {k: (v[0] if v else '') for k, v in params.items()}
        else:
            try:
                data = json.loads(raw.decode('utf-8'))
            except json.JSONDecodeError:
                self._json({'error': 'invalid json'}, 400)
                return
        url = data.get('url', '')
        mpv = data.get('mpv_path') or data.get('mpv') or DEFAULT_MPV
        title = data.get('title') or 'Nexus Player'
        referer = data.get('referer') or ''
        if not url:
            self._json({'error': 'missing url'}, 400)
            return
        try:
            self._exec_launch(url, mpv, title, referer or None)
            if self.path == '/launch-form' or 'application/x-www-form-urlencoded' in content_type:
                self._html(
                    '<!doctype html><html><head><meta charset="utf-8"><title>MPV</title></head>'
                    '<body style="font-family:sans-serif;padding:1rem">'
                    '<p>MPV launched.</p><script>setTimeout(()=>window.close(),400)</script></body></html>'
                )
            else:
                self._json({'ok': True})
        except Exception as exc:
            if self.path == '/launch-form' or 'application/x-www-form-urlencoded' in content_type:
                self._html(f'<p>Error: {exc}</p>', 500)
            else:
                self._json({'error': str(exc)}, 500)

    def _exec_launch(self, url: str, mpv: str, title: str, referer=None):
        if not os.path.isfile(mpv):
            raise FileNotFoundError(mpv)
        args = [mpv, '--force-window=immediate', f'--title={title}']
        if referer:
            args.append(f'--referrer={referer}')
        args.append(url)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _json(self, data, code=200):
        payload = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, html: str, code=200):
        if not html.lstrip().startswith('<!'):
            html = f'<!doctype html><html><body>{html}</body></html>'
        payload = html.encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    mpv = DEFAULT_MPV
    if len(sys.argv) > 1:
        mpv = sys.argv[1]
    ensure_protocol_handler(mpv)
    host = '127.0.0.1'
    httpd = ThreadingHTTPServer((host, PORT), Handler)
    print(f'Nexus MPV bridge on http://{host}:{PORT}')
    print(f'MPV: {mpv}')
    print('Keep this running — MPV launch and local resolve for blocked sites use this bridge.')
    httpd.serve_forever()


if __name__ == '__main__':
    main()