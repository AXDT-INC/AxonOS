"""Explicitly opted-in real bootstrap tests on newly created Docker resources.

No Compose stack, existing database, host secret, published port, or persistent
volume is used. BOOTSTRAP may be replaced in memory by a test driver for a
pre-fix control; production configuration has no corresponding override.
"""

import os
import re
import secrets
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "docker/x-capi-worker/bootstrap_x_capi_db.sh"
LABEL = "axonos.test.bootstrap-run"
DATABASE = "synthetic_capi"
BOOTSTRAP_ROLE = "synthetic_bootstrap"
OWNER = "synthetic_owner"
WORKER = "synthetic_worker"

_WAIT_FOR_SECRET = """
count=0
while [ ! -f /run/secrets/.ready ]; do
    count=$((count+1))
    [ "$count" -lt 200 ] || exit 90
    sleep 0.1
done
"""
_PROVISION_SECRETS = """
set -eu
umask 077
IFS= read -r bootstrap_password
IFS= read -r worker_password
printf '%s' "$bootstrap_password" > /run/secrets/x_capi_postgres_bootstrap_password
printf '%s' "$worker_password" > /run/secrets/x_capi_postgres_worker_password
chmod 600 /run/secrets/x_capi_postgres_bootstrap_password /run/secrets/x_capi_postgres_worker_password
unset bootstrap_password worker_password
touch /run/secrets/.ready
"""
_PSQL_PROBE = """#!/bin/sh
set -eu
[ "${PGPASSWORD+x}" != x ] || exit 91
mode=$(stat -c '%a' "$PGPASSFILE")
[ "$mode" = 600 ] || exit 92
printf 'TEST_DB=%s\\nTEST_PGPASS_MODE=%s\\n' "$PGDATABASE" "$mode" >&2
awk -F: '{printf "TEST_SCOPE=%s:%s:%s:%s\\n",$1,$2,$3,$4}' "$PGPASSFILE" >&2
exec /usr/local/bin/psql "$@"
"""
_ORIGINAL_PASSFILE_BLOCK = """printf '%s:%s:%s:%s:%s\\n' \\
    x-capi-postgres 5432 "$database_name" "$bootstrap_role" \\
    "$escaped_bootstrap" > "$pgpass_file"
"""

# Explicit columns exclude both PostgreSQL password verifiers and view-level
# password placeholders. The second bootstrap may legitimately regenerate the
# worker's SCRAM verifier; successful TCP login proves the password still works.
_CLUSTER_SNAPSHOT_SQL = """
BEGIN READ ONLY;
SELECT jsonb_build_object(
 'roles', (SELECT jsonb_agg(to_jsonb(r) ORDER BY rolname) FROM (
   SELECT oid,rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,
          rolcanlogin,rolreplication,rolconnlimit,rolvaliduntil,
          rolbypassrls,rolconfig,shobj_description(oid,'pg_authid') AS comment
     FROM pg_roles) r),
 'memberships', (SELECT jsonb_agg(to_jsonb(m) ORDER BY roleid,member)
                   FROM pg_auth_members m),
 'databases', (SELECT jsonb_agg(to_jsonb(d) ORDER BY datname) FROM (
   SELECT oid,datname,datdba,encoding,datcollate,datctype,datistemplate,
          datallowconn,datconnlimit,dattablespace,datacl,
          shobj_description(oid,'pg_database') AS comment FROM pg_database) d),
 'settings', (SELECT jsonb_agg(to_jsonb(s) ORDER BY setdatabase,setrole)
                FROM pg_db_role_setting s)
);
ROLLBACK;
"""


class BootstrapPassfileScopeTests(unittest.TestCase):
    def test_real_shell_passfile_has_only_three_exact_database_scopes(self):
        source = BOOTSTRAP.read_text(encoding="utf-8")
        fragment = source.split("pgpass_file=", 1)[1].split("export PGHOST=", 1)[0]
        fragment = "pgpass_file=" + fragment
        with tempfile.TemporaryDirectory(prefix="xcapi-pass-scope-") as runtime:
            passfile = Path(runtime) / "bootstrap.pgpass"
            fragment = fragment.replace(
                "pgpass_file=/tmp/x-capi-bootstrap.pgpass",
                "pgpass_file=" + shlex.quote(str(passfile)), 1,
            )
            password = "synthetic:" + secrets.token_hex(12) + "\\escaped"
            program = (
                "set -eu\numask 077\n"
                f"database_name={DATABASE}\nbootstrap_role={BOOTSTRAP_ROLE}\n"
                "bootstrap_password=" + shlex.quote(password) + "\n" + fragment +
                "stat -c 'MODE=%a' \"$pgpass_file\"\n"
                "awk -F: '{print $1 \":\" $2 \":\" $3 \":\" $4}' \"$pgpass_file\"\n"
            )
            result = subprocess.run(
                ["/bin/sh"], input=program, text=True, capture_output=True,
                timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.splitlines(), ["MODE=600"] + [
                f"x-capi-postgres:5432:{database}:{BOOTSTRAP_ROLE}"
                for database in (DATABASE, "postgres", "template1")
            ])
            self.assertFalse(passfile.exists(), "passfile survived shell exit")


