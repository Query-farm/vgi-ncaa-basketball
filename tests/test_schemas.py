"""Flattening of recorded ESPN and ncaa-api documents into typed Arrow rows."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from conftest import load

from vgi_ncaa_basketball import schemas as s

ALL_SCHEMAS = [
    s.RANKINGS_SCHEMA,
    s.STANDINGS_SCHEMA,
    s.CONFERENCES_SCHEMA,
    s.TEAMS_SCHEMA,
    s.GAMES_SCHEMA,
    s.ROSTER_SCHEMA,
    s.PLAYER_STATS_SCHEMA,
    s.BOX_SCORE_SCHEMA,
    s.TEAM_BOX_SCORE_SCHEMA,
    s.PLAY_BY_PLAY_SCHEMA,
    s.NET_SCHEMA,
    s.STAT_LEADERS_SCHEMA,
    s.STAT_CATEGORIES_SCHEMA,
    s.BRACKET_SCHEMA,
]


@pytest.mark.parametrize("schema", ALL_SCHEMAS)
def test_every_column_has_a_comment_and_unique_name(schema: pa.Schema) -> None:
    assert len(set(schema.names)) == len(schema.names)
    assert all(field.metadata and field.metadata[b"comment"] for field in schema)


def test_parsers_handle_markers_and_junk() -> None:
    assert s.to_int("1,490") == 1490
    assert s.to_int(12.0) == 12
    assert s.to_int(12.5) is None
    assert s.to_int("12.0") == 12
    assert s.to_int("x") is None
    assert s.to_int(True) is None
    assert s.to_decimal("0.2580645", 3) == Decimal("0.258")
    assert s.to_decimal("NaN") is None
    assert s.to_decimal("") is None
    assert s.to_decimal("abc") is None
    assert s.to_timestamp("2026-11-10T00:00Z") == datetime(2026, 11, 10, tzinfo=UTC)
    assert s.to_timestamp("2026-11-10T00:00:00") == datetime(2026, 11, 10, tzinfo=UTC)
    assert s.to_timestamp("not a date") is None
    assert s.to_timestamp(None) is None
    assert s.to_bool("true") is None
    assert s.to_text("  ") is None
    assert s.parse_record("8-23") == (8, 23)
    assert s.parse_record("-") == (None, None)
    assert s.team_id_from_ref({"$ref": "http://x/seasons/2026/teams/2509?lang=en"}) == 2509
    assert s.team_id_from_ref({}) is None


def test_current_rankings_include_both_polls_and_votes() -> None:
    rows = s.flatten_rankings(load("mens_rankings.json"))
    batch = s.batch_from_rows(rows, s.RANKINGS_SCHEMA)
    assert batch.num_rows == 14
    top = rows[0]
    assert (top["poll_id"], top["rank"], top["team_location"], top["points"]) == (1, 1, "Florida", 1490)
    assert top["season"] == 2027 and top["week"] == 2 and (top["wins"], top["losses"]) == (2, 0)
    assert {row["poll_name"] for row in rows} == {"AP Top 25", "Coaches Poll"}
    votes = [row for row in rows if row["receiving_votes"]]
    assert votes and all(row["rank"] is None for row in votes)


def test_rankings_without_a_coaches_poll() -> None:
    # Recorded before the women's Coaches Poll for the week was published: AP only.
    rows = s.flatten_rankings(load("womens_rankings.json"))
    assert {row["poll_id"] for row in rows} == {1}


def test_historical_poll_resolves_team_refs() -> None:
    teams = s.team_index(load("mens_teams.json"))
    rows = s.flatten_poll(load("mens_core_rankings_2026_w5_ap.json"), teams)
    assert rows[0]["team_id"] == 2509 and rows[0]["team_location"] == "Purdue"
    assert rows[0]["season"] == 2026 and rows[0]["week"] == 5 and rows[0]["record"] == "7-0"
    unknown = [row for row in rows if row["team_location"] is None]
    assert unknown, "teams absent from the teams list keep their id with NULL names"
    assert s.flatten_poll({"ranks": []}, teams) == []


def test_standings_rows() -> None:
    payload = load("mens_standings.json")
    rows = s.flatten_standings(payload)
    batch = s.batch_from_rows(rows, s.STANDINGS_SCHEMA)
    assert batch.num_rows == 8
    gt = next(row for row in rows if row["team_id"] == 59)
    assert gt["conference_abbreviation"] == "acc" and gt["season"] == 2026
    assert (gt["wins"], gt["losses"], gt["conference_wins"], gt["conference_losses"]) == (11, 20, 2, 16)
    assert gt["win_percentage"] == Decimal("0.355")
    assert gt["point_differential"] == gt["points_for"] - gt["points_against"]
    assert gt["streak"] == "L12"


def test_conference_lookup() -> None:
    payload = load("mens_standings.json")
    rows = s.flatten_conferences(payload)
    assert {row["short_name"] for row in rows} == {"ACC", "Big Ten"}
    assert s.resolve_conference(payload, "Big Ten") == 7
    assert s.resolve_conference(payload, "BIG10") == 7
    assert s.resolve_conference(payload, "Atlantic Coast Conference") == 2
    assert s.resolve_conference(payload, " 23 ") == 23
    with pytest.raises(ValueError, match="unknown conference 'Pac-8'.*ACC"):
        s.resolve_conference(payload, "Pac-8")


def test_teams_join_current_conference() -> None:
    rows = s.flatten_teams(load("womens_teams.json"), load("womens_standings.json"))
    by_conf = {row["conference_abbreviation"] for row in rows}
    assert {"big10", "sec"} <= by_conf
    assert all(row["team_id"] for row in rows)
    assert s.flatten_teams(load("womens_teams.json"), None)[0]["conference_id"] is None


def test_scoreboard_events() -> None:
    rows = s.flatten_events(load("mens_scoreboard.json")["events"])
    s.batch_from_rows(rows, s.GAMES_SCHEMA)
    game = rows[0]
    assert game["event_id"] == 401920686 and game["status"] == "STATUS_FINAL" and game["completed"]
    assert (game["home_team_abbreviation"], game["home_score"], game["home_rank"]) == ("CONN", 83, 4)
    assert (game["away_team_abbreviation"], game["away_score"], game["away_rank"]) == ("WAG", 41, None)
    assert game["season"] == 2027 and game["season_type"] == 2
    assert s.flatten_events([{"id": "x"}, game | {}]) == []


def test_schedule_events_use_score_objects() -> None:
    rows = s.flatten_events(load("mens_schedule_150.json")["events"])
    first = rows[0]
    assert first["home_team_id"] == 150 and first["home_score"] == 75 and first["neutral_site"]
    assert first["event_note"] == "Dick Vitale Invitational presented by Belk"
    assert first["season"] == 2026
    duplicated = s.flatten_events(load("mens_schedule_150.json")["events"] * 2)
    assert len(duplicated) == len(rows)


def test_roster() -> None:
    rows = s.flatten_roster(load("mens_roster_150.json"), 150)
    s.batch_from_rows(rows, s.ROSTER_SCHEMA)
    boozer = rows[0]
    assert (boozer["full_name"], boozer["height_inches"], boozer["class_year"]) == ("Cayden Boozer", 76, "SO")


@pytest.mark.parametrize("league", ["mens", "womens"])
def test_player_stats(league: str) -> None:
    payload = load(f"{league}_player_stats.json")
    rows = s.flatten_player_stats(payload["categories"], payload["athletes"] * 2, 2026)
    batch = s.batch_from_rows(rows, s.PLAYER_STATS_SCHEMA)
    assert batch.num_rows == 3
    row = rows[0]
    assert row["season"] == 2026 and row["player_id"] and row["team_id"]
    assert row["points"] >= row["field_goals_made"]
    if row["games_played"]:
        assert abs(row["points_per_game"] - Decimal(row["points"]) / row["games_played"]) < Decimal("0.1")


def test_box_score_rows() -> None:
    payload = load("mens_summary_401920686.json")
    rows = s.flatten_box_score(payload, 401920686)
    s.batch_from_rows(rows, s.BOX_SCORE_SCHEMA)
    assert {row["home_away"] for row in rows} == {"home", "away"}
    jones = next(row for row in rows if row["player_name"] == "Tajae Jones")
    assert (jones["minutes"], jones["points"], jones["field_goals_made"], jones["field_goals_attempted"]) == (
        17,
        2,
        1,
        3,
    )


def test_team_box_score_computes_exact_percentages() -> None:
    rows = s.flatten_team_box_score(load("mens_summary_401920686.json"), 401920686)
    s.batch_from_rows(rows, s.TEAM_BOX_SCORE_SCHEMA)
    wagner = next(row for row in rows if row["team_id"] == 2681)
    assert (wagner["points"], wagner["winner"], wagner["home_away"]) == (41, False, "away")
    assert wagner["field_goal_pct"] == Decimal("33.3")
    assert wagner["turnovers"] == 26


def test_play_by_play_rows() -> None:
    rows = s.flatten_play_by_play(load("mens_summary_401920686.json"), 401920686)
    s.batch_from_rows(rows, s.PLAY_BY_PLAY_SCHEMA)
    assert rows[0]["play_type"] == "Jumpball" and rows[0]["period"] == 1
    scoring = [row for row in rows if row["scoring_play"]]
    assert scoring and scoring[0]["player_id"] and scoring[0]["secondary_player_id"]
    assert s._coordinate(-214748340) is None and s._coordinate(25) == 25


def test_womens_summary_has_quarters() -> None:
    payload = load("womens_summary_401918925.json")
    plays = s.flatten_play_by_play(payload, 401918925)
    assert plays and plays[0]["period_display"] == "1st Quarter"
    teams = {row["team_abbreviation"]: row for row in s.flatten_team_box_score(payload, 401918925)}
    assert (teams["IU"]["points"], teams["BRAD"]["points"]) == (84, 56) and teams["IU"]["winner"]
    assert s.flatten_play_by_play({"plays": [{"id": None}]}, 1) == []


@pytest.mark.parametrize(("league", "ppg"), [("mens", 136), ("womens", 102)])
def test_stat_categories(league: str, ppg: int) -> None:
    payload = load(f"ncaa_{league}_stats_index.json")
    rows = s.flatten_stat_categories({**payload, "team": payload["team"] + payload["team"][:1]})
    s.batch_from_rows(rows, s.STAT_CATEGORIES_SCHEMA)
    assert {"individual", "team"} == {row["category"] for row in rows}
    lookup = {(row["category"], row["name"]): row["stat_id"] for row in rows}
    assert lookup[("individual", "Points Per Game")] == ppg
    assert len(rows) == len(payload["individual"]) + len(payload["team"])


def test_net_rows() -> None:
    rows = s.flatten_net(load("ncaa_mens_net.json"))
    s.batch_from_rows(rows, s.NET_SCHEMA)
    assert (rows[0]["rank"], rows[0]["school"], rows[0]["wins"], rows[0]["quad1_wins"]) == (
        1,
        "Michigan",
        37,
        21,
    )
    assert rows[0]["updated"] == "Through Games Apr. 06 2026"


def test_stat_leaders_ties_and_value_column() -> None:
    payload = load("ncaa_mens_stat_team_145.json")
    rows = s.flatten_stat_leaders(payload["data"], stat_id=145, category="team", title="x", updated=None)
    s.batch_from_rows(rows, s.STAT_LEADERS_SCHEMA)
    tied = next(row for row in rows if row["tied"])
    assert tied["rank"] == 2 and tied["stat_column"] == "PPG" and tied["player_name"] is None
    individual = load("ncaa_mens_stat_individual_136.json")
    rows = s.flatten_stat_leaders(
        individual["data"], stat_id=136, category="individual", title="t", updated="u"
    )
    assert rows[0]["player_name"] == "Dior Johnson" and rows[0]["stat_value"] == Decimal("34.5")
    assert rows[0]["games"] == 2


@pytest.mark.parametrize(("league", "champion", "score"), [("mens", "Michigan", 69), ("womens", "UCLA", 79)])
def test_bracket_title_game(league: str, champion: str, score: int) -> None:
    rows = s.flatten_bracket(load(f"ncaa_{league}_bracket_2026.json"))
    s.batch_from_rows(rows, s.BRACKET_SCHEMA)
    final = next(row for row in rows if row["round_number"] == 7)
    assert final["winner"] == champion and final["round_title"] == "Championship"
    assert max(final["top_score"], final["bottom_score"]) == score
    first_four = next(row for row in rows if row["round_number"] == 1)
    assert first_four["round_title"] == "First Four" and first_four["region"] is None
    assert first_four["start_date"] < date(2026, 4, 1)
