# AxonOS X CAPI: First Production Preparation in `off` Mode

## Purpose

This guide records the commands used during the first production-host
preparation of the AxonOS X Conversion API (CAPI) subsystem while
keeping X attribution and delivery disabled:

``` dotenv
X_CAPI_MODE=off
```

It covers the actual manual preparation performed on the AxonOS host,
including internal secret provisioning, Compose validation, image
preparation, isolated service startup, verification, and the first real
bootstrap failure discovered during deployment.

No real X access token, X event identifiers, real `twclid`, or X API
request is required for this procedure.

## Current workflow note — 2026-10-02

This is the historical record of the first production preparation/deployment
and troubleshooting while `X_CAPI_MODE=off`. Its manual Compose commands,
failures, corrections, and observations below record what was actually done;
the current deployment script was not used for those operations. Intermediate
failure checkpoints are followed by recovery and the completed state in §26.

For current routine deployment, follow the [production deployment runbook](PRODUCTION_DEPLOYMENT.md):
run `./scripts/deploy-production.sh --check` from `~/AxonOS`, then, only after
it passes and within the operator maintenance window, run
`./scripts/deploy-production.sh`. The script owns the routine Compose sequence.
That runbook also records the verified 2026-10-02 host baseline, protected
metadata helper, and post-reboot lock provisioning requirements.
Use [X CAPI](X_CAPI.md) for architecture, security, privacy, activation, and
configuration. The historical commands below are not the preferred routine
deployment interface today.

## Repository checkpoint

The CAPI implementation had already been integrated locally into `main`:

``` text
e42f890  feat(capi): integrate privacy-minimized X Conversion API
f18a697  test(capi): verify synthetic browser attribution capture
7307bdb  fix(capi): harden transport and attribution staging
e36629e  feat(capi): add dedicated-token X transport
02ee299  feat(capi): add privacy-minimized X conversion infrastructure
```

The staging branch was also preserved remotely as
`feature/x-capi-staging-ready`.

------------------------------------------------------------------------

## 1. Set CAPI to `off`

Add to the production `.env`:

``` dotenv
X_CAPI_MODE=off
```

Verify without printing the rest of `.env`:

``` bash
cd ~/AxonOS
grep '^X_CAPI_MODE=' .env
```

Observed:

``` text
X_CAPI_MODE=off
```

`off` is the fail-closed/non-collecting mode. X-specific credentials and
event identifiers are not required for this preparation.

------------------------------------------------------------------------

## 2. Create the protected host secret directory

``` bash
sudo install -d -m 0700 -o root -g root /etc/axonos
```

Verify:

``` bash
sudo stat -c '%U %G %a %n' /etc/axonos
```

Observed:

``` text
root root 700 /etc/axonos
```

The directory is outside the Git repository and is used for CAPI
internal secrets.

------------------------------------------------------------------------

## 3. Generate the CAPI context encryption key

``` bash
sudo python3 - <<'PY'
from cryptography.fernet import Fernet
from pathlib import Path

p = Path("/etc/axonos/x-capi-context-key")
p.write_bytes(Fernet.generate_key())
PY

sudo chown 10001:10001 /etc/axonos/x-capi-context-key
sudo chmod 0400 /etc/axonos/x-capi-context-key
```

Verify metadata only:

``` bash
sudo stat -c '%u %g %a %s %n' /etc/axonos/x-capi-context-key
```

Observed:

``` text
10001 10001 400 44 /etc/axonos/x-capi-context-key
```

Do not print the key contents.

------------------------------------------------------------------------

## 4. Generate the remaining internal CAPI secrets

This creates the worker HMAC/hash key, dedicated PostgreSQL bootstrap
password, dedicated PostgreSQL worker password, and worker DB URL:

