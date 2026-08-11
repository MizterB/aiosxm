"""Tests for HLS stream handling."""

import re
from collections.abc import AsyncGenerator

import pytest
from aioresponses import aioresponses

from aiosxm import SxmClient, SxmStream
from aiosxm.const import API_BASE, BITRATE_32, BITRATE_96, BITRATE_256
from tests.conftest import mock_auth

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=256000
HLS_chan_256k_v3/chan_256k_full_v3.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=64000
HLS_chan_64k_v3/chan_64k_full_v3.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=AES-128,URI="https://api.example.com/playback/key/v1/abc"
#EXTINF:10.0,
chan_256k_1_000_00100_v3.aac
#EXTINF:10.0,
chan_256k_1_000_00101_v3.aac
"""

TUNE = {
    "streams": [
        {
            "id": "stream-1",
            "urls": [{"url": "https://cdn.example.com/v1/token/chan/master.m3u8"}],
        }
    ]
}
MASTER_URL = TUNE["streams"][0]["urls"][0]["url"]
BASE = "https://cdn.example.com/v1/token/chan"


@pytest.fixture
async def stream() -> AsyncGenerator[SxmStream]:
    """An initialized stream backed by the sample playlists above."""
    client = SxmClient("u", "p")
    with aioresponses() as mocked:
        mock_auth(mocked)
        await client.connect()
        mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
        mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
        result = await client.get_stream("channel-linear", "abc")
    yield result
    await client.disconnect()


class TestMasterPlaylist:
    """Bitrate variants are discovered from the master playlist."""

    async def test_finds_available_bitrates(self, stream: SxmStream) -> None:
        assert stream.available_bitrates == [BITRATE_256, "64k"]

    async def test_unlisted_bitrates_are_absent(self, stream: SxmStream) -> None:
        assert BITRATE_96 not in stream.available_bitrates
        assert BITRATE_32 not in stream.available_bitrates

    async def test_variant_url_is_absolute(self, stream: SxmStream) -> None:
        assert stream.playlist_url(BITRATE_256).startswith(BASE)

    async def test_unavailable_bitrate_falls_back(self, stream: SxmStream) -> None:
        # 96k isn't offered here; the best available should be used instead.
        assert stream.playlist_url(BITRATE_96) == stream.playlist_url(BITRATE_256)

    async def test_no_variants_raises(self) -> None:
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body="#EXTM3U\n", content_type="application/x-mpegurl")
            empty = await client.get_stream("channel-linear", "empty")
        with pytest.raises(ValueError, match="No playlist available"):
            empty.playlist_url()
        await client.disconnect()


class TestSegmentBitrate:
    """Players drop query strings, so the segment name carries the bitrate."""

    async def test_bitrate_inferred_from_filename(self, stream: SxmStream) -> None:
        assert stream.bitrate_for_segment("chan_64k_1_000_00100_v3.aac") == "64k"
        assert stream.bitrate_for_segment("chan_256k_1_000_00100_v3.aac") == BITRATE_256

    async def test_unknown_filename_uses_default(self, stream: SxmStream) -> None:
        assert stream.bitrate_for_segment("mystery.aac") == BITRATE_256
        assert stream.bitrate_for_segment("mystery.aac", default="64k") == "64k"

    async def test_segment_fetched_from_matching_variant_directory(
        self, stream: SxmStream
    ) -> None:
        # A 64k segment must come from the 64k directory, not the 256k one.
        with aioresponses() as mocked:
            mocked.get(
                re.compile(r".*HLS_chan_64k_v3.*"),
                body=b"64k-audio",
                content_type="audio/aac",
            )
            data = await stream.get_segment("chan_64k_1_000_00100_v3.aac")
        assert data == b"64k-audio"


class TestProxiedPlaylist:
    """Playlist rewriting for proxy consumption."""

    async def test_key_uri_is_rewritten(self, stream: SxmStream) -> None:
        with aioresponses() as mocked:
            mocked.get(
                stream.playlist_url(BITRATE_256),
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            playlist = await stream.get_proxied_playlist("/my/key")
        assert '#EXT-X-KEY:METHOD=AES-128,URI="/my/key"' in playlist
        assert "api.example.com/playback/key" not in playlist

    async def test_segments_relative_by_default(self, stream: SxmStream) -> None:
        with aioresponses() as mocked:
            mocked.get(
                stream.playlist_url(BITRATE_256),
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            playlist = await stream.get_proxied_playlist("/my/key")
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert segments == ["chan_256k_1_000_00100_v3.aac", "chan_256k_1_000_00101_v3.aac"]

    async def test_absolute_segments_point_at_the_cdn(self, stream: SxmStream) -> None:
        with aioresponses() as mocked:
            mocked.get(
                stream.playlist_url(BITRATE_256),
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            playlist = await stream.get_proxied_playlist("/my/key", absolute_segments=True)
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert all(s.startswith("https://cdn.example.com/") for s in segments)

    async def test_directives_are_preserved(self, stream: SxmStream) -> None:
        with aioresponses() as mocked:
            mocked.get(
                stream.playlist_url(BITRATE_256),
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            playlist = await stream.get_proxied_playlist("/my/key")
        assert "#EXT-X-MEDIA-SEQUENCE:100" in playlist
        assert playlist.count("#EXTINF:10.0,") == 2


class TestKey:
    """The AES key is base64 in the API and raw bytes on the wire."""

    async def test_key_id_comes_from_the_playlist(self, stream: SxmStream) -> None:
        # The playlist names its own key. Deriving one from the stream id only
        # works by coincidence for channels and 404s for on-demand episodes.
        with aioresponses() as mocked:
            mocked.get(
                stream.playlist_url(BITRATE_256),
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            mocked.get(
                "https://api.example.com/playback/key/v1/abc",
                payload={"key": "AAECAwQFBgcICQoLDA0ODw=="},
            )
            key = await stream.get_key()
        assert key == bytes(range(16))

    async def test_falls_back_when_the_playlist_has_no_key(self) -> None:
        unencrypted = "#EXTM3U\n#EXTINF:10.0,\nchan_256k_1_000_00100_v3.aac\n"
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            plain = await client.get_stream("channel-linear", "abc")
            mocked.get(
                plain.playlist_url(BITRATE_256),
                body=unencrypted,
                content_type="application/x-mpegurl",
            )
            mocked.get(
                f"{API_BASE}/playback/key/v1/00000000-0000-0000-0000-000000000000",
                payload={"key": "AAECAwQFBgcICQoLDA0ODw=="},
            )
            assert len(await plain.get_key()) == 16
        await client.close()


PROGRESSIVE_TUNE = {
    "streams": [
        {
            "id": "aod-1",
            "urls": [{"url": "https://cdn.example.com/ep/audio/128/default.mp3"}],
        }
    ]
}


class TestProgressiveStreams:
    """On-demand episodes are a single MP3, not HLS."""

    async def _aod(self) -> tuple[SxmClient, SxmStream]:
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/playback/play/v1/tuneSource", payload=PROGRESSIVE_TUNE
            )
            # No master playlist is fetched for a progressive stream; if the code
            # tried, aioresponses would raise a connection error here.
            stream = await client.get_stream("episode-podcast", "ep-1")
        return client, stream

    async def test_detected_as_progressive(self) -> None:
        client, stream = await self._aod()
        assert stream.is_progressive is True
        assert stream.content_url.endswith(".mp3")
        await client.disconnect()

    async def test_playlist_url_refuses(self) -> None:
        client, stream = await self._aod()
        with pytest.raises(ValueError, match="not HLS"):
            stream.playlist_url()
        await client.disconnect()

    async def test_hls_stream_is_not_progressive(self, stream: SxmStream) -> None:
        assert stream.is_progressive is False
        with pytest.raises(ValueError, match="HLS stream"):
            _ = stream.content_url


class TestUninitialized:
    """Accessing a stream before initialize() should say so plainly."""

    async def test_raises_runtime_error(self) -> None:
        client = SxmClient("u", "p")
        uninitialized = SxmStream(client, "channel-linear", "abc")
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = uninitialized.master_playlist_url
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = uninitialized.stream_id
        await client.disconnect()


ARTIST_TUNE = {
    "id": "station-1",
    "type": "artist-station",
    "streams": [
        {
            "id": "trk-1",
            "urls": [
                {"url": "https://cdn.example.com/a/pri-1/audio/1/A_64000.mp4", "isPrimary": True},
                {"url": "https://cdn.example.com/a/sec-1/audio/1/A_64000.mp4", "isPrimary": False},
            ],
            "metadata": {
                "artist": {
                    "stationName": "Dean Martin",
                    "items": [
                        {
                            "id": "trk-1",
                            "name": "That Lady",
                            "artistName": "The Isley Brothers",
                            "albumName": "Greatest Hits",
                            "duration": 158430,
                            "images": {
                                "tile": {
                                    "aspect_1x1": {
                                        "preferredImage": {"url": "art/one.jpg"},
                                        "defaultImage": {"url": "art/one.jpg"},
                                    }
                                }
                            },
                        }
                    ],
                }
            },
        },
        {
            "id": "trk-2",
            "urls": [{"url": "https://cdn.example.com/a/pri-1/audio/2/B_64000.mp4"}],
            "metadata": {
                "artist": {"items": [{"id": "trk-2", "name": "L-O-V-E", "artistName": "Nat King Cole"}]}
            },
        },
    ],
}


class TestArtistStations:
    """Artist stations are a queue of discrete tracks, not a stream."""

    async def _station(self) -> tuple[SxmClient, SxmStream]:
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=ARTIST_TUNE)
            # No master playlist fetch should happen; aioresponses would error.
            stream = await client.get_stream("artist-station", "station-1")
        return client, stream

    async def test_detected_as_a_track_queue(self) -> None:
        client, stream = await self._station()
        assert stream.is_track_queue is True
        assert stream.is_progressive is False
        await client.close()

    async def test_tracks_are_parsed(self) -> None:
        client, stream = await self._station()
        tracks = stream.tracks
        assert len(tracks) == 2
        first = tracks[0]
        assert first.title == "That Lady"
        assert first.artist == "The Isley Brothers"
        assert first.album == "Greatest Hits"
        assert first.duration == pytest.approx(158.43)
        await client.close()

    async def test_primary_url_is_preferred(self) -> None:
        client, stream = await self._station()
        assert "/pri-1/" in stream.tracks[0].url
        await client.close()

    async def test_track_art_is_resolved(self) -> None:
        # Track art nests differently again: [aspect_WxH][preferredImage].
        client, stream = await self._station()
        assert stream.tracks[0].image_url() is not None
        assert stream.tracks[1].image_url() is None
        await client.close()

    async def test_playlist_url_refuses(self) -> None:
        client, stream = await self._station()
        with pytest.raises(ValueError, match="queue of tracks"):
            stream.playlist_url()
        await client.close()

    async def test_hls_stream_is_not_a_track_queue(self, stream: SxmStream) -> None:
        assert stream.is_track_queue is False
        assert stream.tracks == []


class TestMultiStreamChannels:
    """Several streams does not mean a track queue."""

    async def test_xtra_channel_with_mirrors_is_still_hls(self) -> None:
        # channel-xtra returns multiple streams that are HLS mirrors of one
        # broadcast; treating them as a queue broke bitrate discovery.
        mirrored = {
            "type": "channel-xtra",
            "streams": [
                {"id": f"s{i}", "urls": [{"url": MASTER_URL}]} for i in range(3)
            ],
        }
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=mirrored)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            stream = await client.get_stream("channel-xtra", "x1")

        assert stream.is_track_queue is False
        assert stream.available_bitrates, "bitrates must still be discovered"
        await client.close()


def _artist_page(prefix: str, token: str | None) -> dict:
    """A tuneSource page of three tracks, optionally with a queue cursor."""
    page = {
        "type": "artist-station",
        "streams": [
            {
                "id": f"{prefix}-{n}",
                "urls": [{"url": f"https://cdn.example.com/{prefix}{n}.mp4", "isPrimary": True}],
                "metadata": {
                    "artist": {"items": [{"id": f"{prefix}-{n}", "name": f"Track {prefix}{n}"}]}
                },
            }
            for n in range(3)
        ],
    }
    if token:
        page["sequenceToken"] = token
    return page


class TestEndlessQueue:
    """The sequence token chains one batch of tracks to the next."""

    async def _station(self, pages: list[dict]) -> tuple[SxmClient, SxmStream]:
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            for page in pages:
                mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=page)
            stream = await client.get_stream("artist-station", "station-1")
        return client, stream

    async def test_next_tracks_advances(self) -> None:
        client, stream = await self._station(
            [_artist_page("a", "tok-1"), _artist_page("b", "tok-2")]
        )
        assert [t.id for t in stream.tracks] == ["a-0", "a-1", "a-2"]
        with aioresponses() as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=_artist_page("b", "t2"))
            assert [t.id for t in await stream.next_tracks()] == ["b-0", "b-1", "b-2"]
        await client.close()

    async def test_next_tracks_without_a_cursor_stops(self) -> None:
        client, stream = await self._station([_artist_page("a", None)])
        assert await stream.next_tracks() == []
        await client.close()

    async def test_iter_tracks_refills(self) -> None:
        client, stream = await self._station([_artist_page("a", "tok-1")])
        with aioresponses() as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=_artist_page("b", "t2"))
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=_artist_page("c", "t3"))
            got = [t.id async for t in stream.iter_tracks(limit=8)]
        assert got == ["a-0", "a-1", "a-2", "b-0", "b-1", "b-2", "c-0", "c-1"]
        await client.close()

    async def test_iter_tracks_stops_when_the_queue_repeats(self) -> None:
        # A station that starts handing back the same tracks must terminate
        # rather than spin forever.
        client, stream = await self._station([_artist_page("a", "tok-1")])
        with aioresponses() as mocked:
            for _ in range(3):
                mocked.post(
                    f"{API_BASE}/playback/play/v1/tuneSource", payload=_artist_page("a", "tok-1")
                )
            got = [t.id async for t in stream.iter_tracks(limit=50)]
        assert got == ["a-0", "a-1", "a-2"]
        await client.close()

    async def test_iter_tracks_is_empty_for_hls(self, stream: SxmStream) -> None:
        assert [t async for t in stream.iter_tracks(limit=5)] == []


VIDEO_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=300053,RESOLUTION=416x234
Clip-Stream1-ts-416x234p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=660943,RESOLUTION=640x360
Clip-Stream3-ts-640x360p.m3u8
"""


