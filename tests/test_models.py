"""Tests for parsing SiriusXM payloads into models."""

import base64
import json
from typing import ClassVar

import pytest

from aiosxm.models import Channel, Episode, Image, NowPlaying, Show, build_image_url


class TestChannel:
    """Parsing an all-channels container item."""

    def test_parses_real_payload(self, channel_item: dict) -> None:
        channel = Channel.from_item(channel_item)
        assert channel is not None
        assert channel.id
        assert channel.title
        assert channel.type.startswith("channel")
        assert channel.channel_number is not None

    def test_channel_number_is_a_string(self, channel_item: dict) -> None:
        # The API sends an int; MA and the proxy JSON both want it stringly typed.
        assert isinstance(channel_item["decorations"]["channelNumber"], int)
        channel = Channel.from_item(channel_item)
        assert isinstance(channel.channel_number, str)

    def test_is_linear(self) -> None:
        assert Channel(id="1", type="channel-linear", title="x").is_linear
        assert not Channel(id="1", type="channel-xtra", title="x").is_linear

    def test_rejects_non_channel_entities(self) -> None:
        assert Channel.from_item({"entity": {"id": "1", "type": "show-podcast"}}) is None

    def test_rejects_item_without_id(self) -> None:
        assert Channel.from_item({"entity": {"type": "channel-linear"}}) is None
        assert Channel.from_item({}) is None

    def test_accepts_typed_wrapper_shape(self) -> None:
        # The container endpoint wraps entities as `channelLinear` rather than `entity`.
        channel = Channel.from_item(
            {"channelLinear": {"id": "abc", "type": "channel-linear", "texts": {}}}
        )
        assert channel is not None
        assert channel.id == "abc"

    def test_survives_missing_optional_fields(self) -> None:
        channel = Channel.from_item({"entity": {"id": "abc", "type": "channel-linear"}})
        assert channel is not None
        assert channel.title == ""
        assert channel.description is None
        assert channel.images == []
        assert channel.unentitled is False

    def test_genre_dict_is_flattened(self) -> None:
        channel = Channel.from_item(
            {
                "entity": {"id": "a", "type": "channel-linear", "texts": {}},
                "decorations": {"genre": {"default": "Pop"}},
            }
        )
        assert channel is not None
        assert channel.genre == "Pop"

    def test_genre_of_unexpected_type_is_dropped(self) -> None:
        channel = Channel.from_item(
            {
                "entity": {"id": "a", "type": "channel-linear", "texts": {}},
                "decorations": {"genre": ["Pop", "Rock"]},
            }
        )
        assert channel is not None
        assert channel.genre is None


class TestChannelImages:
    """Images nest three levels deep; a flat read silently yields nothing."""

    def test_parses_nested_image_structure(self, channel_item: dict) -> None:
        channel = Channel.from_item(channel_item)
        assert channel is not None
        assert channel.images, "expected images to be parsed"
        assert all(isinstance(i, Image) for i in channel.images)
        assert all(i.key for i in channel.images)

    def test_aspect_prefix_is_stripped(self, channel_item: dict) -> None:
        channel = Channel.from_item(channel_item)
        assert channel is not None
        assert all(not (i.aspect_ratio or "").startswith("aspect_") for i in channel.images)

    def test_image_lookup_prefers_requested_aspect(self) -> None:
        channel = Channel(
            id="a",
            type="channel-linear",
            title="x",
            images=[
                Image(name="tile", key="wide.jpg", aspect_ratio="16x9"),
                Image(name="tile", key="square.jpg", aspect_ratio="1x1"),
            ],
        )
        assert channel.image("tile", "1x1").key == "square.jpg"
        assert channel.image("tile", "16x9").key == "wide.jpg"

    def test_image_lookup_falls_back_to_any_aspect(self) -> None:
        channel = Channel(
            id="a",
            type="channel-linear",
            title="x",
            images=[Image(name="tile", key="wide.jpg", aspect_ratio="16x9")],
        )
        assert channel.image("tile", "1x1").key == "wide.jpg"

    def test_missing_image_returns_none(self) -> None:
        channel = Channel(id="a", type="channel-linear", title="x")
        assert channel.image("tile") is None
        assert channel.image_url("tile") is None

    def test_malformed_image_entries_are_skipped(self) -> None:
        channel = Channel.from_item(
            {
                "entity": {
                    "id": "a",
                    "type": "channel-linear",
                    "texts": {},
                    "images": {
                        "tile": {"aspect_1x1": {"default": {}}},  # no url
                        "logo": "not-a-dict",
                        "hero": {"aspect_1x1": "also-not-a-dict"},
                    },
                }
            }
        )
        assert channel is not None
        assert channel.images == []


class TestImageUrls:
    """Artwork goes through a transform service keyed by base64 JSON."""

    def test_url_encodes_key_and_size(self) -> None:
        url = build_image_url("path/to/art.jpg", 300, 300)
        encoded = url.rsplit("/", 1)[-1]
        payload = json.loads(base64.b64decode(encoded))
        assert payload["key"] == "path/to/art.jpg"
        assert payload["edits"][0]["resize"] == {"width": 300, "height": 300}

    def test_format_is_optional(self) -> None:
        payload = json.loads(base64.b64decode(build_image_url("a.jpg", 1, 1).rsplit("/", 1)[-1]))
        assert len(payload["edits"]) == 1

        payload = json.loads(
            base64.b64decode(build_image_url("a.jpg", 1, 1, "webp").rsplit("/", 1)[-1])
        )
        assert payload["edits"][1] == {"format": {"type": "webp"}}

    def test_size_is_requestable(self) -> None:
        image = Image(name="tile", key="a.jpg", width=1920, height=1920)
        small = json.loads(base64.b64decode(image.url_at(64, 64).rsplit("/", 1)[-1]))
        assert small["edits"][0]["resize"] == {"width": 64, "height": 64}

    def test_native_size_used_by_default(self) -> None:
        image = Image(name="tile", key="a.jpg", width=800, height=600)
        payload = json.loads(base64.b64decode(image.url.rsplit("/", 1)[-1]))
        assert payload["edits"][0]["resize"] == {"width": 800, "height": 600}


