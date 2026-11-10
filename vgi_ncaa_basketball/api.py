"""Small, read-only, cached client for ESPN's college basketball endpoints and ncaa-api.

Every outbound request in the package goes through :func:`get_json`. It applies
timeouts, bounded retries with backoff for 429 and transient 5xx responses,
``Retry-After``, a total query deadline, a per-process concurrency bound, a
polite minimum interval for the ncaa-api proxy, and an in-process TTL cache so
repeated SQL scans do not re-fetch the same upstream document.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from vgi_ncaa_basketball import __version__

USER_AGENT = f"vgi-ncaa-basketball/{__version__} (+https://github.com/Query-farm/vgi-ncaa-basketball)"

# Each upstream host can be pointed elsewhere (a self-hosted ncaa-api, or a
# local fixture server in hermetic tests). Never set these from user input.
SOURCES: dict[str, tuple[str, str]] = {
    "site": ("NCAAB_ESPN_SITE_URL", "https://site.api.espn.com"),
    "web": ("NCAAB_ESPN_WEB_URL", "https://site.web.api.espn.com"),
    "core": ("NCAAB_ESPN_CORE_URL", "https://sports.core.api.espn.com"),
    "ncaa": ("NCAAB_NCAA_API_URL", "https://ncaa-api.henrygd.me"),
}
NCAA_API_KEY_ENV = "NCAAB_NCAA_API_KEY"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
MAX_RETRY_DELAY = 30.0
QUERY_TIMEOUT = 120.0
MAX_CONCURRENT_REQUESTS = 4
# The public ncaa-api host allows 3 requests per second per IP.
NCAA_MIN_INTERVAL = 0.35
MAX_CACHE_ENTRIES = 96
PLAYER_PAGE_SIZE = 1_000
MAX_PLAYER_PAGES = 20

# Minimum cache lifetimes (seconds) by data class. ESPN advertises very short
# max-age values (often 5-15 seconds); these floors keep repeated scans polite.
TTL_LIVE = 30
TTL_STANDARD = 300
TTL_SLOW = 3_600

logger = logging.getLogger(__name__)
_request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_ncaa_lock = threading.Lock()
_ncaa_last_request = 0.0
_cache_lock = threading.Lock()
_cache: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = OrderedDict()
_client_lock = threading.Lock()
_client: httpx.Client | None = None

_MAX_AGE = re.compile(r"(?:^|[,\s])max-age=(\d+)", re.IGNORECASE)


class UpstreamError(RuntimeError):
    """A failed response from ESPN or ncaa-api."""

    def __init__(self, source: str, status: int, body: str) -> None:
        name = "ncaa-api" if source == "ncaa" else "ESPN"
        super().__init__(f"{name} returned HTTP {status}: {body[:300]}")
        self.source = source
        self.status = status


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """A decoded JSON document plus the cache lifetime that remains for it."""

    payload: Any
    max_age: int


@dataclass(frozen=True, slots=True)
class League:
    """Upstream identifiers for one NCAA Division I basketball competition."""

    key: str
    espn_slug: str
    ncaa_sport: str
    net_slug: str
    title: str
    adjective: str


MENS = League(
    key="mens",
    espn_slug="mens-college-basketball",
    ncaa_sport="basketball-men",
    net_slug="ncaa-mens-basketball-net-rankings",
    title="NCAA Men's Basketball",
    adjective="men's",
)
WOMENS = League(
    key="womens",
    espn_slug="womens-college-basketball",
    ncaa_sport="basketball-women",
    net_slug="ncaa-womens-basketball-net-rankings",
    title="NCAA Women's Basketball",
    adjective="women's",
)
LEAGUES = (MENS, WOMENS)


def base_url(source: str) -> str:
    """Return the root URL of one upstream source, honoring its override variable."""
    variable, default = SOURCES[source]
    return os.environ.get(variable, default).rstrip("/")


def clear_cache() -> None:
    """Drop every cached upstream document (used by tests)."""
    with _cache_lock:
        _cache.clear()


def _shared_client() -> httpx.Client:
    """Return one process-wide connection pool; httpx clients are thread-safe."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        return _client


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return a bounded Retry-After delay or exponential backoff with jitter."""
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = -1.0
            if delay >= 0:
                return min(delay, MAX_RETRY_DELAY)
    base = min(0.25 * 2**attempt, MAX_RETRY_DELAY)
    return min(base + random.uniform(0, base * 0.25), MAX_RETRY_DELAY)


def _throttle_ncaa() -> None:
    """Keep requests to the ncaa-api proxy under its published per-IP rate."""
    global _ncaa_last_request
    with _ncaa_lock:
        wait = NCAA_MIN_INTERVAL - (time.monotonic() - _ncaa_last_request)
        if wait > 0:
            time.sleep(wait)
        _ncaa_last_request = time.monotonic()


def _headers(source: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    key = os.environ.get(NCAA_API_KEY_ENV)
    if source == "ncaa" and key:
        headers["x-ncaa-key"] = key
    return headers


def _request(source: str, path: str, params: Mapping[str, str | int], *, deadline: float) -> httpx.Response:
    """Issue one GET, retrying transport failures, 429 and transient 5xx responses."""
    url = f"{base_url(source)}/{path.lstrip('/')}"
    client = _shared_client()
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"upstream query exceeded {QUERY_TIMEOUT:g} seconds")
        started = time.monotonic()
        try:
            if not _request_slots.acquire(timeout=remaining):
                raise TimeoutError(f"upstream query exceeded {QUERY_TIMEOUT:g} seconds")
            try:
                if source == "ncaa":
                    _throttle_ncaa()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"upstream query exceeded {QUERY_TIMEOUT:g} seconds")
                response = client.get(
                    url,
                    params=params,
                    headers=_headers(source),
                    timeout=httpx.Timeout(min(30.0, remaining), connect=min(10.0, remaining)),
                )
            finally:
                _request_slots.release()
        except httpx.TransportError:
            logger.warning(
                "upstream_transport_error source=%s path=%s attempt=%d duration_ms=%d",
                source,
                path,
                attempt + 1,
                round((time.monotonic() - started) * 1_000),
            )
            if attempt + 1 == MAX_ATTEMPTS:
                raise
            time.sleep(min(_retry_delay(None, attempt), max(0.0, deadline - time.monotonic())))
            continue
        logger.debug(
            "upstream_response source=%s path=%s status=%d attempt=%d duration_ms=%d",
            source,
            path,
            response.status_code,
            attempt + 1,
            round((time.monotonic() - started) * 1_000),
        )
        if response.status_code not in RETRYABLE_STATUSES or attempt + 1 == MAX_ATTEMPTS:
            return response
        delay = min(_retry_delay(response, attempt), max(0.0, deadline - time.monotonic()))
        logger.warning(
            "upstream_retry source=%s path=%s status=%d attempt=%d delay_seconds=%.3f",
            source,
            path,
            response.status_code,
            attempt + 1,
            delay,
        )
        time.sleep(delay)
    raise AssertionError("retry loop exited without a response")


def get_json(
    source: str,
    path: str,
    params: Mapping[str, str | int] | None = None,
    *,
    ttl: int = TTL_STANDARD,
    deadline: float | None = None,
) -> JsonResponse:
    """Fetch one JSON document, serving it from the in-process cache while fresh.

    The cache lifetime is the larger of ``ttl`` and the upstream ``max-age``,
    so a source that asks to be cached longer (ncaa-api sends 30 minutes) is
    honored, while ESPN's few-second values do not cause re-fetch storms.
    """
    clean = {key: str(value) for key, value in (params or {}).items()}
    key = (f"{source}:{path}", tuple(sorted(clean.items())))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            _cache.move_to_end(key)
            return JsonResponse(hit[1], max(1, int(hit[0] - now)))
    response = _request(source, path, clean, deadline=deadline or time.monotonic() + QUERY_TIMEOUT)
    if not response.is_success:
        raise UpstreamError(source, response.status_code, response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError(source, response.status_code, f"invalid JSON: {response.text}") from exc
    match = _MAX_AGE.search(response.headers.get("cache-control", ""))
    lifetime = max(ttl, int(match.group(1)) if match else 0)
    with _cache_lock:
        _cache[key] = (time.monotonic() + lifetime, payload)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return JsonResponse(payload, lifetime)


def _require_dict(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{what} response is not a JSON object")
    return payload


def _site(league: League, tail: str) -> str:
    return f"apis/site/v2/sports/basketball/{league.espn_slug}/{tail}"


# --------------------------------------------------------------------------- ESPN


def fetch_scoreboard(league: League, game_date: date | None, group: str | int = 50) -> JsonResponse:
    """Fetch one day's scoreboard; ``group`` 50 is all of Division I, otherwise a conference id."""
    params: dict[str, str | int] = {"groups": group, "limit": 1_000}
    if game_date is not None:
        params["dates"] = game_date.strftime("%Y%m%d")
    response = get_json("site", _site(league, "scoreboard"), params, ttl=TTL_LIVE)
    payload = _require_dict(response.payload, "ESPN scoreboard")
    if not isinstance(payload.get("events"), list):
        raise ValueError("ESPN scoreboard response has no events array")
    return response


