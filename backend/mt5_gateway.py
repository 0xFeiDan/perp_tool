"""Read-only MT5 data gateway for the primary trading control plane.

The primary service can use a local adapter during Wine-based development or a
dedicated local MT5 sidecar in production.  Remote transport is deliberately
limited to loopback TCP or a mounted Unix domain socket; no public MT5 URL is
accepted and no method in this module can submit a trade.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from mt5_readonly import MT5ReadOnlyError, MT5ReadOnlySidecar


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class MT5GatewaySettings:
    sidecar_url: str | None = None
    sidecar_token: str | None = None
    unix_socket: str | None = None
    timeout_seconds: float = 5.0

    @property
    def remote_enabled(self) -> bool:
        return self.sidecar_url is not None or self.unix_socket is not None

    @classmethod
    def from_env(cls) -> "MT5GatewaySettings":
        raw_url = os.getenv("MT5_SIDECAR_URL", "").strip().rstrip("/")
        raw_socket = os.getenv("MT5_SIDECAR_UNIX_SOCKET", "").strip()
        raw_token = os.getenv("MT5_SIDECAR_TOKEN", "").strip()
        timeout_raw = os.getenv("MT5_SIDECAR_TIMEOUT_SECONDS", "5").strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as error:
            raise MT5ReadOnlyError("MT5_SIDECAR_TIMEOUT_SECONDS must be numeric") from error
        if not 1 <= timeout_seconds <= 30:
            raise MT5ReadOnlyError("MT5_SIDECAR_TIMEOUT_SECONDS must be between 1 and 30")

        if raw_socket and not raw_url:
            # httpx needs an HTTP authority even though the TCP layer is
            # replaced by the Unix socket.  It is never resolved on a network.
            raw_url = "http://mt5-sidecar"
        if not raw_url:
            return cls(timeout_seconds=timeout_seconds)

        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MT5ReadOnlyError("MT5_SIDECAR_URL must be a clean http(s) endpoint")
        if raw_socket and parsed.scheme != "http":
            raise MT5ReadOnlyError("Unix-socket MT5 sidecar transport must use an http URL")
        if not raw_socket and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
            raise MT5ReadOnlyError("MT5_SIDECAR_URL must be loopback unless MT5_SIDECAR_UNIX_SOCKET is set")
        if len(raw_token) < 32:
            raise MT5ReadOnlyError("MT5_SIDECAR_TOKEN must contain at least 32 characters when a sidecar is configured")
        return cls(sidecar_url=raw_url, sidecar_token=raw_token, unix_socket=raw_socket or None, timeout_seconds=timeout_seconds)


class MT5ReadOnlyGateway:
    """Fixed read-only surface shared by local and sidecar configurations."""

    def __init__(self, settings: MT5GatewaySettings | None = None, *, local: MT5ReadOnlySidecar | None = None):
        self.settings = settings or MT5GatewaySettings.from_env()
        self.local = local or MT5ReadOnlySidecar()
        self._remote_available = False

    async def start(self) -> None:
        if self.settings.remote_enabled:
            # MT5 telemetry must never prevent the exchange execution plane
            # from starting.  Its visible status remains failed closed.
            await self.status()
            return
        await asyncio.to_thread(self.local.start)

    async def close(self) -> None:
        if not self.settings.remote_enabled:
            await asyncio.to_thread(self.local.close)

    async def status(self) -> dict[str, Any]:
        if not self.settings.remote_enabled:
            return {**self.local.status().as_dict(), "source": "local", "available": True}
        try:
            status = await self._remote_json("/v1/status")
            self._remote_available = True
            return {**status, "source": "sidecar", "available": True}
        except MT5ReadOnlyError:
            self._remote_available = False
            return {
                "enabled": True,
                "initialized": False,
                "readonly_verified": False,
                "terminal_trade_allowed": None,
                "account_trade_allowed": None,
                "source": "sidecar",
                "available": False,
            }

    async def account_info(self) -> dict[str, Any]:
        if self.settings.remote_enabled:
            result = await self._remote_json("/v1/account")
            return result.get("account")
        return await asyncio.to_thread(self.local.account_info)

    async def positions(self) -> list[dict[str, Any]]:
        if self.settings.remote_enabled:
            result = await self._remote_json("/v1/positions")
            positions = result.get("positions")
            if not isinstance(positions, list):
                raise MT5ReadOnlyError("MT5 sidecar returned an invalid positions response")
            return positions
        return await asyncio.to_thread(self.local.positions)

    async def quote(self, symbol: str) -> dict[str, Any] | None:
        if self.settings.remote_enabled:
            cleaned = symbol.strip()
            if not cleaned:
                raise MT5ReadOnlyError("symbol is required")
            result = await self._remote_json(f"/v1/quote/{quote(cleaned, safe='')}")
            return result.get("quote")
        return await asyncio.to_thread(self.local.quote, symbol)

    async def _remote_json(self, path: str) -> dict[str, Any]:
        if not self.settings.sidecar_url or not self.settings.sidecar_token:
            raise MT5ReadOnlyError("MT5 read-only sidecar is not configured")
        transport = httpx.AsyncHTTPTransport(uds=self.settings.unix_socket, retries=0) if self.settings.unix_socket else None
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.sidecar_url,
                headers={"X-MT5-Sidecar-Token": self.settings.sidecar_token},
                timeout=self.settings.timeout_seconds,
                transport=transport,
                trust_env=False,
            ) as client:
                response = await client.get(path)
        except httpx.HTTPError as error:
            raise MT5ReadOnlyError("MT5 read-only sidecar is unavailable") from error
        if response.status_code != 200:
            raise MT5ReadOnlyError("MT5 read-only sidecar rejected the data request")
        try:
            body = response.json()
        except ValueError as error:
            raise MT5ReadOnlyError("MT5 read-only sidecar returned invalid JSON") from error
        if not isinstance(body, dict):
            raise MT5ReadOnlyError("MT5 read-only sidecar returned an invalid response")
        return body
