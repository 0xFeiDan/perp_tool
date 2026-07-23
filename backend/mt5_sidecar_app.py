"""Local-only HTTP boundary for the Ubuntu/Wine MT5 read-only adapter.

Run this process next to the Wine-hosted MetaTrader 5 terminal, bound to
``127.0.0.1`` only.  The primary control plane must reach it through an
authenticated Unix-socket proxy; this app intentionally contains no trade,
order, modify, delete, or generic MT5 RPC endpoint.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from mt5_readonly import MT5ReadOnlyError, MT5ReadOnlySidecar


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(Path(__file__).with_name(".env"))


@dataclass(frozen=True)
class MT5SidecarAuthSettings:
    """Separate shared secret for the local control-plane-to-sidecar hop."""

    token: str

    @property
    def configured(self) -> bool:
        return len(self.token) >= 32

    @classmethod
    def from_env(cls) -> "MT5SidecarAuthSettings":
        return cls(token=os.getenv("MT5_SIDECAR_TOKEN", "").strip())


auth_settings = MT5SidecarAuthSettings.from_env()
sidecar = MT5ReadOnlySidecar()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Do not make a disabled sidecar fatal to an otherwise read-only process.
    # Once it is explicitly enabled, a dedicated 32+ character hop token and
    # investor-mode terminal are both mandatory.
    if sidecar.settings.enabled and not auth_settings.configured:
        raise RuntimeError("MT5_SIDECAR_TOKEN must contain at least 32 characters when MT5 is enabled")
    await asyncio.to_thread(sidecar.start)
    try:
        yield
    finally:
        await asyncio.to_thread(sidecar.close)


class SidecarSecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


app = FastAPI(
    title="MT5 Read-only Sidecar",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SidecarSecurityHeaders)


def require_sidecar_auth(request: Request) -> None:
    if not auth_settings.configured:
        raise HTTPException(status_code=503, detail="MT5 read-only sidecar is not configured")
    supplied = request.headers.get("X-MT5-Sidecar-Token", "")
    if not supplied or not hmac.compare_digest(supplied, auth_settings.token):
        raise HTTPException(status_code=401, detail="MT5 sidecar authentication required")


def readonly_error(error: MT5ReadOnlyError) -> HTTPException:
    # ``MT5ReadOnlySidecar`` errors contain only safe operational states; do
    # not return traceback or terminal configuration details to callers.
    return HTTPException(status_code=503, detail=str(error))


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness only; it deliberately exposes no MT5 data."""
    return {"status": "ok", "service": "mt5-readonly-sidecar", "enabled": sidecar.settings.enabled}


@app.get("/v1/status")
async def status(request: Request):
    require_sidecar_auth(request)
    return sidecar.status().as_dict()


@app.get("/v1/account")
async def account(request: Request):
    require_sidecar_auth(request)
    try:
        return {"account": await asyncio.to_thread(sidecar.account_info)}
    except MT5ReadOnlyError as error:
        raise readonly_error(error)


@app.get("/v1/positions")
async def positions(request: Request):
    require_sidecar_auth(request)
    try:
        return {"positions": await asyncio.to_thread(sidecar.positions)}
    except MT5ReadOnlyError as error:
        raise readonly_error(error)


@app.get("/v1/quote/{symbol}")
async def quote(symbol: str, request: Request):
    require_sidecar_auth(request)
    try:
        return {"symbol": symbol, "quote": await asyncio.to_thread(sidecar.quote, symbol)}
    except MT5ReadOnlyError as error:
        raise readonly_error(error)
