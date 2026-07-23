from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from mt5_gateway import MT5GatewaySettings
from mt5_readonly import MT5ReadOnlyError


def test_remote_sidecar_rejects_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_SIDECAR_URL", "https://mt5.example.com")
    monkeypatch.setenv("MT5_SIDECAR_TOKEN", "x" * 40)
    monkeypatch.delenv("MT5_SIDECAR_UNIX_SOCKET", raising=False)

    with pytest.raises(MT5ReadOnlyError, match="loopback"):
        MT5GatewaySettings.from_env()


def test_unix_socket_sidecar_requires_separate_strong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_SIDECAR_UNIX_SOCKET", "/run/mt5-sidecar/mt5.sock")
    monkeypatch.delenv("MT5_SIDECAR_URL", raising=False)
    monkeypatch.setenv("MT5_SIDECAR_TOKEN", "x" * 40)

    settings = MT5GatewaySettings.from_env()

    assert settings.remote_enabled
    assert settings.sidecar_url == "http://mt5-sidecar"
    assert settings.unix_socket == "/run/mt5-sidecar/mt5.sock"


def test_remote_sidecar_never_uses_implicit_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # This is a construction-only invariant. The gateway creates AsyncClient
    # with trust_env=False, so HTTP_PROXY cannot redirect the local sidecar hop.
    monkeypatch.setenv("MT5_SIDECAR_URL", "http://127.0.0.1:8900")
    monkeypatch.setenv("MT5_SIDECAR_TOKEN", "x" * 40)
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.example:8080")
    settings = MT5GatewaySettings.from_env()
    assert settings.sidecar_url == "http://127.0.0.1:8900"
