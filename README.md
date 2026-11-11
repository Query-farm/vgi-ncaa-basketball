<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-kalshi/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-ncaa-basketball</h1>

<p align="center">
  NCAA Division I men's and women's college basketball as ordinary DuckDB tables.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-ncaa-basketball/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-ncaa-basketball/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-3776AB.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-read--only-6f42c1.svg" alt="Read-only VGI worker"></a>
</p>

---

`vgi-ncaa-basketball` fetches data on demand from ESPN's public college
basketball endpoints and, for official NCAA rankings, leaderboards and brackets,
from the [ncaa-api](https://github.com/henrygd/ncaa-api) proxy of ncaa.com. It
reformats the responses as typed Arrow data for Haybarn SQL. It requires no
credentials and does not rehost or persist any dataset.

## Quick start

The worker is not published to PyPI. `uvx` runs the tagged release straight from
GitHub and caches it after the first use:

```bash
uvx --from git+https://github.com/Query-farm/vgi-ncaa-basketball@v0.1.0 vgi-ncaa-basketball --help
```

Launch Haybarn with the catalog pre-attached:

```bash
uvx haybarn-cli \
  -cmd "INSTALL vgi FROM community; LOAD vgi; ATTACH 'ncaa_basketball' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-ncaa-basketball@v0.1.0 vgi-ncaa-basketball');" \
  :memory:
```

Or attach it in an existing Haybarn session:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'ncaa_basketball' (
  TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-ncaa-basketball@v0.1.0 vgi-ncaa-basketball'
);
```

For source development, run `uv lock --check --no-sources`, then
`uv sync --no-sources` and `uv run --no-sources vgi-ncaa-basketball`.
The lockfile resolves all dependencies from package registries and does not
require a sibling checkout.

## Catalog surface

The catalog has two schemas with identical shapes: `mens` for men's and
`womens` for women's Division I basketball. Each exposes eight tables and
eleven parameterized table functions:

| Kind | Objects |
| --- | --- |
| Current tables | `current_rankings`, `current_standings`, `current_scoreboard`, `current_player_stats`, `net_rankings` |
| Reference tables | `teams`, `conferences`, `stat_categories` |
| Season and date | `rankings(season, week)`, `standings()`, `scoreboard(game_date)`, `player_stats()` |
| Team | `team_schedule(team_id)`, `team_roster(team_id)` |
| Game | `box_score(event_id)`, `team_box_score(event_id)`, `play_by_play(event_id)` |
| Official NCAA | `stat_leaders(stat_id)`, `bracket(season)` |

Each table is backed by a parameterless `all_*` scan (for example
`all_current_standings`); query the table rather than the scan.

**Seasons are labelled by the year they end**, as ESPN and the NCAA do: the
2025-26 season is `2026`, and the season that tips off in November 2026 is
`2027`. ESPN `team_id` and `event_id` join the tables and functions.

## Usage

### Polls and NET rankings

```sql
-- The current AP Top 25
SELECT rank, team_location, record, points, first_place_votes
FROM ncaa_basketball.mens.current_rankings
WHERE poll_id = 1 AND rank IS NOT NULL
ORDER BY rank;

-- The AP top ten in week 5 of the 2025-26 women's season
SELECT rank, team_location, points, record
FROM ncaa_basketball.womens.rankings(2026, 5)
WHERE rank <= 10
ORDER BY rank;

-- Official NET, with Quadrant 1 records
SELECT rank, school, conference, record, quad1_record
FROM ncaa_basketball.mens.net_rankings
WHERE rank <= 25
ORDER BY rank;
```

`current_rankings` carries the AP Top 25 (`poll_id = 1`) and, where ESPN
publishes it, the Coaches Poll (`poll_id = 2`). Teams that received votes but
are outside the top 25 have `receiving_votes = true` and a `NULL` rank.
`rankings()` takes `poll := 'Coaches'` for the coaches' poll; week 1 is the
preseason poll. Early in a season
the NCAA may still publish the previous season's final NET; the `updated`
column says which release you have.

### Standings and conferences

```sql
-- Final 2025-26 Big Ten standings
SELECT conference_seed, team_name, conference_wins, conference_losses, wins, losses
FROM ncaa_basketball.mens.standings(season := 2026, conference := 'Big Ten')
ORDER BY conference_seed;

-- Every conference and its size
SELECT conference_id, short_name, abbreviation, team_count
FROM ncaa_basketball.womens.conferences
ORDER BY team_count DESC;
```

`conference` accepts a conference id, abbreviation (`big10`) or name
(`Big Ten`, `Big Ten Conference`) from the `conferences` table. Percentages
are exact decimals. Early-season standings include wins over non-Division I
opponents, so league-wide wins and losses need not balance until conference
play.

### Scores and schedules

```sql
-- Every Division I game on one date, not only ESPN's featured games
SELECT away_team_name, away_score, home_team_name, home_score, status
FROM ncaa_basketball.mens.scoreboard(DATE '2026-11-09')
ORDER BY start_time;

-- Today's games involving ranked teams
SELECT short_name, status_detail, away_rank, away_score, home_rank, home_score
FROM ncaa_basketball.womens.current_scoreboard
WHERE away_rank IS NOT NULL OR home_rank IS NOT NULL;

-- Duke's 2025-26 results, regular season and postseason
SELECT start_time, short_name, away_score, home_score, event_note
FROM ncaa_basketball.mens.team_schedule(150, season := 2026)
ORDER BY start_time;
```

Find a `team_id` in `teams` (`WHERE display_name ILIKE '%duke%'`).
ESPN's team schedule feed has no conference flag, so `team_schedule()` derives
`conference_game` from that season's standings: conference-tournament games
count, as on ESPN's scoreboard, and NCAA postseason games are `NULL`.
`team_roster(team_id)` returns the current roster; ESPN does not reliably serve
historical rosters, so it takes no season.

### Player statistics

```sql
-- 2025-26 scoring leaders with at least 20 games
SELECT player_name, team_abbreviation, games_played, points_per_game, field_goal_pct
FROM ncaa_basketball.mens.player_stats(season := 2026)
WHERE games_played >= 20
ORDER BY points_per_game DESC
LIMIT 10;

-- Look up an official NCAA leaderboard id, then read it
SELECT category, stat_id, name
FROM ncaa_basketball.womens.stat_categories
WHERE name ILIKE '%per game%';

SELECT rank, player_name, team, games, stat_value
FROM ncaa_basketball.womens.stat_leaders(102)   -- women's points per game
ORDER BY rank;
```

`player_stats()` and `current_player_stats` cover every Division I player
(about 7,600 men and 6,200 women) and fetch every page of ESPN's per-athlete
statistics. Shooting percentages use a 0-100 scale. `stat_leaders(stat_id)`
returns an official NCAA leaderboard. Ids differ between the men's and women's
games (points per game is 136 for men and 102 for women), so look them up in
`stat_categories`; `category := 'team'` selects team leaderboards. `pages`
fetches 50 rows per page (default 1).

### Box scores and play-by-play

```sql
SELECT team_abbreviation, player_name, minutes, points, rebounds, assists
FROM ncaa_basketball.mens.box_score(401920686)
ORDER BY points DESC NULLS LAST;

SELECT team_name, points, field_goal_pct, three_point_pct, turnovers, points_in_paint
FROM ncaa_basketball.mens.team_box_score(401920686);

SELECT period, clock, text, home_score, away_score
FROM ncaa_basketball.mens.play_by_play(401920686)
WHERE scoring_play
ORDER BY sequence_number;
```

Event ids come from `scoreboard()`, `current_scoreboard` or `team_schedule()`.
Men's games have two halves and women's games four quarters; any later period is
overtime.

### March Madness

```sql
SELECT round_title, region, top_seed, top_team, top_score,
       bottom_seed, bottom_team, bottom_score, winner
FROM ncaa_basketball.womens.bracket(2026)
WHERE round_number >= 5
ORDER BY bracket_position_id;
```

`round_number` runs from 1 (First Four) to 7 (the championship game).
Michigan won the 2026 men's title 69-63 over UConn; UCLA won the women's title
79-51 over South Carolina.

## Data sources, terms and affiliation

**This project is not affiliated with, endorsed by, or sponsored by the NCAA,
ESPN, The Walt Disney Company, ncaa.com, Warner Bros. Discovery, any
conference or any member school.** NCAA, March Madness, Final Four and related
marks belong to their owners.

- **ESPN** (`site.api.espn.com`, `site.web.api.espn.com`,
  `sports.core.api.espn.com`) is the primary source for polls, standings,
  scores, schedules, rosters, player statistics, box scores and play-by-play.
  These endpoints are **undocumented and unofficial**; ESPN can change or
  withdraw them at any time, and their use is subject to ESPN's and Disney's
  terms of use, which restrict automated access.
- **ncaa-api** (`ncaa-api.henrygd.me`) is a community MIT-licensed proxy of
  ncaa.com used for the official NET rankings, statistical leaderboards and
  tournament brackets. The public host is a hobby service limited to three
  requests per second per IP and is not guaranteed; the data remains
  ncaa.com's and is subject to its terms, which generally restrict commercial
  reuse. For anything beyond light use, self-host it
  (`docker run -p 3000:3000 henrygd/ncaa-api`) and point
  `NCAAB_NCAA_API_URL` at your instance; `NCAAB_NCAA_API_KEY` supplies its
  optional `x-ncaa-key` access header.

The worker fetches only what a query needs, when it runs. It identifies itself
honestly (`User-Agent: vgi-ncaa-basketball/<version>
(+https://github.com/Query-farm/vgi-ncaa-basketball)`), caches responses
briefly in process to avoid repeated requests, and never stores, republishes
or redistributes upstream data. **You are responsible for making sure your use
complies with the upstream providers' terms**, including any restrictions on
commercial use, redistribution, and betting or fantasy products. The MIT
licence below covers this worker's code only, not the data it retrieves.

## Production HTTP deployment

Stdio is the recommended transport when Haybarn can launch the worker locally.
Use HTTP when the worker must be shared across hosts. The production HTTP entry
point fails closed unless it has a stable signing key and either authentication
or an explicit anonymous-access decision:

```bash
export VGI_SIGNING_KEY='a-long-random-secret-shared-by-every-replica'
export VGI_BEARER_TOKENS='replace-with-a-random-token=haybarn'

uvx --from git+https://github.com/Query-farm/vgi-ncaa-basketball@v0.1.0 vgi-ncaa-basketball-http \
  --host 0.0.0.0 \
  --port 8000 \
  --cors-origins 'https://sql.example.com' \
  --log-format json
```

`vgi-ncaa-basketball-http` disables the description pages and cross-origin
access unless they are explicitly configured. Terminate TLS at a trusted proxy
or load balancer. All replicas must receive the same `VGI_SIGNING_KEY`; rotate
it as a coordinated deployment because outstanding state tokens are signed with
it.

Supported authentication options are:

- `VGI_BEARER_TOKENS`: comma-separated `token=principal` pairs.
- `VGI_JWT_ISSUER` and `VGI_JWT_AUDIENCE`: JWT/JWKS authentication.
- `VGI_PROXY_PROOF_MODE=require` with the corresponding VGI proxy-proof
  settings when a trusted proxy authenticates users.

Because this connector exposes read-only public data, an intentionally public
service can set `VGI_ALLOW_ANONYMOUS=1`. It should still be protected by gateway
rate limits, and a shared deployment should use a self-hosted ncaa-api. The
health probe is `GET /health`.

### Container

```bash
docker build -t vgi-ncaa-basketball .
docker run --rm -p 8000:8000 \
  -e VGI_SIGNING_KEY='replace-with-a-stable-random-secret' \
  -e VGI_BEARER_TOKENS='replace-with-a-random-token=haybarn' \
  vgi-ncaa-basketball
```

The image runs as an unprivileged user, emits JSON logs, disables interactive
description pages, and includes a health check.

## Reliability and operations

Every upstream call goes through one client with a 30-second request timeout, a
120-second total query deadline, at most four attempts, and at most four
simultaneous requests per process. HTTP 429 and transient 5xx responses and
transport errors are retried with jittered exponential backoff; `Retry-After` is
honored with a 30-second per-retry cap. Redirects are followed. Requests to
ncaa-api are spaced at least 0.35 seconds apart to stay under its published
limit.

Responses are cached in process for the larger of the upstream `max-age` and a
per-endpoint floor: 30 seconds for scoreboards and game summaries, 5 minutes for
standings, polls and player statistics, and an hour for team lists, rosters and
historical polls. The same lifetime is forwarded to VGI clients as the result's
cache policy. Retries and transport failures are logged as warnings; per-request
timings are available at debug level.

Each upstream host can be overridden for self-hosting or hermetic tests:
`NCAAB_ESPN_SITE_URL`, `NCAAB_ESPN_WEB_URL`, `NCAAB_ESPN_CORE_URL` and
`NCAAB_NCAA_API_URL`. Do not set them from untrusted user input.

## Development and release checks

```bash
uv lock --check --no-sources
uv sync --no-sources
uv run --no-sources ruff check .
uv run --no-sources ruff format --check .
uv run --no-sources pytest --cov=vgi_ncaa_basketball --cov-fail-under=80
uv build
uvx twine check dist/*
uvx --no-sources --from vgi-lint-check==2.2.0 vgi-lint lint \
  'uv run --no-sources ncaa_basketball_worker.py' \
  --no-execute --no-check-links --audit-waivers --fail-on warning
```

The offline suite parses recorded ESPN and ncaa-api responses in
`tests/fixtures` and runs a hermetic Haybarn end-to-end test against a local
fixture server. Live upstream smoke tests are opt-in with
`uv run pytest -m live` and also run in a serialized scheduled workflow
alongside the executing VGI lint of every catalog example. Publishing is driven by releases. Create a GitHub release named `vX.Y.Z` that
matches the version in `pyproject.toml`. CI then builds, validates and attests
the wheel and sdist, attaches them to the release, and publishes a
multi-platform container image to `ghcr.io/query-farm/vgi-ncaa-basketball`. The package is
not published to PyPI; install it from the release tag as shown in the quick
start.

## License

Copyright 2026 Query Farm LLC - https://query.farm

The worker's source code is released under the [MIT License](LICENSE). Data
retrieved through it remains the property of its providers and is subject to
their terms.
