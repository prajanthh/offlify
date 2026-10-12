"""Input validation shared by the API layer and the services.

Every function here raises :class:`ValidationError` with a message that is safe
to show to end users (no paths, secrets or stack traces).
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


class ValidationError(ValueError):
    """Raised when user input fails validation. The message is user-facing."""

    status_code = 400


YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 300

_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_BARE_PLAYLIST_ID_RE = re.compile(r"^(?:PL|OL|UU|FL)[A-Za-z0-9_-]{10,62}$")
_HEX_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIDEO_PATH_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/")


def _parse_youtube_url(value: str, wrong_host_message: str):
    """Parse ``value`` and check it is a plain http(s) URL on a YouTube host."""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("That doesn't look like a valid URL.") from exc

    if parsed.scheme not in ("http", "https"):
        raise ValidationError("Only http and https YouTube URLs are accepted.")
    if parsed.username or parsed.password:
        raise ValidationError("URLs containing credentials are not accepted.")
    if port not in (None, 80, 443):
        raise ValidationError("That doesn't look like a YouTube URL.")
    if (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise ValidationError(wrong_host_message)
    return parsed


def extract_playlist_id(raw: object) -> str:
    """Return the playlist ID contained in a YouTube playlist URL.

    Accepts full URLs (``https://www.youtube.com/playlist?list=...``), watch
    URLs that carry a ``list`` parameter, ``youtu.be`` links with ``list``, and
    bare playlist IDs such as ``PLxxxxxxxxxxxx``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("Please enter a YouTube playlist URL.")

    value = raw.strip()
    if len(value) > MAX_URL_LENGTH:
        raise ValidationError("The URL is too long.")
    if any(ch.isspace() for ch in value):
        raise ValidationError("The URL must not contain spaces.")

    if "://" not in value:
        if _BARE_PLAYLIST_ID_RE.match(value):
            return value
        value = "https://" + value

    parsed = _parse_youtube_url(value, "Please enter a youtube.com playlist URL.")

    list_values = parse_qs(parsed.query).get("list")
    if not list_values:
        raise ValidationError(
            "This URL doesn't contain a playlist (no 'list=' parameter). "
            "Open the playlist on YouTube and copy its URL."
        )

    playlist_id = list_values[0].strip()
    if playlist_id.upper() in ("WL", "LL", "LM"):
        raise ValidationError(
            "Watch Later and Liked lists are private to your account and "
            "can't be read with an API key. Use a public or unlisted playlist."
        )
    if playlist_id.startswith("RD"):
        raise ValidationError(
            "YouTube Mix playlists (IDs starting with 'RD') are generated per "
            "viewer and aren't available through the YouTube Data API. "
            "Please use a regular playlist."
        )
    if not _PLAYLIST_ID_RE.match(playlist_id):
        raise ValidationError("The playlist ID in this URL is not valid.")
    return playlist_id


def extract_video_id(raw: object) -> str:
    """Return the video ID contained in a single-video YouTube link.

    Accepts ``watch?v=`` URLs, ``youtu.be`` short links, ``/shorts/``,
    ``/live/`` and ``/embed/`` URLs, and bare 11-character video IDs.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("Please paste a YouTube video or playlist link.")

    value = raw.strip()
    if len(value) > MAX_URL_LENGTH:
        raise ValidationError("The URL is too long.")
    if any(ch.isspace() for ch in value):
        raise ValidationError("The URL must not contain spaces.")

    if "://" not in value:
        if _VIDEO_ID_RE.match(value):
            return value
        value = "https://" + value

    parsed = _parse_youtube_url(value, "Please enter a YouTube video or playlist link.")
    path = parsed.path
    if (parsed.hostname or "").lower() == "youtu.be":
        candidate = path.strip("/").split("/")[0]
    elif path.rstrip("/") == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        candidate = next(
            (path[len(prefix):].split("/")[0] for prefix in _VIDEO_PATH_PREFIXES if path.startswith(prefix)),
            "",
        )
    if not _VIDEO_ID_RE.match(candidate):
        raise ValidationError(
            "This link doesn't contain a YouTube video or playlist. "
            "Use the Share button on YouTube and copy that link."
        )
    return candidate


def link_kind(raw: object) -> str:
    """Decide whether a pasted link should load as a ``"playlist"`` or a ``"video"``.

    A readable playlist wins. Otherwise a link that carries a video (a plain
    video link, or a video inside a Mix / Watch Later list) loads that video.
    Anything else is treated as a playlist so its error message is shown.
    """
    try:
        extract_playlist_id(raw)
        return "playlist"
    except ValidationError:
        pass
    try:
        extract_video_id(raw)
        return "video"
    except ValidationError:
        return "playlist"


def validate_output_format(value: object, available: list[str]) -> str:
    """Normalise and check a requested output format against those supported."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Please choose an output format.")
    fmt = value.strip().lower()
    if fmt not in available:
        supported = ", ".join(f.upper() for f in available) or "none"
        raise ValidationError(
            f"Unsupported output format '{value}'. Supported formats: {supported}."
        )
    return fmt


