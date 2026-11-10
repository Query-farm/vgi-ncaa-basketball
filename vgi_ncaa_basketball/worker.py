"""VGI worker exposing NCAA Division I men's and women's basketball as a SQL catalog."""

from __future__ import annotations

import json
import os
import sys

import pyarrow as pa
from vgi import Worker
from vgi.catalog import Catalog, ConstraintInfo, Schema, Table, constraint_record

from vgi_ncaa_basketball import api
from vgi_ncaa_basketball import schemas as s
from vgi_ncaa_basketball.functions import CATALOG, build_functions
from vgi_ncaa_basketball.meta import column_comments

SOURCE_URL = "https://github.com/Query-farm/vgi-ncaa-basketball"


def _key_constraints(schema: str, table: str, key: tuple[str, ...]) -> tuple[ConstraintInfo, ...]:
    """Primary-key and NOT NULL records for a read-only table keyed by ``key``.

    Only keys the worker itself guarantees are declared. Cross-table foreign keys
    (for example team_id to teams) are deliberately not declared: each table is
    fetched from a different ESPN document, and DuckDB accepts only fully
    enforced constraint records, which the worker cannot attest across documents.
    """
    records = [
        constraint_record(
            [schema],
            table,
            "pk",
            "PRIMARY_KEY",
            [[column] for column in key],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
    ]
    records += [
        constraint_record(
            [schema],
            table,
            f"{column}_not_null",
            "NOT_NULL",
            [[column]],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
        for column in key
    ]
    return tuple(records)


def _examples(*items: tuple[str, str]) -> str:
    return json.dumps([{"description": text, "sql": sql} for text, sql in items])


def _table_specs(league: api.League) -> list[dict]:
    """Describe the ordinary tables of one league schema."""
    q = f"{CATALOG}.{league.key}"
    short = league.adjective.capitalize()
    return [
        {
            "name": "current_rankings",
            "function": "all_current_rankings",
            "schema": s.RANKINGS_SCHEMA,
            "key": ("poll_id", "team_id"),
            "estimate": 80,
            "comment": f"Latest {league.adjective} AP Top 25 and Coaches Poll, one row per team per poll",
            "tags": {
                "vgi.category": "rankings",
                "vgi.title": f"{short} Polls",
                "vgi.keywords": json.dumps(["AP Top 25", "Coaches Poll", "rankings", "polls", "top 25"]),
                "vgi.doc_llm": (
                    f"The latest {league.adjective} college basketball polls: the AP Top 25 and, where "
                    "ESPN carries it, the Coaches Poll. Each ranked team has rank 1-25; teams receiving "
                    "votes have rank NULL and receiving_votes true. Filter poll_name or poll_id "
                    "(1 AP, 2 Coaches). For an earlier week use rankings(season, week)."
                ),
                "vgi.doc_md": "The latest weekly polls as published by ESPN.",
                "vgi.example_queries": _examples(
                    (
                        "The current AP Top 25.",
                        f"SELECT rank, team_location, record, points, first_place_votes "
                        f"FROM {q}.current_rankings WHERE poll_id = 1 AND rank IS NOT NULL ORDER BY rank",
                    ),
                ),
            },
        },
        {
            "name": "current_standings",
            "function": "all_current_standings",
            "schema": s.STANDINGS_SCHEMA,
            "key": ("team_id",),
            "estimate": 365,
            "comment": f"Current {league.adjective} Division I standings, one row per team",
            "tags": {
                "vgi.category": "standings",
                "vgi.title": f"{short} Standings",
                "vgi.keywords": json.dumps(["standings", "conference standings", "records", "games behind"]),
                "vgi.doc_llm": (
                    f"Current {league.adjective} Division I standings, one row per team, with overall and "
                    "conference records, conference_seed (ESPN's order within the conference), games "
                    "behind, home/road splits, points for and against and streak. For a past season or "
                    "one conference call standings()."
                ),
                "vgi.doc_md": "Current Division I standings for every conference.",
                "vgi.example_queries": _examples(
                    (
                        "Current Big Ten table in conference order.",
                        "SELECT conference_seed, team_name, conference_wins, conference_losses, "
                        f"wins, losses "
                        f"FROM {q}.current_standings WHERE conference_abbreviation = 'big10' "
                        "ORDER BY conference_seed",
                    ),
                ),
            },
        },
        {
            "name": "current_scoreboard",
            "function": "all_current_scoreboard",
            "schema": s.GAMES_SCHEMA,
            "key": ("event_id",),
            "estimate": 60,
            "comment": f"ESPN's current {league.adjective} Division I scoreboard day, one row per game",
            "tags": {
                "vgi.category": "games",
                "vgi.title": f"{short} Scoreboard",
                "vgi.keywords": json.dumps(["scores", "scoreboard", "today", "live scores", "games"]),
                "vgi.doc_llm": (
                    f"Every {league.adjective} Division I game on ESPN's current scoreboard day (today, "
                    "US Eastern), with status, live or final scores, AP ranks and venue. status is "
                    "STATUS_SCHEDULED, STATUS_IN_PROGRESS or STATUS_FINAL. For another day call "
                    "scoreboard() with a `DATE`."
                ),
                "vgi.doc_md": "Today's full Division I scoreboard, not only ESPN's featured games.",
                "vgi.example_queries": _examples(
                    (
                        "Today's games involving ranked teams.",
                        f"SELECT short_name, status_detail, away_rank, away_score, home_rank, home_score "
                        f"FROM {q}.current_scoreboard WHERE away_rank IS NOT NULL OR home_rank IS NOT NULL "
                        "ORDER BY start_time",
                    ),
                ),
            },
        },
        {
            "name": "current_player_stats",
            "function": "all_current_player_stats",
            "schema": s.PLAYER_STATS_SCHEMA,
            "key": ("player_id",),
            "estimate": 6_000,
            "comment": f"Current-season {league.adjective} Division I player statistics, one row per player",
            "tags": {
                "vgi.category": "player-stats",
                "vgi.title": f"{short} Player Stats",
                "vgi.keywords": json.dumps(["players", "scoring leaders", "points per game", "rebounds"]),
                "vgi.doc_llm": (
                    f"Current-season statistics for every {league.adjective} Division I player: totals "
                    "and per-game points, rebounds, assists, steals, blocks, shooting percentages "
                    "(0-100 scale) and PER. Require a minimum games_played for leaderboards. For an "
                    "earlier season call player_stats(season := ...)."
                ),
                "vgi.doc_md": "Complete current-season per-player lines from ESPN.",
                "vgi.example_queries": _examples(
                    (
                        "Current rebounding leaders with at least three games.",
                        f"SELECT player_name, team_abbreviation, games_played, rebounds_per_game "
                        f"FROM {q}.current_player_stats WHERE games_played >= 3 "
                        "ORDER BY rebounds_per_game DESC LIMIT 10",
                    ),
                ),
            },
        },
        {
            "name": "teams",
            "function": "all_teams",
            "schema": s.TEAMS_SCHEMA,
            "key": ("team_id",),
            "estimate": 365,
            "comment": f"Every {league.adjective} Division I team with its current conference",
            "tags": {
                "vgi.category": "teams",
                "vgi.title": f"{short} Teams",
                "vgi.keywords": json.dumps(["teams", "schools", "team id", "conference membership"]),
                "vgi.doc_llm": (
                    f"Every {league.adjective} Division I team with ESPN team_id, names, colours and current "
                    "conference. Look up team_id here before calling team_schedule or team_roster."
                ),
                "vgi.doc_md": "Division I teams joined to their current conference.",
                "vgi.example_queries": _examples(
                    (
                        "Find a team id by name.",
                        f"SELECT team_id, display_name, conference_name FROM {q}.teams "
                        "WHERE display_name ILIKE '%duke%'",
                    ),
                ),
            },
        },
        {
            "name": "conferences",
            "function": "all_conferences",
            "schema": s.CONFERENCES_SCHEMA,
            "key": ("conference_id",),
            "estimate": 31,
            "comment": f"Every {league.adjective} Division I conference",
            "tags": {
                "vgi.category": "teams",
                "vgi.title": f"{short} Conferences",
                "vgi.keywords": json.dumps(["conferences", "leagues", "Big Ten", "SEC", "ACC"]),
                "vgi.doc_llm": (
                    "Division I conferences with ESPN conference_id, abbreviation, short name and team "
                    "count. Any of these values works as the conference argument of standings() and "
                    "scoreboard()."
                ),
                "vgi.doc_md": "Division I conferences in the current standings.",
                "vgi.example_queries": _examples(
                    (
                        "Largest conferences.",
                        f"SELECT short_name, abbreviation, team_count FROM {q}.conferences "
                        "ORDER BY team_count DESC",
                    ),
                ),
            },
        },
        {
            "name": "stat_categories",
            "function": "all_stat_categories",
            "schema": s.STAT_CATEGORIES_SCHEMA,
            "key": ("category", "stat_id"),
            "estimate": 120,
            "provider": "ncaa",
            "comment": f"Official NCAA {league.adjective} leaderboards available to stat_leaders",
            "tags": {
                "vgi.category": "player-stats",
                "vgi.title": f"{short} Stat Categories",
                "vgi.keywords": json.dumps(["stat id", "leaderboards", "NCAA statistics", "categories"]),
                "vgi.doc_llm": (
                    f"Every official NCAA {league.adjective} Division I statistical leaderboard, with the "
                    "stat_id to pass to stat_leaders. category is individual (players) or team. Ids differ "
                    "between the men's and women's games, so look them up here by name."
                ),
                "vgi.doc_md": "The ncaa.com leaderboard index, served through ncaa-api.",
                "vgi.example_queries": _examples(
                    (
                        "Find the points-per-game leaderboard id.",
                        f"SELECT category, stat_id, name FROM {q}.stat_categories "
                        "WHERE name ILIKE '%per game%' ORDER BY category, name",
                    ),
                ),
            },
        },
        {
            "name": "net_rankings",
            "function": "all_net_rankings",
            "schema": s.NET_SCHEMA,
            "key": ("school",),
            "estimate": 365,
            "provider": "ncaa",
            "comment": f"Latest official NCAA {league.adjective} NET rankings, one row per school",
            "tags": {
                "vgi.category": "rankings",
                "vgi.title": f"{short} NET Rankings",
                "vgi.keywords": json.dumps(["NET", "NCAA Evaluation Tool", "quadrant", "Quad 1", "bubble"]),
                "vgi.doc_llm": (
                    f"The latest official NCAA Evaluation Tool (NET) ranking for every {league.adjective} "
                    "Division I school, with records by site and by quadrant (Quad 1 is the strongest "
                    "tier of games). Sourced from ncaa.com through ncaa-api. Early in a season the NCAA "
                    "may still publish the previous season's final release; check updated."
                ),
                "vgi.doc_md": (
                    "Official NET rankings via ncaa-api, an optional source whose base URL is "
                    "configurable. School names follow the NCAA, not ESPN."
                ),
                "vgi.example_queries": _examples(
                    (
                        "The NET top ten with Quadrant 1 records.",
                        f"SELECT rank, school, conference, record, quad1_record FROM {q}.net_rankings "
                        "WHERE rank <= 10 ORDER BY rank",
                    ),
                ),
            },
        },
    ]


_CATEGORIES = json.dumps(
    [
        {
            "name": "rankings",
            "title": "Rankings",
            "description": "AP and Coaches polls and the official NCAA NET.",
            "keywords": ["AP", "Coaches", "NET", "top 25"],
        },
        {
            "name": "standings",
            "title": "Standings",
            "description": "Overall and conference records by team.",
            "keywords": ["standings", "conference", "records"],
        },
        {
            "name": "games",
            "title": "Games",
            "description": "Scoreboards and team schedules with results.",
            "keywords": ["scores", "schedule", "results"],
        },
        {
            "name": "teams",
            "title": "Teams",
            "description": "Teams, conferences and rosters.",
            "keywords": ["teams", "conferences", "rosters"],
        },
        {
            "name": "player-stats",
            "title": "Player Statistics",
            "description": "Season player statistics and official NCAA leaderboards.",
            "keywords": ["players", "leaders", "scoring"],
        },
        {
            "name": "game-detail",
            "title": "Game Detail",
            "description": "Box scores and play-by-play for one game.",
            "keywords": ["box score", "play-by-play"],
        },
        {
            "name": "postseason",
            "title": "Postseason",
            "description": "The NCAA tournament bracket.",
            "keywords": ["March Madness", "bracket", "tournament"],
        },
    ]
)


def _schema(league: api.League) -> Schema:
    functions = build_functions(league)
    q = f"{CATALOG}.{league.key}"
    tables = []
    for spec in _table_specs(league):
        schema: pa.Schema = spec["schema"]
        tables.append(
            Table(
                name=spec["name"],
                function=functions[spec["function"]],
                comment=spec["comment"],
                tags={"provider": spec.get("provider", "espn"), **spec["tags"]},
                column_comments=column_comments(schema),
                constraints=_key_constraints(league.key, spec["name"], spec["key"]),
                cardinality_estimate=spec["estimate"],
            )
        )
    return Schema(
        path=[league.key],
        comment=f"{league.title} (NCAA Division I)",
        tags={
            "provider": "espn",
            "domain": "sports",
            "vgi.title": league.title,
            "vgi.keywords": json.dumps(
                ["college basketball", league.adjective, "NCAA", "Division I", "March Madness"]
            ),
            "vgi.categories": _CATEGORIES,
            "vgi.doc_llm": (
                f"{league.title}, Division I. Start with current_rankings, current_standings, "
                "current_scoreboard, current_player_stats, teams or conferences. Parameterized functions "
                "cover past polls (rankings), past standings, any date's scoreboard, team schedules and "
                "rosters, past player seasons, box scores and play-by-play by ESPN event_id, and the "
                "official NCAA NET, stat leaders and tournament bracket. Seasons are labelled by the "
                "year they end: 2025-26 is 2026."
            ),
            "vgi.doc_md": (
                f"## {league.title}\n\n"
                "Polls, standings, scores, schedules, rosters, player statistics, box scores and "
                "play-by-play from ESPN, plus the official NCAA NET, statistical leaderboards and "
                "tournament bracket via ncaa-api.\n\n"
                "ESPN `team_id` and `event_id` join the tables and functions. Seasons are labelled by "
                "the year they end, so the 2025-26 season is `2026`.\n\n"
                "Use the ordinary tables for what is happening now and the parameterized functions "
                "for earlier seasons, other dates, a single team or a single game."
            ),
            "vgi.example_queries": _examples(
                (
                    "The current AP Top 25.",
                    f"SELECT rank, team_location, record FROM {q}.current_rankings "
                    "WHERE poll_id = 1 AND rank IS NOT NULL ORDER BY rank",
                ),
                (
                    "Teams with the best current conference record.",
                    f"SELECT team_name, conference_name, conference_wins, conference_losses "
                    f"FROM {q}.current_standings ORDER BY conference_win_percentage DESC NULLS LAST LIMIT 10",
                ),
            ),
        },
        functions=list(functions.values()),
        tables=tables,
    )


_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "mens_standings_cover_division_i",
            "description": "Current men's standings cover more than 300 Division I teams.",
            "sql": f"SELECT count(*) > 300 AS populated FROM {CATALOG}.mens.current_standings",
            "expected_result": [{"populated": True}],
        },
        {
            "name": "womens_standings_cover_division_i",
            "description": "Current women's standings cover more than 300 Division I teams.",
            "sql": f"SELECT count(*) > 300 AS populated FROM {CATALOG}.womens.current_standings",
            "expected_result": [{"populated": True}],
        },
        {
            "name": "mens_2026_champion",
            "description": "Michigan won the 2026 men's national championship game.",
            "sql": f"SELECT winner FROM {CATALOG}.mens.bracket(2026) WHERE round_number = 7",
            "expected_result": [{"winner": "Michigan"}],
        },
        {
            "name": "pinned_box_score_totals",
            "description": "UConn's player points sum to its 83-point team total against Wagner.",
            "sql": (
                f"SELECT sum(points) AS points FROM {CATALOG}.mens.box_score(401920686) WHERE team_id = 41"
            ),
            "expected_result": [{"points": 83}],
        },
    ]
)

