# Production deployment

Use `scripts/deploy-production.sh` for reviewed central-gate updates, including
when `X_CAPI_MODE=off`. The Bash entrypoint invokes the bounded Python controller.
Every Compose operation uses the base file **and** `docker-compose.x-capi.yml`,
profile `x-capi`, project `axonos`, and the authorized checkout's explicit `.env`.
A base-only gate recreation can omit context-key, runtime, and privacy-fence
mounts. Do not substitute a generic `docker compose up -d --build`.

This is an operator-triggered update tool for an **already provisioned, healthy
deployment**, not a fresh installer, repair tool, PostgreSQL upgrader, or
unattended release system. It does not promise zero downtime or transactional
rollback. Establish an external maintenance/admission window before deployment.

## Authorized checkout and shared lock

Use the reviewed, committed script in the actual production checkout. The script
reports branch and HEAD, refuses detached HEAD and any dirty/untracked files,
and never pulls Git changes or changes branches itself. The existing `axonos`,
`axonos-launcher`, and core `postgres` containers must all have Compose
`project.working_dir` labels matching the script's canonical checkout path and
`project.config_files` labels including that checkout's base Compose file. The
gate must also identify its CAPI overlay. A second worktree with identical code
is not authorized merely because it can access Docker. Missing or different
provenance is refused; there is no authorization override. Moving the deployed
checkout requires a separately reviewed re-homing procedure, not label editing
or a deployment-flag bypass.

All invocations, including `--check`, acquire an exclusive nonblocking lock on:

```text
/run/lock/axonos-production-deploy.lock
```

The existing lock must be a canonical, regular, root:root-owned file with mode
`0444` and exactly one hard link. The controller opens it without following
symlinks, verifies its identity, and holds the same inode throughout the run.
It does not create, truncate, replace, or repair the lock. The shared path
serializes cooperating operators across checkouts targeting the supported
local Docker daemon/project; a worktree-local lock would not.

An administrator must provision this file **once**, during a maintenance window
when no deploy invocation is running. Inspect any existing file first. Never
replace or truncate an existing lock: doing so could create two lock inodes and
allow concurrent deployment. For an absent path only, one possible operator
procedure is:

```bash
sudo sh -eu -c '
  lock=/run/lock/axonos-production-deploy.lock
  if [ -e "$lock" ] || [ -L "$lock" ]; then
    printf "%s\n" "Lock already exists; inspect it, do not replace it." >&2
    exit 1
  fi
  install -o root -g root -m 0444 /dev/null "$lock"
'
sudo stat -c '%U:%G %a %h %F' /run/lock/axonos-production-deploy.lock
```

Expect `root:root 444 1 regular empty file` (wording can vary with locale).
The absent-path check and `install` are not an atomic multi-administrator
provisioning protocol: serialize this one-time administrative action separately.
Since `/run` is normally volatile, arrange separately reviewed boot-time
provisioning before deployment is used after reboot. Do not periodically replace
the file. No lock or administrative provisioning is performed by the script.

The supported engine is Linux Docker at `unix:///var/run/docker.sock`. Alternate
hosts/contexts and arbitrary Compose projects are refused. Docker Compose,
Buildx with Compose `build --print` support, Python 3, Git, and curl are required.
An operator needs Docker access and permission to inspect protected secret-file
metadata. Keep secrets outside the checkout/build context.
Candidate-reference classification additionally requires a source-reviewed
Engine/image-store combination and a compatible **effective client API**; see
the explicit support matrices below. Read-only admission checks run in every
preflight, including `--check`, even when no candidate images exist.

## Docker API admission

An allowed daemon version alone is insufficient: an older client or inherited
`DOCKER_API_VERSION` can produce responses missing required inspection fields.
Under the same Docker environment used for subsequent commands, preflight reads
`docker version --format '{{json .}}'` and uses **`Client.ApiVersion`**, the CLI's
effective negotiated/forced API. `Server.ApiVersion` is the daemon's advertised
maximum, not proof of the API actually used.

| Reviewed Linux Engine | Permitted effective API |
| --- | --- |
| **28.5.2** | **1.48–1.51**, also within the client's supported maximum and the server's declared minimum |
| **29.5.1 or 29.5.2** | **1.48–1.54**, with the same client/server bounds |

