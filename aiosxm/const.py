"""Constants for the aiosxm package."""

BITRATE_256 = "256k"
BITRATE_96 = "96k"
BITRATE_64 = "64k"
BITRATE_32 = "32k"

BITRATES = (BITRATE_256, BITRATE_96, BITRATE_64, BITRATE_32)

DEFAULT_REQUEST_TIMEOUT: int = 10

# Refresh the access token this many seconds before it actually expires, so a
# request never goes out holding a token that dies mid-flight.
TOKEN_EXPIRY_MARGIN: int = 300

API_BASE = "https://api.edge-gateway.siriusxm.com"

# The refresh token arrives as a cookie rather than in the session payload, and
# is presented as the bearer token when refreshing. It lasts ~90 days.
REFRESH_TOKEN_COOKIE = "sxm-refresh-token"  # noqa: S105  (a cookie name, not a secret)

# Live now-playing metadata for every channel, published as a single cached feed.
# It needs no authentication, which is why it can be polled cheaply.
LOOKAROUND_URL = (
    "https://lookaround-cache-prod.streaming.siriusxm.com/playbackservices/v1/live/lookAround"
)

# The "All channels" curated grouping. Both ids come from the page descriptor at
# page/v1/page/curated-grouping/{ALL_CHANNELS_ENTITY_ID}, which is what the web
# player itself reads. They're stable, but see SxmClient.discover_all_channels_ids()
# for re-resolving them if SiriusXM ever moves the grouping.
ALL_CHANNELS_ENTITY_ID = "403ab6a5-d3c9-4c2a-a722-a94a6a5fd056"
ALL_CHANNELS_CONTAINER_ID = "3JoBfOCIwo6FmTpzM1S2H7"

# How many channels to pull per request when walking the channel list. The API
# caps a page at 30 items regardless of what `maxResponses` asks for.
CHANNEL_PAGE_SIZE = 30

# How long a cached channel list stays fresh. The catalog changes rarely, but a
# long-running process should still pick up additions without a restart.
CHANNEL_CACHE_TTL: int = 6 * 60 * 60

# Re-tune this many seconds before a stream's signed URLs expire. The CDN grants
# a short window (~5 minutes), so a stream cached for hours would otherwise start
# returning 403 with no way to recover.
STREAM_EXPIRY_MARGIN: int = 60

# Safety bound on the channel walk, so a server that keeps returning pages can't
# loop forever. The full catalog is ~712 channels.
MAX_CHANNEL_OFFSET = 5000

SXM_REQUEST_HEADERS = {
    "Accept": "application/json; charset=utf-8",
    "Accept-Language": "en-US,en;q=0.9",
    "Baggage": "sentry-environment=prod,sentry-release=release-sxm-player-7.0",
    "Content-Type": "application/json; charset=UTF-8",
    "Dnt": "1",
    "Origin": "https://www.siriusxm.com",
    "Referer": "https://www.siriusxm.com/",
    "Sec-Ch-Ua": "'Not_A Brand';v='8', 'Chromium';v='120', 'Microsoft Edge';v='120'",
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": "'macOS'",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",  # noqa: E501
    "X-Sxm-Clock": "[0,0]",
    "X-Sxm-Platform": "browser",
    "X-Sxm-Tenant": "sxm",
}

SXM_DEVICE_PAYLOAD = {
    "devicePlatform": "web-desktop",
    "deviceAttributes": {
        "browser": {
            "browserVersion": "120.0.0.0",
            "browser": "Edge",
            "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",  # noqa: E501
            "sdk": "web",
            "app": "web",
            "sdkVersion": "120.0.0.0",
            "appVersion": "120.0.0.0",
        },
    },
}
