"""Local control-plane security primitives.

Secrets never leave the backend.  The browser exchanges the long-lived control
token for a short-lived, HttpOnly session cookie and a CSRF value.  This module
intentionally keeps no credentials in logs or API responses.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import ipaddress
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, WebSocket


def _truthy(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SecuritySettings:
    control_token: str
    session_minutes: int
    public_https: bool
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    trust_proxy_headers: bool
    bind_session_to_ip: bool
    login_attempts_per_10_minutes: int = 5

    @property
    def configured(self) -> bool:
        return len(self.control_token) >= 32

    @classmethod
    def from_env(cls) -> "SecuritySettings":
        split = lambda value: tuple(x.strip() for x in value.split(",") if x.strip())
        return cls(
            control_token=os.getenv("CONTROL_PLANE_TOKEN", "").strip(),
            session_minutes=max(5, min(int(os.getenv("SESSION_MINUTES", "30")), 240)),
            public_https=_truthy("PUBLIC_HTTPS"),
            allowed_hosts=split(os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost")),
            allowed_origins=split(os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8790,http://localhost:8790")),
            trust_proxy_headers=_truthy("TRUST_PROXY_HEADERS"),
            bind_session_to_ip=_truthy("BIND_SESSION_TO_IP"),
            login_attempts_per_10_minutes=max(3, min(int(os.getenv("LOGIN_ATTEMPTS_PER_10_MINUTES", "5")), 30)),
        )


class ControlPlane:
    """Short-lived UI sessions, CSRF checks, rate limits and redacted audit log."""

    def __init__(self, settings: SecuritySettings, audit_path: Path):
        self.settings = settings
        self.audit_path = audit_path
        self.sessions: dict[str, dict[str, Any]] = {}
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.login_hits: dict[str, deque[float]] = defaultdict(deque)
        self.idempotency: dict[str, float] = {}

    def client_key(self, request: Request | WebSocket) -> str:
        """Return a stable client identity without trusting browser headers.

        ``X-Real-IP`` is accepted only when the deployment has explicitly
        declared a private reverse proxy in front of this service.  Caddy
        overwrites that header at the proxy boundary; local development keeps
        this setting false and uses the direct peer address.
        """
        if self.settings.trust_proxy_headers:
            forwarded = request.headers.get("X-Real-IP", "").strip()
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                # A trusted deployment with a missing/malformed proxy header
                # must fail into one shared bucket, never trust user input.
                return "unverified-proxy-client"
        return request.client.host if request.client else "unknown"

    def _purge(self) -> None:
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > now}
        self.idempotency = {key: expiry for key, expiry in self.idempotency.items() if expiry > now}
        monotonic_now = time.monotonic()
        for key, bucket in list(self.login_hits.items()):
            while bucket and monotonic_now - bucket[0] > 10 * 60:
                bucket.popleft()
            if not bucket:
                self.login_hits.pop(key, None)

    def create_session(self, submitted_token: str, client_ip: str) -> tuple[str, str, int]:
        if not self.settings.configured:
            raise HTTPException(503, "CONTROL_PLANE_TOKEN is not configured; trading control is locked")
        self._purge()
        self.login_rate_limit(client_ip)
        if not hmac.compare_digest(submitted_token, self.settings.control_token):
            raise HTTPException(401, "invalid control token")
        session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        expires = int(time.time() + self.settings.session_minutes * 60)
        self.sessions[session] = {"csrf": csrf, "expires": expires, "client_ip": client_ip}
        return session, csrf, expires

    def _session(self, session_id: str | None, client_ip: str) -> dict[str, Any] | None:
        self._purge()
        session = self.sessions.get(session_id or "")
        if not session:
            return None
        if self.settings.bind_session_to_ip and not hmac.compare_digest(session["client_ip"], client_ip):
            return None
        return session

    def require_http(self, request: Request, *, write: bool = False, execute: bool = False) -> None:
        if not self.settings.configured:
            raise HTTPException(503, "CONTROL_PLANE_TOKEN is not configured; trading control is locked")
        client_ip = self.client_key(request)
        session = self._session(request.cookies.get("strategy_session"), client_ip)
        if not session:
            raise HTTPException(401, "control-plane authentication required")
        if write:
            submitted_csrf = request.headers.get("X-CSRF-Token", "")
            if not submitted_csrf or not hmac.compare_digest(submitted_csrf, session["csrf"]):
                raise HTTPException(403, "CSRF verification failed")
        # Rate limit by authenticated session rather than reverse-proxy IP;
        # multiple tailnet users otherwise collapse into the Caddy container's
        # address and can starve each other.
        session_id = request.cookies.get("strategy_session", "")
        session_key = hashlib.sha256(session_id.encode()).hexdigest()[:16]
        self.rate_limit(f"{session_key}:{'execute' if execute else 'write' if write else 'read'}", 5 if execute else 60)

    async def require_websocket(self, socket: WebSocket) -> bool:
        if not self.settings.configured:
            await socket.close(code=4403)
            return False
        origin = socket.headers.get("origin")
        if not origin or origin not in self.settings.allowed_origins:
            await socket.close(code=4403)
            return False
        session = self._session(socket.cookies.get("strategy_session"), self.client_key(socket))
        if not session:
            await socket.close(code=4401)
            return False
        return True

    def rate_limit(self, key: str, maximum_per_minute: int) -> None:
        now = time.monotonic()
        bucket = self.hits[key]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= maximum_per_minute:
            raise HTTPException(429, "too many control-plane requests; wait before retrying")
        bucket.append(now)

    def login_rate_limit(self, client_ip: str) -> None:
        """Throttle control-token guesses independently of an authenticated session."""
        now = time.monotonic()
        bucket = self.login_hits[hashlib.sha256(client_ip.encode()).hexdigest()[:16]]
        while bucket and now - bucket[0] > 10 * 60:
            bucket.popleft()
        if len(bucket) >= self.settings.login_attempts_per_10_minutes:
            retry_after = max(1, math.ceil((10 * 60) - (now - bucket[0])))
            raise HTTPException(
                429,
                "LOGIN_RATE_LIMITED",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)

    def claim_idempotency(self, value: str) -> None:
        if len(value) < 16 or len(value) > 128:
            raise HTTPException(400, "invalid request id")
        self._purge()
        if value in self.idempotency:
            raise HTTPException(409, "duplicate execution request blocked")
        self.idempotency[value] = time.time() + 15 * 60

    def audit(self, action: str, **fields: Any) -> None:
        """Append only redacted audit event; do not include keys, signatures or cookies."""
        safe = {key: value for key, value in fields.items() if key not in {"token", "secret", "signature", "cookie", "private_key"}}
        event = {"at": int(time.time() * 1000), "action": action, **safe}
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def revoke_session(self, request: Request) -> bool:
        """Invalidate a server-side session in addition to clearing its cookie."""
        self._purge()
        session_id = request.cookies.get("strategy_session")
        if not session_id or not self._session(session_id, self.client_key(request)):
            return False
        self.sessions.pop(session_id, None)
        return True

    def session_status(self, request: Request) -> dict[str, Any]:
        session = self._session(request.cookies.get("strategy_session"), self.client_key(request))
        return {
            "configured": self.settings.configured,
            "authenticated": bool(session),
            "expires": session["expires"] if session else None,
            # A CSRF token is not an authentication secret; it is returned only
            # to the authenticated same-origin session to survive a page reload.
            "csrf": session["csrf"] if session else None,
        }

    def authenticated_owner(self, request: Request) -> str:
        """Stable, non-secret identity used to bind one-time order intents.

        Call this only after ``require_http``.  The raw session identifier or
        control token is never exposed in a preview, audit event, or database.
        """
        session_id = request.cookies.get("strategy_session")
        if not session_id or not self._session(session_id, self.client_key(request)):
            raise HTTPException(401, "control-plane authentication required")
        material = f"session:{session_id}"
        return hashlib.sha256(material.encode()).hexdigest()


def request_fingerprint(payload: dict[str, Any]) -> str:
    """Useful for audits without persisting a user-supplied id verbatim."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
