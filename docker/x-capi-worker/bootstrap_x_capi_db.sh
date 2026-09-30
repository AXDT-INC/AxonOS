#!/bin/sh
# Bootstrap only the dedicated CAPI database. The bootstrap credential and
# migration authority are never mounted into the runtime worker container.
set -eu
umask 077

fail() {
    echo "$1" >&2
    exit 64
}

read_secret() {
    secret_path=$1
    [ -f "$secret_path" ] && [ ! -L "$secret_path" ] \
        || fail "dedicated database secret must be a regular non-symlink"
    set -- $(stat -c '%u %a %h %s' "$secret_path")
    [ "$1" = 0 ] && { [ "$2" = 400 ] || [ "$2" = 600 ]; } \
        && [ "$3" = 1 ] && [ "$4" -ge 16 ] && [ "$4" -le 8192 ] \
        || fail "unsafe dedicated database secret metadata"
    non_visible=$(LC_ALL=C tr -d '\041-\176' < "$secret_path" | wc -c)
    [ "$non_visible" -eq 0 ] \
        || fail "dedicated database secret must be visible ASCII"
    secret_value=$(dd if="$secret_path" bs=8193 count=1 2>/dev/null)
    [ "${#secret_value}" -ge 16 ] && [ "${#secret_value}" -le 8192 ] \
        || fail "invalid dedicated database secret length"
    printf '%s' "$secret_value"
}

validate_identifier() {
    identifier=$1
    [ "${#identifier}" -ge 1 ] && [ "${#identifier}" -le 63 ] \
        || fail "invalid dedicated database identifier length"
    case "$identifier" in
        [A-Za-z_]* ) ;;
        * ) fail "invalid dedicated database identifier start" ;;
    esac
    case "$identifier" in
        *[!A-Za-z0-9_]* ) fail "invalid dedicated database identifier" ;;
    esac
    identifier_lower=$(printf '%s' "$identifier" | tr 'A-Z' 'a-z')
    case "$identifier_lower" in
        pg_* ) fail "reserved PostgreSQL role/database identifier" ;;
    esac
}

bootstrap_password=$(read_secret /run/secrets/x_capi_postgres_bootstrap_password)
worker_password=$(read_secret /run/secrets/x_capi_postgres_worker_password)
case "$worker_password" in
    *"'"*|*"\\"*) fail "worker password contains unsupported quoting bytes" ;;
esac
owner_role=${X_CAPI_OWNER_DB_ROLE:-axonos_x_capi_owner}
worker_role=${X_CAPI_WORKER_DB_ROLE:-axonos_x_capi_worker}
database_name=${POSTGRES_DB:-axonos_x_capi}
bootstrap_role=${POSTGRES_USER:-x_capi_bootstrap}

for identifier in "$owner_role" "$worker_role" "$database_name" "$bootstrap_role"; do
    validate_identifier "$identifier"
done
[ "$owner_role" != "$worker_role" ] \
    && [ "$owner_role" != "$bootstrap_role" ] \
    && [ "$worker_role" != "$bootstrap_role" ] \
    && [ "$database_name" != "$owner_role" ] \
    && [ "$database_name" != "$worker_role" ] \
    && [ "$database_name" != "$bootstrap_role" ] \
    || fail "dedicated database identities must be distinct"

pgpass_file=/tmp/x-capi-bootstrap.pgpass
cleanup() {
    rm -f "$pgpass_file"
}
trap cleanup EXIT HUP INT TERM
escaped_bootstrap=$(printf '%s' "$bootstrap_password" | sed 's/\\/\\\\/g; s/:/\\:/g')
# libpq matches the database as well as host, port, and user. Administrative
# preflights below connect to postgres/template1 before any persistent changes.
# Keep credentials scoped to exactly those databases, never a wildcard.
for password_database in "$database_name" postgres template1; do
    printf '%s:%s:%s:%s:%s\n' \
        x-capi-postgres 5432 "$password_database" "$bootstrap_role" \
        "$escaped_bootstrap"
done > "$pgpass_file"
chmod 600 "$pgpass_file"
unset bootstrap_password escaped_bootstrap

export PGHOST=x-capi-postgres PGPORT=5432 PGDATABASE=$database_name
export PGUSER=$bootstrap_role PGPASSFILE=$pgpass_file
PGCONNECT_TIMEOUT=5
PGOPTIONS='-c search_path=public -c statement_timeout=30000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=30000'
export PGCONNECT_TIMEOUT PGOPTIONS

migration_dir=/opt/axonos/x-capi-migrations

# Prove the connection, cluster, schema namespace, and any pre-existing roles
# belong to this dedicated service before the first persistent mutation.
psql -X -w -q -o /dev/null -v ON_ERROR_STOP=1 \
    -v bootstrap_role="$bootstrap_role" -v database_name="$database_name" \
    -v owner_role="$owner_role" -v worker_role="$worker_role" \
    -f "$migration_dir/000_x_capi_bootstrap_target_preflight.sql"
for administrative_database in postgres template1; do
    PGDATABASE=$administrative_database \
        psql -X -w -q -o /dev/null -v ON_ERROR_STOP=1 \
        -v bootstrap_role="$bootstrap_role" \
        -f "$migration_dir/000_x_capi_admin_database_preflight.sql"
