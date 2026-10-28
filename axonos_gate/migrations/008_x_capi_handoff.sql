-- Dedicated CAPI database only. Financial migrations 006/007 are unrelated.
-- Execute through apply_x_capi_migrations.sh, which attests the dedicated target
-- and migration identity before this transactional exact-schema upgrade.
-- Atomic exact-v4 -> exact-v5 transition. Gate and landing receive no SQL rights.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '30s';
SET LOCAL search_path = public;
\ir 008_x_capi_v4_preflight.sql
CREATE TABLE x_capi_handoff_generations (
    generation UUID PRIMARY KEY,
    policy_epoch BIGINT NOT NULL CHECK(policy_epoch BETWEEN 1 AND 2147483647),
    audience_scope BYTEA NOT NULL CHECK(octet_length(audience_scope)=32),
    state TEXT NOT NULL CHECK(state IN ('active','quarantined','retired')),
    landing_control_version BIGINT NOT NULL CHECK(landing_control_version BETWEEN 0 AND 9007199254740991),
    source_count BIGINT NOT NULL CHECK(source_count BETWEEN 0 AND 9007199254740991),
    app_owner_count BIGINT NOT NULL CHECK(app_owner_count BETWEEN 0 AND 9007199254740991),
    transaction_count BIGINT NOT NULL CHECK(transaction_count BETWEEN 0 AND 9007199254740991),
    created_at_ms BIGINT NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991),
    retired_at_ms BIGINT CHECK(retired_at_ms BETWEEN 1 AND 9007199254740991),
    updated_at_ms BIGINT NOT NULL CHECK(updated_at_ms BETWEEN 1 AND 9007199254740991),
    UNIQUE(policy_epoch),
    UNIQUE(generation,policy_epoch),
    CHECK((state='retired')=(retired_at_ms IS NOT NULL))
);

CREATE TABLE x_capi_handoff_sources (
    generation UUID NOT NULL,
    policy_epoch BIGINT NOT NULL CHECK(policy_epoch BETWEEN 1 AND 2147483647),
    landing_owner_hash BYTEA NOT NULL CHECK(octet_length(landing_owner_hash)=32),
    state TEXT NOT NULL CHECK(state IN ('captured','assigned','consumed','closed','expired')),
    capture_request_id UUID CHECK(substring(capture_request_id::text,15,1)='4' AND substring(capture_request_id::text,20,1) IN ('8','9','a','b')),
    capture_request_digest BYTEA CHECK(octet_length(capture_request_digest)=32),
    observation_id UUID,
    observed_at_ms BIGINT CHECK(observed_at_ms BETWEEN 1 AND 9007199254740991),
    landing_consented_at_ms BIGINT CHECK(landing_consented_at_ms BETWEEN 1 AND 9007199254740991),
    original_expires_at_ms BIGINT CHECK(original_expires_at_ms BETWEEN 1 AND 9007199254740991),
    landing_policy_version TEXT CHECK(landing_policy_version='landing-x-v2'),
    twclid_ciphertext BYTEA CHECK(octet_length(twclid_ciphertext) BETWEEN 24 AND 272),
    encryption_nonce BYTEA CHECK(octet_length(encryption_nonce)=12),
    encryption_key_id TEXT CHECK(encryption_key_id ~ '^[A-Za-z0-9_-]{1,32}$' AND octet_length(encryption_key_id) BETWEEN 1 AND 32),
    assigned_transaction_hash BYTEA CHECK(octet_length(assigned_transaction_hash)=32),
    revocation_version BIGINT NOT NULL CHECK(revocation_version BETWEEN 0 AND 9007199254740991),
    close_request_id UUID CHECK(substring(close_request_id::text,15,1)='4' AND substring(close_request_id::text,20,1) IN ('8','9','a','b')),
    close_request_digest BYTEA CHECK(octet_length(close_request_digest)=32),
    closed_reason TEXT CHECK(closed_reason IN ('withdrawn','gpc','denied','generation_retired','capacity')),
    closed_at_ms BIGINT CHECK(closed_at_ms BETWEEN 1 AND 9007199254740991),
    tombstone_until_ms BIGINT CHECK(tombstone_until_ms BETWEEN 1 AND 9007199254740991),
    created_at_ms BIGINT NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991),
    updated_at_ms BIGINT NOT NULL CHECK(updated_at_ms BETWEEN 1 AND 9007199254740991),
    PRIMARY KEY(generation,landing_owner_hash),
    UNIQUE(generation,capture_request_id),
    UNIQUE(generation,close_request_id),
    UNIQUE(generation,observation_id),
    UNIQUE(assigned_transaction_hash),
    CHECK((capture_request_id IS NULL)=(capture_request_digest IS NULL)),
    CHECK((close_request_id IS NULL)=(close_request_digest IS NULL)),
    CHECK((encryption_nonce IS NULL)=(twclid_ciphertext IS NULL) AND (encryption_key_id IS NULL)=(twclid_ciphertext IS NULL)),
    CHECK(state NOT IN ('captured','assigned','consumed') OR (capture_request_id IS NOT NULL AND observation_id IS NOT NULL AND observed_at_ms IS NOT NULL AND landing_consented_at_ms IS NOT NULL AND original_expires_at_ms IS NOT NULL AND landing_consented_at_ms>=observed_at_ms AND original_expires_at_ms=observed_at_ms+604790000 AND landing_consented_at_ms<original_expires_at_ms AND landing_policy_version IS NOT NULL)),
    CHECK(state NOT IN ('captured','assigned') OR twclid_ciphertext IS NOT NULL),
    CHECK(state NOT IN ('consumed','closed','expired') OR twclid_ciphertext IS NULL),
    CHECK(state<>'captured' OR assigned_transaction_hash IS NULL),
    CHECK(state NOT IN ('assigned','consumed') OR assigned_transaction_hash IS NOT NULL),
    CHECK(state<>'closed' OR (closed_reason IS NOT NULL AND closed_at_ms IS NOT NULL AND revocation_version>0 AND tombstone_until_ms>=closed_at_ms+7862400000)),
    FOREIGN KEY(generation,policy_epoch) REFERENCES x_capi_handoff_generations(generation,policy_epoch)
);

