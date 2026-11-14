"""The SQL-facing catalog shape and its discovery metadata."""

from __future__ import annotations

import json

import pytest

from vgi_ncaa_basketball.worker import NCAA_CATALOG

TABLES = {
    "current_rankings": ("all_current_rankings", [("poll_id", "team_id")]),
    "current_standings": ("all_current_standings", [("team_id",)]),
    "current_scoreboard": ("all_current_scoreboard", [("event_id",)]),
    "current_player_stats": ("all_current_player_stats", [("player_id",)]),
    "teams": ("all_teams", [("team_id",)]),
    "conferences": ("all_conferences", [("conference_id",)]),
    "net_rankings": ("all_net_rankings", [("school",)]),
    "stat_categories": ("all_stat_categories", [("category", "stat_id")]),
}
FUNCTIONS = {
    "rankings",
    "standings",
    "scoreboard",
    "team_schedule",
    "team_roster",
    "player_stats",
    "box_score",
    "team_box_score",
    "play_by_play",
    "stat_leaders",
    "bracket",
} | {backing for backing, _ in TABLES.values()}


def _keys(table, kind: str) -> list[tuple[str, ...]]:
    return [
        tuple(name for path in record.definition.column_paths for name in path)
        for record in table.constraints
        if record.definition.kind == kind
    ]


def test_catalog_has_two_league_schemas() -> None:
    assert NCAA_CATALOG.name == "ncaa_basketball"
    assert NCAA_CATALOG.default_schema == "mens"
    assert [schema.path for schema in NCAA_CATALOG.schemas] == [["mens"], ["womens"]]


@pytest.mark.parametrize("schema", NCAA_CATALOG.schemas, ids=lambda value: value.path[0])
def test_schema_surface(schema) -> None:
    assert {function.Meta.name for function in schema.functions} == FUNCTIONS
    assert {table.name for table in schema.tables} == set(TABLES)
    for table in schema.tables:
        backing, key = TABLES[table.name]
        assert table.function.Meta.name == backing
        assert _keys(table, "PRIMARY_KEY") == key
        assert [column for (column,) in _keys(table, "NOT_NULL")] == list(key[0])
        assert table.column_comments
        fields = {field.name: field for field in table.function.FIXED_SCHEMA}
        assert all(not fields[column].nullable for column in key[0])
        tags = table.tags
        assert {"vgi.title", "vgi.doc_llm", "vgi.doc_md", "vgi.keywords", "vgi.example_queries"} <= set(tags)
        assert len(tags["vgi.title"]) <= 32
        for example in json.loads(tags["vgi.example_queries"]):
            assert f"ncaa_basketball.{schema.path[0]}.{table.name}" in example["sql"]


@pytest.mark.parametrize("schema", NCAA_CATALOG.schemas, ids=lambda value: value.path[0])
def test_schema_examples_include_composed_queries(schema) -> None:
    from vgi_ncaa_basketball import api
    from vgi_ncaa_basketball.functions import COMPOSED_EXAMPLES, league_examples

    league = next(league for league in api.LEAGUES if league.key == schema.path[0])
    raw = schema.tags["vgi.example_queries"]
    examples = json.loads(raw)
    assert all(item in examples for item in league_examples(league, COMPOSED_EXAMPLES))
    assert len(raw) <= 8000  # vgi-lint VGI417 cap for vgi.example_queries


def test_required_and_optional_arguments() -> None:
    functions = {function.Meta.name: function for function in NCAA_CATALOG.schemas[0].functions}
    expectations = {
        "rankings": {"season": True, "week": True, "poll": False},
        "standings": {"season": False, "conference": False},
        "scoreboard": {"game_date": True, "conference": False},
        "team_schedule": {"team_id": True, "season": False},
        "box_score": {"event_id": True},
        "stat_leaders": {"stat_id": True, "category": False, "pages": False},
        "bracket": {"season": True},
    }
    for name, expected in expectations.items():
        metadata = functions[name].get_metadata()
        assert {parameter.name: parameter.required for parameter in metadata.parameters} == expected


def test_discovery_contract_is_public_and_machine_readable() -> None:
    tags = NCAA_CATALOG.tags
    for key in ("vgi.title", "vgi.subtitle", "vgi.doc_llm", "vgi.doc_md", "vgi.keywords", "vgi.copyright"):
        assert tags[key]
    assert "Not affiliated" in tags["vgi.copyright"]
    examples = json.loads(tags["vgi.executable_examples"])
    tasks = json.loads(tags["vgi.agent_test_tasks"])
    assert all({"name", "description", "sql", "expected_result"} <= example.keys() for example in examples)
    assert 6 <= len(tasks) <= 10
    assert all(set(task) == {"name", "prompt"} for task in tasks)
    assert len({task["name"] for task in tasks}) == len(tasks)


def test_agent_sidecar_matches_public_tasks() -> None:
    from pathlib import Path

    sidecar = (Path(__file__).resolve().parent.parent / "vgi-agent-tests.yaml").read_text()
    names = [line.split(":", 1)[1].strip() for line in sidecar.splitlines() if line.startswith("  - name:")]
    public = [task["name"] for task in json.loads(NCAA_CATALOG.tags["vgi.agent_test_tasks"])]
    assert names == public
    assert "reference_sql:" not in sidecar, "live data must be graded with check_sql"
    assert sidecar.count("check_sql:") == len(public)
