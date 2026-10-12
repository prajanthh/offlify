import pytest
import requests

from conftest import FakeResponse, FakeYouTube
from services.playlist_service import PlaylistError, PlaylistService, parse_iso8601_duration
from utils.validators import ValidationError

URL = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"


def service(fake, **kwargs):
    return PlaylistService("secret-key-123", session=fake, **kwargs)


@pytest.mark.parametrize("value, seconds", [
    ("PT4M13S", 253), ("PT1H2M3S", 3723), ("PT45S", 45), ("PT2H", 7200),
    ("P1DT1S", 86401), ("PT1.6S", 2), ("P0D", None), ("PT", None), ("", None), (None, None), ("bogus", None),
])
def test_parse_iso8601_duration(value, seconds):
    assert parse_iso8601_duration(value) == seconds


def test_fetches_all_pages_in_order():
    fake = FakeYouTube(count=123)
    result = service(fake).fetch_playlist(URL)

    item_calls = [c for c in fake.calls if c[0] == "playlistItems"]
    video_calls = [c for c in fake.calls if c[0] == "videos"]
    assert len(item_calls) == 3  # 50 + 50 + 23
    assert len(video_calls) == 3  # batched 50 ids per call
    assert result["total_items"] == 123
    assert [t["position"] for t in result["tracks"]] == list(range(1, 124))
    assert result["tracks"][0]["title"] == "Song 1"
    assert result["tracks"][0]["duration_text"] == "1:05"
    assert result["title"] == "Road Trip"
    assert result["truncated"] is False


def test_respects_max_items_and_reports_truncation():
    fake = FakeYouTube(count=130)
    result = service(fake, max_items=60).fetch_playlist(URL)
    assert result["total_items"] == 60
    assert result["truncated"] is True
    assert len([c for c in fake.calls if c[0] == "playlistItems"]) == 2


def test_marks_deleted_private_and_missing_videos():
    fake = FakeYouTube(count=5, deleted={2}, private={3}, missing_details={4})
    tracks = service(fake).fetch_playlist(URL)["tracks"]
    by_pos = {t["position"]: t for t in tracks}
    assert by_pos[1]["available"] is True
    assert by_pos[2]["unavailable_reason"] == "Deleted video"
    assert by_pos[3]["unavailable_reason"] == "Private video"
    assert by_pos[4]["unavailable_reason"] == "Unavailable"
    assert by_pos[2]["thumbnail"] is None
    assert by_pos[5]["available"] is True


def test_playlist_not_found():
    with pytest.raises(PlaylistError) as exc:
        service(FakeYouTube(playlist_found=False)).fetch_playlist(URL)
    assert exc.value.status_code == 404


def test_invalid_url_is_rejected_before_any_request():
    fake = FakeYouTube()
    with pytest.raises(ValidationError):
        service(fake).fetch_playlist("https://example.com/?list=PLabcdefghijklmnop")
    assert fake.calls == []


def test_missing_api_key():
    with pytest.raises(PlaylistError) as exc:
        PlaylistService("", session=FakeYouTube()).fetch_playlist(URL)
    assert exc.value.status_code == 503


class ErrorSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc

    def get(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        return self.response


def api_error(status, reason):
    return FakeResponse(status, {"error": {"code": status, "errors": [{"reason": reason}]}})


@pytest.mark.parametrize("response, status, fragment", [
    (api_error(403, "quotaExceeded"), 429, "quota"),
    (api_error(400, "keyInvalid"), 503, "invalid"),
    (FakeResponse(400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}}), 503, "invalid"),
    (api_error(403, "accessNotConfigured"), 503, "not enabled"),
    (api_error(404, "playlistNotFound"), 404, "not found"),
    (api_error(403, "playlistItemsNotAccessible"), 403, "private"),
    (FakeResponse(500, ValueError("not json")), 502, "HTTP 500"),
    (FakeResponse(200, ValueError("not json")), 502, "unreadable"),
])
def test_api_errors_are_mapped_without_leaking_key(response, status, fragment):
    with pytest.raises(PlaylistError) as exc:
        service(ErrorSession(response)).fetch_playlist(URL)
    assert exc.value.status_code == status
    assert fragment in exc.value.message
    assert "secret-key-123" not in exc.value.message


@pytest.mark.parametrize("error, status", [
    (requests.Timeout(), 504),
    (requests.ConnectionError("boom key=secret-key-123"), 502),
])
def test_network_failures(error, status):
    with pytest.raises(PlaylistError) as exc:
        service(ErrorSession(exc=error)).fetch_playlist(URL)
    assert exc.value.status_code == status
    assert "secret-key-123" not in exc.value.message


def test_rejects_non_google_thumbnail_hosts():
    fake = FakeYouTube(count=1)
    original = fake._playlistItems

    def tampered(params):
        resp = original(params)
        resp._data["items"][0]["snippet"]["thumbnails"] = {"default": {"url": "https://evil.example/x.jpg"}}
        return resp

    fake._playlistItems = tampered
    assert service(fake).fetch_playlist(URL)["tracks"][0]["thumbnail"] is None
