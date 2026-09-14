-- Fresh version-four schema for the dedicated X CAPI database. This file
-- intentionally refuses to merge with existing objects. Pre-v4 data must be
-- retained only in a backup and cut over to a fresh dedicated database; it is
-- never upgraded/re-exported in place.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';
-- pg_catalog is implicitly searched before public when it is not listed.
SET LOCAL search_path = public;

DO $preflight$
DECLARE
    relation_name TEXT;
BEGIN
    SELECT c.relname INTO relation_name
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname=current_schema()
       AND c.relname LIKE 'x_capi\_%' ESCAPE '\'
     ORDER BY c.relname LIMIT 1;
    IF relation_name IS NOT NULL THEN
        RAISE EXCEPTION
            'fresh X CAPI install refused: relation % already exists; provision a fresh dedicated database',
            relation_name;
    END IF;
    FOREACH relation_name IN ARRAY ARRAY[
        'x_capi_schema_meta',
        'x_capi_config_guard',
        'x_capi_attribution_contexts',
        'x_capi_revocation_tombstones',
        'x_capi_capacity',
        'x_capi_dedup',
        'x_capi_outbox',
        'x_capi_counters',
        'x_capi_worker_state'
    ] LOOP
        IF to_regclass(relation_name) IS NOT NULL THEN
            RAISE EXCEPTION
                'fresh X CAPI install refused: relation % already exists; provision a fresh dedicated database',
                relation_name;
        END IF;
    END LOOP;
END
$preflight$;

