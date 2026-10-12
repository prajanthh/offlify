"""ZIP archive creation for completed audio files."""
from __future__ import annotations

import zipfile
from pathlib import Path

from utils.file_utils import sanitize_filename

# Already-compressed formats are stored as-is; deflating them wastes CPU.
_STORED_EXTENSIONS = {"mp3", "ogg", "m4a", "flac", "opus", "aac"}


def build_zip(entries: list[tuple[Path, str]], destination: Path) -> Path:
    """Write ``entries`` (path, name-inside-archive) to a ZIP at ``destination``.

    Archive names are sanitised (no directories, no traversal) and
    de-duplicated, so two tracks with the same title never overwrite each other
    inside the archive.
    """
    if not entries:
        raise ValueError("There are no files to archive.")

    used: set[str] = set()
    destination = Path(destination)
    try:
        with zipfile.ZipFile(destination, "w") as archive:
            for path, arcname in entries:
                path = Path(path)
                if not path.is_file():
                    continue
                stem = sanitize_filename(Path(arcname).stem, fallback="track")
                ext = Path(arcname).suffix.lower().lstrip(".") or path.suffix.lower().lstrip(".")
                name, counter = f"{stem}.{ext}", 2
                while name.lower() in used:
                    name, counter = f"{stem} ({counter}).{ext}", counter + 1
                used.add(name.lower())
                compression = zipfile.ZIP_STORED if ext in _STORED_EXTENSIONS else zipfile.ZIP_DEFLATED
                archive.write(path, arcname=name, compress_type=compression)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if not used:
        destination.unlink(missing_ok=True)
        raise ValueError("There are no files to archive.")
    return destination