CREATE TABLE x_capi_handoff_app_owners (
    generation UUID NOT NULL,
    policy_epoch BIGINT NOT NULL CHECK(policy_epoch BETWEEN 1 AND 2147483647),
    app_owner_hash BYTEA NOT NULL CHECK(octet_length(app_owner_hash)=32),
    state TEXT NOT NULL CHECK(state IN ('prepared','imported','closed','expired')),
    prepare_request_id UUID NOT NULL CHECK(substring(prepare_request_id::text,15,1)='4' AND substring(prepare_request_id::text,20,1) IN ('8','9','a','b')),
    prepare_request_digest BYTEA NOT NULL CHECK(octet_length(prepare_request_digest)=32),
    created_at_ms BIGINT NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991),
    provisional_expires_at_ms BIGINT NOT NULL CHECK(provisional_expires_at_ms BETWEEN 1 AND 9007199254740991),
    cookie_confirmed_at_ms BIGINT CHECK(cookie_confirmed_at_ms BETWEEN 1 AND 9007199254740991),
    owner_expires_at_ms BIGINT NOT NULL CHECK(owner_expires_at_ms BETWEEN 1 AND 9007199254740991),
    open_transaction_count BIGINT NOT NULL CHECK(open_transaction_count BETWEEN 0 AND 4),
    closed_at_ms BIGINT CHECK(closed_at_ms BETWEEN 1 AND 9007199254740991),
    updated_at_ms BIGINT NOT NULL CHECK(updated_at_ms BETWEEN 1 AND 9007199254740991),
    PRIMARY KEY(generation,app_owner_hash),
    UNIQUE(generation,prepare_request_id),
    CHECK(provisional_expires_at_ms=created_at_ms+120000),
    CHECK(owner_expires_at_ms>created_at_ms AND owner_expires_at_ms<=created_at_ms+604800000),
    CHECK(state<>'closed' OR closed_at_ms IS NOT NULL),
    FOREIGN KEY(generation,policy_epoch) REFERENCES x_capi_handoff_generations(generation,policy_epoch)
);