class TestNonBitrateVariants:
    """Video variants are labelled by resolution, not audio bitrate."""

    async def _video(self) -> tuple[SxmClient, SxmStream]:
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=VIDEO_MASTER, content_type="application/x-mpegurl")
            stream = await client.get_stream("episode-video", "v1")
        return client, stream

    async def test_no_bitrates_are_detected(self) -> None:
        client, stream = await self._video()
        assert stream.available_bitrates == []
        await client.close()

    async def test_variants_are_still_collected(self) -> None:
        client, stream = await self._video()
        assert len(stream.variants) == 2
        assert all(v.endswith(".m3u8") for v in stream.variants)
        await client.close()

    async def test_playlist_url_falls_back_to_a_variant(self) -> None:
        # Previously this raised "No playlist available", which was accurate but
        # useless — the variants were right there.
        client, stream = await self._video()
        assert stream.playlist_url() == stream.variants[0]
        await client.close()

    async def test_truly_empty_master_still_raises(self) -> None:
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body="#EXTM3U\n", content_type="application/x-mpegurl")
            empty = await client.get_stream("channel-linear", "e1")
        with pytest.raises(ValueError, match="No playlist available"):
            empty.playlist_url()
        await client.close()


LONG_MEDIA = "\n".join([
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-TARGETDURATION:10",
    "#EXT-X-MEDIA-SEQUENCE:100",
    '#EXT-X-KEY:METHOD=AES-128,URI="https://api.example.com/playback/key/v1/abc"',
    *[f"#EXTINF:10.0,\nchan_256k_1_000_{100 + n:05d}_v3.aac" for n in range(50)],
]) + "\n"