@unittest.skipUnless(
    os.getenv("X_CAPI_RUN_BOOTSTRAP_DOCKER_TESTS") == "1",
    "explicit disposable Docker bootstrap opt-in is required",
)
class BootstrapDockerIntegrationTests(unittest.TestCase):
    def observe_bootstrap(self, phase, report):
        """Test-driver hook containing sanitized diagnostics, never raw logs."""
        if not hasattr(self, "bootstrap_reports"):
            self.bootstrap_reports = {}
        self.bootstrap_reports[phase] = report

    def test_real_bootstrap_authenticates_every_database_and_rerun_is_idempotent(self):
        overlay = (ROOT / "docker-compose.x-capi.yml").read_text(encoding="utf-8")
        service = overlay.split("  x-capi-postgres:", 1)[1].split("  x-capi-db-init:", 1)[0]
        image = re.search(r"^    image: (\S+)$", service, re.MULTILINE).group(1)
        self.assertRegex(image, r"^postgres:15-alpine@sha256:[a-f0-9]{64}$")
        init_service = overlay.split("  x-capi-db-init:", 1)[1].split("  x-capi-worker:", 1)[0]
        self.assertEqual(re.search(r"^    image: (\S+)$", init_service, re.MULTILINE).group(1), image)
        run_id = secrets.token_hex(10)
        network = "xcapi-bootstrap-" + run_id
        postgres = network + "-postgres"
        containers = []
        # Only the bootstrap password needs pgpass escaping; the runtime worker
        # deliberately rejects backslashes/quotes in its distinct password.
        bootstrap_password = "synthetic:" + secrets.token_hex(20) + "\\escaped"
        worker_password = "synthetic-worker-" + secrets.token_hex(20)
        secret_input = bootstrap_password + "\n" + worker_password + "\n"

        def docker(arguments, *, input_text=None, timeout=30, require_success=True):
            result = subprocess.run(
                ["docker", *arguments], input=input_text, text=True,
                capture_output=True, timeout=timeout, check=False,
            )
            if require_success:
                self.assertEqual(result.returncode, 0,
                                 "disposable Docker operation failed: " + arguments[0])
            return result

        def remove_owned(kind, name):
            template = "{{ index .Config.Labels \"" + LABEL + "\" }}" if kind == "container" else "{{ index .Labels \"" + LABEL + "\" }}"
            result = docker([kind, "inspect", "--format", template, name], require_success=False)
            if result.returncode == 0 and result.stdout.strip() == run_id:
                command = ["rm", "-f", "-v", name] if kind == "container" else ["network", "rm", name]
                docker(command)

        def run_container(name, arguments, program):
            containers.append(name)
            docker([
                "run", "--detach", "--pull=never", "--name", name,
                "--label", LABEL + "=" + run_id, "--network", network,
                "--tmpfs", "/run/secrets:rw,nosuid,nodev,noexec,size=1m,mode=0700",
                "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=16m,mode=1777",
                "--memory", "256m", "--pids-limit", "128",
                "--entrypoint", "/bin/sh", *arguments, image, "-c", program,
            ])

        def provision(name):
            docker(["exec", "-i", name, "/bin/sh", "-c", _PROVISION_SECRETS],
                   input_text=secret_input)

        def snapshot():
            state = {}
            result = docker([
                "exec", "-i", postgres, "psql", "-X", "-w", "-Atq",
                "-v", "ON_ERROR_STOP=1", "-U", BOOTSTRAP_ROLE, "-d", DATABASE,
            ], input_text=_CLUSTER_SNAPSHOT_SQL)
            state["cluster"] = result.stdout
            for database in (DATABASE, "postgres", "template1"):
                result = docker([
                    "exec", postgres, "pg_dump", "-w", "-U", BOOTSTRAP_ROLE,
                    "-d", database,
                ])
                # Newer PG15 patch releases randomize psql safety guards. Only
                # these session tokens vary; retain all schema/ACL/data output.
                state[database] = "\n".join(
                    line for line in result.stdout.splitlines()
                    if not line.startswith(("\\restrict ", "\\unrestrict "))
                )
            return state

        def assert_state_equal(before, after):
            self.assertEqual(set(before), set(after))
            for scope in before:
                self.assertTrue(before[scope] == after[scope],
                                f"bootstrap unexpectedly changed {scope} state")

        def run_bootstrap(suffix, *, script=BOOTSTRAP, expected_status=0):
            name = network + "-" + suffix
            program = _WAIT_FOR_SECRET + """
PATH=/tmp/psql-probe:$PATH
export PATH
/bin/sh /opt/axonos/bootstrap_x_capi_db.sh
result=$?
if [ -e /tmp/x-capi-bootstrap.pgpass ]; then
    printf 'TEST_PGPASS_REMOVED=0\\n'
    exit 93
fi
printf 'TEST_PGPASS_REMOVED=1\\n'
exit "$result"
"""
            run_container(name, [
                "--env", "POSTGRES_DB=" + DATABASE,
                "--env", "POSTGRES_USER=" + BOOTSTRAP_ROLE,
                "--env", "X_CAPI_OWNER_DB_ROLE=" + OWNER,
                "--env", "X_CAPI_WORKER_DB_ROLE=" + WORKER,
                "--mount", f"type=bind,src={script.resolve()},dst=/opt/axonos/bootstrap_x_capi_db.sh,readonly",
                "--mount", f"type=bind,src={ROOT / 'axonos_gate/migrations'},dst=/opt/axonos/x-capi-migrations,readonly",
            ], program)
            docker(["exec", "-i", name, "/bin/sh", "-c",
                    "umask 077; mkdir /tmp/psql-probe; "
                    "dd of=/tmp/psql-probe/psql 2>/dev/null; chmod 700 /tmp/psql-probe/psql"],
                   input_text=_PSQL_PROBE)
            provision(name)
            status = int(docker(["wait", name], timeout=60).stdout.strip())
            result = docker(["logs", name])
            output = result.stdout + result.stderr
            self.assertFalse(any(secret in output for secret in (bootstrap_password, worker_password)),
                             "synthetic credential appeared in bootstrap output")
            self.assertIn("TEST_PGPASS_REMOVED=1", output)
            databases = re.findall(r"^TEST_DB=(\S+)$", output, re.MULTILINE)
            error_line = re.search(r"ERROR:\s+([^\r\n]{1,200})", output)
            reason = error_line.group(1) if error_line else "details suppressed"
            for secret in (bootstrap_password, worker_password):
                reason = reason.replace(secret, "[redacted]")
            if "fe_sendauth: no password supplied" in output:
                reason = "fe_sendauth: no password supplied"
            context = [line[:200] for line in output.splitlines()
                       if line.startswith(("CONTEXT:", "PL/pgSQL function"))]
            self.observe_bootstrap(suffix, {
                "exit_code": status, "databases": databases,
                "error": reason if status else None, "context": context,
                "passfile_removed": True,
            })
            self.assertEqual(status, expected_status,
                             f"bootstrap exit {status}; databases={databases[:3]}; {reason}; {context}")
            expected_databases = [DATABASE, "postgres", "template1"]
            scope_databases = expected_databases
            if expected_status == 2:
                self.assertEqual(reason, "fe_sendauth: no password supplied")
                self.assertEqual(databases, [DATABASE, "postgres"])
                scope_databases = [DATABASE]
            else:
                self.assertEqual(databases[:3], expected_databases)
                self.assertEqual(set(databases), set(expected_databases))
            self.assertEqual(set(re.findall(r"^TEST_SCOPE=(\S+)$", output, re.MULTILINE)), {
                f"x-capi-postgres:5432:{database}:{BOOTSTRAP_ROLE}"
                for database in scope_databases
            })
            self.assertEqual(set(re.findall(r"^TEST_PGPASS_MODE=(\S+)$", output, re.MULTILINE)), {"600"})

        try:
            docker(["network", "create", "--internal", "--label", LABEL + "=" + run_id, network])
            run_container(postgres, [
                "--network-alias", "x-capi-postgres",
                "--tmpfs", "/var/lib/postgresql/data:rw,size=256m,mode=0700",
                "--env", "POSTGRES_DB=" + DATABASE,
                "--env", "POSTGRES_USER=" + BOOTSTRAP_ROLE,
                "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/x_capi_postgres_bootstrap_password",
                "--env", "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
            ], _WAIT_FOR_SECRET + """
exec /usr/local/bin/docker-entrypoint.sh postgres \
    -c log_error_verbosity=terse -c log_min_error_statement=panic \
    -c log_parameter_max_length_on_error=0 -c log_statement=none
""")
            provision(postgres)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                ready = docker(["exec", postgres, "pg_isready", "-h", "x-capi-postgres",
                                "-t", "1", "-U", BOOTSTRAP_ROLE, "-d", DATABASE],
                               require_success=False)
                if ready.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                self.fail("disposable PostgreSQL did not become ready")
            # Restore only the original broken password-file block in a test
            # script. Execute real psql/SQL over SCRAM, and prove this failed
            # administrative connection precedes every persistent mutation.
            source = BOOTSTRAP.read_text(encoding="utf-8")
            start = source.index('for password_database in "$database_name" postgres template1; do\n')
            end = source.index('chmod 600 "$pgpass_file"', start)
            old_source = source[:start] + _ORIGINAL_PASSFILE_BLOCK + source[end:]
            initial_state = snapshot()
            with tempfile.TemporaryDirectory(prefix="xcapi-bootstrap-old-passfile-") as runtime:
                old_script = Path(runtime) / "bootstrap_x_capi_db.sh"
                old_script.write_text(old_source, encoding="utf-8")
                run_bootstrap("old-passfile", script=old_script, expected_status=2)
            assert_state_equal(initial_state, snapshot())
            run_bootstrap("fresh")
            docker([
                "exec", "-i", postgres, "psql", "-X", "-w", "-Atq",
                "-v", "ON_ERROR_STOP=1", "-U", BOOTSTRAP_ROLE, "-d", DATABASE,
            ], input_text="INSERT INTO public.x_capi_counters(reason,count,updated_at) "
                          "VALUES ('synthetic_bootstrap_sentinel',7,1);\n")
            # A genuine TCP/SCRAM worker login proves the final password was
            # activated, while its metadata query checks the finite DB boundary.
            probe = """
set -eu
umask 077
IFS= read -r worker_password
trap 'rm -f /tmp/synthetic-worker.pgpass' EXIT HUP INT TERM
printf 'x-capi-postgres:5432:synthetic_capi:synthetic_worker:%s\\n' "$worker_password" > /tmp/synthetic-worker.pgpass
unset worker_password
export PGPASSFILE=/tmp/synthetic-worker.pgpass
exec_psql=/usr/local/bin/psql
"$exec_psql" -X -w -Atq -h x-capi-postgres -p 5432 -U synthetic_worker -d synthetic_capi -c "
SELECT schema_version FROM public.x_capi_schema_meta WHERE singleton;
SELECT current_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,
       has_database_privilege(current_user,'postgres','CONNECT'),
       has_database_privilege(current_user,'template1','CONNECT'),
       has_schema_privilege(current_user,'public','CREATE'),
       has_column_privilege(current_user,'public.x_capi_outbox','twclid','SELECT'),
       has_column_privilege(current_user,'public.x_capi_outbox','source_key_hash','SELECT'),
       has_column_privilege(current_user,'public.x_capi_outbox','twclid','SELECT WITH GRANT OPTION')
  FROM pg_roles WHERE rolname=current_user;
SELECT rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication
  FROM pg_roles WHERE rolname='synthetic_owner';
WITH acl_rows AS (
    SELECT c.oid AS relation_oid,0::smallint AS column_number,acl.*
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      CROSS JOIN LATERAL aclexplode(c.relacl) acl
     WHERE n.nspname='public' AND c.relkind='r'
    UNION ALL
    SELECT c.oid,a.attnum,acl.*
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      JOIN pg_attribute a ON a.attrelid=c.oid
      CROSS JOIN LATERAL aclexplode(a.attacl) acl
     WHERE n.nspname='public' AND c.relkind='r'
       AND a.attnum>0 AND NOT a.attisdropped
)
SELECT count(*)=count(DISTINCT (
    relation_oid,column_number,grantor,grantee,privilege_type,is_grantable
)) FROM acl_rows;"
"""
            def assert_worker_login():
                result = docker(["exec", "-i", postgres, "/bin/sh", "-c", probe],
                                input_text=worker_password + "\n")
                self.assertEqual(result.stdout.splitlines(), [
                    "4", WORKER + "|f|f|f|f|f|f|f|t|f|f",
                    OWNER + "|f|f|f|f|f|f", "t",
                ])

            assert_worker_login()
            fresh_state = snapshot()
            run_bootstrap("rerun")
            assert_worker_login()
            assert_state_equal(fresh_state, snapshot())
        finally:
            for container in reversed(containers):
                remove_owned("container", container)
            remove_owned("network", network)


if __name__ == "__main__":
    unittest.main()
