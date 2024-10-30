"""Support for streaming audio from SiriusXM."""

import re

from aiosxm.client import SxmClient
from aiosxm.const import BITRATE_32, BITRATE_64, BITRATE_96, BITRATE_256


class SxmStream:
    """A stream from SiriusXM."""

    def __init__(self, client: SxmClient, entity_type: str, entity_id: str) -> None:
        """Initialize the object."""
        self._client: SxmClient = client
        self.entity_type: str = entity_type
        self.entity_id: str = entity_id
        self.initialized: bool = False
        self._tune_source: dict | None = None
        self._streams_by_bitrate: dict | None = {}
        self._playback_key = None

    async def initialize(self) -> None:
        """Initialize the stream."""
        tune_source = await self._client.request(
            method="POST",
            url="https://api.edge-gateway.siriusxm.com/playback/play/v1/tuneSource",
            json={
                "id": self.entity_id,
                "type": self.entity_type,
                "hlsVersion": "V3",
                "manifestVariant": "FULL",
                "mtcVersion": "V2",
            },
        )
        self._tune_source = tune_source

        streams_by_bitrate = await self._client.request(
            method="GET",
            url=self.streams_by_bitrate_url,
        )
        for bitrate in [BITRATE_256, BITRATE_96, BITRATE_64, BITRATE_32]:
            pattern = rf"^.*_{bitrate}_full_v3\.m3u8.*$"
            match = re.search(pattern, streams_by_bitrate, re.MULTILINE)
            if match:
                self._streams_by_bitrate[bitrate] = f"{
                    self.base_url}/{match.group(0).strip()}"
            else:
                self._streams_by_bitrate[bitrate] = None

        self.initialized = True

    @property
    def stream_id(self) -> str:
        """The stream ID."""
        return self._tune_source["streams"][0]["id"]

    @property
    def streams_by_bitrate_url(self) -> str:
        """The URL to get the streams for each bitrate."""
        return self._tune_source["streams"][0]["urls"][0]["url"]

    @property
    def base_url(self) -> str:
        """The base URL for the stream."""
        return self._tune_source["streams"][0]["urls"][0]["url"].rsplit("/", 1)[0]

    async def get_playlist(self, bitrate: str = BITRATE_256) -> str:
        """Get a playlist for a given bitrate."""
        return await self._client.request(method="GET", url=self._streams_by_bitrate.get(bitrate))

    async def get_segment(self, segment_file: str, bitrate: str = BITRATE_256) -> bytes:
        """Get a playlist file segment."""
        bitrate_dir = self._streams_by_bitrate.get(
            bitrate).split("/")[-2]  # 2nd-to-last path segment
        return await self._client.request(method="GET", url=f"{self.base_url}/{bitrate_dir}/{segment_file}")

    async def get_key(self) -> dict:
        """Get the playback key information."""
        key_id = "00000000-0000-0000-0000-000000000000" if self.entity_type == "channel-linear" else self.stream_id
        return await self._client.request(
            method="GET",
            url=f"https://api.edge-gateway.siriusxm.com/playback/key/v1/{
                key_id}",
        )
