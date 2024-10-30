"""Constants for the aiosxm package."""

BITRATE_256 = "256k"
BITRATE_96 = "96k"
BITRATE_64 = "64k"
BITRATE_32 = "32k"

DEFAULT_REQUEST_TIMEOUT: int = 10

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
