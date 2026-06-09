import importlib.util
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
TORRENTS_DIR = ROOT / 'torrents'
TORRENTS_DIR.mkdir(exist_ok=True)

HAS_LIBTORRENT = bool(importlib.util.find_spec('libtorrent'))

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.mpg', '.mpeg', '.ts', '.m2ts', '.flv', '.ogv'}
MIME_MAP = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mkv': 'video/x-matroska', '.webm': 'video/webm',
    '.avi': 'video/x-msvideo', '.mov': 'video/quicktime', '.wmv': 'video/x-ms-wmv',
    '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg', '.ts': 'video/mp2t', '.m2ts': 'video/mp2t',
    '.ogv': 'video/ogg', '.flv': 'video/x-flv',
}

_manager = None
_manager_lock = threading.Lock()


def is_magnet(url: str) -> bool:
    return (url or '').strip().lower().startswith('magnet:')


def is_torrent_bytes(data: bytes) -> bool:
    return data[:11] == b'd8:announce' or data[:2] == b'PK'


class TorrentManager:
    def __init__(self, save_path: Path):
        if not HAS_LIBTORRENT:
            raise RuntimeError('libtorrent is not installed')

        import libtorrent as lt

        self.lt = lt
        self.save_path = Path(save_path)
        self.save_path.mkdir(parents=True, exist_ok=True)
        self.session = lt.session()
        self.session.listen_on(6881, 6991)

        settings = {
            'enable_dht': True,
            'enable_lsd': True,
            'enable_upnp': True,
            'enable_natpmp': True,
            'allow_multiple_connections_per_ip': True,
        }
        pack = lt.session_settings_pack()
        for key, value in settings.items():
            pack.set_bool(key, value)
        self.session.apply_settings(pack)

        self.entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _register(self, handle, source: str, label: str = '') -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self.entries[tid] = {
                'id': tid,
                'handle': handle,
                'source': source,
                'label': label,
                'added_at': time.time(),
            }
        return tid

    def add_magnet(self, magnet: str) -> str:
        magnet = magnet.strip()
        if not is_magnet(magnet):
            raise ValueError('Not a magnet link')

        params = self.lt.parse_magnet_uri(magnet)
        params.save_path = str(self.save_path)
        handle = self.session.add_torrent(params)
        handle.set_flags(self.lt.torrent_flags.sequential_download)
        return self._register(handle, magnet, 'magnet')

    def add_torrent_data(self, data: bytes, label: str = 'torrent file') -> str:
        if not data:
            raise ValueError('Empty torrent file')

        info = self.lt.torrent_info(self.lt.bdecode(data))
        params = self.lt.add_torrent_params()
        params.ti = info
        params.save_path = str(self.save_path)
        handle = self.session.add_torrent(params)
        handle.set_flags(self.lt.torrent_flags.sequential_download)
        return self._register(handle, label, label)

    def get(self, tid: str) -> Optional[dict]:
        return self.entries.get(tid)

    def wait_metadata(self, tid: str, timeout: float = 120.0) -> dict:
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')

        handle = entry['handle']
        deadline = time.time() + timeout
        while time.time() < deadline:
            if handle.status().has_metadata:
                ti = handle.torrent_file()
                entry['name'] = ti.name()
                return {'id': tid, 'name': ti.name()}
            time.sleep(0.2)

        raise TimeoutError('Timed out waiting for torrent metadata. Add trackers or check peers.')

    def list_files(self, tid: str) -> list[dict]:
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')

        handle = entry['handle']
        if not handle.status().has_metadata:
            self.wait_metadata(tid)

        ti = handle.torrent_file()
        fs = ti.files()
        progress = handle.file_progress()
        files = []

        for i in range(fs.num_files()):
            path = fs.file_path(i)
            ext = Path(path).suffix.lower()
            files.append({
                'index': i,
                'path': path,
                'name': Path(path).name,
                'size': fs.file_size(i),
                'progress': round((progress[i] if i < len(progress) else 0) * 100, 1),
                'is_video': ext in VIDEO_EXTS,
                'ext': ext.lstrip('.'),
            })

        return files

    def pick_best_video(self, tid: str) -> int:
        files = self.list_files(tid)
        videos = [f for f in files if f['is_video']]
        if not videos:
            raise RuntimeError('No video files found in torrent')
        return max(videos, key=lambda f: f['size'])['index']

    def set_file_priority(self, tid: str, file_index: int, priority: int):
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')
        entry['handle'].set_file_priority(file_index, max(0, min(7, priority)))

    def prioritize_file(self, tid: str, file_index: int):
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')

        handle = entry['handle']
        ti = handle.torrent_file()
        fs = ti.files()

        for i in range(fs.num_files()):
            handle.set_file_priority(i, 7 if i == file_index else 1)

        handle.set_flags(self.lt.torrent_flags.sequential_download)

    def status(self, tid: str) -> dict:
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')

        handle = entry['handle']
        st = handle.status()
        s = {
            'id': tid,
            'name': entry.get('name') or (st.name if st.has_metadata else 'Loading metadata...'),
            'source': entry.get('source', ''),
            'progress': round(st.progress * 100, 1),
            'download_rate': st.download_rate,
            'upload_rate': st.upload_rate,
            'peers': st.num_peers,
            'seeds': st.num_seeds,
            'state': str(st.state).split('.')[-1],
            'paused': st.paused,
            'has_metadata': st.has_metadata,
            'total_size': st.total_wanted if st.has_metadata else 0,
        }
        return s

    def list_all(self) -> list[dict]:
        return [self.status(tid) for tid in list(self.entries.keys())]

    def pause(self, tid: str):
        entry = self.get(tid)
        if entry:
            entry['handle'].pause()

    def resume(self, tid: str):
        entry = self.get(tid)
        if entry:
            entry['handle'].resume()

    def remove(self, tid: str, delete_files: bool = False):
        entry = self.get(tid)
        if not entry:
            return
        self.session.remove_torrent(entry['handle'], int(delete_files))
        with self._lock:
            self.entries.pop(tid, None)

    def file_path(self, tid: str, file_index: int) -> Path:
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')

        handle = entry['handle']
        if not handle.status().has_metadata:
            self.wait_metadata(tid)

        ti = handle.torrent_file()
        rel = ti.files().file_path(file_index)
        return self.save_path / rel

    def mime_for_file(self, tid: str, file_index: int) -> str:
        path = self.file_path(tid, file_index)
        return MIME_MAP.get(path.suffix.lower(), 'application/octet-stream')

    def ensure_range(self, tid: str, file_index: int, start: int, end: int, timeout: float = 180.0) -> bool:
        entry = self.get(tid)
        if not entry:
            raise KeyError('Torrent not found')

        handle = entry['handle']
        if not handle.status().has_metadata:
            self.wait_metadata(tid)

        self.prioritize_file(tid, file_index)

        ti = handle.torrent_file()
        size = ti.files().file_size(file_index)
        end = min(end, size - 1)
        start = max(0, start)

        start_piece = ti.map_file(file_index, start, 0).piece
        end_piece = ti.map_file(file_index, end, 0).piece

        for piece in range(start_piece, end_piece + 1):
            handle.piece_priority(piece, 7)

        deadline = time.time() + timeout
        while time.time() < deadline:
            st = handle.status()
            if st.is_seeding or st.progress >= 1.0:
                return True

            ready = True
            for piece in range(start_piece, end_piece + 1):
                if not handle.have_piece(piece):
                    ready = False
                    break
            if ready:
                return True
            time.sleep(0.15)

        return False

    def resolve_for_play(self, magnet_or_label: str, file_index: Optional[int] = None) -> dict:
        if is_magnet(magnet_or_label):
            tid = self.add_magnet(magnet_or_label)
        else:
            raise ValueError('Expected magnet link')

        meta = self.wait_metadata(tid)
        idx = file_index if file_index is not None else self.pick_best_video(tid)
        files = self.list_files(tid)
        file_info = next(f for f in files if f['index'] == idx)

        self.prioritize_file(tid, idx)

        return {
            'type': 'video',
            'title': f"{meta['name']} — {file_info['name']}",
            'url': magnet_or_label,
            'thumbnail': None,
            'duration': None,
            'site': 'torrent',
            'torrent_id': tid,
            'file_index': idx,
            'stream_url': f'/api/torrent/{tid}/stream/{idx}',
            'stream_type': 'progressive',
            'content_type': self.mime_for_file(tid, idx),
            'formats': [{
                'format_id': str(idx),
                'ext': file_info['ext'],
                'quality': 'torrent',
                'resolution': 'source',
                'filesize': file_info['size'],
            }],
            'best_format_id': str(idx),
            'subtitles': [],
            'headers': {},
            'play_url': f'/api/torrent/{tid}/stream/{idx}',
        }

    def resolve_torrent_file(self, data: bytes, filename: str, file_index: Optional[int] = None) -> dict:
        tid = self.add_torrent_data(data, filename)
        meta = self.wait_metadata(tid)
        idx = file_index if file_index is not None else self.pick_best_video(tid)
        files = self.list_files(tid)
        file_info = next(f for f in files if f['index'] == idx)
        self.prioritize_file(tid, idx)

        return {
            'type': 'video',
            'title': f"{meta['name']} — {file_info['name']}",
            'url': filename,
            'thumbnail': None,
            'duration': None,
            'site': 'torrent',
            'torrent_id': tid,
            'file_index': idx,
            'stream_url': f'/api/torrent/{tid}/stream/{idx}',
            'stream_type': 'progressive',
            'content_type': self.mime_for_file(tid, idx),
            'formats': [{
                'format_id': str(f['index']),
                'ext': f['ext'],
                'quality': f['name'],
                'resolution': 'source',
                'filesize': f['size'],
            } for f in files if f['is_video']] or [{
                'format_id': str(idx),
                'ext': file_info['ext'],
                'quality': 'torrent',
                'resolution': 'source',
                'filesize': file_info['size'],
            }],
            'best_format_id': str(idx),
            'subtitles': [],
            'headers': {},
            'play_url': f'/api/torrent/{tid}/stream/{idx}',
        }


def get_manager() -> TorrentManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            if not HAS_LIBTORRENT:
                raise RuntimeError(
                    'Torrent support requires libtorrent. Install with: pip install libtorrent'
                )
            _manager = TorrentManager(TORRENTS_DIR)
        return _manager


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header or not range_header.startswith('bytes='):
        return 0, file_size - 1

    match = re.match(r'bytes=(\d*)-(\d*)', range_header)
    if not match:
        return 0, file_size - 1

    start_s, end_s = match.groups()
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    return start, end