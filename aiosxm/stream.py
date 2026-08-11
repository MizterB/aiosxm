"""Support for streaming audio from SiriusXM."""

import base64
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from aiosxm.const import API_BASE, BITRATE_256, BITRATES, STREAM_EXPIRY_MARGIN
from aiosxm.models import Track

if TYPE_CHECKING:
    from aiosxm.client import SxmClient

# Linear channels use a fixed all-zero key id; on-demand content is keyed per stream.
LINEAR_KEY_ID = "00000000-0000-0000-0000-000000000000"

_KEY_URI_RE = re.compile(r'(#EXT-X-KEY:[^\n]*?URI=")([^"]+)(")')

# CDN paths embed their signed validity window as _<start>-<end>_.
_SIGNED_WINDOW_RE = re.compile(r"_(\d{10})-(\d{10})_")

_LOGGER = logging.getLogger(__name__)


def trim_to_live_window(playlist: str, segments: int) -> str:
    """Keep only the last `segments` media segments of a live playlist.

    `EXT-X-MEDIA-SEQUENCE` is advanced to match, so a player still computes the
    right AES-128 IV for each segment — the IV defaults to the sequence number,
    so getting this wrong produces noise instead of audio.
    """
    lines = playlist.splitlines()
    # A segment is a non-comment line; its preceding #EXT* tags belong with it.
    indices = [i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")]
    if len(indices) <= segments:
        return playlist

    keep_from = indices[-segments]
    # Walk back over the tags attached to that segment (#EXTINF, #EXT-X-KEY, ...).
    start = keep_from
    while start > 0 and lines[start - 1].startswith("#EXT") and not lines[start - 1].startswith(
        ("#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE")
    ):
        start -= 1

    header = []
    active_key = None
    dropped = len(indices) - segments
    for line in lines[:start]:
        if not line.startswith("#"):
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            first = int(line.split(":", 1)[1])
            header.append(f"#EXT-X-MEDIA-SEQUENCE:{first + dropped}")
        elif line.startswith("#EXT-X-KEY"):
            # Keys apply to every following segment, so the one in force at the
            # cut point has to be carried over or nothing decrypts.
            active_key = line
        elif line.startswith(("#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION",
                              "#EXT-X-ALLOW-CACHE", "#EXT-X-PLAYLIST-TYPE")):
            header.append(line)

    if active_key and not any(ln.startswith("#EXT-X-KEY") for ln in lines[start:]):
        header.append(active_key)

    return "\n".join([*header, *lines[start:]]) + "\n"


class SxmStream:
    """A stream from SiriusXM."""

    def __init__(self, client: "SxmClient", entity_type: str, entity_id: str) -> None:
        """Initialize the object."""
        self._client: SxmClient = client
        self.entity_type: str = entity_type
        self.entity_id: str = entity_id
        self.initialized: bool = False
        self._tune_source: dict | None = None
        self._streams_by_bitrate: dict[str, str | None] = {}
        self._variants: list[str] = []

    async def initialize(self) -> None:
        """Initialize the stream."""
        await self._tune()

        if self.is_track_queue or self.is_progressive:
            # Artist stations hand back a queue of discrete tracks, and on-demand
            # episodes a single file. Neither has a master playlist to walk.
            self.initialized = True
            return

        master_playlist = await self._client.request(method="GET", url=self.master_playlist_url)
        self._streams_by_bitrate = self._parse_master_playlist(master_playlist)
        self.initialized = True

    async def _tune(self, sequence_token: str | None = None) -> dict:
        """Ask the API for playback sources, optionally continuing a queue."""
        payload: dict[str, Any] = {
            "id": self.entity_id,
            "type": self.entity_type,
            "hlsVersion": "V3",
            "manifestVariant": "FULL",
            "mtcVersion": "V2",
        }
        if sequence_token:
            payload["sequenceToken"] = sequence_token
        self._tune_source = await self._client.request(
            method="POST",
            url=f"{API_BASE}/playback/play/v1/tuneSource",
            json=payload,
        )
        return self._tune_source

    async def next_tracks(self) -> list[Track]:
        """Fetch the next batch of tracks, continuing the queue.

        Artist stations hand back three tracks at a time along with a
        `sequenceToken` that acts as a cursor. Feeding it back returns the next
        three, so a consumer can keep pulling indefinitely — the queue does not
        appear to terminate.

        Returns an empty list for anything that isn't a track queue, or when the
        API stops issuing a cursor.
        """
        if not self.is_track_queue:
            return []
        token = self._source.get("sequenceToken")
        if not token:
            return []
        await self._tune(token)
        return self.tracks

    async def iter_tracks(self, limit: int | None = None) -> AsyncIterator[Track]:
        """Yield tracks continuously, refilling the queue as it drains.

        Stops after `limit` tracks, or when the API stops handing back new ones.
        Tracks are de-duplicated, so a station that starts looping ends the
        iteration rather than repeating forever.
        """
        seen: set[str] = set()
        yielded = 0
        batch = self.tracks
        while batch:
            for track in batch:
                if track.id in seen:
                    continue
                seen.add(track.id)
                yield track
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            batch = [t for t in await self.next_tracks() if t.id not in seen]

    @property
    def is_track_queue(self) -> bool:
        """Whether this is a queue of discrete tracks rather than one stream.

        Artist stations return one stream per upcoming track, each a separate
        media file. This keys off the entity type rather than the stream count:
        `channel-xtra` also returns several streams, but those are HLS mirrors of
        one continuous broadcast, not a queue.
        """
        return self.entity_type == "artist-station"

    @property
    def tracks(self) -> list[Track]:
        """The queued tracks, for an artist station.

        Empty for anything that isn't a track queue.
        """
        if not self.is_track_queue:
            return []
        tracks: list[Track] = []
        for stream in self._source.get("streams", []):
            items = ((stream.get("metadata") or {}).get("artist") or {}).get("items") or []
            item = items[0] if items else {}
            url = next(
                (u.get("url") for u in stream.get("urls", []) if u.get("isPrimary")),
                next((u.get("url") for u in stream.get("urls", [])), None),
            )
            # Track art nests as images[name][aspect_WxH][preferredImage|defaultImage],
            # which is a third spelling again — channels use [aspect][preferred]
            # and artist stations use [aspect] directly.
            image_key = None
            for variants in (item.get("images") or {}).values():
                if not isinstance(variants, dict):
                    continue
                for aspect in variants.values():
                    if not isinstance(aspect, dict):
                        continue
                    image = aspect.get("preferredImage") or aspect.get("defaultImage")
                    if isinstance(image, dict) and image.get("url"):
                        image_key = image["url"]
                        break
                if image_key:
                    break
            tracks.append(
                Track(
                    id=stream.get("id") or item.get("id") or "",
                    title=item.get("name") or "",
                    artist=item.get("artistName"),
                    album=item.get("albumName"),
                    duration_ms=item.get("duration"),
                    url=url,
                    image_key=image_key,
                ),
            )
        return tracks

    @property
    def is_progressive(self) -> bool:
        """Whether this is a single downloadable file rather than an HLS stream.

        Live channels are HLS; on-demand episodes are a plain MP3. The two need
        completely different playback handling, so callers have to branch on it.
        """
        if self.is_track_queue:
            return False
        return self.master_playlist_url.split("?")[0].rsplit(".", 1)[-1].lower() in {
            "mp3",
            "m4a",
            "aac",
        }

    @property
    def content_url(self) -> str:
        """The direct media URL for a progressive (non-HLS) stream."""
        if not self.is_progressive:
            message = (
                f"{self.entity_type}/{self.entity_id} is an HLS stream; "
                "use get_playlist() instead"
            )
            raise ValueError(message)
        return self.master_playlist_url

    def _parse_master_playlist(self, playlist: str) -> dict[str, str | None]:
        """Map each known bitrate to its variant playlist URL.

        Audio variants are named by bitrate (`..._256k_...`). Video variants are
        named by resolution instead, so none of the bitrate keys match; those are
        collected under :attr:`variants` and reached with `playlist_url(None)`.
        """
        variants: dict[str, str | None] = dict.fromkeys(BITRATES)
        base = self.base_url
        self._variants = []
        for line in playlist.splitlines():
            line = line.strip()  # noqa: PLW2901
            if not line or line.startswith("#"):
                continue
            url = f"{base}/{line}"
            self._variants.append(url)
            for bitrate in BITRATES:
                if f"_{bitrate}_" in line:
                    variants[bitrate] = url
                    break
        return variants

    @property
    def variants(self) -> list[str]:
        """Every variant playlist URL, in the order the master listed them.

        Useful for streams whose variants aren't labelled by audio bitrate, such
        as video episodes (which are labelled by resolution).
        """
        return list(self._variants)

    @property
    def _source(self) -> dict:
        """The tuneSource payload, once the stream has been initialized."""
        if self._tune_source is None:
            message = (
                f"Stream {self.entity_type}/{self.entity_id} is not initialized; "
                "await initialize() first"
            )
            raise RuntimeError(message)
        return self._tune_source

    @property
    def stream_id(self) -> str:
        """The stream ID."""
        return self._source["streams"][0]["id"]

    @property
    def master_playlist_url(self) -> str:
        """The URL of the master (multi-bitrate) playlist."""
        return self._source["streams"][0]["urls"][0]["url"]

    @property
    def base_url(self) -> str:
        """The base URL for the stream."""
        return self.master_playlist_url.rsplit("/", 1)[0]

    @property
    def available_bitrates(self) -> list[str]:
        """The bitrates this stream actually offers."""
        return [b for b, url in self._streams_by_bitrate.items() if url]

    def playlist_url(self, bitrate: str = BITRATE_256) -> str:
        """Return the variant playlist URL for a bitrate.

        Not every channel offers every bitrate, so an unavailable one falls back
        to the highest that is available.
        """
        if self.is_track_queue:
            message = (
                f"{self.entity_type}/{self.entity_id} is a queue of tracks, not HLS; "
                "use tracks instead"
            )
            raise ValueError(message)
        if self.is_progressive:
            message = (
                f"{self.entity_type}/{self.entity_id} is a single media file, not HLS; "
                "use content_url instead"
            )
            raise ValueError(message)
        url = self._streams_by_bitrate.get(bitrate)
        if url:
            return url
        for candidate in BITRATES:
            if fallback := self._streams_by_bitrate.get(candidate):
                _LOGGER.debug(
                    "Bitrate %s unavailable for %s; using %s instead",
                    bitrate,
                    self.entity_id,
                    candidate,
                )
                return fallback
        if self._variants:
            # Variants exist but aren't bitrate-labelled (e.g. video, keyed by
            # resolution); the first is the lowest and a reasonable default.
            return self._variants[0]
        message = f"No playlist available for {self.entity_type}/{self.entity_id}"
        raise ValueError(message)

    @property
    def expires_at(self) -> float | None:
        """When this stream's signed URLs stop working, as a unix timestamp.

        The CDN path carries its own validity window; once it passes, every
        playlist and segment request 403s.
        """
        match = _SIGNED_WINDOW_RE.search(self.master_playlist_url)
        return float(match.group(2)) if match else None

    @property
    def is_expiring(self) -> bool:
        """Whether the signed URLs are close enough to expiry to re-tune."""
        expires = self.expires_at
        return expires is not None and time.time() >= (expires - STREAM_EXPIRY_MARGIN)

    async def refresh(self) -> None:
        """Re-tune, replacing signed URLs that are about to expire."""
        _LOGGER.debug("Re-tuning %s/%s before its URLs expire", self.entity_type, self.entity_id)
        await self._tune()
        if not (self.is_track_queue or self.is_progressive):
            master = await self._client.request(method="GET", url=self.master_playlist_url)
            self._streams_by_bitrate = self._parse_master_playlist(master)

    async def _ensure_fresh(self) -> None:
        """Re-tune if the signed URLs are nearly stale.

        Without this a stream cached for hours — a radio player left running —
        starts 403ing with no way back.
        """
        if self.is_expiring:
            await self.refresh()

    async def get_playlist(self, bitrate: str = BITRATE_256) -> str:
        """Get a playlist for a given bitrate."""
        await self._ensure_fresh()
        return await self._client.request(method="GET", url=self.playlist_url(bitrate))

    async def get_proxied_playlist(
        self,
        key_url: str,
        bitrate: str = BITRATE_256,
        *,
        absolute_segments: bool = False,
        window: int | None = None,
    ) -> str:
        """Get a playlist with the decryption key URI rewritten to `key_url`.

        `window` keeps only the last N segments. SiriusXM serves a live playlist
        containing its whole ~5 hour rewind buffer (1845 segments); hls.js aims
        at the live edge, and Safari stalls rather than buffering that far in.
        Trimming to a normal live window fixes it and costs nothing elsewhere.

        By default segment URIs are left relative, so they resolve back through
        whatever proxy is serving the playlist. That is the portable choice:
        browsers are blocked from fetching CDN segments directly, because the CDN
        only sends `Access-Control-Allow-Origin: https://www.siriusxm.com`.

        Set `absolute_segments` to point segment URIs straight at the CDN. Those
        URLs carry a signed token and need no auth header, so audio goes
        client-to-CDN without passing through the proxy.
        """
        playlist = await self.get_playlist(bitrate)
        if window:
            playlist = trim_to_live_window(playlist, window)
        variant_base = self.playlist_url(bitrate).rsplit("/", 1)[0] + "/"

        lines = []
        for line in playlist.splitlines():
            if line.startswith("#EXT-X-KEY"):
                lines.append(_KEY_URI_RE.sub(rf"\g<1>{key_url}\g<3>", line))
            elif line and not line.startswith("#"):
                segment = line.strip()
                lines.append(
                    urljoin(variant_base, segment)
                    if absolute_segments
                    else segment.rsplit("/", 1)[-1]
                )
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    def bitrate_for_segment(self, segment_file: str, default: str = BITRATE_256) -> str:
        """Work out which bitrate a segment belongs to from its filename.

        Segments are named like `siriushits1_64k_1_..._v3.aac`, and each bitrate
        lives in its own directory. Players resolve segment URIs relative to the
        playlist and drop any query string, so the filename is the only clue we
        get about which variant was requested.
        """
        for bitrate in BITRATES:
            if f"_{bitrate}_" in segment_file:
                return bitrate
        return default

    async def get_segment(self, segment_file: str, bitrate: str | None = None) -> bytes:
        """Get a playlist file segment."""
        await self._ensure_fresh()
        resolved = bitrate or self.bitrate_for_segment(segment_file)
        variant_base = self.playlist_url(resolved).rsplit("/", 1)[0] + "/"
        return await self._client.request(method="GET", url=urljoin(variant_base, segment_file))

    async def get_key(self, bitrate: str = BITRATE_256) -> bytes:
        """Get the raw AES-128 decryption key for this stream.

        The key id is read from the playlist's own `EXT-X-KEY` URI. Deriving it
        instead only works by coincidence: a linear channel's key id is a fixed
        all-zero uuid, but an on-demand episode's differs from its stream id, so
        guessing produces a 404.
        """
        key_url = await self._key_url(bitrate)
        resp = await self._client.request(method="GET", url=key_url)
        return base64.b64decode(resp["key"])

    async def _key_url(self, bitrate: str = BITRATE_256) -> str:
        """Find the decryption key URL for this stream."""
        await self._ensure_fresh()
        playlist = await self.get_playlist(bitrate)
        match = _KEY_URI_RE.search(playlist)
        if match:
            return match.group(2)
        # Nothing in the playlist; fall back to the historical derivation.
        key_id = LINEAR_KEY_ID if self.entity_type == "channel-linear" else self.stream_id
        return f"{API_BASE}/playback/key/v1/{key_id}"
