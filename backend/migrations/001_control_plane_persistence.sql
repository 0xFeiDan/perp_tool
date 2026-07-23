-- PostgreSQL durable control-plane schema.
--
-- Apply with a migration role before enabling DATABASE_URL in the runtime.
-- The application never stores exchange API keys, private keys, raw browser
-- confirmation tokens, cookies, or raw idempotency keys in these tables.

BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    email VARCHAR(254) NOT NULL UNIQUE,
    role VARCHAR(32) NOT NULL DEFAULT 'operator',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS exchange_accounts (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    venue VARCHAR(24) NOT NULL,
    label VARCHAR(80) NOT NULL,
    mode VARCHAR(24) NOT NULL,
    -- Environment/vault pointer only, never a secret itself.
    credential_reference VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_exchange_account_owner_venue_label UNIQUE (user_id, venue, label)
);

CREATE TABLE IF NOT EXISTS instruments (
    id VARCHAR(36) PRIMARY KEY,
    venue VARCHAR(24) NOT NULL,
    external_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(96) NOT NULL,
    instrument_type VARCHAR(24) NOT NULL DEFAULT 'perpetual',
    base_asset VARCHAR(24),
    quote_asset VARCHAR(24),
    min_notional NUMERIC(38, 18),
    quantity_step NUMERIC(38, 18),
    price_tick NUMERIC(38, 18),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_instrument_venue_external_id UNIQUE (venue, external_id)
);

CREATE TABLE IF NOT EXISTS order_intents (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    exchange_account_id VARCHAR(36) NOT NULL REFERENCES exchange_accounts(id) ON DELETE RESTRICT,
    instrument_id VARCHAR(36) NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
    venue VARCHAR(24) NOT NULL,
    instrument_key VARCHAR(180) NOT NULL,
    side VARCHAR(8) NOT NULL,
    intent VARCHAR(8) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    notional NUMERIC(38, 18) NOT NULL,
    -- SHA-256 only; never a raw one-time confirmation token.
    confirmation_token_hash VARCHAR(64) NOT NULL UNIQUE,
    request_fingerprint VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending_confirmation',
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ
);

-- Safe for an early schema created before consumed_at was added to the model.
ALTER TABLE order_intents ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(36) PRIMARY KEY,
    order_intent_id VARCHAR(36) NOT NULL REFERENCES order_intents(id) ON DELETE RESTRICT,
    exchange_account_id VARCHAR(36) NOT NULL REFERENCES exchange_accounts(id) ON DELETE RESTRICT,
    instrument_id VARCHAR(36) NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
    venue VARCHAR(24) NOT NULL,
    instrument_key VARCHAR(180) NOT NULL,
    client_order_id VARCHAR(64) NOT NULL,
    exchange_order_id VARCHAR(128),
    side VARCHAR(8) NOT NULL,
    intent VARCHAR(8) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    quantity NUMERIC(38, 18) NOT NULL,
    limit_price NUMERIC(38, 18) NOT NULL,
    filled_quantity NUMERIC(38, 18) NOT NULL DEFAULT 0,
    reduce_only BOOLEAN NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'created',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_order_account_client_order_id UNIQUE (exchange_account_id, client_order_id),
    CONSTRAINT uq_order_account_exchange_order_id UNIQUE (exchange_account_id, exchange_order_id)
);

CREATE TABLE IF NOT EXISTS follow_strategies (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    exchange_account_id VARCHAR(36) NOT NULL REFERENCES exchange_accounts(id) ON DELETE RESTRICT,
    instrument_id VARCHAR(36) NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
    venue VARCHAR(24) NOT NULL,
    instrument_key VARCHAR(180) NOT NULL,
    side VARCHAR(8) NOT NULL,
    intent VARCHAR(8) NOT NULL,
    quantity NUMERIC(38, 18) NOT NULL,
    reduce_only BOOLEAN NOT NULL,
    max_reprices_per_minute INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    active_order_id VARCHAR(36) REFERENCES orders(id) ON DELETE SET NULL,
    last_price NUMERIC(38, 18),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id VARCHAR(36) PRIMARY KEY,
    scope VARCHAR(128) NOT NULL,
    -- SHA-256(scope + raw key); never the raw idempotency key.
    key_hash VARCHAR(64) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'claimed',
    response_reference VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_idempotency_scope_key_hash UNIQUE (scope, key_hash)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id VARCHAR(36) PRIMARY KEY,
    event_type VARCHAR(96) NOT NULL,
    success BOOLEAN NOT NULL,
    actor_user_id VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
    venue VARCHAR(24),
    instrument_key VARCHAR(180),
    order_id VARCHAR(36) REFERENCES orders(id) ON DELETE SET NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_exchange_accounts_user_id ON exchange_accounts(user_id);
CREATE INDEX IF NOT EXISTS ix_instruments_venue ON instruments(venue);
CREATE INDEX IF NOT EXISTS ix_instruments_symbol ON instruments(symbol);
CREATE INDEX IF NOT EXISTS ix_order_intents_expires_at ON order_intents(expires_at);
CREATE INDEX IF NOT EXISTS ix_order_intents_request_fingerprint ON order_intents(request_fingerprint);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_follow_strategy_active_market ON follow_strategies(status, venue, instrument_key);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_request_fingerprint ON idempotency_keys(request_fingerprint);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_expires_at ON idempotency_keys(expires_at);
CREATE INDEX IF NOT EXISTS ix_audit_events_occurred_type ON audit_events(occurred_at, event_type);

COMMIT;
