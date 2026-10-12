"""Playlist metadata retrieval through the official YouTube Data API v3.

This module only reads *metadata* (titles, thumbnails, durations, order). It
never downloads media. Access to metadata does not imply permission to obtain
the audio; audio comes from user-supplied, authorised sources (see
``audio_service``).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from utils.file_utils import format_duration
from utils.validators import extract_playlist_id, extract_video_id

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
PAGE_SIZE = 50  # maximum allowed by the API for playlistItems and videos

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_PLACEHOLDER_TITLES = {"Deleted video": "deleted", "Private video": "private"}
_THUMBNAIL_HOSTS = ("ytimg.com", "ggpht.com", "googleusercontent.com")
_REASON_TEXT = {
    "deleted": "Deleted video",
    "private": "Private video",
    "unavailable": "Unavailable",
    "invalid": "Invalid entry",
}


class PlaylistError(Exception):
    """A user-facing error while fetching playlist metadata."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def parse_iso8601_duration(value: str | None) -> int | None:
    """Convert an ISO-8601 duration such as ``PT1H2M3S`` to whole seconds."""
    if not value or value in ("P", "PT"):
        return None
    match = _ISO_DURATION_RE.match(value)
    if not match:
        return None
    parts = {k: float(v) if v else 0.0 for k, v in match.groupdict().items()}
    total = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    # Live streams and premieres report P0D; treat that as "unknown".
    return int(round(total)) or None


def _pick_thumbnail(thumbnails: dict | None) -> str | None:
    """Pick a reasonably sized thumbnail and only accept known Google image hosts."""
    if not isinstance(thumbnails, dict):
        return None
    for key in ("medium", "high", "standard", "default", "maxres"):
        url = (thumbnails.get(key) or {}).get("url")
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in _THUMBNAIL_HOSTS):
            return url
    return None