_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "ap_top_five_conference_records",
            "prompt": (
                "For each team in the current men's AP top five, show its conference and its current "
                "conference record."
            ),
        },
        {
            "name": "big_ten_final_standings",
            "prompt": "Show the final 2025-26 men's Big Ten standings in conference order.",
        },
        {
            "name": "womens_2026_champion",
            "prompt": (
                "Who won the 2026 NCAA women's basketball championship game, what was the score, and "
                "where does the champion sit in the latest official women's NET rankings?"
            ),
        },
        {
            "name": "mens_scoring_leaders_2026",
            "prompt": (
                "List the ten highest-scoring men's players by points per game in the 2025-26 season "
                "among players with at least 20 games, with each player's conference."
            ),
        },
        {
            "name": "pinned_game_scoring",
            "prompt": (
                "In men's ESPN game 401920686 (Wagner at UConn), who scored the most points for UConn, "
                "what share of UConn's team total was that, and how many scoring plays did UConn have?"
            ),
        },
        {
            "name": "duke_season_and_roster",
            "prompt": (
                "How many games did Duke's men's team win in the 2025-26 season including the "
                "postseason, and how many players are on Duke's current roster?"
            ),
        },
        {
            "name": "official_vs_espn_ppg_leader",
            "prompt": (
                "Who is number one on the NCAA's official men's points-per-game leaderboard right now, "
                "and what are that player's current-season numbers in ESPN's player statistics?"
            ),
        },
        {
            "name": "womens_week5_poll_finish",
            "prompt": (
                "Which teams were in the top five of the women's AP poll in week 5 of the 2025-26 "
                "season, and what were their final conference records that season?"
            ),
        },
        {
            "name": "ranked_games_on_date",
            "prompt": (
                "List the men's games played on 9 November 2026 that involved an AP-ranked team, with "
                "final scores."
            ),
        },
        {
            "name": "womens_ranked_teams_today",
            "prompt": (
                "Which teams in the current women's AP Top 25 play on today's scoreboard, and against whom?"
            ),
        },
    ]
)

