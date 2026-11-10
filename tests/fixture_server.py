"""A tiny local HTTP server that answers ESPN and ncaa-api paths from recorded fixtures."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEAGUES = {"mens-college-basketball": "mens", "womens-college-basketball": "womens"}
SPORTS = {"basketball-men": "mens", "basketball-women": "womens"}
SUMMARIES = {"401920686": "mens_summary_401920686.json", "401918925": "womens_summary_401918925.json"}

_SITE = re.compile(r"^/apis/site/v2/sports/basketball/(?P<slug>[a-z-]+)/(?P<tail>.+)$")
_STANDINGS = re.compile(r"^/apis/v2/sports/basketball/(?P<slug>[a-z-]+)/standings$")
_PLAYERS = re.compile(r"^/apis/common/v3/sports/basketball/(?P<slug>[a-z-]+)/statistics/byathlete$")
_POLL = re.compile(
    r"^/v2/sports/basketball/leagues/(?P<slug>[a-z-]+)/seasons/(?P<season>\d+)"
    r"/types/2/weeks/(?P<week>\d+)/rankings/(?P<poll>\d+)$"
)
_NET = re.compile(r"^/rankings/(?P<sport>basketball-(?:men|women))/d1/[a-z-]+-net-rankings$")
_STATS = re.compile(r"^/stats/(?P<sport>basketball-(?:men|women))/d1/current/(?P<category>\w+)/(?P<id>\d+)$")
_STATS_INDEX = re.compile(r"^/stats/(?P<sport>basketball-(?:men|women))/d1$")
_BRACKET = re.compile(r"^/brackets/(?P<sport>basketball-(?:men|women))/d1/(?P<year>\d+)$")


def resolve(path: str, query: dict[str, list[str]]) -> str | None:
    """Return the fixture file name for one request path, or None for a 404."""
    if match := _SITE.match(path):
        league, tail = LEAGUES.get(match["slug"]), match["tail"]
        if tail in {"scoreboard", "rankings", "teams"}:
            return f"{league}_{tail}.json"
        if tail == "summary":
            return SUMMARIES.get(query.get("event", [""])[0])
        if league == "mens" and tail == "teams/150/schedule":
            return "mens_schedule_150.json"
        if league == "mens" and tail == "teams/150/roster":
            return "mens_roster_150.json"
        return None
    if match := _STANDINGS.match(path):
        return f"{LEAGUES.get(match['slug'])}_standings.json"
    if match := _PLAYERS.match(path):
        return f"{LEAGUES.get(match['slug'])}_player_stats.json"
    if (match := _POLL.match(path)) and LEAGUES.get(match["slug"]) == "mens":
        if (match["season"], match["week"], match["poll"]) == ("2026", "5", "1"):
            return "mens_core_rankings_2026_w5_ap.json"
        return None
    if match := _NET.match(path):
        return f"ncaa_{SPORTS[match['sport']]}_net.json"
    if (match := _STATS.match(path)) and SPORTS[match["sport"]] == "mens":
        return f"ncaa_mens_stat_{match['category']}_{match['id']}.json"
    if match := _STATS_INDEX.match(path):
        return f"ncaa_{SPORTS[match['sport']]}_stats_index.json"
    if match := _BRACKET.match(path):
        return f"ncaa_{SPORTS[match['sport']]}_bracket_{match['year']}.json"
    return None


class FixtureHandler(BaseHTTPRequestHandler):
    """Serve fixture JSON with a short Cache-Control, like ESPN does."""

    requests: list[str] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        type(self).requests.append(self.path)
        name = resolve(parsed.path, parse_qs(parsed.query))
        file = FIXTURES / name if name else None
        if file is None or not file.exists():
            body = json.dumps({"message": "Resource not found"}).encode()
            self.send_response(404)
        else:
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Cache-Control", "max-age=11")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def serve() -> Iterator[str]:
    """Run the fixture server on an ephemeral port and yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
