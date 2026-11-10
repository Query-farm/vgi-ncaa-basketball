"""Arrow schemas and response flattening for the NCAA basketball catalog.

Every flattener is a pure function from a decoded upstream JSON document to a
list of row dictionaries keyed by schema column name, so it can be tested
directly against the recorded fixtures in ``tests/fixtures``.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import pyarrow as pa

from vgi_ncaa_basketball.meta import field

TIMESTAMP = pa.timestamp("us", tz="UTC")
PCT3 = pa.decimal128(4, 3)
ONE_DP = pa.decimal128(6, 1)
STAT_VALUE = pa.decimal128(12, 3)

# --------------------------------------------------------------------------- parsing


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def to_int(value: Any) -> int | None:
    """Parse an integer-like value (``"12"``, ``12.0``, ``"1,490"``) without accepting booleans."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).replace(",", "").strip()
    try:
        return int(text)
    except ValueError:
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return int(parsed) if parsed.is_finite() and parsed == parsed.to_integral_value() else None


def to_decimal(value: Any, places: int | None = None) -> Decimal | None:
    """Parse a finite decimal, optionally rounded half-up to ``places`` digits."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    if places is not None:
        result = result.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return result


def to_timestamp(value: Any) -> datetime | None:
    """Parse ESPN's ISO-8601 timestamps (``2026-11-10T00:00Z``) into UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def to_bool(value: Any) -> bool | None:
    """Return a boolean only when the value is a real JSON boolean."""
    return value if isinstance(value, bool) else None


def to_text(value: Any) -> str | None:
    """Return stripped non-empty text, or null."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_RECORD = re.compile(r"^\s*(\d+)-(\d+)(?:-(\d+))?\s*$")


def parse_record(value: Any) -> tuple[int | None, int | None]:
    """Split a ``"W-L"`` record summary into wins and losses."""
    match = _RECORD.match(value) if isinstance(value, str) else None
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def parse_made_attempted(value: Any) -> tuple[int | None, int | None]:
    """Split a box-score ``"17-51"`` made-attempted pair."""
    return parse_record(value)


_ID_IN_REF = re.compile(r"/teams/(\d+)")


def team_id_from_ref(value: Any) -> int | None:
    """Extract the team id from an ESPN core-API ``$ref`` URL."""
    ref = _mapping(value).get("$ref")
    match = _ID_IN_REF.search(ref) if isinstance(ref, str) else None
    return int(match.group(1)) if match else None


def batch_from_rows(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build a record batch in the declared column order."""
    return pa.RecordBatch.from_arrays(
        [pa.array([row.get(value.name) for row in rows], type=value.type) for value in schema],
        schema=schema,
    )


# --------------------------------------------------------------------------- rankings

RANKINGS_SCHEMA = pa.schema(
    [
        field("season", pa.int32(), "Season, labelled by the year it ends (2025-26 is 2026)."),
        field("week", pa.int32(), "Poll week number within the season (1 is the preseason poll)."),
        field(
            "poll_id",
            pa.int32(),
            "ESPN poll identifier: 1 for the AP Top 25, 2 for the Coaches Poll.",
            nullable=False,
        ),
        field("poll_name", pa.string(), "Poll name, for example AP Top 25 or Coaches Poll."),
        field("poll_date", TIMESTAMP, "Date the poll was released."),
        field("rank", pa.int32(), "Poll position 1-25; NULL for teams only receiving votes."),
        field("previous_rank", pa.int32(), "Rank in the previous poll; NULL when unranked."),
        field("trend", pa.string(), "Movement since the previous poll, for example +3, -2 or -."),
        field("receiving_votes", pa.bool_(), "True for teams outside the top 25 that received votes."),
        field("points", pa.int32(), "Total poll points from all ballots (25 for first place down to 1)."),
        field("first_place_votes", pa.int32(), "Number of first-place ballots received."),
        field("team_id", pa.int32(), "Stable ESPN team identifier (joins teams.team_id).", nullable=False),
        field("team_location", pa.string(), "School name, for example Florida."),
        field("team_nickname", pa.string(), "Team nickname, for example Gators."),
        field("team_abbreviation", pa.string(), "Short team code, for example DUKE."),
        field("record", pa.string(), "Win-loss record at poll time."),
        field("wins", pa.int32(), "Number of games won when the poll was released."),
        field("losses", pa.int32(), "Number of games lost when the poll was released."),
    ]
)


def _rank_row(
    entry: Mapping[str, Any],
    poll: Mapping[str, Any],
    *,
    season: int | None,
    receiving_votes: bool,
    team: Mapping[str, Any],
    team_id: int | None,
) -> dict[str, Any]:
    occurrence = _mapping(poll.get("occurrence"))
    record = entry.get("recordSummary") or _mapping(entry.get("record")).get("summary")
    wins, losses = parse_record(record)
    current = to_int(entry.get("current"))
    previous = to_int(entry.get("previous"))
    return {
        "season": season,
        "week": to_int(occurrence.get("number")),
        "poll_id": to_int(poll.get("id")),
        "poll_name": to_text(poll.get("name")),
        "poll_date": to_timestamp(poll.get("date")),
        "rank": None if receiving_votes or not current else current,
        "previous_rank": previous or None,
        "trend": to_text(entry.get("trend")),
        "receiving_votes": receiving_votes,
        "points": to_int(entry.get("points")),
        "first_place_votes": to_int(entry.get("firstPlaceVotes")),
        "team_id": team_id,
        "team_location": to_text(team.get("location")),
        "team_nickname": to_text(team.get("name")),
        "team_abbreviation": to_text(team.get("abbreviation")),
        "record": to_text(record),
        "wins": wins,
        "losses": losses,
    }


def _poll_season(poll: Mapping[str, Any]) -> int | None:
    season = poll.get("season")
    return to_int(_mapping(season).get("year")) if isinstance(season, Mapping) else to_int(season)


def flatten_rankings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten ESPN's site rankings document (current AP and Coaches polls)."""
    rows: list[dict[str, Any]] = []
    for poll in _list(payload.get("rankings")):
        if not isinstance(poll, Mapping) or to_int(poll.get("id")) is None:
            continue
        season = _poll_season(poll)
        for others, entries in ((False, poll.get("ranks")), (True, poll.get("others"))):
            for entry in _list(entries):
                team = _mapping(_mapping(entry).get("team"))
                team_id = to_int(team.get("id"))
                if team_id is None:
                    continue
                rows.append(
                    _rank_row(entry, poll, season=season, receiving_votes=others, team=team, team_id=team_id)
                )
    return rows


