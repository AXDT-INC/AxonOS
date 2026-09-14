#!/bin/sh
# Fail-fast, explicit migration entry point. Authentication comes from normal
# libpq mechanisms (PGHOST/PGDATABASE/PGSERVICE/PGPASSFILE), never argv.
set -eu

# Initialization must never wait indefinitely for DNS/TCP, a DDL lock, a
# statement, an abandoned transaction, or an interactive password prompt.
# These are fixed safety ceilings rather than operator-tunable feature knobs.
PGCONNECT_TIMEOUT=5
PGOPTIONS='-c search_path=public -c statement_timeout=30000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=30000'
export PGCONNECT_TIMEOUT PGOPTIONS

validate_identifier() {
    identifier=$1
    kind=$2
    if [ "${#identifier}" -lt 1 ] || [ "${#identifier}" -gt 63 ]; then
        echo "$kind must be a canonical PostgreSQL identifier" >&2
        exit 64
    fi
    case "$identifier" in
        [A-Za-z_]* ) ;;
        * )
            echo "$kind must start with a letter or underscore" >&2
            exit 64
            ;;
    esac
    case "$identifier" in
        *[!A-Za-z0-9_]*)
            echo "$kind must contain only letters, digits, and underscores" >&2
            exit 64
            ;;
    esac
    identifier_lower=$(printf '%s' "$identifier" | tr 'A-Z' 'a-z')
    case "$identifier_lower" in
        pg_*) echo "reserved PostgreSQL role name" >&2; exit 64 ;;
    esac
}

if [ "$#" -ne 2 ]; then
    echo "usage: $0 OWNER_ROLE WORKER_ROLE" >&2
    exit 64
fi

for role in "$@"; do
    validate_identifier "$role" "role name"
done
[ "$1" != "$2" ] || { echo "owner and worker roles must differ" >&2; exit 64; }

migration_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Resolve the actual libpq target, then prove it is the dedicated X CAPI
# cluster before even the role/ACL preflight.  Without this check, a DBA could
# accidentally run a fresh 001 install in the core AxonOS database: 001 quite
# correctly rejects only colliding x_capi_* names and is not itself a dedicated
# database attestation.
bootstrap_role=$(psql -X -w -v ON_ERROR_STOP=1 -Atq -c 'SELECT current_user')
database_name=$(psql -X -w -v ON_ERROR_STOP=1 -Atq -c 'SELECT current_database()')
validate_identifier "$bootstrap_role" "connected PostgreSQL role"
validate_identifier "$database_name" "connected PostgreSQL database"
[ "$1" != "$bootstrap_role" ] \
    && [ "$2" != "$bootstrap_role" ] \
    && [ "$database_name" != "$1" ] \
    && [ "$database_name" != "$2" ] \
    && [ "$database_name" != "$bootstrap_role" ] \
    || { echo "dedicated database identities must be distinct" >&2; exit 64; }
psql -X -w -q -o /dev/null -v ON_ERROR_STOP=1 \
    -v bootstrap_role="$bootstrap_role" -v database_name="$database_name" \
    -v owner_role="$1" -v worker_role="$2" \
    -f "$migration_dir/000_x_capi_bootstrap_target_preflight.sql"

psql -X -w -v ON_ERROR_STOP=1 \
    -v owner_role="$1" -v worker_role="$2" \
    -f "$migration_dir/000_x_capi_roles_preflight.sql"
schema_present=$(psql -X -w -v ON_ERROR_STOP=1 -Atq <<'SQL'
BEGIN READ ONLY;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
SET LOCAL search_path = public;
SELECT pg_catalog.to_regclass('public.x_capi_schema_meta') IS NOT NULL;
ROLLBACK;
SQL
)
if [ "$schema_present" = f ]; then
    schema_version=0
elif [ "$schema_present" = t ]; then
    schema_version=$(psql -X -w -v ON_ERROR_STOP=1 -Atq <<'SQL'
BEGIN READ ONLY;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
SET LOCAL search_path = public;
SELECT COALESCE((
    SELECT schema_version FROM public.x_capi_schema_meta
     WHERE singleton=TRUE
),-1);
ROLLBACK;
SQL
    )
else
    echo "invalid X CAPI schema-presence probe result" >&2
    exit 65
fi
if [ "$schema_version" = "0" ]; then
    psql -X -w -v ON_ERROR_STOP=1 -f "$migration_dir/001_x_capi_outbox.sql"
elif [ "$schema_version" = "4" ]; then
    : # Exact role/ACL postflight below remains mandatory on every start.
else
    echo "unsupported existing X CAPI schema; provision a fresh dedicated database" >&2
    exit 65
fi
psql -X -w -v ON_ERROR_STOP=1 \
    -v owner_role="$1" -v worker_role="$2" \
    -f "$migration_dir/004_x_capi_roles_and_grants.sql"
