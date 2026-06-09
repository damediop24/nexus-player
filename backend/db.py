import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get('DATABASE_PATH', Path(__file__).parent / 'nexus.db'))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            thumbnail TEXT,
            duration REAL DEFAULT 0,
            site TEXT,
            format_id TEXT,
            position REAL DEFAULT 0,
            played_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            thumbnail TEXT,
            site TEXT,
            added_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS playlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            position INTEGER DEFAULT 0,
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            filepath TEXT,
            status TEXT DEFAULT 'pending',
            progress REAL DEFAULT 0,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    ''')

    defaults = {
        'default_quality': 'best',
        'autoplay': 'true',
        'volume': '1.0',
        'playback_rate': '1.0',
        'subtitle_lang': 'en',
        'fit_mode': 'contain',
        'pikpak_enabled': 'false',
    }
    for key, value in defaults.items():
        conn.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            (key, value),
        )

    if conn.execute('SELECT COUNT(*) FROM playlists').fetchone()[0] == 0:
        conn.execute("INSERT INTO playlists (name) VALUES ('Watch Later')")

    conn.commit()
    conn.close()


def row_to_dict(row):
    return dict(row) if row else None


def get_settings():
    conn = get_conn()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, str(value)),
    )
    conn.commit()
    conn.close()