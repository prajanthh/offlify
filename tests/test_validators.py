import pytest

from utils.validators import (
    ValidationError,
    extract_playlist_id,
    extract_video_id,
    link_kind,
    parse_allowed_hosts,
    validate_hex_id,
    validate_output_format,
    validate_source_url,
    validate_track_items,
)

PID = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/playlist?list={PID}",
    f"http://youtube.com/playlist?list={PID}",
    f"https://m.youtube.com/playlist?list={PID}&si=abc",
    f"https://music.youtube.com/playlist?list={PID}",
    f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PID}&index=3",
    f"https://youtu.be/dQw4w9WgXcQ?list={PID}",
    f"www.youtube.com/playlist?list={PID}",
    f"  https://www.youtube.com/playlist?list={PID}  ",
    PID,
])
def test_extract_playlist_id_accepts_valid_forms(url):
    assert extract_playlist_id(url) == PID


@pytest.mark.parametrize("url, fragment", [
    ("", "Please enter"),
    (None, "Please enter"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "list="),
    (f"https://evil.example.com/playlist?list={PID}", "youtube.com"),
    (f"https://youtube.com.evil.com/playlist?list={PID}", "youtube.com"),
    (f"ftp://youtube.com/playlist?list={PID}", "http"),
    (f"javascript:alert(1)//youtube.com?list={PID}", "valid URL"),
    (f"https://user:pw@youtube.com/playlist?list={PID}", "credentials"),
    (f"https://youtube.com:8080/playlist?list={PID}", "YouTube URL"),
    ("https://www.youtube.com/playlist?list=WL", "Watch Later"),
    ("https://www.youtube.com/playlist?list=RDdQw4w9WgXcQ", "Mix"),
    ("https://www.youtube.com/playlist?list=PL<script>", "not valid"),
    ("https://www.youtube.com/playlist?list=" + "A" * 80, "not valid"),
    ("https://youtube.com/" + "a" * 3000, "too long"),
    (f"https://www.youtube.com/playlist?list={PID} x", "spaces"),
])
def test_extract_playlist_id_rejects_bad_input(url, fragment):
    with pytest.raises(ValidationError) as exc:
        extract_playlist_id(url)
    assert fragment in str(exc.value)


def test_validate_output_format():
    assert validate_output_format(" MP3 ", ["mp3", "wav"]) == "mp3"
    with pytest.raises(ValidationError):
        validate_output_format("exe", ["mp3", "wav"])
    with pytest.raises(ValidationError):
        validate_output_format(None, ["mp3"])


def test_validate_hex_id():
    assert validate_hex_id("a" * 32) == "a" * 32
    for bad in ("../../etc/passwd", "A" * 32, "a" * 31, 123, None):
        with pytest.raises(ValidationError):
            validate_hex_id(bad)


def test_validate_track_items_normalises_and_rejects():
    items = [{"track_id": "t1", "position": 1, "title": "  Hello\n world ", "source_id": "b" * 32}]
    assert validate_track_items(items, 10) == [
        {"track_id": "t1", "position": 1, "title": "Hello world", "source_id": "b" * 32}
    ]
    with pytest.raises(ValidationError):
        validate_track_items([], 10)
    with pytest.raises(ValidationError):
        validate_track_items(items * 2, 10)  # duplicate track id
    with pytest.raises(ValidationError):
        validate_track_items(items, 0)  # over the limit
    with pytest.raises(ValidationError):
        validate_track_items([{**items[0], "source_id": "../x"}], 10)
    with pytest.raises(ValidationError):
        validate_track_items([{**items[0], "track_id": "../../x"}], 10)
    with pytest.raises(ValidationError):
        validate_track_items([{**items[0], "position": True}], 10)


def test_source_url_allowlist():
    hosts = parse_allowed_hosts("media.example.org, Cdn.Example.org")
    assert hosts == {"media.example.org", "cdn.example.org"}
    assert validate_source_url("https://media.example.org/a.mp3", hosts)
    for bad in (
        "http://media.example.org/a.mp3",
        "https://other.example.org/a.mp3",
        "https://media.example.org:8443/a.mp3",
        "https://u:p@media.example.org/a.mp3",
        "file:///etc/passwd",
        "https://169.254.169.254/latest/meta-data",
    ):
        with pytest.raises(ValidationError):
            validate_source_url(bad, hosts)
    with pytest.raises(ValueError):
        parse_allowed_hosts("bad host!")


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc123",
    "https://youtube.com/shorts/dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMdQw4w9WgXcQ",
    "dQw4w9WgXcQ",
])
def test_extract_video_id_accepts_valid_forms(url):
    assert extract_video_id(url) == "dQw4w9WgXcQ"
    assert link_kind(url) == "video"


@pytest.mark.parametrize("url", ["", "https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/@channel"])
def test_extract_video_id_rejects_bad_input(url):
    with pytest.raises(ValidationError):
        extract_video_id(url)


def test_link_kind_prefers_a_readable_playlist():
    assert link_kind(f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PID}") == "playlist"
