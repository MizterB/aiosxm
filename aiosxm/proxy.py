"""A proxy server that exposes SiriusXM streams as plain HLS.

The SiriusXM CDN requires an Authorization header on every playlist, segment, and
key request, which ordinary HLS players won't send. This proxy holds the session
and re-serves the stream at URLs a player can consume directly.

Run it with::

    python -m aiosxm.proxy --host 127.0.0.1 --port 8080

Credentials come from --username/--password, the SXM_USERNAME / SXM_PASSWORD
environment variables, or a .env file in the current directory or the repo root.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, unquote
from weakref import WeakKeyDictionary

from aiohttp import web

from aiosxm.client import SxmClient
from aiosxm.const import BITRATE_256, CHANNEL_CACHE_TTL
from aiosxm.exceptions import NotEntitledError, RequestError
from aiosxm.models import (
    ArtistStation,
    Channel,
    Episode,
    NowPlaying,
    Show,
    Track,
    _clean_text,
    _first_image_key,
    build_image_url,
)

_LOGGER = logging.getLogger(__name__)

UI_FILE = Path(__file__).parent / "static" / "index.html"

# Upper bound on ?count= so one request can't walk the queue forever.
MAX_QUEUE_TRACKS = 100

# SiriusXM's live playlists carry the whole ~5 hour rewind buffer (1845 segments),
# which is far more than a player needs and makes some of them stall. Trim to a
# few minutes of lead: enough for a player to recover from a stall without
# re-buffering the world. `?window=0` serves the untrimmed playlist.
LIVE_WINDOW_SEGMENTS = 20

# Entity types that can be tuned directly, rather than naming another entity.
PLAYABLE_TYPES = frozenset({"channel-linear", "channel-xtra", "artist-station", "episode-podcast", "episode-audio"})

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Channel caches for embedded use, keyed weakly so they die with the client.
_EMBEDDED_CACHES: "WeakKeyDictionary[SxmClient, tuple[dict, asyncio.Lock]]" = WeakKeyDictionary()

router = web.RouteTableDef()


def load_dotenv(path: Path | None = None) -> None:
    """Load SXM_* variables from a .env file, without overriding the environment.

    Looks in the current directory and then the repo root, so running the proxy
    from anywhere in a checkout picks up the same credentials the examples use.
    """
    candidates = [path] if path else [Path.cwd() / ".env", Path(__file__).parent.parent / ".env"]
    for env_file in candidates:
        if not env_file or not env_file.is_file():
            continue
        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        _LOGGER.debug("Loaded credentials from %s", env_file)
        return


@web.middleware
async def cors_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Allow any origin, so a page served elsewhere can still drive this proxy."""
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@router.get("/")
async def get_ui(_request: web.Request) -> web.Response:
    """Serve the test UI."""
    if not UI_FILE.is_file():
        raise web.HTTPNotFound(text="UI not installed")
    return web.Response(body=UI_FILE.read_bytes(), content_type="text/html")


def _client(request: web.Request) -> SxmClient:
    """Get the client for a request.

    Standalone, it lives in application state. When the routes are embedded via
    :func:`make_routes` the handler puts it on the request instead, so the host
    application doesn't need aiosxm-specific app state.
    """
    client = request.get("sxm") or request.app.get("sxm")
    if client is None:
        message = "No SiriusXM client available for this request"
        raise web.HTTPInternalServerError(text=message)
    return client


def _bitrate(request: web.Request) -> str:
    """Read the requested bitrate from the query string."""
    return request.query.get("bitrate", BITRATE_256)


async def _get_stream(request: web.Request):  # noqa: ANN202
    """Resolve the stream for a request, translating client errors to HTTP."""
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    client = _client(request)
    try:
        return await client.get_stream(entity_type, entity_id)
    except NotEntitledError as err:
        raise web.HTTPForbidden(text=str(err)) from err
    except RequestError as err:
        raise web.HTTPBadGateway(text=str(err)) from err


