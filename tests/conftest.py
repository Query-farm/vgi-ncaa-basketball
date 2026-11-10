"""Shared fixtures: recorded upstream documents and a hermetic local upstream."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_server import FIXTURES, serve  # noqa: E402

from vgi_ncaa_basketball import api  # noqa: E402

ENV_VARS = ("NCAAB_ESPN_SITE_URL", "NCAAB_ESPN_WEB_URL", "NCAAB_ESPN_CORE_URL", "NCAAB_NCAA_API_URL")


def load(name: str) -> Any:
    """Decode one recorded fixture document."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    api.clear_cache()
    yield
    api.clear_cache()


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point every upstream source at the local fixture server."""
    with serve() as url:
        for name in ENV_VARS:
            monkeypatch.setenv(name, url)
        monkeypatch.setattr(api, "NCAA_MIN_INTERVAL", 0.0)
        yield url
