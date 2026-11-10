"""The single HTTP chokepoint: URLs, headers, caching, retries, throttling and paging."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from vgi_ncaa_basketball import api


@pytest.fixture
def mock(monkeypatch: pytest.MonkeyPatch):
    """Install a MockTransport client and return the list of requests it saw."""
    seen: list[httpx.Request] = []
    responses: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result = responses.pop(0) if len(responses) > 1 else responses[0]
        return result(request) if callable(result) else result

    monkeypatch.setattr(api, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    for name, (variable, _) in api.SOURCES.items():
        monkeypatch.setenv(variable, f"https://{name}.example.test/")
    return seen, responses


def test_default_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable, _ in api.SOURCES.values():
        monkeypatch.delenv(variable, raising=False)
    assert api.base_url("site") == "https://site.api.espn.com"
    assert api.base_url("ncaa") == "https://ncaa-api.henrygd.me"


def test_scoreboard_query_headers_and_cache(mock) -> None:
    seen, responses = mock
    responses.append(httpx.Response(200, json={"events": []}, headers={"Cache-Control": "max-age=6"}))
    first = api.fetch_scoreboard(api.WOMENS, date(2026, 11, 9))
    second = api.fetch_scoreboard(api.WOMENS, date(2026, 11, 9))
    assert len(seen) == 1, "the second call is served from the in-process cache"
    request = seen[0]
    assert request.url.host == "site.example.test"
    assert request.url.path == "/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard"
    assert dict(request.url.params) == {"groups": "50", "limit": "1000", "dates": "20261109"}
    assert request.headers["user-agent"].startswith("vgi-ncaa-basketball/")
    assert "+https://github.com/Query-farm/vgi-ncaa-basketball" in request.headers["user-agent"]
    assert first.max_age == api.TTL_LIVE and 1 <= second.max_age <= api.TTL_LIVE


def test_longer_upstream_max_age_is_honored(mock) -> None:
    seen, responses = mock
    responses.append(
        httpx.Response(200, json={"data": []}, headers={"Cache-Control": "public, max-age=1800"})
    )
    assert api.fetch_net_rankings(api.MENS).max_age == 1800
    assert seen[0].url.path == "/rankings/basketball-men/d1/ncaa-mens-basketball-net-rankings"


def test_cache_is_bounded(mock, monkeypatch: pytest.MonkeyPatch) -> None:
    _, responses = mock
    responses.append(httpx.Response(200, json={"events": []}))
    monkeypatch.setattr(api, "MAX_CACHE_ENTRIES", 2)
    for day in (1, 2, 3):
        api.fetch_scoreboard(api.MENS, date(2026, 11, day))
    assert len(api._cache) == 2


def test_retries_429_then_succeeds(mock) -> None:
    seen, responses = mock
    responses.extend(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(503),
            httpx.Response(200, json={"rankings": []}),
        ]
    )
    assert api.fetch_rankings(api.MENS).payload == {"rankings": []}
    assert len(seen) == 3


def test_retry_exhaustion_raises_descriptive_error(mock) -> None:
    seen, responses = mock
    responses.append(httpx.Response(502, text="bad gateway"))
    with pytest.raises(api.UpstreamError, match="ESPN returned HTTP 502: bad gateway"):
        api.fetch_standings(api.MENS, 2026)
    assert len(seen) == api.MAX_ATTEMPTS


def test_client_errors_are_not_retried(mock) -> None:
    seen, responses = mock
    responses.append(httpx.Response(404, json={"message": "Resource not found"}))
    with pytest.raises(api.UpstreamError, match="ncaa-api returned HTTP 404"):
        api.fetch_bracket(api.WOMENS, 2009)
    assert len(seen) == 1


def test_transport_errors_retry(mock) -> None:
    seen, responses = mock
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"children": []})

    responses.append(flaky)
    assert api.fetch_standings(api.MENS).payload == {"children": []}
    assert calls["n"] == 2


def test_transport_errors_eventually_raise(mock) -> None:
    _, responses = mock

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    responses.append(down)
    with pytest.raises(httpx.ConnectError):
        api.fetch_teams(api.MENS)


def test_invalid_json_and_shapes_are_rejected(mock) -> None:
    _, responses = mock
    responses.append(httpx.Response(200, text="<html>"))
    with pytest.raises(api.UpstreamError, match="invalid JSON"):
        api.fetch_summary(api.MENS, 1)
    api.clear_cache()
    responses[0] = httpx.Response(200, json=[1, 2])
    with pytest.raises(ValueError, match="not a JSON object"):
        api.fetch_summary(api.MENS, 1)
    for call, message in [
        (lambda: api.fetch_scoreboard(api.MENS, None), "no events"),
        (lambda: api.fetch_rankings(api.MENS), "no rankings"),
        (lambda: api.fetch_standings(api.MENS), "no conference"),
        (lambda: api.fetch_net_rankings(api.MENS), "no data"),
        (lambda: api.fetch_bracket(api.MENS, 2026), "no championships"),
    ]:
        api.clear_cache()
        responses[0] = httpx.Response(200, json={})
        with pytest.raises(ValueError, match=message):
            call()


def test_argument_validation() -> None:
    for call in (
        lambda: api.fetch_summary(api.MENS, 0),
        lambda: api.fetch_team_schedule(api.MENS, 0, None, 2),
        lambda: api.fetch_team_roster(api.MENS, -1),
        lambda: api.fetch_stat_leaders(api.MENS, 0, "team", 1),
        lambda: api.fetch_stat_leaders(api.MENS, 136, "school", 1),
    ):
        with pytest.raises(ValueError):
            call()


def test_retry_delay_parsing() -> None:
    assert api._retry_delay(httpx.Response(429, headers={"Retry-After": "2.5"}), 0) == 2.5
    assert api._retry_delay(httpx.Response(429, headers={"Retry-After": "9999"}), 0) == api.MAX_RETRY_DELAY
    past = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert api._retry_delay(past, 0) <= 0.25 * 1.25
    garbage = httpx.Response(429, headers={"Retry-After": "soon"})
    assert 0.25 <= api._retry_delay(garbage, 0) <= 0.3125
    assert api._retry_delay(None, 10) <= api.MAX_RETRY_DELAY


def test_player_stats_follow_every_page(mock) -> None:
    seen, responses = mock

    def page(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "requestedSeason": {"year": 2026},
                "categories": [{"name": "general", "names": ["gamesPlayed"]}],
                "pagination": {"pages": 3, "page": number},
                "athletes": [{"athlete": {"id": str(number)}}],
            },
        )

    responses.append(page)
    pages = api.fetch_player_stats(api.MENS, 2026)
    assert [item["athlete"]["id"] for item in pages.athletes] == ["1", "2", "3"]
    assert pages.season == 2026 and pages.categories[0]["name"] == "general"
    assert seen[0].url.host == "web.example.test"
    assert seen[0].url.params["limit"] == str(api.PLAYER_PAGE_SIZE)


def test_player_stats_page_cap(mock, monkeypatch: pytest.MonkeyPatch) -> None:
    _, responses = mock
    monkeypatch.setattr(api, "MAX_PLAYER_PAGES", 2)
    responses.append(
        lambda request: httpx.Response(200, json={"pagination": {"pages": 99}, "athletes": [{}]})
    )
    with pytest.raises(RuntimeError, match="exceeded 2 pages"):
        api.fetch_player_stats(api.MENS)
    api.clear_cache()
    responses[0] = httpx.Response(200, json={"athletes": None})
    with pytest.raises(ValueError, match="no athletes"):
        api.fetch_player_stats(api.MENS)


def test_stat_leaders_pages_and_key_header(mock, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, responses = mock
    monkeypatch.setenv(api.NCAA_API_KEY_ENV, "secret")

    def page(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200, json={"title": "PPG", "updated": "now", "pages": 2, "data": [{"Rank": str(number)}]}
        )

    responses.append(page)
    result = api.fetch_stat_leaders(api.MENS, 136, "individual", 5)
    assert [row["Rank"] for row in result.rows] == ["1", "2"]
    assert result.title == "PPG" and len(seen) == 2
    assert seen[0].headers["x-ncaa-key"] == "secret"
    assert "page" not in seen[0].url.params and seen[1].url.params["page"] == "2"
    api.clear_cache()
    responses[0] = httpx.Response(200, json={"data": None})
    with pytest.raises(ValueError, match="no data"):
        api.fetch_stat_leaders(api.MENS, 136, "individual", 1)


def test_espn_requests_never_carry_ncaa_key(mock, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, responses = mock
    monkeypatch.setenv(api.NCAA_API_KEY_ENV, "secret")
    responses.append(httpx.Response(200, json={}))
    api.fetch_team_roster(api.MENS, 150)
    assert "x-ncaa-key" not in seen[0].headers


def test_poll_and_schedule_paths(mock) -> None:
    seen, responses = mock
    responses.append(httpx.Response(200, json={}))
    api.fetch_poll(api.WOMENS, 2026, 5, 2)
    api.fetch_team_schedule(api.MENS, 150, 2026, 3)
    assert seen[0].url.host == "core.example.test"
    assert seen[0].url.path.endswith(
        "/leagues/womens-college-basketball/seasons/2026/types/2/weeks/5/rankings/2"
    )
    assert dict(seen[1].url.params) == {"seasontype": "3", "season": "2026"}


def test_deadline_is_enforced(mock) -> None:
    _, responses = mock
    responses.append(httpx.Response(200, json={}))
    with pytest.raises(TimeoutError):
        api.get_json("site", "x", deadline=api.time.monotonic() - 1)


def test_ncaa_throttle_spaces_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(api.time, "sleep", slept.append)
    monkeypatch.setattr(api, "_ncaa_last_request", api.time.monotonic())
    api._throttle_ncaa()
    assert slept and 0 < slept[0] <= api.NCAA_MIN_INTERVAL


def test_shared_client_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_client", None)
    first = api._shared_client()
    assert api._shared_client() is first
    assert first.follow_redirects
    first.close()
