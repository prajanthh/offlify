import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg is not installed")


def make_tone(path: Path, seconds: float = 2.0) -> Path:
    """Generate a short sine-wave WAV file with FFmpeg's lavfi source."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def tone_wav(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg is not installed")
    return make_tone(tmp_path / "tone.wav")


class FakeResponse:
    def __init__(self, status_code=200, data=None, headers=None, body=b""):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}
        self._body = body

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeYouTube:
    """Minimal stand-in for the YouTube Data API v3 used by PlaylistService."""

    def __init__(self, count=3, deleted=(), private=(), missing_details=(), playlist_found=True):
        self.count = count
        self.deleted = set(deleted)
        self.private = set(private)
        self.missing_details = set(missing_details)
        self.playlist_found = playlist_found
        self.calls = []

    def get(self, url, params=None, timeout=None):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, dict(params or {})))
        return getattr(self, f"_{endpoint}")(params or {})

    def _playlists(self, params):
        if not self.playlist_found:
            return FakeResponse(200, {"items": []})
        return FakeResponse(200, {"items": [{
            "snippet": {"title": "Road Trip", "channelTitle": "Thanu",
                        "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/x/mqdefault.jpg"}}},
            "contentDetails": {"itemCount": self.count},
        }]})

    def _playlistItems(self, params):
        start = int(params.get("pageToken") or 0)
        end = min(start + int(params["maxResults"]), self.count)
        items = []
        for i in range(start, end):
            n = i + 1
            title = f"Song {n}"
            if n in self.deleted:
                title = "Deleted video"
            elif n in self.private:
                title = "Private video"
            items.append({
                "snippet": {"title": title, "position": i, "videoOwnerChannelTitle": "Artist",
                            "thumbnails": {"default": {"url": f"https://i.ytimg.com/vi/v{n}/default.jpg"}},
                            "resourceId": {"videoId": f"vid{n:08d}"}},
                "contentDetails": {"videoId": f"vid{n:08d}"},
                "status": {"privacyStatus": "private" if n in self.private else "public"},
            })
        data = {"items": items}
        if end < self.count:
            data["nextPageToken"] = str(end)
        return FakeResponse(200, data)

    def _videos(self, params):
        ids = params["id"].split(",")
        assert len(ids) <= 50
        items = []
        for vid in ids:
            n = int(vid[3:])
            if n in self.deleted or n in self.private or n in self.missing_details:
                continue
            items.append({"id": vid, "contentDetails": {"duration": f"PT{n}M5S"},
                          "status": {"privacyStatus": "public", "uploadStatus": "processed"}})
        return FakeResponse(200, {"items": items})


@pytest.fixture
def app(tmp_path):
    from app import create_app

    application = create_app({
        "DATA_DIR": str(tmp_path / "data"),
        "CLEANUP_INTERVAL_SECONDS": 0,
        "YOUTUBE_API_KEY": "test-key-not-real",
        "MAX_UPLOAD_MB": 5,
        "ALLOWED_SOURCE_HOSTS": "",
    })
    application.config["TESTING"] = True
    yield application
    application.extensions["pas"]["jobs"].shutdown()


@pytest.fixture
def client(app):
    return app.test_client()
