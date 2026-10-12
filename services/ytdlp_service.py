"""YouTube audio downloader using yt-dlp.

Downloads the best available audio stream from a YouTube video and converts
it to MP3 using yt-dlp's built-in FFmpeg post-processor.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path

import yt_dlp

from utils.file_utils import format_bytes, format_duration, remove_path, sanitize_filename

log = logging.getLogger(__name__)


class DownloadError(Exception):
    """User-facing error during a yt-dlp download."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class YtDlpDownloader:
    """Downloads audio from YouTube videos as MP3 files.

    Each download is stored in ``directory`` under a random filename so there
    are no conflicts.  A record dict is returned with the metadata the rest of
    the app needs.
    """

    def __init__(self, directory: Path, *, ffmpeg_path: str | None = None, timeout: int = 600):
        self.directory = Path(directory)
        self.ffmpeg_path = ffmpeg_path
        self.timeout = timeout
        self._downloads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def download(
        self,
        video_id: str,
        title: str = "",
        *,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        """Download audio for a YouTube video and return a record dict.

        Parameters
        ----------
        video_id:
            The 11-character YouTube video ID (e.g. ``dQw4w9WgXcQ``).
        title:
            Desired filename stem (sanitised internally).
        progress_cb:
            ``progress_cb(fraction)`` called with a 0.0–1.0 float during download.
        cancel_event:
            If set, the download is aborted.

        Returns
        -------
        dict with keys: ``id``, ``path``, ``filename``, ``size``, ``duration``,
        ``codec``, ``origin``.
        """
        cancel_event = cancel_event or threading.Event()
        download_id = uuid.uuid4().hex
        safe_title = sanitize_filename(title, fallback=video_id)
        out_path = self.directory / f"{download_id}.%(ext)s"
        url = f"https://www.youtube.com/watch?v={video_id}"

        last_progress = {"pct": 0.0}

        def _progress_hook(d):
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("Download cancelled by user.")

            if d.get("status") == "downloading" and progress_cb:
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total and total > 0:
                    fraction = min(downloaded / total, 0.95)
                    last_progress["pct"] = fraction
                    progress_cb(fraction)
            elif d.get("status") == "finished" and progress_cb:
                progress_cb(0.97)

        def _postprocessor_hook(d):
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("Download cancelled by user.")
            if d.get("status") == "finished" and progress_cb:
                progress_cb(1.0)

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_path),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "progress_hooks": [_progress_hook],
            "postprocessor_hooks": [_postprocessor_hook],
            "socket_timeout": 30,
        }
        if self.ffmpeg_path:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadCancelled:
            self._cleanup_partial(download_id)
            raise DownloadError("Download was cancelled.", 409)
        except yt_dlp.utils.DownloadError as exc:
            self._cleanup_partial(download_id)
            msg = str(exc)
            if "private" in msg.lower() or "sign in" in msg.lower():
                raise DownloadError("This video is private or requires sign-in.", 403) from exc
            if "not available" in msg.lower() or "unavailable" in msg.lower():
                raise DownloadError("This video is unavailable.", 404) from exc
            if "copyright" in msg.lower():
                raise DownloadError("This video is blocked due to copyright.", 403) from exc
            raise DownloadError(f"Could not download audio: {msg[:200]}", 502) from exc
        except Exception as exc:
            self._cleanup_partial(download_id)
            log.exception("Unexpected yt-dlp error for video %s", video_id)
            raise DownloadError("An unexpected error occurred during download.", 500) from exc

        # yt-dlp writes the file with the final extension after post-processing.
        final_path = self.directory / f"{download_id}.mp3"
        if not final_path.exists():
            # Fallback: look for any file matching the download ID
            candidates = list(self.directory.glob(f"{download_id}.*"))
            if candidates:
                final_path = candidates[0]
            else:
                raise DownloadError("Download completed but the output file was not found.", 500)

        duration = info.get("duration")
        file_size = final_path.stat().st_size

        record = {
            "id": download_id,
            "path": final_path,
            "filename": f"{safe_title}.mp3",
            "size": file_size,
            "size_text": format_bytes(file_size),
            "duration": duration,
            "duration_text": format_duration(duration),
            "codec": "mp3",
            "origin": "youtube",
            "created_at": time.time(),
        }
        with self._lock:
            self._downloads[download_id] = record
        return record

    def get(self, download_id: str) -> dict | None:
        """Retrieve a download record by its ID."""
        with self._lock:
            record = self._downloads.get(download_id)
        if record and record["path"].exists():
            return record
        return None

    def remove(self, download_id: str) -> bool:
        """Remove a downloaded file and its record."""
        with self._lock:
            record = self._downloads.pop(download_id, None)
        if record:
            remove_path(record["path"])
        return record is not None

    def cleanup_expired(self, max_age_seconds: float) -> int:
        """Remove downloads older than ``max_age_seconds``."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            expired = [did for did, rec in self._downloads.items() if rec["created_at"] < cutoff]
            records = [self._downloads.pop(did) for did in expired]
        for record in records:
            remove_path(record["path"])
        return len(records)

    def _cleanup_partial(self, download_id: str) -> None:
        """Remove any partial files left by a failed/cancelled download."""
        for path in self.directory.glob(f"{download_id}.*"):
            remove_path(path)