NCAA_CATALOG = Catalog(
    name=CATALOG,
    default_schema="mens",
    source_url=SOURCE_URL,
    comment="Read-only NCAA Division I men's and women's basketball data from ESPN and ncaa-api",
    tags={
        "provider": "espn",
        "domain": "sports",
        "vgi.title": "NCAA Division I Basketball",
        "vgi.subtitle": "College basketball polls, standings, scores, player stats and box scores as SQL.",
        "vgi.source_url": SOURCE_URL,
        "vgi.author": "Query Farm LLC <hello@query.farm>",
        "vgi.copyright": (
            "Worker (c) 2026 Query Farm LLC. Data is fetched on demand from ESPN's undocumented public "
            "endpoints and from ncaa.com via ncaa-api; it remains subject to those providers' terms. "
            "Not affiliated with the NCAA, ESPN or Disney."
        ),
        "vgi.license": "MIT",
        "vgi.support_contact": "https://github.com/Query-farm/vgi-ncaa-basketball/issues",
        "vgi.support_policy_url": "https://github.com/Query-farm/vgi-ncaa-basketball/blob/main/README.md",
        "vgi.keywords": json.dumps(
            ["NCAA", "college basketball", "March Madness", "AP Top 25", "NET rankings", "Division I"]
        ),
        "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
        "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
        "vgi.doc_llm": (
            "Read-only NCAA Division I college basketball for analysis in SQL. Schema mens holds the "
            "men's game and womens the women's game; both expose the same shapes. Reach for it for "
            "AP and Coaches polls, the official NCAA NET, conference standings, daily scoreboards, "
            "team schedules and rosters, season player statistics, official NCAA stat leaders, game "
            "box scores and play-by-play, and the March Madness bracket. Seasons are labelled by the "
            "year they end (2025-26 is 2026). ESPN team_id and event_id are the join keys; data is "
            "fetched live on demand, so values move during the season."
        ),
        "vgi.doc_md": (
            "# NCAA Division I basketball\n\n"
            "Men's (`mens`) and women's (`womens`) college basketball from ESPN's public endpoints, plus "
            "the official NCAA NET rankings, statistical leaderboards and tournament brackets through "
            "the ncaa-api proxy of ncaa.com.\n\n"
            "## Data model\n\n"
            "Current tables are the quickest entry point. Parameterized functions select seasons, "
            "weeks, dates, teams and games. ESPN `team_id` and `event_id` link standings, scoreboards, "
            "schedules, rosters and box scores. Seasons are labelled by their ending year.\n\n"
            "Data is fetched on demand and cached briefly in process; nothing is rehosted."
        ),
    },
    schemas=[_schema(league) for league in api.LEAGUES],
)