def _serialize(channel: Channel) -> dict:
    """Render a channel as JSON, with resolved image URLs."""
    return {
        "id": channel.id,
        "type": channel.type,
        "title": channel.title,
        "title_short": channel.title_short,
        "channel_number": channel.channel_number,
        "description": channel.description,
        "genre": channel.genre,
        "unentitled": channel.unentitled,
        "off_air": channel.off_air,
        "images": [
            {
                "name": i.name,
                "aspect_ratio": i.aspect_ratio,
                "width": i.width,
                "height": i.height,
                "url": i.url,
            }
            for i in channel.images
        ],
    }


def _serialize_now_playing(current: NowPlaying) -> dict:
    """Render now-playing data as JSON, with a resolved image URL."""
    return {
        "channel_id": current.channel_id,
        "title": current.title,
        "artist": current.artist,
        "show": current.show,
        "is_ad": current.is_ad,
        "started_at": current.started_at,
        "image_url": current.image_url(),
    }


def _invalidate_library(request: web.Request) -> None:
    """Drop any cached library view after a write.

    The channel catalog is untouched — only which of it is saved has changed.
    """
    for cache in (request.app.get("channel_cache"), _embedded_cache(_client(request))[0]):
        if isinstance(cache, dict):
            cache.pop("library", None)


def _cache_is_stale(cache: dict) -> bool:
    """Whether a cached channel list has outlived its TTL.

    Without this a long-running process never notices SiriusXM adding or
    removing a channel.
    """
    fetched_at = cache.get("fetched_at")
    if fetched_at is None:
        return False
    return (time.monotonic() - fetched_at) > CHANNEL_CACHE_TTL


def _embedded_cache(client: SxmClient) -> tuple[dict, asyncio.Lock]:
    """Per-client channel cache, for when the routes are embedded elsewhere.

    Keyed on the client itself so it is collected with it, rather than living in
    a module-level dict that would outlive the provider.
    """
    cache = _EMBEDDED_CACHES.get(client)
    if cache is None:
        cache = ({}, asyncio.Lock())
        _EMBEDDED_CACHES[client] = cache
    return cache


async def _cached_channels(request: web.Request, *, refresh: bool = False) -> list[dict]:
    """Get the serialized channel list.

    The client owns the expensive part — walking the catalog, its TTL and the
    lock against concurrent walks. This only memoises the JSON serialization, so
    repeat requests don't re-render 712 channels.
    """
    client = _client(request)
    channels = await client.get_channels(refresh=refresh)

    cache = request.app.get("channel_cache")
    if cache is None:
        cache, _lock = _embedded_cache(client)
    # get_channels() hands back a copy each call, so compare the client's fetch
    # stamp rather than object identity to know whether the catalog moved.
    stamp = client.channels_fetched_at
    if refresh or cache.get("stamp") != stamp or "channels" not in cache:
        cache["stamp"] = stamp
        cache["channels"] = [_serialize(c) for c in channels]
    return cache["channels"]


@router.get("/channels")
async def get_channels(request: web.Request) -> web.Response:
    """Get the channels available to the user. Pass `?refresh=1` to rebuild the cache."""
    refresh = request.query.get("refresh") in ("1", "true", "yes")
    return web.json_response(await _cached_channels(request, refresh=refresh))


