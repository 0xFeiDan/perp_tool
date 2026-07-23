"""Optional bridge from the live in-memory intent flow to durable storage.

The current FastAPI service can remain completely in-memory by constructing
this bridge with no database URL.  When enabled, the bridge creates stable,
single-operator mappings and persists only hashes of confirmation and
idempotency tokens.  It performs no exchange/network I/O and is intentionally
not imported by ``main.py`` yet.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

try:  # Compatible with both ``--app-dir backend`` and package imports.
    from .domain_models import (
        AccountMode,
        DomainValidationError,
        ExchangeAccount,
        ExecutionMode,
        Instrument,
        Order,
        OrderIntent,
        OrderStatus,
        Side,
        TradeIntent,
        User,
        Venue,
    )
    from .repository import (
        IdempotencyClaim,
        OrderStateSnapshot,
        PersistenceRepository,
        repository_if_configured,
    )
except ImportError:  # pragma: no cover - direct app-dir import path
    from domain_models import (
        AccountMode,
        DomainValidationError,
        ExchangeAccount,
        ExecutionMode,
        Instrument,
        Order,
        OrderIntent,
        OrderStatus,
        Side,
        TradeIntent,
        User,
        Venue,
    )
    from repository import (
        IdempotencyClaim,
        OrderStateSnapshot,
        PersistenceRepository,
        repository_if_configured,
    )


class RuntimePersistenceBridgeError(RuntimeError):
    """The runtime input cannot safely be represented by durable state."""


_NAMESPACE = uuid.UUID("cfb4a478-4e43-4ff4-948b-58e1bb4b64b0")
_TRUSTED_OPERATOR_NAME = "trusted-control-plane-operator"


def _stable_id(kind: str, value: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{value}"))


def _decimal(value: Decimal | str | int | float, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise RuntimePersistenceBridgeError(f"{field_name} must be a decimal") from error
    if not result.is_finite() or result <= 0:
        raise RuntimePersistenceBridgeError(f"{field_name} must be positive")
    return result


def _nonnegative_decimal(value: Decimal | str | int | float, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise RuntimePersistenceBridgeError(f"{field_name} must be a decimal") from error
    if not result.is_finite() or result < 0:
        raise RuntimePersistenceBridgeError(f"{field_name} must be non-negative")
    return result


def _optional_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise RuntimePersistenceBridgeError(f"{field_name} must be a decimal") from error
    if not result.is_finite() or result < 0:
        raise RuntimePersistenceBridgeError(f"{field_name} must be non-negative")
    # Some exchange metadata uses zero to mean that no public minimum is set.
    return result if result > 0 else None


def _read(instrument: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(instrument, Mapping):
        return instrument.get(field_name, default)
    return getattr(instrument, field_name, default)


@dataclass(frozen=True, slots=True)
class RuntimeIntentContext:
    """Safe durable context returned to the in-memory order-intent runtime."""

    persistence_enabled: bool
    persistent_intent_id: str | None
    user_id: str
    exchange_account_id: str
    instrument_id: str
    internal_instrument_id: str
    venue: str
    instrument_key: str
    side: str
    intent: str
    mode: str
    notional: Decimal
    request_fingerprint: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeExecutionClaim:
    persistence_enabled: bool
    persistent_claim_id: str | None
    claimed: bool
    status: str
    response_reference: str | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeSubmissionContext:
    """A durable intent has been consumed and may now have an order recorded."""

    intent: RuntimeIntentContext
    persistent_consumed: bool
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeOrderContext:
    persistence_enabled: bool
    persistent_order_id: str | None
    persistent_intent_id: str | None
    exchange_account_id: str
    instrument_id: str
    venue: str
    instrument_key: str
    client_order_id: str
    status: str
    quantity: Decimal
    limit_price: Decimal
    exchange_order_id: str | None
    filled_quantity: Decimal


class RuntimePersistenceBridge:
    """Bridge API intended to be called around the current ``OrderIntentStore``.

    Suggested main-service order when persistence is enabled:

    ``prepare_intent`` -> browser/in-memory owner validation ->
    ``claim_execution`` -> ``consume_for_submission`` -> ``pre_submit_order``
    -> exchange adapter -> ``mark_order_submitted`` / ``record_order_status``.

    If any durable step fails, no exchange call should be attempted.
    """

    def __init__(self, repository: PersistenceRepository | None = None):
        self._repository = repository

    @classmethod
    def from_database_url(
        cls,
        database_url: str | None,
        *,
        initialize_schema: bool = False,
    ) -> "RuntimePersistenceBridge":
        return cls(repository_if_configured(database_url, initialize_schema=initialize_schema))

    @property
    def enabled(self) -> bool:
        return self._repository is not None

    def assert_ready(self) -> None:
        """Fail closed if configured durable storage cannot be reached."""
        if self._repository is not None:
            self._repository.ping()

    def dispose(self) -> None:
        if self._repository is not None:
            self._repository.dispose()

    def prepare_intent(
        self,
        *,
        venue: str,
        internal_instrument: Any,
        side: str,
        intent: str,
        mode: str,
        notional: Decimal | str | int | float,
        raw_token: str,
        expires_in_seconds: int = 30,
    ) -> RuntimeIntentContext:
        """Persist a new order intent.  ``raw_token`` is immediately hashed.

        ``internal_instrument`` accepts the existing
        ``instrument_registry.Instrument`` object or a mapping with the same
        public fields.  Its stable UUID is reused as the database instrument ID.
        """
        venue_value = self._venue(venue)
        if venue_value is Venue.MT5:
            raise RuntimePersistenceBridgeError("MT5_TRADING_FORBIDDEN: MT5 is read-only")
        domain_instrument = self._domain_instrument(venue_value, internal_instrument)
        side_value = self._side(side)
        intent_value = self._intent(intent)
        mode_value = self._mode(mode)
        notional_value = _decimal(notional, "notional")
        user, account = self._trusted_operator_mapping(venue_value)
        fingerprint = self._request_fingerprint(
            venue=venue_value,
            internal_instrument_id=self._internal_id(internal_instrument),
            instrument_key=domain_instrument.canonical_key,
            side=side_value,
            intent=intent_value,
            mode=mode_value,
            notional=notional_value,
        )

        if self._repository is None:
            return RuntimeIntentContext(
                persistence_enabled=False,
                persistent_intent_id=None,
                user_id=user.id,
                exchange_account_id=account.id,
                instrument_id=domain_instrument.id,
                internal_instrument_id=self._internal_id(internal_instrument),
                venue=venue_value.value,
                instrument_key=domain_instrument.canonical_key,
                side=side_value.value,
                intent=intent_value.value,
                mode=mode_value.value,
                notional=notional_value,
                request_fingerprint=fingerprint,
                expires_at=None,
            )

        try:
            mapping = self._repository.bootstrap_single_operator(
                user=user,
                account=account,
                instruments=[domain_instrument],
            )
            persistent_instrument_id = mapping.instrument_ids[domain_instrument.canonical_key]
            domain_intent = OrderIntent.create(
                user_id=mapping.user_id,
                exchange_account_id=mapping.exchange_account_id,
                instrument_id=persistent_instrument_id,
                venue=venue_value,
                instrument_key=domain_instrument.canonical_key,
                side=side_value,
                intent=intent_value,
                mode=mode_value,
                notional=notional_value,
                confirmation_token=raw_token,
                request_fingerprint=fingerprint,
                expires_in_seconds=expires_in_seconds,
            )
            self._repository.store_order_intent(domain_intent)
        except (DomainValidationError, ValueError) as error:
            raise RuntimePersistenceBridgeError(str(error)) from error

        return RuntimeIntentContext(
            persistence_enabled=True,
            persistent_intent_id=domain_intent.id,
            user_id=mapping.user_id,
            exchange_account_id=mapping.exchange_account_id,
            instrument_id=persistent_instrument_id,
            internal_instrument_id=self._internal_id(internal_instrument),
            venue=venue_value.value,
            instrument_key=domain_instrument.canonical_key,
            side=side_value.value,
            intent=intent_value.value,
            mode=mode_value.value,
            notional=notional_value,
            request_fingerprint=fingerprint,
            expires_at=domain_intent.expires_at,
        )

    def claim_execution(self, context: RuntimeIntentContext, *, request_id: str) -> RuntimeExecutionClaim:
        """Durably claim request-idempotency before the exchange adapter executes."""
        self._validate_context(context)
        if self._repository is None:
            return RuntimeExecutionClaim(False, None, True, "disabled", None, None)
        if not context.persistence_enabled or context.persistent_intent_id is None:
            raise RuntimePersistenceBridgeError("persistence bridge context is inconsistent")
        claim = self._repository.claim_idempotency(
            scope=f"runtime-execute:{context.exchange_account_id}:{context.persistent_intent_id}",
            key=request_id,
            request_fingerprint=context.request_fingerprint,
        )
        return self._claim_context(claim)

    def complete_execution(
        self,
        claim: RuntimeExecutionClaim,
        *,
        status: str,
        response_reference: str | None = None,
    ) -> RuntimeExecutionClaim:
        """Close an idempotency claim after a safe terminal result is known.

        The raw browser idempotency key is never accepted here again; callers
        can only complete the opaque persistent claim that was previously
        created for this exact server-side intent.
        """
        if not claim.persistence_enabled:
            return RuntimeExecutionClaim(False, None, False, status, response_reference, None)
        if self._repository is None or not claim.persistent_claim_id:
            raise RuntimePersistenceBridgeError("persistence execution claim is inconsistent")
        return self._claim_context(
            self._repository.complete_idempotency(
                claim.persistent_claim_id,
                status=status,
                response_reference=response_reference,
            )
        )

    def consume_for_submission(
        self,
        context: RuntimeIntentContext,
        *,
        raw_token: str,
    ) -> RuntimeSubmissionContext:
        """Confirm and atomically consume the durable token before pre-submit."""
        self._validate_context(context)
        if self._repository is None:
            return RuntimeSubmissionContext(intent=context, persistent_consumed=False, consumed_at=None)
        if not context.persistence_enabled or context.persistent_intent_id is None:
            raise RuntimePersistenceBridgeError("persistence bridge context is inconsistent")
        self._repository.confirm_order_intent(context.persistent_intent_id, raw_token)
        consumed = self._repository.consume_confirmed_order_intent(context.persistent_intent_id)
        if (
            consumed.exchange_account_id != context.exchange_account_id
            or consumed.instrument_id != context.instrument_id
            or consumed.instrument_key != context.instrument_key
            or consumed.request_fingerprint != context.request_fingerprint
        ):
            raise RuntimePersistenceBridgeError("durable order intent does not match runtime context")
        return RuntimeSubmissionContext(intent=context, persistent_consumed=True, consumed_at=consumed.consumed_at)

    def pre_submit_order(
        self,
        submission: RuntimeSubmissionContext,
        *,
        client_order_id: str,
        quantity: Decimal | str | int | float,
        limit_price: Decimal | str | int | float,
    ) -> RuntimeOrderContext:
        """Create the durable ``created`` order before sending it to an adapter."""
        context = submission.intent
        self._validate_context(context)
        quantity_value = _decimal(quantity, "quantity")
        price_value = _decimal(limit_price, "limit_price")
        if self._repository is None:
            return RuntimeOrderContext(
                persistence_enabled=False,
                persistent_order_id=None,
                persistent_intent_id=None,
                exchange_account_id=context.exchange_account_id,
                instrument_id=context.instrument_id,
                venue=context.venue,
                instrument_key=context.instrument_key,
                client_order_id=client_order_id,
                status=OrderStatus.CREATED.value,
                quantity=quantity_value,
                limit_price=price_value,
                exchange_order_id=None,
                filled_quantity=Decimal("0"),
            )
        if not submission.persistent_consumed or context.persistent_intent_id is None:
            raise RuntimePersistenceBridgeError("a persistent order requires an atomically consumed intent")
        order = Order(
            id=_stable_id("order", f"{context.persistent_intent_id}:{client_order_id}"),
            order_intent_id=context.persistent_intent_id,
            exchange_account_id=context.exchange_account_id,
            instrument_id=context.instrument_id,
            venue=Venue(context.venue),
            instrument_key=context.instrument_key,
            client_order_id=client_order_id,
            side=Side(context.side),
            intent=TradeIntent(context.intent),
            mode=ExecutionMode(context.mode),
            quantity=quantity_value,
            limit_price=price_value,
            reduce_only=context.intent == TradeIntent.CLOSE.value,
        )
        snapshot = self._repository.store_order(order)
        return self._order_context(context, client_order_id, quantity_value, price_value, snapshot)

    def mark_order_submitted(
        self,
        order: RuntimeOrderContext,
        *,
        exchange_order_id: str | None = None,
    ) -> RuntimeOrderContext:
        """Record that the adapter accepted the pre-submitted order."""
        return self.record_order_status(order, status=OrderStatus.SUBMITTED, exchange_order_id=exchange_order_id)

    def record_order_status(
        self,
        order: RuntimeOrderContext,
        *,
        status: OrderStatus | str,
        exchange_order_id: str | None = None,
        filled_quantity: Decimal | str | int | float | None = None,
        audit_metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeOrderContext:
        """Persist a post-adapter state transition and append a redacted audit event."""
        if not order.persistence_enabled:
            return replace(
                order,
                status=OrderStatus(status).value,
                exchange_order_id=exchange_order_id or order.exchange_order_id,
                filled_quantity=order.filled_quantity if filled_quantity is None else _nonnegative_decimal(filled_quantity, "filled_quantity"),
            )
        if self._repository is None or order.persistent_order_id is None:
            raise RuntimePersistenceBridgeError("persistence order context is inconsistent")
        snapshot = self._repository.record_order_status(
            order.persistent_order_id,
            OrderStatus(status),
            exchange_order_id=exchange_order_id,
            filled_quantity=None if filled_quantity is None else _nonnegative_decimal(filled_quantity, "filled_quantity"),
            audit_metadata=audit_metadata,
        )
        return RuntimeOrderContext(
            persistence_enabled=True,
            persistent_order_id=snapshot.id,
            persistent_intent_id=order.persistent_intent_id,
            exchange_account_id=order.exchange_account_id,
            instrument_id=order.instrument_id,
            venue=order.venue,
            instrument_key=order.instrument_key,
            client_order_id=order.client_order_id,
            status=snapshot.status,
            quantity=order.quantity,
            limit_price=order.limit_price,
            exchange_order_id=snapshot.exchange_order_id,
            filled_quantity=snapshot.filled_quantity,
        )

    @staticmethod
    def _claim_context(claim: IdempotencyClaim) -> RuntimeExecutionClaim:
        return RuntimeExecutionClaim(
            persistence_enabled=True,
            persistent_claim_id=claim.id,
            claimed=claim.claimed,
            status=claim.status,
            response_reference=claim.response_reference,
            expires_at=claim.expires_at,
        )

    @staticmethod
    def _order_context(
        context: RuntimeIntentContext,
        client_order_id: str,
        quantity: Decimal,
        limit_price: Decimal,
        snapshot: OrderStateSnapshot,
    ) -> RuntimeOrderContext:
        return RuntimeOrderContext(
            persistence_enabled=True,
            persistent_order_id=snapshot.id,
            persistent_intent_id=context.persistent_intent_id,
            exchange_account_id=context.exchange_account_id,
            instrument_id=context.instrument_id,
            venue=context.venue,
            instrument_key=context.instrument_key,
            client_order_id=client_order_id,
            status=snapshot.status,
            quantity=quantity,
            limit_price=limit_price,
            exchange_order_id=snapshot.exchange_order_id,
            filled_quantity=snapshot.filled_quantity,
        )

    @staticmethod
    def _venue(value: str) -> Venue:
        try:
            return Venue(str(value).strip().lower())
        except ValueError as error:
            raise RuntimePersistenceBridgeError("unsupported venue") from error

    @staticmethod
    def _side(value: str) -> Side:
        try:
            return Side(str(value).strip().lower())
        except ValueError as error:
            raise RuntimePersistenceBridgeError("unsupported order side") from error

    @staticmethod
    def _intent(value: str) -> TradeIntent:
        try:
            return TradeIntent(str(value).strip().lower())
        except ValueError as error:
            raise RuntimePersistenceBridgeError("unsupported trade intent") from error

    @staticmethod
    def _mode(value: str) -> ExecutionMode:
        try:
            return ExecutionMode(str(value).strip().lower())
        except ValueError as error:
            raise RuntimePersistenceBridgeError("unsupported execution mode") from error

    @staticmethod
    def _internal_id(instrument: Any) -> str:
        value = _read(instrument, "internal_instrument_id")
        if not isinstance(value, str) or not value.strip():
            raise RuntimePersistenceBridgeError("internal instrument id is required")
        try:
            return str(uuid.UUID(value))
        except (ValueError, TypeError) as error:
            raise RuntimePersistenceBridgeError("internal instrument id must be a UUID") from error

    def _domain_instrument(self, venue: Venue, runtime: Any) -> Instrument:
        runtime_venue = str(_read(runtime, "venue", "")).strip().lower()
        if runtime_venue != venue.value:
            raise RuntimePersistenceBridgeError("internal instrument does not belong to the requested venue")
        status = str(_read(runtime, "status", "active")).strip().lower()
        external_id = str(_read(runtime, "external_id", "")).strip()
        symbol = str(_read(runtime, "symbol", "")).strip()
        if not external_id or not symbol:
            raise RuntimePersistenceBridgeError("internal instrument is missing external id or symbol")
        try:
            return Instrument(
                id=self._internal_id(runtime),
                venue=venue,
                external_id=external_id,
                symbol=symbol,
                base_asset=_read(runtime, "base_asset"),
                quote_asset=_read(runtime, "quote_asset"),
                min_notional=_optional_decimal(_read(runtime, "min_notional"), "min_notional"),
                quantity_step=_optional_decimal(_read(runtime, "quantity_step"), "quantity_step"),
                price_tick=_optional_decimal(_read(runtime, "price_tick"), "price_tick"),
                is_active=status in {"active", "trading"},
                # Use a fixed public subset instead of carrying arbitrary gateway data.
                metadata={
                    "runtime_internal_instrument_id": self._internal_id(runtime),
                    "status": status,
                    "settle_asset": _read(runtime, "settle_asset"),
                },
            )
        except DomainValidationError as error:
            raise RuntimePersistenceBridgeError(str(error)) from error

    @staticmethod
    def _trusted_operator_mapping(venue: Venue) -> tuple[User, ExchangeAccount]:
        user = User(
            id=_stable_id("user", _TRUSTED_OPERATOR_NAME),
            username="trusted_operator",
            email="operator@control-plane.invalid",
            role="operator",
        )
        account = ExchangeAccount(
            id=_stable_id("exchange-account", f"{user.id}:{venue.value}:primary"),
            user_id=user.id,
            venue=venue,
            label=f"{venue.value}-primary",
            mode=AccountMode.LIVE,
            # Reference only.  No environment variable is read by this bridge.
            credential_reference=f"env:{venue.value.upper()}_EXECUTION_CREDENTIALS",
        )
        return user, account

    @staticmethod
    def _request_fingerprint(
        *,
        venue: Venue,
        internal_instrument_id: str,
        instrument_key: str,
        side: Side,
        intent: TradeIntent,
        mode: ExecutionMode,
        notional: Decimal,
    ) -> str:
        payload = {
            "venue": venue.value,
            "internal_instrument_id": internal_instrument_id,
            "instrument_key": instrument_key,
            "side": side.value,
            "intent": intent.value,
            "mode": mode.value,
            "notional": format(notional, "f"),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_context(context: RuntimeIntentContext) -> None:
        if not isinstance(context, RuntimeIntentContext):
            raise RuntimePersistenceBridgeError("invalid runtime persistence context")
        if context.venue not in {Venue.LIGHTER.value, Venue.HYPERLIQUID.value, Venue.BINANCE.value}:
            raise RuntimePersistenceBridgeError("runtime context venue is not executable")
        if not context.instrument_key.startswith(f"{context.venue}:"):
            raise RuntimePersistenceBridgeError("runtime context has a cross-venue instrument key")

def bridge_if_configured(
    database_url: str | None,
    *,
    initialize_schema: bool = False,
) -> RuntimePersistenceBridge:
    """Build a disabled bridge when DATABASE_URL is absent; no implicit env read."""
    return RuntimePersistenceBridge.from_database_url(database_url, initialize_schema=initialize_schema)


__all__ = [
    "RuntimeExecutionClaim",
    "RuntimeIntentContext",
    "RuntimeOrderContext",
    "RuntimePersistenceBridge",
    "RuntimePersistenceBridgeError",
    "RuntimeSubmissionContext",
    "bridge_if_configured",
]
