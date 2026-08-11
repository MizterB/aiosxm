"""Tests for the HLS proxy and its embedding API."""

import asyncio
import re
import time
from pathlib import Path
from typing import ClassVar

import pytest
from aiohttp import web
from aioresponses import aioresponses

from aiosxm import SxmClient
from aiosxm.const import API_BASE, CHANNEL_CACHE_TTL, LOOKAROUND_URL
from aiosxm.proxy import _cache_is_stale, load_dotenv, make_routes, router
from tests.conftest import mock_auth
from tests.test_stream import MASTER, MASTER_URL, MEDIA, TUNE

# aioresponses patches all aiohttp traffic; let the test client reach its own server.
PASSTHROUGH = ["http://127.0.0.1", "http://localhost"]
CHANNELS_RE = re.compile(r".*relationship/v1/container/all-channels.*")


@pytest.fixture
async def proxy_client(aiohttp_client):
    """A test client for the proxy, backed by a mocked SiriusXM API."""
    client = SxmClient("u", "p")
    with aioresponses(passthrough=PASSTHROUGH) as mocked:
        mock_auth(mocked)
        await client.connect()

    app = web.Application()
    for method, path, handler in make_routes(client):
        app.router.add_route(method, path, handler)

    yield await aiohttp_client(app), client
    await client.disconnect()


