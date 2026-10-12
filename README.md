# Offlify

**Your music, offline.** Paste a YouTube playlist or video link, pick the tracks
you want, and save them as audio files (MP3, WAV, FLAC, OGG or M4A) that play
anywhere without a connection.

Offlify is a small web app you run **on your own computer**. It reads track
lists through the official YouTube Data API v3, downloads audio with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and converts it with FFmpeg.

> **Use it responsibly.** Only download content you have the right to download:
> your own uploads, Creative Commons or public-domain material, or content you
> have permission for. Downloading other content may breach YouTube's Terms of
> Service and copyright law in your country. You are responsible for how you
> use this tool.

## What you need

| Requirement | Notes |
|---|---|
| Python | 3.10 or newer |
| FFmpeg + ffprobe | on your `PATH` |
| A YouTube Data API key | free, from a Google account (steps below) |

## Setup

### 1. Clone and install

```bash
git clone https://github.com/prajanthh/offlify.git
cd offlify
python -m venv .venv
```

Activate the environment, then install the dependencies:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Install FFmpeg

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (then open a new terminal) |
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt update && sudo apt install ffmpeg` |

Check it with `ffmpeg -version`.

### 3. Get your own YouTube Data API key

Each person needs their own key. It is free and takes a few minutes.

1. Go to <https://console.cloud.google.com/> and create (or pick) a project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Recommended: **Edit API key → API restrictions → Restrict key → YouTube Data API v3**.

The free quota is 10,000 units a day. Loading a video costs 1 unit and a
60-track playlist about 5, so normal personal use stays far below the limit.

### 4. Add the key

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and set:

```
YOUTUBE_API_KEY=your-key-here
```

`.env` is in `.gitignore`. Never commit it or share your key.

### 5. Run

```bash
python app.py
```

Open **<http://127.0.0.1:5000>** in your browser.

## Using it

1. Paste a link and click **Fetch**. These all work:
   - playlists: `youtube.com/playlist?list=PL…`
   - videos: `youtube.com/watch?v=…`, `youtu.be/…`, Shorts
2. For a playlist, tick the tracks you want. A single video needs no selection.
3. Choose a format and click **Download**.
4. Save each finished file, or grab them all with **Download ZIP**.

The chips in the header show whether the API key, FFmpeg and yt-dlp are ready.

Private playlists, Watch Later, Liked videos and auto-generated Mixes cannot be
read with an API key. A video link taken from inside one of those lists still
loads as a single video.

## Good to know

- **Run it at home, not on a cloud server.** YouTube blocks downloads from most
  hosting providers with a "confirm you're not a bot" check. Playlists still
  load there, but downloads fail. On a home connection it works.
- **GitHub Pages cannot run it.** Offlify needs its Python server, so a static
  host only shows a page that does nothing.
- **Use it from your phone** by running it on your PC and sharing it privately
  with a tool such as [Tailscale](https://tailscale.com/) (`tailscale serve 5000`).
- **Downloads failing after working before?** YouTube changes often. Update
  yt-dlp with `pip install -U yt-dlp`.
- Downloaded and converted files are stored in `data/` and deleted
  automatically after two hours (`FILE_TTL_SECONDS` in `.env`).

## Docker (optional)

```bash
docker build -t offlify .
docker run -p 8000:8000 -e YOUTUBE_API_KEY=your-key-here offlify
```

Then open <http://localhost:8000>. The image turns on a per-visitor rate limit
(`RATE_LIMIT_PER_MINUTE`) and expects one reverse proxy in front of it
(`TRUSTED_PROXIES`); see `.env.example`.

## Tests

```bash
pytest
```

The YouTube API is stubbed, so no key or network access is needed. Tests that
need FFmpeg are skipped if it is not installed.

## Project structure

```
offlify/
├── app.py                      Flask app, config, REST routes, rate limit, security headers
├── services/
│   ├── playlist_service.py     YouTube Data API v3: playlists, single videos, video details
│   ├── ytdlp_service.py        Audio download through yt-dlp
│   ├── audio_service.py        FFmpeg conversion and file checks
│   ├── job_service.py          Background jobs: progress, cancel, retry, cleanup
│   └── archive_service.py      ZIP creation with safe, de-duplicated names
├── utils/                      Link validation and filename helpers
├── static/                     index.html, css/styles.css, js/app.js, favicon.svg
├── tests/                      pytest suite
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Main API routes

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Status, FFmpeg / API-key availability, formats, limits |
| `POST /api/resolve` `{url}` | Load a playlist or a single video from a link |
| `POST /api/process-youtube` `{format, items[], playlist_title}` | Start a download job |
| `GET /api/jobs/<job_id>` | Overall and per-track progress |
| `POST /api/jobs/<job_id>/cancel` | Cancel queued and running tracks |
| `POST /api/jobs/<job_id>/retry` | Retry failed or cancelled tracks |
| `GET /api/download/<file_id>` | Download one finished file |
| `GET /api/download-zip/<job_id>` | ZIP of all finished files in a job |

Errors are always JSON: `{"error": "human-readable message"}`.

## Limitations

- Jobs live in memory: restarting the server forgets them, and it must run as a
  single process.
- `python app.py` starts Flask's development server, which is meant for use on
  your own machine.
