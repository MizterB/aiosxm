"""Data models for the aiosxm package."""

import base64
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Self

# SiriusXM serves artwork through an image-transform service: the path is a
# base64-encoded JSON document describing the source key and the edits to apply.
IMAGE_SERVICE_BASE = "https://imgsrv-sxm-prod-device.streaming.siriusxm.com/"


def build_image_url(key: str, width: int, height: int, image_format: str | None = None) -> str:
    """Build a transform-service URL for an image key at a given size."""
    edits: list[dict[str, Any]] = [{"resize": {"width": width, "height": height}}]
    if image_format:
        edits.append({"format": {"type": image_format}})
    payload = json.dumps({"key": key, "edits": edits}, separators=(",", ":"))
    return IMAGE_SERVICE_BASE + base64.b64encode(payload.encode()).decode()


def _first_image_key(images: dict, names: tuple[str, ...] = ("tile", "logo", "header")) -> str | None:
    """Find an image key, tolerating both nesting shapes SiriusXM uses.

    Hydration returns images[name][aspect] -> {url}; search returns the
    channel-style images[name][aspect][default|preferred] -> {url}.
    """
    for name in names:
        variants = images.get(name)
        if not isinstance(variants, dict):
            continue
        for image in variants.values():
            if not isinstance(image, dict):
                continue
            if image.get("url"):
                return image["url"]
            nested = image.get("preferred") or image.get("default")
            if isinstance(nested, dict) and nested.get("url"):
                return nested["url"]
    return None


def _clean_text(value: str | None) -> str | None:
    """Strip HTML from a description.

    Podcast descriptions arrive as markup (`<p>…</p>`) with escaped entities, so
    handing them straight to a UI shows tags instead of text.
    """
    if not value:
        return value
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _text(texts: dict, key: str, variant: str = "default") -> str | None:
    """Pull a text variant out of a SiriusXM `texts` block."""
    value = texts.get(key)
    if isinstance(value, dict):
        return value.get(variant) or value.get("default")
    return None


@dataclass(slots=True)
class Image:
    """An image associated with a channel."""

    name: str
    key: str
    width: int | None = None
    height: int | None = None
    aspect_ratio: str | None = None

    @property
    def url(self) -> str:
        """The image URL at its native size."""
        return self.url_at(self.width or 300, self.height or 300)

    def url_at(self, width: int, height: int, image_format: str | None = None) -> str:
        """Return the image URL resized to the given dimensions."""
        return build_image_url(self.key, width, height, image_format)


@dataclass(slots=True)
class SkipLimits:
    """How many skips the account has left on the current stream.

    SiriusXM rations skips per hour, the same way broadcast-licensed radio
    services generally must. A client should check `can_skip_forward` before
    offering a next-track control, and surface `more_skips_at` when it is
    exhausted rather than letting the skip fail.
    """

    forward: int = 0
    backward: int = 0
    # ISO-8601 timestamp at which the allowance refills, when exhausted.
    more_skips_at: str | None = None

    @property
    def can_skip_forward(self) -> bool:
        """Whether a forward skip is currently allowed."""
        return self.forward > 0

    @property
    def can_skip_backward(self) -> bool:
        """Whether a backward skip is currently allowed."""
        return self.backward > 0

    @classmethod
    def from_payload(cls, payload: dict | None) -> Self:
        """Build from a tuneSource `skipLimits` block, or a skip response.

        Both spell the counts the same way; tuneSource nests them under
        `limited`, while the skip endpoint returns them at the top level.
        """
        data = payload or {}
        limited = data.get("limited")
        if isinstance(limited, dict):
            data = limited
        return cls(
            forward=data.get("availableForwardSkips") or 0,
            backward=data.get("availableBackwardSkips") or 0,
            more_skips_at=data.get("moreSkipsAvailableTime"),
        )


