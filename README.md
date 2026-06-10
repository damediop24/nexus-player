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

## Deploy Online (public URL)

GitHub hosts the **code**, not a running player. To get a public URL like `https://nexus-player.onrender.com`, deploy to a cloud host.

### Option A — Render (free, easiest)

1. Go to [render.com](https://render.com) and sign up with GitHub
2. **New +** → **Blueprint** (or **Web Service**)
3. Connect repo: `damediop24/nexus-player`
4. Render detects `render.yaml` and `Dockerfile` automatically
5. Click **Deploy** — wait ~5 minutes
6. Your player URL will be shown, e.g. `https://nexus-player.onrender.com`

> Free tier sleeps after 15 min idle — first visit may take ~30s to wake up.

### Option B — Railway

1. Go to [railway.app](https://railway.app) and sign up with GitHub
2. **New Project** → **Deploy from GitHub repo** → select `nexus-player`
3. Railway uses the included `Dockerfile`
4. Open **Settings** → **Networking** → **Generate Domain**
5. Use that URL (e.g. `https://nexus-player-production.up.railway.app`)

### Option C — Fly.io

```bash
fly launch --from nexus-player
fly deploy
fly open
```

### Cloud limitations

| Feature | Local | Cloud |
|---------|-------|-------|
| Play streams | ✅ | ✅ (most sites) |
| MPV button | ✅ | ❌ (no desktop app) |
| Browser cookies (403 fix) | ✅ | ❌ |
| Downloads saved | ✅ | Limited (ephemeral disk) |
| Bandwidth | Your ISP | Host limits apply |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 403 Forbidden | Log into the site in Chrome/Edge, then retry |
| Video won't play | Try **MPV** button or a lower quality |
| Autoplay blocked | Click the video or ▶ button |
| Site not supported | Update yt-dlp: `pip install -U yt-dlp` |

## Telegram Bot (Restricted Content Downloader)

Nexus Player now includes the **neex** Telegram bot for downloading photos, videos, audio, documents and text from private/restricted Telegram channels and posts (single or batch).

Downloads (especially video/audio) are saved directly into the shared `downloads/` folder so they appear automatically in the **Library** tab of the web UI.

### Setup

1. Install (or re-install) dependencies so the bot packages are present:

   ```bat
   pip install -r requirements.txt
   ```

2. Configure the bot:

   - Edit `bot/config.env`
   - Fill in:
     - `API_ID` + `API_HASH` → from https://my.telegram.org
     - `BOT_TOKEN` → from @BotFather (`/newbot`)
     - `SESSION_STRING` → from @TgDevToolBot (use the Pyrogram Session button and follow the flow)

   > The account used for `SESSION_STRING` must be a member of the source chats/channels you want to download from.

   Optional settings in the same file:
   - `FORWARD_CHAT_ID` (the bot will auto-copy downloads to this channel/group if the bot has permission)
   - `MAX_CONCURRENT_DOWNLOADS`, `BATCH_SIZE`, `FLOOD_WAIT_DELAY`

3. Run the bot (separate process from the web player):

   ```bat
   start-bot.bat
   ```

   Or on Unix:
   ```bash
   chmod +x start-bot.sh
   ./start-bot.sh
   ```

   The console will show "Bot Started!". Use the bot in Telegram by sending it post links or `/dl <link>`, `/bdl ...`, `/help`, etc.

4. (Optional) Start everything:
   - Run `start.bat` for the web player (port 8899)
   - Run `start-bot.bat` for the downloader

   TG downloads land in `downloads/<message_id>/...` and videos/audio will be browsable in the Nexus Library.

### Notes

- The bot is primarily intended for **local/desktop** use (it uses a user session to access restricted content).
- Cloud deploys of Nexus (Render/Railway/Fly) run the web player only. The bot is not started automatically in Docker.
- FFmpeg is already part of the Nexus setup (thumbnails + media info for the bot).
- Without `TgCrypto` the bot works but is slower for crypto operations. Install Microsoft C++ Build Tools then `pip install tgcrypto` if you transfer lots of media.

See also `bot/USAGE.md` (from the original neex project) for detailed command examples.

## License

MIT — see [LICENSE](LICENSE).