"""Strictly read-only MetaTrader 5 adapter for an Ubuntu/Wine sidecar.

This module intentionally has no HTTP server and no generic MT5 method proxy.
The future control-plane integration must call only the named read methods below.
It never imports MetaTrader5 while disabled, which keeps the default installation
safe on machines that do not run an MT5 terminal.

Use an MT5 *investor* password for ``MT5_READONLY_PASSWORD``.  On startup the
terminal must explicitly report ``trade_allowed == False``; otherwise this
sidecar shuts down and refuses to serve any data.
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping


class MT5ReadOnlyError(RuntimeError):
    """Base exception for the deliberately restricted MT5 integration."""


class MT5ReadOnlyDisabled(MT5ReadOnlyError):
    """Raised when a caller attempts to use the sidecar while it is disabled."""


class MT5ReadOnlyUnsafe(MT5ReadOnlyError):
    """Raised when the terminal has not proven that trading is disabled."""


_MISSING = object()

# This is the complete set of MT5 data functions the adapter can invoke after
# startup.  There is purposefully no passthrough/call(method_name, ...) API.
READ_ONLY_MT5_CALLS = frozenset(
    {
        "terminal_info",
        "account_info",
        "symbols_get",
        "symbol_info",
        "symbol_info_tick",
        "positions_get",
        "orders_get",
        "history_deals_get",
        "history_orders_get",
        "copy_rates_from",
        "copy_rates_from_pos",
        "copy_rates_range",
        "copy_ticks_from",
        "copy_ticks_range",
        "version",
        "last_error",
    }
)


@dataclass(frozen=True)
class MT5ReadOnlySettings:
    """Configuration read from environment without exposing its secret values."""

    enabled: bool = False
    terminal_path: str | None = None
    login: int | None = None
    password: str | None = None
    server: str | None = None
    timeout_ms: int = 60_000

    @classmethod
    def from_env(cls) -> "MT5ReadOnlySettings":
        def enabled(name: str) -> bool:
            return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}

        def optional_int(name: str) -> int | None:
            value = os.getenv(name, "").strip()
            if not value:
                return None
            try:
                return int(value)
            except ValueError as error:
                raise MT5ReadOnlyError(f"{name} must be an integer") from error

        timeout_value = os.getenv("MT5_READONLY_TIMEOUT_MS", "60000").strip()
        try:
            timeout_ms = int(timeout_value)
        except ValueError as error:
            raise MT5ReadOnlyError("MT5_READONLY_TIMEOUT_MS must be an integer") from error
        if not 1_000 <= timeout_ms <= 300_000:
            raise MT5ReadOnlyError("MT5_READONLY_TIMEOUT_MS must be between 1000 and 300000")

        return cls(
            enabled=enabled("MT5_READONLY_ENABLED"),
            terminal_path=os.getenv("MT5_TERMINAL_PATH", "").strip() or None,
            login=optional_int("MT5_READONLY_LOGIN"),
            password=os.getenv("MT5_READONLY_PASSWORD", "").strip() or None,
            server=os.getenv("MT5_READONLY_SERVER", "").strip() or None,
            timeout_ms=timeout_ms,
        )


@dataclass(frozen=True)
class MT5ReadOnlyStatus:
    enabled: bool
    initialized: bool
    readonly_verified: bool
    terminal_trade_allowed: bool | None
    account_trade_allowed: bool | None

    def as_dict(self) -> dict[str, bool | None]:
        return {
            "enabled": self.enabled,
            "initialized": self.initialized,
            "readonly_verified": self.readonly_verified,
            "terminal_trade_allowed": self.terminal_trade_allowed,
            "account_trade_allowed": self.account_trade_allowed,
        }


class MT5ReadOnlySidecar:
    """A narrow data adapter for an MT5 terminal logged in with investor access.

    ``mt5_module`` is injectable solely for offline tests.  Production callers
    should leave it unset; MetaTrader5 is imported lazily only after the explicit
    ``MT5_READONLY_ENABLED=true`` switch has been set.
    """

    def __init__(self, settings: MT5ReadOnlySettings | None = None, *, mt5_module: Any | None = None):
        self.settings = settings or MT5ReadOnlySettings.from_env()
        self._mt5 = mt5_module
        self._initialized = False
        self._readonly_verified = False
        self._terminal_trade_allowed: bool | None = None
        self._account_trade_allowed: bool | None = None

    def start(self) -> MT5ReadOnlyStatus:
        """Connect and prove that the configured terminal cannot trade.

        A disabled sidecar is a benign no-op.  If enabled, incomplete credentials
        and any terminal that reports trading as allowed are hard failures.
        """

        if not self.settings.enabled:
            return self.status()
        if self._readonly_verified:
            return self.status()

        self._validate_connection_settings()
        module = self._load_mt5_module()
        init_kwargs: dict[str, Any] = {"timeout": self.settings.timeout_ms}
        if self.settings.terminal_path:
            init_kwargs["path"] = self.settings.terminal_path
        if self.settings.login is not None:
            init_kwargs.update(
                {
                    "login": self.settings.login,
                    "password": self.settings.password,
                    "server": self.settings.server,
                }
            )

        try:
            initialized = bool(module.initialize(**init_kwargs))
        except Exception as error:
            raise MT5ReadOnlyError("MT5 terminal initialization failed") from error
        if not initialized:
            raise MT5ReadOnlyError(f"MT5 terminal initialization failed: {self._last_error_text(module)}")

        self._initialized = True
        try:
            terminal = self._call_allowlisted("terminal_info")
            account = self._call_allowlisted("account_info")
            self._verify_readonly_permissions(terminal, account)
            self._readonly_verified = True
            return self.status()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Detach from the terminal.  This is lifecycle cleanup, not trading."""

        if self._initialized and self._mt5 is not None:
            try:
                self._mt5.shutdown()
            finally:
                self._initialized = False
                self._readonly_verified = False

    def status(self) -> MT5ReadOnlyStatus:
        return MT5ReadOnlyStatus(
            enabled=self.settings.enabled,
            initialized=self._initialized,
            readonly_verified=self._readonly_verified,
            terminal_trade_allowed=self._terminal_trade_allowed,
            account_trade_allowed=self._account_trade_allowed,
        )

    # Named data APIs: each is intentionally backed by one item in the fixed
    # allow-list above.  Keep this surface narrow when integrating it into FastAPI.
    def terminal_info(self) -> dict[str, Any]:
        return _json_safe(self._read("terminal_info"))

    def account_info(self) -> dict[str, Any]:
        return _json_safe(self._read("account_info"))

    def version(self) -> Any:
        return _json_safe(self._read("version"))

    def symbols(self, *, group: str | None = None) -> list[dict[str, Any]]:
        kwargs = {"group": group} if group else {}
        return _json_safe(self._read("symbols_get", **kwargs))

    def symbol_info(self, symbol: str) -> dict[str, Any] | None:
        return _json_safe(self._read("symbol_info", self._required_symbol(symbol)))

    def quote(self, symbol: str) -> dict[str, Any] | None:
        return _json_safe(self._read("symbol_info_tick", self._required_symbol(symbol)))

    def positions(self, *, symbol: str | None = None, group: str | None = None, ticket: int | None = None) -> list[dict[str, Any]]:
        return _json_safe(self._read("positions_get", **self._single_filter(symbol=symbol, group=group, ticket=ticket)))

    def pending_orders(self, *, symbol: str | None = None, group: str | None = None, ticket: int | None = None) -> list[dict[str, Any]]:
        return _json_safe(self._read("orders_get", **self._single_filter(symbol=symbol, group=group, ticket=ticket)))

    def history_deals(self, date_from: datetime | date, date_to: datetime | date, *, group: str | None = None, position: int | None = None) -> list[dict[str, Any]]:
        filters = self._single_filter(group=group, position=position)
        return _json_safe(self._read("history_deals_get", date_from, date_to, **filters))

    def history_orders(self, date_from: datetime | date, date_to: datetime | date, *, group: str | None = None, position: int | None = None) -> list[dict[str, Any]]:
        filters = self._single_filter(group=group, position=position)
        return _json_safe(self._read("history_orders_get", date_from, date_to, **filters))

    def rates_from(self, symbol: str, timeframe: int, date_from: datetime, count: int) -> list[dict[str, Any]]:
        return _json_safe(self._read("copy_rates_from", self._required_symbol(symbol), int(timeframe), date_from, self._positive_count(count)))

    def rates_from_position(self, symbol: str, timeframe: int, start_position: int, count: int) -> list[dict[str, Any]]:
        if start_position < 0:
            raise MT5ReadOnlyError("start_position must not be negative")
        return _json_safe(self._read("copy_rates_from_pos", self._required_symbol(symbol), int(timeframe), int(start_position), self._positive_count(count)))

    def rates_range(self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime) -> list[dict[str, Any]]:
        return _json_safe(self._read("copy_rates_range", self._required_symbol(symbol), int(timeframe), date_from, date_to))

    def ticks_from(self, symbol: str, date_from: datetime, count: int, flags: int) -> list[dict[str, Any]]:
        return _json_safe(self._read("copy_ticks_from", self._required_symbol(symbol), date_from, self._positive_count(count), int(flags)))

    def ticks_range(self, symbol: str, date_from: datetime, date_to: datetime, flags: int) -> list[dict[str, Any]]:
        return _json_safe(self._read("copy_ticks_range", self._required_symbol(symbol), date_from, date_to, int(flags)))

    def _read(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if not self.settings.enabled:
            raise MT5ReadOnlyDisabled("MT5 read-only sidecar is disabled")
        if not self._readonly_verified:
            raise MT5ReadOnlyUnsafe("MT5 read-only permissions have not been verified")
        return self._call_allowlisted(operation, *args, **kwargs)

    def _call_allowlisted(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if operation not in READ_ONLY_MT5_CALLS:
            raise MT5ReadOnlyUnsafe(f"MT5 operation is not in the read-only allow-list: {operation}")
        if self._mt5 is None:
            raise MT5ReadOnlyError("MT5 module is unavailable")
        function = getattr(self._mt5, operation, None)
        if not callable(function):
            raise MT5ReadOnlyError(f"MT5 does not expose required read function: {operation}")
        return function(*args, **kwargs)

    def _verify_readonly_permissions(self, terminal: Any, account: Any) -> None:
        terminal_flag = _bool_field(terminal, "trade_allowed")
        self._terminal_trade_allowed = terminal_flag
        if terminal_flag is not False:
            raise MT5ReadOnlyUnsafe("MT5 terminal must report trade_allowed=false for this sidecar")

        # An investor login must make the account flag explicitly false too.
        # Failing closed here avoids accepting an unexpected terminal/account
        # version whose permissions cannot be verified.
        account_flag = _bool_field(account, "trade_allowed")
        self._account_trade_allowed = account_flag
        if account_flag is not False:
            raise MT5ReadOnlyUnsafe("MT5 account must report trade_allowed=false; use investor access")

    def _load_mt5_module(self) -> Any:
        if self._mt5 is not None:
            return self._mt5
        try:
            self._mt5 = importlib.import_module("MetaTrader5")
        except ImportError as error:
            raise MT5ReadOnlyError("MetaTrader5 is not installed in the MT5 sidecar environment") from error
        return self._mt5

    def _validate_connection_settings(self) -> None:
        credential_values = (self.settings.login, self.settings.password, self.settings.server)
        if any(value is not None for value in credential_values) and not all(value is not None for value in credential_values):
            raise MT5ReadOnlyError("MT5_READONLY_LOGIN, MT5_READONLY_PASSWORD, and MT5_READONLY_SERVER must be set together")

    @staticmethod
    def _required_symbol(symbol: str) -> str:
        cleaned = symbol.strip()
        if not cleaned:
            raise MT5ReadOnlyError("symbol is required")
        return cleaned

    @staticmethod
    def _positive_count(count: int) -> int:
        value = int(count)
        if value <= 0:
            raise MT5ReadOnlyError("count must be positive")
        return value

    @staticmethod
    def _single_filter(**values: Any) -> dict[str, Any]:
        present = {key: value for key, value in values.items() if value is not None}
        if len(present) > 1:
            raise MT5ReadOnlyError("only one MT5 query filter may be used at a time")
        return present

    @staticmethod
    def _last_error_text(module: Any) -> str:
        try:
            result = module.last_error()
        except Exception:
            return "unknown MT5 error"
        return str(result)


def _bool_field(value: Any, field: str) -> bool | None:
    """Return a strict bool for MT5 namedtuple/dict fields, else ``None``."""

    if isinstance(value, Mapping):
        raw = value.get(field, _MISSING)
    else:
        raw = getattr(value, field, _MISSING)
    if raw is _MISSING:
        return None
    if raw is True or raw == 1:
        return True
    if raw is False or raw == 0:
        return False
    return None


def _json_safe(value: Any) -> Any:
    """Convert MT5 namedtuples/arrays into values safe for a future JSON route."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "_asdict"):
        return {str(key): _json_safe(item) for key, item in value._asdict().items()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    return str(value)
