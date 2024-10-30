"""A client for interacting with SiriusXM."""

import json
import logging
import os
import re
import types
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiofiles
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError, ClientResponseError, InvalidURL, ServerTimeoutError

from aiosxm.const import (
    DEFAULT_REQUEST_TIMEOUT,
    SXM_DEVICE_PAYLOAD,
    SXM_REQUEST_HEADERS,
)

if TYPE_CHECKING:
    from aiosxm.stream import SxmStream

_LOGGER = logging.getLogger(__name__)


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
        self._username: str = username or os.getenv("SXM_USERNAME")
        self._password: str = password or os.getenv("SXM_PASSWORD")
        self._http_client_session: ClientSession | None = session
        self._http_client_session_internal: bool = False

        self._device_session: dict | None = None
        self._anonymous_session: dict | None = None
        self._authentication_response: dict | None = None
        self._authenticated_session: dict | None = None

        self._access_token: str | None = None
        self._access_token_expiration: datetime | None = None

        self._streams: dict[tuple[str, str], SxmStream] = {}

    async def __aenter__(self) -> "SxmClient":
        """Enter the runtime context related to this object."""
        await self.connect()

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
        await self._create_device_session()
        await self._authenticate()
        await self._load_config()

    async def disconnect(self) -> None:
        """Disconnect from SiriusXM."""
        if self._http_client_session_internal and self._http_client_session:
            await self._http_client_session.close()
            self._http_client_session = None
            self._http_client_session_internal = False

    def _get_http_client_session(self) -> ClientSession:
        """Get the HTTP client session."""
        if not self._http_client_session or self._http_client_session.closed:
            self._http_client_session = ClientSession(
                timeout=ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT))
            self._http_client_session_internal = True
        return self._http_client_session

    async def request(self, method: str, url: str, **kwargs: dict[str, Any]) -> dict | str:
        """Make a request against the API."""
        if self._access_token_expiration and datetime.now(tz=UTC) >= self._access_token_expiration:
            _LOGGER.info("Access token expired; requesting a new one.")
            self._access_token = None
            self._access_token_expiration = None
            await self._authenticate()

        if "headers" not in kwargs:
            kwargs.setdefault("headers", SXM_REQUEST_HEADERS)

        if self._access_token:
            kwargs["headers"]["Authorization"] = f"Bearer {self._access_token}"

        session = self._get_http_client_session()
        try:
            async with session.request(method, url, **kwargs) as resp:
                resp.raise_for_status()
                if resp.content_type == "application/json":
                    return await resp.json()
                if resp.content_type in ["text/html", "application/x-mpegurl", "application/vnd.apple.mpegurl"]:
                    return await resp.text()
                if resp.content_type == "audio/aac":
                    return await resp.read()
                return await resp.read()
        except (ClientResponseError, ServerTimeoutError, InvalidURL, ClientError) as err:
            _LOGGER.exception("Error occurred while requesting %s", url)
            raise RequestError(url, err) from err

    async def _create_device_session(self) -> None:
        """Create a SiriusXM device session."""
        resp = await self.request(
            method="POST",
            url="https://api.edge-gateway.siriusxm.com/device/v1/devices",
            json=SXM_DEVICE_PAYLOAD,
        )
        self._device_session = resp

    async def _create_anonymous_session(self, device_grant: str) -> None:
        """Create an anonymous SiriusXM user session."""
        resp = await self.request(
            method="POST",
            url="https://api.edge-gateway.siriusxm.com/session/v1/sessions/anonymous",
            headers={**SXM_REQUEST_HEADERS,
                     "Authorization": f"Bearer {device_grant}"},
            json=True,
        )
        self._anonymous_session = resp

    async def _get_identity_status(self, username: str, anonymous_access_token: str) -> dict:
        """Get the status of an identity."""
        return await self.request(
            method="GET",
            url=f"https://api.edge-gateway.siriusxm.com/identity/v1/identities/status?handle={
                username}",
            headers={
                **SXM_REQUEST_HEADERS,
                "Authorization": f"Bearer {anonymous_access_token}",
            },
            json=True,
        )

    async def _authenticate_with_password(self, anonymous_access_token: str) -> None:
        """Authenticate using a password."""
        resp = await self.request(
            method="POST",
            url="https://api.edge-gateway.siriusxm.com/identity/v1/identities/authenticate/password",
            headers={
                **SXM_REQUEST_HEADERS,
                "Authorization": f"Bearer {anonymous_access_token}",
            },
            json={
                "handle": self._username,
                "password": self._password,
            },
        )
        self._authentication_response = resp

    async def _create_authenticated_session(self, authentication_grant: str) -> None:
        """Create an authenticated SiriusXM user session."""
        resp = await self.request(
            method="POST",
            url="https://api.edge-gateway.siriusxm.com/session/v1/sessions/authenticated",
            headers={
                **SXM_REQUEST_HEADERS,
                "Authorization": f"Bearer {authentication_grant}",
            },
            json=True,
        )
        self._authenticated_session = resp

    async def _authenticate(self) -> None:
        """Authenticate against the API."""
        try:
            if not self._device_session:
                await self._create_device_session()
            await self._create_anonymous_session(self._device_session["grant"])
            identity_status = await self._get_identity_status(self._username, self._anonymous_session["accessToken"])
            if not identity_status["hasPassword"]:
                message = f"User {
                    self._username} does not have a password set."
                raise AuthenticationError(message)
            await self._authenticate_with_password(self._anonymous_session["accessToken"])
            await self._create_authenticated_session(self._authentication_response["grant"])
            self._access_token = self._authenticated_session["accessToken"]
            self._access_token_expiration = datetime.fromisoformat(
                self._authenticated_session["accessTokenExpiresAt"])
        except RequestError as err:
            message = f"An error occurred during authentication of user {
                self._username}"
            raise AuthenticationError(message) from err

    async def _load_config(self, to_file: str | None = None) -> None:
        """Load the full web player config."""
        player_html = await self.request(
            method="GET",
            url="https://www.siriusxm.com/player",
        )
        hydrated_data = "{}"
        match = re.search(
            r'<script id="hydrated_data" type="application/json">(.*?)</script>', player_html, re.DOTALL)
        if match:
            hydrated_data = match.group(1)
        hydrated_data = json.loads(hydrated_data)
        if to_file:
            async with aiofiles.open(to_file, "w") as f:
                await f.write(json.dumps(hydrated_data, indent=4))
        self._config = hydrated_data.get("config", {})

    async def get_library(self) -> dict:
        """Get library entities."""
        resp = await self.request(
            method="GET",
            url="https://api.edge-gateway.siriusxm.com/ondemand/v1/library/all",
            json=True,
        )
        return list(resp["allDataMap"].values())

    async def get_channels(self) -> dict:
        """Get linear and on-demand channel list."""
        resp = await self.request(
            method="GET",
            url="https://api.edge-gateway.siriusxm.com/relationship/v1/container/all-channels?entityType=curated-grouping&entityId=&offset=0&size=1000",
            json=True,
        )
        return [
            {
                "channelNumber": c["decorations"]["channelNumber"],
                "unentitled": c["decorations"]["unentitled"],
                "type": c["entity"]["type"],
                "id": c["entity"]["id"],
                "title": c["entity"]["texts"]["title"]["default"],
                "title_short": c["entity"]["texts"]["title"].get("short"),
                "description": c["entity"]["texts"]["description"]["default"],
                "images": c["entity"]["images"],
            }
            for c in resp["container"]["sets"][0]["items"]
        ]

    async def get_podcast_episodes(self, podcast_entity_id: str) -> dict:
        """Get podcast episode."""
        resp = await self.request(
            method="GET",
            url=f"https://api.edge-gateway.siriusxm.com/relationship/v1/container/aod?&entityType=show-podcast&entityId={
                podcast_entity_id}&offset=0&size=1000&maxResponses=1000",
            json=True,
        )
        return [r["entity"] for r in resp["container"]["sets"][0]["items"]]

    async def get_stream(self, entity_type: str, entity_id: str) -> "SxmStream":
        """Get a playback stream for an entity."""
        from aiosxm.stream import SxmStream

        if (entity_type, entity_id) not in self._streams:
            self._streams[(entity_type, entity_id)] = SxmStream(
                self, entity_type, entity_id)
        stream = self._streams[(entity_type, entity_id)]
        if not stream.initialized:
            await stream.initialize()
        return stream


class RequestError(Exception):
    """An error occurred while making a request."""

    def __init__(self, url: str, original_exception: Exception) -> None:
        """Initialize the exception."""
        super().__init__(f"Error for URL {url}: {original_exception}")
        self.url = url
        self.original_exception = original_exception


class AuthenticationError(Exception):
    """An error occurred while authenticating."""

    def __init__(self, message: str, original_exception: Exception) -> None:
        """Initialize the exception."""
        super().__init__(message)
        self.original_exception = original_exception
