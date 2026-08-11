"""Tests for the SiriusXM client."""

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from aioresponses import aioresponses

from aiosxm import AuthenticationError, NotEntitledError, RequestError, SxmClient
from aiosxm.client import MAX_RETRIES, RETRY_BACKOFF
from aiosxm.const import (
    ALL_CHANNELS_CONTAINER_ID,
    ALL_CHANNELS_ENTITY_ID,
    API_BASE,
    CHANNEL_CACHE_TTL,
    LOOKAROUND_URL,
    REFRESH_TOKEN_COOKIE,
    SXM_REQUEST_HEADERS,
)
from tests.conftest import load_fixture, mock_auth

CHANNELS_RE = re.compile(rf"{re.escape(API_BASE)}/relationship/v1/container/all-channels.*")


class TestCredentials:
    """Credentials come from arguments or the environment."""

    def test_requires_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SXM_USERNAME", raising=False)
        monkeypatch.delenv("SXM_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="username and password are required"):
            SxmClient()

    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SXM_USERNAME", "env@example.com")
        monkeypatch.setenv("SXM_PASSWORD", "envpass")
        client = SxmClient()
        assert client._username == "env@example.com"

    def test_arguments_win_over_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SXM_USERNAME", "env@example.com")
        monkeypatch.setenv("SXM_PASSWORD", "envpass")
        client = SxmClient("arg@example.com", "argpass")
        assert client._username == "arg@example.com"


class TestAuthentication:
    """The five-step device -> anonymous -> identity -> password -> session chain."""

    async def test_connect_walks_the_chain(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
        assert client._access_token == "access-token"
        assert client.entitlement_hash == "hash123"
        await client.disconnect()

    async def test_missing_password_is_an_auth_error(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mocked.post(f"{API_BASE}/device/v1/devices", payload={"grant": "g"})
            mocked.post(
                f"{API_BASE}/session/v1/sessions/anonymous", payload={"accessToken": "a"}
            )
            mocked.get(
                re.compile(rf"{re.escape(API_BASE)}/identity/v1/identities/status.*"),
                payload={"hasPassword": False},
            )
            with pytest.raises(AuthenticationError, match="does not have a password"):
                await client.connect()
        await client.disconnect()

    async def test_bad_credentials_raise_auth_error(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mocked.post(f"{API_BASE}/device/v1/devices", payload={"grant": "g"})
            mocked.post(
                f"{API_BASE}/session/v1/sessions/anonymous", payload={"accessToken": "a"}
            )
            mocked.get(
                re.compile(rf"{re.escape(API_BASE)}/identity/v1/identities/status.*"),
                payload={"hasPassword": True},
            )
            mocked.post(
                f"{API_BASE}/identity/v1/identities/authenticate/password", status=401
            )
            with pytest.raises(AuthenticationError):
                await client.connect()
        await client.disconnect()

    async def test_unexpected_response_shape_is_an_auth_error(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mocked.post(f"{API_BASE}/device/v1/devices", payload={"unexpected": True})
            with pytest.raises(AuthenticationError, match="unexpected response"):
                await client.connect()
        await client.disconnect()


class TestTokenHandling:
    """Token refresh must be proactive, and must not leak into shared state."""

    async def test_shared_header_constant_is_not_mutated(self, client: SxmClient) -> None:
        before = dict(SXM_REQUEST_HEADERS)
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", payload={})
            await client.request("GET", f"{API_BASE}/ping")
        assert before == SXM_REQUEST_HEADERS
        assert "Authorization" not in SXM_REQUEST_HEADERS
        await client.disconnect()

    async def test_token_near_expiry_is_refreshed(self, client: SxmClient) -> None:
        soon = (datetime.now(tz=UTC) + timedelta(seconds=60)).isoformat()
        with aioresponses() as mocked:
            mock_auth(mocked, expires_at=soon)
            await client.connect()
            # Within the refresh margin, so the next request re-authenticates.
            assert client._token_is_stale()
            mock_auth(mocked)
            mocked.get(f"{API_BASE}/ping", payload={"ok": True})
            assert await client.request("GET", f"{API_BASE}/ping") == {"ok": True}
        await client.disconnect()

    async def test_token_far_from_expiry_is_kept(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            assert not client._token_is_stale()
        await client.disconnect()

    async def test_401_triggers_one_retry(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", status=401)
            mock_auth(mocked)
            mocked.get(f"{API_BASE}/ping", payload={"recovered": True})
            assert await client.request("GET", f"{API_BASE}/ping") == {"recovered": True}
        await client.disconnect()

    async def test_repeated_401_gives_up(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", status=401)
            mock_auth(mocked)
            mocked.get(f"{API_BASE}/ping", status=401)
            with pytest.raises(RequestError) as err:
                await client.request("GET", f"{API_BASE}/ping")
            assert err.value.status == 401
        await client.disconnect()

    async def test_client_errors_are_not_retried(self, client: SxmClient) -> None:
        # A 403 is an answer, not a blip: asking again cannot change it.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", status=403, body="not entitled")
            with pytest.raises(RequestError) as err:
                await client.request("GET", f"{API_BASE}/ping")
            assert err.value.status == 403
        await client.disconnect()


class TestChannels:
    """Walking the paginated all-channels container."""

    async def test_walks_until_a_page_is_empty(self, client: SxmClient, channel_item: dict) -> None:
        def page(items: list[dict]) -> dict:
            # `hits` deliberately lies (it echoes the requested page size), which
            # is why the walk must not trust it as a total.
            return {
                "container": {
                    "sets": [{"items": items, "pagination": {"hits": 30, "offset": 0}}]
                }
            }

        def channel(n: int) -> dict:
            return {**channel_item, "entity": {**channel_item["entity"], "id": f"id-{n}"}}

        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(CHANNELS_RE, payload=page([channel(i) for i in range(30)]))
            mocked.get(CHANNELS_RE, payload=page([channel(i) for i in range(30, 45)]))
            mocked.get(CHANNELS_RE, payload=page([]))
            channels = await client.get_channels(page_size=30)

        assert len(channels) == 45, "should keep paging past a full first page"
        await client.disconnect()

    async def test_deduplicates_and_stops_on_repeats(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        # A server that keeps returning the same page must not loop forever.
        page = {
            "container": {"sets": [{"items": [channel_item], "pagination": {"hits": 999}}]}
        }
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            for _ in range(5):
                mocked.get(CHANNELS_RE, payload=page)
            channels = await client.get_channels(page_size=30)
        assert len(channels) == 1
        await client.disconnect()

    async def test_empty_response_yields_no_channels(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            assert await client.get_channels() == []
        await client.disconnect()

    async def test_request_includes_the_params_the_api_demands(self, client: SxmClient) -> None:
        # Dropping containerId/maxResponses/setResponseStructure makes the API
        # return an empty set instead of an error, so assert they're present.
        url = client._channels_url(0, 30, "entity-id", ALL_CHANNELS_CONTAINER_ID)
        for param in (
            "containerId=",
            "maxResponses=",
            "setResponseStructure=",
            "entityType=curated-grouping",
        ):
            assert param in url


class TestLibrary:
    """Library entries are id stubs that must be resolved against the catalog."""

    async def test_resolves_stubs_against_the_catalog(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        wanted = channel_item["entity"]["id"]
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                f"{API_BASE}/ondemand/v1/library/all",
                payload={
                    "allDataMap": {
                        wanted: {"entityId": wanted, "entityType": "channel-linear"},
                        "gone": {"entityId": "gone", "entityType": "channel-linear"},
                        "pod": {"entityId": "pod", "entityType": "show-podcast"},
                    }
                },
            )
            mocked.get(
                CHANNELS_RE,
                payload={"container": {"sets": [{"items": [channel_item]}]}},
            )
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            library = await client.get_library_channels()

        # 'gone' is no longer in the catalog and 'pod' isn't a channel.
        assert [c.id for c in library] == [wanted]
        await client.disconnect()

    async def test_empty_library_skips_the_catalog_walk(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ondemand/v1/library/all", payload={"allDataMap": {}})
            assert await client.get_library_channels() == []
        await client.disconnect()


class TestEntitlements:
    """A lapsed subscription authenticates but cannot play."""

    async def test_revoked_entitlements_are_not_entitled(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                f"{API_BASE}/entitlement/v1/entitlements",
                payload={"entitlements": [{"id": "a", "state": "revoked"}]},
            )
            assert await client.is_entitled() is False
        await client.disconnect()

    async def test_granted_entitlement_is_entitled(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                f"{API_BASE}/entitlement/v1/entitlements",
                payload={
                    "entitlements": [
                        {"id": "a", "state": "revoked"},
                        {"id": "b", "state": "granted"},
                    ]
                },
            )
            assert await client.is_entitled() is True
        await client.disconnect()


class TestNowPlaying:
    """The lookaround feed is unauthenticated and covers every channel at once."""

    async def test_parses_the_feed(self, client: SxmClient, lookaround: dict) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(LOOKAROUND_URL, payload=lookaround)
            feed = await client.get_now_playing_all()
        assert feed
        assert all(hasattr(v, "channel_id") for v in feed.values())
        await client.disconnect()

    async def test_single_channel_lookup(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                LOOKAROUND_URL,
                payload={"channels": {"chan-1": {"cuts": [{"name": "Song", "artistName": "X"}]}}},
            )
            current = await client.get_now_playing("chan-1")
            assert current is not None
            assert current.title == "Song"

            mocked.get(LOOKAROUND_URL, payload={"channels": {}})
            assert await client.get_now_playing("missing") is None
        await client.disconnect()


class TestStreams:
    """Stream construction and the not-entitled path."""

    async def test_403_becomes_not_entitled(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/playback/play/v1/tuneSource", status=403, body="not entitled"
            )
            with pytest.raises(NotEntitledError):
                await client.get_stream("channel-linear", "abc")
            # The failed stream must not be cached.
            assert ("channel-linear", "abc") not in client._streams
        await client.disconnect()

    async def test_streams_are_reused(
        self, client: SxmClient, tune_source: dict, master_playlist: str
    ) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/playback/play/v1/tuneSource", payload=tune_source)
            mocked.get(
                tune_source["streams"][0]["urls"][0]["url"],
                body=master_playlist,
                content_type="application/x-mpegurl",
            )
            first = await client.get_stream("channel-linear", "abc")
            second = await client.get_stream("channel-linear", "abc")
        assert first is second, "an initialized stream should be reused"
        await client.disconnect()


class TestDiscovery:
    """The all-channels ids are re-readable from the page descriptor."""

    async def test_reads_container_id_from_page(self, client: SxmClient) -> None:
        page = load_fixture("page_descriptor")
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                re.compile(rf"{re.escape(API_BASE)}/page/v1/page/curated-grouping/.*"),
                payload=page,
            )
            entity_id, container_id = await client.discover_all_channels_ids()
        assert entity_id == ALL_CHANNELS_ENTITY_ID
        assert container_id
        await client.disconnect()

    async def test_falls_back_to_defaults(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                re.compile(rf"{re.escape(API_BASE)}/page/v1/page/curated-grouping/.*"),
                payload={"page": {"containers": []}},
            )
            entity_id, container_id = await client.discover_all_channels_ids()
        assert (entity_id, container_id) == (
            ALL_CHANNELS_ENTITY_ID,
            ALL_CHANNELS_CONTAINER_ID,
        )
        await client.disconnect()


class TestPodcasts:
    """On-demand episodes are what the legacy library cannot do at all."""

    async def test_parses_episodes(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                re.compile(rf"{re.escape(API_BASE)}/relationship/v1/container/aod.*"),
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {"entity": {"id": "ep1", "type": "episode-podcast"}},
                                    {"no_entity": True},
                                ]
                            }
                        ]
                    }
                },
            )
            episodes = await client.get_podcast_episodes("show-1")
        assert [e["id"] for e in episodes] == ["ep1"]
        await client.disconnect()

    async def test_empty_response(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                re.compile(rf"{re.escape(API_BASE)}/relationship/v1/container/aod.*"),
                payload={"container": {"sets": []}},
            )
            assert await client.get_podcast_episodes("show-1") == []
        await client.disconnect()


class TestContextManager:
    """`async with` must yield the client, not None."""

    async def test_aenter_returns_self(self) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            async with SxmClient("u", "p") as instance:
                assert isinstance(instance, SxmClient)
                assert instance.entitlement_hash == "hash123"


class TestSearch:
    """Search is the only route to on-demand content."""

    async def test_groups_results_by_entity_type(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {"entity": {"id": "c1", "type": "channel-linear"}},
                                    {"entity": {"id": "p1", "type": "show-podcast"}},
                                ]
                            },
                            {"items": [{"entity": {"id": "p2", "type": "show-podcast"}}]},
                            {"items": [{"no_entity": True}]},
                        ]
                    }
                },
            )
            results = await client.search("news")
        assert sorted(results) == ["channel-linear", "show-podcast"]
        assert len(results["show-podcast"]) == 2
        await client.disconnect()

    async def test_search_channels_returns_models(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "c1",
                                            "type": "channel-linear",
                                            "texts": {"title": {"default": "Chan"}},
                                        }
                                    },
                                    {"entity": {"id": "p1", "type": "show-podcast"}},
                                ]
                            }
                        ]
                    }
                },
            )
            channels = await client.search_channels("news")
        assert [c.id for c in channels] == ["c1"]
        assert channels[0].title == "Chan"
        await client.disconnect()

    async def test_empty_results(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/search/v1/search", payload={"container": {"sets": []}})
            assert await client.search("nothing") == {}
        await client.disconnect()

    async def test_search_all_splits_every_type(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "c1",
                                            "type": "channel-linear",
                                            "texts": {"title": {"default": "Chan"}},
                                        }
                                    },
                                    {"entity": {"id": "c2", "type": "channel-xtra"}},
                                    {
                                        "entity": {
                                            "id": "s1",
                                            "type": "show-podcast",
                                            "texts": {"title": {"default": "Pod"}},
                                        }
                                    },
                                    {"entity": {"id": "s2", "type": "show"}},
                                    {
                                        "entity": {
                                            "id": "a1",
                                            "type": "artist-station",
                                            "texts": {"title": {"default": "Station"}},
                                        }
                                    },
                                    {
                                        "entity": {
                                            "id": "t1",
                                            "type": "talent",
                                            "texts": {"title": {"default": "Host"}},
                                        }
                                    },
                                    {
                                        "entity": {
                                            "id": "g1",
                                            "type": "genre",
                                            "texts": {"title": {"default": "Jazz"}},
                                        }
                                    },
                                    # Types with no model of their own are dropped.
                                    {"entity": {"id": "e1", "type": "episode-audio"}},
                                ]
                            }
                        ]
                    }
                },
            )
            results = await client.search_all("news")

        assert {c.id for c in results.channels} == {"c1", "c2"}
        assert {s.id for s in results.shows} == {"s1", "s2"}
        assert [s.title for s in results.artist_stations] == ["Station"]
        assert [t.title for t in results.talent] == ["Host"]
        assert [g.title for g in results.genres] == ["Jazz"]
        assert bool(results) is True
        await client.disconnect()

    async def test_search_all_makes_one_request(self, client: SxmClient) -> None:
        """One call covers every type; the per-type helpers would each re-request."""
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/search/v1/search", payload={"container": {"sets": []}})
            results = await client.search_all("nothing")

        assert bool(results) is False
        assert results.channels == []
        assert results.genres == []
        await client.disconnect()