class TestNowPlaying:
    """The lookaround feed carries cuts (tracks) and shows (programmes)."""

    def test_parses_real_feed(self, lookaround: dict) -> None:
        parsed = {
            cid: NowPlaying.from_feed_entry(cid, entry)
            for cid, entry in lookaround["channels"].items()
        }
        assert any(v is not None for v in parsed.values())

    def test_latest_cut_wins(self) -> None:
        current = NowPlaying.from_feed_entry(
            "chan",
            {
                "cuts": [
                    {"name": "Old", "artistName": "A", "validFrom": "2026-01-01T00:00:00Z"},
                    {"name": "New", "artistName": "B", "validFrom": "2026-01-01T00:05:00Z"},
                ]
            },
        )
        assert current is not None
        assert current.title == "New"
        assert current.artist == "B"

    def test_ads_are_flagged(self) -> None:
        current = NowPlaying.from_feed_entry(
            "chan", {"cuts": [{"name": "Buy things", "isAd": True}]}
        )
        assert current is not None
        assert current.is_ad is True

    def test_show_without_cuts(self) -> None:
        current = NowPlaying.from_feed_entry(
            "chan", {"cuts": [], "shows": [{"name": "Morning Show"}]}
        )
        assert current is not None
        assert current.show == "Morning Show"
        assert current.title is None

    def test_empty_entry_returns_none(self) -> None:
        assert NowPlaying.from_feed_entry("chan", {}) is None
        assert NowPlaying.from_feed_entry("chan", {"cuts": [], "shows": []}) is None

    def test_art_falls_back_to_show_image(self) -> None:
        current = NowPlaying.from_feed_entry(
            "chan",
            {
                "cuts": [{"name": "Track"}],
                "shows": [{"name": "Show", "image": {"url": "show.jpg"}}],
            },
        )
        assert current is not None
        assert current.image_url() is not None

    def test_no_art_returns_none(self) -> None:
        current = NowPlaying.from_feed_entry("chan", {"cuts": [{"name": "Track"}]})
        assert current is not None
        assert current.image_url() is None


class TestShow:
    """Shows are containers; episodes hang off them."""

    ENTITY: ClassVar[dict] = {
        "id": "s1",
        "type": "show-podcast",
        "texts": {
            "title": {"default": "SmartLess"},
            "description": {"default": "<p>Jason &amp; Sean</p>"},
        },
        "images": {"tile": {"aspect_1x1": {"preferred": {"url": "art/show.jpg"}}}},
    }

    def test_parses_a_search_entity(self) -> None:
        show = Show.from_item({"entity": self.ENTITY})
        assert show is not None
        assert show.title == "SmartLess"
        assert show.is_podcast is True
        assert show.image_url() is not None

    def test_description_html_is_stripped(self) -> None:
        # Descriptions arrive as markup with escaped entities.
        show = Show.from_item({"entity": self.ENTITY})
        assert show is not None
        assert show.description == "Jason & Sean"

    def test_broadcast_show_is_not_a_podcast(self) -> None:
        show = Show.from_item({"entity": {**self.ENTITY, "type": "show"}})
        assert show is not None
        assert show.is_podcast is False

    def test_missing_id_returns_none(self) -> None:
        assert Show.from_item({"entity": {"type": "show-podcast"}}) is None


class TestEpisode:
    """Duration and air date live on the container item, not the entity."""

    ITEM: ClassVar[dict] = {
        "entity": {
            "id": "e1",
            "type": "episode-podcast",
            "texts": {
                "title": {"default": "An episode"},
                "description": {"default": "<p>Notes &amp; more</p>"},
            },
            "images": {"tile": {"aspect_1x1": {"preferred": {"url": "art/ep.jpg"}}}},
        },
        "decorations": {
            "duration": 3260081,
            "originalAirDate": "2026-08-03T07:00:00Z",
            "showId": "s1",
            "unentitled": False,
        },
    }

    def test_parses_a_container_item(self) -> None:
        episode = Episode.from_item(self.ITEM)
        assert episode is not None
        assert episode.title == "An episode"
        assert episode.show_id == "s1"
        assert episode.air_date == "2026-08-03T07:00:00Z"
        assert episode.image_url() is not None

    def test_duration_is_seconds(self) -> None:
        episode = Episode.from_item(self.ITEM)
        assert episode is not None
        assert episode.duration == pytest.approx(3260.081)

    def test_description_html_is_stripped(self) -> None:
        episode = Episode.from_item(self.ITEM)
        assert episode is not None
        assert episode.description == "Notes & more"

    def test_entity_without_decorations(self) -> None:
        # A bare entity carries no duration; it must not crash.
        episode = Episode.from_item({"entity": self.ITEM["entity"]})
        assert episode is not None
        assert episode.duration is None
        assert episode.air_date is None

    def test_missing_id_returns_none(self) -> None:
        assert Episode.from_item({"entity": {"type": "episode-podcast"}}) is None