def fetch_summary(league: League, event_id: int) -> JsonResponse:
    """Fetch the game summary (box score and play-by-play) for one event."""
    if event_id <= 0:
        raise ValueError("event_id must be positive")
    response = get_json("site", _site(league, "summary"), {"event": event_id}, ttl=TTL_LIVE)
    _require_dict(response.payload, "ESPN summary")
    return response


def fetch_rankings(league: League) -> JsonResponse:
    """Fetch the latest published poll rankings (AP, and Coaches where ESPN carries it)."""
    response = get_json("site", _site(league, "rankings"), ttl=TTL_STANDARD)
    payload = _require_dict(response.payload, "ESPN rankings")
    if not isinstance(payload.get("rankings"), list):
        raise ValueError("ESPN rankings response has no rankings array")
    return response


def fetch_poll(league: League, season: int, week: int, poll_id: int, season_type: int = 2) -> JsonResponse:
    """Fetch one historical weekly poll from ESPN's core API."""
    path = (
        f"v2/sports/basketball/leagues/{league.espn_slug}/seasons/{season}"
        f"/types/{season_type}/weeks/{week}/rankings/{poll_id}"
    )
    response = get_json("core", path, ttl=TTL_SLOW)
    _require_dict(response.payload, "ESPN poll")
    return response


