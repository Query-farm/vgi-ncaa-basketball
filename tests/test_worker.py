"""Tests for production entry-point policy."""

from __future__ import annotations

import sys

import pytest

from vgi_ncaa_basketball import worker


def test_http_requires_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VGI_SIGNING_KEY", raising=False)

    with pytest.raises(SystemExit, match="VGI_SIGNING_KEY is required"):
        worker.main_http()


def test_http_requires_authentication_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VGI_SIGNING_KEY", "test-signing-key")
    for name in ("VGI_BEARER_TOKENS", "VGI_JWT_ISSUER", "VGI_PROXY_PROOF_MODE", "VGI_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit, match="HTTP authentication is required"):
        worker.main_http()


def test_http_applies_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VGI_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("VGI_BEARER_TOKENS", "token=ci")
    monkeypatch.setattr(sys, "argv", ["vgi-ncaa-basketball-http", "--port", "8000"])
    called = False

    def fake_main() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(worker.NcaaBasketballWorker, "main", fake_main)
    worker.main_http()

    assert called
    assert sys.argv == [
        "vgi-ncaa-basketball-http",
        "--cors-origins",
        "",
        "--no-describe",
        "--http",
        "--port",
        "8000",
    ]