@router.get("/library")
async def get_library(request: web.Request) -> web.Response:
    """Get the channels in the user's library.

    Resolving the library means matching it against the catalog, so this reuses
    the cached channel list rather than walking it again.
    """
    client = _client(request)
    library = await client.get_library()
    wanted = {
        entry["entityId"]
        for entry in library
        if entry.get("entityId") and str(entry.get("entityType", "")).startswith("channel")
    }
    channels = await _cached_channels(request)
    items = [c for c in channels if c["id"] in wanted]

    # Artist stations live outside the channel catalog, so they have to be
    # hydrated separately or they silently vanish from the library. Don't let a
    # hydration failure take the whole listing down with it.
    if any(e.get("entityType") == "artist-station" for e in library):
        try:
            items.extend(_serialize_station(s) for s in await client.get_library_artist_stations())
        except RequestError:
            _LOGGER.warning("Could not load artist stations", exc_info=True)

    # A library can hold shows, podcasts and teams too. They aren't in the
    # channel catalog and were previously dropped without trace, so hydrate
    # whatever is left over rather than silently under-reporting the library.
    known = {item["id"] for item in items}
    leftover = [
        entry
        for entry in library
        if entry.get("entityId")
        and entry["entityId"] not in known
        and entry.get("entityType") != "artist-station"
        and not str(entry.get("entityType", "")).startswith("channel")
    ]
    for entry in leftover:
        try:
            hydrated = await client.hydrate(entry["entityType"], entry["entityId"])
        except RequestError:
            _LOGGER.debug("Could not hydrate %s/%s", entry["entityType"], entry["entityId"])
            continue
        if hydrated:
            items.append(_serialize_library_entry(hydrated, entry["entityType"]))

    return web.json_response(items)


def _serialize_library_entry(entity: dict, entity_type: str) -> dict:
    """Render a hydrated library entity (show, podcast, team) as a browse node."""
    texts = entity.get("texts") or {}
    title = (texts.get("title") or {}).get("default")
    description = (texts.get("description") or {}).get("default")
    is_show = entity_type in ("show", "show-podcast")
    return {
        "id": entity["id"],
        "type": entity_type,
        "title": title or "",
        "description": _clean_text(description),
        "image_url": _entity_image_url(entity),
        # Shows list their episodes; anything else browses its container page.
        "path": (f"/podcasts/{entity['id']}/episodes" if is_show else f"/browse/{entity_type}/{entity['id']}"),
        "is_show": is_show,
    }


def _entity_image_url(entity: dict, width: int = 300, height: int = 300) -> str | None:
    """Artwork for a hydrated entity, if it has any."""
    key = _first_image_key(entity.get("images") or {}, ("tile", "logo", "hero_tile", "header"))
    return build_image_url(key, width, height) if key else None


def _serialize_station(station: ArtistStation) -> dict:
    """Render an artist station as JSON."""
    return {
        "id": station.id,
        "type": "artist-station",
        "title": station.title,
        "description": station.description,
        "image_url": station.image_url(),
    }


@router.get("/browse")
async def browse_root(_request: web.Request) -> web.Response:
    """List the top-level browse categories.

    Shaped as a tree so a consumer (Music Assistant's `browse(path)`, the test
    console) can walk it without knowing anything about SiriusXM's own layout.
    """
    return web.json_response(
        [
            {"id": "genres", "title": "Genres", "type": "folder", "path": "/browse/genres"},
            {"id": "sports", "title": "Sports teams", "type": "search", "path": "/teams?q="},
            {"id": "channels", "title": "All channels", "type": "folder", "path": "/channels"},
            {"id": "library", "title": "My library", "type": "folder", "path": "/library"},
            {
                "id": "artist-stations",
                "title": "Artist stations",
                "type": "folder",
                "path": "/artist-stations",
            },
        ]
    )


