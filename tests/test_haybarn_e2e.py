"""Hermetic end-to-end coverage through Haybarn and the VGI transport.

A local HTTP server answers every ESPN and ncaa-api path from tests/fixtures,
so the attached worker exercises its real HTTP client, parsing, and Arrow
output without touching the network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fixture_server import serve

TABLES = (
    "current_rankings",
    "current_standings",
    "current_scoreboard",
    "current_player_stats",
    "teams",
    "conferences",
    "net_rankings",
    "stat_categories",
)
# Rows each table yields from the recorded fixtures, per schema.
EXPECTED = {
    "mens": (14, 8, 3, 3, 12, 2, 5, 11),
    "womens": (7, 8, 3, 3, 12, 2, 5, 11),
}


def test_haybarn_attaches_and_queries_every_table_in_both_schemas() -> None:
    project = Path(__file__).resolve().parent.parent
    location = f"uv run --project {project} vgi-ncaa-basketball"
    counts = ", ".join(
        f"(SELECT count(*) FROM ncaa_basketball.{schema}.{table}) AS {schema}_{table}"
        for schema in EXPECTED
        for table in TABLES
    )
    sql = (
        f"INSTALL vgi FROM community; LOAD vgi; "
        f"ATTACH 'ncaa_basketball' (TYPE vgi, LOCATION '{location}'); "
        f"SELECT {counts}; "
        # Same-named objects in the two schemas must dispatch (and cache) separately.
        "SELECT (SELECT string_agg(DISTINCT conference_abbreviation, '|' ORDER BY conference_abbreviation) "
        "FROM ncaa_basketball.mens.current_standings) AS mens_conferences, "
        "(SELECT string_agg(DISTINCT conference_abbreviation, '|' ORDER BY conference_abbreviation) "
        "FROM ncaa_basketball.womens.current_standings) AS womens_conferences; "
        "SELECT sum(points) AS uconn_points FROM ncaa_basketball.mens.box_score(401920686) "
        "WHERE team_id = 41; "
        "SELECT winner FROM ncaa_basketball.womens.bracket(2026) WHERE round_number = 7;"
    )
    with serve() as url:
        env = {
            **os.environ,
            "NCAAB_ESPN_SITE_URL": url,
            "NCAAB_ESPN_WEB_URL": url,
            "NCAAB_ESPN_CORE_URL": url,
            "NCAAB_NCAA_API_URL": url,
        }
        result = subprocess.run(
            ["uvx", "haybarn-cli", ":memory:", "-csv", "-c", sql],
            cwd=project,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

    lines = result.stdout.splitlines()
    header = ",".join(f"{schema}_{table}" for schema in EXPECTED for table in TABLES)
    assert header in lines, result.stdout + result.stderr
    values = lines[lines.index(header) + 1].split(",")
    assert [int(value) for value in values] == [n for counts in EXPECTED.values() for n in counts]
    assert "acc|big10,big10|sec" in lines
    # The trimmed fixture keeps four UConn players: 11 + 12 + 10 + 10 points.
    assert lines[lines.index("uconn_points") + 1] == "43"
    assert lines[lines.index("winner") + 1] == "UCLA"
