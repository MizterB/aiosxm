"""Shared test fixtures.

The JSON under `fixtures/` is captured from the live SiriusXM API and sanitized
(tokens redacted, account ids zeroed), so the parsing tests run against payload
shapes that really occurred rather than ones invented to match the code.
"""

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from aiosxm import SxmClient
from aiosxm.const import API_BASE

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a captured API response."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def channels_page() -> dict:
    """One page of the all-channels container."""
    return load_fixture("channels_page")


@pytest.fixture
def channel_item(channels_page: dict) -> dict:
    """A single channel item from the container."""
    return channels_page["container"]["sets"][0]["items"][0]


@pytest.fixture
def lookaround() -> dict:
    """The live now-playing feed."""
    return load_fixture("lookaround")


@pytest.fixture
def tune_source() -> dict:
    """A tuneSource response for a linear channel."""
    return load_fixture("tune_source")


@pytest.fixture
def master_playlist() -> str:
    """The master (multi-bitrate) HLS playlist."""
    return load_fixture("master_playlist_text")


@pytest.fixture
def auth_urls() -> dict[str, str]:
    """The URLs that make up the authentication chain."""
    return {
        "device": f"{API_BASE}/device/v1/devices",
        "anonymous": f"{API_BASE}/session/v1/sessions/anonymous",
        "status": f"{API_BASE}/identity/v1/identities/status",
        "password": f"{API_BASE}/identity/v1/identities/authenticate/password",
        "authenticated": f"{API_BASE}/session/v1/sessions/authenticated",
    }


def mock_auth(mocked: Any, *, expires_at: str = "2099-01-01T00:00:00.000000Z") -> None:
    """Register a successful authentication chain on an aioresponses mock."""
    import re

    mocked.post(f"{API_BASE}/device/v1/devices", payload={"grant": "device-grant"})
    mocked.post(
        f"{API_BASE}/session/v1/sessions/anonymous",
        payload={"accessToken": "anon-token"},
    )
    mocked.get(
        re.compile(rf"{re.escape(API_BASE)}/identity/v1/identities/status.*"),
        payload={"hasPassword": True},
    )
    mocked.post(
        f"{API_BASE}/identity/v1/identities/authenticate/password",
        payload={"grant": "auth-grant"},
    )
    mocked.post(
        f"{API_BASE}/session/v1/sessions/authenticated",
        payload={
            "accessToken": "access-token",
            "accessTokenExpiresAt": expires_at,
            "entitlementHash": "hash123",
        },
    )


@pytest.fixture
async def client() -> AsyncGenerator[SxmClient]:
    """An un-connected client with dummy credentials, closed after the test."""
    instance = SxmClient("user@example.com", "hunter2")
    yield instance
    await instance.disconnect()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the --live flag."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run smoke tests against the real SiriusXM API (needs credentials)",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the live marker."""
    config.addinivalue_line("markers", "live: hits the real SiriusXM API")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip live tests unless --live is passed and credentials are available.

    Two gates on purpose: --live keeps them out of CI and out of an ordinary
    local run, and the credential check turns a confusing auth failure into a
    clear skip.
    """
    if not config.getoption("--live"):
        skip = pytest.mark.skip(reason="needs --live")
    elif not (os.getenv("SXM_USERNAME") and os.getenv("SXM_PASSWORD")):
        skip = pytest.mark.skip(reason="needs SXM_USERNAME and SXM_PASSWORD")
    else:
        return

    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