CREATE TABLE x_capi_schema_meta (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE x_capi_config_guard (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    max_policy_epoch BIGINT NOT NULL CHECK (max_policy_epoch > 0),
    deployment_id_hash CHAR(64) NOT NULL CHECK (
        deployment_id_hash ~ '^[0-9a-f]{64}$'
    ),
    mode_scope TEXT NOT NULL CHECK (mode_scope IN ('dry_run','live')),
    policy_version TEXT NOT NULL,
    audience_scope CHAR(64) NOT NULL CHECK (audience_scope ~ '^[0-9a-f]{64}$'),
    hash_key_fingerprint CHAR(64) NOT NULL CHECK (
        hash_key_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    context_key_fingerprint CHAR(64) NOT NULL CHECK (
        context_key_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE x_capi_attribution_contexts (
    id UUID PRIMARY KEY,
    handle_hash CHAR(64) NOT NULL UNIQUE,
    csrf_hash CHAR(64) NOT NULL,
    consent_state TEXT NOT NULL CHECK (
        consent_state IN ('unset','granted','denied','revoked','stale')
    ),
    mode_scope TEXT NOT NULL CONSTRAINT x_capi_attribution_contexts_mode_scope_check
        CHECK (mode_scope IN ('dry_run','live')),
    policy_version TEXT NOT NULL,
    policy_epoch BIGINT NOT NULL CHECK (policy_epoch > 0),
    audience_scope CHAR(64) NOT NULL CHECK (audience_scope ~ '^[0-9a-f]{64}$'),
    lifecycle_expires_at DOUBLE PRECISION NOT NULL CHECK (lifecycle_expires_at > 0),
    twclid TEXT,
    consented_at DOUBLE PRECISION,
    declined_at DOUBLE PRECISION,
    revoked_at DOUBLE PRECISION,
    expires_at DOUBLE PRECISION,
    wallet_hash CHAR(64),
    wallet_bound_at DOUBLE PRECISION,
    first_seen_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    CHECK (twclid IS NULL OR octet_length(twclid) BETWEEN 8 AND 256),
    CONSTRAINT x_capi_context_state_fields_check CHECK (
        (consent_state='granted' AND twclid IS NOT NULL
         AND consented_at IS NOT NULL AND expires_at IS NOT NULL
         AND consented_at < expires_at AND expires_at <= lifecycle_expires_at)
        OR
        (consent_state<>'granted' AND twclid IS NULL AND expires_at IS NULL)
    )
);

CREATE INDEX x_capi_context_expiry_v4_idx
    ON x_capi_attribution_contexts (expires_at) WHERE twclid IS NOT NULL;
CREATE UNIQUE INDEX x_capi_context_wallet_once_idx
    ON x_capi_attribution_contexts (wallet_hash)
    WHERE wallet_hash IS NOT NULL;

CREATE TABLE x_capi_revocation_tombstones (
    handle_hash CHAR(64) PRIMARY KEY,
    csrf_hash CHAR(64) NOT NULL,
    lifecycle_expires_at DOUBLE PRECISION NOT NULL CHECK (lifecycle_expires_at > 0),
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX x_capi_revocation_tombstone_expiry_idx
    ON x_capi_revocation_tombstones (lifecycle_expires_at);

CREATE TABLE x_capi_capacity (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    context_count BIGINT NOT NULL CHECK (context_count >= 0),
    tombstone_count BIGINT NOT NULL CHECK (tombstone_count >= 0),
    outbox_active_count BIGINT NOT NULL CHECK (outbox_active_count >= 0),
    revocation_saturated BOOLEAN NOT NULL DEFAULT FALSE,
    revocation_saturated_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    CONSTRAINT x_capi_capacity_revocation_saturation_check CHECK (
        (NOT revocation_saturated AND revocation_saturated_until=0)
        OR
        (revocation_saturated AND revocation_saturated_until>0
         AND revocation_saturated_until<'Infinity'::DOUBLE PRECISION)
    ),
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE x_capi_dedup (
    milestone TEXT NOT NULL CHECK (
        milestone IN ('wallet_verified','deposit_completed','session_started')
    ),
    source_key_hash CHAR(64) NOT NULL,
    first_seen_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (milestone, source_key_hash)
);

CREATE INDEX x_capi_dedup_expiry_idx ON x_capi_dedup (expires_at);

CREATE TABLE x_capi_outbox (
    conversion_id UUID PRIMARY KEY,
    milestone TEXT NOT NULL CHECK (
        milestone IN ('wallet_verified','deposit_completed','session_started')
    ),
    source_key_hash CHAR(64) NOT NULL,
    mode_scope TEXT NOT NULL CHECK (mode_scope IN ('dry_run','live')),
    pixel_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    conversion_timestamp_ms BIGINT NOT NULL CHECK (conversion_timestamp_ms > 0),
    twclid TEXT,
    context_id UUID NOT NULL REFERENCES x_capi_attribution_contexts(id),
    consent_policy_version TEXT NOT NULL,
    consent_policy_epoch BIGINT NOT NULL CHECK (consent_policy_epoch > 0),
    consent_audience_scope CHAR(64) NOT NULL CHECK (
        consent_audience_scope ~ '^[0-9a-f]{64}$'
    ),
    attribution_expires_at DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('dry_run','pending','leased','retrying','accepted','failed','expired','cancelled')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at DOUBLE PRECISION NOT NULL,
    lease_owner TEXT,
    lease_token UUID,
    lease_expires_at DOUBLE PRECISION,
    accepted_at DOUBLE PRECISION,
    last_error_code TEXT,
    safe_debug_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE (milestone, source_key_hash),
    CHECK (twclid IS NULL OR octet_length(twclid) BETWEEN 8 AND 256),
    CONSTRAINT x_capi_outbox_lease_fence_check CHECK (
        (status='leased' AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status<>'leased' AND lease_owner IS NULL
         AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE INDEX x_capi_outbox_claim_v4_idx
    ON x_capi_outbox (next_attempt_at, created_at)
    WHERE mode_scope='live' AND status IN ('pending','retrying','leased');
CREATE INDEX x_capi_outbox_context_v4_idx ON x_capi_outbox (context_id);
CREATE INDEX x_capi_outbox_cleanup_v4_idx
    ON x_capi_outbox (updated_at, status);
CREATE INDEX x_capi_outbox_queue_capacity_v4_idx
    ON x_capi_outbox (status, conversion_id)
    WHERE twclid IS NOT NULL
      AND status IN ('dry_run','pending','retrying','leased');

CREATE TABLE x_capi_counters (
    reason TEXT PRIMARY KEY,
    count BIGINT NOT NULL DEFAULT 0 CHECK (count >= 0),
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE x_capi_worker_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    paused_reason TEXT,
    paused_at DOUBLE PRECISION,
    lifecycle_state TEXT NOT NULL DEFAULT 'dirty'
        CONSTRAINT x_capi_worker_lifecycle_state_check CHECK (
        lifecycle_state IN ('dirty','clean')
    ),
    lifecycle_token UUID,
    ticket_not_before DOUBLE PRECISION NOT NULL DEFAULT 0
        CONSTRAINT x_capi_worker_ticket_cutoff_check CHECK (
        ticket_not_before >= 0
    ),
    updated_at DOUBLE PRECISION NOT NULL
);

INSERT INTO x_capi_schema_meta(singleton,schema_version,updated_at)
VALUES(TRUE,4,EXTRACT(EPOCH FROM clock_timestamp()));
INSERT INTO x_capi_capacity(
    singleton,context_count,tombstone_count,outbox_active_count,
    revocation_saturated,revocation_saturated_until,updated_at
) VALUES(TRUE,0,0,0,FALSE,0,EXTRACT(EPOCH FROM clock_timestamp()));

COMMIT;