def fetch_standings(league: League, season: int | None = None) -> JsonResponse:
    """Fetch Division I standings for every conference."""
    params: dict[str, str | int] = {}
    if season is not None:
        params["season"] = season
    response = get_json(
        "site", f"apis/v2/sports/basketball/{league.espn_slug}/standings", params, ttl=TTL_STANDARD
    )
    payload = _require_dict(response.payload, "ESPN standings")
    if not isinstance(payload.get("children"), list):
        raise ValueError("ESPN standings response has no conference children")
    return response


def fetch_teams(league: League) -> JsonResponse:
    """Fetch every Division I team."""
    response = get_json("site", _site(league, "teams"), {"groups": 50, "limit": 1_000}, ttl=TTL_SLOW)
    _require_dict(response.payload, "ESPN teams")
    return response


def fetch_team_schedule(league: League, team_id: int, season: int | None, season_type: int) -> JsonResponse:
    """Fetch one team's regular-season (2) or postseason (3) schedule."""
    if team_id <= 0:
        raise ValueError("team_id must be positive")
    params: dict[str, str | int] = {"seasontype": season_type}
    if season is not None:
        params["season"] = season
    response = get_json("site", _site(league, f"teams/{team_id}/schedule"), params, ttl=TTL_LIVE)
    _require_dict(response.payload, "ESPN team schedule")
    return response


def fetch_team_roster(league: League, team_id: int) -> JsonResponse:
    """Fetch one team's current roster."""
    if team_id <= 0:
        raise ValueError("team_id must be positive")
    response = get_json("site", _site(league, f"teams/{team_id}/roster"), ttl=TTL_SLOW)
    _require_dict(response.payload, "ESPN team roster")
    return response


@dataclass(frozen=True, slots=True)
class PlayerStatsPages:
    """Every byathlete page for one season, plus their shared category layout."""

    season: int | None
    categories: list[dict[str, Any]]
    athletes: list[dict[str, Any]]
    max_age: int


