import threading

import pytest

from utils.file_utils import format_bytes, format_duration, reserve_unique_path, safe_join, sanitize_filename


@pytest.mark.parametrize("raw, expected", [
    ("Hello World", "Hello World"),
    ("AC/DC: Back in Black", "AC_DC_ Back in Black"),
    ('a<b>c:"d"|e?f*g', "a_b_c_d_e_f_g"),
    ("../../etc/passwd", "etc_passwd"),
    ("..", "track"),
    ("", "track"),
    (None, "track"),
    ("   \t\n  ", "track"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("bell\x07 and ​tab", "bell and tab"),
    ("trailing dots...", "trailing dots"),
    ("Ｆｕｌｌ ｗｉｄｔｈ", "Full width"),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_length_limits():
    assert len(sanitize_filename("x" * 500)) == 120
    long_unicode = sanitize_filename("音" * 150)
    assert len(long_unicode.encode("utf-8")) <= 200


def test_reserve_unique_path_never_overwrites(tmp_path):
    (tmp_path / "Song.mp3").write_bytes(b"existing")
    second = reserve_unique_path(tmp_path, "Song", "mp3")
    third = reserve_unique_path(tmp_path, "Song", ".mp3")
    assert second.name == "Song (2).mp3"
    assert third.name == "Song (3).mp3"
    assert (tmp_path / "Song.mp3").read_bytes() == b"existing"


def test_reserve_unique_path_is_race_safe(tmp_path):
    results = []

    def claim():
        results.append(reserve_unique_path(tmp_path, "Same", "wav").name)

    threads = [threading.Thread(target=claim) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 20


def test_safe_join_blocks_traversal(tmp_path):
    assert safe_join(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    for bad in ("../outside.txt", "/etc/passwd", "a/../../x"):
        with pytest.raises(ValueError):
            safe_join(tmp_path, bad)


def test_formatters():
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert format_duration(65) == "1:05"
    assert format_duration(3725) == "1:02:05"
    assert format_duration(None) == ""