done

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
    fail "invalid X CAPI schema-presence probe result"
fi

# Execute only the read-only prefix of the reviewed migration.  Requiring one
# exact stop marker means a renamed/truncated file fails closed instead of ever
# falling through into DDL.  The prefix opens a transaction; append ROLLBACK so
# this attestation cannot persist even temporary state.
run_migration_preflight() {
    migration_file=$1
    stop_marker=$2
    marker_count=$(grep -Fxc "$stop_marker" "$migration_file") || marker_count=0
    [ "$marker_count" -eq 1 ] \
        || fail "invalid X CAPI schema preflight migration marker"
    {
        awk -v stop="$stop_marker" '{ print; if ($0 == stop) exit }' \
            "$migration_file"
        printf '%s\n' 'ROLLBACK;'
    } | psql -X -w -q -o /dev/null -v ON_ERROR_STOP=1
}

if [ "$schema_version" = 0 ]; then
    run_migration_preflight \
        "$migration_dir/001_x_capi_outbox.sql" '$preflight$;'
elif [ "$schema_version" = 4 ]; then
    run_migration_preflight \
        "$migration_dir/004_x_capi_roles_and_grants.sql" \
        '$catalog_preflight$;'
else
    fail "unsupported existing X CAPI schema; provision a fresh dedicated database"
fi

# Provision identities and the finite database boundary first.  A newly created
# LOGIN role has no password and cannot authenticate; an existing worker keeps
# its old password until every schema/ACL postflight succeeds below.
psql -X -w -q -v ON_ERROR_STOP=1 \
    -v owner_role="$owner_role" -v worker_role="$worker_role" \
    -v database_name="$database_name" <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '30s';
CREATE TEMPORARY TABLE x_capi_bootstrap_roles(
    role_kind TEXT PRIMARY KEY,
    role_name NAME NOT NULL
) ON COMMIT DROP;
INSERT INTO x_capi_bootstrap_roles(role_kind,role_name) VALUES
    ('owner', :'owner_role'), ('worker', :'worker_role');
DO $roles$
DECLARE
    requested_role NAME;
    role_oid OID;
    role_comment TEXT;
    unsafe BOOLEAN;
BEGIN
    SELECT role_name INTO requested_role FROM x_capi_bootstrap_roles
     WHERE role_kind='owner';
    SELECT oid, shobj_description(oid,'pg_authid'),
           rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole
           OR rolreplication OR rolcanlogin
      INTO role_oid, role_comment, unsafe
      FROM pg_roles WHERE rolname=requested_role;
    IF role_oid IS NULL THEN
        EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS '
                       'NOCREATEDB NOCREATEROLE NOREPLICATION', requested_role);
        EXECUTE format('COMMENT ON ROLE %I IS %L', requested_role,
                       'AxonOS X CAPI owner role v1');
    ELSIF role_comment IS DISTINCT FROM 'AxonOS X CAPI owner role v1'
          OR unsafe THEN
        RAISE EXCEPTION 'refusing unproven existing owner role';
    END IF;

    SELECT role_name INTO requested_role FROM x_capi_bootstrap_roles
     WHERE role_kind='worker';
    SELECT oid, shobj_description(oid,'pg_authid'),
           rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole
           OR rolreplication OR NOT rolcanlogin
      INTO role_oid, role_comment, unsafe
      FROM pg_roles WHERE rolname=requested_role;
    IF role_oid IS NULL THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS '
                       'NOCREATEDB NOCREATEROLE NOREPLICATION', requested_role);
        EXECUTE format('COMMENT ON ROLE %I IS %L', requested_role,
                       'AxonOS X CAPI worker role v1');
    ELSIF role_comment IS DISTINCT FROM 'AxonOS X CAPI worker role v1'
          OR unsafe THEN
        RAISE EXCEPTION 'refusing unproven existing worker role';
    END IF;
END
$roles$;
COMMENT ON DATABASE :"database_name" IS
    'AxonOS dedicated X CAPI database v1';
-- Normalize only the three databases admitted by the dedicated-cluster
-- preflight.  Do not sweep an open-ended pg_database result set.
REVOKE ALL PRIVILEGES ON DATABASE :"database_name" FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres, template1 FROM PUBLIC;
REVOKE ALL PRIVILEGES ON DATABASE :"database_name", postgres, template1
    FROM :"worker_role";
GRANT CONNECT ON DATABASE :"database_name" TO :"worker_role";
COMMIT;
SQL

/bin/sh "$migration_dir/apply_x_capi_migrations.sh" \
    "$owner_role" "$worker_role"

# Activate/rotate the worker credential only after all migration postflights.
# It is supplied over an anonymous pipe, never process argv or the environment,
# and is psql-escaped only after the strict visible-ASCII check above.
escaped_worker=$(printf '%s' "$worker_password" | sed "s/'/''/g")
{
    printf "\\set worker_password '%s'\n" "$escaped_worker"
    cat <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
SET LOCAL idle_in_transaction_session_timeout = '15s';
ALTER ROLE :"worker_role" PASSWORD :'worker_password';
COMMIT;
SQL
} | psql -X -w -q -v ON_ERROR_STOP=1 -v worker_role="$worker_role"
unset worker_password escaped_worker
