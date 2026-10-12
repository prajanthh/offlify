import io
import time
import zipfile

import pytest

from conftest import FakeYouTube, requires_ffmpeg

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"


def upload(client, data, name="tone.wav"):
    return client.post("/api/sources", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def wait(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["finished"]:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_health(client):
    data = client.get("/api/health").get_json()
    assert data["status"] == "ok"
    assert data["youtube_api_configured"] is True
    assert "test-key-not-real" not in str(data)


def test_index_and_security_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Offlify" in resp.data
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/static/js/app.js").status_code == 200


def test_rate_limit_blocks_excess_posts(tmp_path):
    from app import create_app

    application = create_app({"DATA_DIR": str(tmp_path / "data"), "CLEANUP_INTERVAL_SECONDS": 0,
                              "YOUTUBE_API_KEY": "test-key-not-real", "RATE_LIMIT_PER_MINUTE": 2})
    try:
        client = application.test_client()
        codes = [client.post("/api/resolve", json={"url": ""}).status_code for _ in range(3)]
        assert codes == [400, 400, 429]
        assert client.get("/api/health").status_code == 200
    finally:
        application.extensions["pas"]["jobs"].shutdown()


def test_playlist_endpoint(app, client):
    app.extensions["pas"]["playlists"]._session = FakeYouTube(count=60, deleted={7})
    data = client.post("/api/playlist", json={"url": PLAYLIST_URL}).get_json()
    assert data["total_items"] == 60
    assert data["available_count"] == 59
    assert data["tracks"][6]["unavailable_reason"] == "Deleted video"


@pytest.mark.parametrize("body, status", [
    ({"url": "https://example.com/?list=PLabcdefghijklmnop"}, 400),
    ({"url": ""}, 400),
    ({}, 400),
])
def test_playlist_validation(client, body, status):
    resp = client.post("/api/playlist", json=body)
    assert resp.status_code == status
    assert "error" in resp.get_json()


def test_playlist_requires_json_object(client):
    resp = client.post("/api/playlist", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_missing_api_key_message(app, client):
    app.extensions["pas"]["playlists"]._api_key = ""
    resp = client.post("/api/playlist", json={"url": PLAYLIST_URL})
    assert resp.status_code == 503
    assert "YOUTUBE_API_KEY" in resp.get_json()["error"]


@requires_ffmpeg
def test_full_flow_upload_process_download_zip(client, tone_wav):
    resp = upload(client, tone_wav.read_bytes(), "My Song.wav")
    assert resp.status_code == 201
    source = resp.get_json()
    assert source["filename"] == "My Song.wav"
    assert "path" not in source  # server paths are never exposed

    items = [
        {"track_id": "t1", "position": 1, "title": "First", "source_id": source["id"]},
        {"track_id": "t2", "position": 2, "title": "First", "source_id": source["id"]},
    ]
    resp = client.post("/api/process", json={"format": "mp3", "items": items, "playlist_title": "Road/Trip"})
    assert resp.status_code == 202
    job = wait(client, resp.get_json()["id"])
    assert job["state"] == "completed"

    one = client.get(f"/api/download/{job['items'][0]['file_id']}")
    assert one.status_code == 200
    assert one.mimetype == "audio/mpeg"
    assert "01 - First.mp3" in one.headers["Content-Disposition"]

    z = client.get(f"/api/download-zip/{job['id']}")
    assert z.status_code == 200
    assert "Road_Trip.zip" in z.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(z.data)) as zf:
        assert zf.namelist() == ["01 - First.mp3", "02 - First.mp3"]


@requires_ffmpeg
def test_cancel_and_retry_endpoints(client, tone_wav):
    source = upload(client, tone_wav.read_bytes()).get_json()
    items = [{"track_id": "t1", "position": 1, "title": "x", "source_id": source["id"]}]
    job_id = client.post("/api/process", json={"format": "wav", "items": items}).get_json()["id"]
    wait(client, job_id)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    resp = client.post(f"/api/jobs/{job_id}/retry", json={})
    assert resp.status_code == 400  # nothing failed
    assert client.post(f"/api/jobs/{job_id}/retry", json={"sources": {"t1": "../etc"}}).status_code == 400


def test_process_validation(client):
    good_item = {"track_id": "t1", "position": 1, "title": "x", "source_id": "a" * 32}
    assert client.post("/api/process", json={"format": "exe", "items": [good_item]}).status_code == 400
    assert client.post("/api/process", json={"format": "mp3", "items": []}).status_code == 400
    resp = client.post("/api/process", json={"format": "mp3", "items": [good_item]})
    assert resp.status_code == 400
    assert "expired" in resp.get_json()["error"]


def test_upload_validation(client):
    assert upload(client, b"MZ\x90\x00", "virus.exe").status_code == 415
    assert client.post("/api/sources", data={}, content_type="multipart/form-data").status_code == 400
    big = upload(client, b"\0" * (6 * 1024 * 1024), "big.wav")
    assert big.status_code == 413
    assert "MB" in big.get_json()["error"]


def test_url_sources_disabled(client):
    resp = client.post("/api/sources/url", json={"url": "https://media.example.org/a.mp3"})
    assert resp.status_code == 403


@pytest.mark.parametrize("path", [
    "/api/download/../../etc/passwd",
    "/api/download/..%2F..%2Fetc%2Fpasswd",
    "/api/download/" + "a" * 32,
    "/api/download-zip/" + "b" * 32,
    "/api/jobs/" + "c" * 32,
    "/api/jobs/not-a-job",
])
def test_downloads_only_serve_registered_files(client, path):
    resp = client.get(path)
    assert resp.status_code in (400, 404)
    assert b"root:" not in resp.data
    assert resp.is_json


def test_unknown_api_route_returns_json(client):
    resp = client.get("/api/nope")
    assert resp.status_code == 404 and resp.is_json


def test_internal_errors_are_generic(app, client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail /srv/path")

    monkeypatch.setattr(app.extensions["pas"]["playlists"], "fetch_playlist", boom)
    resp = client.post("/api/playlist", json={"url": PLAYLIST_URL})
    assert resp.status_code == 500
    assert "secret" not in resp.get_json()["error"]
