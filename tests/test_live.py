"""Opt-in smoke tests against the live ESPN and ncaa-api endpoints (pytest -m live).

They pin completed seasons and games so the assertions are stable facts.
"""

from __future__ import annotations

from datetime import date

import pytest

from vgi_ncaa_basketball import api
from vgi_ncaa_basketball import schemas as s

pytestmark = pytest.mark.live


@pytest.mark.parametrize("league", api.LEAGUES, ids=lambda value: value.key)
def test_current_standings_cover_division_i(league: api.League) -> None:
    rows = s.flatten_standings(api.fetch_standings(league).payload)
    assert len(rows) > 300
    assert len({row["team_id"] for row in rows}) == len(rows)
    assert len({row["conference_id"] for row in rows}) >= 30


def test_final_2026_big_ten_standings() -> None:
    rows = [
        row
        for row in s.flatten_standings(api.fetch_standings(api.MENS, 2026).payload)
        if row["conference_abbreviation"] == "big10"
    ]
    assert len(rows) == 18
    # Conference play is a closed system: every conference win is another member's loss.
    assert sum(row["conference_wins"] for row in rows) == sum(row["conference_losses"] for row in rows)


@pytest.mark.parametrize("league", api.LEAGUES, ids=lambda value: value.key)
def test_current_ap_poll(league: api.League) -> None:
    rows = s.flatten_rankings(api.fetch_rankings(league).payload)
    ranked = sorted(row["rank"] for row in rows if row["poll_id"] == 1 and row["rank"] is not None)
    assert ranked[:5] == [1, 2, 3, 4, 5] and len(ranked) >= 25


def test_historical_poll_resolves_teams() -> None:
    teams = s.team_index(api.fetch_teams(api.MENS).payload)
    rows = s.flatten_poll(api.fetch_poll(api.MENS, 2026, 5, 1).payload, teams)
    assert rows[0]["rank"] == 1 and rows[0]["team_location"] == "Purdue"


def test_pinned_game_box_score_and_plays() -> None:
    payload = api.fetch_summary(api.MENS, 401920686).payload
    box = s.flatten_box_score(payload, 401920686)
    teams = {row["team_id"]: row for row in s.flatten_team_box_score(payload, 401920686)}
    assert teams[41]["points"] == 83 and teams[2681]["points"] == 41
    for team_id, team in teams.items():
        assert sum(row["points"] or 0 for row in box if row["team_id"] == team_id) == team["points"]
    plays = s.flatten_play_by_play(payload, 401920686)
    assert len(plays) > 400
    assert plays[-1]["home_score"] == 83 and plays[-1]["away_score"] == 41


def test_full_division_i_scoreboard() -> None:
    rows = s.flatten_events(api.fetch_scoreboard(api.MENS, date(2026, 11, 9)).payload["events"])
    assert len(rows) > 10, "groups=50 returns the full slate, not just featured games"
    assert any(row["event_id"] == 401920686 for row in rows)


def test_duke_2026_schedule() -> None:
    events = [
        *(api.fetch_team_schedule(api.MENS, 150, 2026, 2).payload.get("events") or []),
        *(api.fetch_team_schedule(api.MENS, 150, 2026, 3).payload.get("events") or []),
    ]
    rows = s.flatten_events(events)
    wins = sum(
        1
        for row in rows
        if (row["home_team_id"] == 150 and row["home_winner"])
        or (row["away_team_id"] == 150 and row["away_winner"])
    )
    assert wins == 35


def test_player_stats_2026_all_pages() -> None:
    pages = api.fetch_player_stats(api.WOMENS, 2026)
    rows = s.flatten_player_stats(pages.categories, pages.athletes, pages.season)
    assert pages.season == 2026 and len(rows) > 5_000
    assert len({row["player_id"] for row in rows}) == len(rows)


@pytest.mark.parametrize(("league", "champion"), [(api.MENS, "Michigan"), (api.WOMENS, "UCLA")])
def test_2026_champions(league: api.League, champion: str) -> None:
    rows = s.flatten_bracket(api.fetch_bracket(league, 2026).payload)
    assert next(row for row in rows if row["round_number"] == 7)["winner"] == champion
    assert len(rows) >= 63


@pytest.mark.parametrize(("league", "ppg"), [(api.MENS, 136), (api.WOMENS, 102)])
def test_stat_category_ids(league: api.League, ppg: int) -> None:
    rows = s.flatten_stat_categories(api.fetch_stat_categories(league).payload)
    assert {"category": "individual", "stat_id": ppg, "name": "Points Per Game"} in rows


def test_womens_pinned_game() -> None:
    payload = api.fetch_summary(api.WOMENS, 401918925).payload
    teams = {row["team_id"]: row for row in s.flatten_team_box_score(payload, 401918925)}
    assert sorted(team["points"] for team in teams.values()) == [56, 84]
    plays = s.flatten_play_by_play(payload, 401918925)
    assert max(play["period"] for play in plays) >= 4


def test_net_and_stat_leaders() -> None:
    net = s.flatten_net(api.fetch_net_rankings(api.MENS).payload)
    assert len(net) > 300 and net[0]["rank"] == 1
    leaders = api.fetch_stat_leaders(api.MENS, 136, "individual", 1)
    rows = s.flatten_stat_leaders(
        leaders.rows, stat_id=136, category="individual", title=leaders.title, updated=leaders.updated
    )
    assert rows and rows[0]["rank"] == 1 and rows[0]["stat_column"] == "PPG"