def fetch_player_stats(league: League, season: int | None = None) -> PlayerStatsPages:
    """Fetch every page of ESPN's per-athlete season statistics."""
    path = f"apis/common/v3/sports/basketball/{league.espn_slug}/statistics/byathlete"
    deadline = time.monotonic() + QUERY_TIMEOUT
    athletes: list[dict[str, Any]] = []
    categories: list[dict[str, Any]] = []
    max_ages: list[int] = []
    resolved_season: int | None = season
    for page in range(1, MAX_PLAYER_PAGES + 1):
        params: dict[str, str | int] = {"limit": PLAYER_PAGE_SIZE, "page": page}
        if season is not None:
            params["season"] = season
        response = get_json("web", path, params, ttl=TTL_STANDARD, deadline=deadline)
        payload = _require_dict(response.payload, "ESPN player statistics")
        max_ages.append(response.max_age)
        if page == 1:
            categories = [value for value in payload.get("categories") or [] if isinstance(value, dict)]
            requested = payload.get("requestedSeason")
            if isinstance(requested, dict) and isinstance(requested.get("year"), int):
                resolved_season = requested["year"]
        rows = payload.get("athletes")
        if not isinstance(rows, list):
            raise ValueError("ESPN player statistics response has no athletes array")
        athletes.extend(value for value in rows if isinstance(value, dict))
        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        pages = pagination.get("pages") if isinstance(pagination.get("pages"), int) else page
        if not rows or page >= pages:
            return PlayerStatsPages(resolved_season, categories, athletes, min(max_ages))
    raise RuntimeError(f"ESPN player statistics exceeded {MAX_PLAYER_PAGES} pages")


# ----------------------------------------------------------------------- ncaa-api


def fetch_net_rankings(league: League) -> JsonResponse:
    """Fetch the official NCAA NET rankings through ncaa-api."""
    response = get_json("ncaa", f"rankings/{league.ncaa_sport}/d1/{league.net_slug}", ttl=TTL_STANDARD)
    payload = _require_dict(response.payload, "ncaa-api NET rankings")
    if not isinstance(payload.get("data"), list):
        raise ValueError("ncaa-api NET rankings response has no data array")
    return response


@dataclass(frozen=True, slots=True)
class StatLeaderPages:
    """Concatenated pages of one official NCAA statistical leaderboard."""

    title: str | None
    updated: str | None
    rows: list[dict[str, Any]]
    max_age: int


def fetch_stat_leaders(league: League, stat_id: int, category: str, pages: int) -> StatLeaderPages:
    """Fetch the first ``pages`` pages (50 rows each) of an NCAA statistical leaderboard."""
    if category not in {"individual", "team"}:
        raise ValueError("category must be 'individual' or 'team'")
    if stat_id <= 0:
        raise ValueError("stat_id must be positive")
    path = f"stats/{league.ncaa_sport}/d1/current/{category}/{stat_id}"
    deadline = time.monotonic() + QUERY_TIMEOUT
    rows: list[dict[str, Any]] = []
    title = updated = None
    max_ages: list[int] = []
    for page in range(1, pages + 1):
        response = get_json("ncaa", path, {"page": page} if page > 1 else None, deadline=deadline)
        payload = _require_dict(response.payload, "ncaa-api statistics")
        max_ages.append(response.max_age)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("ncaa-api statistics response has no data array")
        if page == 1:
            title, updated = payload.get("title"), payload.get("updated")
        rows.extend(value for value in data if isinstance(value, dict))
        total = payload.get("pages") if isinstance(payload.get("pages"), int) else page
        if page >= total:
            break
    return StatLeaderPages(title, updated, rows, min(max_ages))


def fetch_stat_categories(league: League) -> JsonResponse:
    """Fetch the index of official NCAA individual and team leaderboards."""
    response = get_json("ncaa", f"stats/{league.ncaa_sport}/d1", ttl=TTL_SLOW)
    _require_dict(response.payload, "ncaa-api statistics index")
    return response


def fetch_bracket(league: League, season: int) -> JsonResponse:
    """Fetch the NCAA tournament bracket for the championship played in ``season``."""
    response = get_json("ncaa", f"brackets/{league.ncaa_sport}/d1/{season}", ttl=TTL_STANDARD)
    payload = _require_dict(response.payload, "ncaa-api bracket")
    if not isinstance(payload.get("championships"), list):
        raise ValueError("ncaa-api bracket response has no championships array")
    return response