class TestRetries:
    """Transient failures are retried; definitive ones are not."""

    @pytest.fixture(autouse=True)
    def _no_sleeping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep backoff from actually delaying the suite."""
        self.delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            self.delays.append(delay)

        monkeypatch.setattr("aiosxm.client.asyncio.sleep", fake_sleep)

    async def test_500_is_retried_then_succeeds(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", status=500, body="boom")
            mocked.get(f"{API_BASE}/ping", payload={"ok": True})
            assert await client.request("GET", f"{API_BASE}/ping") == {"ok": True}
        assert len(self.delays) == 1
        await client.disconnect()

    async def test_gives_up_after_max_retries(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            for _ in range(MAX_RETRIES + 1):
                mocked.get(f"{API_BASE}/ping", status=503, body="unavailable")
            with pytest.raises(RequestError) as err:
                await client.request("GET", f"{API_BASE}/ping")
            assert err.value.status == 503
        assert len(self.delays) == MAX_RETRIES
        await client.disconnect()

    async def test_backoff_is_exponential(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            for _ in range(MAX_RETRIES + 1):
                mocked.get(f"{API_BASE}/ping", status=500)
            with pytest.raises(RequestError):
                await client.request("GET", f"{API_BASE}/ping")
        assert self.delays == [RETRY_BACKOFF, RETRY_BACKOFF * 2]
        await client.disconnect()

    async def test_429_is_retried(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", status=429)
            mocked.get(f"{API_BASE}/ping", payload={"ok": True})
            assert await client.request("GET", f"{API_BASE}/ping") == {"ok": True}
        await client.disconnect()

    async def test_timeouts_are_retried(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", exception=TimeoutError())
            mocked.get(f"{API_BASE}/ping", payload={"ok": True})
            assert await client.request("GET", f"{API_BASE}/ping") == {"ok": True}
        assert len(self.delays) == 1
        await client.disconnect()

    async def test_404_is_not_retried(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(f"{API_BASE}/ping", status=404)
            with pytest.raises(RequestError):
                await client.request("GET", f"{API_BASE}/ping")
        assert self.delays == []
        await client.disconnect()


class TestSessionRefresh:
    """The refresh token buys a new access token without a full login."""

    async def test_refresh_avoids_a_full_login(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            # Pretend the refresh cookie is present.
            client._http_client_session.cookie_jar.update_cookies(
                {REFRESH_TOKEN_COOKIE: "refresh-me"}
            )
            mocked.post(
                f"{API_BASE}/session/v1/sessions/refresh",
                payload={
                    "accessToken": "refreshed-token",
                    "accessTokenExpiresAt": "2099-01-01T00:00:00.000000Z",
                },
            )
            assert await client._refresh_session() is True
            assert client._access_token == "refreshed-token"
        await client.disconnect()

    async def test_refresh_without_a_cookie_declines(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            assert await client._refresh_session() is False
        await client.disconnect()

    async def test_failed_refresh_falls_back_to_full_login(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            client._http_client_session.cookie_jar.update_cookies(
                {REFRESH_TOKEN_COOKIE: "stale"}
            )
            mocked.post(f"{API_BASE}/session/v1/sessions/refresh", status=401)
            assert await client._refresh_session() is False
            # A full re-auth must still work afterwards.
            mock_auth(mocked)
            await client._authenticate(force=True)
            assert client._access_token == "access-token"
        await client.disconnect()


class TestConcurrency:
    """The auth lock must collapse a burst of expired-token requests into one login."""

    async def test_concurrent_requests_authenticate_once(self, client: SxmClient) -> None:
        calls = 0
        original = client._authenticate_with_password

        async def counting(token: str) -> dict:
            nonlocal calls
            calls += 1
            return await original(token)

        client._authenticate_with_password = counting

        with aioresponses() as mocked:
            mock_auth(mocked)
            for _ in range(5):
                mocked.get(f"{API_BASE}/ping", payload={"ok": True})
            results = await asyncio.gather(
                *(client.request("GET", f"{API_BASE}/ping") for _ in range(5))
            )

        assert all(r == {"ok": True} for r in results)
        assert calls == 1, "five concurrent requests should trigger one login"
        await client.disconnect()


class TestArtistStationLibrary:
    """Artist stations sit outside the channel catalog."""

    STATION: ClassVar[dict] = {
        "entity": {
            "artistStation": {
                "id": "station-1",
                "texts": {
                    "title": {"default": "Dean Martin"},
                    "description": {"default": "Rat Pack cool"},
                },
                "images": {"tile": {"1x1": {"url": "art/station.jpg"}}},
            }
        }
    }

    async def test_hydrates_a_station(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                f"{API_BASE}/hydration/v2/hydration/item-core/artist-station/station-1",
                payload=self.STATION,
            )
            station = await client.get_artist_station("station-1")
        assert station is not None
        assert station.title == "Dean Martin"
        assert station.description == "Rat Pack cool"
        assert station.image_url() is not None
        await client.close()

    async def test_library_includes_stations(self, client: SxmClient) -> None:
        # The library mixes channels and stations; stations were being dropped.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                f"{API_BASE}/ondemand/v1/library/all",
                payload={
                    "allDataMap": {
                        "c1": {"entityId": "c1", "entityType": "channel-linear"},
                        "s1": {"entityId": "station-1", "entityType": "artist-station"},
                    }
                },
            )
            mocked.get(
                f"{API_BASE}/hydration/v2/hydration/item-core/artist-station/station-1",
                payload=self.STATION,
            )
            stations = await client.get_library_artist_stations()
        assert [s.id for s in stations] == ["station-1"]
        await client.close()

    async def test_library_without_stations(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                f"{API_BASE}/ondemand/v1/library/all",
                payload={"allDataMap": {"c1": {"entityId": "c1", "entityType": "channel-linear"}}},
            )
            assert await client.get_library_artist_stations() == []
        await client.close()


class TestGenreDiscovery:
    """Genre browsing is derived from the catalog, not a separate API call."""

    async def test_genres_are_counted(self, client: SxmClient, channel_item: dict) -> None:
        jazz = {
            **channel_item,
            "entity": {**channel_item["entity"], "id": "j1"},
            "decorations": {**channel_item.get("decorations", {}), "genre": "Jazz"},
        }
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                CHANNELS_RE,
                payload={"container": {"sets": [{"items": [channel_item, jazz]}]}},
            )
            mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            genres = await client.get_genres()
        assert genres.get("Jazz") == 1
        await client.close()

    async def test_channels_by_genre_is_case_insensitive(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        jazz = {
            **channel_item,
            "entity": {**channel_item["entity"], "id": "j1"},
            "decorations": {**channel_item.get("decorations", {}), "genre": "Jazz"},
        }
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            for _ in range(2):
                mocked.get(
                    CHANNELS_RE,
                    payload={"container": {"sets": [{"items": [channel_item, jazz]}]}},
                )
                mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})
            assert [c.id for c in await client.get_channels_by_genre("JAZZ")] == ["j1"]
        await client.close()


class TestSportsBroadcasts:
    """Games are containers; the channel carrying them is what you tune."""

    TEAM_PAGE: ClassVar[dict] = {
        "page": {
            "containers": [
                {"id": "c1", "url": "relationship/v1/container/live?entityId=t1"},
                {
                    "id": "c2",
                    "url": "relationship/v1/container/live-upcoming-events?entityId=t1",
                },
                {"id": "c3", "url": "relationship/v1/container/aod?entityId=t1"},
            ]
        }
    }

    LIVE: ClassVar[dict] = {
        "container": {
            "sets": [
                {
                    "items": [
                        {
                            "entity": {
                                "id": "ep1",
                                "type": "episode-linear",
                                "texts": {"title": {"default": "Hometown Play-by-Play"}},
                            },
                            "decorations": {"airDate": "2026-08-10T04:00:00Z"},
                            "actions": {
                                "play": [{"entity": {"type": "channel-linear", "id": "ch1"}}]
                            },
                        }
                    ]
                }
            ]
        }
    }

    UPCOMING: ClassVar[dict] = {
        "container": {
            "sets": [
                {
                    "items": [
                        {
                            "entity": {
                                "id": "ev1",
                                "type": "event",
                                "texts": {"title": {"default": "Bears @ Bengals"}},
                            },
                            "decorations": {
                                "airDate": "2026-08-22T23:00:00Z",
                                "leagueName": "NFL",
                            },
                            "actions": {
                                "pickFeed": [
                                    {
                                        "data": {
                                            "set": {
                                                "items": [
                                                    {
                                                        "entity": {
                                                            "id": "f1",
                                                            "type": "episode-linear",
                                                        },
                                                        "decorations": {
                                                            "airingCoverage": "AWAY"
                                                        },
                                                        "actions": {
                                                            "play": [
                                                                {
                                                                    "entity": {
                                                                        "type": "channel-linear",
                                                                        "id": "ch-away",
                                                                    }
                                                                }
                                                            ]
                                                        },
                                                    },
                                                    {
                                                        "entity": {
                                                            "id": "f2",
                                                            "type": "episode-linear",
                                                        },
                                                        "decorations": {
                                                            "airingCoverage": "HOME"
                                                        },
                                                        "actions": {
                                                            "play": [
                                                                {
                                                                    "entity": {
                                                                        "type": "channel-linear",
                                                                        "id": "ch-home",
                                                                    }
                                                                }
                                                            ]
                                                        },
                                                    },
                                                ]
                                            }
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            ]
        }
    }

    def _mock_team(self, mocked) -> None:
        mocked.get(re.compile(r".*/page/v1/page/team/.*"), payload=self.TEAM_PAGE)
        mocked.get(re.compile(r".*container/live\?.*"), payload=self.LIVE)
        mocked.get(re.compile(r".*container/live-upcoming-events\?.*"), payload=self.UPCOMING)

    async def test_live_and_upcoming_are_returned(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._mock_team(mocked)
            broadcasts = await client.get_team_broadcasts("t1")

        assert [b.is_live for b in broadcasts] == [True, False, False]
        assert broadcasts[0].title == "Hometown Play-by-Play"
        await client.close()

    async def test_a_game_expands_into_its_feeds(self, client: SxmClient) -> None:
        # One scheduled game, two commentary feeds — each separately playable.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._mock_team(mocked)
            broadcasts = await client.get_team_broadcasts("t1")

        feeds = [b for b in broadcasts if not b.is_live]
        assert [f.coverage for f in feeds] == ["AWAY", "HOME"]
        assert [f.play_entity_id for f in feeds] == ["ch-away", "ch-home"]
        # Feed entries carry no title of their own; the game's name is used.
        assert all(f.title == "Bears @ Bengals" for f in feeds)
        await client.close()

    async def test_broadcasts_point_at_a_channel(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._mock_team(mocked)
            broadcasts = await client.get_team_broadcasts("t1")

        assert all(b.is_playable for b in broadcasts)
        assert broadcasts[0].play_entity_type == "channel-linear"
        await client.close()

    async def test_unplayable_broadcast_raises(self, client: SxmClient) -> None:
        from aiosxm import Broadcast

        with pytest.raises(ValueError, match="no playable feed"):
            await client.get_broadcast_stream(Broadcast(id="x", title="No feed"))
        await client.close()

    async def test_missing_containers_are_skipped(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                re.compile(r".*/page/v1/page/team/.*"), payload={"page": {"containers": []}}
            )
            assert await client.get_team_broadcasts("t1") == []
        await client.close()


class TestContainerBrowsing:
    """Every container entity exposes the same page-of-containers shape."""

    PAGE: ClassVar[dict] = {
        "page": {
            "containers": [
                {"url": "relationship/v1/container/aod?entityId=x"},
                {"url": "relationship/v1/container/epg?entityId=x"},
                {"id": "inline-only"},  # no url: an inline container
            ]
        }
    }

    async def test_containers_are_named(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            containers = await client.get_containers("talent", "t1")
        assert sorted(containers) == ["aod", "epg"]
        await client.close()

    async def test_inline_containers_are_skipped(self, client: SxmClient) -> None:
        # A container with no URL can't be followed, so it must not appear.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            containers = await client.get_containers("talent", "t1")
        assert "inline-only" not in containers
        await client.close()

    async def test_browse_entity_returns_children(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            mocked.get(
                re.compile(r".*container/aod\?.*"),
                payload={
                    "container": {
                        "sets": [{"items": [{"entity": {"id": "e1", "type": "episode-audio"}}]}]
                    }
                },
            )
            items = await client.browse_entity("talent", "t1", "aod")
        assert [i["entity"]["id"] for i in items] == ["e1"]
        await client.close()

    async def test_unknown_container_raises_key_error(self, client: SxmClient) -> None:
        # Distinguishes "no such section" from "section is empty".
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), payload=self.PAGE)
            with pytest.raises(KeyError, match="has no container"):
                await client.browse_entity("talent", "t1", "nope")
        await client.close()

    async def test_entity_without_containers(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.get(
                re.compile(r".*/page/v1/page/talent/.*"), payload={"page": {"containers": []}}
            )
            assert await client.get_containers("talent", "t1") == {}
        await client.close()


class TestArtistStationSearch:
    """Artist stations have no catalog; they're found by name."""

    STATION_ENTITY: ClassVar[dict] = {
        "id": "as1",
        "type": "artist-station",
        "texts": {"title": {"default": "Frank Sinatra"}},
        # Search returns the deeper channel-style image nesting.
        "images": {"tile": {"aspect_1x1": {"preferred": {"url": "art/a.jpg"}}}},
    }

    async def test_search_finds_stations(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {"sets": [{"items": [{"entity": self.STATION_ENTITY}]}]}
                },
            )
            stations = await client.search_artist_stations("sinatra")
        assert [s.title for s in stations] == ["Frank Sinatra"]
        # The deeper nesting must still yield artwork.
        assert stations[0].image_url() is not None
        await client.close()

    async def test_talent_page_adds_variants(self, client: SxmClient) -> None:
        # An artist's page lists seasonal variants that search alone misses.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {"entity": self.STATION_ENTITY},
                                    {"entity": {"id": "t1", "type": "talent"}},
                                ]
                            }
                        ]
                    }
                },
            )
            mocked.get(
                re.compile(r".*/page/v1/page/talent/.*"),
                payload={
                    "page": {
                        "containers": [
                            {"url": "relationship/v1/container/pandora-artist-radio?entityId=t1"}
                        ]
                    }
                },
            )
            mocked.get(
                re.compile(r".*container/pandora-artist-radio\?.*"),
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {
                                        "entity": {
                                            "id": "as2",
                                            "type": "artist-station",
                                            "texts": {
                                                "title": {"default": "Frank Sinatra (Holiday)"}
                                            },
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            stations = await client.search_artist_stations("sinatra")
        assert {s.id for s in stations} == {"as1", "as2"}
        await client.close()

    async def test_talent_failure_is_survivable(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(
                f"{API_BASE}/search/v1/search",
                payload={
                    "container": {
                        "sets": [
                            {
                                "items": [
                                    {"entity": self.STATION_ENTITY},
                                    {"entity": {"id": "t1", "type": "talent"}},
                                ]
                            }
                        ]
                    }
                },
            )
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), status=500)
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), status=500)
            mocked.get(re.compile(r".*/page/v1/page/talent/.*"), status=500)
            stations = await client.search_artist_stations("sinatra")
        # The search hit still comes back even though the talent page failed.
        assert [s.id for s in stations] == ["as1"]
        await client.close()