class NcaaBasketballWorker(Worker):
    """Worker process hosting the read-only NCAA basketball catalog."""

    catalog = NCAA_CATALOG


def main() -> None:
    """Run over stdio by default, or HTTP when VGI's ``--http`` flag is supplied."""
    NcaaBasketballWorker.main()


def main_http() -> None:
    """Run HTTP with production-safe configuration requirements."""
    if not os.environ.get("VGI_SIGNING_KEY"):
        raise SystemExit(
            "VGI_SIGNING_KEY is required for HTTP mode so state remains valid across restarts and replicas"
        )
    auth_configured = bool(os.environ.get("VGI_BEARER_TOKENS") or os.environ.get("VGI_JWT_ISSUER"))
    auth_configured = auth_configured or os.environ.get("VGI_PROXY_PROOF_MODE", "").lower() == "require"
    if not auth_configured and os.environ.get("VGI_ALLOW_ANONYMOUS") != "1":
        raise SystemExit(
            "HTTP authentication is required; configure bearer/JWT/proxy proof, or explicitly set "
            "VGI_ALLOW_ANONYMOUS=1 for an intentionally public deployment"
        )
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    if "--describe" not in argv and "--no-describe" not in argv:
        argv = ["--no-describe", *argv]
    if not any(value == "--cors-origins" or value.startswith("--cors-origins=") for value in argv):
        argv = ["--cors-origins", "", *argv]
    sys.argv = [sys.argv[0], *argv]
    NcaaBasketballWorker.main()
