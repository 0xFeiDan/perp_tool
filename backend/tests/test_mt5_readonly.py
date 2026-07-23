from __future__ import annotations

import sys
import unittest
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from mt5_readonly import (  # noqa: E402
    MT5ReadOnlyDisabled,
    MT5ReadOnlyError,
    MT5ReadOnlySettings,
    MT5ReadOnlySidecar,
    MT5ReadOnlyUnsafe,
    READ_ONLY_MT5_CALLS,
)


TerminalInfo = namedtuple("TerminalInfo", "trade_allowed connected")
AccountInfo = namedtuple("AccountInfo", "trade_allowed login balance")
TickInfo = namedtuple("TickInfo", "bid ask time")


class FakeMT5:
    """Offline MT5 double; it records every method the sidecar touches."""

    def __init__(self, *, terminal_trade_allowed: bool = False, account_trade_allowed: bool | None = False):
        self.terminal_trade_allowed = terminal_trade_allowed
        self.account_trade_allowed = account_trade_allowed
        self.calls: list[tuple[str, tuple, dict]] = []

    def initialize(self, **kwargs):
        self.calls.append(("initialize", (), kwargs))
        return True

    def shutdown(self):
        self.calls.append(("shutdown", (), {}))

    def terminal_info(self):
        self.calls.append(("terminal_info", (), {}))
        return TerminalInfo(self.terminal_trade_allowed, True)

    def account_info(self):
        self.calls.append(("account_info", (), {}))
        return AccountInfo(self.account_trade_allowed, 123456, 1000.5)

    def symbol_info_tick(self, symbol):
        self.calls.append(("symbol_info_tick", (symbol,), {}))
        return TickInfo(1.2345, 1.2348, 1_700_000_000)

    def positions_get(self, **kwargs):
        self.calls.append(("positions_get", (), kwargs))
        return []

    def last_error(self):
        self.calls.append(("last_error", (), {}))
        return (0, "ok")


class MT5ReadOnlySidecarTests(unittest.TestCase):
    def test_public_surface_is_named_read_only_operations(self):
        public_methods = {name for name, value in vars(MT5ReadOnlySidecar).items() if callable(value) and not name.startswith("_")}
        self.assertEqual(
            public_methods,
            {
                "start",
                "close",
                "status",
                "terminal_info",
                "account_info",
                "version",
                "symbols",
                "symbol_info",
                "quote",
                "positions",
                "pending_orders",
                "history_deals",
                "history_orders",
                "rates_from",
                "rates_from_position",
                "rates_range",
                "ticks_from",
                "ticks_range",
            },
        )
        forbidden_execution_calls = {
            "order_send",
            "order_check",
            "order_modify",
            "order_delete",
            "close_by",
        }
        self.assertTrue(READ_ONLY_MT5_CALLS.isdisjoint(forbidden_execution_calls))

    def test_default_disabled_does_not_touch_mt5(self):
        fake = FakeMT5()
        sidecar = MT5ReadOnlySidecar(MT5ReadOnlySettings(), mt5_module=fake)

        status = sidecar.start()

        self.assertFalse(status.enabled)
        self.assertFalse(status.initialized)
        self.assertEqual(fake.calls, [])
        with self.assertRaises(MT5ReadOnlyDisabled):
            sidecar.quote("EURUSD")

    def test_enabled_sidecar_requires_terminal_trade_allowed_false(self):
        fake = FakeMT5(terminal_trade_allowed=True)
        sidecar = MT5ReadOnlySidecar(MT5ReadOnlySettings(enabled=True), mt5_module=fake)

        with self.assertRaises(MT5ReadOnlyUnsafe):
            sidecar.start()

        self.assertEqual([name for name, _, _ in fake.calls], ["initialize", "terminal_info", "account_info", "shutdown"])
        self.assertFalse(sidecar.status().initialized)
        self.assertFalse(sidecar.status().readonly_verified)

    def test_enabled_sidecar_rejects_explicit_trade_enabled_account(self):
        fake = FakeMT5(terminal_trade_allowed=False, account_trade_allowed=True)
        sidecar = MT5ReadOnlySidecar(MT5ReadOnlySettings(enabled=True), mt5_module=fake)

        with self.assertRaises(MT5ReadOnlyUnsafe):
            sidecar.start()

        self.assertIn(("shutdown", (), {}), fake.calls)

    def test_only_named_read_methods_are_reachable_after_verification(self):
        fake = FakeMT5()
        sidecar = MT5ReadOnlySidecar(MT5ReadOnlySettings(enabled=True), mt5_module=fake)

        status = sidecar.start()
        quote = sidecar.quote("EURUSD")
        positions = sidecar.positions(symbol="EURUSD")

        self.assertTrue(status.readonly_verified)
        self.assertEqual(quote, {"bid": 1.2345, "ask": 1.2348, "time": 1_700_000_000})
        self.assertEqual(positions, [])
        names = {name for name, _, _ in fake.calls}
        self.assertTrue(names - {"initialize", "shutdown"} <= READ_ONLY_MT5_CALLS)
        self.assertFalse(hasattr(sidecar, "call"))
        self.assertFalse(hasattr(sidecar, "raw_client"))

    def test_read_query_validation_rejects_ambiguous_filters(self):
        fake = FakeMT5()
        sidecar = MT5ReadOnlySidecar(MT5ReadOnlySettings(enabled=True), mt5_module=fake)
        sidecar.start()

        with self.assertRaisesRegex(MT5ReadOnlyError, "only one MT5 query filter"):
            sidecar.positions(symbol="EURUSD", group="*")

    def test_missing_whitelisted_read_function_fails_closed(self):
        fake = FakeMT5()
        sidecar = MT5ReadOnlySidecar(MT5ReadOnlySettings(enabled=True), mt5_module=fake)
        sidecar.start()

        # The fake deliberately lacks the history function: this verifies the
        # production adapter fails closed instead of dynamically proxying APIs.
        with self.assertRaisesRegex(MT5ReadOnlyError, "required read function"):
            sidecar.history_orders(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
