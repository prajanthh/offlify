"""Offlify — Flask application and REST API.

Run with ``python app.py`` (see README for setup).
"""
from __future__ import annotations

import atexit
import logging
import os
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from services.archive_service import build_zip
from services.audio_service import AudioConverter, AudioError, SourceStore
from services.job_service import JobManager, JobNotFound
from services.playlist_service import PlaylistError, PlaylistService
from services.ytdlp_service import YtDlpDownloader
from utils.file_utils import ensure_dir, remove_path, sanitize_filename
from utils.validators import (
    ValidationError,
    link_kind,
    parse_allowed_hosts,
    validate_hex_id,
    validate_output_format,
    validate_track_items,
    validate_youtube_track_items,
)

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("playlist_audio_studio")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        log.warning("Ignoring invalid integer for %s", name)
        return default


def load_config() -> dict:
    load_dotenv(BASE_DIR / ".env")
    return {
        "YOUTUBE_API_KEY": os.environ.get("YOUTUBE_API_KEY", "").strip(),
        "DATA_DIR": os.environ.get("DATA_DIR", "").strip() or str(BASE_DIR / "data"),
        "MAX_UPLOAD_MB": _env_int("MAX_UPLOAD_MB", 200, 1),
        "MAX_PLAYLIST_ITEMS": _env_int("MAX_PLAYLIST_ITEMS", 500, 1),
        "MAX_TRACKS_PER_JOB": _env_int("MAX_TRACKS_PER_JOB", 200, 1),
        "WORKER_THREADS": _env_int("WORKER_THREADS", 2, 1),
        "FFMPEG_TIMEOUT_SECONDS": _env_int("FFMPEG_TIMEOUT_SECONDS", 900, 10),
        "FILE_TTL_SECONDS": _env_int("FILE_TTL_SECONDS", 7200, 60),
        "CLEANUP_INTERVAL_SECONDS": _env_int("CLEANUP_INTERVAL_SECONDS", 300, 0),
        "ALLOWED_SOURCE_HOSTS": os.environ.get("ALLOWED_SOURCE_HOSTS", ""),
        "RATE_LIMIT_PER_MINUTE": _env_int("RATE_LIMIT_PER_MINUTE", 0),
        "TRUSTED_PROXIES": _env_int("TRUSTED_PROXIES", 0),
        "HOST": os.environ.get("HOST", "127.0.0.1"),
        "PORT": _env_int("PORT", 5000, 1),
    }


