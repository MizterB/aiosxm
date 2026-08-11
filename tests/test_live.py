"""Smoke tests against the real SiriusXM API.

These are the tests that notice when SiriusXM changes something. Everything else
in the suite runs against captured fixtures, which means a payload shape can move
underneath us and the mocked tests will happily keep passing — that is exactly how
the channel listing broke in the first place.

Skipped unless you pass `--live` AND credentials are available:

    uv run pytest --live

They need an entitled account; playback assertions are skipped without one.
"""

import pytest

from aiosxm import Channel, NotEntitledError, NowPlaying, SxmClient

pytestmark = pytest.mark.live


@pytest.fixture
async def live_client() -> SxmClient:
    """A client authenticated against the real API.

    Function-scoped: each test gets a fresh login. That is slower than sharing
    one session, but it also exercises the auth chain on every test rather than
    once, which is the point of a smoke test.
    """
    client = SxmClient()
    await client.connect()
    yield client
    await client.close()


class TestLiveAuth:
    """The five-step chain still works."""

    async def test_authenticates(self, live_client: SxmClient) -> None:
        assert live_client._access_token
        assert live_client._access_token_expiration is not None
        assert live_client.entitlement_hash

    async def test_refresh_token_is_issued(self, live_client: SxmClient) -> None:
        assert live_client._refresh_token, "no refresh cookie — refresh flow would fall back"

    async def test_refresh_yields_a_working_token(self, live_client: SxmClient) -> None:
        before = live_client._access_token
        assert await live_client._refresh_session() is True
        assert live_client._access_token != before
        # The new token has to actually work.
        assert await live_client.get_entitlements()


class TestLiveChannels:
    """The channel catalog still parses."""

    async def test_catalog_is_populated(self, live_client: SxmClient) -> None:
        channels = await live_client.get_channels()
        # A silent contract change shows up as an empty or tiny list, which is
        # what the original bug looked like.
        assert len(channels) > 100, f"only {len(channels)} channels — has the API moved?"
        assert all(isinstance(c, Channel) for c in channels)

    async def test_channels_have_the_fields_consumers_need(
        self, live_client: SxmClient
    ) -> None:
        channels = await live_client.get_channels()
        linear = [c for c in channels if c.is_linear]
        assert linear, "no linear channels"
        sample = linear[0]
        assert sample.id
        assert sample.title
        assert sample.channel_number
        assert sample.images, "channel artwork disappeared"
        assert sample.image_url("logo", "1x1", (300, 300))

    async def test_both_channel_types_are_present(self, live_client: SxmClient) -> None:
        types = {c.type for c in await live_client.get_channels()}
        assert "channel-linear" in types
        assert "channel-xtra" in types

    async def test_discovery_still_finds_the_container(self, live_client: SxmClient) -> None:
        entity_id, container_id = await live_client.discover_all_channels_ids()
        assert entity_id
        assert container_id


class TestLiveNowPlaying:
    """The lookaround feed still parses."""

    async def test_feed_covers_many_channels(self, live_client: SxmClient) -> None:
        feed = await live_client.get_now_playing_all()
        assert len(feed) > 100, f"only {len(feed)} channels in the feed"
        assert all(isinstance(v, NowPlaying) for v in feed.values())

    async def test_some_channel_has_a_track(self, live_client: SxmClient) -> None:
        feed = await live_client.get_now_playing_all()
        assert any(v.title and v.artist for v in feed.values())


class TestLiveSearch:
    """Search still reaches on-demand content."""

    async def test_finds_podcasts(self, live_client: SxmClient) -> None:
        results = await live_client.search("news")
        assert "show-podcast" in results, "search no longer returns podcasts"
        assert results["show-podcast"][0].get("id")

    async def test_finds_channels(self, live_client: SxmClient) -> None:
        channels = await live_client.search_channels("hits")
        assert channels
        assert all(isinstance(c, Channel) for c in channels)


class TestLivePlayback:
    """Streams still resolve. Requires an entitled account."""

    @pytest.fixture(autouse=True)
    async def _require_entitlement(self, live_client: SxmClient) -> None:
        if not await live_client.is_entitled():
            pytest.skip("account has no active entitlement")

    async def test_linear_channel_plays(self, live_client: SxmClient) -> None:
        channels = await live_client.get_channels()
        channel = next(c for c in channels if c.is_linear and not c.unentitled)
        stream = await live_client.get_stream(channel.type, channel.id)

        assert not stream.is_progressive
        assert stream.available_bitrates
        playlist = await stream.get_playlist()
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert segments, "playlist has no segments"
        assert len(await stream.get_key()) == 16
        assert len(await stream.get_segment(segments[0])) > 1000

    async def test_xtra_channel_plays(self, live_client: SxmClient) -> None:
        channels = await live_client.get_channels()
        channel = next(
            (c for c in channels if c.type == "channel-xtra" and not c.unentitled), None
        )
        if channel is None:
            pytest.skip("no entitled xtra channel")
        stream = await live_client.get_stream(channel.type, channel.id)
        assert stream.available_bitrates
        assert len(await stream.get_key()) == 16

    async def test_podcast_episode_is_progressive(self, live_client: SxmClient) -> None:
        results = await live_client.search("news")
        shows = results.get("show-podcast") or []
        episodes: list[dict] = []
        for show in shows[:3]:
            episodes = await live_client.get_podcast_episodes(show["id"])
            if episodes:
                break
        if not episodes:
            pytest.skip("no podcast episodes found")

        try:
            stream = await live_client.get_stream(episodes[0]["type"], episodes[0]["id"])
        except NotEntitledError:
            pytest.skip("account not entitled for on-demand")
        # Episodes are a single media file rather than HLS.
        assert stream.is_progressive
        assert stream.content_url.startswith("http")