class TestLiveWindow:
    """SiriusXM serves its whole rewind buffer; players want a live window."""

    def test_trims_to_the_last_n_segments(self) -> None:
        from aiosxm.stream import trim_to_live_window

        out = trim_to_live_window(LONG_MEDIA, 6)
        segments = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
        assert len(segments) == 6
        assert segments[-1].endswith("00149_v3.aac"), "should keep the newest segments"

    def test_media_sequence_is_advanced(self) -> None:
        # The AES IV defaults to the sequence number, so a stale one decrypts to
        # noise rather than audio.
        from aiosxm.stream import trim_to_live_window

        out = trim_to_live_window(LONG_MEDIA, 6)
        seq = next(ln for ln in out.splitlines() if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"))
        assert seq == "#EXT-X-MEDIA-SEQUENCE:144"

    def test_key_is_carried_over(self) -> None:
        # The key tag sits above the cut point; dropping it breaks decryption.
        from aiosxm.stream import trim_to_live_window

        out = trim_to_live_window(LONG_MEDIA, 6)
        assert any(ln.startswith("#EXT-X-KEY") for ln in out.splitlines())

    def test_short_playlist_is_untouched(self) -> None:
        from aiosxm.stream import trim_to_live_window

        assert trim_to_live_window(LONG_MEDIA, 500) == LONG_MEDIA

    async def test_proxied_playlist_honours_window(self, stream: SxmStream) -> None:
        with aioresponses() as mocked:
            mocked.get(
                stream.playlist_url(BITRATE_256),
                body=LONG_MEDIA,
                content_type="application/x-mpegurl",
            )
            playlist = await stream.get_proxied_playlist("/my/key", window=4)
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert len(segments) == 4


SIGNED_TUNE = {
    "streams": [
        {
            "id": "stream-1",
            "urls": [{"url": "https://cdn.example.com/v1/tok_1700000000-1700086400_x/chan/master.m3u8"}],
        }
    ]
}


class TestSignedUrlExpiry:
    """CDN URLs are signed for a window; a long-lived stream must re-tune."""

    def test_expiry_is_read_from_the_url(self) -> None:
        client = SxmClient("u", "p")
        stream = SxmStream(client, "channel-linear", "abc")
        stream._tune_source = SIGNED_TUNE
        assert stream.expires_at == 1700086400.0

    def test_no_window_means_no_expiry(self) -> None:
        client = SxmClient("u", "p")
        stream = SxmStream(client, "channel-linear", "abc")
        stream._tune_source = TUNE  # no signed window in the URL
        assert stream.expires_at is None
        assert stream.is_expiring is False

    def test_past_window_is_expiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = SxmClient("u", "p")
        stream = SxmStream(client, "channel-linear", "abc")
        stream._tune_source = SIGNED_TUNE
        monkeypatch.setattr("aiosxm.stream.time.time", lambda: 1700086400.0)
        assert stream.is_expiring is True

    def test_inside_window_is_not_expiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = SxmClient("u", "p")
        stream = SxmStream(client, "channel-linear", "abc")
        stream._tune_source = SIGNED_TUNE
        monkeypatch.setattr("aiosxm.stream.time.time", lambda: 1700000000.0)
        assert stream.is_expiring is False

    async def test_playlist_retunes_when_expiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A stream cached past its window would otherwise 403 forever.
        client = SxmClient("u", "p")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=SIGNED_TUNE)
            mocked.get(
                re.compile(r".*master\.m3u8"), body=MASTER, content_type="application/x-mpegurl"
            )
            stream = await client.get_stream("channel-linear", "abc")

            monkeypatch.setattr("aiosxm.stream.time.time", lambda: 1700086400.0)
            assert stream.is_expiring

            # A fresh tune, then the variant playlist it points at.
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            mocked.get(
                re.compile(r".*HLS_chan_256k_v3.*"),
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            playlist = await stream.get_playlist()

        assert "#EXTM3U" in playlist
        # The re-tune replaced the stale source with the fresh one.
        assert stream.master_playlist_url == MASTER_URL
        await client.close()
