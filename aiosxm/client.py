"""A client for interacting with SiriusXM."""

import asyncio
import logging
import os
import time
import types
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError, ClientResponseError

from aiosxm.const import (
    ALL_CHANNELS_CONTAINER_ID,
    ALL_CHANNELS_ENTITY_ID,
    API_BASE,
    CHANNEL_CACHE_TTL,
    CHANNEL_PAGE_SIZE,
    DEFAULT_REQUEST_TIMEOUT,
    LOOKAROUND_URL,
    MAX_CHANNEL_OFFSET,
    REFRESH_TOKEN_COOKIE,
    SXM_DEVICE_PAYLOAD,
    SXM_REQUEST_HEADERS,
    TOKEN_EXPIRY_MARGIN,
)
from aiosxm.exceptions import AuthenticationError, NotEntitledError, RequestError
from aiosxm.models import (
    ArtistStation,
    Broadcast,
    Channel,
    Episode,
    Genre,
    NowPlaying,
    SearchResults,
    Show,
    Talent,
)

if TYPE_CHECKING:
    from aiosxm.stream import SxmStream

_LOGGER = logging.getLogger(__name__)

HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_BAD_REQUEST = 400
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500

# Transient failures are retried twice, so the worst case adds ~1.5s before the
# error surfaces. Live audio is better served by failing fast than by stalling.
MAX_RETRIES = 2
RETRY_BACKOFF = 0.5


def _is_transient(err: RequestError) -> bool:
    """Whether a failed request is worth retrying.

    Network errors and 5xx/429 responses may succeed on a second attempt. Other
    4xx responses are answers, not blips: a 403 means "not entitled" no matter
    how many times it is asked.
    """
    if err.status is None:
        return True  # timeout or connection error
    return err.status >= HTTP_SERVER_ERROR or err.status == HTTP_TOO_MANY_REQUESTS


