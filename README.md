# aiosxm

An asynchronous Python library for SiriusXM.

Talks to the current SiriusXM edge-gateway API (`api.edge-gateway.siriusxm.com`) —
the same one the web player uses — rather than the legacy `player.siriusxm.com/rest`
endpoints, which no longer respond.

## Notice

### For Personal Use Only

`aiosxm` is an unofficial project, provided without any warranties or guarantees. By using this software, you accept all risks and liabilities, including potential legal repercussions. This software is intended solely for personal use by individual subscribers to the SiriusXM streaming service. It is not designed for corporate use, commercial redistribution, or any activity that may violate content provider's terms of service.

The authors of `aiosxm` assume no legal responsibility for any use of the software that violates laws, terms of service, or results in legal action. Use of `aiosxm` is entirely at your own risk.

## Install

```
pip install aiosxm
```

Or from a checkout:

```
uv sync
```

## Credentials

The library and the proxy both read `SXM_USERNAME` and `SXM_PASSWORD` from the
environment. The easiest setup is a `.env` file in the repo root (it's gitignored):

```
SXM_USERNAME=you@example.com
SXM_PASSWORD=your-password
```

The proxy loads that automatically. An exported environment variable takes
precedence over the file, and `--username` / `--password` override both.

## Usage

```python
import asyncio
from aiosxm import SxmClient

async def main():
    async with SxmClient() as client:
        channels = await client.get_channels()
        hits1 = next(c for c in channels if c.title == "SiriusXM Hits 1")

        stream = await client.get_stream(hits1.type, hits1.id)
        playlist = await stream.get_playlist()   # HLS media playlist
        key = await stream.get_key()             # raw AES-128 key

asyncio.run(main())
```

Live track metadata comes from one cached feed covering every channel, so it's a
single request no matter how many channels you're watching:

```python
now_playing = await client.get_now_playing_all()
current = now_playing[hits1.id]
print(current.artist, "-", current.title)   # Noah Kahan - Orbiter
print(current.show)                          # The Weekend Countdown
print(current.image_url(300, 300))           # album art
```

Walking the catalog is ~24 sequential requests, so `get_channels()` caches its
result for six hours and everything derived from it — the library, genres, search
by genre — is served from that. Pass `refresh=True` to rebuild it early.

Channel artwork is served through SiriusXM's image-transform service, so you can
request whatever size you need:

```python
hits1.image_url("tile", "1x1", (300, 300))
hits1.image_url("background", "16x9", (1280, 720))
```

### Proxy

Ordinary HLS players won't send the `Authorization` header SiriusXM requires for
playlist and key requests. The bundled proxy holds the session and re-serves the
stream at URLs any player can consume:

```
uv run aiosxm-proxy
```

(`python -m aiosxm` and `python -m aiosxm.proxy` both work too.)

Open <http://127.0.0.1:8080/> for a test console: browse channels with live
now-playing, click to play, and inspect the raw API responses. Safari uses native
HLS; other browsers use hls.js, and `?hlsjs=1` forces the hls.js path.

Embedded webviews are not supported — use a standalone browser.

| Route | Purpose |
| --- | --- |
| `GET /` | Test console (web UI) |
| `GET /channels` | Full channel list as JSON (`?refresh=1` rebuilds the cache) |
| `GET /entitlements` | Whether the account can play |
| `GET /stream/{type}/{id}/info` | Bitrates this channel actually offers |
| `GET /library` | Channels saved to the account's library |
| `POST /library/{type}/{id}` | Add an item to the library |
| `DELETE /library/{type}/{id}` | Remove an item from the library |
| `GET /artist-stations` | Library stations, or `?q=` to search for an artist's |
| `GET /stream/{type}/{id}/tracks` | Queued tracks (`?count=30` to pull more) |
| `GET /search?q=` | Search the catalog |
| `GET /shows?q=` | Find shows and podcasts |
| `GET /podcasts/{show_id}/episodes` | Episodes of a podcast, with durations |
| `GET /now-playing` | Live track metadata for every channel |
| `GET /now-playing/{channel_id}` | Live track metadata for one channel |
| `GET /stream/{type}/{id}/playlist.m3u8` | HLS playlist (add `?bitrate=96k` to pick a bitrate) |
| `GET /stream/{type}/{id}/key` | AES-128 decryption key |

```
ffplay http://127.0.0.1:8080/stream/channel-linear/<channel-id>/playlist.m3u8
```

Segments are served through the proxy by default. Browsers require it — the CDN
only sends `Access-Control-Allow-Origin: https://www.siriusxm.com` — and it's
marginally faster anyway, since the proxy reuses a warm connection pool instead
of doing a fresh TLS handshake per segment.

SiriusXM's live playlists carry the whole ~5 hour rewind buffer (1845 segments),
which some players stall on. The proxy trims to a few minutes of lead by
default; `?window=N` sets the segment count and `?window=0` serves the untrimmed
playlist. Segments and keys also honour `Range` requests, which some players
require.

Add `?absolute=1` to point segments straight at the CDN, so audio goes
client-to-CDN and never passes through the proxy. Those URLs carry a signed
token, so they need no auth header. Both modes work with ffmpeg.

### Editing the library

Adding and removing writes to the real account — the change shows up in the
SiriusXM app too:

```python
await client.add_to_library("channel-linear", channel.id)
await client.remove_from_library("artist-station", station.id)
```

Anything with an id works: channels, artist stations, shows, teams. Over the
proxy these are `POST` and `DELETE` on `/library/{type}/{id}`, deliberately not
`GET`, so a crawler can't reshape someone's library by following links.

### Discovery

There's no single catalog call that returns everything, so discovery is assembled
from three sources: the channel catalog (genres), search (podcasts, shows, artist
stations), and the account's library.

```python
genres = await client.get_genres()              # {"Rock": 89, "Jazz": 10, ...}
jazz = await client.get_channels_by_genre("Jazz")

show = (await client.search_shows("SmartLess"))[0]
for episode in await client.get_episodes(show.id):
    print(episode.title, episode.duration, episode.air_date)
```

Genres come from the cached channel list rather than an extra request — the API
accepts a `filterId` parameter but ignores it server-side, so filtering has to
happen client-side regardless.

The proxy exposes the same thing as a walkable tree, rooted at `GET /browse`.
Every node returns objects with `id`, `title`, `type` and — for folders — a
`path` to the next level, so a consumer can walk the whole catalog without
knowing anything about SiriusXM's own layout.

### Browsing the catalog graph

Container entities all share one shape: an entity has a page of *named
containers*, each holding child entities. A team's `live`, a show's `aod`, a
talent's `epg` — same mechanism everywhere, so two calls walk the whole catalog:

```python
containers = await client.get_containers("talent", talent_id)
# {'aod', 'epg', 'on-air', 'vod', 'show-podcasts', ...}

episodes = await client.browse_entity("talent", talent_id, "aod")
```

`browse_entity` raises `KeyError` for a section that doesn't exist, so "no such
section" is distinguishable from "section is empty".

Over the proxy the same two steps are `GET /browse/{type}/{id}` and
`GET /browse/{type}/{id}/{container}`. Children come back with a `browse_path`
for descending further and a `play_path` when something is playable — including
entities that aren't playable themselves but name a channel that is.

### Sports

Games aren't playable entities. A team page lists them in two containers — one
for what's on air, one for what's scheduled — and each entry names the *channel*
carrying the broadcast, which is what you tune:

```python
team = (await client.get_teams("Chicago Bears"))[0]
for game in await client.get_team_broadcasts(team["id"]):
    print(game.is_live, game.title, game.coverage)   # coverage: HOME / AWAY

stream = await client.get_broadcast_stream(game)     # tunes the carrying channel
```

A scheduled game usually offers two feeds (home and away commentary) on separate
channels; each is returned as its own `Broadcast` so a caller can choose. Over
the proxy: `GET /teams?q=bears` then `GET /teams/{id}/broadcasts`, where each
entry carries a ready-to-play `play_path`.

### Content types

SiriusXM exposes fifteen entity types. Only five are directly playable; the rest
are containers you browse through to reach one.

| Type | Playable | Shape |
| --- | --- | --- |
| `channel-linear` | yes | HLS, one stream |
| `channel-xtra` | yes | HLS, mirrored streams |
| `artist-station` | yes | queue of unencrypted MP4 tracks |
| `episode-podcast` | yes | single MP3, off-CDN |
| `episode-audio` | yes | HLS, on-demand |
| `episode-video` | partial | HLS, variants by resolution (audio-only use is untested) |
| `show`, `show-podcast` | no | containers — list their episodes |
| `talent`, `team`, `league`, `genre`, `curated-grouping` | no | containers |
| `event`, `episode-linear` | no | games and airings — carry a `play` action naming a channel |

Everything playable is reachable with `client.get_stream(type, id)`; check
`is_track_queue` and `is_progressive` to know which accessor to use.

### Artist stations

A library can contain artist stations as well as channels. They are personalised
queues of individual tracks rather than broadcast channels, so they don't appear
in the channel catalog and have to be fetched separately:

SiriusXM publishes no catalog of artist stations, so they can only be found by
name — either from your library, or by searching:

```python
stations = await client.get_library_artist_stations()
stations = await client.search_artist_stations("Frank Sinatra")
# -> Frank Sinatra, Frank Sinatra (Holiday)

stream = await client.get_stream("artist-station", stations[0].id)

stream.is_track_queue     # True
for track in stream.tracks:
    print(track.artist, "-", track.title, track.url)
```

Each track is an unencrypted MP4, so `track.url` plays directly.

The API returns three tracks at a time plus a cursor, and feeding the cursor back
returns the next three — verified over 20 consecutive pages with no repeats. So a
consumer can keep a queue topped up indefinitely:

```python
async for track in stream.iter_tracks(limit=50):
    enqueue(track.url)
```

`iter_tracks()` de-duplicates and stops if a station does start repeating, so it
terminates rather than spinning. Over the proxy, `?count=` does the same:
`GET /stream/artist-station/{id}/tracks?count=30` (capped at 100 per request).

A player has to advance the queue itself — each track is a separate file, so
there is no continuous stream to keep pulling. The test console does this on the
audio element's `ended` event, topping the queue back up as it drains.

### On-demand

Podcasts are not in the channel listing, so they have to be found by search:

```python
results = await client.search("SmartLess")
show = results["show-podcast"][0]
episodes = await client.get_podcast_episodes(show["id"])

stream = await client.get_stream("episode-podcast", episodes[0]["id"])
stream.is_progressive   # True - episodes are a single MP3, not HLS
stream.content_url      # play this directly
```

Live channels are HLS; on-demand episodes are a plain media file. Check
`is_progressive` before choosing between `get_playlist()` and `content_url`. The
proxy's playlist route handles both, redirecting to the file for episodes.

### Embedding the proxy

To mount the playback routes inside an existing aiohttp application, use
`make_routes` — the handlers close over the client, so the host application needs
no aiosxm-specific state:

```python
from aiosxm.proxy import make_routes

routes = make_routes(client, stream_only=True)   # [(method, path, handler), ...]
for method, path, handler in routes:
    app.router.add_route(method, path, handler)
```

## Tests

```
uv sync --dev
uv run pytest
```

The JSON under `tests/fixtures/` is captured from the live API and sanitized
(tokens redacted, account ids zeroed), so the parsing tests run against payload
shapes that really occurred rather than ones invented to match the code. Nothing
in the default suite touches the network.

There is also an opt-in smoke test that runs against the real API. It is what
notices when SiriusXM changes a payload shape — the mocked tests can't:

```
uv run pytest --live      # needs SXM_USERNAME / SXM_PASSWORD
```

## Notes

- An active subscription is required. An account whose subscription has lapsed
  still authenticates, but every channel reports `unentitled` and playback raises
  `NotEntitledError`. Check with `await client.is_entitled()`.
- The channel list is paginated 30 items at a time regardless of the requested
  page size, and the reported total is unreliable, so `get_channels()` walks until
  a page comes back empty. Expect ~712 channels (linear plus "xtra").
- A library entry is not always a channel. Artist stations sit outside the
  catalog, so `get_library_channels()` alone will silently under-report what the
  SiriusXM app shows; pair it with `get_library_artist_stations()`.
- On-demand episodes are hosted off SiriusXM's own CDN (Simplecast and similar),
  are unencrypted, and need no auth header.
- Transient failures (5xx, 429, timeouts) are retried twice with exponential
  backoff. A 4xx is treated as an answer and returned immediately.
- The access token is refreshed with the refresh-token cookie rather than a full
  re-login, falling back to a full login if that is refused.
- Both `channel-linear` and `channel-xtra` play identically (verified live).
- Now-playing data covers ~500 channels and needs no authentication, so it can be
  polled cheaply. Cuts flagged `is_ad` are commercials rather than music.
- The all-channels listing depends on a curated-grouping id that SiriusXM could
  move. `SxmClient.discover_all_channels_ids()` re-reads it from the page
  descriptor, the same way the web player does.

## Acknowledgements

The following projects were used as examples:

- [sxm-client](https://github.com/AngellusMortis/sxm-client)
- [SXM Commander Server for SiriusXM](https://github.com/UiharuKazari2008/lizumi-sxm-rest-hander)
