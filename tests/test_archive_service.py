import zipfile

import pytest

from services.archive_service import build_zip


def test_build_zip_dedupes_and_sanitises_names(tmp_path):
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    w = tmp_path / "c.wav"
    a.write_bytes(b"A" * 100)
    b.write_bytes(b"B" * 100)
    w.write_bytes(b"\0" * 1000)

    dest = build_zip([(a, "Song.mp3"), (b, "song.mp3"), (w, "../../evil/../Track.wav")], tmp_path / "out.zip")

    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert names == ["Song.mp3", "song (2).mp3", "Track.wav"]
        assert all("/" not in n and "\\" not in n for n in names)
        assert zf.read(names[0]) == b"A" * 100
        info = {i.filename: i for i in zf.infolist()}
        assert info["Song.mp3"].compress_type == zipfile.ZIP_STORED
        assert info[names[2]].compress_type == zipfile.ZIP_DEFLATED
        assert zf.testzip() is None


def test_build_zip_skips_missing_files(tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x")
    dest = build_zip([(tmp_path / "gone.mp3", "gone.mp3"), (real, "real.mp3")], tmp_path / "o.zip")
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["real.mp3"]


def test_build_zip_requires_files(tmp_path):
    with pytest.raises(ValueError):
        build_zip([], tmp_path / "o.zip")
    with pytest.raises(ValueError):
        build_zip([(tmp_path / "missing.mp3", "m.mp3")], tmp_path / "o.zip")
    assert not (tmp_path / "o.zip").exists()