class SxmClient:
    """A client for interacting with SiriusXM."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        session: ClientSession | None = None,
    ) -> None:
        """Initialize the object."""
        username = username or os.getenv("SXM_USERNAME")
        password = password or os.getenv("SXM_PASSWORD")
        if not username or not password:
            message = (
                "A username and password are required, either as arguments or via "
                "the SXM_USERNAME and SXM_PASSWORD environment variables."
            )
            raise ValueError(message)
        self._username: str = username
        self._password: str = password

        self._http_client_session: ClientSession | None = session
        self._http_client_session_internal: bool = False

        self._device_session: dict | None = None
        self._anonymous_session: dict | None = None
        self._authentication_response: dict | None = None
        self._authenticated_session: dict | None = None

        self._access_token: str | None = None
        self._access_token_expiration: datetime | None = None
        # Serializes re-authentication so a burst of concurrent requests that all
        # notice an expired token only triggers one login.
        self._auth_lock = asyncio.Lock()

        self._streams: dict[tuple[str, str], SxmStream] = {}

        # Walking the catalog is expensive, so it's cached with a TTL.
        self._channels_cache: list[Channel] | None = None
        self._channels_fetched_at: float = 0.0
        self._channels_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        """Enter the runtime context related to this object."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: types.TracebackType | None,
    ) -> None:
        """Exit the runtime context related to this object."""
        await self.disconnect()

    async def connect(self) -> None:
        """Connect to SiriusXM."""
        await self._authenticate()

    async def disconnect(self) -> None:
        """Disconnect from SiriusXM."""
        if self._http_client_session_internal and self._http_client_session:
            await self._http_client_session.close()
            self._http_client_session = None
            self._http_client_session_internal = False

    async def close(self) -> None:
        """Close the client. Alias of :meth:`disconnect`, matching aiohttp."""
        await self.disconnect()

    @property
    def channels_fetched_at(self) -> float:
        """When the channel catalog was last walked (monotonic clock, 0 if never).

        Lets a caller memoise anything derived from the catalog and know when to
        rebuild it.
        """
        return self._channels_fetched_at

    @property
    def entitlement_hash(self) -> str | None:
        """The entitlement hash for the current session, if authenticated."""
        if not self._authenticated_session:
            return None
        return self._authenticated_session.get("entitlementHash")

    def _get_http_client_session(self) -> ClientSession:
        """Get the HTTP client session."""
        if not self._http_client_session or self._http_client_session.closed:
            self._http_client_session = ClientSession(
                timeout=ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT),
            )
            self._http_client_session_internal = True
        return self._http_client_session

    def _token_is_stale(self) -> bool:
        """Whether the access token is missing, expired, or about to expire."""
        if not self._access_token:
            return True
        if not self._access_token_expiration:
            return False
        margin = timedelta(seconds=TOKEN_EXPIRY_MARGIN)
        return datetime.now(tz=UTC) >= (self._access_token_expiration - margin)

    async def request(
        self,
        method: str,
        url: str,
        *,
        authenticate: bool = True,
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Make a request against the API.

        Refreshes the access token when it is close to expiring, re-authenticates
        once if the API rejects the token anyway, and retries transient server
        errors and timeouts with exponential backoff.
        """
        if authenticate and self._token_is_stale():
            await self._authenticate()

        for attempt in range(MAX_RETRIES + 1):
            try:
                return await self._request(method, url, **kwargs)
            except RequestError as err:
                if authenticate and err.status == HTTP_UNAUTHORIZED:
                    # The token was rejected even though we thought it was valid;
                    # the session may have been revoked server-side.
                    _LOGGER.info("Access token rejected; re-authenticating and retrying.")
                    await self._authenticate(force=True)
                    return await self._request(method, url, **kwargs)

                if not _is_transient(err) or attempt == MAX_RETRIES:
                    raise

                delay = RETRY_BACKOFF * (2**attempt)
                _LOGGER.debug(
                    "Request to %s failed (%s); retrying in %.1fs",
                    url,
                    err.status or "network error",
                    delay,
                )
                await asyncio.sleep(delay)
        # Unreachable: the loop either returns or raises.
        raise AssertionError

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """Make a single request against the API."""
        # Copy the shared header constant; writing the Authorization header into
        # it directly would leak the token into every later request.
        headers = {**SXM_REQUEST_HEADERS, **(kwargs.pop("headers", None) or {})}
        if self._access_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self._access_token}"

        session = self._get_http_client_session()
        try:
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                body = await resp.read()
                if resp.status >= HTTP_BAD_REQUEST:
                    raise RequestError(url, status=resp.status, body=body[:500].decode(errors="replace"))
                if resp.content_type == "application/json":
                    return await resp.json()
                if resp.content_type in (
                    "text/html",
                    "text/plain",
                    "application/x-mpegurl",
                    "application/vnd.apple.mpegurl",
                ):
                    return body.decode(errors="replace")
                return body
        except ClientResponseError as err:
            raise RequestError(url, status=err.status, original_exception=err) from err
        except (TimeoutError, ClientError) as err:
            raise RequestError(url, original_exception=err) from err

    async def _create_device_session(self) -> dict:
        """Create a SiriusXM device session."""
        self._device_session = await self.request(
            method="POST",
            url=f"{API_BASE}/device/v1/devices",
            json=SXM_DEVICE_PAYLOAD,
            authenticate=False,
        )
        return self._device_session

    async def _create_anonymous_session(self, device_grant: str) -> dict:
        """Create an anonymous SiriusXM user session."""
        self._anonymous_session = await self.request(
            method="POST",
            url=f"{API_BASE}/session/v1/sessions/anonymous",
            headers={"Authorization": f"Bearer {device_grant}"},
            json=True,
            authenticate=False,
        )
        return self._anonymous_session

    async def _get_identity_status(self, username: str, anonymous_access_token: str) -> dict:
        """Get the status of an identity."""
        query = urlencode({"handle": username})
        return await self.request(
            method="GET",
            url=f"{API_BASE}/identity/v1/identities/status?{query}",
            headers={"Authorization": f"Bearer {anonymous_access_token}"},
            authenticate=False,
        )

    async def _authenticate_with_password(self, anonymous_access_token: str) -> dict:
        """Authenticate using a password."""
        self._authentication_response = await self.request(
            method="POST",
            url=f"{API_BASE}/identity/v1/identities/authenticate/password",
            headers={"Authorization": f"Bearer {anonymous_access_token}"},
            json={"handle": self._username, "password": self._password},
            authenticate=False,
        )
        return self._authentication_response

    async def _create_authenticated_session(self, authentication_grant: str) -> dict:
        """Create an authenticated SiriusXM user session."""
        self._authenticated_session = await self.request(
            method="POST",
            url=f"{API_BASE}/session/v1/sessions/authenticated",
            headers={"Authorization": f"Bearer {authentication_grant}"},
            json=True,
            authenticate=False,
        )
        return self._authenticated_session

    @property
    def _refresh_token(self) -> str | None:
        """The refresh token, which the API returns as a cookie."""
        if not self._http_client_session:
            return None
        return next(
            (
                cookie.value
                for cookie in self._http_client_session.cookie_jar
                if cookie.key == REFRESH_TOKEN_COOKIE
            ),
            None,
        )

    async def _refresh_session(self) -> bool:
        """Exchange the refresh token for a new access token.

        Cheaper than the full five-step login, and the refresh token is valid for
        ~90 days. Returns False if there is nothing to refresh with or the API
        turns it down, in which case the caller falls back to a full login.
        """
        token = self._refresh_token
        if not token:
            return False
        try:
            session = await self.request(
                method="POST",
                url=f"{API_BASE}/session/v1/sessions/refresh",
                headers={"Authorization": f"Bearer {token}"},
                authenticate=False,
            )
            self._access_token = session["accessToken"]
        except (RequestError, KeyError, TypeError) as err:
            _LOGGER.debug("Session refresh failed (%s); falling back to full login", err)
            return False

        self._authenticated_session = session
        expires_at = session.get("accessTokenExpiresAt")
        self._access_token_expiration = (
            datetime.fromisoformat(expires_at) if expires_at else None
        )
        _LOGGER.debug("Refreshed access token without a full login")
        return True

    async def _authenticate(self, *, force: bool = False) -> None:
        """Authenticate against the API."""
        async with self._auth_lock:
            # Another coroutine may have refreshed the token while we waited.
            if not force and not self._token_is_stale():
                return

            # A refresh is three fewer requests than logging in again.
            if self._access_token and await self._refresh_session():
                return

            self._access_token = None
            self._access_token_expiration = None
            try:
                device = await self._create_device_session()
                anonymous = await self._create_anonymous_session(device["grant"])
                anonymous_token = anonymous["accessToken"]

                identity_status = await self._get_identity_status(self._username, anonymous_token)
                if not identity_status.get("hasPassword"):
                    message = f"User {self._username} does not have a password set."
                    raise AuthenticationError(message)

                authentication = await self._authenticate_with_password(anonymous_token)
                session = await self._create_authenticated_session(authentication["grant"])

                self._access_token = session["accessToken"]
                expires_at = session.get("accessTokenExpiresAt")
                self._access_token_expiration = (
                    datetime.fromisoformat(expires_at) if expires_at else None
                )
            except RequestError as err:
                message = f"An error occurred during authentication of user {self._username}"
                raise AuthenticationError(message, original_exception=err) from err
            except (KeyError, TypeError) as err:
                message = "The SiriusXM authentication API returned an unexpected response"
                raise AuthenticationError(message, original_exception=err) from err

    async def get_entitlements(self) -> list[dict]:
        """Get the entitlements for the account.

        An account whose subscription has lapsed authenticates fine but has all of
        its entitlements in the "revoked" state, and playback will fail.
        """
        resp = await self.request(method="GET", url=f"{API_BASE}/entitlement/v1/entitlements")
        return resp.get("entitlements", [])

    async def is_entitled(self) -> bool:
        """Whether the account currently has an active entitlement."""
        return any(e.get("state") != "revoked" for e in await self.get_entitlements())

    async def discover_all_channels_ids(self) -> tuple[str, str]:
        """Resolve the entity/container ids used by the all-channels listing.

        The defaults in const.py are what the web player currently uses. This
        re-reads them from the page descriptor, which is where the player gets
        them, so the library can be repaired without a code change if they move.
        """
        resp = await self.request(
            method="GET",
            url=f"{API_BASE}/page/v1/page/curated-grouping/{ALL_CHANNELS_ENTITY_ID}",
        )
        for container in resp.get("page", {}).get("containers", []):
            url = container.get("url", "")
            if "container/all-channels" in url and container.get("id"):
                return ALL_CHANNELS_ENTITY_ID, container["id"]
        return ALL_CHANNELS_ENTITY_ID, ALL_CHANNELS_CONTAINER_ID

    def _channels_url(self, offset: int, size: int, entity_id: str, container_id: str) -> str:
        """Build the all-channels container URL.

        These query parameters mirror the URL the web player is handed by the page
        descriptor. Dropping any of containerId/maxResponses/setResponseStructure
        makes the API return an empty set instead of an error.
        """
        params = [
            ("entityId", entity_id),
            ("entityType", "curated-grouping"),
            ("containerId", container_id),
            ("maxResponses", str(size)),
            ("offset", str(offset)),
            ("locale", "en-US"),
            ("useCuratedContext", "false"),
            ("supportedMediaType", "AUDIO"),
            ("supportedMediaType", "VIDEO"),
            ("setStyle", "small_list"),
            ("unentitledContent", "lock"),
            ("preferredImageVariant", "default"),
            ("setResponseStructure", "default"),
        ]
        return f"{API_BASE}/relationship/v1/container/all-channels?{urlencode(params)}"

    async def get_channels(
        self,
        *,
        page_size: int = CHANNEL_PAGE_SIZE,
        refresh: bool = False,
    ) -> list[Channel]:
        """Get the full linear and on-demand channel list.

        Walks the container's pagination until every channel has been collected.
        That is ~24 sequential requests, so the result is cached for
        `CHANNEL_CACHE_TTL`; pass `refresh=True` to rebuild it. The lock keeps a
        burst of concurrent callers from each starting their own walk.
        """
        async with self._channels_lock:
            if not refresh and self._channels_cache is not None:
                age = time.monotonic() - self._channels_fetched_at
                if age < CHANNEL_CACHE_TTL:
                    return list(self._channels_cache)

            channels = await self._walk_channels(page_size)
            self._channels_cache = channels
            self._channels_fetched_at = time.monotonic()
            return list(channels)

    async def _walk_channels(self, page_size: int) -> list[Channel]:
        """Page through the all-channels container until it runs dry."""
        entity_id, container_id = ALL_CHANNELS_ENTITY_ID, ALL_CHANNELS_CONTAINER_ID
        channels: list[Channel] = []
        seen: set[str] = set()
        offset = 0

        # The API caps a page at 30 items no matter what `maxResponses` asks for,
        # and `pagination.hits` echoes the requested size rather than the real
        # total, so neither can be trusted to decide when to stop. Walk until a
        # page comes back empty (or stops producing anything new).
        while offset < MAX_CHANNEL_OFFSET:
            resp = await self.request(
                method="GET",
                url=self._channels_url(offset, page_size, entity_id, container_id),
            )
            sets = resp.get("container", {}).get("sets", [])
            items = [item for s in sets for item in s.get("items", [])]
            if not items:
                break

            new = 0
            for item in items:
                channel = Channel.from_item(item)
                if channel and channel.id not in seen:
                    seen.add(channel.id)
                    channels.append(channel)
                    new += 1

            offset += len(items)
            if new == 0:
                # The server is repeating a page; stop rather than loop forever.
                break

        return channels

    async def get_library(self) -> list[dict]:
        """Get the raw library entries for the account.

        Entries are reference stubs of the form
        ``{"entityId": ..., "entityType": ..., "createdTime": ...}``, not full
        entities. Use :meth:`get_library_channels` to resolve them to channels.
        """
        resp = await self.request(method="GET", url=f"{API_BASE}/ondemand/v1/library/all")
        return list(resp.get("allDataMap", {}).values())

    async def add_to_library(self, entity_type: str, entity_id: str) -> None:
        """Add an item to the account's library.

        This writes to the real account — the change shows up in the SiriusXM
        app too. Adding something already saved is accepted and does nothing.
        """
        await self._update_library("ADD", entity_type, entity_id)

    async def remove_from_library(self, entity_type: str, entity_id: str) -> None:
        """Remove an item from the account's library."""
        await self._update_library("DELETE", entity_type, entity_id)

    async def _update_library(self, method: str, entity_type: str, entity_id: str) -> None:
        """Add or remove one library entry.

        The API takes a flat body and spells removal `DELETE`, not `REMOVE`,
        which it rejects as an illegal enum value.
        """
        await self.request(
            method="POST",
            url=f"{API_BASE}/ondemand/v1/library/update",
            json={"method": method, "entityId": entity_id, "entityType": entity_type},
        )
        _LOGGER.debug("Library %s: %s/%s", method, entity_type, entity_id)

    async def get_library_channels(self) -> list[Channel]:
        """Get the channels in the account's library, resolved against the catalog.

        Library entries that no longer exist in the catalog are skipped.
        """
        library = await self.get_library()
        wanted = {
            entry["entityId"]
            for entry in library
            if entry.get("entityId") and str(entry.get("entityType", "")).startswith("channel")
        }
        if not wanted:
            return []
        return [c for c in await self.get_channels() if c.id in wanted]

    async def search(self, query: str) -> dict[str, list[dict]]:
        """Search the catalog, grouped by entity type.

        This is the only way to reach on-demand content: podcasts and episodes
        are not in the all-channels listing, so their ids have to be discovered
        here before :meth:`get_podcast_episodes` can be used.
        """
        resp = await self.request(
            method="POST",
            url=f"{API_BASE}/search/v1/search",
            json={"searchString": query},
        )
        results: dict[str, list[dict]] = {}
        for result_set in resp.get("container", {}).get("sets", []):
            for item in result_set.get("items", []):
                entity = item.get("entity")
                if not entity or not entity.get("type"):
                    continue
                results.setdefault(entity["type"], []).append(entity)
        return results

    async def search_channels(self, query: str) -> list[Channel]:
        """Search for live channels."""
        results = await self.search(query)
        channels: list[Channel] = []
        for entity_type, entities in results.items():
            if not entity_type.startswith("channel"):
                continue
            channels.extend(
                channel
                for entity in entities
                if (channel := Channel.from_item({"entity": entity})) is not None
            )
        return channels

    async def get_genres(self) -> dict[str, int]:
        """Get the genres in the catalog, with how many channels each holds.

        Derived from the cached channel list rather than a separate call: the
        API's own `filterId` parameter is accepted but ignored server-side, so
        filtering has to happen here anyway.
        """
        genres: dict[str, int] = {}
        for channel in await self.get_channels():
            if channel.genre:
                genres[channel.genre] = genres.get(channel.genre, 0) + 1
        return dict(sorted(genres.items(), key=lambda kv: (-kv[1], kv[0])))

    async def get_channels_by_genre(self, genre: str) -> list[Channel]:
        """Get every channel in a genre, matched case-insensitively."""
        wanted = genre.casefold()
        return [c for c in await self.get_channels() if (c.genre or "").casefold() == wanted]

    async def get_shows(self, query: str) -> list[dict]:
        """Find shows and podcasts matching a query.

        Shows are containers rather than playable entities; use
        :meth:`get_podcast_episodes` with a show's id to reach its episodes.
        """
        results = await self.search(query)
        return results.get("show-podcast", []) + results.get("show", [])

    async def get_containers(self, entity_type: str, entity_id: str) -> dict[str, str]:
        """List the browsable containers on an entity's page, keyed by name.

        Every container entity — a team, a show, a talent, a genre, a league —
        has a page listing named containers (`aod`, `epg`, `live`, `vod`, ...),
        each a URL returning child entities. This is the general shape behind
        the whole catalog; :meth:`get_container` follows one.
        """
        page = await self.request(
            method="GET",
            url=f"{API_BASE}/page/v1/page/{entity_type}/{entity_id}",
        )
        containers: dict[str, str] = {}
        for container in page.get("page", {}).get("containers", []):
            url = container.get("url") or ""
            if "container/" not in url:
                continue  # an inline container, with no URL to follow
            name = url.split("container/")[-1].split("?")[0]
            containers.setdefault(name, url)
        return containers

    async def get_container(self, url: str) -> list[dict]:
        """Follow a container URL and return the entities it holds."""
        resp = await self.request(method="GET", url=f"{API_BASE}/{url.lstrip('/')}")
        return [
            item
            for result_set in resp.get("container", {}).get("sets", [])
            for item in result_set.get("items", [])
        ]

    async def browse_entity(self, entity_type: str, entity_id: str, container: str) -> list[dict]:
        """Fetch one named container from an entity's page.

        Raises :class:`KeyError` if the entity has no such container, so a
        caller can distinguish "no such section" from "section is empty".
        """
        containers = await self.get_containers(entity_type, entity_id)
        if container not in containers:
            message = (
                f"{entity_type}/{entity_id} has no container {container!r}; "
                f"available: {sorted(containers)}"
            )
            raise KeyError(message)
        return await self.get_container(containers[container])

    async def get_teams(self, query: str) -> list[dict]:
        """Find sports teams matching a query."""
        return (await self.search(query)).get("team", [])

    async def get_team_broadcasts(self, team_id: str) -> list[Broadcast]:
        """Get a team's game broadcasts, live first then upcoming.

        A team page lists games in two containers: `live` for what is on air now
        and `live-upcoming-events` for what is scheduled. Neither the game nor
        the event is playable itself — each names the channel carrying it, which
        is what :meth:`get_stream` needs.
        """
        containers = await self.get_containers("team", team_id)

        broadcasts: list[Broadcast] = []
        for name, live in (("live", True), ("live-upcoming-events", False)):
            url = containers.get(name)
            if not url:
                continue
            try:
                items = await self.get_container(url)
            except RequestError:
                _LOGGER.debug("Could not load %s for team %s", name, team_id)
                continue
            for item in items:
                broadcasts.extend(self._broadcasts_from_item(item, is_live=live))
        return broadcasts

    @staticmethod
    def _broadcasts_from_item(item: dict, *, is_live: bool) -> list[Broadcast]:
        """Expand one container item into broadcasts.

        A scheduled game offers a choice of feeds (home and away commentary)
        under `pickFeed`; each becomes its own broadcast so a caller can pick.
        """
        feeds = (item.get("actions") or {}).get("pickFeed") or []
        feed_items = (feeds[0].get("data", {}).get("set", {}) or {}).get("items", []) if feeds else []
        if feed_items:
            parent = Broadcast.from_item(item, is_live=is_live)
            expanded = []
            for feed in feed_items:
                broadcast = Broadcast.from_item(feed, is_live=is_live)
                if broadcast is None:
                    continue
                # Feed entries carry the coverage flag but not the game's name.
                if parent and not broadcast.title:
                    broadcast.title = parent.title
                expanded.append(broadcast)
            if expanded:
                return expanded
        broadcast = Broadcast.from_item(item, is_live=is_live)
        return [broadcast] if broadcast else []

    async def get_broadcast_stream(self, broadcast: Broadcast) -> "SxmStream":
        """Tune the channel carrying a broadcast."""
        entity_type, entity_id = broadcast.play_entity_type, broadcast.play_entity_id
        if not entity_type or not entity_id:
            message = f"Broadcast {broadcast.title!r} has no playable feed"
            raise ValueError(message)
        return await self.get_stream(entity_type, entity_id)

    async def hydrate(self, entity_type: str, entity_id: str) -> dict | None:
        """Fetch an entity's details by id, whatever its type.

        The response wraps the entity under a camel-cased key (`showPodcast`,
        `team`, ...), which this unwraps so callers get the entity itself.
        """
        resp = await self.request(
            method="GET",
            url=f"{API_BASE}/hydration/v2/hydration/item-core/{entity_type}/{entity_id}",
        )
        entity = resp.get("entity") or {}
        if not entity:
            return None
        inner = next(iter(entity.values()), None)
        if not isinstance(inner, dict) or not inner.get("id"):
            return None
        return {**inner, "type": inner.get("type") or entity_type}

    async def get_artist_station(self, station_id: str) -> ArtistStation | None:
        """Get an artist station by id.

        Artist stations are personalised track queues rather than broadcast
        channels, so they are not in the all-channels catalog and have to be
        hydrated individually.
        """
        resp = await self.request(
            method="GET",
            url=f"{API_BASE}/hydration/v2/hydration/item-core/artist-station/{station_id}",
        )
        return ArtistStation.from_entity(resp.get("entity", {}))

    async def search_artist_stations(self, query: str) -> list[ArtistStation]:
        """Find artist stations for an artist.

        There is no catalog of artist stations to list — they are only reachable
        by name. Search returns the obvious match; the artist's own page also
        lists variants (seasonal ones, for instance) that search alone misses,
        so both are merged here.
        """
        results = await self.search(query)
        stations: dict[str, ArtistStation] = {}

        for entity in results.get("artist-station", []):
            if station := ArtistStation.from_entity({"artistStation": entity}):
                stations[station.id] = station

        for talent in results.get("talent", [])[:1]:
            try:
                containers = await self.get_containers("talent", talent["id"])
                url = containers.get("pandora-artist-radio")
                if not url:
                    continue
                for item in await self.get_container(url):
                    entity = item.get("entity") or {}
                    if entity.get("type") != "artist-station":
                        continue
                    if station := ArtistStation.from_entity({"artistStation": entity}):
                        stations.setdefault(station.id, station)
            except (RequestError, KeyError):
                _LOGGER.debug("Could not read artist stations for %s", query)

        return list(stations.values())

    async def get_library_artist_stations(self) -> list[ArtistStation]:
        """Get the artist stations saved to the account's library."""
        library = await self.get_library()
        station_ids = [
            entry["entityId"]
            for entry in library
            if entry.get("entityType") == "artist-station" and entry.get("entityId")
        ]
        stations = await asyncio.gather(
            *(self.get_artist_station(station_id) for station_id in station_ids),
            return_exceptions=True,
        )
        return [s for s in stations if isinstance(s, ArtistStation)]

    async def search_shows(self, query: str) -> list[Show]:
        """Find shows and podcasts matching a query, as models."""
        results = await self.search(query)
        entities = results.get("show-podcast", []) + results.get("show", [])
        return [s for e in entities if (s := Show.from_item({"entity": e}))]

    async def search_all(self, query: str) -> SearchResults:
        """Search once and return every result type as models.

        The API has no way to ask for a subset — one call returns everything —
        so a caller wanting several types should use this rather than the
        per-type helpers, which would each repeat the same request.
        """
        results = await self.search(query)

        channels: list[Channel] = []
        for entity_type, entities in results.items():
            if not entity_type.startswith("channel"):
                continue
            channels.extend(
                c for e in entities if (c := Channel.from_item({"entity": e})) is not None
            )

        shows = results.get("show-podcast", []) + results.get("show", [])

        return SearchResults(
            channels=channels,
            shows=[show for e in shows if (show := Show.from_item({"entity": e}))],
            artist_stations=[
                station
                for e in results.get("artist-station", [])
                if (station := ArtistStation.from_entity({"artistStation": e}))
            ],
            talent=[t for e in results.get("talent", []) if (t := Talent.from_item({"entity": e}))],
            genres=[g for e in results.get("genre", []) if (g := Genre.from_item({"entity": e}))],
        )

    async def get_episodes(self, show_id: str) -> list[Episode]:
        """Get a show's episodes, as models.

        Duration and air date come from each item's decorations, which is why
        this parses the container item rather than the bare entity.
        """
        items = await self._podcast_episode_items(show_id)
        return [e for n, item in enumerate(items) if (e := Episode.from_item(item, n))]

    async def _podcast_episode_items(self, podcast_entity_id: str) -> list[dict]:
        """Fetch a podcast's episodes as raw container items."""
        params = urlencode(
            {
                "entityType": "show-podcast",
                "entityId": podcast_entity_id,
                "offset": 0,
                "size": 1000,
                "maxResponses": 1000,
            },
        )
        resp = await self.request(
            method="GET",
            url=f"{API_BASE}/relationship/v1/container/aod?{params}",
        )
        sets = resp.get("container", {}).get("sets", [])
        return [item for s in sets for item in s.get("items", []) if "entity" in item]

    async def get_podcast_episodes(self, podcast_entity_id: str) -> list[dict]:
        """Get the episodes for a podcast as raw entities.

        Prefer :meth:`get_episodes`, which returns models and keeps the duration
        and air date that only exist on the surrounding container item.
        """
        return [item["entity"] for item in await self._podcast_episode_items(podcast_entity_id)]

    async def get_now_playing_all(self) -> dict[str, NowPlaying]:
        """Get what is currently playing on every live channel, keyed by channel id.

        SiriusXM publishes this as one cached feed covering all channels, so this
        is a single request no matter how many channels you care about. Poll it
        rather than requesting per-channel.
        """
        resp = await self.request(method="GET", url=LOOKAROUND_URL, authenticate=False)
        now_playing = {}
        for channel_id, entry in (resp.get("channels") or {}).items():
            if parsed := NowPlaying.from_feed_entry(channel_id, entry):
                now_playing[channel_id] = parsed
        return now_playing

    async def get_now_playing(self, channel_id: str) -> NowPlaying | None:
        """Get what is currently playing on a single channel.

        This pulls the whole feed; use :meth:`get_now_playing_all` when you need
        more than one channel.
        """
        return (await self.get_now_playing_all()).get(channel_id)

    async def get_channel_peek(self, channel_id: str) -> dict:
        """Get the neighbouring channels in the lineup (for channel-surfing UIs).

        Despite the name this is not now-playing data; see
        :meth:`get_now_playing`.
        """
        return await self.request(
            method="GET",
            url=f"{API_BASE}/channel-guide/v1/channel/{channel_id}/peek",
        )

    async def get_stream(self, entity_type: str, entity_id: str) -> "SxmStream":
        """Get a playback stream for an entity."""
        from aiosxm.stream import SxmStream  # noqa: PLC0415  (circular import)

        key = (entity_type, entity_id)
        if key not in self._streams:
            self._streams[key] = SxmStream(self, entity_type, entity_id)
        stream = self._streams[key]
        if not stream.initialized:
            try:
                await stream.initialize()
            except RequestError as err:
                # A lapsed subscription authenticates fine but cannot play.
                if err.status == HTTP_FORBIDDEN:
                    self._streams.pop(key, None)
                    message = (
                        "SiriusXM refused playback for this account. The subscription "
                        "may have lapsed; check get_entitlements()."
                    )
                    raise NotEntitledError(message, original_exception=err) from err
                self._streams.pop(key, None)
                raise
        return stream