The server version must agree with `docker info`; its advertised API maximum
must match the reviewed release. Missing, malformed, inconsistent, or unsupported
metadata is refused. A nonempty inherited `DOCKER_API_VERSION` is preserved and
must agree exactly with the reported effective API; the tool never clears or
raises it to bypass admission. Forcing a newer API on a client whose
`DefaultAPIVersion` is older does not satisfy this contract. API **1.47 and below
are refused for both image stores**, even if a particular classic-store image
could be inspected successfully.

Version text is not the only evidence. Before building or service mutation,
preflight inspects the already-required gate's **immutable image ID**, without
pulling/building an image or creating a probe container. Its identity, config,
labels, and complete tag/digest metadata must satisfy the selected store's
reference contract. The current gate image must still be locally inspectable;
an unavailable image or unsupported response fails preflight rather than
creating a replacement probe. Containerd additionally requires the matching target
`Descriptor` supplied from API 1.48 onward. Existing managed-network inspect
responses must contain a real boolean `EnableIPv4`, an API 1.48 field; missing
metadata is not silently interpreted as the default. These capability checks
also run with an empty candidate inventory and in `--check`.

The relevant later API changes are accounted for: containerd's legacy
`GraphDriver` may be absent from API 1.52 onward, and API 1.53's additional image
`Identity` field does not replace the image ID or target descriptor used here.
Network `Status` and legacy top-level container bridge fields are not used for
resource attestation. Passing admission does not approve unreviewed daemon
versions or storage modes, prove future candidate-retention capacity, or
guarantee that the next image build or deployment will succeed.

