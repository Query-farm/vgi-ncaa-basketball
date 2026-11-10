"""Every table function's process() against the local fixture upstream."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from vgi_ncaa_basketball import api, functions
from vgi_ncaa_basketball.functions import (
    BracketArgs,
    EventArgs,
    RankingsArgs,
    ScoreboardArgs,
    SeasonArgs,
    StandingsArgs,
    StatLeadersArgs,
    TeamArgs,
    TeamScheduleArgs,
    build_functions,
)

MENS = build_functions(api.MENS)
WOMENS = build_functions(api.WOMENS)


class Collector:
    """Minimal stand-in for vgi-rpc's OutputCollector."""

    def __init__(self) -> None:
        self.batches: list[pa.RecordBatch] = []
        self.cache_controls: list[Any] = []
        self.finished = False

    def emit(self, batch: pa.RecordBatch, *, cache_control: Any = None) -> None:
        self.batches.append(batch)
        self.cache_controls.append(cache_control)

    def finish(self) -> None:
        self.finished = True


def run(cls: type, args: Any = None) -> tuple[list[dict], Collector]:
    out = Collector()
    params = SimpleNamespace(args=args, output_schema=cls.FIXED_SCHEMA)
    cls.process(params, None, out)
    assert out.finished and len(out.batches) == 1
    assert out.batches[0].schema.equals(cls.FIXED_SCHEMA)
    return out.batches[0].to_pylist(), out


def test_every_league_builds_the_same_surface() -> None:
    assert MENS.keys() == WOMENS.keys()
    assert len(MENS) == len(functions.SPECS) == 19
    for name, cls in MENS.items():
        assert cls.Meta.name == name
        assert cls.LEAGUE is api.MENS and WOMENS[name].LEAGUE is api.WOMENS
        assert "ncaa_basketball.mens." in cls.Meta.tags["vgi.example_queries"]
        assert "ncaa_basketball.womens." in WOMENS[name].Meta.tags["vgi.example_queries"]
        assert cls.cardinality(None).estimate > 0


def test_tables_scans(upstream: str) -> None:
    rows, out = run(MENS["all_current_rankings"])
    assert len(rows) == 14 and out.cache_controls[0].ttl >= 11
    assert len(run(MENS["all_current_standings"])[0]) == 8
    assert len(run(WOMENS["all_current_scoreboard"])[0]) == 3
    assert len(run(WOMENS["all_current_player_stats"])[0]) == 3
    teams, _ = run(MENS["all_teams"])
    assert any(row["conference_id"] for row in teams)
    assert {row["short_name"] for row in run(WOMENS["all_conferences"])[0]} == {"Big Ten", "SEC"}
    assert run(MENS["all_net_rankings"])[0][0]["school"] == "Michigan"
    categories, _ = run(WOMENS["all_stat_categories"])
    assert ("individual", 102, "Points Per Game") in {tuple(row.values()) for row in categories}


def test_historical_rankings(upstream: str) -> None:
    rows, _ = run(MENS["rankings"], RankingsArgs(season=2026, week=5, poll="AP"))
    assert rows[0]["team_location"] == "Purdue"


def test_standings_with_conference_filter(upstream: str) -> None:
    rows, _ = run(MENS["standings"], StandingsArgs(season=2026, conference="big10"))
    assert rows and {row["conference_id"] for row in rows} == {7}
    rows, _ = run(MENS["standings"], StandingsArgs(season=None, conference=None))
    assert len(rows) == 8


def test_scoreboard_by_date_and_conference(upstream: str) -> None:
    rows, _ = run(MENS["scoreboard"], ScoreboardArgs(game_date=date(2026, 11, 9), conference=None))
    assert rows[0]["event_id"] == 401920686
    rows, out = run(MENS["scoreboard"], ScoreboardArgs(game_date=date(2026, 11, 9), conference="ACC"))
    assert any("groups=2" in path for path in _requests())
    assert out.cache_controls[0] is not None


def _requests() -> list[str]:
    from fixture_server import FixtureHandler

    return FixtureHandler.requests


def test_team_schedule_and_roster(upstream: str) -> None:
    rows, _ = run(MENS["team_schedule"], TeamScheduleArgs(team_id=150, season=2026))
    times = [row["start_time"] for row in rows]
    assert times == sorted(times) and len(rows) == 3
    rows, _ = run(MENS["team_roster"], TeamArgs(team_id=150))
    assert rows[0]["team_id"] == 150


def test_player_stats_season(upstream: str) -> None:
    rows, _ = run(MENS["player_stats"], SeasonArgs(season=2026))
    assert all(row["season"] == 2026 for row in rows)


def test_game_detail(upstream: str) -> None:
    box, _ = run(MENS["box_score"], EventArgs(event_id=401920686))
    team, _ = run(MENS["team_box_score"], EventArgs(event_id=401920686))
    plays, _ = run(MENS["play_by_play"], EventArgs(event_id=401920686))
    assert box and len(team) == 2 and plays
    assert {row["event_id"] for row in box + team + plays} == {401920686}


def test_womens_game_detail(upstream: str) -> None:
    plays, _ = run(WOMENS["play_by_play"], EventArgs(event_id=401918925))
    assert plays[0]["period_display"] == "1st Quarter"


def test_unknown_game_is_an_upstream_error(upstream: str) -> None:
    with pytest.raises(api.UpstreamError, match="HTTP 404"):
        run(WOMENS["box_score"], EventArgs(event_id=1))


def test_examples_use_each_leagues_pinned_ids() -> None:
    assert "box_score(401920686)" in MENS["box_score"].Meta.tags["vgi.example_queries"]
    assert "box_score(401918925)" in WOMENS["box_score"].Meta.tags["vgi.example_queries"]
    assert "stat_leaders(102)" in WOMENS["stat_leaders"].Meta.tags["vgi.example_queries"]
    assert "{" not in WOMENS["stat_leaders"].Meta.tags["vgi.doc_llm"]


def test_ncaa_api_functions(upstream: str) -> None:
    rows, _ = run(MENS["stat_leaders"], StatLeadersArgs(stat_id=136, category="individual", pages=3))
    assert rows[0]["stat_title"] == "Points Per Game" and rows[0]["rank"] == 1
    rows, _ = run(WOMENS["bracket"], BracketArgs(season=2026))
    assert next(row for row in rows if row["round_number"] == 7)["winner"] == "UCLA"


def test_cache_helpers() -> None:
    assert functions._cache(None) is None and functions._cache(0) is None
    assert functions._cache(30).ttl == 30
    assert functions._min_age(None, 30, 12) == 12 and functions._min_age(None) is None
