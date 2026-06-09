# Nexus Player

A powerful full-stack media player for streaming URLs and local files. Built with **FastAPI**, **yt-dlp**, and a modern web UI.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License](https://img.shields.io/badge/license-MIT-blue)

## Features

- **1800+ sites** — YouTube, Twitch, Vimeo, and more via yt-dlp
- **Stream proxy** — Range-request support for smooth browser playback
- **Display modes** — Fit, Crop, Stretch, Original (+ fullscreen toolbar)
- **Wide format support** — MP4, WebM, MKV, HLS, DASH, audio files
- **Queue & playlists** — Batch paste, shuffle, repeat, auto-advance
- **History & resume** — Pick up where you left off
- **Downloads** — Save videos locally with progress tracking
- **Subtitles & quality picker** — Choose format and caption track
- **MPV integration** — Launch external MPV for tough streams
- **Remote control** — Control playback from any device on your LAN
- **Anti-bot bypass** — Browser impersonation + cookie extraction (curl_cffi)

## Requirements

- **Python 3.10+**
- **FFmpeg** (recommended, for downloads and format merging)
- **MPV** (optional, for external playback)

## Quick Start

### Windows

```bat
install.bat
start.bat
```

### macOS / Linux

```bash
python3 -m pip install -r requirements.txt
chmod +x start.sh
./start.sh
```

Open **http://localhost:8899** in your browser.

## Manual Start

```bash
cd backend
python app.py
```

Set a custom port:

```bash
set PORT=9000        # Windows
export PORT=9000     # macOS/Linux
python app.py
```

## Usage

| Action | How |
|--------|-----|
| Play URL | Paste link → **Play** |
| Local file | Drag & drop onto player |
| Display mode | Dropdown or **V** to cycle (Fit / Crop / Stretch / Original) |
| Fullscreen | **F** — fit toolbar appears top-right |
| Seek | Click or drag the progress bar |
| MPV | **MPV** button for external player |
| Remote | Open LAN URL shown in sidebar footer |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` / `K` | Play / Pause |
| `J` / `L` | −10s / +10s |
| `←` / `→` | −5s / +5s (Shift = ±30s) |
| `F` | Fullscreen |
| `V` | Cycle display mode |
| `M` | Mute |
| `S` | Screenshot |
| `0`–`9` | Jump to 0%–90% |

### Display Modes

| Mode | Behavior |
|------|----------|
| **Fit** | Full video visible, letterboxed |
| **Crop** | Fills screen, crops edges |
| **Stretch** | Stretches to fill (may distort) |
| **Original** | Native video resolution |

## Project Structure

```
nexus-player/
├── backend/
│   ├── app.py          # FastAPI server & API
│   ├── resolver.py     # yt-dlp stream resolver
│   ├── db.py           # SQLite persistence
│   └── streams.py      # Stream token proxy
├── public/
│   ├── index.html      # Web UI
│   ├── styles.css
│   └── app.js          # Player logic
├── downloads/          # Saved videos
├── uploads/            # Uploaded local files
├── requirements.txt
├── start.bat / start.sh
└── README.md
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/play` | Resolve & play URL |
| `POST` | `/api/resolve` | Inspect available formats |
| `GET` | `/api/proxy/{token}` | Proxied video stream |
| `GET` | `/api/queue` | Current queue |
| `GET` | `/api/history` | Watch history |
| `GET` | `/api/library` | Local media files |
| `WS` | `/ws` | Remote control & events |

## Optional Tools

Install for the best experience:

```bash
# FFmpeg (Windows)
winget install Gyan.FFmpeg

# MPV (Windows)
winget install shinchiro.mpv
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 403 Forbidden | Log into the site in Chrome/Edge, then retry |
| Video won't play | Try **MPV** button or a lower quality |
| Autoplay blocked | Click the video or ▶ button |
| Site not supported | Update yt-dlp: `pip install -U yt-dlp` |

## License

MIT — see [LICENSE](LICENSE).