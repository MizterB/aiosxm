"""Command-line entry point for the proxy server."""

import argparse
import logging
import os
from pathlib import Path

from aiohttp import web

from aiosxm.exceptions import AuthenticationError
from aiosxm.proxy import create_app, load_dotenv

_LOGGER = logging.getLogger(__name__)


def main() -> None:
    """Run the proxy server."""
    parser = argparse.ArgumentParser(
        prog="aiosxm-proxy",
        description="Serve SiriusXM streams over plain HLS.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8080, help="port to bind (default: %(default)s)")
    parser.add_argument("--username", help="SiriusXM username (default: $SXM_USERNAME or .env)")
    parser.add_argument("--password", help="SiriusXM password (default: $SXM_PASSWORD or .env)")
    parser.add_argument("--env-file", type=Path, help="path to a .env file holding the credentials")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    load_dotenv(args.env_file)

    username = args.username or os.getenv("SXM_USERNAME")
    password = args.password or os.getenv("SXM_PASSWORD")
    if not username or not password:
        parser.exit(
            2,
            "No SiriusXM credentials found.\n"
            "Provide them with --username/--password, export SXM_USERNAME and "
            "SXM_PASSWORD, or put them in a .env file:\n\n"
            "    SXM_USERNAME=you@example.com\n"
            "    SXM_PASSWORD=your-password\n",
        )

    _LOGGER.info("Test console will be at http://%s:%s/", args.host, args.port)
    try:
        web.run_app(create_app(username, password), host=args.host, port=args.port)
    except AuthenticationError as err:
        parser.exit(1, f"SiriusXM login failed: {err}\n")
