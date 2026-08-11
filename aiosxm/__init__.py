"""An asynchronous Python library for SiriusXM."""

from importlib.metadata import PackageNotFoundError, version

from aiosxm.client import SxmClient
from aiosxm.exceptions import (
    AuthenticationError,
    NotEntitledError,
    RequestError,
    SxmError,
)
from aiosxm.models import (
    ArtistStation,
    Broadcast,
    Channel,
    Episode,
    Genre,
    Image,
    NowPlaying,
    SearchResults,
    Show,
    Talent,
    Track,
)
from aiosxm.stream import SxmStream

try:
    __version__ = version("aiosxm")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"

__all__ = [
    "ArtistStation",
    "AuthenticationError",
    "Broadcast",
    "Channel",
    "Episode",
    "Genre",
    "Image",
    "NotEntitledError",
    "NowPlaying",
    "RequestError",
    "SearchResults",
    "Show",
    "SxmClient",
    "SxmError",
    "SxmStream",
    "Talent",
    "Track",
    "__version__",
]