CREATE TABLE x_capi_handoff_transactions (
    transaction_hash BYTEA NOT NULL CHECK(octet_length(transaction_hash)=32),
    generation UUID NOT NULL,
    policy_epoch BIGINT NOT NULL CHECK(policy_epoch BETWEEN 1 AND 2147483647),
    app_owner_hash BYTEA NOT NULL CHECK(octet_length(app_owner_hash)=32),
    state TEXT NOT NULL CHECK(state IN ('app_granted','source_approved','redeemed','cancelled','expired')),
    begin_request_id UUID NOT NULL CHECK(substring(begin_request_id::text,15,1)='4' AND substring(begin_request_id::text,20,1) IN ('8','9','a','b')),
    begin_request_digest BYTEA NOT NULL CHECK(octet_length(begin_request_digest)=32),
    app_policy_version TEXT NOT NULL CHECK(app_policy_version='app-x-v2'),
    app_consented_at_ms BIGINT NOT NULL CHECK(app_consented_at_ms BETWEEN 1 AND 9007199254740991),
    created_at_ms BIGINT NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991),
    expires_at_ms BIGINT NOT NULL CHECK(expires_at_ms BETWEEN 1 AND 9007199254740991),
    landing_owner_hash BYTEA CHECK(octet_length(landing_owner_hash)=32),
    authorize_request_id UUID CHECK(substring(authorize_request_id::text,15,1)='4' AND substring(authorize_request_id::text,20,1) IN ('8','9','a','b')),
    authorize_request_digest BYTEA CHECK(octet_length(authorize_request_digest)=32),
    redeemed_at_ms BIGINT CHECK(redeemed_at_ms BETWEEN 1 AND 9007199254740991),
    cancelled_reason TEXT CHECK(cancelled_reason IN ('withdrawn','gpc','denied','generation_retired','expired','owner_closed')),
    updated_at_ms BIGINT NOT NULL CHECK(updated_at_ms BETWEEN 1 AND 9007199254740991),
    PRIMARY KEY(transaction_hash),
    UNIQUE(generation,transaction_hash),
    UNIQUE(generation,app_owner_hash,begin_request_id),
    UNIQUE(generation,authorize_request_id),
    FOREIGN KEY(generation,app_owner_hash) REFERENCES x_capi_handoff_app_owners(generation,app_owner_hash),
    FOREIGN KEY(generation,landing_owner_hash) REFERENCES x_capi_handoff_sources(generation,landing_owner_hash),
    CHECK(expires_at_ms>created_at_ms AND expires_at_ms<=created_at_ms+300000),
    CHECK(app_consented_at_ms<=created_at_ms),
    CHECK((authorize_request_id IS NULL)=(authorize_request_digest IS NULL)),
    CHECK(state NOT IN ('source_approved','redeemed') OR (landing_owner_hash IS NOT NULL AND authorize_request_id IS NOT NULL)),
    CHECK((state='redeemed')=(redeemed_at_ms IS NOT NULL)),
    FOREIGN KEY(generation,policy_epoch) REFERENCES x_capi_handoff_generations(generation,policy_epoch)
);

