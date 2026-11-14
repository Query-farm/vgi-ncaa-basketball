# Player score coverage through VGI

[01 — Reconcile player score coverage](01_reconcile_player_score_coverage.ipynb) distinguishes a deliberate minutes filter from incomplete box-score coverage. The default is an existing trimmed replay fixture; an explicit live mode queries the same ESPN event without fallback.

## Run independently

Requires uv, Git and Python 3.13/3.14. The worker runs in uv's Python 3.13 environment. Initial network access downloads the pinned remote package, published dependencies and VGI extension; replay data is then local. No API credentials are used.

```sh
cd notebooks
uv sync --frozen
uv run --frozen python execute.py
uv run --frozen python execute.py --parameters '{"MIN_MINUTES":20}' --output-dir artifacts/experiment
uv run --frozen python execute.py --parameters '{"DATA_MODE":"live"}' --output-dir artifacts/live
```

Select `.venv/bin/python` in Jupyter, or register it:

```sh
uv run --frozen python -m ipykernel install --user --name vgi-ncaa-basketball-notebooks --display-name "Python (vgi-ncaa-basketball notebooks)"
```

The runner creates a temporary kernel specification, uses notebook-directory cwd, and atomically replaces a notebook only after success. Overrides require a separate destination; unknown parameters fail. Experiment copies include branding, recordings, helpers, environment and README. A failed live run cannot overwrite the successful replay default. Artifacts and environments are ignored.

## Pins and provenance

Visible ATTACH uses public Git revision `a3be7c2dd5860c5540e326bc365d1fe816840196`, `vgi-python==0.51.0`, `vgi-rpc==0.53.1`, `pyarrow==25.0.1` and `httpx==0.28.1` with the unchanged `vgi-ncaa-basketball` entry point. The notebook's independent lock pins Haybarn **2.0.0rc11**. There is no local source or root-environment discovery and no engine build. Upgrades require retesting both data modes and the parameter experiment.

`recordings/summary.json` is byte-identical to `tests/fixtures/mens_summary_401920686.json` at that source revision. The repository describes it as a recorded ESPN response, trimmed to four players per team. Its original capture timestamp is unavailable; this notebook does not claim a new capture or full historical roster. The event is identified in its header as Wagner at UConn, 2026-11-10 00:00 UTC, ESPN ID 401920686. Fixture SHA-256: `2d1f348a38e140fe9c3080f53ef06f43563147c4a6b065cbab3e1ea23db1c179`.

Replay serves only the exact summary endpoint from a loopback HTTP server and returns 404 for every unrecorded request. All four existing upstream base-URL hooks point to that server. Live mode clears all four overrides and starts no replay server, so the unchanged worker reaches ESPN's public endpoint. Caller environment settings are restored at cleanup. Live responses may fail or change; no response is silently substituted. Materialized SQL calls avoid capture-driven repeated retrieval. Worker processes may separately cache the same summary; replay audits every request, without pretending the calls form one atomic snapshot.

## Interpretation

The join uses event and team IDs, and the player key includes player ID. Known points reconstruct as `2*FGM+3PM+FTM`. Missing stats remain unknown. Selected points plus other returned-player points plus the unassigned residual reconcile to team points. That residual is not an invented player's score. A minutes cutoff does not repair omitted rows or establish efficiency/causal impact. Default and live analyses describe one historical game, not a season or recommendation.

Validation outcomes are recorded after default, standalone experiment, live endpoint, chart review and source checks.

Validated on macOS arm64: replay default, standalone 20-minute experiment, a 60-minute empty-selection boundary and live ESPN mode all pass ten SQL invariants. Replay audits only the recorded endpoint. Default selects 8 players/66 points; cutoff 20 selects 6/53; the 58-point unassigned residual is unchanged. Live returns 26 players accounting for all 124 team points, with zero residual. Six default/experiment/live charts were visually reviewed; three runner guards preserve the saved default. Source validation: 95 offline tests, 98.07% coverage, Ruff lint/format, standalone entry points, wheel/sdist build, Twine checks, isolated wheel launch and dependency audit passed. Structural metadata passes the configured warning gate at 92/100 L1 with 44 informational findings and no errors/warnings; live metadata examples were not run. Docker build was attempted but Docker Desktop could not start, so no container validation is claimed. Worker source, original recorded fixtures and root lockfile are unchanged.
