# Offlify

Paste a YouTube playlist URL to get its track list (titles, artwork, durations,
order) from the **official YouTube Data API v3**. Then attach **audio files you
own or are licensed to use** to those tracks, convert them to MP3 / WAV / FLAC /
OGG / M4A with FFmpeg, and download them one by one or as a ZIP.

> **What this app does not do:** it never downloads, streams, decrypts or
> rips audio or video from YouTube. Having access to a playlist's *metadata*
> does not give you permission to obtain its *audio*. The playlist is used as
> a track list; the audio always comes from your own files or from servers
> you are authorised to use.

---

## 1. Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.10 – 3.13 (tested on 3.13) |
| FFmpeg + ffprobe | 4.4 or newer (tested on 6.1) |
| A Google account | to create a YouTube Data API key |

## 2. Install Python dependencies

```bash
cd playlist-audio-studio
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## 3. Install FFmpeg

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (then open a new terminal) |
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt update && sudo apt install ffmpeg` |

Check it: `ffmpeg -version` and `ffprobe -version` should both print a version.
The app detects which encoders your build has and only offers those formats.

## 4. Get a YouTube Data API key

1. Go to <https://console.cloud.google.com/> and create (or pick) a project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Recommended: **Edit API key → API restrictions → Restrict key → YouTube Data API v3**.

The free quota is 10,000 units/day. Fetching a playlist costs roughly
`2 + 2 × ceil(items / 50)` units, so a 200-item playlist is about 10 units.

An API key can read **public and unlisted** playlists. Private playlists,
Watch Later, Liked videos and auto-generated Mixes (`RD…`) can't be read with
an API key, and the app explains this when you try.

## 5. Configure environment variables

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env` and set at least:

```
YOUTUBE_API_KEY=your-key-here
```

All options are documented in `.env.example` (upload size, playlist size,
worker threads, file lifetime, optional source-URL allowlist). The key is only
read by the Flask server and is never sent to the browser or written to logs.

## 6. Start the server

```bash
python app.py
```

You should see `Running on http://127.0.0.1:5000`.

## 7. Open the app

Go to **<http://127.0.0.1:5000>** in your browser.

1. Paste a playlist URL and click **Fetch playlist**.
2. Attach audio: click **Attach audio** on a track, or use **Attach files…**
   to match many files at once by title or by a leading track number
   (`03 - Coastline.mp3`).
3. Pick a format and click **Process selected** or **Process all**.
4. Watch per-track and overall progress, **Cancel** if needed, **Retry** failures.
5. Download files individually or click **Download ZIP**.

The header chips show whether the API key and FFmpeg are configured.

## 8. Run the tests

```bash
pytest
```

124 tests cover URL validation, pagination (including more than 50 items),
deleted/private items, API error mapping, filename sanitisation, race-safe
unique filenames, real FFmpeg conversion to every format, cancellation,
retries, ZIP creation, upload limits, SSRF guards and path-traversal attempts.
FFmpeg-dependent tests are skipped automatically if FFmpeg isn't installed.
No real API key or network access is needed, because the YouTube API is stubbed.

## Project structure

```
playlist-audio-studio/
├── app.py                      Flask app, config, REST routes, error handling, security headers
├── services/
│   ├── playlist_service.py     YouTube Data API v3: playlist, paginated items, video details
│   ├── audio_service.py        Upload / allowlisted-URL ingestion, ffprobe check, FFmpeg conversion
│   ├── job_service.py          Background jobs: thread pool, progress, cancel, retry, cleanup
│   └── archive_service.py      ZIP creation with safe, de-duplicated names
├── utils/
│   ├── validators.py           Playlist URL parsing, IDs, formats, track lists, source URLs
│   └── file_utils.py           Filename sanitising, unique paths, containment checks
├── static/                     index.html, css/styles.css, js/app.js, favicon.svg
├── tests/                      pytest suite (+ conftest with a fake YouTube API)
├── requirements.txt
├── .env.example
├── pytest.ini
└── .gitignore
```

## REST API

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Status, FFmpeg/API-key availability, formats, limits |
| `POST /api/playlist` `{url}` | Validated, fully paginated, normalised playlist metadata |
| `POST /api/sources` (multipart `file`) | Upload an authorised audio file → `source_id` |
| `POST /api/sources/url` `{url}` | Fetch from an allowlisted https host (disabled by default) |
| `DELETE /api/sources/<source_id>` | Remove an uploaded source |
| `POST /api/process` `{format, items[], playlist_title}` | Start a job; `items` = `{track_id, position, title, source_id}` |
| `GET /api/jobs/<job_id>` | Overall + per-track status and progress |
| `POST /api/jobs/<job_id>/cancel` | Cancel queued and running tracks |
| `POST /api/jobs/<job_id>/retry` `{track_ids?, sources?}` | Retry failed/cancelled tracks, optionally with new sources |
| `GET /api/download/<file_id>` | Download one converted file |
| `GET /api/download-zip/<job_id>` | ZIP of all completed files in a job |

Errors are always JSON: `{"error": "human-readable message"}`.

## Security measures

- API key only in `.env`/environment; never returned, logged (urllib3 debug logging is silenced), or included in error messages.
- Playlist URLs: only http(s) on known YouTube hosts, no credentials or odd ports, strict ID pattern.
- Uploads: extension allowlist, streamed size limit, verified by ffprobe, stored under a random server-chosen name (client filenames are never used as paths).
- FFmpeg runs without a shell, with `-protocol_whitelist file` and `file:` paths, so inputs can't be interpreted as network URLs; it has a timeout and is killed on cancel.
- Output names come from sanitised titles and are reserved atomically (`O_EXCL`), so nothing is overwritten. Duplicates become `Title (2).mp3`.
- Downloads are served only by opaque IDs from the server's own registry; there are no filesystem paths in URLs.
- URL sources (optional): https only, exact-host allowlist, DNS must resolve to public IPs, no redirects, size-limited.
- Strict Content-Security-Policy, `nosniff`, `X-Frame-Options: DENY`, `no-referrer`; the frontend inserts all remote text with `textContent`.
- Unhandled errors return a generic 500 message; details go to the server log only.
- Temporary `.part` files, failed outputs and ZIPs are cleaned up; sources and outputs expire after `FILE_TTL_SECONDS`.

## Limitations

- **Audio must be supplied by you.** Tracks without an attached file are skipped.
  The playlist only provides names and order.
- **In-memory job manager.** Jobs live in the Flask process: restarting the
  server forgets them, and it must run as a single process (one Gunicorn worker
  or `python app.py`). To scale out, move `JobManager._submit` to a Celery task
  with Redis as broker and store job/item state in Redis or a database, keeping
  the same method signatures so routes don't change.
- Progress percentage needs a known duration (read by ffprobe). It falls back to
  queued → processing → done for formats that don't report one.
- Bulk matching is a title-similarity heuristic. Check the matches and fix any
  mismatches with the per-track button.
- The Flask dev server is for local use. For a shared deployment, run behind a
  production WSGI server and reverse proxy with HTTPS and authentication.
- The DNS check for URL sources narrows but can't fully eliminate DNS-rebinding
  attacks; only allowlist hosts you control or trust.

## Copyright and authorisation

You are responsible for having the rights to every audio file you process.
YouTube's Terms of Service prohibit downloading content except where YouTube
provides a download option. This project respects that by design: it uses
YouTube only for metadata, via the official API, and contains no code to fetch
YouTube media.