@router.get("/browse/genres")
async def browse_genres(request: web.Request) -> web.Response:
    """List genres, each with the number of channels it holds."""
    channels = await _cached_channels(request)
    counts: dict[str, int] = {}
    for channel in channels:
        if genre := channel.get("genre"):
            counts[genre] = counts.get(genre, 0) + 1
    return web.json_response(
        [
            {
                "id": name,
                "title": name,
                "type": "folder",
                "count": count,
                "path": f"/browse/genres/{quote(name, safe='')}",
            }
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    )


@router.get("/browse/genres/{genre}")
async def browse_genre(request: web.Request) -> web.Response:
    """List the channels in one genre."""
    genre = unquote(request.match_info["genre"]).casefold()
    channels = [c for c in await _cached_channels(request) if (c.get("genre") or "").casefold() == genre]
    if not channels:
        raise web.HTTPNotFound(text=f"No channels in genre {genre!r}")
    return web.json_response(channels)


@router.get("/shows")
async def get_shows(request: web.Request) -> web.Response:
    """Find shows and podcasts. Requires `?q=`."""
    query = request.query.get("q", "").strip()
    if not query:
        raise web.HTTPBadRequest(text="Missing required query parameter 'q'")
    client = _client(request)
    return web.json_response([_serialize_show(s) for s in await client.search_shows(query)])


def _serialize_entity(item: dict) -> dict:
    """Render a container child as a browse node."""
    entity = item.get("entity") or {}
    entity_type = entity.get("type") or ""
    entity_id = entity.get("id") or ""
    decorations = item.get("decorations") or {}

    # Some entities aren't playable themselves but name a channel that is.
    play = ((item.get("actions") or {}).get("play") or [{}])[0].get("entity") or {}
    play_type = play.get("type") or (entity_type if entity_type in PLAYABLE_TYPES else None)
    play_id = play.get("id") or (entity_id if entity_type in PLAYABLE_TYPES else None)

    return {
        "id": entity_id,
        "type": entity_type,
        "title": (entity.get("texts", {}).get("title") or {}).get("default"),
        "air_date": decorations.get("airDate"),
        "browse_path": f"/browse/{entity_type}/{entity_id}" if entity_type else None,
        "play_path": (f"/stream/{play_type}/{play_id}/playlist.m3u8" if play_type and play_id else None),
    }


@router.get("/browse/{entity_type}/{entity_id}")
async def browse_entity(request: web.Request) -> web.Response:
    """List the browsable sections of any entity.

    Every container entity — show, talent, team, league, genre — has a page of
    named sections (`aod`, `epg`, `vod`, `live`, ...). This is the same shape
    everywhere, so a consumer can walk the whole catalog with two routes.
    """
    client = _client(request)
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    try:
        containers = await client.get_containers(entity_type, entity_id)
    except RequestError as err:
        raise web.HTTPNotFound(text=f"No page for {entity_type}/{entity_id}") from err
    return web.json_response(
        [
            {
                "id": name,
                "title": name.replace("-", " ").title(),
                "type": "folder",
                "path": f"/browse/{entity_type}/{entity_id}/{name}",
            }
            for name in sorted(containers)
        ]
    )


@router.get("/browse/{entity_type}/{entity_id}/{container}")
async def browse_entity_container(request: web.Request) -> web.Response:
    """List the entities inside one section of an entity's page."""
    client = _client(request)
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    container = request.match_info["container"]
    try:
        items = await client.browse_entity(entity_type, entity_id, container)
    except KeyError as err:
        raise web.HTTPNotFound(text=str(err)) from err
    except RequestError as err:
        raise web.HTTPBadGateway(text=str(err)) from err
    return web.json_response([_serialize_entity(i) for i in items])


def _serialize_show(show: Show) -> dict:
    """Render a show as JSON."""
    return {
        "id": show.id,
        "type": show.type,
        "title": show.title,
        "description": show.description,
        "image_url": show.image_url(),
        "path": f"/podcasts/{show.id}/episodes",
    }


def _serialize_episode(episode: Episode) -> dict:
    """Render an episode as JSON."""
    return {
        "id": episode.id,
        "type": episode.type,
        "title": episode.title,
        "description": episode.description,
        "duration": episode.duration,
        "air_date": episode.air_date,
        "show_id": episode.show_id,
        "position": episode.position,
        "unentitled": episode.unentitled,
        "image_url": episode.image_url(),
        "play_path": f"/stream/{episode.type}/{episode.id}/playlist.m3u8",
    }


@router.get("/teams")
async def get_teams(request: web.Request) -> web.Response:
    """Find sports teams. Requires `?q=`."""
    query = request.query.get("q", "").strip()
    if not query:
        raise web.HTTPBadRequest(text="Missing required query parameter 'q'")
    client = _client(request)
    return web.json_response(
        [
            {
                "id": team.get("id"),
                "type": "team",
                "title": (team.get("texts", {}).get("title") or {}).get("default"),
                "path": f"/teams/{team.get('id')}/broadcasts",
            }
            for team in await client.get_teams(query)
        ]
    )


@router.get("/teams/{team_id}/broadcasts")
async def get_team_broadcasts(request: web.Request) -> web.Response:
    """List a team's game broadcasts, live first then scheduled.

    Games aren't playable themselves; each entry points at the channel carrying
    it, so `play_path` is what a player should open.
    """
    client = _client(request)
    broadcasts = await client.get_team_broadcasts(request.match_info["team_id"])
    return web.json_response(
        [
            {
                "id": b.id,
                "title": b.title,
                "air_date": b.air_date,
                "league": b.league,
                "is_live": b.is_live,
                "coverage": b.coverage,
                "playable": b.is_playable,
                "play_path": (
                    f"/stream/{b.play_entity_type}/{b.play_entity_id}/playlist.m3u8" if b.is_playable else None
                ),
            }
            for b in broadcasts
        ]
    )


@router.get("/artist-stations")
async def get_artist_stations(request: web.Request) -> web.Response:
    """Get artist stations.

    With `?q=`, searches for an artist's stations. Without it, returns the ones
    saved to the account's library — SiriusXM publishes no catalog of artist
    stations, so they can only be found by name.
    """
    client = _client(request)
    query = request.query.get("q", "").strip()
    stations = await client.search_artist_stations(query) if query else await client.get_library_artist_stations()
    return web.json_response([_serialize_station(s) for s in stations])


def _ranged_response(request: web.Request, body: bytes, content_type: str) -> web.Response:
    """Serve bytes, honouring a `Range` request header.

    WebKit's native HLS player — Safari, and the webview VS Code's Simple
    Browser uses — fetches segments with range requests and expects `206
    Partial Content`. Answering `200` with the whole body makes playback fail
    there, even though hls.js in Chrome is happy with it.
    """
    headers = {"Accept-Ranges": "bytes"}
    total = len(body)
    range_header = request.headers.get("Range", "")

    if not range_header.startswith("bytes="):
        return web.Response(body=body, content_type=content_type, headers=headers)

    spec = range_header.removeprefix("bytes=").split(",")[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # A suffix range: the last N bytes.
            start = max(0, total - int(end_text))
            end = total - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else total - 1
    except ValueError:
        return web.Response(body=body, content_type=content_type, headers=headers)

    end = min(end, total - 1)
    if start > end or start >= total:
        headers["Content-Range"] = f"bytes */{total}"
        raise web.HTTPRequestRangeNotSatisfiable(headers=headers)

    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return web.Response(
        body=body[start : end + 1],
        status=206,
        content_type=content_type,
        headers=headers,
    )


def _serialize_track(track: Track) -> dict:
    """Render a queued track as JSON."""
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "duration": track.duration,
        "url": track.url,
        "image_url": track.image_url(),
    }


@router.get("/stream/{entity_type}/{entity_id}/tracks")
async def get_tracks(request: web.Request) -> web.Response:
    """Get queued tracks for an artist station.

    The API hands back three at a time; `?count=` pulls further batches by
    following the queue cursor, so a consumer can fill a playlist of any length.
    """
    stream = await _get_stream(request)
    if not stream.is_track_queue:
        raise web.HTTPBadRequest(text="This entity is not a track queue")

    try:
        count = int(request.query.get("count", 0))
    except ValueError:
        raise web.HTTPBadRequest(text="count must be an integer") from None

    if count <= 0:
        return web.json_response([_serialize_track(t) for t in stream.tracks])

    count = min(count, MAX_QUEUE_TRACKS)
    tracks = [_serialize_track(t) async for t in stream.iter_tracks(limit=count)]
    return web.json_response(tracks)


@router.post("/library/{entity_type}/{entity_id}")
async def add_to_library(request: web.Request) -> web.Response:
    """Add an item to the account's library.

    A write: the change is visible in the SiriusXM app too.
    """
    client = _client(request)
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    try:
        await client.add_to_library(entity_type, entity_id)
    except RequestError as err:
        raise web.HTTPBadGateway(text=str(err)) from err
    _invalidate_library(request)
    return web.json_response({"added": True, "type": entity_type, "id": entity_id})


@router.delete("/library/{entity_type}/{entity_id}")
async def remove_from_library(request: web.Request) -> web.Response:
    """Remove an item from the account's library."""
    client = _client(request)
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    try:
        await client.remove_from_library(entity_type, entity_id)
    except RequestError as err:
        raise web.HTTPBadGateway(text=str(err)) from err
    _invalidate_library(request)
    return web.json_response({"removed": True, "type": entity_type, "id": entity_id})


@router.get("/entitlements")
async def get_entitlements(request: web.Request) -> web.Response:
    """Get the account's entitlements, to check whether playback is allowed."""
    client = _client(request)
    entitlements = await client.get_entitlements()
    return web.json_response(
        {
            "entitled": any(e.get("state") != "revoked" for e in entitlements),
            "entitlements": entitlements,
        },
    )


@router.get("/search")
async def search(request: web.Request) -> web.Response:
    """Search the catalog. Podcasts and episodes are only reachable this way."""
    query = request.query.get("q", "").strip()
    if not query:
        raise web.HTTPBadRequest(text="Missing required query parameter 'q'")
    client = _client(request)
    results = await client.search(query)
    return web.json_response(
        {
            entity_type: [
                {
                    "id": e.get("id"),
                    "type": e.get("type"),
                    "title": (e.get("texts", {}).get("title") or {}).get("default"),
                }
                for e in entities
            ]
            for entity_type, entities in results.items()
        }
    )


@router.get("/podcasts/{show_id}/episodes")
async def get_podcast_episodes(request: web.Request) -> web.Response:
    """List the episodes of a podcast."""
    client = _client(request)
    episodes = await client.get_episodes(request.match_info["show_id"])
    return web.json_response([_serialize_episode(e) for e in episodes])


@router.get("/now-playing")
async def get_now_playing_all(request: web.Request) -> web.Response:
    """Get what is currently playing on every live channel."""
    client = _client(request)
    now_playing = await client.get_now_playing_all()
    return web.json_response({k: _serialize_now_playing(v) for k, v in now_playing.items()})


@router.get("/now-playing/{channel_id}")
async def get_now_playing(request: web.Request) -> web.Response:
    """Get what is currently playing on a single channel."""
    client = _client(request)
    channel_id = request.match_info["channel_id"]
    current = await client.get_now_playing(channel_id)
    if current is None:
        raise web.HTTPNotFound(text=f"No now-playing data for channel {channel_id}")
    return web.json_response(_serialize_now_playing(current))


@router.get("/stream/{entity_type}/{entity_id}/playlist.m3u8")
async def get_playlist(request: web.Request) -> web.Response:
    """Get a stream playlist with the key URI pointed back at this proxy.

    The playlist is trimmed to the last few segments by default (`?window=N`,
    or `?window=0` for the full ~5 hour rewind buffer).

    Segments are served through this proxy by default. Browsers require it (the
    CDN only sends `Access-Control-Allow-Origin` for siriusxm.com), and it is
    slightly faster in practice because the proxy reuses a warm connection pool
    rather than doing a fresh TLS handshake per segment.

    Pass `?absolute=1` to point segments straight at the CDN. Those URLs carry a
    signed token and need no auth header, so audio flows client-to-CDN and never
    passes through this process. Both modes work with ffmpeg.
    """
    stream = await _get_stream(request)
    if stream.is_track_queue:
        # There is no continuous stream; play the head of the queue and let the
        # caller advance through /tracks.
        tracks = stream.tracks
        if not tracks or not tracks[0].url:
            raise web.HTTPNotFound(text="No playable tracks in this station")
        raise web.HTTPFound(tracks[0].url)
    if stream.is_progressive:
        # On-demand episodes are a single media file; send the player straight to it.
        raise web.HTTPFound(stream.content_url)
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    key_url = f"/stream/{entity_type}/{entity_id}/key"
    absolute = request.query.get("absolute", "") in ("1", "true", "yes")
    try:
        window = int(request.query.get("window", LIVE_WINDOW_SEGMENTS))
    except ValueError:
        raise web.HTTPBadRequest(text="window must be an integer") from None
    playlist = await stream.get_proxied_playlist(
        key_url,
        _bitrate(request),
        absolute_segments=absolute,
        window=window if window > 0 else None,
    )
    return web.Response(body=playlist.encode(), content_type="application/vnd.apple.mpegurl")


@router.get("/stream/{entity_type}/{entity_id}/info")
async def get_stream_info(request: web.Request) -> web.Response:
    """Get stream details, including which bitrates this channel actually offers."""
    stream = await _get_stream(request)
    return web.json_response(
        {
            "entity_type": stream.entity_type,
            "entity_id": stream.entity_id,
            "track_queue": stream.is_track_queue,
            "progressive": stream.is_progressive,
            "available_bitrates": stream.available_bitrates,
            "master_playlist_url": stream.master_playlist_url,
        },
    )


@router.get("/stream/{entity_type}/{entity_id}/key")
async def get_key(request: web.Request) -> web.Response:
    """Get a stream decryption key."""
    stream = await _get_stream(request)
    key = await stream.get_key()
    return _ranged_response(request, key, "application/octet-stream")


@router.get(r"/stream/{entity_type}/{entity_id}/{segment_file:.+\.aac}")
async def get_segment(request: web.Request) -> web.Response:
    """Get a playlist segment.

    The bitrate is taken from the segment filename rather than the query string,
    because players resolve segment URIs relative to the playlist and drop any
    query we put there.
    """
    stream = await _get_stream(request)
    segment_file = request.match_info["segment_file"]
    bitrate = request.query.get("bitrate") or stream.bitrate_for_segment(segment_file)
    segment = await stream.get_segment(segment_file, bitrate)
    return _ranged_response(request, segment, "audio/aac")


def make_routes(
    client: SxmClient,
    *,
    prefix: str = "",
    stream_only: bool = False,
) -> list[tuple[str, str, Handler]]:
    """Build `(method, path, handler)` routes bound to an existing client.

    For embedding the proxy in another aiohttp application — the handlers close
    over `client` instead of reading it from application state, so the host app
    doesn't have to know anything about aiosxm.

    Set `stream_only` to mount just the playback routes (playlist, key, segments
    and stream info) and skip the JSON/UI ones.
    """
    routes: list[tuple[str, str, Handler]] = []
    for route in router:
        if stream_only and not route.path.startswith("/stream/"):
            continue

        async def handler(
            request: web.Request,
            _route_handler: Handler = route.handler,
        ) -> web.StreamResponse:
            request["sxm"] = client
            return await _route_handler(request)

        routes.append((route.method, prefix + route.path, handler))
    return routes


async def create_app(
    username: str | None = None,
    password: str | None = None,
) -> web.Application:
    """Build the proxy application."""
    app = web.Application(middlewares=[cors_middleware])
    app.add_routes(router)
    app["channel_cache"] = {}
    app["channel_lock"] = asyncio.Lock()

    async def _connect(app: web.Application) -> None:
        client = SxmClient(username, password)
        await client.connect()
        if not await client.is_entitled():
            _LOGGER.warning(
                "This SiriusXM account has no active entitlements; playback will fail.",
            )
        app["sxm"] = client

    async def _disconnect(app: web.Application) -> None:
        await app["sxm"].disconnect()

    app.on_startup.append(_connect)
    app.on_cleanup.append(_disconnect)
    return app


if __name__ == "__main__":  # pragma: no cover
    from aiosxm.cli import main

    main()
