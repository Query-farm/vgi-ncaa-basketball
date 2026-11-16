"""Serve the recorded ESPN and ncaa-api fixtures over HTTP for the SQL tests.

``ci/run-integration.sh`` starts this server, points the worker's base-URL
environment variables at it, and runs ``test/sql`` through DuckDB, so the
suite is hermetic. Prints ``PORT:<n>`` once listening.
"""

from __future__ import annotations

import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from fixture_server import FixtureHandler  # noqa: E402


def main() -> None:
    """Serve until killed."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    print(f"PORT:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