def flatten_poll(payload: Mapping[str, Any], teams: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten one ESPN core-API weekly poll, resolving team ``$ref`` links through ``teams``."""
    rows: list[dict[str, Any]] = []
    season = _poll_season(payload)
    if to_int(payload.get("id")) is None:
        return rows
    for others, entries in ((False, payload.get("ranks")), (True, payload.get("others"))):
        for entry in _list(entries):
            team_id = team_id_from_ref(_mapping(entry).get("team"))
            if team_id is None:
                continue
            rows.append(
                _rank_row(
                    entry,
                    payload,
                    season=season,
                    receiving_votes=others,
                    team=teams.get(team_id, {}),
                    team_id=team_id,
                )
            )
    return rows


# --------------------------------------------------------------------------- standings

STANDINGS_SCHEMA = pa.schema(
    [
        field("season", pa.int32(), "Season, labelled by the year it ends (2025-26 is 2026)."),
        field("conference_id", pa.int32(), "ESPN conference (group) identifier."),
        field("conference_name", pa.string(), "Full conference name, for example Big Ten Conference."),
        field("conference_abbreviation", pa.string(), "ESPN conference abbreviation, for example big10."),
        field("team_id", pa.int32(), "Stable ESPN team identifier (joins teams.team_id).", nullable=False),
        field("team_name", pa.string(), "Full team name."),
        field("team_location", pa.string(), "School name."),
        field("team_abbreviation", pa.string(), "Short team code, for example DUKE."),
        field("conference_seed", pa.int32(), "ESPN's ordering within the conference (1 = first)."),
        field("wins", pa.int32(), "Number of games won overall."),
        field("losses", pa.int32(), "Number of games lost overall."),
        field("win_percentage", PCT3, "Share of all games won, as a 0-1 exact decimal."),
        field("conference_wins", pa.int32(), "Number of conference games won."),
        field("conference_losses", pa.int32(), "Number of conference games lost."),
        field("conference_win_percentage", PCT3, "Share of conference games won, as a 0-1 exact decimal."),
        field(
            "games_behind", ONE_DP, "Games behind the leader in ESPN's ordering, in games; the leader is 0.0."
        ),
        field("home_wins", pa.int32(), "Number of home games won."),
        field("home_losses", pa.int32(), "Number of home games lost."),
        field("road_wins", pa.int32(), "Number of road games won."),
        field("road_losses", pa.int32(), "Number of road games lost."),
        field("vs_ap_top25_wins", pa.int32(), "Number of games won against AP top-25 opponents."),
        field("vs_ap_top25_losses", pa.int32(), "Number of games lost against AP top-25 opponents."),
        field("points_for", pa.int32(), "Points scored over the season."),
        field("points_against", pa.int32(), "Points allowed over the season."),
        field("point_differential", pa.int32(), "Points scored minus points allowed, in points."),
        field("points_per_game", ONE_DP, "Average points scored per game."),
        field("opponent_points_per_game", ONE_DP, "Average points allowed per game."),
        field("streak", pa.string(), "Current streak, for example W3 or L1."),
    ]
)


def _stat_index(stats: Iterable[Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(value.get("type")): value
        for value in stats
        if isinstance(value, Mapping) and value.get("type") is not None
    }


def flatten_standings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten ESPN's Division I standings into one row per team."""
    rows: list[dict[str, Any]] = []
    for conference in _list(payload.get("children")):
        if not isinstance(conference, Mapping):
            continue
        standings = _mapping(conference.get("standings"))
        season = to_int(standings.get("season"))
        for entry in _list(standings.get("entries")):
            team = _mapping(_mapping(entry).get("team"))
            team_id = to_int(team.get("id"))
            if team_id is None:
                continue
            stats = _stat_index(_list(_mapping(entry).get("stats")))

            def value(name: str, stats: dict[str, Mapping[str, Any]] = stats) -> Any:
                return _mapping(stats.get(name)).get("value")

            def summary(name: str, stats: dict[str, Mapping[str, Any]] = stats) -> Any:
                return _mapping(stats.get(name)).get("summary")

            conf_wins, conf_losses = parse_record(summary("vsconf"))
            home_wins, home_losses = parse_record(summary("home"))
            road_wins, road_losses = parse_record(summary("road"))
            ap_wins, ap_losses = parse_record(summary("vsaprankedteams"))
            streak = _mapping(stats.get("streak")).get("displayValue")
            rows.append(
                {
                    "season": season,
                    "conference_id": to_int(conference.get("id")),
                    "conference_name": to_text(conference.get("name")),
                    "conference_abbreviation": to_text(conference.get("abbreviation")),
                    "team_id": team_id,
                    "team_name": to_text(team.get("displayName")),
                    "team_location": to_text(team.get("location")),
                    "team_abbreviation": to_text(team.get("abbreviation")),
                    "conference_seed": to_int(value("playoffseed")),
                    "wins": to_int(value("wins")),
                    "losses": to_int(value("losses")),
                    "win_percentage": to_decimal(value("winpercent"), 3),
                    "conference_wins": conf_wins,
                    "conference_losses": conf_losses,
                    "conference_win_percentage": to_decimal(value("leaguewinpercent"), 3),
                    "games_behind": to_decimal(value("gamesbehind"), 1),
                    "home_wins": home_wins,
                    "home_losses": home_losses,
                    "road_wins": road_wins,
                    "road_losses": road_losses,
                    "vs_ap_top25_wins": ap_wins,
                    "vs_ap_top25_losses": ap_losses,
                    "points_for": to_int(value("pointsfor")),
                    "points_against": to_int(value("pointsagainst")),
                    "point_differential": to_int(value("pointdifferential")),
                    "points_per_game": to_decimal(value("avgpointsfor"), 1),
                    "opponent_points_per_game": to_decimal(value("avgpointsagainst"), 1),
                    "streak": None if streak in (None, "-") else to_text(streak),
                }
            )
    return rows


CONFERENCES_SCHEMA = pa.schema(
    [
        field("conference_id", pa.int32(), "ESPN conference (group) identifier.", nullable=False),
        field("conference_name", pa.string(), "Full conference name, for example Big Ten Conference."),
        field("abbreviation", pa.string(), "ESPN conference abbreviation, for example big10."),
        field("short_name", pa.string(), "Short conference name, for example Big Ten."),
        field("team_count", pa.int32(), "Number of Division I member teams in the current standings."),
    ]
)


def flatten_conferences(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one row per conference in the current standings document."""
    rows: list[dict[str, Any]] = []
    for conference in _list(payload.get("children")):
        conference_id = to_int(_mapping(conference).get("id"))
        if conference_id is None:
            continue
        entries = _list(_mapping(conference.get("standings")).get("entries"))
        rows.append(
            {
                "conference_id": conference_id,
                "conference_name": to_text(conference.get("name")),
                "abbreviation": to_text(conference.get("abbreviation")),
                "short_name": to_text(conference.get("shortName")),
                "team_count": len(entries),
            }
        )
    return rows


def resolve_conference(payload: Mapping[str, Any], conference: str) -> int:
    """Resolve a conference id, abbreviation, or name against the standings document."""
    wanted = conference.strip().lower()
    if wanted.isdigit():
        return int(wanted)
    for row in flatten_conferences(payload):
        names = {row["abbreviation"], row["short_name"], row["conference_name"]}
        if wanted in {name.lower() for name in names if name}:
            return row["conference_id"]
    known = sorted(row["short_name"] or row["abbreviation"] or "" for row in flatten_conferences(payload))
    raise ValueError(f"unknown conference {conference!r}; expected an id or one of: {', '.join(known)}")


# --------------------------------------------------------------------------- teams

TEAMS_SCHEMA = pa.schema(
    [
        field("team_id", pa.int32(), "Stable ESPN team identifier (joins teams.team_id).", nullable=False),
        field("display_name", pa.string(), "Full team name, for example Duke Blue Devils."),
        field("location", pa.string(), "School name, for example Duke."),
        field("nickname", pa.string(), "Team nickname, for example Blue Devils."),
        field("abbreviation", pa.string(), "Short team code, for example DUKE."),
        field(
            "short_display_name",
            pa.string(),
            "Short team name used in tight layouts, for example Abilene Chrstn.",
        ),
        field("slug", pa.string(), "ESPN URL slug."),
        field("color", pa.string(), "Primary colour as a hex string without #."),
        field("alternate_color", pa.string(), "Alternate colour as a hex string without #."),
        field("is_active", pa.bool_(), "Whether ESPN marks the team active."),
        field("conference_id", pa.int32(), "Current conference id; NULL when not in current standings."),
        field("conference_name", pa.string(), "Current conference name."),
        field("conference_abbreviation", pa.string(), "Current conference abbreviation."),
    ]
)


def team_index(payload: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """Map team id to the ESPN team object from a teams document."""
    index: dict[int, Mapping[str, Any]] = {}
    for sport in _list(payload.get("sports")):
        for league in _list(_mapping(sport).get("leagues")):
            for item in _list(_mapping(league).get("teams")):
                team = _mapping(_mapping(item).get("team"))
                team_id = to_int(team.get("id"))
                if team_id is not None:
                    index[team_id] = team
    return index


def flatten_teams(teams: Mapping[str, Any], standings: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return one row per Division I team, joined to its current conference."""
    membership: dict[int, Mapping[str, Any]] = {}
    for conference in _list(_mapping(standings).get("children")):
        for entry in _list(_mapping(_mapping(conference).get("standings")).get("entries")):
            team_id = to_int(_mapping(_mapping(entry).get("team")).get("id"))
            if team_id is not None:
                membership[team_id] = conference
    rows: list[dict[str, Any]] = []
    for team_id, team in team_index(teams).items():
        conference = membership.get(team_id, {})
        rows.append(
            {
                "team_id": team_id,
                "display_name": to_text(team.get("displayName")),
                "location": to_text(team.get("location")),
                "nickname": to_text(team.get("name")),
                "abbreviation": to_text(team.get("abbreviation")),
                "short_display_name": to_text(team.get("shortDisplayName")),
                "slug": to_text(team.get("slug")),
                "color": to_text(team.get("color")),
                "alternate_color": to_text(team.get("alternateColor")),
                "is_active": to_bool(team.get("isActive")),
                "conference_id": to_int(conference.get("id")),
                "conference_name": to_text(conference.get("name")),
                "conference_abbreviation": to_text(conference.get("abbreviation")),
            }
        )
    return rows


# --------------------------------------------------------------------------- games

GAMES_SCHEMA = pa.schema(
    [
        field("event_id", pa.int32(), "Stable ESPN event (game) identifier.", nullable=False),
        field("season", pa.int32(), "Season, labelled by the year it ends (2026-27 is 2027)."),
        field("season_type", pa.int32(), "ESPN season-type code: 2 regular season, 3 postseason."),
        field("start_time", TIMESTAMP, "Scheduled tip-off time in UTC."),
        field("name", pa.string(), "Matchup name, for example Wagner Seahawks at UConn Huskies."),
        field("short_name", pa.string(), "Short matchup name, for example WAG @ CONN."),
        field("status", pa.string(), "ESPN status code, for example STATUS_FINAL or STATUS_SCHEDULED."),
        field("status_detail", pa.string(), "Human-readable status, for example Final or 2nd Half 5:32."),
        field("completed", pa.bool_(), "Whether the game is final."),
        field("period", pa.int32(), "Current or final period (halves for men, quarters for women)."),
        field("clock", pa.string(), "Game clock display value."),
        field("neutral_site", pa.bool_(), "Whether the game is at a neutral site."),
        field(
            "conference_game",
            pa.bool_(),
            "Whether both teams are in the same conference, including conference-tournament games "
            "(as ESPN flags them). In team_schedule it is derived from the season's standings for "
            "regular-season games and is NULL for NCAA postseason games.",
        ),
        field("event_note", pa.string(), "Event headline such as a tournament name, when present."),
        field("venue_name", pa.string(), "Arena name."),
        field("venue_city", pa.string(), "Arena city."),
        field("venue_state", pa.string(), "Arena state."),
        field("attendance", pa.int32(), "Reported crowd size in people."),
        field("home_team_id", pa.int32(), "ESPN team identifier of the home team (joins teams.team_id)."),
        field("home_team_name", pa.string(), "Home team full name."),
        field("home_team_abbreviation", pa.string(), "Short code of the home team, for example CONN."),
        field(
            "home_rank", pa.int32(), "Home team's AP poll position (1-25) at game time; NULL when unranked."
        ),
        field("home_score", pa.int32(), "Points scored by the home team so far or at the final."),
        field("home_winner", pa.bool_(), "Whether the home team won."),
        field("away_team_id", pa.int32(), "ESPN team identifier of the away team (joins teams.team_id)."),
        field("away_team_name", pa.string(), "Away team full name."),
        field("away_team_abbreviation", pa.string(), "Short code of the away team, for example WAG."),
        field(
            "away_rank", pa.int32(), "Away team's AP poll position (1-25) at game time; NULL when unranked."
        ),
        field("away_score", pa.int32(), "Points scored by the away team so far or at the final."),
        field("away_winner", pa.bool_(), "Whether the away team won."),
    ]
)


def _score(value: Any) -> int | None:
    if isinstance(value, Mapping):
        return to_int(value.get("value"))
    return to_int(value)


def _rank(competitor: Mapping[str, Any]) -> int | None:
    rank = to_int(_mapping(competitor.get("curatedRank")).get("current"))
    return rank if rank is not None and 1 <= rank <= 25 else None


def conference_index(standings: Mapping[str, Any]) -> dict[int, int]:
    """Map each team id in a standings document to its conference id."""
    return {
        row["team_id"]: row["conference_id"]
        for row in flatten_standings(standings)
        if row["conference_id"] is not None
    }


def _derived_conference_game(row: Mapping[str, Any], conferences: Mapping[int, int]) -> bool | None:
    """Infer a regular-season conference game from both teams' conferences.

    ESPN's team schedule omits ``conferenceCompetition``. In ESPN's regular
    season (which includes conference tournaments, flagged as conference games
    on its scoreboard) two members of the same conference meet only in
    conference play, and a Division I team that meets a team outside the
    standings (a non-Division I opponent) is never in a conference game. NCAA
    postseason games (season type 3) stay NULL.
    """
    if row["season_type"] != 2:
        return None
    home, away = conferences.get(row["home_team_id"]), conferences.get(row["away_team_id"])
    if home is None and away is None:
        return None
    return home is not None and home == away


def flatten_events(
    events: Iterable[Any], conferences: Mapping[int, int] | None = None
) -> list[dict[str, Any]]:
    """Flatten scoreboard or schedule events into one row per game.

    ``conferences`` (team id to conference id, from :func:`conference_index`)
    fills ``conference_game`` when ESPN leaves it out, as team schedules do.
    """
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for event in events:
        event = _mapping(event)
        event_id = to_int(event.get("id"))
        if event_id is None or event_id in seen:
            continue
        seen.add(event_id)
        competition = _mapping(next(iter(_list(event.get("competitions"))), {}))
        status = _mapping(competition.get("status")) or _mapping(event.get("status"))
        status_type = _mapping(status.get("type"))
        venue = _mapping(competition.get("venue"))
        address = _mapping(venue.get("address"))
        sides: dict[str, Mapping[str, Any]] = {}
        for competitor in _list(competition.get("competitors")):
            side = _mapping(competitor).get("homeAway")
            if side in {"home", "away"}:
                sides[side] = competitor
        note = next(
            (to_text(_mapping(value).get("headline")) for value in _list(competition.get("notes"))),
            None,
        )
        season = _mapping(event.get("season"))
        season_type = event.get("seasonType")
        row: dict[str, Any] = {
            "event_id": event_id,
            "season": to_int(season.get("year")),
            "season_type": to_int(
                _mapping(season_type).get("type") if isinstance(season_type, Mapping) else season.get("type")
            ),
            "start_time": to_timestamp(event.get("date")),
            "name": to_text(event.get("name")),
            "short_name": to_text(event.get("shortName")),
            "status": to_text(status_type.get("name")),
            "status_detail": to_text(status_type.get("shortDetail")),
            "completed": to_bool(status_type.get("completed")),
            "period": to_int(status.get("period")),
            "clock": to_text(status.get("displayClock")),
            "neutral_site": to_bool(competition.get("neutralSite")),
            "conference_game": to_bool(competition.get("conferenceCompetition")),
            "event_note": note,
            "venue_name": to_text(venue.get("fullName")),
            "venue_city": to_text(address.get("city")),
            "venue_state": to_text(address.get("state")),
            "attendance": to_int(competition.get("attendance")) or None,
        }
        for side in ("home", "away"):
            competitor = _mapping(sides.get(side))
            team = _mapping(competitor.get("team"))
            row[f"{side}_team_id"] = to_int(team.get("id"))
            row[f"{side}_team_name"] = to_text(team.get("displayName"))
            row[f"{side}_team_abbreviation"] = to_text(team.get("abbreviation"))
            row[f"{side}_rank"] = _rank(competitor)
            row[f"{side}_score"] = _score(competitor.get("score"))
            row[f"{side}_winner"] = to_bool(competitor.get("winner"))
        if row["conference_game"] is None and conferences:
            row["conference_game"] = _derived_conference_game(row, conferences)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- roster

ROSTER_SCHEMA = pa.schema(
    [
        field("team_id", pa.int32(), "ESPN team identifier (joins teams.team_id).", nullable=False),
        field(
            "player_id",
            pa.int32(),
            "Stable ESPN athlete identifier, shared by rosters, box scores and stats.",
            nullable=False,
        ),
        field("full_name", pa.string(), "Player full name."),
        field("first_name", pa.string(), "First name."),
        field("last_name", pa.string(), "Last name."),
        field("jersey", pa.string(), "Jersey number as printed (may have a leading zero)."),
        field("position", pa.string(), "Position abbreviation: G, F or C."),
        field("height_inches", pa.int32(), "Height in inches."),
        field("display_height", pa.string(), "Height as displayed, for example 6' 4\"."),
        field(
            "weight_lbs", pa.int32(), "Weight in pounds; ESPN generally omits it (NULL) for women's rosters."
        ),
        field("class_year", pa.string(), "Academic class abbreviation: FR, SO, JR or SR."),
        field("class_display", pa.string(), "Academic class name, for example Sophomore."),
        field("birth_city", pa.string(), "Hometown city."),
        field("birth_state", pa.string(), "Hometown state or province."),
        field("birth_country", pa.string(), "Hometown country."),
        field("status", pa.string(), "Roster status, for example Active."),
    ]
)


def flatten_roster(payload: Mapping[str, Any], team_id: int) -> list[dict[str, Any]]:
    """Flatten a team roster into one row per player."""
    rows: list[dict[str, Any]] = []
    for athlete in _list(payload.get("athletes")):
        athlete = _mapping(athlete)
        player_id = to_int(athlete.get("id"))
        if player_id is None:
            continue
        birth = _mapping(athlete.get("birthPlace"))
        experience = _mapping(athlete.get("experience"))
        rows.append(
            {
                "team_id": team_id,
                "player_id": player_id,
                "full_name": to_text(athlete.get("fullName") or athlete.get("displayName")),
                "first_name": to_text(athlete.get("firstName")),
                "last_name": to_text(athlete.get("lastName")),
                "jersey": to_text(athlete.get("jersey")),
                "position": to_text(_mapping(athlete.get("position")).get("abbreviation")),
                "height_inches": to_int(athlete.get("height")),
                "display_height": to_text(athlete.get("displayHeight")),
                "weight_lbs": to_int(athlete.get("weight")),
                "class_year": to_text(experience.get("abbreviation")),
                "class_display": to_text(experience.get("displayValue")),
                "birth_city": to_text(birth.get("city")),
                "birth_state": to_text(birth.get("state")),
                "birth_country": to_text(birth.get("country")),
                "status": to_text(_mapping(athlete.get("status")).get("name")),
            }
        )
    return rows


# --------------------------------------------------------------------------- player stats

PLAYER_STATS_SCHEMA = pa.schema(
    [
        field("season", pa.int32(), "Season, labelled by the year it ends (2025-26 is 2026)."),
        field(
            "player_id",
            pa.int32(),
            "Stable ESPN athlete identifier, shared by rosters, box scores and stats.",
            nullable=False,
        ),
        field("player_name", pa.string(), "Player display name."),
        field("position", pa.string(), "Position abbreviation."),
        field("team_id", pa.int32(), "ESPN team identifier (joins teams.team_id)."),
        field("team_name", pa.string(), "Team full name."),
        field("team_abbreviation", pa.string(), "Short team code, for example DUKE."),
        field("games_played", pa.int32(), "Number of games played."),
        field("minutes", pa.int32(), "Minutes on the court over the season."),
        field("minutes_per_game", ONE_DP, "Average minutes played per game."),
        field("points", pa.int32(), "Points scored over the season."),
        field("points_per_game", ONE_DP, "Average points scored per game played."),
        field("rebounds", pa.int32(), "Count of rebounds over the season."),
        field("rebounds_per_game", ONE_DP, "Average rebounds per game played."),
        field("assists", pa.int32(), "Count of assists over the season."),
        field("assists_per_game", ONE_DP, "Average assists per game played."),
        field("steals", pa.int32(), "Count of steals over the season."),
        field("steals_per_game", ONE_DP, "Average steals per game played."),
        field("blocks", pa.int32(), "Count of blocked shots over the season."),
        field("blocks_per_game", ONE_DP, "Average blocked shots per game played."),
        field("turnovers", pa.int32(), "Count of turnovers committed over the season."),
        field("field_goals_made", pa.int32(), "Count of field goals made (twos and threes)."),
        field("field_goals_attempted", pa.int32(), "Count of field-goal attempts (twos and threes)."),
        field("field_goal_pct", ONE_DP, "Field-goal percentage on a 0-100 scale."),
        field("three_pointers_made", pa.int32(), "Count of three-point field goals made."),
        field("three_pointers_attempted", pa.int32(), "Count of three-point attempts."),
        field("three_point_pct", ONE_DP, "Three-point percentage on a 0-100 scale."),
        field("free_throws_made", pa.int32(), "Count of free throws made."),
        field("free_throws_attempted", pa.int32(), "Count of free-throw attempts."),
        field("free_throw_pct", ONE_DP, "Free-throw percentage on a 0-100 scale."),
        field("fouls", pa.int32(), "Count of personal fouls committed."),
        field("double_doubles", pa.int32(), "Number of games with 10+ in two statistical categories."),
        field("triple_doubles", pa.int32(), "Number of games with 10+ in three statistical categories."),
    ]
)

# Output column -> (ESPN category, ESPN stat name, decimal places or None for integers).
_PLAYER_STAT_MAP: dict[str, tuple[str, str, int | None]] = {
    "games_played": ("general", "gamesPlayed", None),
    "minutes": ("general", "minutes", None),
    "minutes_per_game": ("general", "avgMinutes", 1),
    "points": ("offensive", "points", None),
    "points_per_game": ("offensive", "avgPoints", 1),
    "rebounds": ("general", "rebounds", None),
    "rebounds_per_game": ("general", "avgRebounds", 1),
    "assists": ("offensive", "assists", None),
    "assists_per_game": ("offensive", "avgAssists", 1),
    "steals": ("defensive", "steals", None),
    "steals_per_game": ("defensive", "avgSteals", 1),
    "blocks": ("defensive", "blocks", None),
    "blocks_per_game": ("defensive", "avgBlocks", 1),
    "turnovers": ("offensive", "turnovers", None),
    "field_goals_made": ("offensive", "fieldGoalsMade", None),
    "field_goals_attempted": ("offensive", "fieldGoalsAttempted", None),
    "field_goal_pct": ("offensive", "fieldGoalPct", 1),
    "three_pointers_made": ("offensive", "threePointFieldGoalsMade", None),
    "three_pointers_attempted": ("offensive", "threePointFieldGoalsAttempted", None),
    "three_point_pct": ("offensive", "threePointFieldGoalPct", 1),
    "free_throws_made": ("offensive", "freeThrowsMade", None),
    "free_throws_attempted": ("offensive", "freeThrowsAttempted", None),
    "free_throw_pct": ("offensive", "freeThrowPct", 1),
    "fouls": ("general", "fouls", None),
    "double_doubles": ("general", "doubleDouble", None),
    "triple_doubles": ("general", "tripleDouble", None),
}


def flatten_player_stats(
    categories: Sequence[Mapping[str, Any]], athletes: Iterable[Any], season: int | None
) -> list[dict[str, Any]]:
    """Flatten ESPN byathlete pages using the response-level category name layout."""
    layout = {
        str(category.get("name")): {
            str(name): index for index, name in enumerate(_list(category.get("names")))
        }
        for category in categories
    }
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in athletes:
        item = _mapping(item)
        athlete = _mapping(item.get("athlete"))
        player_id = to_int(athlete.get("id"))
        if player_id is None or player_id in seen:
            continue
        seen.add(player_id)
        values = {
            str(_mapping(category).get("name")): _list(_mapping(category).get("values"))
            for category in _list(item.get("categories"))
        }
        team = _mapping(next(iter(_list(athlete.get("teams"))), {}))
        row: dict[str, Any] = {
            "season": season,
            "player_id": player_id,
            "player_name": to_text(athlete.get("displayName")),
            "position": to_text(_mapping(athlete.get("position")).get("abbreviation")),
            "team_id": to_int(athlete.get("teamId") or team.get("id")),
            "team_name": to_text(team.get("displayName")),
            "team_abbreviation": to_text(team.get("abbreviation") or athlete.get("teamShortName")),
        }
        for column, (category, name, places) in _PLAYER_STAT_MAP.items():
            index = layout.get(category, {}).get(name)
            raw = values.get(category, [])
            stat = raw[index] if index is not None and index < len(raw) else None
            row[column] = to_int(stat) if places is None else to_decimal(stat, places)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- box scores

BOX_SCORE_SCHEMA = pa.schema(
    [
        field("event_id", pa.int32(), "ESPN event identifier.", nullable=False),
        field("team_id", pa.int32(), "ESPN team identifier (joins teams.team_id).", nullable=False),
        field("team_abbreviation", pa.string(), "Short team code, for example DUKE."),
        field("home_away", pa.string(), "home or away."),
        field(
            "player_id",
            pa.int32(),
            "Stable ESPN athlete identifier, shared by rosters, box scores and stats.",
            nullable=False,
        ),
        field("player_name", pa.string(), "Player display name."),
        field("jersey", pa.string(), "Jersey number."),
        field("position", pa.string(), "Position abbreviation."),
        field("starter", pa.bool_(), "Whether the player started."),
        field("did_not_play", pa.bool_(), "Whether the player did not play; stat columns are then NULL."),
        field("minutes", pa.int32(), "Minutes on the court in the game."),
        field("points", pa.int32(), "Points scored in the game."),
        field("field_goals_made", pa.int32(), "Count of field goals made (twos and threes)."),
        field("field_goals_attempted", pa.int32(), "Count of field-goal attempts (twos and threes)."),
        field("three_pointers_made", pa.int32(), "Count of three-point field goals made."),
        field("three_pointers_attempted", pa.int32(), "Count of three-point attempts."),
        field("free_throws_made", pa.int32(), "Count of free throws made."),
        field("free_throws_attempted", pa.int32(), "Count of free-throw attempts."),
        field("rebounds", pa.int32(), "Count of rebounds over the season."),
        field("offensive_rebounds", pa.int32(), "Count of offensive rebounds."),
        field("defensive_rebounds", pa.int32(), "Count of defensive rebounds."),
        field("assists", pa.int32(), "Count of assists in the game."),
        field("steals", pa.int32(), "Count of steals in the game."),
        field("blocks", pa.int32(), "Count of shots blocked in the game."),
        field("turnovers", pa.int32(), "Count of turnovers committed in the game."),
        field("fouls", pa.int32(), "Count of personal fouls committed."),
    ]
)

_PAIRS = {
    "fieldGoalsMade-fieldGoalsAttempted": ("field_goals_made", "field_goals_attempted"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": (
        "three_pointers_made",
        "three_pointers_attempted",
    ),
    "freeThrowsMade-freeThrowsAttempted": ("free_throws_made", "free_throws_attempted"),
}
_PLAYER_SINGLES = {
    "minutes": "minutes",
    "points": "points",
    "rebounds": "rebounds",
    "offensiveRebounds": "offensive_rebounds",
    "defensiveRebounds": "defensive_rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "fouls": "fouls",
}


def _home_away(payload: Mapping[str, Any]) -> dict[int, str]:
    sides: dict[int, str] = {}
    for team in _list(_mapping(payload.get("boxscore")).get("teams")):
        team_id = to_int(_mapping(_mapping(team).get("team")).get("id"))
        if team_id is not None and team.get("homeAway") in {"home", "away"}:
            sides[team_id] = team["homeAway"]
    for competition in _list(_mapping(payload.get("header")).get("competitions")):
        for competitor in _list(_mapping(competition).get("competitors")):
            team_id = to_int(_mapping(competitor).get("id"))
            if team_id is not None and competitor.get("homeAway") in {"home", "away"}:
                sides.setdefault(team_id, competitor["homeAway"])
    return sides


def flatten_box_score(payload: Mapping[str, Any], event_id: int) -> list[dict[str, Any]]:
    """Flatten a game summary's player box score into one row per player."""
    sides = _home_away(payload)
    rows: list[dict[str, Any]] = []
    for team_block in _list(_mapping(payload.get("boxscore")).get("players")):
        team = _mapping(_mapping(team_block).get("team"))
        team_id = to_int(team.get("id"))
        if team_id is None:
            continue
        for block in _list(team_block.get("statistics")):
            keys = [str(key) for key in _list(_mapping(block).get("keys"))]
            for athlete_row in _list(_mapping(block).get("athletes")):
                athlete_row = _mapping(athlete_row)
                athlete = _mapping(athlete_row.get("athlete"))
                player_id = to_int(athlete.get("id"))
                if player_id is None:
                    continue
                stats = dict(zip(keys, _list(athlete_row.get("stats")), strict=False))
                row: dict[str, Any] = {
                    "event_id": event_id,
                    "team_id": team_id,
                    "team_abbreviation": to_text(team.get("abbreviation")),
                    "home_away": sides.get(team_id),
                    "player_id": player_id,
                    "player_name": to_text(athlete.get("displayName")),
                    "jersey": to_text(athlete.get("jersey")),
                    "position": to_text(_mapping(athlete.get("position")).get("abbreviation")),
                    "starter": to_bool(athlete_row.get("starter")),
                    "did_not_play": to_bool(athlete_row.get("didNotPlay")),
                }
                for key, (made, attempted) in _PAIRS.items():
                    row[made], row[attempted] = parse_made_attempted(stats.get(key))
                for key, column in _PLAYER_SINGLES.items():
                    row[column] = to_int(stats.get(key))
                rows.append(row)
    return rows


TEAM_BOX_SCORE_SCHEMA = pa.schema(
    [
        field("event_id", pa.int32(), "ESPN event identifier.", nullable=False),
        field("team_id", pa.int32(), "ESPN team identifier (joins teams.team_id).", nullable=False),
        field("team_name", pa.string(), "Team full name."),
        field("team_abbreviation", pa.string(), "Short team code, for example DUKE."),
        field("home_away", pa.string(), "home or away."),
        field("points", pa.int32(), "Final or current points."),
        field("winner", pa.bool_(), "Whether the team won."),
        field("field_goals_made", pa.int32(), "Count of field goals made (twos and threes)."),
        field("field_goals_attempted", pa.int32(), "Count of field-goal attempts (twos and threes)."),
        field("field_goal_pct", ONE_DP, "Field-goal percentage on a 0-100 scale."),
        field("three_pointers_made", pa.int32(), "Count of three-point field goals made."),
        field("three_pointers_attempted", pa.int32(), "Count of three-point attempts."),
        field("three_point_pct", ONE_DP, "Three-point percentage on a 0-100 scale."),
        field("free_throws_made", pa.int32(), "Count of free throws made."),
        field("free_throws_attempted", pa.int32(), "Count of free-throw attempts."),
        field("free_throw_pct", ONE_DP, "Free-throw percentage on a 0-100 scale."),
        field("rebounds", pa.int32(), "Count of rebounds over the season."),
        field("offensive_rebounds", pa.int32(), "Count of offensive rebounds."),
        field("defensive_rebounds", pa.int32(), "Count of defensive rebounds."),
        field("assists", pa.int32(), "Count of assists in the game."),
        field("steals", pa.int32(), "Count of steals in the game."),
        field("blocks", pa.int32(), "Count of shots blocked in the game."),
        field("turnovers", pa.int32(), "Total turnovers including team turnovers."),
        field("fouls", pa.int32(), "Count of personal fouls committed."),
        field("technical_fouls", pa.int32(), "Technical fouls."),
        field("points_off_turnovers", pa.int32(), "Points scored off opponent turnovers."),
        field("fast_break_points", pa.int32(), "Fast-break points."),
        field("points_in_paint", pa.int32(), "Points in the paint."),
        field("largest_lead", pa.int32(), "Largest lead held."),
    ]
)

_TEAM_SINGLES = {
    "totalRebounds": "rebounds",
    "offensiveRebounds": "offensive_rebounds",
    "defensiveRebounds": "defensive_rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "totalTurnovers": "turnovers",
    "fouls": "fouls",
    "technicalFouls": "technical_fouls",
    "turnoverPoints": "points_off_turnovers",
    "fastBreakPoints": "fast_break_points",
    "pointsInPaint": "points_in_paint",
    "largestLead": "largest_lead",
}
_TEAM_PCTS = ("field_goal_pct", "three_point_pct", "free_throw_pct")


def flatten_team_box_score(payload: Mapping[str, Any], event_id: int) -> list[dict[str, Any]]:
    """Flatten a game summary's team statistics into one row per team."""
    header_teams: dict[int, Mapping[str, Any]] = {}
    for competition in _list(_mapping(payload.get("header")).get("competitions")):
        for competitor in _list(_mapping(competition).get("competitors")):
            team_id = to_int(_mapping(competitor).get("id"))
            if team_id is not None:
                header_teams[team_id] = competitor
    sides = _home_away(payload)
    rows: list[dict[str, Any]] = []
    for team_block in _list(_mapping(payload.get("boxscore")).get("teams")):
        team = _mapping(_mapping(team_block).get("team"))
        team_id = to_int(team.get("id"))
        if team_id is None:
            continue
        stats = {
            str(_mapping(value).get("name")): _mapping(value).get("displayValue")
            for value in _list(team_block.get("statistics"))
        }
        header = _mapping(header_teams.get(team_id))
        row: dict[str, Any] = {
            "event_id": event_id,
            "team_id": team_id,
            "team_name": to_text(team.get("displayName")),
            "team_abbreviation": to_text(team.get("abbreviation")),
            "home_away": sides.get(team_id),
            "points": to_int(header.get("score")),
            "winner": to_bool(header.get("winner")),
        }
        for key, (made, attempted) in _PAIRS.items():
            row[made], row[attempted] = parse_made_attempted(stats.get(key))
        for (made, attempted), column in zip(_PAIRS.values(), _TEAM_PCTS, strict=True):
            # ESPN's team percentage display values are rounded to whole numbers.
            row[column] = (
                to_decimal(Decimal(100 * row[made]) / Decimal(row[attempted]), 1)
                if row[made] is not None and row[attempted]
                else None
            )
        for key, column in _TEAM_SINGLES.items():
            row[column] = to_int(stats.get(key))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- play-by-play

PLAY_BY_PLAY_SCHEMA = pa.schema(
    [
        field("event_id", pa.int32(), "ESPN event identifier.", nullable=False),
        field("play_id", pa.int64(), "ESPN play identifier, unique within the game.", nullable=False),
        field("sequence_number", pa.int64(), "Ordering key; sort by this for chronological order."),
        field("period", pa.int32(), "Period number (halves for men, quarters for women; 3+ is overtime)."),
        field("period_display", pa.string(), "Period label, for example 1st Half."),
        field("clock", pa.string(), "Game clock at the play, counting down."),
        field("wallclock", TIMESTAMP, "Real-world UTC time of the play, when recorded."),
        field("play_type_id", pa.int32(), "ESPN play type identifier."),
        field("play_type", pa.string(), "Play type, for example JumpShot or Defensive Rebound."),
        field("text", pa.string(), "Play-by-play description."),
        field("team_id", pa.int32(), "Team credited with the play."),
        field("player_id", pa.int32(), "ESPN athlete identifier of the primary player on the play."),
        field("secondary_player_id", pa.int32(), "Secondary athlete, for example the assisting player."),
        field("scoring_play", pa.bool_(), "Whether points were scored."),
        field("shooting_play", pa.bool_(), "Whether the play was a shot attempt."),
        field("score_value", pa.int32(), "Points scored on the play."),
        field("points_attempted", pa.int32(), "Points attempted on the play (1, 2 or 3 for shots)."),
        field("home_score", pa.int32(), "Home team's running score in points after the play."),
        field("away_score", pa.int32(), "Away team's running score in points after the play."),
        field("coordinate_x", pa.int32(), "Shot-chart x coordinate in ESPN's half-court frame."),
        field("coordinate_y", pa.int32(), "Shot-chart y coordinate in ESPN's half-court frame."),
    ]
)


def _coordinate(value: Any) -> int | None:
    number = to_int(value)
    # ESPN uses a large negative sentinel for plays without a location.
    return number if number is not None and abs(number) < 1_000 else None


def flatten_play_by_play(payload: Mapping[str, Any], event_id: int) -> list[dict[str, Any]]:
    """Flatten a game summary's plays into one row per play."""
    rows: list[dict[str, Any]] = []
    for play in _list(payload.get("plays")):
        play = _mapping(play)
        play_id = to_int(play.get("id"))
        if play_id is None:
            continue
        participants = [
            to_int(_mapping(_mapping(value).get("athlete")).get("id"))
            for value in _list(play.get("participants"))
        ]
        period = _mapping(play.get("period"))
        coordinate = _mapping(play.get("coordinate"))
        rows.append(
            {
                "event_id": event_id,
                "play_id": play_id,
                "sequence_number": to_int(play.get("sequenceNumber")),
                "period": to_int(period.get("number")),
                "period_display": to_text(period.get("displayValue")),
                "clock": to_text(_mapping(play.get("clock")).get("displayValue")),
                "wallclock": to_timestamp(play.get("wallclock")),
                "play_type_id": to_int(_mapping(play.get("type")).get("id")),
                "play_type": to_text(_mapping(play.get("type")).get("text")),
                "text": to_text(play.get("text")),
                "team_id": to_int(_mapping(play.get("team")).get("id")),
                "player_id": participants[0] if participants else None,
                "secondary_player_id": participants[1] if len(participants) > 1 else None,
                "scoring_play": to_bool(play.get("scoringPlay")),
                "shooting_play": to_bool(play.get("shootingPlay")),
                "score_value": to_int(play.get("scoreValue")),
                "points_attempted": to_int(play.get("pointsAttempted")),
                "home_score": to_int(play.get("homeScore")),
                "away_score": to_int(play.get("awayScore")),
                "coordinate_x": _coordinate(coordinate.get("x")),
                "coordinate_y": _coordinate(coordinate.get("y")),
            }
        )
    return rows


# --------------------------------------------------------------------------- ncaa-api

NET_SCHEMA = pa.schema(
    [
        field("rank", pa.int32(), "Official NCAA NET position, 1 being the strongest team.", nullable=False),
        field("previous_rank", pa.int32(), "NET rank in the previous release."),
        field("school", pa.string(), "School name as published by the NCAA.", nullable=False),
        field("conference", pa.string(), "Conference name as published by the NCAA."),
        field("record", pa.string(), "Overall win-loss record."),
        field("wins", pa.int32(), "Number of games won overall."),
        field("losses", pa.int32(), "Number of games lost overall."),
        field("home_record", pa.string(), "Win-loss record in home games, for example 15-0."),
        field("road_record", pa.string(), "Win-loss record in road games, for example 10-1."),
        field("neutral_record", pa.string(), "Neutral-site record."),
        field("non_division_i_record", pa.string(), "Record against non-Division I opponents."),
        field("quad1_record", pa.string(), "Record in Quadrant 1 games."),
        field("quad1_wins", pa.int32(), "Number of Quadrant 1 games won."),
        field("quad2_record", pa.string(), "Record in Quadrant 2 games."),
        field("quad3_record", pa.string(), "Record in Quadrant 3 games."),
        field("quad4_record", pa.string(), "Record in Quadrant 4 games."),
        field("updated", pa.string(), "The NCAA's as-of note, for example Through Games Apr. 06 2026."),
    ]
)


def flatten_net(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten ncaa-api NET rankings rows."""
    rows: list[dict[str, Any]] = []
    updated = to_text(payload.get("updated"))
    for item in _list(payload.get("data")):
        item = _mapping(item)
        rank = to_int(item.get("Rank"))
        school = to_text(item.get("School"))
        if rank is None or school is None:
            continue
        wins, losses = parse_record(item.get("Record"))
        quad1_wins, _ = parse_record(item.get("Quad 1"))
        rows.append(
            {
                "rank": rank,
                "previous_rank": to_int(item.get("Prev")),
                "school": school,
                "conference": to_text(item.get("Conf")),
                "record": to_text(item.get("Record")),
                "wins": wins,
                "losses": losses,
                "home_record": to_text(item.get("Home")),
                "road_record": to_text(item.get("Road")),
                "neutral_record": to_text(item.get("Neutral")),
                "non_division_i_record": to_text(item.get("Non-Div I")),
                "quad1_record": to_text(item.get("Quad 1")),
                "quad1_wins": quad1_wins,
                "quad2_record": to_text(item.get("Quad 2")),
                "quad3_record": to_text(item.get("Quad 3")),
                "quad4_record": to_text(item.get("Quad 4")),
                "updated": updated,
            }
        )
    return rows


STAT_LEADERS_SCHEMA = pa.schema(
    [
        field(
            "stat_id", pa.int32(), "NCAA statistic identifier from the ncaa.com stats URL.", nullable=False
        ),
        field("category", pa.string(), "individual or team."),
        field("stat_title", pa.string(), "Statistic title, for example Points Per Game."),
        field("rank", pa.int32(), "Official leaderboard position; tied rows repeat the position above them."),
        field("tied", pa.bool_(), "True when the NCAA lists the row as tied with the one above."),
        field("player_name", pa.string(), "Player name; NULL for team leaderboards."),
        field("team", pa.string(), "School name as published by the NCAA."),
        field("class_year", pa.string(), "Academic class, for example Sr."),
        field("position", pa.string(), "Player position."),
        field("games", pa.int32(), "Number of games played."),
        field("stat_column", pa.string(), "Name of the ranked column, for example PPG."),
        field("stat_value", STAT_VALUE, "Value of the ranked column."),
        field("columns_json", pa.string(), "Every published column for the row as a JSON object."),
        field("updated", pa.string(), "The NCAA's as-of note for the leaderboard."),
    ]
)


def flatten_stat_leaders(
    rows: Sequence[Mapping[str, Any]],
    *,
    stat_id: int,
    category: str,
    title: str | None,
    updated: str | None,
) -> list[dict[str, Any]]:
    """Flatten an official NCAA leaderboard; the last published column is the ranked statistic."""
    result: list[dict[str, Any]] = []
    previous_rank: int | None = None
    for item in rows:
        columns = [key for key in item if key != "Rank"]
        stat_column = columns[-1] if columns else None
        rank = to_int(item.get("Rank"))
        tied = rank is None and previous_rank is not None
        rank = rank if rank is not None else previous_rank
        previous_rank = rank
        result.append(
            {
                "stat_id": stat_id,
                "category": category,
                "stat_title": to_text(title),
                "rank": rank,
                "tied": tied,
                "player_name": to_text(item.get("Name")),
                "team": to_text(item.get("Team")),
                "class_year": to_text(item.get("Cl")),
                "position": to_text(item.get("Position")),
                "games": to_int(item.get("G") or item.get("GM")),
                "stat_column": stat_column,
                "stat_value": to_decimal(item.get(stat_column), 3) if stat_column else None,
                "columns_json": json.dumps(dict(item), separators=(",", ":"), ensure_ascii=False),
                "updated": to_text(updated),
            }
        )
    return result


STAT_CATEGORIES_SCHEMA = pa.schema(
    [
        field(
            "category",
            pa.string(),
            "individual for player leaderboards or team for team leaderboards.",
            nullable=False,
        ),
        field("stat_id", pa.int32(), "NCAA leaderboard identifier to pass to stat_leaders.", nullable=False),
        field("name", pa.string(), "Leaderboard name, for example Points Per Game or Scoring Offense."),
    ]
)


def flatten_stat_categories(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten the ncaa-api statistics index into one row per leaderboard."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for category in ("individual", "team"):
        for item in _list(payload.get(category)):
            stat_id = to_int(_mapping(item).get("id"))
            if stat_id is None or (category, stat_id) in seen:
                continue
            seen.add((category, stat_id))
            rows.append({"category": category, "stat_id": stat_id, "name": to_text(item.get("name"))})
    return rows


BRACKET_SCHEMA = pa.schema(
    [
        field("season", pa.int32(), "Year the championship was played (2026 for the 2025-26 season)."),
        field("contest_id", pa.int32(), "NCAA contest identifier.", nullable=False),
        field("round_number", pa.int32(), "Round: 1 First Four, 2 First Round ... 6 Final Four, 7 title."),
        field("round_title", pa.string(), "Round name, for example Sweet 16."),
        field("region", pa.string(), "Bracket region, for example EAST; NULL for First Four and Final Four."),
        field("bracket_position_id", pa.int32(), "NCAA bracket slot identifier."),
        field("game_state", pa.string(), "NCAA game state: P pre-game, I in progress, F final."),
        field("start_date", pa.date32(), "Game date (US Eastern calendar date)."),
        field("start_time", TIMESTAMP, "Scheduled start in UTC."),
        field("title", pa.string(), "Matchup title, for example UConn vs Michigan."),
        field("top_team", pa.string(), "Top-listed team short name."),
        field("top_seed", pa.int32(), "Top-listed team seed."),
        field("top_score", pa.int32(), "Top-listed team score."),
        field("top_winner", pa.bool_(), "Whether the top-listed team won."),
        field("bottom_team", pa.string(), "Bottom-listed team short name."),
        field("bottom_seed", pa.int32(), "Bottom-listed team seed."),
        field("bottom_score", pa.int32(), "Bottom-listed team score."),
        field("bottom_winner", pa.bool_(), "Whether the bottom-listed team won."),
        field("winner", pa.string(), "Winning team short name once final."),
        field("broadcaster", pa.string(), "Television network."),
    ]
)


def _date_mdy(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None


def flatten_bracket(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten an ncaa-api tournament bracket into one row per game."""
    rows: list[dict[str, Any]] = []
    for championship in _list(payload.get("championships")):
        championship = _mapping(championship)
        rounds = {
            to_int(_mapping(value).get("roundNumber")): to_text(
                html.unescape(str(value.get("title") or "")).replace("\u00ae", "")
            )
            for value in _list(championship.get("rounds"))
        }
        regions = {
            to_int(_mapping(value).get("sectionId")): to_text(value.get("title"))
            for value in _list(championship.get("regions"))
        }
        season = to_int(championship.get("year"))
        for game in _list(championship.get("games")):
            game = _mapping(game)
            contest_id = to_int(game.get("contestId"))
            if contest_id is None:
                continue
            position = to_int(game.get("bracketPositionId"))
            round_number = position // 100 if position is not None else None
            teams = [_mapping(value) for value in _list(game.get("teams"))]
            top = next((team for team in teams if team.get("isTop") is True), {})
            bottom = next((team for team in teams if team.get("isTop") is False), {})
            epoch = to_int(game.get("startTimeEpoch"))
            winner = next((team for team in teams if team.get("isWinner") is True), {})
            rows.append(
                {
                    "season": season,
                    "contest_id": contest_id,
                    "round_number": round_number,
                    "round_title": rounds.get(round_number),
                    "region": regions.get(to_int(game.get("sectionId"))),
                    "bracket_position_id": position,
                    "game_state": to_text(game.get("gameState")),
                    "start_date": _date_mdy(game.get("startDate")),
                    "start_time": datetime.fromtimestamp(epoch, UTC) if epoch else None,
                    "title": to_text(game.get("title")),
                    "top_team": to_text(top.get("nameShort")),
                    "top_seed": to_int(top.get("seed")),
                    "top_score": to_int(top.get("score")),
                    "top_winner": to_bool(top.get("isWinner")),
                    "bottom_team": to_text(bottom.get("nameShort")),
                    "bottom_seed": to_int(bottom.get("seed")),
                    "bottom_score": to_int(bottom.get("score")),
                    "bottom_winner": to_bool(bottom.get("isWinner")),
                    "winner": to_text(winner.get("nameShort")),
                    "broadcaster": to_text(_mapping(game.get("broadcaster")).get("name")),
                }
            )
    return rows