@dataclass(slots=True)
class Track:
    """One track from an artist station's or Xtra channel's queue."""

    id: str
    title: str
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    url: str | None = None
    image_key: str | None = None
    # Where to start playing. Non-zero only for the track already in progress
    # when a channel is tuned mid-song.
    start_offset_ms: int = 0
    # Each queued track is encrypted under its own key.
    encryption_key_id: str | None = None

    @property
    def duration(self) -> float | None:
        """Duration in seconds."""
        return self.duration_ms / 1000 if self.duration_ms else None

    @property
    def start_offset(self) -> float:
        """Start offset in seconds."""
        return self.start_offset_ms / 1000 if self.start_offset_ms else 0.0

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Album art for this track."""
        return build_image_url(self.image_key, width, height) if self.image_key else None


@dataclass(slots=True)
class ArtistStation:
    """A personalised station seeded from an artist.

    Unlike a channel, this is a queue of individual tracks rather than a
    continuous broadcast, and each track is a separate unencrypted MP4.
    """

    id: str
    title: str
    description: str | None = None
    image_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Station artwork."""
        return build_image_url(self.image_key, width, height) if self.image_key else None

    @classmethod
    def from_entity(cls, entity: dict) -> Self | None:
        """Build a station from a hydrated `artistStation` entity."""
        station = entity.get("artistStation") or entity
        station_id = station.get("id")
        if not station_id:
            return None

        image_key = _first_image_key(station.get("images") or {})

        texts = station.get("texts", {})
        return cls(
            id=station_id,
            title=_text(texts, "title") or "",
            description=_text(texts, "description"),
            image_key=image_key,
            raw=station,
        )


@dataclass(slots=True)
class Show:
    """A show or podcast: a container of episodes, not playable itself."""

    id: str
    type: str
    title: str
    description: str | None = None
    image_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_podcast(self) -> bool:
        """Whether this is an on-demand podcast rather than a broadcast show."""
        return self.type == "show-podcast"

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Show artwork."""
        return build_image_url(self.image_key, width, height) if self.image_key else None

    @classmethod
    def from_item(cls, item: dict) -> Self | None:
        """Build a show from a search result or container item."""
        entity = item.get("entity") or item
        entity_id = entity.get("id")
        if not entity_id:
            return None
        texts = entity.get("texts", {})
        return cls(
            id=entity_id,
            type=entity.get("type") or "show",
            title=_text(texts, "title") or "",
            description=_clean_text(_text(texts, "description")),
            image_key=_first_image_key(entity.get("images") or {}, ("tile", "hero_tile")),
            raw=item,
        )


@dataclass(slots=True)
class SearchResults:
    """Everything one search returned, split by type.

    A search hits every type at once, so each list is independently empty rather
    than the whole result being absent.
    """

    channels: list["Channel"] = field(default_factory=list)
    shows: list["Show"] = field(default_factory=list)
    artist_stations: list["ArtistStation"] = field(default_factory=list)
    talent: list["Talent"] = field(default_factory=list)
    genres: list["Genre"] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Whether the search matched anything at all."""
        return any((self.channels, self.shows, self.artist_stations, self.talent, self.genres))