def validate_hex_id(value: object, label: str = "ID") -> str:
    """Validate the opaque 32-hex-character identifiers generated by the server."""
    if not isinstance(value, str) or not _HEX_ID_RE.match(value):
        raise ValidationError(f"Invalid {label}.")
    return value


def validate_track_items(items: object, max_items: int) -> list[dict]:
    """Validate the list of tracks sent to ``POST /api/process``."""
    if not isinstance(items, list) or not items:
        raise ValidationError("Select at least one track that has an audio source attached.")
    if len(items) > max_items:
        raise ValidationError(f"Too many tracks in one job (limit is {max_items}).")

    normalised: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"Track #{index} is malformed.")

        track_id = item.get("track_id")
        if not isinstance(track_id, str) or not _TRACK_ID_RE.match(track_id):
            raise ValidationError(f"Track #{index} has an invalid track_id.")
        if track_id in seen:
            raise ValidationError(f"Track '{track_id}' was submitted more than once.")
        seen.add(track_id)

        position = item.get("position", index)
        if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= 100_000:
            raise ValidationError(f"Track #{index} has an invalid position.")

        title = item.get("title") or f"Track {position}"
        if not isinstance(title, str):
            raise ValidationError(f"Track #{index} has an invalid title.")
        title = " ".join(title.split())[:MAX_TITLE_LENGTH] or f"Track {position}"

        source_id = validate_hex_id(item.get("source_id"), f"audio source for track #{index}")

        normalised.append(
            {"track_id": track_id, "position": position, "title": title, "source_id": source_id}
        )
    return normalised


def validate_youtube_track_items(items: object, max_items: int) -> list[dict]:
    """Validate the list of tracks sent to ``POST /api/process-youtube``.

    Each item must have a ``video_id`` (11-character YouTube video ID) instead
    of a ``source_id``.  The job worker will download audio from YouTube.
    """
    if not isinstance(items, list) or not items:
        raise ValidationError("Select at least one track to download.")
    if len(items) > max_items:
        raise ValidationError(f"Too many tracks in one job (limit is {max_items}).")

    normalised: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"Track #{index} is malformed.")

        track_id = item.get("track_id")
        if not isinstance(track_id, str) or not _TRACK_ID_RE.match(track_id):
            raise ValidationError(f"Track #{index} has an invalid track_id.")
        if track_id in seen:
            raise ValidationError(f"Track '{track_id}' was submitted more than once.")
        seen.add(track_id)

        video_id = item.get("video_id")
        if not isinstance(video_id, str) or not _VIDEO_ID_RE.match(video_id):
            raise ValidationError(f"Track #{index} has an invalid video_id.")

        position = item.get("position", index)
        if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= 100_000:
            raise ValidationError(f"Track #{index} has an invalid position.")

        title = item.get("title") or f"Track {position}"
        if not isinstance(title, str):
            raise ValidationError(f"Track #{index} has an invalid title.")
        title = " ".join(title.split())[:MAX_TITLE_LENGTH] or f"Track {position}"

        normalised.append(
            {"track_id": track_id, "position": position, "title": title, "video_id": video_id}
        )
    return normalised


def parse_allowed_hosts(raw: str | None) -> frozenset[str]:
    """Parse the comma-separated ALLOWED_SOURCE_HOSTS setting."""
    hosts = set()
    for part in (raw or "").split(","):
        host = part.strip().lower().rstrip(".")
        if not host:
            continue
        if not _HOSTNAME_RE.match(host) or host.startswith(".") or ".." in host:
            raise ValueError(f"Invalid host in ALLOWED_SOURCE_HOSTS: {part!r}")
        hosts.add(host)
    return frozenset(hosts)


def validate_source_url(value: object, allowed_hosts: frozenset[str]) -> str:
    """Validate a remote audio source URL against the server's allowlist.

    Only ``https`` on the default port, without credentials, to an exact host
    in the allowlist. This is the first line of SSRF defence; the audio
    service additionally refuses hosts that resolve to non-public addresses.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Please enter a source URL.")
    url = value.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError("The URL is too long.")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("That doesn't look like a valid URL.") from exc
    if parsed.scheme != "https":
        raise ValidationError("Only https:// source URLs are accepted.")
    if parsed.username or parsed.password:
        raise ValidationError("URLs containing credentials are not accepted.")
    if port not in (None, 443):
        raise ValidationError("Source URLs must use the default https port.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in allowed_hosts:
        raise ValidationError(
            "That host is not on this server's list of permitted audio sources."
        )
    return url