class TestLiveArtistStations:
    """Artist stations sit outside the channel catalog."""

    async def test_library_reports_every_entity_type(self, live_client: SxmClient) -> None:
        # The library can hold artist stations as well as channels; resolving
        # only channels under-reports what the SiriusXM app shows.
        raw = await live_client.get_library()
        channels = await live_client.get_library_channels()
        stations = await live_client.get_library_artist_stations()

        wanted = [
            e
            for e in raw
            if str(e.get("entityType", "")).startswith(("channel", "artist-station"))
        ]
        assert len(channels) + len(stations) == len(wanted), (
            "library resolution is dropping entries"
        )

    async def test_stations_hydrate(self, live_client: SxmClient) -> None:
        stations = await live_client.get_library_artist_stations()
        if not stations:
            pytest.skip("no artist stations in this library")
        station = stations[0]
        assert station.id
        assert station.title
        assert station.image_url()

    async def test_station_yields_playable_tracks(self, live_client: SxmClient) -> None:
        stations = await live_client.get_library_artist_stations()
        if not stations:
            pytest.skip("no artist stations in this library")
        stream = await live_client.get_stream("artist-station", stations[0].id)

        assert stream.is_track_queue
        assert not stream.is_progressive
        tracks = stream.tracks
        assert tracks, "station returned no tracks"
        assert all(t.url for t in tracks)
        assert tracks[0].title
        assert tracks[0].artist


class TestLiveOnDemandAudio:
    """`episode-audio` is HLS, but its key id differs from its stream id."""

    async def test_episode_audio_plays(self, live_client: SxmClient) -> None:
        results = await live_client.search("spa")
        episodes = results.get("episode-audio") or []
        if not episodes:
            pytest.skip("no episode-audio results")

        stream = await live_client.get_stream("episode-audio", episodes[0]["id"])
        assert not stream.is_progressive
        assert stream.available_bitrates

        playlist = await stream.get_playlist()
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert segments
        # Deriving the key id from the stream id 404s here; it must come from
        # the playlist.
        assert len(await stream.get_key()) == 16


class TestLiveSports:
    """A team's game broadcasts resolve to a tunable channel."""

    async def test_team_search_finds_teams(self, live_client: SxmClient) -> None:
        teams = await live_client.get_teams("Chicago Bears")
        assert teams, "no teams matched"
        assert teams[0].get("id")

    async def test_team_broadcasts_are_playable(self, live_client: SxmClient) -> None:
        teams = await live_client.get_teams("Chicago Bears")
        if not teams:
            pytest.skip("no teams matched")
        broadcasts = await live_client.get_team_broadcasts(teams[0]["id"])
        if not broadcasts:
            pytest.skip("no broadcasts scheduled for this team right now")

        playable = [b for b in broadcasts if b.is_playable]
        assert playable, "no broadcast named a playable channel"
        # Games are containers; they must resolve to a real channel.
        assert all(b.play_entity_type == "channel-linear" for b in playable)

        stream = await live_client.get_broadcast_stream(playable[0])
        assert stream.available_bitrates
        assert len(await stream.get_key()) == 16


class TestLiveArtistStationSearch:
    """Artist stations are discoverable beyond the library."""

    async def test_search_finds_stations_outside_the_library(
        self, live_client: SxmClient
    ) -> None:
        library = {s.id for s in await live_client.get_library_artist_stations()}
        found = await live_client.search_artist_stations("Frank Sinatra")
        assert found, "no artist stations matched"
        assert any(s.id not in library for s in found), (
            "search returned nothing beyond the library"
        )
        assert all(s.title for s in found)

    async def test_a_searched_station_plays(self, live_client: SxmClient) -> None:
        stations = await live_client.search_artist_stations("Frank Sinatra")
        if not stations:
            pytest.skip("no artist stations matched")
        stream = await live_client.get_stream("artist-station", stations[0].id)
        assert stream.is_track_queue
        tracks = stream.tracks
        assert tracks
        assert all(t.url for t in tracks)


class TestLiveStreamLifetime:
    """A stream cached for a long session must not go stale silently."""

    async def test_stream_urls_carry_an_expiry(self, live_client: SxmClient) -> None:
        channels = await live_client.get_channels()
        channel = next(c for c in channels if c.is_linear and not c.unentitled)
        stream = await live_client.get_stream(channel.type, channel.id)
        assert stream.expires_at, "no signed window in the CDN URL — expiry can't be detected"
        assert not stream.is_expiring, "a freshly tuned stream should not look expired"

    async def test_refresh_yields_working_urls(self, live_client: SxmClient) -> None:
        # Within the signed window SiriusXM hands back the same URL, so assert
        # that a refreshed stream still plays rather than that the URL changed.
        channels = await live_client.get_channels()
        channel = next(c for c in channels if c.is_linear and not c.unentitled)
        stream = await live_client.get_stream(channel.type, channel.id)

        await stream.refresh()
        assert stream.expires_at
        assert not stream.is_expiring

        playlist = await stream.get_playlist()
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert segments
        assert len(await stream.get_key()) == 16
