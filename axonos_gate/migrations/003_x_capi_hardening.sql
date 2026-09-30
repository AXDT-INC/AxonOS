-- Retired compatibility entry point.
--
-- No pre-v4 schema is accepted for in-place mutation. Earlier revisions of
-- this file attempted to recognize a legacy layout and then erase identifier
-- rows while reshaping it. A catalog-level proof of every possible deployed
-- v1 object was never established, so retaining that DDL would make a manual
-- invocation needlessly destructive and misleading. The supported procedure
-- is backup-for-audit, provision a fresh dedicated database with 001, validate
-- it with 004, and then cut over. This transaction deliberately changes
-- nothing, including when invoked against an empty or exact-v4 database.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
-- pg_catalog is implicitly searched before public when it is not listed.
SET LOCAL search_path = public;

DO $retired$
BEGIN
    RAISE EXCEPTION
        '003 in-place X CAPI upgrade is retired; provision a fresh dedicated v4 database';
END
$retired$;

-- Unreachable by design; retained so an interactive client cannot mistake the
-- open transaction for a migration that may be committed after ignoring the
-- exception.
ROLLBACK;