def _last_library_call(mocked) -> dict:
    """The JSON body of the most recent library/update POST."""
    for (method, url), calls in mocked.requests.items():
        if method == "POST" and "library/update" in str(url):
            return calls[-1].kwargs["json"]
    message = "no library/update call was made"
    raise AssertionError(message)


class TestLibraryMutation:
    """Adding and removing library items writes to the real account."""

    async def test_add_sends_the_expected_body(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/ondemand/v1/library/update", payload={})
            await client.add_to_library("channel-linear", "c1")

            sent = _last_library_call(mocked)
            assert sent == {
                "method": "ADD",
                "entityId": "c1",
                "entityType": "channel-linear",
            }
        await client.close()

    async def test_remove_uses_delete_not_remove(self, client: SxmClient) -> None:
        # The API rejects "REMOVE" as an illegal enum value.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/ondemand/v1/library/update", payload={})
            await client.remove_from_library("channel-linear", "c1")

            assert _last_library_call(mocked)["method"] == "DELETE"
        await client.close()

    async def test_failure_propagates(self, client: SxmClient) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            mocked.post(f"{API_BASE}/ondemand/v1/library/update", status=400, body="bad")
            with pytest.raises(RequestError):
                await client.add_to_library("channel-linear", "nope")
        await client.close()


class TestChannelCache:
    """Walking the catalog is expensive, so the client caches it."""

    def _pages(self, mocked, channel_item: dict, entity_id: str = "c1") -> None:
        item = {**channel_item, "entity": {**channel_item["entity"], "id": entity_id}}
        mocked.get(CHANNELS_RE, payload={"container": {"sets": [{"items": [item]}]}})
        mocked.get(CHANNELS_RE, payload={"container": {"sets": []}})

    async def test_second_call_is_served_from_cache(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._pages(mocked, channel_item)
            first = await client.get_channels()
            # No further upstream pages are registered; a re-walk would fail.
            second = await client.get_channels()
        assert [c.id for c in first] == [c.id for c in second]
        await client.close()

    async def test_refresh_forces_a_walk(self, client: SxmClient, channel_item: dict) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._pages(mocked, channel_item, "old")
            assert [c.id for c in await client.get_channels()] == ["old"]
            self._pages(mocked, channel_item, "new")
            assert [c.id for c in await client.get_channels(refresh=True)] == ["new"]
        await client.close()

    async def test_expired_cache_is_rebuilt(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._pages(mocked, channel_item, "old")
            await client.get_channels()
            client._channels_fetched_at = time.monotonic() - (CHANNEL_CACHE_TTL + 1)
            self._pages(mocked, channel_item, "new")
            assert [c.id for c in await client.get_channels()] == ["new"]
        await client.close()

    async def test_concurrent_callers_walk_once(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        # Five simultaneous callers must not each start their own walk.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._pages(mocked, channel_item)
            results = await asyncio.gather(*(client.get_channels() for _ in range(5)))
        assert all([c.id for c in r] == ["c1"] for r in results)
        await client.close()

    async def test_callers_get_their_own_list(
        self, client: SxmClient, channel_item: dict
    ) -> None:
        # A caller mutating the returned list must not corrupt the cache.
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._pages(mocked, channel_item)
            first = await client.get_channels()
            first.clear()
            assert await client.get_channels(), "cache was emptied by a caller"
        await client.close()

    async def test_fetch_stamp_is_exposed(self, client: SxmClient, channel_item: dict) -> None:
        assert client.channels_fetched_at == 0.0
        with aioresponses() as mocked:
            mock_auth(mocked)
            await client.connect()
            self._pages(mocked, channel_item)
            await client.get_channels()
        assert client.channels_fetched_at > 0
        await client.close()