``` bash
sudo python3 - <<'PY'
import secrets
from pathlib import Path

root = Path("/etc/axonos")

# Worker-only HMAC key
hash_key = secrets.token_hex(32)
(root / "x-capi-hash-key").write_text(hash_key)

# Dedicated PostgreSQL bootstrap password
bootstrap_password = secrets.token_hex(32)
(root / "x-capi-postgres-bootstrap-password").write_text(bootstrap_password)

# Dedicated least-privilege worker PostgreSQL password
worker_password = secrets.token_hex(32)
(root / "x-capi-postgres-worker-password").write_text(worker_password)

# Worker-only PostgreSQL connection URL.
# token_hex() uses only [0-9a-f], so URL escaping is unnecessary.
db_url = (
    "postgresql://axonos_x_capi_worker:"
    + worker_password
    + "@x-capi-postgres:5432/axonos_x_capi"
)
(root / "x-capi-db-url").write_text(db_url)
PY

sudo chown 10001:10001 \
  /etc/axonos/x-capi-hash-key \
  /etc/axonos/x-capi-db-url

sudo chmod 0400 \
  /etc/axonos/x-capi-hash-key \
  /etc/axonos/x-capi-db-url

sudo chown root:root \
  /etc/axonos/x-capi-postgres-bootstrap-password \
  /etc/axonos/x-capi-postgres-worker-password

sudo chmod 0400 \
  /etc/axonos/x-capi-postgres-bootstrap-password \
  /etc/axonos/x-capi-postgres-worker-password
```

Do not `cat` any of these files.

------------------------------------------------------------------------

## 5. Verify all five secret files

``` bash
sudo stat -c '%u %g %a %s %n' \
  /etc/axonos/x-capi-context-key \
  /etc/axonos/x-capi-hash-key \
  /etc/axonos/x-capi-db-url \
  /etc/axonos/x-capi-postgres-bootstrap-password \
  /etc/axonos/x-capi-postgres-worker-password
```

Observed:

``` text
10001 10001 400 44 /etc/axonos/x-capi-context-key
10001 10001 400 64 /etc/axonos/x-capi-hash-key
10001 10001 400 133 /etc/axonos/x-capi-db-url
0 0 400 64 /etc/axonos/x-capi-postgres-bootstrap-password
0 0 400 64 /etc/axonos/x-capi-postgres-worker-password
```

The DB URL size may vary. Ownership and restrictive permissions are the
important properties.

------------------------------------------------------------------------

## 6. Validate the base + CAPI Compose configuration

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  config --quiet
```

Confirm:

``` bash
echo $?
```

Observed:

``` text
0
```

This validation does not start or restart containers.

------------------------------------------------------------------------

## 7. Pull and build CAPI images

Pull the pinned PostgreSQL image:

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  pull x-capi-postgres x-capi-db-init
```

Build the CAPI privacy initializer and worker:

``` bash
docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  build x-capi-privacy-init x-capi-worker
```

Observed:

``` text
postgres:15-alpine@sha256:5fe8ca7fc662071188c30271cb870d1ce9a6ec4578c934b064697ae77a9241e1 Pulled
axonos-x-capi-worker Built
axonos-x-capi-privacy-init Built
```

No existing production AxonOS container was restarted.

------------------------------------------------------------------------

## 8. Start only the isolated CAPI services

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  up -d \
  x-capi-postgres \
  x-capi-privacy-init \
  x-capi-db-init \
  x-capi-worker
```

Do not use a generic profile-wide unnamed `up`, and do not use
`--no-deps` for the worker.

> **2026-10-02 current-state note:** The worker instruction above applies to
> this historical manual startup. The current reviewed
> [backend deployment sequence](PRODUCTION_DEPLOYMENT.md#explicit-capi-backend-sequence-and-mutation-boundary)
> uses `--no-deps` only for its subsequent worker startup after verifying
> database readiness and both fresh, current-attempt initializer IDs exited
> successfully. It does not authorize bypassing first-time initialization.

### First deployment result

The first attempt created the CAPI network and volumes and produced:

``` text
axonos-x-capi-privacy-init-1   Exited
axonos_x_capi_postgres         Healthy
axonos-x-capi-db-init-1        Error: exit 2
axonos_x_capi_worker           Created
```

The worker did not start because the database initializer failed,
demonstrating the intended fail-closed dependency behavior.

------------------------------------------------------------------------

## 9. Inspect service state

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  ps -a
```