@dataclass(slots=True)
class Talent:
    """A host or personality: a container of shows, not playable itself."""

    id: str
    title: str
    description: str | None = None
    image_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Talent artwork."""
        return build_image_url(self.image_key, width, height) if self.image_key else None

    @classmethod
    def from_item(cls, item: dict) -> Self | None:
        """Build a talent from a search result or container item."""
        entity = item.get("entity") or item
        entity_id = entity.get("id")
        if not entity_id:
            return None
        texts = entity.get("texts", {})
        return cls(
            id=entity_id,
            title=_text(texts, "title") or "",
            description=_clean_text(_text(texts, "description")),
            image_key=_first_image_key(entity.get("images") or {}, ("tile", "hero_tile")),
            raw=item,
        )


@dataclass(slots=True)
class Genre:
    """A genre as a searchable entity, distinct from a channel's genre string."""

    id: str
    title: str
    image_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Genre artwork."""
        return build_image_url(self.image_key, width, height) if self.image_key else None

    @classmethod
    def from_item(cls, item: dict) -> Self | None:
        """Build a genre from a search result or container item."""
        entity = item.get("entity") or item
        entity_id = entity.get("id")
        if not entity_id:
            return None
        return cls(
            id=entity_id,
            title=_text(entity.get("texts", {}), "title") or "",
            image_key=_first_image_key(
                entity.get("images") or {},
                ("tile_circle", "tile_background", "tile"),
            ),
            raw=item,
        )


@dataclass(slots=True)
class Episode:
    """One episode of a show or podcast."""

    id: str
    type: str
    title: str
    description: str | None = None
    duration_ms: int | None = None
    air_date: str | None = None
    show_id: str | None = None
    position: int = 0
    unentitled: bool = False
    image_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def duration(self) -> float | None:
        """Duration in seconds."""
        return self.duration_ms / 1000 if self.duration_ms else None

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Episode artwork."""
        return build_image_url(self.image_key, width, height) if self.image_key else None

    @classmethod
    def from_item(cls, item: dict, position: int = 0) -> Self | None:
        """Build an episode from a container item.

        Duration and air date live in the item's `decorations`, not the entity,
        so an entity on its own cannot carry them. `position` is the index in the
        show's episode list — SiriusXM doesn't number episodes, but consumers
        (Music Assistant among them) need a sort position.
        """
        entity = item.get("entity") or item
        entity_id = entity.get("id")
        if not entity_id:
            return None
        texts = entity.get("texts", {})
        decorations = item.get("decorations") or {}
        return cls(
            id=entity_id,
            type=entity.get("type") or "episode-podcast",
            title=_text(texts, "title") or "",
            description=_clean_text(_text(texts, "description")),
            duration_ms=decorations.get("duration"),
            air_date=decorations.get("originalAirDate") or decorations.get("airDate"),
            show_id=decorations.get("showId"),
            position=position,
            unentitled=bool(decorations.get("unentitled", False)),
            image_key=_first_image_key(entity.get("images") or {}, ("tile", "hero_tile")),
            raw=item,
        )


@dataclass(slots=True)
class Broadcast:
    """A sports broadcast: a live game, or one scheduled to air.

    Games are not playable entities themselves. Each carries one or more feeds
    (typically home and away commentary), and each feed names the channel that
    actually carries it — which is what you tune.
    """

    id: str
    title: str
    air_date: str | None = None
    league: str | None = None
    is_live: bool = False
    coverage: str | None = None
    play_entity_type: str | None = None
    play_entity_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_playable(self) -> bool:
        """Whether this broadcast names a channel that can be tuned."""
        return bool(self.play_entity_type and self.play_entity_id)

    @classmethod
    def from_item(cls, item: dict, *, is_live: bool = False) -> Self | None:
        """Build a broadcast from a `live` or `live-upcoming-events` item."""
        entity = item.get("entity") or {}
        if not entity.get("id"):
            return None
        decorations = item.get("decorations") or {}
        actions = item.get("actions") or {}

        play = (actions.get("play") or [{}])[0].get("entity") or {}
        return cls(
            id=entity["id"],
            title=_text(entity.get("texts", {}), "title") or "",
            air_date=decorations.get("airDate"),
            league=decorations.get("leagueName"),
            is_live=is_live,
            coverage=decorations.get("airingCoverage"),
            play_entity_type=play.get("type"),
            play_entity_id=play.get("id"),
            raw=item,
        )


