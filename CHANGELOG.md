# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.2.0]

- Port to VGI protocol 3.3 (vgi-python 0.51). Requires the current VGI DuckDB
  extension; 3.2 extensions are incompatible. No SQL surface changes.

## [0.1.0]

- Initial release on VGI protocol 3.2 (vgi-python 0.50) with vgi-lint-check 2.2.0.
- `mens` and `womens` schemas for NCAA Division I basketball with identical
  surfaces: current polls, standings, scoreboard, player statistics, teams,
  conferences and NET rankings as tables, plus historical polls, standings by
  season and conference, scoreboards by date, team schedules and rosters,
  season player statistics, box scores, play-by-play, official NCAA stat
  leaders and tournament brackets as table functions.
- ESPN's public site, core and common endpoints as the primary source; the
  ncaa-api proxy of ncaa.com (configurable base URL, optional access key) for
  NET rankings, official stat leaders and brackets.
- One HTTP chokepoint with timeouts, bounded retries for 429/5xx that honor
  `Retry-After`, a total query deadline, a concurrency bound, ncaa-api request
  spacing, and an in-process TTL cache.
- Typed Arrow schemas with column comments, discovery metadata, agent test
  tasks graded by `check_sql`, and hermetic Haybarn end-to-end coverage.
