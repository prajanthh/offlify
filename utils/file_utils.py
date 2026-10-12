"""Filesystem helpers: safe filenames, collision-free paths, containment checks."""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_FILENAME_BYTES = 200


def sanitize_filename(name: object, fallback: str = "track", max_length: int = 120) -> str:
    """Turn an untrusted string (e.g. a YouTube title) into a safe file name stem.

    Removes path separators, control/format characters and characters invalid on
    Windows, collapses whitespace, strips leading/trailing dots and spaces,
    avoids Windows reserved device names and limits the length in characters
    and UTF-8 bytes. Never returns an empty string, "." or "..".
    """
    text = name if isinstance(name, str) else ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = _INVALID_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"_{2,}", "_", text).strip(" ._")

    if not text:
        text = fallback
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"_{text}"

    text = text[:max_length]
    while len(text.encode("utf-8")) > MAX_FILENAME_BYTES:
        text = text[:-1]
    text = text.rstrip(" .")
    return text or fallback


def reserve_unique_path(directory: Path, stem: str, extension: str) -> Path:
    """Atomically create an empty file ``<stem>.<ext>`` that doesn't exist yet.

    If the name is taken, ``<stem> (2).<ext>``, ``<stem> (3).<ext>``... are
    tried. Using O_EXCL means two workers can never claim the same name and an
    existing file is never overwritten.
    """
    directory = Path(directory)
    stem = sanitize_filename(stem)
    ext = extension.lstrip(".")
    for counter in range(1, 10_000):
        name = f"{stem}.{ext}" if counter == 1 else f"{stem} ({counter}).{ext}"
        candidate = directory / name
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError("Could not find a free file name.")


def safe_join(base: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and refuse anything that escapes ``base``."""
    base_resolved = Path(base).resolve()
    target = base_resolved.joinpath(*parts).resolve()
    if not target.is_relative_to(base_resolved):
        raise ValueError("Path escapes the permitted directory.")
    return target


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_path(path: Path | None) -> None:
    """Delete a file or directory tree, ignoring anything that's already gone."""
    if path is None:
        return
    path = Path(path)
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def format_bytes(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
