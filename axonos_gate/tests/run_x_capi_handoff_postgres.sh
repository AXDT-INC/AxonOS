#!/bin/sh
# Creates only a new disposable /tmp PostgreSQL 15 cluster, using synthetic roles.
# No network listen socket, downloaded software, deployment DSN, or secrets.
set -eu
umask 077
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
pg_bin=${PG15_BIN:-/usr/lib/postgresql/15/bin}
for program in postgres initdb pg_ctl psql; do
    [ -x "$pg_bin/$program" ] || { echo 'Set PG15_BIN to an existing PostgreSQL 15 bin directory' >&2; exit 64; }
done
case "$("$pg_bin/postgres" --version)" in
    'postgres (PostgreSQL) 15.'*) ;;
    *) echo 'PostgreSQL 15 is required' >&2; exit 64 ;;
esac
# Read-only dependency check before creating or starting any disposable service.
python3 -c 'import psycopg2, cryptography'
test_dir=$(mktemp -d /tmp/axon-handoff-pg15-XXXXXXXX)
started=false
cleanup() {
    if [ "$started" = true ]; then
        "$pg_bin/pg_ctl" -D "$test_dir/data" -m immediate -w stop >/dev/null 2>&1 || return
    fi
    # test_dir is solely mktemp output, never an environment/deployment path.
    case "$test_dir" in /tmp/axon-handoff-pg15-*) rm -rf -- "$test_dir" ;; esac
}
trap cleanup EXIT HUP INT TERM
mkdir "$test_dir/socket"
"$pg_bin/initdb" -D "$test_dir/data" -U cluadmin -A trust --no-locale --encoding=UTF8 >"$test_dir/init.log"
started=true
"$pg_bin/pg_ctl" -D "$test_dir/data" -l "$test_dir/server.log" \
    -o "-k $test_dir/socket -p 55439 -c listen_addresses=''" -w start >/dev/null
export PGHOST="$test_dir/socket" PGPORT=55439 PGUSER=cluadmin PGDATABASE=postgres
export PATH="$pg_bin:$PATH" PGCONNECT_TIMEOUT=5
# All SQL targets are the just-created socket; no preexisting database is used.
psql -X -w -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE DATABASE capi_handoff_test;
CREATE ROLE handoff_owner NOLOGIN;
CREATE ROLE handoff_worker LOGIN;
COMMENT ON ROLE handoff_owner IS 'AxonOS X CAPI owner role v1';
COMMENT ON ROLE handoff_worker IS 'AxonOS X CAPI worker role v1';
COMMENT ON DATABASE capi_handoff_test IS 'AxonOS dedicated X CAPI database v1';
REVOKE ALL ON DATABASE capi_handoff_test FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres,template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE capi_handoff_test TO handoff_worker;
SQL
export PGDATABASE=capi_handoff_test
psql -X -w -v ON_ERROR_STOP=1 -q -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC'
sh "$repo_dir/axonos_gate/migrations/apply_x_capi_migrations.sh" handoff_owner handoff_worker >"$test_dir/migration.log" 2>&1 || {
    cat "$test_dir/migration.log" >&2; exit 1;
}
# A second invocation must attest schema 5 without running historical v4 grants.
sh "$repo_dir/axonos_gate/migrations/apply_x_capi_migrations.sh" handoff_owner handoff_worker >>"$test_dir/migration.log" 2>&1 || {
    cat "$test_dir/migration.log" >&2; exit 1;
}
export X_HANDOFF_TEST_DSN="host=$test_dir/socket port=55439 dbname=capi_handoff_test user=cluadmin"
export X_HANDOFF_TEST_DISPOSABLE_CONFIRM=DESTROY_TMP_HANDOFF_DATABASE
cd "$repo_dir"
python3 -m unittest axonos_gate.tests.test_x_capi_handoff_store axonos_gate.tests.test_x_capi_handoff_authority_postgres axonos_gate.tests.test_x_capi_handoff_process -v
