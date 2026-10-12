"""In-process background job manager for audio conversion.

Limitations (by design for the first version):
* State lives in memory: restarting the server forgets jobs (files on disk
  are cleaned up by the TTL sweep on the next run).
* Works with a single server process only. Running Flask under several
  Gunicorn workers would give each worker its own, separate job table.
* Throughput is bounded by ``max_workers`` FFmpeg processes on one machine.

To scale out, replace ``JobManager._submit`` with a Celery task (Redis or
RabbitMQ broker), store job/item state in Redis or a database, and keep the
same public methods so the Flask routes don't change. See README.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from services.audio_service import FORMAT_SPECS, AudioConverter, AudioError, ConversionCancelled, SourceStore
from services.ytdlp_service import DownloadError, YtDlpDownloader
from utils.file_utils import ensure_dir, format_bytes, remove_path, reserve_unique_path, sanitize_filename
from utils.validators import ValidationError

log = logging.getLogger(__name__)

ACTIVE = ("queued", "processing")
TERMINAL = ("completed", "failed", "cancelled")


@dataclass
class JobItem:
    track_id: str
    position: int
    title: str
    source_id: str = ""
    video_id: str = ""
    status: str = "queued"
    progress: float = 0.0
    error: str | None = None
    file_id: str | None = None
    filename: str | None = None
    size: int | None = None
    attempts: int = 0


@dataclass
class Job:
    id: str
    format: str
    title: str
    directory: Path
    items: dict[str, JobItem]
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobNotFound(Exception):
    pass


class JobManager:
    def __init__(
        self,
        converter: AudioConverter,
        sources: SourceStore,
        output_dir: Path,
        *,
        downloader: YtDlpDownloader | None = None,
        max_workers: int = 2,
        max_items_per_job: int = 200,
    ):
        self._converter = converter
        self._sources = sources
        self._downloader = downloader
        self._output_dir = ensure_dir(output_dir)
        self.max_items_per_job = max_items_per_job
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="convert")
        self._jobs: dict[str, Job] = {}
        self._files: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- lifecycle
    def create_job(self, items: list[dict], fmt: str, title: str = "") -> dict:
        if fmt not in FORMAT_SPECS:
            raise ValidationError("Unsupported output format.")
        # Only validate sources for items that use the source-based path
        source_items = [i for i in items if i.get("source_id")]
        missing = [i["title"] for i in source_items if self._sources.get(i["source_id"]) is None]
        if missing:
            raise ValidationError(
                f"The audio source for '{missing[0]}' has expired or was removed. Please attach it again."
            )
        # Items with video_id use the YouTube download path
        yt_items = [i for i in items if i.get("video_id") and not i.get("source_id")]
        if yt_items and not self._downloader:
            raise ValidationError("YouTube downloading is not available on this server.")

        job_id = uuid.uuid4().hex
        # Build JobItem kwargs, only include fields that exist in the dataclass
        def _make_item(d: dict) -> JobItem:
            return JobItem(
                track_id=d["track_id"],
                position=d["position"],
                title=d["title"],
                source_id=d.get("source_id", ""),
                video_id=d.get("video_id", ""),
            )

        job = Job(
            id=job_id,
            format=fmt,
            title=sanitize_filename(title, fallback="playlist-audio"),
            directory=ensure_dir(self._output_dir / job_id),
            items={i["track_id"]: _make_item(i) for i in sorted(items, key=lambda x: x["position"])},
        )
        with self._lock:
            self._jobs[job_id] = job
        for item in job.items.values():
            self._submit(job, item)
        return self.get_job(job_id)

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self._require_job(job_id)
            job.cancel_event.set()
            for item in job.items.values():
                if item.status == "queued":
                    item.status = "cancelled"
                    item.error = "Cancelled before processing started."
        return self.get_job(job_id)

    def retry(self, job_id: str, track_ids: list[str] | None = None, sources: dict[str, str] | None = None) -> dict:
        """Re-queue failed/cancelled items, optionally with newly attached sources."""
        sources = sources or {}
        with self._lock:
            job = self._require_job(job_id)
            candidates = [
                item for item in job.items.values()
                if item.status in ("failed", "cancelled") and (track_ids is None or item.track_id in track_ids)
            ]
            if not candidates:
                raise ValidationError("There are no failed or cancelled tracks to retry.")
            chosen = {item.track_id: sources.get(item.track_id) or item.source_id for item in candidates}
            for item in candidates:
                if self._sources.get(chosen[item.track_id]) is None:
                    raise ValidationError(
                        f"The audio source for '{item.title}' has expired. Attach it again, then retry."
                    )
            if job.cancel_event.is_set():
                # Running items keep the old (set) event; new work gets a fresh one.
                job.cancel_event = threading.Event()
            for item in candidates:
                item.source_id = chosen[item.track_id]
                item.status, item.progress, item.error = "queued", 0.0, None
        for item in candidates:
            self._submit(job, item)
        return self.get_job(job_id)

    def shutdown(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                job.cancel_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def cleanup_expired(self, max_age_seconds: float) -> int:
        """Remove finished jobs (and their files) older than ``max_age_seconds``."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            expired = [
                job for job in self._jobs.values()
                if job.created_at < cutoff and not any(i.status in ACTIVE for i in job.items.values())
            ]
            for job in expired:
                del self._jobs[job.id]
                for file_id in [fid for fid, rec in self._files.items() if rec["job_id"] == job.id]:
                    del self._files[file_id]
        for job in expired:
            remove_path(job.directory)
        return len(expired)

    # ---------------------------------------------------------------- queries
    def get_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._require_job(job_id)
            items = sorted(job.items.values(), key=lambda i: i.position)
            total = len(items)
            counts = {s: sum(1 for i in items if i.status == s) for s in (*ACTIVE, *TERMINAL)}
            overall = sum(100.0 if i.status in TERMINAL else i.progress for i in items) / total if total else 100.0

            if counts["queued"] or counts["processing"]:
                state = "cancelling" if job.cancel_event.is_set() else "running"
            elif counts["completed"] == total:
                state = "completed"
            elif counts["completed"]:
                state = "partial"
            elif counts["cancelled"]:
                state = "cancelled"
            else:
                state = "failed"

            return {
                "id": job.id,
                "format": job.format,
                "state": state,
                "finished": state not in ("running", "cancelling"),
                "progress": round(overall, 1),
                "counts": {"total": total, **counts},
                "items": [self._item_dict(i) for i in items],
            }

    def get_file(self, file_id: str) -> dict | None:
        with self._lock:
            record = self._files.get(file_id)
        if record and Path(record["path"]).is_file():
            return record
        return None

    def completed_files(self, job_id: str) -> tuple[str, list[dict]]:
        with self._lock:
            job = self._require_job(job_id)
            ids = [i.file_id for i in sorted(job.items.values(), key=lambda i: i.position)
                   if i.status == "completed" and i.file_id]
            title = job.title
        files = [rec for rec in (self.get_file(fid) for fid in ids) if rec]
        return title, files

    # -------------------------------------------------------------- internals
    def _require_job(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    def _item_dict(self, item: JobItem) -> dict:
        return {
            "track_id": item.track_id,
            "position": item.position,
            "title": item.title,
            "status": item.status,
            "progress": round(item.progress, 1),
            "error": item.error,
            "file_id": item.file_id,
            "filename": item.filename,
            "size": item.size,
            "size_text": format_bytes(item.size),
            "attempts": item.attempts,
        }

    def _submit(self, job: Job, item: JobItem) -> None:
        self._executor.submit(self._run_item, job.id, item.track_id)

    def _run_item(self, job_id: str, track_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            item = job.items[track_id]
            if item.status != "queued":
                return
            cancel_event = job.cancel_event
            if cancel_event.is_set():
                item.status, item.error = "cancelled", "Cancelled before processing started."
                return
            item.status, item.attempts = "processing", item.attempts + 1
            source_id, video_id = item.source_id, item.video_id
            position, title = item.position, item.title
            directory, fmt = job.directory, job.format

        dest: Path | None = None
        downloaded_record: dict | None = None
        try:
            def on_progress(fraction: float) -> None:
                with self._lock:
                    item.progress = round(min(max(fraction, 0.0), 1.0) * 100, 1)

            spec = FORMAT_SPECS[fmt]

            if video_id and self._downloader:
                # ---- YouTube download path ----
                # Phase 1: Download from YouTube (0–80% of progress)
                def on_download_progress(fraction: float) -> None:
                    on_progress(fraction * 0.80)

                downloaded_record = self._downloader.download(
                    video_id, title,
                    progress_cb=on_download_progress,
                    cancel_event=cancel_event,
                )
                source_path = downloaded_record["path"]
                source_duration = downloaded_record.get("duration")

                # Phase 2: Convert to target format (80–100% of progress)
                dest = reserve_unique_path(directory, f"{position:02d} - {title}", spec.extension)

                if fmt == "mp3" and str(source_path).endswith(".mp3"):
                    # Already in the right format, just move it
                    import shutil
                    shutil.copy2(source_path, dest)
                    on_progress(1.0)
                else:
                    def on_convert_progress(fraction: float) -> None:
                        on_progress(0.80 + fraction * 0.20)

                    self._converter.convert(
                        source_path, dest, fmt,
                        duration=source_duration, title=title,
                        progress_cb=on_convert_progress, cancel_event=cancel_event,
                    )
            else:
                # ---- Original source-based path ----
                source = self._sources.get(source_id)
                if source is None:
                    raise AudioError("The audio source has expired or was removed. Attach it again and retry.")
                dest = reserve_unique_path(directory, f"{position:02d} - {title}", spec.extension)

                self._converter.convert(
                    source["path"], dest, fmt,
                    duration=source.get("duration"), title=title,
                    progress_cb=on_progress, cancel_event=cancel_event,
                )
        except ConversionCancelled:
            remove_path(dest)
            self._finish(item, "cancelled", "Cancelled during processing.")
        except (AudioError, DownloadError) as exc:
            remove_path(dest)
            self._finish(item, "failed", exc.message)
        except Exception:  # noqa: BLE001 - keep the worker alive, report generically
            log.exception("Unexpected error processing track %s of job %s", track_id, job_id)
            remove_path(dest)
            self._finish(item, "failed", "Unexpected error while processing this track.")
        else:
            file_id = uuid.uuid4().hex
            size = dest.stat().st_size
            with self._lock:
                self._files[file_id] = {
                    "id": file_id, "job_id": job_id, "path": dest, "filename": dest.name,
                    "size": size, "mimetype": FORMAT_SPECS[fmt].mimetype,
                }
                item.file_id, item.filename, item.size = file_id, dest.name, size
            self._finish(item, "completed", None)
        finally:
            # Clean up the intermediate downloaded file
            if downloaded_record and self._downloader:
                self._downloader.remove(downloaded_record["id"])

    def _finish(self, item: JobItem, status: str, error: str | None) -> None:
        with self._lock:
            item.status, item.error = status, error
            if status == "completed":
                item.progress = 100.0
