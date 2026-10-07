-- Additive funding provenance; axgt_deposits remains the spendable balance.
-- Applied by deposit_ledger.init_once, or manually after the existing tables exist.
-- Manual application must run as one transaction (e.g. psql --single-transaction).
-- This lock matches the initializer's lock and serializes concurrent new workers.
SELECT pg_advisory_xact_lock(1096306510, hashtext(current_schema()));

CREATE TABLE IF NOT EXISTS axonos_funding_transactions (
    id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    funding_type TEXT NOT NULL CHECK (funding_type IN ('fiat', 'crypto')),
    payment_method TEXT NOT NULL CHECK (payment_method IN ('stripe_card', 'eth', 'usdc', 'axgt', 'unknown')),
    provider TEXT NOT NULL,
    provider_transaction_id TEXT,
    fiat_currency TEXT,
    fiat_amount NUMERIC,
    crypto_currency TEXT,
    crypto_amount NUMERIC,
    usd_valuation NUMERIC,
    expected_credits NUMERIC NOT NULL CHECK (expected_credits >= 0),
    credits_added NUMERIC NOT NULL DEFAULT 0 CHECK (credits_added >= 0),
    credits_per_usd NUMERIC,
    pricing_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL,
    stripe_checkout_session_id TEXT UNIQUE,
    stripe_payment_intent_id TEXT UNIQUE,
    stripe_customer_id TEXT,
    stripe_invoice_id TEXT,
    livemode BOOLEAN,
    chain_id BIGINT,
    block_number BIGINT,
    refunded_amount NUMERIC NOT NULL DEFAULT 0 CHECK (refunded_amount >= 0),
    reconciliation_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    paid_at DOUBLE PRECISION,
    credited_at DOUBLE PRECISION,
    UNIQUE (provider, provider_transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_funding_wallet_created
    ON axonos_funding_transactions (wallet_address, created_at);
CREATE INDEX IF NOT EXISTS idx_funding_reconciliation
    ON axonos_funding_transactions (updated_at) WHERE reconciliation_required;

CREATE TABLE IF NOT EXISTS axonos_funding_events (
    provider_event_id TEXT PRIMARY KEY,
    funding_id TEXT NOT NULL REFERENCES axonos_funding_transactions(id),
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_events_payment
    ON axonos_funding_events (funding_id, created_at);

-- Preserve old paid deposits without inventing unavailable historical rates or
-- ETH/USDC amounts. Unknown provenance is explicit; test/guest grants excluded.
INSERT INTO axonos_funding_transactions
    (id, wallet_address, funding_type, payment_method, provider,
     provider_transaction_id, crypto_currency, crypto_amount,
     expected_credits, credits_added, pricing_snapshot, status, block_number,
     created_at, updated_at, paid_at, credited_at)
SELECT 'legacy:' || tx_hash, wallet_address, 'crypto',
       CASE WHEN payment_rail IN ('eth', 'usdc', 'axgt') THEN payment_rail ELSE 'unknown' END,
       'onchain', tx_hash,
       CASE WHEN payment_rail IN ('eth', 'usdc', 'axgt') THEN upper(payment_rail) ELSE NULL END,
       CASE WHEN payment_rail = 'axgt' THEN axgt_amount ELSE NULL END,
       credited_minutes, credited_minutes,
       '{"source":"legacy_backfill","historical_pricing_unavailable":true}'::jsonb,
       'succeeded', block_number, created_at, created_at, created_at, created_at
FROM axgt_verified_deposits
WHERE credit_source = 'onchain' AND block_number > 0 AND credited_minutes > 0
ON CONFLICT (provider, provider_transaction_id) DO NOTHING;