Observed relevant state:

``` text
axonos-x-capi-privacy-init-1   Exited (0)
axonos_x_capi_postgres         Up (healthy)
axonos-x-capi-db-init-1        Exited (2)
axonos_x_capi_worker           Created
```

The existing production `axonos`, `axonos_postgres`,
`axonos_session_launcher`, `axonos_coturn`, and `axonos_files_tls`
services remained running and healthy.

------------------------------------------------------------------------

## 10. Inspect the failed DB initializer

``` bash
docker logs axonos-x-capi-db-init-1
```

Observed:

``` text
psql: error: connection to server at "x-capi-postgres" (...), port 5432 failed:
fe_sendauth: no password supplied
```

This proved that the initializer reached the intended dedicated CAPI
PostgreSQL service but did not obtain a matching authentication
password.

------------------------------------------------------------------------

## 11. Verify the dedicated PostgreSQL service

``` bash
docker logs axonos_x_capi_postgres --tail 80
```

The database initialized successfully and reported:

``` text
database system is ready to accept connections
```

The existing AxonOS PostgreSQL and dedicated CAPI PostgreSQL can both
use container port `5432` because they are separate containers on
separate Docker networks. Neither creates a host-port collision.

``` text
Existing AxonOS DB
axonos_postgres:5432
    |
    +-- axonos_control

Dedicated CAPI DB
x-capi-postgres:5432
    |
    +-- axonos_x_capi_db
```

------------------------------------------------------------------------

## 12. Bootstrap defect discovered during the first real deployment

The first deployment exposed a defect in:

``` text
docker/x-capi-worker/bootstrap_x_capi_db.sh
```

The temporary `.pgpass` entry was scoped to:

``` text
x-capi-postgres:5432:axonos_x_capi:x_capi_bootstrap:<password>
```

The bootstrap script subsequently performs administrative preflight
connections to:

``` text
postgres
template1
```

Those database names do not match the database-specific `.pgpass` entry,
so libpq has no matching password and reports:

``` text
fe_sendauth: no password supplied
```

The defect was handed back to the coding agent for a narrow correction
and regression test. The preferred correction is explicit `.pgpass`
coverage for only the databases the bootstrap process intentionally
accesses rather than unnecessarily broadening credential matching.

### Stop condition at this failure

Do not:

-   regenerate the internal secrets;
-   remove the dedicated CAPI PostgreSQL volume;
-   manually alter DB permissions;
-   manually start the worker;
-   recreate the central `axonos` container;
-   switch to `dry_run` or `live`;
-   provision the real X token;
-   use a real X click.

Wait for the reviewed bootstrap correction.

------------------------------------------------------------------------

## 13. Deployment checkpoint

### Prepared

-   `X_CAPI_MODE=off`
-   `/etc/axonos` protected secret directory
-   Fernet context key
-   HMAC/hash key
-   dedicated DB bootstrap password
-   dedicated DB worker password
-   worker DB URL
-   validated base + CAPI Compose configuration
-   pinned PostgreSQL image
-   CAPI worker image
-   CAPI privacy-init image
-   `axonos_x_capi_db` Docker network
-   CAPI runtime/privacy/database volumes
-   healthy dedicated CAPI PostgreSQL
-   successful privacy initializer

### Not yet active

-   successful CAPI DB schema/role initialization
-   running/healthy CAPI worker
-   central AxonOS gate recreated with the CAPI overlay
-   production synthetic `dry_run`
-   X Pixel/Event Source ID
-   X conversion event IDs
-   X CAPI access token
-   real-X canary
-   live X delivery

------------------------------------------------------------------------

## 14. X-specific configuration is separate

The eventual X configuration is expected to contain:

``` text
X_CAPI_PIXEL_ID=
X_CAPI_EVENT_WALLET_VERIFIED=
X_CAPI_EVENT_DEPOSIT_COMPLETED=
X_CAPI_EVENT_SESSION_STARTED=
X_CAPI_ACCESS_TOKEN=
```

These are not required for the `off`-mode infrastructure preparation
above.

