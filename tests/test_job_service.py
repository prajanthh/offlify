import time

import pytest

from conftest import make_tone, requires_ffmpeg
from services.audio_service import AudioConverter, SourceStore
from services.job_service import JobManager, JobNotFound
from utils.validators import ValidationError


class Upload:
    def __init__(self, filename, data):
        import io
        self.filename = filename
        self.stream = io.BytesIO(data)


@pytest.fixture
def setup(tmp_path):
    converter = AudioConverter(timeout=60)
    (tmp_path / "src").mkdir()
    store = SourceStore(tmp_path / "src", converter, max_bytes=50 * 1024 * 1024)
    jobs = JobManager(converter, store, tmp_path / "out", max_workers=2)
    yield converter, store, jobs, tmp_path
    jobs.shutdown()


def wait(jobs, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.get_job(job_id)
        if job["finished"]:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def item(n, source_id, title=None):
    return {"track_id": f"t{n}", "position": n, "title": title or f"Song {n}", "source_id": source_id}


@requires_ffmpeg
def test_job_converts_all_tracks_with_unique_names(setup, tone_wav):
    _, store, jobs, _ = setup
    src = store.add_upload(Upload("tone.wav", tone_wav.read_bytes()))
    job = jobs.create_job([item(1, src["id"], "Same / Title"), item(2, src["id"], "Same / Title")], "mp3", "My List")
    assert job["state"] in ("running", "completed")

    done = wait(jobs, job["id"])
    assert done["state"] == "completed"
    assert done["progress"] == 100
    names = [i["filename"] for i in done["items"]]
    assert names == ["01 - Same _ Title.mp3", "02 - Same _ Title.mp3"]
    title, files = jobs.completed_files(job["id"])
    assert title == "My List" and len(files) == 2
    assert jobs.get_file(done["items"][0]["file_id"])["size"] > 0


@requires_ffmpeg
def test_failed_track_is_reported_and_retry_with_new_source_works(setup, tone_wav, monkeypatch):
    converter, store, jobs, _ = setup
    good = store.add_upload(Upload("tone.wav", tone_wav.read_bytes()))
    real_convert = converter.convert
    calls = {"n": 0}

    def flaky(src, dest, fmt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            from services.audio_service import AudioError
            raise AudioError("Simulated decoder failure.", 422)
        return real_convert(src, dest, fmt, **kw)

    monkeypatch.setattr(converter, "convert", flaky)
    job = jobs.create_job([item(1, good["id"])], "wav")
    done = wait(jobs, job["id"])
    assert done["state"] == "failed"
    assert done["items"][0]["error"] == "Simulated decoder failure."
    assert list((setup[3] / "out" / job["id"]).iterdir()) == []  # placeholder removed

    replacement = store.add_upload(Upload("tone2.wav", tone_wav.read_bytes()))
    jobs.retry(job["id"], ["t1"], {"t1": replacement["id"]})
    done = wait(jobs, job["id"])
    assert done["state"] == "completed"
    assert done["items"][0]["attempts"] == 2


@requires_ffmpeg
def test_cancel_stops_running_and_queued_tracks(setup):
    _, store, jobs, tmp_path = setup
    long_tone = make_tone(tmp_path / "long.wav", seconds=180)
    src = store.add_upload(Upload("long.wav", long_tone.read_bytes()))
    job = jobs.create_job([item(n, src["id"]) for n in range(1, 6)], "flac")
    time.sleep(0.4)
    jobs.cancel(job["id"])
    done = wait(jobs, job["id"])
    assert done["state"] == "cancelled"
    assert all(i["status"] == "cancelled" for i in done["items"])
    leftovers = list((tmp_path / "out" / job["id"]).iterdir())
    assert leftovers == []


def test_retry_without_failures_and_unknown_job(setup):
    _, _, jobs, _ = setup
    with pytest.raises(JobNotFound):
        jobs.get_job("0" * 32)
    with pytest.raises(ValidationError):
        jobs.create_job([item(1, "a" * 32)], "mp3")  # unknown source


@requires_ffmpeg
def test_cleanup_removes_expired_jobs(setup, tone_wav):
    _, store, jobs, tmp_path = setup
    src = store.add_upload(Upload("tone.wav", tone_wav.read_bytes()))
    job = jobs.create_job([item(1, src["id"])], "mp3")
    done = wait(jobs, job["id"])
    file_id = done["items"][0]["file_id"]
    assert jobs.cleanup_expired(3600) == 0
    assert jobs.cleanup_expired(-1) == 1
    assert jobs.get_file(file_id) is None
    assert not (tmp_path / "out" / job["id"]).exists()
    assert store.cleanup_expired(-1) == 1