def create_app(overrides: dict | None = None) -> Flask:
    config = {**load_config(), **(overrides or {})}

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # urllib3 logs full request URLs at DEBUG, which would include the API key.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
    max_upload = config["MAX_UPLOAD_MB"] * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = max_upload + 1024 * 1024  # multipart overhead
    if config["TRUSTED_PROXIES"]:
        # Behind a hosting platform's proxy, the visitor's address is in X-Forwarded-For.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config["TRUSTED_PROXIES"], x_proto=config["TRUSTED_PROXIES"])

    data_dir = ensure_dir(Path(config["DATA_DIR"]).resolve())
    work_dir = ensure_dir(data_dir / "tmp")
    for stale in work_dir.glob("*.zip"):
        remove_path(stale)

    converter = AudioConverter(timeout=config["FFMPEG_TIMEOUT_SECONDS"])
    sources = SourceStore(
        ensure_dir(data_dir / "sources"),
        converter,
        max_bytes=max_upload,
        allowed_hosts=parse_allowed_hosts(config["ALLOWED_SOURCE_HOSTS"]),
    )
    downloader = YtDlpDownloader(
        ensure_dir(data_dir / "ytdl"),
        ffmpeg_path=converter.ffmpeg,
    )
    jobs = JobManager(
        converter, sources, ensure_dir(data_dir / "outputs"),
        downloader=downloader,
        max_workers=config["WORKER_THREADS"], max_items_per_job=config["MAX_TRACKS_PER_JOB"],
    )
    playlists = PlaylistService(config["YOUTUBE_API_KEY"], max_items=config["MAX_PLAYLIST_ITEMS"])
    app.extensions["pas"] = {"config": config, "sources": sources, "jobs": jobs, "playlists": playlists,
                             "converter": converter, "downloader": downloader}

    def json_body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValidationError("The request body must be a JSON object.")
        return data

    # ------------------------------------------------------------- rate limit
    # Caps how many actions (fetches, downloads) one visitor can start per minute,
    # which protects the YouTube API quota and the server on a public deployment.
    rate_limit = config["RATE_LIMIT_PER_MINUTE"]
    hits: dict[str, deque] = defaultdict(deque)
    hits_lock = threading.Lock()

    @app.before_request
    def enforce_rate_limit():
        if not rate_limit or request.method != "POST" or not request.path.startswith("/api/"):
            return None
        now = time.monotonic()
        with hits_lock:
            if len(hits) > 10_000:
                for addr in [a for a, seen in hits.items() if not seen or now - seen[-1] > 60]:
                    del hits[addr]
            recent = hits[request.remote_addr or "unknown"]
            while recent and now - recent[0] > 60:
                recent.popleft()
            if len(recent) >= rate_limit:
                return jsonify(error="Too many requests. Please wait a minute and try again."), 429
            recent.append(now)
        return None

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # -------------------------------------------------------------------- API
    @app.get("/api/health")
    def health():
        return jsonify(
            status="ok",
            ffmpeg_available=converter.available,
            youtube_api_configured=playlists.configured,
            ytdlp_available=True,
            url_sources_enabled=bool(sources.allowed_hosts),
            formats=converter.format_options(),
            limits={
                "max_upload_mb": config["MAX_UPLOAD_MB"],
                "max_playlist_items": config["MAX_PLAYLIST_ITEMS"],
                "max_tracks_per_job": config["MAX_TRACKS_PER_JOB"],
            },
        )

    @app.post("/api/playlist")
    def playlist():
        body = json_body()
        return jsonify(playlists.fetch_playlist(body.get("url")))

    @app.post("/api/resolve")
    def resolve():
        """Load a pasted link as a playlist, or as a single video when it has no playlist."""
        url = json_body().get("url")
        if link_kind(url) == "video":
            return jsonify(playlists.fetch_video(url))
        return jsonify(playlists.fetch_playlist(url))

    @app.post("/api/sources")
    def upload_source():
        file = request.files.get("file")
        if file is None or not file.filename:
            raise ValidationError("Choose an audio file to upload.")
        record = sources.add_upload(file)
        return jsonify(SourceStore.public(record)), 201

    @app.post("/api/sources/url")
    def url_source():
        record = sources.add_from_url(json_body().get("url"))
        return jsonify(SourceStore.public(record)), 201

    @app.delete("/api/sources/<source_id>")
    def delete_source(source_id: str):
        if not sources.remove(validate_hex_id(source_id, "source ID")):
            return jsonify(error="Audio source not found."), 404
        return jsonify(deleted=True)

    @app.post("/api/process")
    def process():
        body = json_body()
        fmt = validate_output_format(body.get("format"), converter.available_formats())
        items = validate_track_items(body.get("items"), config["MAX_TRACKS_PER_JOB"])
        title = body.get("playlist_title") if isinstance(body.get("playlist_title"), str) else ""
        job = jobs.create_job(items, fmt, title[:200])
        return jsonify(job), 202

    @app.post("/api/process-youtube")
    def process_youtube():
        """Start a job that downloads audio from YouTube and converts it."""
        body = json_body()
        fmt = validate_output_format(body.get("format"), converter.available_formats())
        items = validate_youtube_track_items(body.get("items"), config["MAX_TRACKS_PER_JOB"])
        title = body.get("playlist_title") if isinstance(body.get("playlist_title"), str) else ""
        job = jobs.create_job(items, fmt, title[:200])
        return jsonify(job), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        return jsonify(jobs.get_job(validate_hex_id(job_id, "job ID")))

    @app.post("/api/jobs/<job_id>/cancel")
    def job_cancel(job_id: str):
        return jsonify(jobs.cancel(validate_hex_id(job_id, "job ID")))

    @app.post("/api/jobs/<job_id>/retry")
    def job_retry(job_id: str):
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            raise ValidationError("The request body must be a JSON object.")
        track_ids = body.get("track_ids")
        if track_ids is not None and not (isinstance(track_ids, list) and all(isinstance(t, str) for t in track_ids)):
            raise ValidationError("track_ids must be a list of strings.")
        new_sources = body.get("sources") or {}
        if not isinstance(new_sources, dict):
            raise ValidationError("sources must be an object mapping track_id to source_id.")
        new_sources = {str(k): validate_hex_id(v, "source ID") for k, v in new_sources.items()}
        return jsonify(jobs.retry(validate_hex_id(job_id, "job ID"), track_ids, new_sources)), 202

    @app.get("/api/download/<file_id>")
    def download(file_id: str):
        record = jobs.get_file(validate_hex_id(file_id, "file ID"))
        if record is None:
            return jsonify(error="File not found or expired."), 404
        return send_file(record["path"], mimetype=record["mimetype"], as_attachment=True,
                         download_name=record["filename"], max_age=0)

    @app.get("/api/download-zip/<job_id>")
    def download_zip(job_id: str):
        title, files = jobs.completed_files(validate_hex_id(job_id, "job ID"))
        if not files:
            return jsonify(error="There are no completed files in this job yet."), 404
        fd, tmp_name = tempfile.mkstemp(suffix=".zip", dir=work_dir)
        os.close(fd)
        tmp_path = Path(tmp_name)
        build_zip([(Path(f["path"]), f["filename"]) for f in files], tmp_path)
        response = send_file(tmp_path, mimetype="application/zip", as_attachment=True,
                             download_name=f"{sanitize_filename(title, fallback='playlist-audio')}.zip",
                             max_age=0)
        response.call_on_close(lambda: remove_path(tmp_path))
        return response

    # ----------------------------------------------------------------- errors
    @app.errorhandler(ValidationError)
    def on_validation(exc: ValidationError):
        return jsonify(error=str(exc)), exc.status_code

    @app.errorhandler(PlaylistError)
    @app.errorhandler(AudioError)
    def on_service_error(exc):
        return jsonify(error=exc.message), exc.status_code

    @app.errorhandler(JobNotFound)
    def on_job_missing(_exc):
        return jsonify(error="Job not found or expired."), 404

    @app.errorhandler(RequestEntityTooLarge)
    def on_too_large(_exc):
        return jsonify(error=f"The upload exceeds the {config['MAX_UPLOAD_MB']} MB limit."), 413

    @app.errorhandler(HTTPException)
    def on_http(exc: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description or exc.name), exc.code
        return exc

    @app.errorhandler(Exception)
    def on_unexpected(exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.path)
        return jsonify(error="An unexpected server error occurred."), 500

    @app.after_request
    def security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https://i.ytimg.com https://*.ytimg.com https://*.ggpht.com data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # ------------------------------------------------------- periodic cleanup
    interval = config["CLEANUP_INTERVAL_SECONDS"]
    if interval > 0:
        stop = threading.Event()

        def sweep() -> None:
            while not stop.wait(interval):
                try:
                    removed = jobs.cleanup_expired(config["FILE_TTL_SECONDS"])
                    removed += sources.cleanup_expired(config["FILE_TTL_SECONDS"])
                    removed += downloader.cleanup_expired(config["FILE_TTL_SECONDS"])
                    if removed:
                        log.info("Cleanup removed %d expired jobs/sources", removed)
                except Exception:  # noqa: BLE001
                    log.exception("Cleanup sweep failed")

        threading.Thread(target=sweep, name="cleanup", daemon=True).start()
        atexit.register(stop.set)

    if not playlists.configured:
        log.warning("YOUTUBE_API_KEY is not set: playlist fetching will return a configuration error.")
    if not converter.available:
        log.warning("ffmpeg/ffprobe not found on PATH: audio processing is disabled.")
    return app


if __name__ == "__main__":
    application = create_app()
    cfg = application.extensions["pas"]["config"]
    application.run(host=cfg["HOST"], port=cfg["PORT"], debug=False, threaded=True)
