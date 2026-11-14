"""VGI table functions for the NCAA basketball catalog.

The men's and women's schemas expose the same surface over different ESPN
leagues and ncaa-api sports. Each function is written once as a league-agnostic
base class; :func:`build_functions` derives a concrete subclass per league with
its own discovery metadata, so every SQL object documents its own schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.cache_control import CacheControl
from vgi.metadata import FunctionExample
from vgi.table_function import (
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc.rpc import OutputCollector

from vgi_ncaa_basketball import api
from vgi_ncaa_basketball import schemas as s
from vgi_ncaa_basketball.meta import result_columns_schema

CATALOG = "ncaa_basketball"
FIRST_SEASON = 2002
POLL_IDS = {"AP": 1, "Coaches": 2}

# --------------------------------------------------------------------------- arguments

_SEASON_DOC = "Season labelled by the year it ends (2025-26 is 2026). NULL selects the current season."
_OPTIONAL_SEASON = Arg("season", default=None, arrow_type=pa.int32(), ge=FIRST_SEASON, doc=_SEASON_DOC)
_CONFERENCE = Arg(
    "conference",
    default=None,
    arrow_type=pa.string(),
    doc="Conference to narrow to, as listed in the conferences table. NULL means every conference.",
)
_TEAM_ID = Arg(0, arrow_type=pa.int32(), ge=1, doc="ESPN team id, as found in the teams table.")
_EVENT_ID = Arg(0, arrow_type=pa.int32(), ge=1, doc="ESPN event id, as found in scoreboard results.")


@dataclass(kw_only=True)
class RankingsArgs:
    """Arguments accepted by ``rankings``."""

    season: Annotated[
        int,
        Arg(0, arrow_type=pa.int32(), ge=FIRST_SEASON, doc="Season labelled by its end year."),
    ]
    week: Annotated[
        int,
        Arg(
            1,
            arrow_type=pa.int32(),
            ge=1,
            le=25,
            doc="Regular-season poll week (1 is the preseason poll).",
        ),
    ]
    poll: Annotated[
        str,
        Arg("poll", default="AP", arrow_type=pa.string(), choices=tuple(POLL_IDS), doc="AP or Coaches."),
    ]


@dataclass(kw_only=True)
class StandingsArgs:
    """Arguments accepted by ``standings``."""

    season: Annotated[int | None, _OPTIONAL_SEASON]
    conference: Annotated[str | None, _CONFERENCE]


@dataclass(kw_only=True)
class ScoreboardArgs:
    """Arguments accepted by ``scoreboard``."""

    game_date: Annotated[
        date,
        Arg(0, arrow_type=pa.date32(), doc="Day of the games on the US Eastern calendar."),
    ]
    conference: Annotated[str | None, _CONFERENCE]


@dataclass(kw_only=True)
class TeamScheduleArgs:
    """Arguments accepted by ``team_schedule``."""

    team_id: Annotated[int, _TEAM_ID]
    season: Annotated[int | None, _OPTIONAL_SEASON]


@dataclass(kw_only=True)
class TeamArgs:
    """Arguments accepted by ``team_roster``."""

    team_id: Annotated[int, _TEAM_ID]


@dataclass(kw_only=True)
class SeasonArgs:
    """Arguments accepted by ``player_stats``."""

    season: Annotated[int | None, _OPTIONAL_SEASON]


@dataclass(kw_only=True)
class EventArgs:
    """Arguments accepted by the per-game functions."""

    event_id: Annotated[int, _EVENT_ID]


@dataclass(kw_only=True)
class StatLeadersArgs:
    """Arguments accepted by ``stat_leaders``."""

    stat_id: Annotated[
        int,
        Arg(0, arrow_type=pa.int32(), ge=1, doc="NCAA statistic id, as listed in the stat_categories table."),
    ]
    category: Annotated[
        str,
        Arg(
            "category",
            default="individual",
            arrow_type=pa.string(),
            choices=("individual", "team"),
            doc="individual for player leaders or team for team leaders.",
        ),
    ]
    pages: Annotated[
        int,
        Arg("pages", default=1, arrow_type=pa.int32(), ge=1, le=40, doc="Pages of 50 rows to fetch."),
    ]


@dataclass(kw_only=True)
class BracketArgs:
    """Arguments accepted by ``bracket``."""

    season: Annotated[
        int,
        Arg(0, arrow_type=pa.int32(), ge=2010, doc="Year the tournament was played (2026 for 2025-26)."),
    ]


# --------------------------------------------------------------------------- helpers


def _cache(max_age: int | None) -> CacheControl | None:
    return CacheControl(ttl=max_age) if max_age else None


def _emit(out: OutputCollector, rows: list[dict[str, Any]], schema: pa.Schema, max_age: int | None) -> None:
    out.emit(s.batch_from_rows(rows, schema), cache_control=_cache(max_age))
    out.finish()


def _conference_group(league: api.League, conference: str | None) -> tuple[str | int, int | None]:
    """Return the ESPN scoreboard group for an optional conference filter."""
    if conference is None:
        return 50, None
    standings = api.fetch_standings(league)
    return s.resolve_conference(standings.payload, conference), standings.max_age


def _min_age(*values: int | None) -> int | None:
    present = [value for value in values if value]
    return min(present) if present else None


class _LeagueFunction:
    """Shared mixin: concrete subclasses bind ``LEAGUE`` and their own ``Meta``."""

    LEAGUE: ClassVar[api.League] = api.MENS
    ESTIMATE: ClassVar[int] = 100
    MAXIMUM: ClassVar[int | None] = None

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """Return a planner estimate for this result."""
        return TableCardinality(estimate=cls.ESTIMATE, max=cls.MAXIMUM)


@init_single_worker
@bind_fixed_schema
class _CurrentRankings(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.RANKINGS_SCHEMA
    ESTIMATE = 80

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit the latest AP (and Coaches, where available) poll."""
        response = api.fetch_rankings(cls.LEAGUE)
        _emit(out, s.flatten_rankings(response.payload), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _Rankings(_LeagueFunction, TableFunctionGenerator[RankingsArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.RANKINGS_SCHEMA
    ESTIMATE = 45

    @classmethod
    def process(cls, params: ProcessParams[RankingsArgs], state: None, out: OutputCollector) -> None:
        """Emit one historical weekly poll."""
        args = params.args
        poll = api.fetch_poll(cls.LEAGUE, args.season, args.week, POLL_IDS[args.poll])
        teams = api.fetch_teams(cls.LEAGUE)
        rows = s.flatten_poll(poll.payload, s.team_index(teams.payload))
        _emit(out, rows, params.output_schema, _min_age(poll.max_age, teams.max_age))


@init_single_worker
@bind_fixed_schema
class _CurrentStandings(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.STANDINGS_SCHEMA
    ESTIMATE = 365
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit current Division I standings."""
        response = api.fetch_standings(cls.LEAGUE)
        _emit(out, s.flatten_standings(response.payload), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _Standings(_LeagueFunction, TableFunctionGenerator[StandingsArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.STANDINGS_SCHEMA
    ESTIMATE = 365
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[StandingsArgs], state: None, out: OutputCollector) -> None:
        """Emit standings for a season, optionally narrowed to one conference."""
        args = params.args
        response = api.fetch_standings(cls.LEAGUE, args.season)
        rows = s.flatten_standings(response.payload)
        if args.conference is not None:
            conference_id = s.resolve_conference(response.payload, args.conference)
            rows = [row for row in rows if row["conference_id"] == conference_id]
        _emit(out, rows, params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _CurrentScoreboard(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.GAMES_SCHEMA
    ESTIMATE = 60
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit ESPN's current Division I scoreboard."""
        response = api.fetch_scoreboard(cls.LEAGUE, None)
        _emit(out, s.flatten_events(response.payload["events"]), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _Scoreboard(_LeagueFunction, TableFunctionGenerator[ScoreboardArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.GAMES_SCHEMA
    ESTIMATE = 60
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[ScoreboardArgs], state: None, out: OutputCollector) -> None:
        """Emit every Division I game on one date, optionally for one conference."""
        args = params.args
        group, standings_age = _conference_group(cls.LEAGUE, args.conference)
        response = api.fetch_scoreboard(cls.LEAGUE, args.game_date, group)
        rows = s.flatten_events(response.payload["events"])
        _emit(out, rows, params.output_schema, _min_age(response.max_age, standings_age))


@init_single_worker
@bind_fixed_schema
class _Teams(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.TEAMS_SCHEMA
    ESTIMATE = 365
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit every Division I team with its current conference."""
        teams = api.fetch_teams(cls.LEAGUE)
        standings = api.fetch_standings(cls.LEAGUE)
        rows = s.flatten_teams(teams.payload, standings.payload)
        _emit(out, rows, params.output_schema, _min_age(teams.max_age, standings.max_age))


@init_single_worker
@bind_fixed_schema
class _Conferences(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.CONFERENCES_SCHEMA
    ESTIMATE = 31
    MAXIMUM = 40

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit every Division I conference."""
        response = api.fetch_standings(cls.LEAGUE)
        _emit(out, s.flatten_conferences(response.payload), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _TeamSchedule(_LeagueFunction, TableFunctionGenerator[TeamScheduleArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.GAMES_SCHEMA
    ESTIMATE = 35
    MAXIMUM = 60

    @classmethod
    def process(cls, params: ProcessParams[TeamScheduleArgs], state: None, out: OutputCollector) -> None:
        """Emit one team's regular-season and postseason games."""
        args = params.args
        regular = api.fetch_team_schedule(cls.LEAGUE, args.team_id, args.season, 2)
        post = api.fetch_team_schedule(cls.LEAGUE, args.team_id, args.season, 3)
        # ESPN's team schedule omits conferenceCompetition; the same season's
        # standings supply each team's conference so it can be derived.
        standings = api.fetch_standings(cls.LEAGUE, args.season)
        events = [*(regular.payload.get("events") or []), *(post.payload.get("events") or [])]
        rows = sorted(
            s.flatten_events(events, s.conference_index(standings.payload)),
            key=lambda row: (row["start_time"] is None, row["start_time"] or 0),
        )
        _emit(out, rows, params.output_schema, _min_age(regular.max_age, post.max_age, standings.max_age))


@init_single_worker
@bind_fixed_schema
class _TeamRoster(_LeagueFunction, TableFunctionGenerator[TeamArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.ROSTER_SCHEMA
    ESTIMATE = 16
    MAXIMUM = 40

    @classmethod
    def process(cls, params: ProcessParams[TeamArgs], state: None, out: OutputCollector) -> None:
        """Emit one team's current roster."""
        team_id = params.args.team_id
        response = api.fetch_team_roster(cls.LEAGUE, team_id)
        _emit(out, s.flatten_roster(response.payload, team_id), params.output_schema, response.max_age)


def _player_rows(league: api.League, season: int | None) -> tuple[list[dict[str, Any]], int]:
    pages = api.fetch_player_stats(league, season)
    return s.flatten_player_stats(pages.categories, pages.athletes, pages.season), pages.max_age


@init_single_worker
@bind_fixed_schema
class _CurrentPlayerStats(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.PLAYER_STATS_SCHEMA
    ESTIMATE = 6_000
    MAXIMUM = 20_000

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit current-season per-player statistics."""
        rows, max_age = _player_rows(cls.LEAGUE, None)
        _emit(out, rows, params.output_schema, max_age)


@init_single_worker
@bind_fixed_schema
class _PlayerStats(_LeagueFunction, TableFunctionGenerator[SeasonArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.PLAYER_STATS_SCHEMA
    ESTIMATE = 7_000
    MAXIMUM = 20_000

    @classmethod
    def process(cls, params: ProcessParams[SeasonArgs], state: None, out: OutputCollector) -> None:
        """Emit per-player statistics for one season."""
        rows, max_age = _player_rows(cls.LEAGUE, params.args.season)
        _emit(out, rows, params.output_schema, max_age)


@init_single_worker
@bind_fixed_schema
class _BoxScore(_LeagueFunction, TableFunctionGenerator[EventArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.BOX_SCORE_SCHEMA
    ESTIMATE = 26
    MAXIMUM = 60

    @classmethod
    def process(cls, params: ProcessParams[EventArgs], state: None, out: OutputCollector) -> None:
        """Emit the player box score for one game."""
        event_id = params.args.event_id
        response = api.fetch_summary(cls.LEAGUE, event_id)
        _emit(out, s.flatten_box_score(response.payload, event_id), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _TeamBoxScore(_LeagueFunction, TableFunctionGenerator[EventArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.TEAM_BOX_SCORE_SCHEMA
    ESTIMATE = 2
    MAXIMUM = 2

    @classmethod
    def process(cls, params: ProcessParams[EventArgs], state: None, out: OutputCollector) -> None:
        """Emit the two team box-score lines for one game."""
        event_id = params.args.event_id
        response = api.fetch_summary(cls.LEAGUE, event_id)
        rows = s.flatten_team_box_score(response.payload, event_id)
        _emit(out, rows, params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _PlayByPlay(_LeagueFunction, TableFunctionGenerator[EventArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.PLAY_BY_PLAY_SCHEMA
    ESTIMATE = 450
    MAXIMUM = 1_500

    @classmethod
    def process(cls, params: ProcessParams[EventArgs], state: None, out: OutputCollector) -> None:
        """Emit every play of one game."""
        event_id = params.args.event_id
        response = api.fetch_summary(cls.LEAGUE, event_id)
        rows = s.flatten_play_by_play(response.payload, event_id)
        _emit(out, rows, params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _NetRankings(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.NET_SCHEMA
    ESTIMATE = 365
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit the latest official NET rankings."""
        response = api.fetch_net_rankings(cls.LEAGUE)
        _emit(out, s.flatten_net(response.payload), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _StatLeaders(_LeagueFunction, TableFunctionGenerator[StatLeadersArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.STAT_LEADERS_SCHEMA
    ESTIMATE = 50
    MAXIMUM = 2_000

    @classmethod
    def process(cls, params: ProcessParams[StatLeadersArgs], state: None, out: OutputCollector) -> None:
        """Emit an official NCAA statistical leaderboard."""
        args = params.args
        pages = api.fetch_stat_leaders(cls.LEAGUE, args.stat_id, args.category, args.pages)
        rows = s.flatten_stat_leaders(
            pages.rows, stat_id=args.stat_id, category=args.category, title=pages.title, updated=pages.updated
        )
        _emit(out, rows, params.output_schema, pages.max_age)


@init_single_worker
@bind_fixed_schema
class _StatCategories(_LeagueFunction, TableFunctionGenerator[None]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.STAT_CATEGORIES_SCHEMA
    ESTIMATE = 120
    MAXIMUM = 400

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Emit the official NCAA leaderboards available for stat_leaders."""
        response = api.fetch_stat_categories(cls.LEAGUE)
        _emit(out, s.flatten_stat_categories(response.payload), params.output_schema, response.max_age)


@init_single_worker
@bind_fixed_schema
class _Bracket(_LeagueFunction, TableFunctionGenerator[BracketArgs]):
    FIXED_SCHEMA: ClassVar[pa.Schema] = s.BRACKET_SCHEMA
    ESTIMATE = 67
    MAXIMUM = 80

    @classmethod
    def process(cls, params: ProcessParams[BracketArgs], state: None, out: OutputCollector) -> None:
        """Emit every game of one NCAA tournament bracket."""
        response = api.fetch_bracket(cls.LEAGUE, params.args.season)
        _emit(out, s.flatten_bracket(response.payload), params.output_schema, response.max_age)


# --------------------------------------------------------------------------- metadata


@dataclass(frozen=True, slots=True)
class Spec:
    """League-independent description of one SQL function."""

    base: type[TableFunctionGenerator[Any]]
    name: str
    description: str
    category: str
    doc_llm: str
    doc_md: str
    examples: tuple[tuple[str, str], ...]


# Completed games and stat ids pinned per league so every example runs against real data.
PINNED: dict[str, dict[str, str]] = {
    "mens": {"{event}": "401920686", "{game}": "Wagner at UConn", "{ppg}": "136"},
    "womens": {"{event}": "401918925", "{game}": "Bradley at Indiana", "{ppg}": "102"},
}


def _q(league: api.League, text: str) -> str:
    text = text.replace("{s}", f"{CATALOG}.{league.key}")
    for token, value in PINNED[league.key].items():
        text = text.replace(token, value)
    return text


# Composed examples: each combines two of the worker's own functions in one query. Every SQL is
# defined once here and referenced from the example list of the function that drives it and from
# the schema-level list (worker._schema), so the two cannot drift apart.
SCHEDULE_VS_OPPONENT_RECORDS = (
    "Duke's 2025-26 results beside each opponent's final overall record (team_schedule joined to "
    "standings on the opponent's team_id).",
    "WITH games AS (SELECT start_time, "
    "CASE WHEN home_team_id = 150 THEN away_team_id ELSE home_team_id END AS opponent_id, "
    "CASE WHEN home_team_id = 150 THEN home_score ELSE away_score END AS duke_score, "
    "CASE WHEN home_team_id = 150 THEN away_score ELSE home_score END AS opponent_score "
    "FROM {s}.team_schedule(150, season := 2026)) "
    "SELECT g.start_time, s.team_name AS opponent, g.duke_score, g.opponent_score, "
    "s.wins AS opponent_wins, s.losses AS opponent_losses "
    "FROM games g LEFT JOIN {s}.standings(season := 2026) s ON s.team_id = g.opponent_id "
    "ORDER BY g.start_time",
)
SCORING_BY_PERIOD = (
    "Each scorer's points per period in {game}, summed from play_by_play scoring plays and shown "
    "beside the player's box_score total.",
    "SELECT b.team_abbreviation, b.player_name, p.period, sum(p.score_value) AS period_points, "
    "b.points AS game_points "
    "FROM {s}.play_by_play({event}) p JOIN {s}.box_score({event}) b USING (player_id) "
    "WHERE p.scoring_play "
    "GROUP BY b.team_abbreviation, b.player_name, b.points, p.period "
    "ORDER BY b.team_abbreviation, game_points DESC, b.player_name, p.period",
)
LEADERS_WITH_NET = (
    "The official NCAA points-per-game top ten (ties included) with each player's school in the "
    "latest NET (stat_leaders joined to net_rankings on the NCAA school name).",
    "SELECT l.rank, l.player_name, l.team, l.stat_value, n.rank AS net_rank, n.record "
    "FROM {s}.stat_leaders({ppg}) l LEFT JOIN {s}.net_rankings n ON n.school = l.team "
    "WHERE l.rank <= 10 ORDER BY l.rank, l.player_name",
)
ELITE_EIGHT_PRESEASON_RANK = (
    "How far each 2026 Elite Eight team went in the NCAA tournament and where it sat in the "
    "preseason AP poll (NULL when unranked).",
    "WITH slots AS (SELECT round_number, round_title, unnest([top_team, bottom_team]) AS team "
    "FROM {s}.bracket(2026)), "
    "reached AS (SELECT team, max(round_number) AS furthest_round, "
    "arg_max(round_title, round_number) AS furthest_round_title FROM slots GROUP BY team) "
    "SELECT r.team, r.furthest_round_title, p.rank AS preseason_ap_rank "
    "FROM reached r LEFT JOIN {s}.rankings(2026, 1) p "
    "ON p.team_location = r.team AND p.rank IS NOT NULL "
    "WHERE r.furthest_round >= 5 "
    "ORDER BY r.furthest_round DESC, preseason_ap_rank NULLS LAST, r.team",
)
COMPOSED_EXAMPLES: tuple[tuple[str, str], ...] = (
    SCHEDULE_VS_OPPONENT_RECORDS,
    SCORING_BY_PERIOD,
    LEADERS_WITH_NET,
    ELITE_EIGHT_PRESEASON_RANK,
)


def league_examples(league: api.League, items: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    """Resolve catalog and pinned-value tokens in (description, sql) example pairs for one league."""
    return [{"description": _q(league, text), "sql": _q(league, sql)} for text, sql in items]


SPECS: tuple[Spec, ...] = (
    Spec(
        _CurrentRankings,
        "all_current_rankings",
        "Latest {league} AP and Coaches poll (scan behind current_rankings)",
        "rankings",
        "The scan behind current_rankings. Prefer the table; use rankings(season, week) for an earlier poll.",
        "Returns the latest published polls. Selecting `current_rankings` is normally clearer.",
        (
            (
                "Read the latest top five directly from the backing scan.",
                "SELECT poll_name, rank, team_location FROM {s}.all_current_rankings() "
                "WHERE rank <= 5 ORDER BY poll_name, rank",
            ),
        ),
    ),
    Spec(
        _Rankings,
        "rankings",
        "{league} AP or Coaches poll for one season and week",
        "rankings",
        "One row per team in a historical weekly poll: rank, previous rank, points, first-place "
        "votes and record, plus teams receiving votes (rank NULL). Seasons are labelled by end "
        "year, so the 2025-26 season is 2026. Week 1 is the preseason poll.",
        "Historical weekly poll from ESPN's core API. `poll` is `AP` (default) or `Coaches`. Teams "
        "outside the top 25 that received votes have `receiving_votes = true` and a NULL `rank`.",
        (
            (
                "The AP top ten in week 5 of the 2025-26 season.",
                "SELECT rank, team_location, points, record FROM {s}.rankings(2026, 5) "
                "WHERE rank <= 10 ORDER BY rank",
            ),
        ),
    ),
    Spec(
        _CurrentStandings,
        "all_current_standings",
        "Current {league} Division I standings (scan behind current_standings)",
        "standings",
        "The scan behind current_standings. Prefer the table; use standings() for another season "
        "or one conference.",
        "Returns the current Division I standings. Selecting `current_standings` is normally clearer.",
        (
            (
                "Conference leaders from the backing scan.",
                "SELECT conference_name, team_name, wins, losses FROM {s}.all_current_standings() "
                "WHERE conference_seed = 1 ORDER BY conference_name",
            ),
        ),
    ),
    Spec(
        _Standings,
        "standings",
        "{league} Division I standings by season and conference",
        "standings",
        "One row per Division I team with overall, conference, home, road and vs-AP-top-25 "
        "records, points for and against, streak and ESPN's conference ordering. season NULL is "
        "the current season; conference accepts an id, abbreviation (big10) or name (Big Ten).",
        "Standings flattened to one row per team. Percentages are exact decimals. Seasons are "
        "labelled by the year they end.",
        (
            (
                "Final 2025-26 Big Ten standings.",
                "SELECT conference_seed, team_name, conference_wins, conference_losses, wins, losses "
                "FROM {s}.standings(season := 2026, conference := 'Big Ten') ORDER BY conference_seed",
            ),
        ),
    ),
    Spec(
        _CurrentScoreboard,
        "all_current_scoreboard",
        "ESPN's current {league} Division I scoreboard (scan behind current_scoreboard)",
        "games",
        "The scan behind current_scoreboard. Prefer the table; use scoreboard(game_date) for another day.",
        "Returns ESPN's current scoreboard day. Selecting `current_scoreboard` is normally clearer.",
        (
            (
                "Games on the current scoreboard from the backing scan.",
                "SELECT short_name, status_detail, away_score, home_score "
                "FROM {s}.all_current_scoreboard() ORDER BY start_time",
            ),
        ),
    ),
    Spec(
        _Scoreboard,
        "scoreboard",
        "Every {league} Division I game on one date",
        "games",
        "One row per Division I game on game_date (US Eastern calendar day): teams, AP ranks, "
        "scores, status, venue and attendance. Pass conference to narrow to one conference's "
        "games. event_id feeds box_score, team_box_score and play_by_play.",
        "Full Division I scoreboard for a date (ESPN `groups=50`, so not just featured games).",
        (
            (
                "Results from 9 November 2026.",
                "SELECT away_team_name, away_score, home_team_name, home_score, status "
                "FROM {s}.scoreboard(DATE '2026-11-09') ORDER BY start_time",
            ),
        ),
    ),
    Spec(
        _Teams,
        "all_teams",
        "Every {league} Division I team (scan behind teams)",
        "teams",
        "The scan behind the teams table. Prefer the table.",
        "Returns every Division I team with its current conference. Selecting `teams` is clearer.",
        (
            (
                "Teams in the Big Ten from the backing scan.",
                "SELECT team_id, display_name FROM {s}.all_teams() "
                "WHERE conference_abbreviation = 'big10' ORDER BY display_name",
            ),
        ),
    ),
    Spec(
        _Conferences,
        "all_conferences",
        "Every {league} Division I conference (scan behind conferences)",
        "teams",
        "The scan behind the conferences table. Prefer the table.",
        "Returns every Division I conference. Selecting `conferences` is normally clearer.",
        (
            (
                "Conferences from the backing scan.",
                "SELECT conference_id, short_name, team_count FROM {s}.all_conferences() ORDER BY short_name",
            ),
        ),
    ),
    Spec(
        _TeamSchedule,
        "team_schedule",
        "One {league} team's regular-season and postseason games",
        "games",
        "One row per game for team_id in a season (NULL for current), including conference "
        "tournament and NCAA tournament games, with scores once played. Find team_id in teams.",
        "Regular season and postseason combined, ordered by tip-off time. ESPN's schedule feed omits "
        "the conference flag, so `conference_game` is derived from the season's standings for "
        "regular-season games (conference tournaments count, as on ESPN's scoreboard) and is NULL "
        "for NCAA postseason games.",
        (
            (
                "Duke's 2025-26 results.",
                "SELECT start_time, short_name, away_score, home_score, status "
                "FROM {s}.team_schedule(150, season := 2026) ORDER BY start_time",
            ),
            SCHEDULE_VS_OPPONENT_RECORDS,
        ),
    ),
    Spec(
        _TeamRoster,
        "team_roster",
        "One {league} team's current roster",
        "teams",
        "One row per player on team_id's current roster: jersey, position, height, weight, class "
        "year and hometown. Find team_id in teams.",
        "Current roster only; ESPN does not reliably serve historical rosters.",
        (
            (
                "Duke's current roster.",
                "SELECT jersey, full_name, position, display_height, class_year "
                "FROM {s}.team_roster(150) ORDER BY full_name",
            ),
        ),
    ),
    Spec(
        _CurrentPlayerStats,
        "all_current_player_stats",
        "Current-season {league} player statistics (scan behind current_player_stats)",
        "player-stats",
        "The scan behind current_player_stats. Prefer the table; use player_stats(season) for an "
        "earlier season.",
        "Returns every Division I player's current-season line. Selecting the table is clearer.",
        (
            (
                "Top current scorers with at least three games from the backing scan.",
                "SELECT player_name, team_abbreviation, points_per_game "
                "FROM {s}.all_current_player_stats() WHERE games_played >= 3 "
                "ORDER BY points_per_game DESC LIMIT 10",
            ),
        ),
    ),
    Spec(
        _PlayerStats,
        "player_stats",
        "{league} Division I player season statistics",
        "player-stats",
        "One row per Division I player for a season: games, minutes, points, rebounds, assists, "
        "steals, blocks, shooting splits and percentages (0-100 scale), double-doubles and triple-doubles. "
        "Totals and per-game averages are both present. Filter games_played for qualified leaders.",
        "Complete per-athlete season lines from ESPN, fetched across every page. Percentages use a "
        "0-100 scale; per-game values are exact one-decimal numbers.",
        (
            (
                "2025-26 scoring leaders with at least 20 games.",
                "SELECT player_name, team_abbreviation, games_played, points_per_game "
                "FROM {s}.player_stats(season := 2026) WHERE games_played >= 20 "
                "ORDER BY points_per_game DESC LIMIT 10",
            ),
        ),
    ),
    Spec(
        _BoxScore,
        "box_score",
        "Player box score for one {league} game",
        "game-detail",
        "One row per player in event_id: starter flag, minutes, points, made/attempted shooting "
        "splits, rebounds, assists, steals, blocks, turnovers and fouls. Get event_id from "
        "scoreboard or team_schedule.",
        "Player-grain box score from the ESPN game summary. Players who did not play have NULL stats.",
        (
            (
                "Top scorers in {game} on 9 November 2026.",
                "SELECT team_abbreviation, player_name, minutes, points "
                "FROM {s}.box_score({event}) ORDER BY points DESC NULLS LAST LIMIT 5",
            ),
        ),
    ),
    Spec(
        _TeamBoxScore,
        "team_box_score",
        "Team box-score totals for one {league} game",
        "game-detail",
        "Two rows, one per team, for event_id: points, winner, shooting splits and percentages, "
        "rebounds, assists, turnovers, points off turnovers, fast-break and paint points.",
        "Team-grain box score. Percentages are computed exactly from made and attempted shots.",
        (
            (
                "Team totals for {game} on 9 November 2026.",
                "SELECT team_name, points, field_goal_pct, rebounds, turnovers "
                "FROM {s}.team_box_score({event}) ORDER BY points DESC",
            ),
        ),
    ),
    Spec(
        _PlayByPlay,
        "play_by_play",
        "Play-by-play for one {league} game",
        "game-detail",
        "One row per play in event_id with period, clock, play type, text, team, athletes, running "
        "score and shot coordinates. Order by sequence_number for chronological order.",
        "Every play from the ESPN game summary. Shot coordinates are in ESPN's half-court frame.",
        (
            (
                "Scoring plays in {game} on 9 November 2026.",
                "SELECT period, clock, text, home_score, away_score FROM {s}.play_by_play({event}) "
                "WHERE scoring_play ORDER BY sequence_number",
            ),
            SCORING_BY_PERIOD,
        ),
    ),
    Spec(
        _NetRankings,
        "all_net_rankings",
        "Official {league} NET rankings (scan behind net_rankings)",
        "rankings",
        "The scan behind the net_rankings table. Prefer the table.",
        "Returns the latest official NET release via ncaa-api. Selecting `net_rankings` is clearer.",
        (
            (
                "The NET top ten from the backing scan.",
                "SELECT rank, school, conference, record FROM {s}.all_net_rankings() "
                "WHERE rank <= 10 ORDER BY rank",
            ),
        ),
    ),
    Spec(
        _StatLeaders,
        "stat_leaders",
        "Official NCAA {league} statistical leaderboard",
        "player-stats",
        "An official NCAA leaderboard for the current season by stat_id; look the id up by name "
        "in the stat_categories table (points per game is {ppg} here). stat_value is the ranked "
        "column; columns_json keeps every published column. pages fetches 50 rows per page.",
        "Official NCAA leaders via ncaa-api. Stat ids are ncaa.com's and differ between the men's "
        "and women's games and between individual and team categories.",
        (
            (
                "The NCAA's official points-per-game leaders.",
                "SELECT rank, player_name, team, stat_value FROM {s}.stat_leaders({ppg}) ORDER BY rank",
            ),
            LEADERS_WITH_NET,
        ),
    ),
    Spec(
        _StatCategories,
        "all_stat_categories",
        "Official {league} NCAA leaderboard ids (scan behind stat_categories)",
        "player-stats",
        "The scan behind the stat_categories table. Prefer the table.",
        "Returns the NCAA leaderboard ids accepted by `stat_leaders`. Selecting the table is clearer.",
        (
            (
                "Leaderboard ids from the backing scan.",
                "SELECT category, stat_id, name FROM {s}.all_stat_categories() ORDER BY category, name",
            ),
        ),
    ),
    Spec(
        _Bracket,
        "bracket",
        "NCAA {league} tournament bracket",
        "postseason",
        "One row per NCAA tournament game for the championship played in season (2026 for "
        "2025-26): round, region, seeds, scores and winner. round_number 7 is the title game.",
        "Tournament bracket via ncaa-api, including live scores during March Madness.",
        (
            (
                "The 2026 Final Four and championship game.",
                "SELECT round_title, top_seed, top_team, top_score, bottom_seed, bottom_team, "
                "bottom_score FROM {s}.bracket(2026) WHERE round_number >= 6 ORDER BY bracket_position_id",
            ),
            ELITE_EIGHT_PRESEASON_RANK,
        ),
    ),
)


def _meta(spec: Spec, league: api.League) -> type:
    examples = league_examples(league, spec.examples)
    league_label = f"NCAA {league.adjective} basketball"
    description = spec.description.format(league=league_label)
    tags = {
        "provider": "ncaa"
        if spec.base in (_NetRankings, _StatLeaders, _StatCategories, _Bracket)
        else "espn",
        "vgi.category": spec.category,
        "vgi.result_columns_schema": result_columns_schema(spec.base.FIXED_SCHEMA),
        "vgi.doc_llm": _q(league, f"{league.title}. {spec.doc_llm}"),
        "vgi.doc_md": _q(league, spec.doc_md),
        "vgi.example_queries": json.dumps(examples),
    }
    return type(
        "Meta",
        (),
        {
            "__doc__": "Function discovery metadata.",
            "name": spec.name,
            "description": description,
            "categories": ["basketball", spec.category],
            "projection_pushdown": True,
            "tags": tags,
            "examples": [
                FunctionExample(sql=item["sql"], description=item["description"]) for item in examples
            ],
        },
    )


def build_functions(league: api.League) -> dict[str, type[TableFunctionGenerator[Any]]]:
    """Return the concrete function classes for one league, keyed by SQL name."""
    functions: dict[str, type[TableFunctionGenerator[Any]]] = {}
    for spec in SPECS:
        class_name = "".join(part.title() for part in (league.key, *spec.name.split("_"))) + "Function"
        cls = type(
            class_name,
            (spec.base,),
            {
                "__doc__": spec.description.format(league=league.adjective),
                "__module__": __name__,
                "LEAGUE": league,
                "Meta": _meta(spec, league),
            },
        )
        functions[spec.name] = cls
    return functions