The X access token must not be stored in the shared `.env`; it is
intended for the reviewed worker-only secret-file path.

> **2026-10-02 current-state note:** The empty token line above records a
> historical configuration expectation, not an environment-token interface.
> No delivery token is mounted by the current overlay. Exact IDs and approved
> credentials alone cannot enable live collection/delivery: the current
> [activation contract](X_CAPI.md#status-and-protocol-decision) requires further
> reviewed provenance and activation changes. Keep production in `off` mode.

------------------------------------------------------------------------

## 15. Security rules

-   Never print the contents of `/etc/axonos/x-capi-*`.
-   Keep secrets outside the repository.
-   Preserve worker UID `10001` and root ownership boundaries.
-   Keep the dedicated CAPI PostgreSQL network isolated.
-   Do not expose its PostgreSQL port on the host.
-   Do not attach the CAPI worker to the core AxonOS database network.
-   Keep `X_CAPI_MODE=off` during preparation and failure recovery.
-   A failed DB initializer must prevent the CAPI worker from starting.
-   Do not use real X credentials or click IDs during infrastructure
    preparation.

------------------------------------------------------------------------

## 16. Bootstrap fixes and successful recovery

The first real deployment exposed two genuine bootstrap/idempotency
defects, both corrected and regression-tested before production was
resumed.

### 16.1 `PGPASSFILE` database matching

The bootstrap connects to exactly `axonos_x_capi`, `postgres`, and
`template1`. The original temporary `.pgpass` contained only the
configured CAPI database entry. When preflight switched `PGDATABASE` to
`postgres` or `template1`, libpq had no matching password and `psql -w`
failed with:

``` text
fe_sendauth: no password supplied
```

The corrected bootstrap writes explicit entries for exactly those three
databases; no wildcard was introduced.

### 16.2 ACL idempotency

A second rerun defect produced:

``` text
ACL arrays must be one-dimensional
```

The preflight used `aclexplode(COALESCE(a.attacl,'{}'))`. Legitimate
NULL column ACLs became a zero-dimensional empty ACL array that
PostgreSQL rejected. The correction passes native ACL values directly to
`aclexplode`; NULL yields no explicit grant rows while real grants
remain inspected.

Exact-state testing also found owner privileges were not restored to
their canonical representation after grant cleanup. The corrected
migration restores only the seven standard owner privileges on the nine
reviewed CAPI tables and verifies the exact owner ACL state.

Validation:

``` text
fresh bootstrap                 PASS
identical second bootstrap      PASS
schema/data/roles/ACLs          identical
worker least privilege          PASS
unexpected grants/delegation    rejected
```

The production DB had failed before persistent bootstrap mutation, so
its existing volume and secrets were retained.

Local fix:

``` text
0150efe fix(capi): make database bootstrap authentication and ACLs idempotent
```

The controlled startup was rerun:

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  up -d \
  x-capi-postgres \
  x-capi-privacy-init \
  x-capi-db-init \
  x-capi-worker
```

Successful result:

``` text
axonos_x_capi_postgres         Healthy
axonos-x-capi-privacy-init-1   Exited (0)
axonos-x-capi-db-init-1        Exited (0)
axonos_x_capi_worker           Healthy
```

## 17. Worker postflight in `off` mode

Run:

``` bash
docker exec axonos_x_capi_worker \
  python3 /app/x_capi_cli.py validate --require-listener

docker exec axonos_x_capi_worker \
  python3 /app/x_capi_cli.py status

docker exec axonos_x_capi_worker \
  python3 /app/x_capi_cli.py demo-payload
```

Important state:

``` text
mode                              off
off_ready                         true
worker_ready                      true
worker_db_available               true
worker_db_schema_ready            true
worker_db_config_guard_ready      true
worker_db_target_isolated         true
context_key_file_readable         true
ingest_runtime_secure             true
ingest_listener_present           true
consent_listener_present          true
privacy_fence_secure              true
errors                            []
```

All queue counters were zero. `hash_key_file_readable=false` is expected
in `off`, because readiness probes it only in `dry_run` or `live`.
`token_file_readable=false` is expected because the real X token had
deliberately not been provisioned. The demo payload was generated
locally without contacting X.

## 18. Image-build fix: scientific Python resolver

A fresh full image build became stuck in pathological pip backtracking
at:

``` text
pip install --no-cache-dir 'numpy>=1.24.0,<2' matplotlib spyder
```

The running production image provided the known-good reference:

``` text
Python       3.10.12
pip          22.0.2
numpy        1.26.4
matplotlib   3.10.9
spyder       6.1.7
pyparsing    3.3.2
```

`pip check` passed. Isolated reproduction showed Spyder's dependency
graph caused pip to initially select Pylint 4.1.1/Astroid 4.3.x before
`python-lsp-server[all]` constrained Pylint below 4.1.

The bounded manifest became:

``` text
numpy==1.26.4
matplotlib==3.10.9
spyder==6.1.7
pyparsing==3.3.2
pylint==4.0.10
```

Timing:

``` text
original unpinned command      >300 s and still backtracking
four-package pin               >300 s and still backtracking
final five-package manifest     38.39 s
repeat                          42.70 s
```

The real full build subsequently completed the formerly pathological
Spyder layer in about 39 seconds.

Local fix:

``` text
cdcf674 fix(image): bound scientific Python dependency resolution
```

## 19. Image-build fix: NVIDIA package coherence

The next build reached the NVIDIA Xorg/userspace layer and failed
because APT mixed the requested host-compatible NVIDIA release with a
newer repository release.

Verify host driver:

``` bash
nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u
```

Observed:

``` text
580.173.02
```

The image resolver correctly mapped the requested package version to
`580.173.02-1ubuntu1`, but transitive dependencies drifted to
`580.178.04-1ubuntu1`.

The real driver-coupled closure contains 11 packages:

``` text
libnvidia-cfg1-580
libnvidia-common-580
libnvidia-compute-580
libnvidia-decode-580
libnvidia-gl-580
libnvidia-gpucomp-580
nvidia-firmware-580
nvidia-kernel-common-580
nvidia-modprobe
nvidia-persistenced
xserver-xorg-video-nvidia-580
```

The corrected installer derives the dependency set from APT metadata,
exact-pins every selected driver package, simulates the transaction,
rejects removals/kernel/DKMS installation, and verifies the complete
installed set afterward. All 11 verified at `580.173.02-1ubuntu1`
despite `580.178.04` remaining available.

Local fix:

``` text
573aed1 fix(image): enforce coherent NVIDIA userspace packages
```

The subsequent real full build passed this NVIDIA layer.

## 20. Successful `axonos:latest` build

Run:

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  build axonos
```

One attempt failed during final BuildKit export/unpack after the
Dockerfile steps had completed. Repeating the same command reused the
completed cache and successfully produced `axonos:latest`.

The completed image was:

``` text
sha256:ba1279204496c0b7b29c1fac0ace802c91cfb9b437bd19036b49e2c4fc19ed6d
```

## 21. Validate the new image before deployment

Scientific validation:

``` bash
docker run --rm \
  --entrypoint /usr/bin/python3 \
  axonos:latest \
  /opt/axonos-build/check_scientific_python.py \
  /opt/axonos-build/scientific-python.txt
```

Results:

``` text
numpy==1.26.4
matplotlib==3.10.9
spyder==6.1.7
pyparsing==3.3.2
pylint==4.0.10
spyder: 49 active requirements satisfied
python-lsp-server: 16 active requirements satisfied
NumPy/Matplotlib headless render passed
No broken requirements found.
```

NVIDIA validation:

``` bash
docker run --rm \
  --entrypoint /usr/bin/python3 \
  axonos:latest \
  /usr/local/bin/plan-nvidia-userspace.py verify \
  --manifest /usr/local/share/axonos/nvidia-userspace.txt
```

All 11 driver-coupled packages verified at `580.173.02-1ubuntu1`.

A Matplotlib `Axes3D` import conflict was also observed. The identical
failure was confirmed in the previously running production image, so it
was recorded as a separate pre-existing scientific-image issue rather
than a regression from this rollout.

## 22. Confirm no active tenant sessions

Immediately before replacing the central gate:

``` bash
docker ps \
  --filter 'name=axgt-session-' \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

Observed:

``` text
NAMES     STATUS    IMAGE
```

No active tenant session containers were present.

## 23. Deploy the central gate with CAPI still `off`

Deploy the already-built image without rebuilding dependencies:

``` bash
cd ~/AxonOS

docker compose --project-directory . \
  -f docker-compose.yml \
  -f docker-compose.x-capi.yml \
  --profile x-capi \
  up -d --no-deps axonos
```

The new `axonos` container started and became healthy. The dedicated
CAPI PostgreSQL and worker remained healthy.

## 24. Verify central-gate CAPI wiring

``` bash
echo "CAPI mode inside central gate:"
docker exec axonos printenv X_CAPI_MODE

echo
echo "CAPI paths inside central gate:"
docker exec axonos sh -c '
for p in \
  /run/secrets/x_capi_context_key \
  /run/axonos-x-capi \
  /run/axonos-x-capi-privacy
do
  if [ -e "$p" ]; then
    stat -c "%F %a %u:%g %n" "$p"
  else
    echo "MISSING: $p"
  fi
done
'
```

Observed:

``` text
CAPI mode inside central gate:
off

regular file 400 10001:10001 /run/secrets/x_capi_context_key
directory 700 10001:10001 /run/axonos-x-capi
directory 700 10001:10001 /run/axonos-x-capi-privacy
```

## 25. Final production smoke check

``` bash
curl -fsS -o /dev/null -w 'noVNC HTTP: %{http_code}\n' \
  http://127.0.0.1:6080/vnc.html

curl -fsS -o /dev/null -w 'Gate HTTP: %{http_code}\n' \
  http://127.0.0.1:8889/
```

Observed:

``` text
noVNC HTTP: 200
Gate HTTP: 200
```

## 26. Completed `off`-mode production state

``` text
CAPI-capable axonos image       deployed and healthy
Main AxonOS PostgreSQL          healthy
Session launcher                healthy
Dedicated CAPI PostgreSQL       healthy
CAPI DB bootstrap/migrations    successful
CAPI worker                     healthy
Context key mount               present and restricted
CAPI runtime                    present and restricted
Privacy fence                   present and restricted
X_CAPI_MODE                     off
CAPI queue                      empty at postflight
Public noVNC                    HTTP 200
Gate                            HTTP 200
X Event Source/Pixel ID         not configured
X conversion event IDs          not configured
X CAPI access token             not provisioned
Real X delivery                 disabled
```

> **2026-10-02 evidence note:** The recorded “Public noVNC” label above refers
> to §25's HTTP 200 from `127.0.0.1:6080`; the gate check likewise used loopback.
> Those observations establish local HTTP responses, not public ingress/TLS
> health. The recorded label is preserved here with that qualification.

Leave production in this state until the approved X configuration is
received. Do not invent placeholder X identifiers or tokens merely to
advance from `off`.

## 27. Local Git checkpoint

At completion of the `off`-mode rollout, local `main` included:

``` text
573aed1 fix(image): enforce coherent NVIDIA userspace packages
cdcf674 fix(image): bound scientific Python dependency resolution
0150efe fix(capi): make database bootstrap authentication and ACLs idempotent
e42f890 feat(capi): integrate privacy-minimized X Conversion API
f18a697 test(capi): verify synthetic browser attribution capture
```

Local `main` remained ahead of `origin/main`; pushing remote `main` was
intentionally deferred during the controlled deployment and validation
process. The separate `codex/x-capi-privacy` worktree remained isolated
and was not pushed as a remote branch.

> **2026-10-02 current-state note:** The ahead-of-remote statement records the
> original rollout checkpoint. At the later verified baseline in
> `/home/cluadmin/AxonOS`, `main`, `HEAD`, and `origin/main` all pointed to
> `0c49ad8875428beb444058038ad31a3b6bbd3d45`. See the
> [verified production baseline](PRODUCTION_DEPLOYMENT.md#first-time-host-provisioning-and-verified-production-baseline).
