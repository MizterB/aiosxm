"""A Proxy server for interacting with SiriusXM."""

import base64
import logging
import re

from aiohttp import web

from aiosxm.client import SxmClient

logging.basicConfig(level=logging.DEBUG)
_LOGGER = logging.getLogger(__name__)

router = web.RouteTableDef()


@router.get("/channels")
async def get_channels(request: web.Request) -> web.Response:
    """Get the channels available to the user."""
    sxm = request.app["sxm"]
    channels = await sxm.get_channels()
    return web.json_response(channels)


@router.get("/library")
async def get_library(request: web.Request) -> web.Response:
    """Get the user's library."""
    sxm = request.app["sxm"]
    library = await sxm.get_library()
    return web.json_response(library)


@router.get("/stream/{entity_type}/{entity_id}/playlist.m3u8")
async def get_playlist(request: web.Request) -> web.Response:
    """Get an stream playlist."""
    entity_type = request.match_info.get("entity_type")
    entity_id = request.match_info.get("entity_id")
    sxm = request.app["sxm"]
    stream = await sxm.get_stream(entity_type, entity_id)
    await stream.initialize()
    playlist_data = await stream.get_playlist()
    # Replace the URI in EXT-X_KEY with a proxy path
    find = r'#EXT-X-KEY:METHOD=AES-128,URI="(.+?)"'
    replace = f'#EXT-X-KEY:METHOD=AES-128,URI="/stream/{
        entity_type}/{entity_id}/key"'
    proxy_playlist_data = re.sub(find, replace, playlist_data)
    return web.Response(body=proxy_playlist_data, content_type="application/x-mpegURL")


@router.get("/stream/{entity_type}/{entity_id}/key")
async def get_key(request: web.Request) -> web.Response:
    """Get a stream decryption key."""
    entity_type = request.match_info.get("entity_type")
    entity_id = request.match_info.get("entity_id")
    sxm = request.app["sxm"]
    stream = await sxm.get_stream(entity_type, entity_id)
    key_data = await stream.get_key()
    decoded_key = base64.b64decode(key_data["key"])
    return web.Response(body=decoded_key, content_type="application/octet-stream")


@router.get(r"/stream/{entity_type}/{entity_id}/{segment_file:.+\.aac}")
async def get_segment(request: web.Request) -> web.Response:
    """Get a playlist segment."""
    entity_type = request.match_info.get("entity_type")
    entity_id = request.match_info.get("entity_id")
    sxm = request.app["sxm"]
    segment_file = request.match_info.get("segment_file")
    stream = await sxm.get_stream(entity_type, entity_id)
    segment_data = await stream.get_segment(segment_file)
    return web.Response(body=segment_data, content_type="audio/aac")


async def proxy_server() -> web.Application:
    """Initialize the proxy."""
    app = web.Application()
    app.add_routes(router)
    sxm = SxmClient()
    await sxm.connect()
    app["sxm"] = sxm
    return app


web.run_app(
    app=proxy_server(),
    host="0.0.0.0",  # noqa: S104
    port=8080,
    access_log=_LOGGER,
)
