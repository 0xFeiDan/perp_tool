from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from security import ControlPlane, SecuritySettings


def make_request(*, headers: dict[str, str] | None = None) -> Request:
    encoded = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/execute",
            "query_string": b"",
            "headers": encoded,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def control() -> ControlPlane:
    settings = SecuritySettings(
        control_token="x" * 40,
        session_minutes=30,
        public_https=False,
        allowed_hosts=("testserver",),
        allowed_origins=("http://testserver",),
        trust_proxy_headers=False,
        bind_session_to_ip=False,
    )
    return ControlPlane(settings, Path("audit-test.jsonl"))


def test_direct_control_token_header_cannot_bypass_session_or_csrf() -> None:
    plane = control()
    request = make_request(headers={"X-Strategy-Token": "x" * 40})
    with pytest.raises(HTTPException) as error:
        plane.require_http(request, write=True, execute=True)
    assert error.value.status_code == 401


def test_logout_revoke_invalidates_server_session() -> None:
    plane = control()
    session, csrf, _ = plane.create_session("x" * 40, "127.0.0.1")
    request = make_request(headers={"Cookie": f"strategy_session={session}", "X-CSRF-Token": csrf})

    plane.require_http(request, write=True)
    assert plane.revoke_session(request)
    with pytest.raises(HTTPException) as error:
        plane.require_http(request, write=True)
    assert error.value.status_code == 401


def test_control_token_guesses_are_rate_limited_before_session_creation() -> None:
    plane = control()
    for _ in range(plane.settings.login_attempts_per_10_minutes):
        with pytest.raises(HTTPException) as rejected:
            plane.create_session("wrong-token", "127.0.0.1")
        assert rejected.value.status_code == 401
    with pytest.raises(HTTPException) as limited:
        plane.create_session("x" * 40, "127.0.0.1")
    assert limited.value.status_code == 429
