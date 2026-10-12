import io
import threading
import wave

import pytest

from conftest import FakeResponse, make_tone, requires_ffmpeg
from services.audio_service import AudioConverter, AudioError, ConversionCancelled, SourceStore
from utils.validators import ValidationError


class Upload:
    """Mimics werkzeug's FileStorage just enough for SourceStore."""

    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)


@pytest.fixture
def converter():
    return AudioConverter(timeout=60)


@pytest.fixture
def store(tmp_path, converter):
    directory = tmp_path / "sources"
    directory.mkdir()
    return SourceStore(directory, converter, max_bytes=2 * 1024 * 1024)


@requires_ffmpeg
def test_detects_formats(converter):
    assert {"mp3", "wav"} <= set(converter.available_formats())


@requires_ffmpeg
@pytest.mark.parametrize("fmt, magic", [("mp3", (b"ID3", b"\xff\xfb", b"\xff\xf3")), ("wav", (b"RIFF",)),
                                         ("flac", (b"fLaC",)), ("ogg", (b"OggS",))])
def test_convert_formats_and_progress(tmp_path, tone_wav, converter, fmt, magic):
    dest = tmp_path / f"out.{fmt}"
    progress = []
    converter.convert(tone_wav, dest, fmt, duration=2.0, title="Tone", progress_cb=progress.append)
    assert dest.read_bytes().startswith(magic)
    assert not (tmp_path / f"out.{fmt}.part").exists()
    assert progress and progress[-1] == 1.0
    assert converter.probe(dest)["duration"] == pytest.approx(2.0, abs=0.2)


@requires_ffmpeg
def test_convert_wav_is_valid_pcm(tmp_path, tone_wav, converter):
    dest = converter.convert(tone_wav, tmp_path / "x.wav", "wav")
    with wave.open(str(dest)) as wav:
        assert wav.getsampwidth() == 2
        assert wav.getnframes() > 0


@requires_ffmpeg
def test_corrupt_input_fails_cleanly(tmp_path, converter):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio" * 100)
    dest = tmp_path / "out.mp3"
    with pytest.raises(AudioError):
        converter.convert(bad, dest, "mp3")
    assert not dest.exists()
    assert not (tmp_path / "out.mp3.part").exists()


@requires_ffmpeg
def test_cancellation_kills_ffmpeg(tmp_path, converter):
    long_tone = make_tone(tmp_path / "long.wav", seconds=120)
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    with pytest.raises(ConversionCancelled):
        converter.convert(long_tone, tmp_path / "out.flac", "flac", duration=120, cancel_event=cancel)
    assert not (tmp_path / "out.flac").exists()
    assert not (tmp_path / "out.flac.part").exists()


def test_unknown_format_is_rejected(tmp_path, converter):
    with pytest.raises(AudioError):
        converter.convert(tmp_path / "a.wav", tmp_path / "b.exe", "exe")


def test_missing_ffmpeg_reports_clearly(tmp_path):
    conv = AudioConverter(ffmpeg_path="", ffprobe_path="")
    conv.ffmpeg = conv.ffprobe = None
    assert conv.available_formats() == []
    with pytest.raises(AudioError) as exc:
        conv.probe(tmp_path / "x.wav")
    assert exc.value.status_code == 503


@requires_ffmpeg
def test_upload_registers_probed_source(store, tone_wav):
    record = store.add_upload(Upload("../../My Track.WAV", tone_wav.read_bytes()))
    assert record["filename"] == "My Track.wav"
    assert record["duration"] == pytest.approx(2.0, abs=0.1)
    assert record["path"].parent == store.directory
    assert record["path"].name == f"{record['id']}.wav"  # server-chosen name, not the client's
    assert store.get(record["id"]) is record
    assert store.remove(record["id"]) and not record["path"].exists()


@requires_ffmpeg
def test_upload_rejects_non_audio_content(store):
    with pytest.raises(AudioError) as exc:
        store.add_upload(Upload("song.mp3", b"<?php echo 'hi'; ?>" * 50))
    assert exc.value.status_code == 422
    assert list(store.directory.iterdir()) == []


@pytest.mark.parametrize("name", ["evil.exe", "noext", "song.mp3.sh", ""])
def test_upload_rejects_bad_extensions(store, name):
    with pytest.raises(AudioError) as exc:
        store.add_upload(Upload(name, b"x"))
    assert exc.value.status_code == 415


def test_upload_size_limit_and_empty(store):
    with pytest.raises(AudioError) as exc:
        store.add_upload(Upload("big.wav", b"\0" * (3 * 1024 * 1024)))
    assert exc.value.status_code == 413
    with pytest.raises(AudioError):
        store.add_upload(Upload("empty.wav", b""))
    assert list(store.directory.iterdir()) == []


def test_url_sources_disabled_by_default(store):
    with pytest.raises(AudioError) as exc:
        store.add_from_url("https://media.example.org/a.mp3")
    assert exc.value.status_code == 403


def test_url_sources_enforce_allowlist_and_public_ip(tmp_path, converter, monkeypatch):
    s = SourceStore(tmp_path, converter, max_bytes=1024, allowed_hosts=frozenset({"media.example.org"}))
    with pytest.raises(ValidationError):
        s.add_from_url("https://elsewhere.example.org/a.mp3")
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(AudioError) as exc:
        s.add_from_url("https://media.example.org/a.mp3")
    assert "non-public" in exc.value.message


def test_url_sources_refuse_redirects(tmp_path, converter, monkeypatch):
    class Session:
        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return FakeResponse(302, headers={"Location": "http://169.254.169.254/"})

    s = SourceStore(tmp_path, converter, max_bytes=1024, allowed_hosts=frozenset({"media.example.org"}),
                    session=Session())
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    with pytest.raises(AudioError) as exc:
        s.add_from_url("https://media.example.org/a.mp3")
    assert "redirect" in exc.value.message


@requires_ffmpeg
def test_url_source_downloads_from_allowed_host(tmp_path, converter, tone_wav, monkeypatch):
    body = tone_wav.read_bytes()

    class Session:
        def get(self, url, **kwargs):
            return FakeResponse(200, headers={"Content-Length": str(len(body))}, body=body)

    d = tmp_path / "src"
    d.mkdir()
    s = SourceStore(d, converter, max_bytes=10 * 1024 * 1024,
                    allowed_hosts=frozenset({"media.example.org"}), session=Session())
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    record = s.add_from_url("https://media.example.org/music/My%20Song.wav")
    assert record["filename"] == "My Song.wav"
    assert record["origin"] == "url"