@dataclass(slots=True)
class NowPlaying:
    """What is currently playing on a linear channel."""

    channel_id: str
    title: str | None = None
    artist: str | None = None
    show: str | None = None
    is_ad: bool = False
    started_at: str | None = None
    image_key: str | None = None
    show_image_key: str | None = None

    def image_url(self, width: int = 300, height: int = 300) -> str | None:
        """Return the album art for the current cut, falling back to the show's artwork."""
        key = self.image_key or self.show_image_key
        return build_image_url(key, width, height) if key else None

    @classmethod
    def from_feed_entry(cls, channel_id: str, entry: dict) -> Self | None:
        """Build a NowPlaying from one channel's lookaround feed entry.

        Cuts arrive oldest-first, so the last one is what's playing now.
        """
        cuts = entry.get("cuts") or []
        shows = entry.get("shows") or []
        if not cuts and not shows:
            return None

        cut = cuts[-1] if cuts else {}
        show = shows[-1] if shows else {}
        cut_image = cut.get("image") or {}
        show_image = show.get("image") or {}

        return cls(
            channel_id=channel_id,
            title=cut.get("name"),
            artist=cut.get("artistName"),
            show=show.get("name"),
            is_ad=bool(cut.get("isAd", False)),
            started_at=cut.get("validFrom") or show.get("validFrom"),
            image_key=cut_image.get("url"),
            show_image_key=show_image.get("url"),
        )


@dataclass(slots=True)
class Channel:
    """A SiriusXM channel."""

    id: str
    type: str
    title: str
    channel_number: str | None = None
    title_short: str | None = None
    description: str | None = None
    genre: str | None = None
    unentitled: bool = False
    off_air: bool = False
    images: list[Image] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_linear(self) -> bool:
        """Whether this is a live linear channel."""
        return self.type == "channel-linear"

    def image(self, name: str = "tile", aspect_ratio: str = "1x1") -> Image | None:
        """Get a named image, preferring the given aspect ratio."""
        candidates = [i for i in self.images if i.name == name]
        if not candidates:
            return None
        for image in candidates:
            if image.aspect_ratio == aspect_ratio:
                return image
        return candidates[0]

    def image_url(
        self,
        name: str = "tile",
        aspect_ratio: str = "1x1",
        size: tuple[int, int] = (300, 300),
    ) -> str | None:
        """Get the URL of a named image at the given size."""
        image = self.image(name, aspect_ratio)
        return image.url_at(*size) if image else None

    @classmethod
    def from_item(cls, item: dict) -> Self | None:
        """Build a Channel from an all-channels container item.

        Returns None for items that aren't channels or are missing an id, so a
        single odd entry can't break enumeration of the whole list.
        """
        entity = item.get("entity") or item.get("channelLinear") or {}
        entity_id = entity.get("id")
        entity_type = entity.get("type", "channel-linear")
        if not entity_id or not str(entity_type).startswith("channel"):
            return None

        texts = entity.get("texts", {})
        decorations = item.get("decorations", {})

        # Images nest as images[name][aspect_WxH][default|preferred] -> {url, width, height}.
        images: list[Image] = []
        for name, aspects in (entity.get("images") or {}).items():
            if not isinstance(aspects, dict):
                continue
            for aspect_key, variants in aspects.items():
                if not isinstance(variants, dict):
                    continue
                image = variants.get("preferred") or variants.get("default")
                if not isinstance(image, dict) or not image.get("url"):
                    continue
                images.append(
                    Image(
                        name=name,
                        key=image["url"],
                        width=image.get("width"),
                        height=image.get("height"),
                        aspect_ratio=aspect_key.removeprefix("aspect_"),
                    ),
                )

        channel_number = decorations.get("channelNumber")
        genre = decorations.get("genre")
        if isinstance(genre, dict):
            genre = genre.get("default") or genre.get("name")

        return cls(
            id=entity_id,
            type=entity_type,
            title=_text(texts, "title") or "",
            channel_number=str(channel_number) if channel_number is not None else None,
            title_short=_text(texts, "title", "short"),
            description=_text(texts, "description"),
            genre=genre if isinstance(genre, str) else None,
            unentitled=bool(decorations.get("unentitled", False)),
            off_air=bool(decorations.get("offAir", False)),
            images=images,
            raw=item,
        )
