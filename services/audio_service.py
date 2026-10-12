"""Authorised audio ingestion and FFmpeg conversion.

Audio enters the app in exactly two ways:

1. **Upload** – the user uploads a file they own or are licensed to use.
2. **Permitted URL** – an https URL on a host the server operator has
   explicitly allowlisted (``ALLOWED_SOURCE_HOSTS``), e.g. the operator's own
   media server. Disabled unless configured.

There is deliberately no code path that fetches audio from YouTube.
"""
from __future__ import annotations

import collections
import ipaddress
import json
import logging
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from utils.file_utils import format_bytes, format_duration, remove_path, sanitize_filename
from utils.validators import validate_source_url

log = logging.getLogger(__name__)

INPUT_EXTENSIONS = frozenset(
    {"mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "aif", "aiff", "wma", "webm", "mka", "mp4"}
)
CHUNK_SIZE = 1024 * 1024


class AudioError(Exception):
    """User-facing error during ingestion or conversion."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ConversionCancelled(AudioError):
    def __init__(self) -> None:
        super().__init__("Cancelled.", 409)


@dataclass(frozen=True)
class FormatSpec:
    key: str
    label: str
    encoder: str
    muxer: str
    extension: str
    mimetype: str
    codec_args: tuple[str, ...] = field(default_factory=tuple)


FORMAT_SPECS: dict[str, FormatSpec] = {
    "mp3": FormatSpec("mp3", "MP3 · 192 kbps", "libmp3lame", "mp3", "mp3", "audio/mpeg",
                      ("-c:a", "libmp3lame", "-b:a", "192k")),
    "wav": FormatSpec("wav", "WAV · 16-bit PCM", "pcm_s16le", "wav", "wav", "audio/wav",
                      ("-c:a", "pcm_s16le")),
    "flac": FormatSpec("flac", "FLAC · lossless", "flac", "flac", "flac", "audio/flac",
                       ("-c:a", "flac")),
    "ogg": FormatSpec("ogg", "OGG Vorbis · q5", "libvorbis", "ogg", "ogg", "audio/ogg",
                      ("-c:a", "libvorbis", "-q:a", "5")),
    "m4a": FormatSpec("m4a", "M4A (AAC) · 192 kbps", "aac", "ipod", "m4a", "audio/mp4",
                      ("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart")),
}


class AudioConverter:
    """Thin, safe wrapper around the ffmpeg/ffprobe executables."""

    def __init__(self, ffmpeg_path: str | None = None, ffprobe_path: str | None = None, timeout: int = 900):
        self.ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
        self.ffprobe = ffprobe_path or shutil.which("ffprobe")
        self.timeout = timeout
        self._formats: list[str] | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def available_formats(self) -> list[str]:
        """Formats whose encoder is compiled into the local FFmpeg build."""
        with self._lock:
            if self._formats is None:
                self._formats = self._detect_formats()
            return list(self._formats)

    def format_options(self) -> list[dict]:
        return [{"value": k, "label": FORMAT_SPECS[k].label} for k in self.available_formats()]

    def _detect_formats(self) -> list[str]:
        if not self.available:
            return []
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            log.exception("Could not list FFmpeg encoders")
            return []
        encoders = {line.split()[1] for line in result.stdout.splitlines() if len(line.split()) > 1}
        return [key for key, spec in FORMAT_SPECS.items() if spec.encoder in encoders]

    def _require(self) -> None:
        if not self.available:
            raise AudioError("FFmpeg is not installed on the server (see README).", 503)

    def probe(self, path: Path) -> dict:
        """Confirm the file contains audio; return duration and codec."""
        self._require()
        cmd = [
            self.ffprobe, "-v", "error", "-protocol_whitelist", "file",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name:format=duration,format_name",
            "-of", "json", f"file:{Path(path).resolve()}",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AudioError("Reading the audio file took too long.", 422) from exc
        except OSError as exc:
            raise AudioError("Could not run ffprobe on the server.", 503) from exc
        if result.returncode != 0:
            raise AudioError("The file could not be read as audio. It may be corrupt or not an audio file.", 422)
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise AudioError("The file could not be read as audio.", 422) from exc
        streams = data.get("streams") or []
        if not streams:
            raise AudioError("The file doesn't contain an audio stream.", 422)
        try:
            duration = float((data.get("format") or {}).get("duration"))
        except (TypeError, ValueError):
            duration = None
        return {"duration": duration, "codec": streams[0].get("codec_name")}

    def convert(
        self,
        source: Path,
        dest: Path,
        fmt: str,
        *,
        duration: float | None = None,
        title: str | None = None,
        progress_cb: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        """Convert ``source`` to ``fmt`` at ``dest``.

        Output is written to ``<dest>.part`` and moved into place only on
        success, so a failed or cancelled run never leaves a broken file at
        ``dest``. ``-protocol_whitelist file`` and the ``file:`` prefix stop
        FFmpeg from interpreting paths as network URLs or special protocols.
        """
        self._require()
        spec = FORMAT_SPECS.get(fmt)
        if spec is None or fmt not in self.available_formats():
            raise AudioError(f"Output format '{fmt}' is not supported on this server.")

        source = Path(source).resolve()
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".part")
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file", "-i", f"file:{source}",
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            *spec.codec_args,
        ]
        if title:
            cmd += ["-metadata", f"title={title[:200]}"]
        cmd += ["-f", spec.muxer, "-progress", "pipe:1", "-nostats", f"file:{tmp.resolve()}"]

        cancel_event = cancel_event or threading.Event()
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            raise AudioError("Could not start FFmpeg on the server.", 503) from exc

        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        stopped = {"cancelled": False, "timed_out": False}

        def drain_stderr() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_tail.append(line.rstrip())

        def watchdog() -> None:
            deadline = time.monotonic() + self.timeout
            while proc.poll() is None:
                if cancel_event.wait(0.2):
                    stopped["cancelled"] = True
                    proc.kill()
                    return
                if time.monotonic() > deadline:
                    stopped["timed_out"] = True
                    proc.kill()
                    return

        threads = [threading.Thread(target=drain_stderr, daemon=True), threading.Thread(target=watchdog, daemon=True)]
        for t in threads:
            t.start()

        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and duration and value.isdigit():
                    fraction = int(value) / 1_000_000 / duration
                    if progress_cb:
                        progress_cb(max(0.0, min(fraction, 0.99)))
                elif key == "progress" and value == "end" and progress_cb:
                    progress_cb(1.0)
            proc.wait()
        finally:
            for t in threads:
                t.join(timeout=5)

        if stopped["cancelled"]:
            remove_path(tmp)
            raise ConversionCancelled()
        if stopped["timed_out"]:
            remove_path(tmp)
            raise AudioError(f"Conversion exceeded the {self.timeout}s time limit.", 504)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            remove_path(tmp)
            detail = " | ".join(stderr_tail).replace(str(source), "<input>").replace(str(tmp.resolve()), "<output>")
            log.warning("FFmpeg failed (exit %s): %s", proc.returncode, detail[-500:])
            raise AudioError("FFmpeg could not convert this file. It may be corrupt or use an unsupported codec.", 422)

        tmp.replace(dest)
        return dest


def _assert_public_host(hostname: str) -> None:
    """Refuse hosts that resolve to private, loopback, link-local or reserved IPs."""
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise AudioError("Could not resolve the source host.", 400) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise AudioError("The source host resolves to a non-public address and is not allowed.", 400)


class SourceStore:
    """Holds validated, authorised audio sources on disk, keyed by random IDs."""

    def __init__(
        self,
        directory: Path,
        converter: AudioConverter,
        *,
        max_bytes: int,
        allowed_hosts: frozenset[str] = frozenset(),
        url_timeout: float = 20.0,
        session: requests.Session | None = None,
    ):
        self.directory = Path(directory)
        self.converter = converter
        self.max_bytes = max_bytes
        self.allowed_hosts = allowed_hosts
        self.url_timeout = url_timeout
        self._session = session or requests.Session()
        self._sources: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------------------- ingestion
    def add_upload(self, file_storage) -> dict:
        original = getattr(file_storage, "filename", "") or ""
        ext = self._check_extension(original)
        source_id = uuid.uuid4().hex
        path = self.directory / f"{source_id}.{ext}"
        self._write_limited(file_storage.stream, path)
        return self._register(source_id, path, original, origin="upload")

    def add_from_url(self, url: str) -> dict:
        if not self.allowed_hosts:
            raise AudioError(
                "Fetching audio from URLs is disabled on this server. Upload the audio file instead.", 403
            )
        url = validate_source_url(url, self.allowed_hosts)
        parsed = urlparse(url)
        _assert_public_host(parsed.hostname or "")

        original = unquote(PurePosixPath(parsed.path).name)
        ext = self._check_extension(original)
        try:
            response = self._session.get(
                url, stream=True, timeout=self.url_timeout, allow_redirects=False,
                headers={"User-Agent": "PlaylistAudioStudio/1.0"},
            )
        except requests.RequestException as exc:
            raise AudioError("Could not download the source file.", 502) from exc

        with response:
            if 300 <= response.status_code < 400:
                raise AudioError("The source URL redirects; redirects are not followed.", 400)
            if response.status_code != 200:
                raise AudioError(f"The source server returned HTTP {response.status_code}.", 502)
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > self.max_bytes:
                raise AudioError(self._too_large_message(), 413)
            source_id = uuid.uuid4().hex
            path = self.directory / f"{source_id}.{ext}"
            self._write_chunks(response.iter_content(CHUNK_SIZE), path)
        return self._register(source_id, path, original, origin="url")

    # ---------------------------------------------------------------- queries
    def get(self, source_id: str) -> dict | None:
        with self._lock:
            record = self._sources.get(source_id)
        if record and record["path"].exists():
            return record
        return None

    def remove(self, source_id: str) -> bool:
        with self._lock:
            record = self._sources.pop(source_id, None)
        if record:
            remove_path(record["path"])
        return record is not None

    def cleanup_expired(self, max_age_seconds: float) -> int:
        cutoff = time.time() - max_age_seconds
        with self._lock:
            expired = [sid for sid, rec in self._sources.items() if rec["created_at"] < cutoff]
            records = [self._sources.pop(sid) for sid in expired]
        for record in records:
            remove_path(record["path"])
        return len(records)

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "id": record["id"],
            "filename": record["filename"],
            "size": record["size"],
            "size_text": format_bytes(record["size"]),
            "duration": record["duration"],
            "duration_text": format_duration(record["duration"]),
            "codec": record["codec"],
            "origin": record["origin"],
        }

    # -------------------------------------------------------------- internals
    def _check_extension(self, filename: str) -> str:
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext not in INPUT_EXTENSIONS:
            allowed = ", ".join(sorted(INPUT_EXTENSIONS))
            shown = f"'.{ext}'" if ext else "without an extension"
            raise AudioError(f"Unsupported file type {shown}. Supported: {allowed}.", 415)
        return ext

    def _too_large_message(self) -> str:
        return f"The file is larger than the {self.max_bytes // (1024 * 1024)} MB limit."

    def _write_limited(self, stream, path: Path) -> None:
        self._write_chunks(iter(lambda: stream.read(CHUNK_SIZE), b""), path)

    def _write_chunks(self, chunks, path: Path) -> None:
        total = 0
        try:
            with open(path, "xb") as fh:
                for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise AudioError(self._too_large_message(), 413)
                    fh.write(chunk)
        except BaseException:
            remove_path(path)
            raise
        if total == 0:
            remove_path(path)
            raise AudioError("The file is empty.", 400)

    def _register(self, source_id: str, path: Path, original: str, origin: str) -> dict:
        try:
            info = self.converter.probe(path)
        except BaseException:
            remove_path(path)
            raise
        stem = sanitize_filename(Path(original).stem, fallback="audio")
        record = {
            "id": source_id,
            "path": path,
            "filename": f"{stem}{Path(original).suffix.lower()}",
            "size": path.stat().st_size,
            "duration": info["duration"],
            "codec": info["codec"],
            "origin": origin,
            "created_at": time.time(),
        }
        with self._lock:
            self._sources[source_id] = record
        return record