These checks follow the upstream
[CLI 28.5.2 version output](https://github.com/docker/cli/blob/v28.5.2/cli/command/system/version.go),
[CLI 29.5.1 version output](https://github.com/docker/cli/blob/v29.5.1/cli/command/system/version.go),
[CLI initialization/negotiation](https://github.com/docker/cli/blob/v29.5.1/cli/command/cli.go),
[Engine 28.5.2 API constants](https://github.com/moby/moby/blob/v28.5.2/api/common.go),
[Engine 29.5.1 API constants](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/config/config.go),
and versioned
[image](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/server/router/image/image_routes.go),
[network](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/server/router/network/network_routes.go),
and [container inspection handlers](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/server/router/container/inspect.go).
Docker's [API versioning overview](https://docs.docker.com/reference/api/engine/)
describes negotiation and environment overrides; explicit agreement is checked
rather than assuming every CLI release handles overrides identically.

### Exact 29.5.2 compatibility review

Admission of **29.5.2** follows a pinned upstream source comparison, not an
assumption that patch releases are interchangeable. The reviewed Moby comparison
is [`docker-v29.5.1` → `docker-v29.5.2`](https://github.com/moby/moby/compare/dd24a3adc1db4c762fb1b26b35c08ffd936f2d8f...568f755ebeb1ac9c6a8febbda6cd371ea0a9630b)
(`dd24a3adc1db4c762fb1b26b35c08ffd936f2d8f` →
`568f755ebeb1ac9c6a8febbda6cd371ea0a9630b`); the CLI comparison is
[`v29.5.1` → `v29.5.2`](https://github.com/docker/cli/compare/2518b52d948a0cbee071d394c03c86a3005636ba...79eb04c7d8e1d73247cb7fe011eecc645063e0f0)
(`2518b52d948a0cbee071d394c03c86a3005636ba` →
`79eb04c7d8e1d73247cb7fe011eecc645063e0f0`).

| Contract | Comparison result |
| --- | --- |
| Engine API admission and inspect metadata | `daemon/config/config.go`, `daemon/server/middleware/version.go`, and image/network/container/volume inspect routes are byte-identical. Maximum API remains **1.54**; descriptor, network IPv4, and legacy-field behavior are unchanged. |
| Image-store/reference handling | Reviewed `daemon/containerd/` and `daemon/images/` inspect/delete/tag implementations, containerd listing/store metadata, and daemon info/list code are byte-identical. Derived digest versus stored canonical-reference handling and last-same-repository-tag protection remain unchanged. |
| CLI effective API, inspection, and removal | `cli/command/system/version.go`, `cli/command/cli.go`, context loading, image inspect/remove and inspection helper, vendored SDK client/ping/image inspect/remove, and `vendor/modules.txt` are byte-identical. |

The full Engine delta contains an unrelated `docker cp` mount/symlink fix and
AWS CloudWatch dependency updates, not image-store/reference or API-contract
changes. The CLI delta changes release/CI/documentation files and Buildx versions
used in its upstream development/end-to-end-test Dockerfiles, not the inspected
CLI/SDK implementation. This review does not separately certify arbitrary
Compose/Buildx plugin upgrades.

The 29.5.2 source anchors include its
[API constants](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/config/config.go),
[image response handler](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/server/router/image/image_routes.go),
[containerd inspection](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/containerd/image_inspect.go),
[containerd deletion](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/containerd/image_delete.go),
[classic inspection](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/images/image_inspect.go),
[classic deletion](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/images/image_delete.go),
and [CLI version output](https://github.com/docker/cli/blob/v29.5.2/cli/command/system/version.go).
Synthetic regression fixtures model the supplied host profile: Linux
client/server **29.5.2**, effective/maximum API **1.54**, server minimum **1.40**,
containerd `overlayfs` with `driver-type=io.containerd.snapshotter.v1`, and no
`DOCKER_API_VERSION` override. Source review and these synthetic tests are **not
production execution**: they do not establish that a real host passes all
deployment checks. Both supported storage modes retain their existing reference
and capability checks; no `29.5.x` wildcard or broader release range is admitted.

## Preflight contracts

Both ordinary and backend updates require an existing healthy central gate,
core PostgreSQL, launcher, dedicated CAPI PostgreSQL, and CAPI worker. Existing
CAPI initializer containers and all three CAPI volumes/network must be present.
Ordinary updates additionally require successful initializer exit codes and an
existing worker matching the intended worker configuration. Backend mode may
replace initializer/worker configuration, but it does not repair missing or
drifted persistent infrastructure.

Before any build or service mutation, preflight inspects **actual existing**
Docker resources, not just the desired Compose file:

- The CAPI network must be the owned `axonos_x_capi_db` local internal bridge,
  with matching labels, driver options, attachment/addressing settings, and
  IPAM policy. Docker-assigned subnet/gateway values are allowed only for the
  reviewed default automatic-IPAM form. Unknown attached containers or CAPI
  endpoints attached to other networks are rejected.
- Runtime, privacy-fence, and dedicated PostgreSQL volumes must have the
  expected Compose project/volume labels, custom labels, names, local driver,
  scope, options, and storage metadata. They remain ordinary Compose-managed
  volumes, not resources silently converted to `external`.
- The managed control/stack networks and core PostgreSQL volume are also
  attested for ownership, settings, and Compose hash drift: a targeted `up`
  can reconcile project resources even when dependencies are not recreated.
  Existing external resources are not converted or reconciled by this tool.
- Metadata-only checks inside the existing gate verify the context-key bind
  and runtime/privacy directory ownership/permissions. A metadata-only `stat`
  inside dedicated PostgreSQL checks PGDATA ownership/mode. These commands do
  not read secret bytes, connect to either database, or initialize state.
- Dedicated PostgreSQL must match the reviewed image reference, startup,
  database/user settings, mounts, networks, and access configuration. The tool
  will not retain a different configuration simply because `--no-recreate`
  was requested.
- The preserved launcher's intended configuration is compared with its actual
  container settings, including launcher token, endpoint, relevant environment,
  image reference, startup, mounts, networks, and access settings. Gate/launcher
  core-database URL identities must match the actual preserved core PostgreSQL
  container's declared credentials/database. Core startup, storage, networks,
  and relevant environment must match too. Healthy containers alone do not
  satisfy this contract.
- Gate and worker shared CAPI configuration is normalized using the application
  configuration loader and compared before mutation, including revenue-wallet
  and each exclusion source, audience/policy/deployment settings, privacy
  recovery epoch, context-key path, and sockets. Comparisons also apply while
  CAPI is off, so an off-mode empty audience cannot conceal a latent mismatch.

Dependency credential comparison uses in-memory configuration/inspect metadata,
never printed secret values. It does **not** authenticate to PostgreSQL or prove
that a password declared in container environment still matches an existing
database role. Secret files receive metadata checks, not content reads. Image
reference comparison for preserved dependencies is not proof of new code inside
an existing launcher image: launcher upgrades require a separate procedure.

Compose receives `COMPOSE_REMOVE_ORPHANS=false` and
`COMPOSE_IGNORE_ORPHANS=true`, overriding inherited shell and `.env` settings.
No `--remove-orphans`, `--yes`, or automatic destructive prompt response is used.
Compose stdin is closed; a reconciliation prompt cannot receive approval from
the invoking terminal. If existing resources cannot be attested, stop and
investigate; do not delete/recreate volumes to bypass the refusal.

For inspected Compose-managed resources, preflight also reproduces Compose's
resource configuration hash and rejects any hash that would trigger
reconciliation. This is separate from actual ownership/settings attestation:
apparently identical resources with stale hash labels must not reach Compose's
network recreation or volume-recreation prompt. Legacy absent hashes are
accepted, as Compose accepts them; an empty network hash is also accepted, but
an explicitly present empty volume hash is rejected. Unknown hash-input fields
fail closed rather than guessing. These rules follow the reviewed
[Compose hash implementation](https://github.com/docker/compose/blob/v5.1.4/pkg/compose/hash.go),
[reconciliation implementation](https://github.com/docker/compose/blob/v5.1.4/pkg/compose/create.go),
and [Compose configuration types](https://github.com/compose-spec/compose-go/blob/v2.11.0/types/types.go).
Do not repair a hash label blindly: resolve the actual drift through reviewed
maintenance before retrying. Re-review hash/reconciliation behavior when
upgrading Compose; the implementation was checked against v2.39.2 and v5.1.4.

## Commands and options

After reviewing/committing the release and establishing the maintenance window:

```bash
cd ~/AxonOS
git pull --ff-only
./scripts/deploy-production.sh --check
./scripts/deploy-production.sh
```

| Option | Meaning |
| --- | --- |
| `--check` | Read-only preflight, including Engine/store/effective-API admission, existing-image/network capability probes, and metadata-only exec checks in the existing gate and dedicated PostgreSQL. No image build, candidate cleanup, validator container creation, service mutation, or initialization. |
| `--with-capi-backend` | Explicitly update the already provisioned CAPI initializers/worker before gate rollout. |
| `--allow-active-sessions` | Conspicuous acceptance of possible viewer/control-plane interruption; does not stop or pause tenants. |
| `--health-timeout SECONDS` | Health/init polling deadline; default 300, permitted range 1–1800. |
| `--help` | Print the interface without acquiring the lock or deploying. |

For a reviewed backend change:

```bash
./scripts/deploy-production.sh --check --with-capi-backend
./scripts/deploy-production.sh --with-capi-backend
```

Prefer draining sessions over `--allow-active-sessions`. Active tenants are
identified using launcher names/labels (`axgt-session-<session-id>`), and checked
again after builds and before rollout. A detached viewer can still have an
active, billable tenant. These checks do not lock admission: a session can start
between observations. The host-wide deployment lock does not block manual Docker
commands, Git edits, key rotation, launcher admissions, or unrelated automation.

`--check` is **not** application `X_CAPI_MODE=dry_run`. Mode must be explicitly
assigned in `.env`; `off` and `dry_run` are accepted, `live` remains refused.
The tool neither edits mode nor provisions/reads an X token. Off-mode deployment
does not require token/event IDs. Python bytecode generation is disabled.
Preflight success does not prove an image will build or a future rollout will
succeed; all later gates remain mandatory.

## Exact immutable-image flow

1. Attest the bounded private-candidate inventory and apply the retention policy
   below before admitting another build. Obtain the canonical combined Compose build plan with
   `compose build --print --build-arg AXONOS_SKIP_HEAVY=0 axonos`. Require the
   expected checkout context/Dockerfile and reject unreviewed target inheritance
   or extra contexts. Rewrite the single target's tags to one random private
   `axonos-deploy-candidate:<run-id>` tag and its output to local `type=docker`.
   Add tool-ownership, run-ID, and creation-time image labels for safe retention.
   Send the plan to `docker buildx bake --file - --load axonos` through stdin,
   not a credential-bearing on-disk file. Full scientific applications and normal
   build caching are retained. This does **not** publish `axonos:latest`.
2. Inspect the private candidate once and capture its immutable `sha256:` image
   ID. Both validators run using that ID, never by resolving a mutable tag:

   ```text
   /usr/bin/python3 /opt/axonos-build/check_scientific_python.py /opt/axonos-build/scientific-python.txt
   /usr/bin/python3 /usr/local/bin/plan-nvidia-userspace.py verify --manifest /usr/local/share/axonos/nvidia-userspace.txt
   ```

   Validators are explicit create/start/inspect operations with unique run-owned
   names/labels, `runc`, no GPU/network/production mounts, read-only root, dropped
   capabilities, no-new-privileges, and bounded `/tmp` tmpfs. Success requires an
   exited container with exit code zero. The scientific validator's existing
   warning handling is unchanged. These checks attest image contents, not live
   GPU/host-driver or WebRTC operation.
3. Remove each validator with identity/label/image checks. Revalidate clean Git,
   unchanged branch/HEAD/configuration/secret metadata, actual resources,
   preserved dependency health/contracts, and active sessions. For backend mode,
   complete the backend sequence below before proceeding.
4. Write a temporary minimal Compose override containing **only** the immutable
   `services.axonos.image` ID. Validate the combined configuration. Promote that
   already validated ID to `axonos:latest`, then target only `axonos` with
   `up -d --no-deps --no-build --pull never` using the override. A concurrent tag
   change cannot substitute a different gate image: rollout does not resolve
   `axonos:latest` again.
5. Verify the running gate's immutable ID, health, mode/mount metadata, expected
   worker health/configuration, and local HTTP endpoints. Confirm the shared tag
   still points to the validated ID; concurrent drift reports failure and needs
   operator investigation even though gate rollout was pinned.
6. Remove this successful run's private candidate tag and apply bounded retention
   to prior owned leftovers. Housekeeping warnings do not turn a successfully
   verified production rollout into a failed deployment.

The shared tenant tag changes only after validation, but promotion is still a
separate mutation from gate replacement. New tenant admissions must remain
disabled operationally during deployment. A non-cooperating Docker administrator
can retag or otherwise mutate resources despite this tool's lock. The controller
cannot make arbitrary external administration race-free.

Keep `NVIDIA_DRIVER_PKG_VERSION` configured for the installed host driver. The
tool does not choose/change the driver or bypass scientific/NVIDIA validation.
It does not rebuild the launcher or replace existing tenant containers. Changes
to launcher code/dependencies/configuration or the gate/launcher protocol require
a separately reviewed coordinated procedure; see [Host Launcher](HOST_LAUNCHER.md).

## Private candidate retention

The candidate namespace is `axonos-deploy-candidate:<24-lowercase-hex-run-id>`.
Namespace membership alone does not authorize deletion. A tag is owned only if
its image labels identify `axonos-production-deploy-v1`, match its run ID, and
contain valid creation-time metadata. Cleanup re-inspects the exact tag,
immutable ID, and labels immediately before removal. Old unlabeled candidates,
malformed tags, and mismatched ownership are left untouched with a manual-review
warning; do not infer that a similarly named image is disposable.

After a successful rollout, the controller removes only its own matching
candidate alias. For failed/interrupted leftovers, it retains the newest **three
owned candidate tags** by recorded creation time and attempts to remove older
owned aliases. A successful run whose final cleanup previously failed may also
appear in this leftover inventory. The shared production tag, rollback tags,
and other repository references are never removal targets.

Removal is `docker image rm --no-prune <exact-candidate-tag>`, never image-ID
deletion, force removal, or broad image/builder pruning. Whether a repository
digest protects the candidate depends on the **verified image store**, not merely
whether `RepoDigests` is nonempty:

| Engine/storage combination | Reference contract used by retention |
| --- | --- |
| Linux Engine **28.5.2, 29.5.1, or 29.5.2**, classic `overlay2`, no `driver-type` snapshotter marker | `RepoTags` contains actual tag references; `RepoDigests` contains stored canonical references. |
| Linux Engine **28.5.2, 29.5.1, or 29.5.2**, `overlayfs`, `driver-type=io.containerd.snapshotter.v1` | `RepoTags` exposes actual stored names, including explicit canonical names containing `@`; `RepoDigests` also includes derived repository/digest names for ordinary tags. |
| Other version, storage driver, conflicting/missing mode evidence, or unsupported metadata | Refuse before building; do not guess reference/deletion semantics. |

These are exact version/mode limits, not a blanket approval of future patch or
major versions. The effective-API admission contract above applies to both
stores. Engine/storage metadata is rechecked during housekeeping. An
unsupported host needs a separate source-contract review and regression fixtures
before the allowlist is extended. Do not upgrade, downgrade, or switch a
production host's storage backend merely to bypass this refusal. Docker's
[containerd-store documentation](https://docs.docker.com/engine/storage/containerd/)
identifies the snapshotter marker; changing stores can hide existing images and
containers and is outside this deployment tool's scope.

On the supported containerd store, an ordinary locally built candidate can have
a nonempty derived `RepoDigests` entry without any separately stored canonical
reference. That derived metadata alone does not make the candidate permanent.
Classification checks the complete reference metadata and its agreement with
the image identity; **a digest equaling the image ID alone is not proof that it
is derived**. An explicit canonical name in `RepoTags` is not treated as an
ordinary spare tag. Unknown or inconsistent reference metadata fails closed.

Both supported stores may remove a same-repository canonical reference when
removing that repository's last tag, even if an unrelated production/rollback
tag still references the image. Consequently a real canonical reference in the
candidate repository blocks removal of that repository's last ordinary tag;
another repository's tag does not waive that protection. If another ordinary
tag in the same repository remains, the canonical reference survives removing
the candidate alias. A canonical reference in another repository is itself a
surviving image reference, so the candidate alias can be removed without deleting
that image or canonical reference. References in other repositories remain
untargeted and are preserved. A sole-reference candidate used by any container,
including stopped containers or descendants found by the ancestor filter, is
also retained. Engine non-force deletion checks provide an additional guard.

An externally saved `repository@digest` string is not an observable local
retention reference. To retain an image locally, establish an actual canonical
store record or an explicit rollback/retention tag outside the candidate
repository; do not rely on copied inspect output alone. The conservative
classifier refuses combined `tag@digest` names, IPv6 registry names, and
non-SHA-256 digest formats rather than assuming an unsupported reference is a
safe surviving anchor. Containerd metadata must include a matching target
descriptor with a supported OCI/Docker manifest or index media type. Its legacy
`GraphDriver` field may be absent on newer API responses; backend selection still
requires the attested Engine/storage combination.

The distinction follows the reviewed Moby implementations:
[28.5.2 classic inspection](https://github.com/moby/moby/blob/v28.5.2/daemon/images/image_inspect.go),
[classic deletion](https://github.com/moby/moby/blob/v28.5.2/daemon/images/image_delete.go),
[containerd inspection](https://github.com/moby/moby/blob/v28.5.2/daemon/containerd/image_inspect.go),
[containerd deletion](https://github.com/moby/moby/blob/v28.5.2/daemon/containerd/image_delete.go),
[29.5.1 classic inspection](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/images/image_inspect.go),
[classic deletion](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/images/image_delete.go),
[containerd inspection](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/containerd/image_inspect.go),
[containerd deletion](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/containerd/image_delete.go),
and [API response handling](https://github.com/moby/moby/blob/docker-v29.5.1/daemon/server/router/image/image_routes.go).

Unused older candidates with no protected references may be deleted, not merely
untagged. This retention policy is not a production-image rollback policy or a
promise that old candidates remain available indefinitely.

Each inventory is capped at **32 namespace tags**, with a **60-second housekeeping
deadline** and per-command limits of at most 15 seconds, plus bounded termination
grace. The tool checks retention before each new build and refuses to add another
candidate if it cannot attest the inventory or safely meet the three-tag quota.
A new build can temporarily make four owned tags until final housekeeping.
Unremovable references, an oversized legacy inventory, daemon errors, timeout,
or SIGKILL can leave more than the intended quota; the following deployment
refuses another build until reviewed cleanup resolves it. Final housekeeping is
warning-only and preserves the original deployment success/failure result.
`--check` performs Engine/store/effective-API admission and the read-only
capability probes, but does not run candidate housekeeping or prove future
retention capacity. Normal deployment additionally attests and enforces the
candidate inventory/quota before building.
Daemon-side work can finish after a client timeout; a late-exported owned
candidate is subject to the same checks on the next invocation, not a promise
of immediate removal after the failed client exits.

No pruning of shared layers/build cache occurs. Retained candidates/cache remain
sensitive because the existing Dockerfile build-argument exposure is unchanged.
Non-cooperating administrators can change tags between inspection and deletion;
the shared lock coordinates this tool, not arbitrary Docker access.

## Explicit CAPI backend sequence and mutation boundary

`--with-capi-backend` is not permission to upgrade/recreate PostgreSQL, change
database storage/credentials, or reset privacy state. Dedicated database drift
is rejected before stopping the worker and rechecked afterward. The sequence is:

1. Build worker/privacy-initializer images and fetch the configured pinned
   PostgreSQL/bootstrap images while the existing healthy worker stays running.
   Recheck resources, dedicated database readiness, configuration, and sessions.
2. Prepare **only the two replacement initializers**, without starting them,
   using targeted `up --no-start --no-deps --no-build --pull never --force-recreate`.
   Recheck again while the original worker remains healthy. This preparation
   replaces initializer containers but does not run DB/privacy initialization.
   Record the new initializer container IDs for this deployment attempt.
3. Verify the worker still has the original recorded ID and is healthy. Stop
   that exact ID with a 60-second grace period, then recheck resource/DB readiness.
4. Announce and mark the **persistent mutation boundary before issuing** any
   initializer-start command. Start initializers with dependencies enforced and
   `--no-recreate`; wait for both successful exit codes. A timeout/failure here
   is treated as possibly having changed DB/privacy state, even if no successful
   response was observed.
5. Verify that both recorded **current-attempt** initializer IDs exited zero,
   then prepare the replacement worker without starting dependencies. Recheck
   resource/database readiness, unchanged dedicated database identity, and those exact initializer IDs/completion again
   immediately before starting the worker. Start only the worker with
   `--no-deps --no-recreate`, so Compose cannot replay completed one-shot
   initializers during this second `up`. This is not a dependency bypass:
   fresh initialization in this attempt and successful dependency checks are
   mandatory. Require successful initializer states, healthy worker, and the
   intended worker contract before gate rollout.

Before the persistent boundary, a failure preserves the original worker if it
has not been stopped. If stop has been attempted, the controller makes a bounded
restoration attempt **only** for the original worker after checking unchanged
ID/image/configuration/mounts and resource contracts. A successful restoration
does not turn deployment into success. Prepared initializer containers or built
images may remain changed. If restoration checks fail, no replacement worker is
guessed or created; manual investigation is required.

After the boundary, **no automatic worker/database/privacy rollback** occurs.
The original worker may remain stopped and initialization may be partially
complete. Restoring an old worker against changed state could be unsafe, so
failure blocks gate rollout and requires the reviewed CAPI recovery procedure.
See [X CAPI](X_CAPI.md). Core PostgreSQL is never recreated, and existing
dedicated PostgreSQL is preserved. No supported path deletes or replaces its
data volume. Resource/configuration upgrades need separate maintenance review.

## Timeouts, cleanup, and postflight

Every subprocess has a timeout, a private process group, closed terminal input,
and suppressed diagnostic output. On interruption/timeout the controller sends
TERM to the group, waits up to five seconds, then sends KILL (including children
that survived their leader). Child launch/registration is protected so a signal
cannot unwind past an unrecorded child. Repeated SIGINT/SIGTERM requests are
recorded during bounded teardown, nested cleanup commands, and safe original-worker
restoration instead of interrupting TERM-to-KILL escalation or validator removal.
The shared deployment lock remains held through cleanup; deferred signals do not
release it while cleanup children are still being handled. An interrupted normal
operation remains a failed deployment after cleanup. Most Docker metadata operations allow 30 seconds;
builds allow six hours, each validator 300 seconds, and individual validator
removal 15 seconds, plus process-termination grace. Discovery/inspection during
cleanup have their own bounds, so total cleanup is not a single 15-second limit.
Health deadlines can likewise be exceeded by an in-flight bounded metadata call.
Signal deferral is scoped, not a global ignore setting: prior handlers and masks
are restored after protected launch/teardown windows. An interrupt during final
housekeeping is reported without reclassifying an already healthy rollout.
Uninterruptible kernel tasks are outside the userspace deadline guarantee.

Killing a Docker client does not necessarily stop a daemon-side container.
Therefore cleanup discovers the exact validator name, verifies its run label
and immutable image ID, and removes only that container. It also handles a
create operation that succeeded daemon-side before its client timed out. Cleanup
failure identifies the run-owned validator name for manual inspection rather
than broad removal/pruning. Daemon unavailability, host failure, or SIGKILL of
the controller can prevent cleanup; no userspace tool can guarantee cleanup in
those circumstances. Inspect the reported identity before any manual removal.

Successful postflight requires:

- expected immutable gate image and healthy gate/worker;
- configured CAPI mode and expected context-key/runtime/privacy mounts;
- context-key and runtime/privacy ownership/permission metadata;
- HTTP 200 from `http://127.0.0.1:6080/vnc.html` and
  `http://127.0.0.1:8889/`, with bounded curl and no response-body logging;
- no detected shared-tag drift at the final check.

These are local checks, not verification of public ingress/TLS, login, payments,
desktop launch, WebRTC media, or runtime GPU functionality. Do not infer those
outcomes from a healthy container or HTTP 200.

| Failure stage | Actual remaining state and required response |
| --- | --- |
| Preflight/session refusal | No deployment mutation. Correct the prerequisite or drain admissions; do not bypass ownership/isolation checks. |
| Candidate build/validation | Existing production containers and shared `axonos:latest` are unchanged by this tool. Failed candidate retention is bounded on normal completion; protected/legacy artifacts or cleanup failures can require manual review before another build. A failed cleanup may leave a specifically identified validator. Do not bypass validators. |
| Backend preparation before worker stop | Original worker remains running; built images and replacement unstarted initializers may remain. No initialization has run. |
| After worker stop, before persistent boundary | Attempt to restore only the attested original worker. Restoration can fail/refuse; inspect state. Deployment is failed regardless. |
| At/after initializer start boundary | DB/privacy changes may have occurred and worker may be stopped/partially replaced. No automatic rollback. Gate rollout is blocked. Follow CAPI recovery guidance. |
| After validated-tag promotion | The validated shared tag may remain changed even if gate recreation fails. Promotion and gate replacement are not atomic. |
| Gate health/mode/mount/HTTP failure | Replacement may already be running or unhealthy. No rollback/recreation loop. Diagnose actual state before deliberate recovery/retry. |
| Candidate housekeeping after healthy rollout | Warn and leave any unremovable alias for review; the verified production rollout remains successful. A later build is refused if retention cannot be attested/met. |

For controlled non-secret diagnosis:

```bash
docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' axonos
curl --silent --output /dev/null --write-out '%{http_code}\n' --max-time 10 http://127.0.0.1:6080/vnc.html
curl --silent --output /dev/null --write-out '%{http_code}\n' --max-time 10 http://127.0.0.1:8889/
```

Use the reported stage to select logs for local review; logs may contain user or
operational data and need redaction. Do not publish unfiltered inspect/config,
environment, `.env`, health logs, or build output. Subprocess diagnostics are
withheld deliberately; the tool does not generate a raw failure-log bundle.

## Secret safety and remaining operator responsibilities

The entrypoint disables shell tracing and uses private file permissions. Resolved
configuration/credentials are compared in process memory, not written as an
expanded Compose file or passed as command-line secret values. Secret contents
are not printed. Do not invoke the tool under external tracing or combine it
with environment/secret dumps.

The existing Dockerfile `PASSWORD` build argument remains a separate image/build
cache confidentiality risk. Withholding build output does **not** remediate
build-argument exposure; private candidate images/cache must still be treated as
sensitive. This task does not redesign that build mechanism.

The tool deliberately does not pull/commit/push Git changes, change branches or
CAPI mode, use X credentials/contact X, change host drivers, skip scientific
applications, upgrade/recreate database infrastructure, prune volumes, stop tenants, deploy
launcher changes, change ingress/privacy logging, make backups, or automatically
roll back a multi-service deployment. Verify backups/compatibility before backend
changes. After successful local postflight, perform separately approved
application/ingress smoke tests before reopening admissions. Retain the safe
branch/commit/mode/stage summary without adding secret-bearing diagnostics.