class TestMakeRoutes:
    """Routes must embed in a host app that knows nothing about aiosxm."""

    async def test_returns_method_path_handler_tuples(self) -> None:
        client = SxmClient("u", "p")
        routes = make_routes(client)
        assert routes
        for method, path, handler in routes:
            assert method in {"GET", "POST", "DELETE", "*"}
            assert path.startswith("/")
            assert callable(handler)
        await client.disconnect()

    async def test_stream_only_filters_to_playback_routes(self) -> None:
        client = SxmClient("u", "p")
        routes = make_routes(client, stream_only=True)
        assert routes
        assert all(path.startswith("/stream/") for _, path, _ in routes)
        assert len(routes) < len(make_routes(client))
        await client.disconnect()

    async def test_prefix_is_applied(self) -> None:
        client = SxmClient("u", "p")
        routes = make_routes(client, prefix="/sxm", stream_only=True)
        assert all(path.startswith("/sxm/stream/") for _, path, _ in routes)
        await client.disconnect()

    async def test_handlers_do_not_need_app_state(self, aiohttp_client) -> None:
        # This is what MA does: mount the routes, never set app["sxm"].
        client = SxmClient("u", "p")
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mock_auth(mocked)
            await client.connect()

        app = web.Application()
        for method, path, handler in make_routes(client, stream_only=True):
            app.router.add_route(method, path, handler)
        assert "sxm" not in app

        http = await aiohttp_client(app)
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            mocked.get(
                "https://cdn.example.com/v1/token/chan/HLS_chan_256k_v3/chan_256k_full_v3.m3u8",
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            resp = await http.get("/stream/channel-linear/abc/playlist.m3u8")
        assert resp.status == 200
        await client.disconnect()


class TestPlaylistRoute:
    """Playlist rewriting, bitrates, and segment modes."""

    async def _playlist(self, http, mocked, query: str = "") -> str:
        mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
        mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
        mocked.get(
            "https://cdn.example.com/v1/token/chan/HLS_chan_256k_v3/chan_256k_full_v3.m3u8",
            body=MEDIA,
            content_type="application/x-mpegurl",
        )
        resp = await http.get(f"/stream/channel-linear/abc/playlist.m3u8{query}")
        assert resp.status == 200
        return await resp.text()

    async def test_key_uri_points_back_at_the_proxy(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            playlist = await self._playlist(http, mocked)
        assert 'URI="/stream/channel-linear/abc/key"' in playlist

    async def test_segments_are_relative_by_default(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            playlist = await self._playlist(http, mocked)
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert all(not s.startswith("http") for s in segments)

    async def test_absolute_mode_points_at_the_cdn(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            playlist = await self._playlist(http, mocked, "?absolute=1")
        segments = [ln for ln in playlist.splitlines() if ln and not ln.startswith("#")]
        assert all(s.startswith("https://cdn.example.com/") for s in segments)

    async def test_content_type_is_hls(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            mocked.get(
                "https://cdn.example.com/v1/token/chan/HLS_chan_256k_v3/chan_256k_full_v3.m3u8",
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            resp = await http.get("/stream/channel-linear/abc/playlist.m3u8")
        assert resp.content_type == "application/vnd.apple.mpegurl"

    async def test_not_entitled_becomes_403(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", status=403, body="nope")
            resp = await http.get("/stream/channel-linear/abc/playlist.m3u8")
        assert resp.status == 403

    async def test_upstream_failure_becomes_502(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", status=500, body="boom")
            resp = await http.get("/stream/channel-linear/abc/playlist.m3u8")
        assert resp.status == 502


class TestJsonRoutes:
    """Channel, library, entitlement and now-playing endpoints."""

    async def test_channels_are_serialized_with_image_urls(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(
                CHANNELS_RE,
                payload={"container": {"sets": [{"items": [channel_item]}]}},
            )
            mocked.get(
                CHANNELS_RE,
                payload={"container": {"sets": []}},
            )
            resp = await http.get("/channels")
            body = await resp.json()
        assert resp.status == 200
        assert body
        assert body[0]["id"]
        # `url` is a computed property, so it must be materialised for JSON.
        assert all("url" in image for image in body[0]["images"])

    async def test_channels_are_cached(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        pattern = CHANNELS_RE
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(pattern, payload={"container": {"sets": [{"items": [channel_item]}]}})
            mocked.get(pattern, payload={"container": {"sets": []}})
            first = await (await http.get("/channels")).json()
            # No further upstream responses are registered; a cache miss would 500.
            second = await (await http.get("/channels")).json()
        assert first == second

    async def test_entitlements_report_status(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(
                f"{API_BASE}/entitlement/v1/entitlements",
                payload={"entitlements": [{"id": "a", "state": "revoked"}]},
            )
            body = await (await http.get("/entitlements")).json()
        assert body["entitled"] is False

    async def test_now_playing_for_one_channel(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(
                LOOKAROUND_URL,
                payload={"channels": {"c1": {"cuts": [{"name": "Song", "artistName": "X"}]}}},
            )
            body = await (await http.get("/now-playing/c1")).json()
        assert body["title"] == "Song"
        assert body["artist"] == "X"

    async def test_unknown_channel_now_playing_is_404(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(LOOKAROUND_URL, payload={"channels": {}})
            resp = await http.get("/now-playing/nope")
        assert resp.status == 404


class TestUiRoute:
    """The test console is served from the package."""

    async def test_index_is_served(self, proxy_client) -> None:
        http, _ = proxy_client
        resp = await http.get("/")
        assert resp.status == 200
        assert resp.content_type == "text/html"

    async def test_ui_file_ships_with_the_package(self) -> None:
        from aiosxm.proxy import UI_FILE

        assert UI_FILE.is_file(), "static/index.html must be packaged"


class TestCors:
    """A browser on another origin has to be able to call the proxy."""

    async def test_cors_header_is_present(self, proxy_client) -> None:
        http, _ = proxy_client
        # The middleware is registered by create_app, so assert on that app.
        from aiosxm.proxy import cors_middleware

        assert cors_middleware is not None
        resp = await http.get("/")
        assert resp.status == 200


class TestDotenv:
    """Credential loading for the CLI."""

    def test_reads_key_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SXM_USERNAME", raising=False)
        monkeypatch.delenv("SXM_PASSWORD", raising=False)
        env = tmp_path / ".env"
        env.write_text("# comment\nSXM_USERNAME=file@example.com\n\nSXM_PASSWORD='quoted'\n")
        load_dotenv(env)
        import os

        assert os.environ["SXM_USERNAME"] == "file@example.com"
        assert os.environ["SXM_PASSWORD"] == "quoted"

    def test_environment_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SXM_USERNAME", "env@example.com")
        env = tmp_path / ".env"
        env.write_text("SXM_USERNAME=file@example.com\n")
        load_dotenv(env)
        import os

        assert os.environ["SXM_USERNAME"] == "env@example.com"

    def test_missing_file_is_harmless(self, tmp_path: Path) -> None:
        load_dotenv(tmp_path / "nope.env")


class TestRouteTable:
    """Guard the documented route surface."""

    def test_expected_routes_exist(self) -> None:
        paths = {route.path for route in router}
        assert "/" in paths
        assert "/channels" in paths
        assert "/library" in paths
        assert "/entitlements" in paths
        assert "/now-playing" in paths
        assert "/stream/{entity_type}/{entity_id}/playlist.m3u8" in paths
        assert "/stream/{entity_type}/{entity_id}/key" in paths


class TestLibraryRoute:
    """Library resolution reuses the channel cache."""

    async def test_library_filters_the_catalog(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        wanted = channel_item["entity"]["id"]
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(
                f"{API_BASE}/ondemand/v1/library/all",
                payload={"allDataMap": {wanted: {"entityId": wanted, "entityType": "channel-linear"}}},
            )
            mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": [channel_item]}]}})
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            body = await (await http.get("/library")).json()
        assert [c["id"] for c in body] == [wanted]


class TestSegmentRoute:
    """Segments are fetched from the variant matching their filename."""

    async def test_segment_is_served(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            mocked.get(
                re.compile(r".*HLS_chan_64k_v3.*"),
                body=b"audio-bytes",
                content_type="audio/aac",
            )
            resp = await http.get("/stream/channel-linear/abc/chan_64k_1_000_00100_v3.aac")
            body = await resp.read()
        assert resp.status == 200
        assert resp.content_type == "audio/aac"
        assert body == b"audio-bytes"

    async def test_key_is_served_as_bytes(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            mocked.get(
                "https://cdn.example.com/v1/token/chan/HLS_chan_256k_v3/chan_256k_full_v3.m3u8",
                body=MEDIA,
                content_type="application/x-mpegurl",
            )
            mocked.get(
                "https://api.example.com/playback/key/v1/abc",
                payload={"key": "AAECAwQFBgcICQoLDA0ODw=="},
            )
            resp = await http.get("/stream/channel-linear/abc/key")
            body = await resp.read()
        assert len(body) == 16
        assert resp.content_type == "application/octet-stream"

    async def test_stream_info_lists_bitrates(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            body = await (await http.get("/stream/channel-linear/abc/info")).json()
        assert body["available_bitrates"] == ["256k", "64k"]


class TestMissingClient:
    """A route mounted without a client should fail loudly, not confusingly."""

    async def test_no_client_is_a_500(self, aiohttp_client) -> None:
        app = web.Application()
        for route in router:
            if route.path == "/channels":
                app.router.add_route(route.method, route.path, route.handler)
        http = await aiohttp_client(app)
        resp = await http.get("/channels")
        assert resp.status == 500


class TestChannelCache:
    """The cache must not stampede, and must not go stale forever."""

    async def test_concurrent_requests_walk_the_catalog_once(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            # Only one walk's worth of responses is registered; a stampede would
            # exhaust them and fail.
            mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": [channel_item]}]}})
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            results = await asyncio.gather(*(http.get("/channels") for _ in range(5)))
            bodies = [await r.json() for r in results]
        assert all(r.status == 200 for r in results)
        assert all(b == bodies[0] for b in bodies)

    async def test_expired_cache_is_rebuilt(self, proxy_client, channel_item: dict) -> None:
        http, client = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": [channel_item]}]}})
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            first = await (await http.get("/channels")).json()

            # The client owns the TTL now, so age its cache, not the proxy's.
            client._channels_fetched_at = time.monotonic() - (CHANNEL_CACHE_TTL + 1)

            other = {**channel_item, "entity": {**channel_item["entity"], "id": "new-id"}}
            mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": [other]}]}})
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            second = await (await http.get("/channels")).json()

        assert first[0]["id"] != second[0]["id"], "a stale cache should be refetched"

    def test_fresh_cache_is_not_stale(self) -> None:
        assert _cache_is_stale({"fetched_at": time.monotonic()}) is False

    def test_cache_without_a_timestamp_is_not_stale(self) -> None:
        assert _cache_is_stale({}) is False

    def test_old_cache_is_stale(self) -> None:
        assert _cache_is_stale({"fetched_at": time.monotonic() - CHANNEL_CACHE_TTL - 1}) is True


class TestTrackQueueRoute:
    """?count= walks the queue cursor to fill a longer playlist."""

    ARTIST_TUNE: ClassVar[dict] = {
        "type": "artist-station",
        "sequenceToken": "tok-1",
        "streams": [
            {
                "id": f"t{n}",
                "urls": [{"url": f"https://cdn.example.com/{n}.mp4", "isPrimary": True}],
                "metadata": {"artist": {"items": [{"id": f"t{n}", "name": f"Track {n}"}]}},
            }
            for n in range(3)
        ],
    }

    async def test_default_returns_one_batch(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=self.ARTIST_TUNE)
            body = await (await http.get("/stream/artist-station/s1/tracks")).json()
        assert len(body) == 3

    async def test_count_pulls_further_batches(self, proxy_client) -> None:
        http, _ = proxy_client
        second = {
            **self.ARTIST_TUNE,
            "streams": [
                {
                    "id": f"u{n}",
                    "urls": [{"url": f"https://cdn.example.com/u{n}.mp4", "isPrimary": True}],
                    "metadata": {"artist": {"items": [{"id": f"u{n}", "name": f"Next {n}"}]}},
                }
                for n in range(3)
            ],
        }
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=self.ARTIST_TUNE)
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=second)
            body = await (await http.get("/stream/artist-station/s1/tracks?count=6")).json()
        assert len(body) == 6
        assert len({t["id"] for t in body}) == 6

    async def test_bad_count_is_rejected(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=self.ARTIST_TUNE)
            resp = await http.get("/stream/artist-station/s1/tracks?count=abc")
        assert resp.status == 400

    async def test_tracks_route_rejects_a_channel(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
            mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
            resp = await http.get("/stream/channel-linear/abc/tracks")
        assert resp.status == 400


class TestBrowse:
    """The browse tree is what a consumer walks to discover content."""

    async def _with_catalog(self, http, mocked, channel_item: dict, extra=None) -> list:
        items = [channel_item, *list(extra or [])]
        mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": items}]}})
        mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
        return await (await http.get("/channels")).json()

    async def test_root_lists_folders(self, proxy_client) -> None:
        http, _ = proxy_client
        body = await (await http.get("/browse")).json()
        assert {f["id"] for f in body} >= {"genres", "channels", "library"}
        assert all(f["path"].startswith("/") for f in body)

    async def test_genres_are_counted(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        other = {
            **channel_item,
            "entity": {**channel_item["entity"], "id": "jazz-1"},
            "decorations": {**channel_item.get("decorations", {}), "genre": "Jazz"},
        }
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            await self._with_catalog(http, mocked, channel_item, [other])
            body = await (await http.get("/browse/genres")).json()
        genres = {g["title"]: g["count"] for g in body}
        assert "Jazz" in genres
        assert all(g["path"].startswith("/browse/genres/") for g in body)

    async def test_genre_leaf_returns_channels(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        other = {
            **channel_item,
            "entity": {**channel_item["entity"], "id": "jazz-1"},
            "decorations": {**channel_item.get("decorations", {}), "genre": "Jazz"},
        }
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            await self._with_catalog(http, mocked, channel_item, [other])
            body = await (await http.get("/browse/genres/Jazz")).json()
        assert [c["id"] for c in body] == ["jazz-1"]

    async def test_genre_match_is_case_insensitive(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        other = {
            **channel_item,
            "entity": {**channel_item["entity"], "id": "jazz-1"},
            "decorations": {**channel_item.get("decorations", {}), "genre": "Jazz"},
        }
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            await self._with_catalog(http, mocked, channel_item, [other])
            assert (await http.get("/browse/genres/jazz")).status == 200

    async def test_unknown_genre_is_404(self, proxy_client, channel_item: dict) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            await self._with_catalog(http, mocked, channel_item)
            assert (await http.get("/browse/genres/Nonexistent")).status == 404

    async def test_shows_requires_a_query(self, proxy_client) -> None:
        http, _ = proxy_client
        assert (await http.get("/shows")).status == 400

    async def test_shows_link_to_their_episodes(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "show-1",
                                            "type": "show-podcast",
                                            "texts": {"title": {"default": "SmartLess"}},
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            body = await (await http.get("/shows?q=comedy")).json()
        assert body[0]["title"] == "SmartLess"
        assert body[0]["path"] == "/podcasts/show-1/episodes"


class TestSportsRoutes:
    """Team discovery over HTTP."""

    async def test_teams_requires_a_query(self, proxy_client) -> None:
        http, _ = proxy_client
        assert (await http.get("/teams")).status == 400

    async def test_teams_link_to_broadcasts(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "t1",
                                            "type": "team",
                                            "texts": {"title": {"default": "Chicago Bears"}},
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            body = await (await http.get("/teams?q=bears")).json()
        assert body[0]["title"] == "Chicago Bears"
        assert body[0]["path"] == "/teams/t1/broadcasts"

    async def test_broadcasts_expose_a_play_path(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(
                re.compile(r".*/page/v1/page/team/.*"),
                payload={"page": {"containers": [{"url": "relationship/v1/container/live?entityId=t1"}]}},
            )
            mocked.get(
                re.compile(r".*container/live\?.*"),
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "ep1",
                                            "type": "episode-linear",
                                            "texts": {"title": {"default": "Play-by-Play"}},
                                        },
                                        "actions": {"play": [{"entity": {"type": "channel-linear", "id": "ch1"}}]},
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            body = await (await http.get("/teams/t1/broadcasts")).json()
        assert body[0]["playable"] is True
        assert body[0]["play_path"] == "/stream/channel-linear/ch1/playlist.m3u8"


class TestGenericBrowse:
    """Two routes walk the whole catalog graph."""

    PAGE: ClassVar[dict] = {"page": {"containers": [{"url": "relationship/v1/container/aod?entityId=x"}]}}

    async def test_entity_sections_are_listed(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            body = await (await http.get("/browse/talent/t1")).json()
        assert [s["id"] for s in body] == ["aod"]
        assert body[0]["path"] == "/browse/talent/t1/aod"

    async def test_container_children_carry_paths(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            mocked.get(
                re.compile(r".*container/aod\?.*"),
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "e1",
                                            "type": "episode-podcast",
                                            "texts": {"title": {"default": "An episode"}},
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            body = await (await http.get("/browse/talent/t1/aod")).json()
        assert body[0]["title"] == "An episode"
        # episode-podcast is directly playable, so it gets a play path.
        assert body[0]["play_path"] == "/stream/episode-podcast/e1/playlist.m3u8"

    async def test_non_playable_entity_uses_its_play_action(self, proxy_client) -> None:
        # An event isn't playable itself but names the channel carrying it.
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(re.compile(r".*/page/v1/page/team/.*"), payload=self.PAGE)
            mocked.get(
                re.compile(r".*container/aod\?.*"),
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {"id": "ev1", "type": "event"},
                                        "actions": {"play": [{"entity": {"type": "channel-linear", "id": "ch1"}}]},
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            body = await (await http.get("/browse/team/t1/aod")).json()
        assert body[0]["play_path"] == "/stream/channel-linear/ch1/playlist.m3u8"

    async def test_unknown_container_is_404(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            assert (await http.get("/browse/talent/t1/nope")).status == 404

    async def test_genre_routes_are_not_shadowed(self, proxy_client, channel_item: dict) -> None:
        # /browse/genres must keep winning over /browse/{type}/{id}.
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": [channel_item]}]}})
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            body = await (await http.get("/browse/genres")).json()
        assert body
        assert "count" in body[0]


class TestArtistStationRoute:
    """`?q=` searches; bare returns the library."""

    async def test_query_searches(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "as1",
                                            "type": "artist-station",
                                            "texts": {"title": {"default": "Metallica"}},
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            body = await (await http.get("/artist-stations?q=metallica")).json()
        assert [s["title"] for s in body] == ["Metallica"]

    async def test_no_query_returns_the_library(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.get(f"{API_BASE}/ondemand/v1/library/all", payload={"allDataMap": {}})
            body = await (await http.get("/artist-stations")).json()
        assert body == []


class TestLibraryMutationRoutes:
    """Library writes use POST/DELETE, not GET."""

    async def test_post_adds(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/ondemand/v1/library/update", payload={})
            resp = await http.post("/library/channel-linear/c1")
            body = await resp.json()
        assert resp.status == 200
        assert body["added"] is True

    async def test_delete_removes(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/ondemand/v1/library/update", payload={})
            resp = await http.delete("/library/channel-linear/c1")
            body = await resp.json()
        assert resp.status == 200
        assert body["removed"] is True

    async def test_get_does_not_mutate(self, proxy_client) -> None:
        # /library is the read route; it must not accept a write verb by accident.
        http, _ = proxy_client
        assert (await http.put("/library/channel-linear/c1")).status == 405

    async def test_upstream_failure_is_502(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            mocked.post(f"{API_BASE}/ondemand/v1/library/update", status=400, body="bad")
            assert (await http.post("/library/channel-linear/c1")).status == 502


class TestRangeRequests:
    """WebKit-based players issue range requests and expect 206."""

    async def _segment(self, http, mocked, headers=None) -> object:
        mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=TUNE)
        mocked.get(MASTER_URL, body=MASTER, content_type="application/x-mpegurl")
        mocked.get(
            re.compile(r".*HLS_chan_256k_v3.*\.aac"),
            body=b"0123456789",
            content_type="audio/aac",
        )
        return await http.get(
            "/stream/channel-linear/abc/chan_256k_1_000_00100_v3.aac",
            headers=headers or {},
        )

    async def test_no_range_returns_200_with_accept_ranges(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            resp = await self._segment(http, mocked)
            body = await resp.read()
        assert resp.status == 200
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert body == b"0123456789"

    async def test_range_returns_206(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            resp = await self._segment(http, mocked, {"Range": "bytes=2-5"})
            body = await resp.read()
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 2-5/10"
        assert body == b"2345"

    async def test_open_ended_range(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            resp = await self._segment(http, mocked, {"Range": "bytes=7-"})
            body = await resp.read()
        assert resp.status == 206
        assert body == b"789"

    async def test_suffix_range(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            resp = await self._segment(http, mocked, {"Range": "bytes=-3"})
            body = await resp.read()
        assert resp.status == 206
        assert body == b"789"

    async def test_unsatisfiable_range_is_416(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            resp = await self._segment(http, mocked, {"Range": "bytes=999-"})
        assert resp.status == 416

    async def test_malformed_range_falls_back_to_200(self, proxy_client) -> None:
        http, _ = proxy_client
        with aioresponses(passthrough=PASSTHROUGH) as mocked:
            resp = await self._segment(http, mocked, {"Range": "bytes=abc"})
        assert resp.status == 200