class PlaylistService:
    def __init__(
        self,
        api_key: str | None,
        *,
        session: requests.Session | None = None,
        max_items: int = 500,
        timeout: float = 10.0,
    ):
        self._api_key = (api_key or "").strip()
        self._session = session or requests.Session()
        self.max_items = max_items
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------------ public
    def fetch_playlist(self, url_or_id: str) -> dict:
        """Validate the URL, then return normalised playlist metadata."""
        playlist_id = extract_playlist_id(url_or_id)
        self._require_key()

        meta = self._fetch_playlist_meta(playlist_id)
        raw_items, truncated = self._fetch_all_items(playlist_id)
        video_ids = [vid for vid in (self._video_id(i) for i in raw_items) if vid]
        details = self._fetch_video_details(video_ids)

        tracks = [
            self._normalise_item(item, index, details)
            for index, item in enumerate(raw_items, start=1)
        ]
        available = sum(1 for t in tracks if t["available"])

        snippet = meta.get("snippet") or {}
        return {
            "kind": "playlist",
            "id": playlist_id,
            "title": snippet.get("title") or "Untitled playlist",
            "channel": snippet.get("channelTitle") or "",
            "description": (snippet.get("description") or "")[:500],
            "thumbnail": _pick_thumbnail(snippet.get("thumbnails")),
            "reported_item_count": (meta.get("contentDetails") or {}).get("itemCount"),
            "total_items": len(tracks),
            "available_count": available,
            "unavailable_count": len(tracks) - available,
            "truncated": truncated,
            "max_items": self.max_items,
            "tracks": tracks,
        }

    def fetch_video(self, url_or_id: str) -> dict:
        """Validate a single-video link and return it shaped like a one-track playlist."""
        video_id = extract_video_id(url_or_id)
        self._require_key()

        data = self._get(
            "videos",
            {"part": "snippet,contentDetails,status", "id": video_id, "maxResults": 1},
        )
        items = data.get("items") or []
        if not items:
            raise PlaylistError(
                "Video not found. It may be private, deleted, or the link may be wrong.",
                404,
            )
        video = items[0]
        snippet = video.get("snippet") or {}
        status = video.get("status") or {}

        reason = None
        if status.get("privacyStatus") == "private":
            reason = "private"
        elif status.get("uploadStatus") in ("deleted", "failed", "rejected"):
            reason = "unavailable"

        title = snippet.get("title") or "Untitled video"
        channel = snippet.get("channelTitle") or ""
        thumbnail = _pick_thumbnail(snippet.get("thumbnails"))
        duration = parse_iso8601_duration((video.get("contentDetails") or {}).get("duration"))
        track = {
            "id": "t1",
            "position": 1,
            "video_id": video_id,
            "title": title,
            "channel": channel,
            "thumbnail": thumbnail if reason is None else None,
            "duration_seconds": duration,
            "duration_text": format_duration(duration),
            "available": reason is None,
            "unavailable_reason": _REASON_TEXT.get(reason) if reason else None,
        }
        available = 1 if reason is None else 0
        return {
            "kind": "video",
            "id": video_id,
            "title": title,
            "channel": channel,
            "description": (snippet.get("description") or "")[:500],
            "thumbnail": thumbnail,
            "reported_item_count": 1,
            "total_items": 1,
            "available_count": available,
            "unavailable_count": 1 - available,
            "truncated": False,
            "max_items": self.max_items,
            "tracks": [track],
        }

    # ----------------------------------------------------------------- helpers
    def _require_key(self) -> None:
        if not self.configured:
            raise PlaylistError(
                "The server has no YouTube Data API key configured. "
                "Set YOUTUBE_API_KEY in the .env file (see README).",
                503,
            )

    def _fetch_playlist_meta(self, playlist_id: str) -> dict:
        data = self._get(
            "playlists",
            {"part": "snippet,contentDetails", "id": playlist_id, "maxResults": 1},
        )
        items = data.get("items") or []
        if not items:
            raise PlaylistError(
                "Playlist not found. It may be private, deleted, or the URL may be wrong.",
                404,
            )
        return items[0]

    def _fetch_all_items(self, playlist_id: str) -> tuple[list[dict], bool]:
        items: list[dict] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        max_pages = self.max_items // PAGE_SIZE + 2

        for _ in range(max_pages):
            params = {
                "part": "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "maxResults": PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get("playlistItems", params)
            items.extend(data.get("items") or [])

            page_token = data.get("nextPageToken")
            if len(items) >= self.max_items:
                truncated = len(items) > self.max_items or bool(page_token)
                return items[: self.max_items], truncated
            if not page_token or page_token in seen_tokens:
                break
            seen_tokens.add(page_token)
        return items, False

    def _fetch_video_details(self, video_ids: list[str]) -> dict[str, dict]:
        unique = list(dict.fromkeys(video_ids))
        details: dict[str, dict] = {}
        for start in range(0, len(unique), PAGE_SIZE):
            batch = unique[start : start + PAGE_SIZE]
            data = self._get(
                "videos",
                {"part": "contentDetails,status", "id": ",".join(batch), "maxResults": PAGE_SIZE},
            )
            for video in data.get("items") or []:
                if isinstance(video, dict) and video.get("id"):
                    details[video["id"]] = video
        return details

    @staticmethod
    def _video_id(item: dict) -> str | None:
        vid = (item.get("contentDetails") or {}).get("videoId")
        if not vid:
            vid = ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")
        return vid if isinstance(vid, str) and vid else None

    def _normalise_item(self, item: dict, index: int, details: dict[str, dict]) -> dict:
        snippet = item.get("snippet") or {}
        title = snippet.get("title") or ""
        video_id = self._video_id(item)
        detail = details.get(video_id or "")

        reason = None
        if title in _PLACEHOLDER_TITLES:
            reason = _PLACEHOLDER_TITLES[title]
        elif not video_id:
            reason = "invalid"
        elif (item.get("status") or {}).get("privacyStatus") == "private":
            reason = "private"
        elif detail is None:
            reason = "unavailable"
        else:
            status = detail.get("status") or {}
            if status.get("privacyStatus") == "private":
                reason = "private"
            elif status.get("uploadStatus") in ("deleted", "failed", "rejected"):
                reason = "unavailable"

        duration = None
        if detail:
            duration = parse_iso8601_duration((detail.get("contentDetails") or {}).get("duration"))

        return {
            "id": f"t{index}",
            "position": index,
            "video_id": video_id,
            "title": title or f"Track {index}",
            "channel": snippet.get("videoOwnerChannelTitle") or "",
            "thumbnail": _pick_thumbnail(snippet.get("thumbnails")) if reason is None else None,
            "duration_seconds": duration,
            "duration_text": format_duration(duration),
            "available": reason is None,
            "unavailable_reason": _REASON_TEXT.get(reason) if reason else None,
        }

    def _get(self, endpoint: str, params: dict) -> dict:
        """GET an API endpoint, mapping failures to friendly PlaylistErrors.

        The API key is added here and never included in any error message or log.
        """
        try:
            response = self._session.get(
                f"{API_BASE}/{endpoint}",
                params={**params, "key": self._api_key},
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise PlaylistError("The YouTube API took too long to respond. Please try again.", 504) from exc
        except requests.RequestException as exc:
            log.warning("YouTube API request to %s failed: %s", endpoint, type(exc).__name__)
            raise PlaylistError(
                "Could not reach the YouTube API. Check the server's internet connection.", 502
            ) from exc

        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                raise PlaylistError("The YouTube API returned an unreadable response.", 502) from exc
            if not isinstance(data, dict):
                raise PlaylistError("The YouTube API returned an unexpected response.", 502)
            return data

        reason = self._error_reason(response)
        log.warning("YouTube API %s returned HTTP %s (%s)", endpoint, response.status_code, reason or "no reason")
        raise self._map_error(response.status_code, reason)

    @staticmethod
    def _error_reason(response) -> str:
        try:
            error = (response.json() or {}).get("error") or {}
        except ValueError:
            return ""
        reasons = [e.get("reason", "") for e in error.get("errors") or [] if isinstance(e, dict)]
        reasons += [d.get("reason", "") for d in error.get("details") or [] if isinstance(d, dict)]
        reasons.append(error.get("status", ""))
        return ",".join(r for r in reasons if r)

    @staticmethod
    def _map_error(status: int, reason: str) -> PlaylistError:
        r = reason.lower()
        if "keyinvalid" in r or "api_key_invalid" in r or "key_invalid" in r:
            return PlaylistError("The server's YouTube API key is invalid. Check YOUTUBE_API_KEY.", 503)
        if "quotaexceeded" in r or "dailylimitexceeded" in r or "ratelimitexceeded" in r:
            return PlaylistError("The YouTube API quota has been used up. Please try again later.", 429)
        if "accessnotconfigured" in r or "service_disabled" in r:
            return PlaylistError(
                "YouTube Data API v3 is not enabled for this API key's Google Cloud project.", 503
            )
        if "playlistnotfound" in r or status == 404:
            return PlaylistError(
                "Playlist not found. It may be private, deleted, or the URL may be wrong.", 404
            )
        if "notaccessible" in r or "forbidden" in r or status == 403:
            return PlaylistError("This playlist is private or not accessible.", 403)
        return PlaylistError(f"The YouTube API returned an error (HTTP {status}).", 502)