CREATE TABLE x_capi_handoff_imports (
    context_id UUID PRIMARY KEY REFERENCES x_capi_attribution_contexts(id),
    ownership_id UUID NOT NULL UNIQUE,
    context_handle_hash CHAR(64) NOT NULL UNIQUE CHECK(context_handle_hash ~ '^[0-9a-f]{64}$'),
    transaction_hash BYTEA NOT NULL CHECK(octet_length(transaction_hash)=32),
    generation UUID NOT NULL,
    policy_epoch BIGINT NOT NULL CHECK(policy_epoch BETWEEN 1 AND 2147483647),
    landing_owner_hash BYTEA NOT NULL CHECK(octet_length(landing_owner_hash)=32),
    app_owner_hash BYTEA NOT NULL CHECK(octet_length(app_owner_hash)=32),
    observed_at_ms BIGINT NOT NULL CHECK(observed_at_ms BETWEEN 1 AND 9007199254740991),
    landing_consented_at_ms BIGINT NOT NULL CHECK(landing_consented_at_ms BETWEEN 1 AND 9007199254740991),
    app_consented_at_ms BIGINT NOT NULL CHECK(app_consented_at_ms BETWEEN 1 AND 9007199254740991),
    import_cutoff_ms BIGINT NOT NULL CHECK(import_cutoff_ms BETWEEN 1 AND 9007199254740991),
    original_expires_at_ms BIGINT NOT NULL CHECK(original_expires_at_ms BETWEEN 1 AND 9007199254740991),
    effective_expires_at_ms BIGINT NOT NULL CHECK(effective_expires_at_ms BETWEEN 1 AND 9007199254740991),
    binding_cutoff_ms BIGINT CHECK(binding_cutoff_ms BETWEEN 1 AND 9007199254740991),
    state TEXT NOT NULL CHECK(state IN ('active','closed','expired')),
    context_ciphertext TEXT CHECK(octet_length(context_ciphertext) BETWEEN 1 AND 2048 AND context_ciphertext ~ '^[A-Za-z0-9_=-]+$'),
    result_recover_until_ms BIGINT NOT NULL CHECK(result_recover_until_ms BETWEEN 1 AND 9007199254740991),
    created_at_ms BIGINT NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991),
    updated_at_ms BIGINT NOT NULL CHECK(updated_at_ms BETWEEN 1 AND 9007199254740991),
    UNIQUE(transaction_hash),
    UNIQUE(generation,landing_owner_hash),
    UNIQUE(generation,app_owner_hash),
    FOREIGN KEY(generation,transaction_hash) REFERENCES x_capi_handoff_transactions(generation,transaction_hash),
    FOREIGN KEY(generation,landing_owner_hash) REFERENCES x_capi_handoff_sources(generation,landing_owner_hash),
    FOREIGN KEY(generation,app_owner_hash) REFERENCES x_capi_handoff_app_owners(generation,app_owner_hash),
    CHECK(observed_at_ms<=landing_consented_at_ms AND landing_consented_at_ms<=import_cutoff_ms AND app_consented_at_ms<=import_cutoff_ms AND import_cutoff_ms<effective_expires_at_ms AND effective_expires_at_ms<=original_expires_at_ms),
    CHECK(binding_cutoff_ms IS NULL OR binding_cutoff_ms>=import_cutoff_ms),
    CHECK(result_recover_until_ms<=effective_expires_at_ms),
    CHECK(state='active' OR context_ciphertext IS NULL),
    FOREIGN KEY(generation,policy_epoch) REFERENCES x_capi_handoff_generations(generation,policy_epoch)
);
CREATE UNIQUE INDEX x_capi_handoff_one_active_generation_idx ON x_capi_handoff_generations((state)) WHERE state='active';
ALTER TABLE x_capi_handoff_sources ADD CONSTRAINT x_capi_handoff_assigned_transaction_fk FOREIGN KEY(generation,assigned_transaction_hash) REFERENCES x_capi_handoff_transactions(generation,transaction_hash);
CREATE INDEX x_capi_handoff_source_expiry_idx ON x_capi_handoff_sources(original_expires_at_ms) WHERE twclid_ciphertext IS NOT NULL;
CREATE INDEX x_capi_handoff_tx_expiry_idx ON x_capi_handoff_transactions(expires_at_ms) WHERE state IN ('app_granted','source_approved');
CREATE INDEX x_capi_handoff_import_recovery_idx ON x_capi_handoff_imports(result_recover_until_ms) WHERE context_ciphertext IS NOT NULL;

-- PostgreSQL foreign-key enforcement executes as the NOLOGIN table owner.
-- Explicit schema USAGE is required when PUBLIC schema access is removed.
SELECT set_config('axonos_handoff.owner_role', :'owner_role', TRUE);
DO $owner_schema_usage$
BEGIN
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I',current_schema(),
                   current_setting('axonos_handoff.owner_role'));
END
$owner_schema_usage$;
ALTER TABLE x_capi_handoff_generations OWNER TO :"owner_role";
REVOKE ALL ON x_capi_handoff_generations FROM PUBLIC, :"worker_role";
GRANT SELECT, INSERT, UPDATE ON x_capi_handoff_generations TO :"worker_role";
ALTER TABLE x_capi_handoff_sources OWNER TO :"owner_role";
REVOKE ALL ON x_capi_handoff_sources FROM PUBLIC, :"worker_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON x_capi_handoff_sources TO :"worker_role";
ALTER TABLE x_capi_handoff_app_owners OWNER TO :"owner_role";
REVOKE ALL ON x_capi_handoff_app_owners FROM PUBLIC, :"worker_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON x_capi_handoff_app_owners TO :"worker_role";
ALTER TABLE x_capi_handoff_transactions OWNER TO :"owner_role";
REVOKE ALL ON x_capi_handoff_transactions FROM PUBLIC, :"worker_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON x_capi_handoff_transactions TO :"worker_role";
ALTER TABLE x_capi_handoff_imports OWNER TO :"owner_role";
REVOKE ALL ON x_capi_handoff_imports FROM PUBLIC, :"worker_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON x_capi_handoff_imports TO :"worker_role";
UPDATE x_capi_schema_meta SET schema_version=5, updated_at=extract(epoch FROM clock_timestamp()) WHERE singleton=TRUE;
\ir 008_x_capi_v5_postflight.sql
COMMIT;
