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

Use [X CAPI](X_CAPI.md) for subsystem architecture, security, privacy, activation,
and configuration. The [historical off-mode guide](X_CAPI_OFF_Mode_First_Deployment_Guide_Updated.md)
records the actual first production preparation, deployment, and troubleshooting;
its manual Compose commands remain a historical record. This runbook owns the
current routine production workflow.

**Routine read-only preflight:** run from the authorized, clean production
checkout:

```bash
cd ~/AxonOS
./scripts/deploy-production.sh --check
```

**Routine production deployment:** only after preflight passes and the operator
maintenance/admission window is in place, run:

```bash
cd ~/AxonOS
./scripts/deploy-production.sh
```

The script owns the base-plus-CAPI Compose files, profile, and targeted gate
rollout; operators do not need to reconstruct the long central-gate Compose
command. See [commands and options](#commands-and-options) for release preparation
and explicitly requested backend updates.

## First-time host provisioning and verified production baseline

The operator-verified real-host `--check` on **2026-10-02** passed with the
following baseline. This records that completed verification; the documentation
update did not rerun deployment tooling or privileged tests.

| Item | Verified value |
| --- | --- |
| Production checkout / operator | `/home/cluadmin/AxonOS` / `cluadmin` |
| Branch / Git commit | `main` / `0c49ad8875428beb444058038ad31a3b6bbd3d45` (`origin/main` at the same commit) |
| CAPI mode | `off` |
| Docker client / server | `29.5.2` / `29.5.2` |
| Effective API / server minimum API | `1.54` / `1.40` |
| Storage driver | `overlayfs` |
| `DOCKER_API_VERSION` | unset |

After the [runtime lock](#authorized-checkout-and-shared-lock),
[persistent helper and sudoers policy](#one-time-administrator-provisioning)
were correctly provisioned, the actual final real-host command was:

```bash
cd ~/AxonOS
./scripts/deploy-production.sh --check
```

The successful check concluded with:

```text
Docker API 1.54: read-only image/resource capability checks passed.
Active tenant sessions: none
Deployment summary: branch=main commit=0c49ad8875428beb444058038ad31a3b6bbd3d45 CAPI=off backend=False
Target: local Docker/project axonos, authorized checkout; shared project lock held.
Build private full candidate; validate immutable ID; promote afterward; roll out pinned ID.
Launcher/core DB are preserved. Admissions still require an operator maintenance window.
CHECK PASSED: read-only Docker/metadata checks; no build, service mutation or initialization.
```

`--check` itself did not build an image, mutate services, or run initialization;
the build/promotion/rollout line describes the normal deployment plan only.

This success required the already provisioned healthy services, authorized clean
checkout, Docker access, supported tools, and [preflight contracts](#preflight-contracts)
described below, plus these separately provisioned host prerequisites:

| Host path | Required installation | Reboot behavior |
| --- | --- | --- |
| `/run/lock/axonos-production-deploy.lock` | `root:root`, `0444`, regular empty file, exactly one hard link | Normally volatile; must be safely reprovisioned if absent |
| `/usr/local/libexec/axonos-deploy-secret-metadata.py` | `root:root`, `0444`, regular file | Persistent host configuration |
| `/etc/sudoers.d/axonos-deploy-secret-metadata` | `root:root`, `0440`, exact fixed helper authorization below | Persistent host configuration |
| `/etc/axonos` and its five protected CAPI secret files | Directory `root:root`, `0700`; files retain the [reviewed metadata contract](#protected-secret-metadata-without-directory-access) | Persistent host configuration and secrets |

The installed helper's reviewed SHA-256 at commit `0c49ad8` was:

```text
d8d996ecdee014f33a87f3886da6f4b52142c79eaf4c69964d43caa32140ee3c
```

Follow the existing [shared-lock procedure](#authorized-checkout-and-shared-lock)
and [administrator helper/sudoers procedure](#one-time-administrator-provisioning);
the deployment tool installs neither. `cluadmin` intentionally cannot directly
traverse `/etc/axonos` or read its CAPI secrets through ordinary filesystem
access. The fixed helper attests metadata of exactly the five reviewed paths,
without reading secret contents, preserving the read-only `--check` contract.
Its authorization does not grant general access to the secrets.

### First real-host compatibility findings

- **Exact Docker 29.5.2 support:** the initial reviewed versions were 28.5.2 and
  29.5.1; the production host exposed 29.5.2. That exact release's image
  inspection/deletion, containerd/overlayfs, API 1.54, Descriptor/RepoDigest, and
  network metadata semantics were independently reviewed before admission was
  extended. See the [pinned compatibility review](#exact-2952-compatibility-review).
  Arbitrary `29.5.x` versions are not supported.
- **Compose startup inheritance:** real rendering uses JSON `null` for inherited
  image command/entrypoint values. Treating a present `null` differently from an
  omitted key falsely reported startup drift. The corrected validator treats
  omitted/null fields as inheritance of reviewed image defaults, explicit empty
  values as overrides, and explicit nonempty values as startup argv to normalize
  and compare exactly. Empty overrides must never silently become inheritance;
  the [startup contract](#preflight-contracts) also covers entrypoint/CMD interaction.
- **Protected-secret metadata:** direct `Path.resolve()`/`lstat()` by `cluadmin`
  could not traverse root:root `0700` `/etc/axonos`. The reviewed fixed privileged
  metadata helper resolved this preflight failure while retaining directory
  confinement and leaving secret contents unread. Do not weaken the directory
  to `0711`. The [privileged validation record](#privileged-helper-validation-record)
  documents the subsequent synthetic integration run.

### Reboot behavior and post-reboot checklist

**`/run` is normally volatile.** The shared deployment lock normally disappears
after a host reboot unless a separate boot-time mechanism recreates it. The
helper, sudoers policy, and `/etc/axonos` with its protected secrets are persistent
and normally survive reboot. The helper and sudoers policy do not normally need
reinstalling or recreating after an ordinary reboot. **Do not use deployment
tooling after reboot until the lock has again been safely provisioned under its
reviewed ownership, mode, type, and link contract.**

Separately reviewed boot-time provisioning remains required before deployment
tooling is used after reboot. An automatic mechanism would be preferable to
relying on operator memory; this record does not establish that one is installed.
No systemd-tmpfiles rule, service, cron task, or startup script was installed or
configured as part of this documentation update.
The reviewed absent-path procedure below provides manual provisioning after
reboot; any automatic mechanism requires separate review.

1. Confirm `/run/lock/axonos-production-deploy.lock` meets the
   [reviewed lock procedure](#authorized-checkout-and-shared-lock); if absent,
   have an administrator safely provision it during a serialized maintenance
   window. Inspect any existing file; never replace or truncate it.
2. Verify the persistent helper and sudoers installation at
   `/usr/local/libexec/axonos-deploy-secret-metadata.py` and
   `/etc/sudoers.d/axonos-deploy-secret-metadata` still meets the ownership/mode,
   reviewed helper hash, trusted-path, and exact-authorization requirements.
   Follow the [administrator validation procedure](#one-time-administrator-provisioning),
   including `sudo visudo -cf /etc/sudoers.d/axonos-deploy-secret-metadata` and
   `sudo visudo -c`. Keep `/etc/axonos` root:root `0700`.
3. As `cluadmin`, run:

   ```bash
   cd ~/AxonOS
   ./scripts/deploy-production.sh --check
   ```

4. Do not perform a production deployment unless `--check` passes. Then use
   `./scripts/deploy-production.sh` within the operator maintenance window.

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

The provisioned lock must be a canonical, regular empty, root:root-owned file with mode
`0444` and exactly one hard link. The controller opens it without following
symlinks, verifies its identity, and holds the same inode throughout the run.
It does not create, truncate, replace, or repair the lock. The shared path
serializes cooperating operators across checkouts targeting the supported
local Docker daemon/project; a worktree-local lock would not.

**Volatile `/run` lock provisioning:** an administrator must provision this file
only when absent, during a maintenance window when no deployment invocation is
running. Inspect any existing file first. Never replace or truncate an existing
lock: doing so could create two lock inodes and
allow concurrent deployment while another process still holds the old inode.
During the verified **2026-10-02** setup, the path was confirmed absent and the
following reviewed command was used:

```bash
sudo sh -eu -c '
lock=/run/lock/axonos-production-deploy.lock
if [ -e "$lock" ] || [ -L "$lock" ]; then
printf "%s\n" "Lock already exists; inspect it, do not replace it." >&2
exit 1
fi
install -o root -g root -m 0444 /dev/null "$lock"
'
```

The verification command was:

```bash
sudo stat -c '%U:%G %a %h %F' \
/run/lock/axonos-production-deploy.lock
```

Observed verified state:

```text
root:root 444 1 regular empty file
```

Expect the same metadata on reprovisioning (wording can vary with locale).
The absent-path check and `install` are not an atomic multi-administrator
provisioning protocol: serialize this administrative action separately.
Since `/run` is normally volatile, arrange separately reviewed boot-time
provisioning before deployment is used after reboot. Do not periodically replace
the file. No lock or administrative provisioning is performed by the script.

The supported engine is Linux Docker at `unix:///var/run/docker.sock`. Alternate
hosts/contexts and arbitrary Compose projects are refused. Docker Compose,
Buildx with Compose `build --print` support, Python 3, Git, and curl are required.
An operator needs Docker access and the narrowly provisioned metadata-helper
authorization below, not permission to traverse the protected secret directory.
Keep secrets outside the checkout/build context.
Candidate-reference classification additionally requires a source-reviewed
Engine/image-store combination and a compatible **effective client API**; see
the explicit support matrices below. Read-only admission checks run in every
preflight, including `--check`, even when no candidate images exist.

## Protected secret metadata without directory access

Keep `/etc/axonos` **root:root `0700`**. An unprivileged operator cannot `lstat()`
a child through that directory even when requesting metadata only: Linux
requires search permission on each parent, separately from permissions on the
file itself. The previous host-side check could therefore raise `PermissionError`
before validating an otherwise correct secret. This is a traversal problem,
not evidence of incorrect secret contents or a reason to change directory mode.
See Linux [stat](https://man7.org/linux/man-pages/man2/stat.2.html) and
[path resolution](https://man7.org/linux/man-pages/man7/path_resolution.7.html).

The deployment controller uses a fixed, root-owned helper installed separately
from the writable checkout. It invokes only:

```text
/usr/bin/sudo -n -- /usr/bin/python3 -I -S -B /usr/local/libexec/axonos-deploy-secret-metadata.py
```

The helper accepts no caller arguments, paths, stdin configuration, or
environment-selected policy. Its only secret targets are these five default
host paths; custom locations require a separate reviewed policy change:

| Fixed path beneath `/etc/axonos/` | Required UID:GID | Size in bytes |
| --- | --- | --- |
| `x-capi-context-key` | `10001:10001` | 44–4096 |
| `x-capi-db-url` | `10001:10001` | 1–8192 |
| `x-capi-hash-key` | `10001:10001` | 32–256 |
| `x-capi-postgres-bootstrap-password` | `0:0` | 16–8192 |
| `x-capi-postgres-worker-password` | `0:0` | 16–8192 |

Every target must be a regular file, have exactly one hard link, and retain mode
`0400` or `0600`. The helper checks root-owned, non-writable-by-others path
components and the exact `0700` secret-parent boundary. Directory-relative
`O_PATH`/`O_NOFOLLOW` descriptors and `fstat` inspect metadata without opening
secret contents for reading. Each component is checked; a single final-component
`O_NOFOLLOW` check would not protect against a symlinked parent. Leaf symlinks,
unexpected types, substitutions, or inconsistent metadata are refused. Linux
[O_PATH semantics](https://man7.org/linux/man-pages/man2/open.2.html) permit
descriptor metadata checks but reject content reads on those descriptors.

The bounded output contains only the reviewed metadata schema, including
parent/file device and inode identity, UID/GID, type/mode, link count, size, and
change/modification timestamps. All five files are attested on every relevant
configuration fingerprint/recheck, safe-worker-restoration check, and gate
postflight; results are not cached. These fingerprints detect changes, not
secret contents. No secret is copied, hashed, printed, or saved in temporary
files. The helper has its own five-second alarm in addition to controller command
timeouts. Missing privilege, unavailable helper, access denial, or unverifiable
metadata fails closed with a sanitized explanation; routine deployment does not
prompt for a sudo password or require a preceding `sudo -v`.

### One-time administrator provisioning

**One-time / persistent host provisioning:** the helper and sudoers policy
normally survive reboot; this installation is separate from provisioning the
volatile `/run` lock. The dated commands below record the verified 2026-10-02
setup, with future installation/upgrade requirements retained in each step.

This is a **separate administrator action**, not something the deployment tool
performs. No production installation is implied by adding the source file.
Provision during a serialized maintenance window after reviewing the committed
`scripts/deploy_production_secret_metadata.py` source and sudo policy.

1. Obtain the exact reviewed source in administrator-controlled staging and
   verify it against the approved commit/release. Do not execute a script in the
   operator-writable checkout as root. The helper is a separately installed copy,
   not a symlink back to the repository.
2. Inspect `/usr/local/libexec` and all its ancestors. They must be trusted,
   root:root directories without special permission bits or group/other write access; reject symlinked
   helper paths. Create a missing directory separately with reviewed ownership
   and permissions. Inspect any existing helper and sudoers entry before
   replacing either; do not blindly overwrite an installation or change parent
   permissions to make a check pass.

   During the verified 2026-10-02 setup, the trusted destination/ancestors and
   destination absence were checked before installation. The missing trusted
   libexec directory was created with:

   ```bash
   sudo install -d -o root -g root -m 0755 /usr/local/libexec
   ```

3. For a confirmed absent destination, install the reviewed copy as root:root,
   mode `0444`, at `/usr/local/libexec/axonos-deploy-secret-metadata.py`. It needs
   no executable bit because the exact system interpreter reads it. On
   **2026-10-02**, only after the preceding trust/absence checks, the already
   reviewed/tested helper was installed from the administrator-controlled
   disposable source copy with:

   ```bash
   sudo install -o root -g root -m 0444 \
   /root/axonos-deploy-privtest/source/scripts/deploy_production_secret_metadata.py \
   /usr/local/libexec/axonos-deploy-secret-metadata.py
   ```

   `/root/axonos-deploy-privtest/source` was a disposable validation copy for
   this setup, not permanent infrastructure or a future provisioning source.
   Its creation and tests are recorded [below](#privileged-helper-validation-record).
   For future installation or helper upgrades, use an administrator-controlled
   copy of the exact reviewed commit/release under step 1 and substitute that
   source path. Serialize the absence check and installation; this is not an
   atomic multi-administrator provisioning protocol. Do not reinstall the helper
   merely because the host rebooted.

   The installed helper and libexec metadata, then the helper hash, were verified
   with:

   ```bash
   sudo stat -c '%U:%G %a %h %F %n' \
   /usr/local/libexec \
   /usr/local/libexec/axonos-deploy-secret-metadata.py

   sudo sha256sum \
   /usr/local/libexec/axonos-deploy-secret-metadata.py
   ```

   The helper was root:root, mode `0444`, a regular file with one hard link. Its
   SHA-256 matched the [verified baseline](#first-time-host-provisioning-and-verified-production-baseline):
   `d8d996ecdee014f33a87f3886da6f4b52142c79eaf4c69964d43caa32140ee3c`.
4. Review any existing policy first. The actual **2026-10-02** sudoers procedure
   created this exact candidate rule and validated it **before installation**:

   ```bash
   printf '%s\n' \
   'cluadmin ALL=(root) NOPASSWD: NOSETENV: /usr/bin/python3 -I -S -B /usr/local/libexec/axonos-deploy-secret-metadata.py' \
   > /tmp/axonos-deploy-secret-metadata.sudoers

   sudo visudo -cf /tmp/axonos-deploy-secret-metadata.sudoers
   ```

   Observed:

   ```text
   /tmp/axonos-deploy-secret-metadata.sudoers: parsed OK
   ```

   After successful candidate validation, it was installed and both the installed
   policy and complete sudo configuration were validated:

   ```bash
   sudo install -o root -g root -m 0440 \
   /tmp/axonos-deploy-secret-metadata.sudoers \
   /etc/sudoers.d/axonos-deploy-secret-metadata

   sudo visudo -cf /etc/sudoers.d/axonos-deploy-secret-metadata
   sudo visudo -c

   sudo stat -c '%U:%G %a %h %F %n' \
   /etc/sudoers.d/axonos-deploy-secret-metadata
   ```

   Observed installed state:

   ```text
   root:root 440 1 regular file
   ```

   The temporary candidate was removed afterward:

   ```bash
   rm /tmp/axonos-deploy-secret-metadata.sudoers
   ```

   This authorizes **only**
   `/usr/bin/python3 -I -S -B /usr/local/libexec/axonos-deploy-secret-metadata.py`.
   Do not grant arbitrary passwordless Python, arbitrary scripts, shell, `stat`,
   user-supplied paths, wildcard arguments, or extra trailing arguments. This
   fixed interpreter command is the whole authorization, not broad passwordless
   sudo. The installed policy persists across ordinary reboot and does not
   normally need to be recreated.
5. Verify that `/usr/bin/python3`, its standard library, the helper, and their
   installation paths remain administrator-controlled. Then run the normal
   deployment `--check` as `cluadmin`. Routine invocations use a minimal
   environment and closed stdin; `-I -S -B` disables user/environment import
   influence, site initialization, and bytecode writes. Future helper updates
   require the same separate review/provisioning process.

The [sudo policy documentation](https://github.com/sudo-project/sudo/blob/main/docs/sudoers.man.in)
describes exact argument matching and `NOSETENV`; the
[sudo command documentation](https://github.com/sudo-project/sudo/blob/main/docs/sudo.man.in)
defines noninteractive `-n`. Python's
[interpreter options](https://docs.python.org/3/using/cmdline.html) explain the
isolation flags. The helper performs no filesystem or service mutation; normal
sudo audit records may still be generated by host policy, without secret bytes.

The regression module `axonos_gate/tests/test_production_deploy_secret_metadata.py`
has ordinary metadata/protocol tests and three explicitly root-only integration
tests. The latter create only synthetic temporary trees under the test checkout,
then chroot children before using the fixed `/etc/axonos` paths; one child drops
to UID/GID `1000` to reproduce the original search denial. Run them only from
an administrator-reviewed, administrator-controlled disposable source copy as
root, not against production secret files. The exact historical invocation is
recorded [below](#privileged-helper-validation-record); future releases require
review and validation of their own source copy. A single-UID rootless namespace
cannot exercise the distinct `0`, `1000`, and `10001` ownership cases. An
unprivileged run skips these three tests; passing mocks or the supplementary
real search-denial test does **not**
establish that the root-only integration tests ran successfully.

### Privileged helper validation record

**Historical validation record — 2026-10-02:** following the first real-host
compatibility fixes, the root-only synthetic integration tests were run from an
administrator-controlled disposable source copy. From the reviewed worktree,
the administrator created and restricted that copy with:

```bash
sudo mkdir -m 0700 /root/axonos-deploy-privtest
sudo cp -a . /root/axonos-deploy-privtest/source
sudo chown -R root:root /root/axonos-deploy-privtest
sudo chmod -R go-w /root/axonos-deploy-privtest
```

The source helper and disposable copy were compared by SHA-256:

```bash
sha256sum scripts/deploy_production_secret_metadata.py

sudo sha256sum \
  /root/axonos-deploy-privtest/source/scripts/deploy_production_secret_metadata.py
```

Both produced the reviewed implementation hash:

```text
d8d996ecdee014f33a87f3886da6f4b52142c79eaf4c69964d43caa32140ee3c
```

The privileged synthetic suite was then run from the root-controlled copy:

```bash
sudo /usr/bin/python3 -I -S -B -m unittest discover \
  -s /root/axonos-deploy-privtest/source/axonos_gate/tests \
  -p 'test_production_deploy_secret_metadata.py' \
  -v
```

Observed result:

```text
Ran 20 tests in 0.541s
OK (skipped=1)
```

All three previously unavailable root-only integration tests executed and passed:

- `test_real_invalid_leaf_metadata_is_refused`
- `test_real_parent_substitution_and_metadata_change_during_collection_refuse`
- `test_uid_1000_reproduces_resolve_and_lstat_denial_then_root_collects`

The sole skip, `test_real_unprivileged_search_denial_has_sanitized_refusal`,
intentionally requires an unprivileged context. That DAC/search-denial scenario
had separately been exercised as `cluadmin` and produced the expected sanitized
refusal. These tests used synthetic temporary/chroot fixtures; they did not
operate on production CAPI secret contents. This is a record of completed
validation, not a request to run privileged tests on the production checkout.

This historical validation is **not a routine deployment or reboot prerequisite**.
Do not recreate `/root/axonos-deploy-privtest` for every deployment or reboot;
the disposable path is not permanent host infrastructure. Future helper releases
must be reviewed and validated from an administrator-controlled copy of the exact
approved commit/release under the [current provisioning procedure](#one-time-administrator-provisioning).

### Why not change permissions or use another access path?

| Alternative | Security/coverage limitation |
| --- | --- |
| Change the secret directory to `0711` | Allows everyone to probe known filenames and inspect metadata. File permissions still deny ordinary non-owner reads, but host processes with a secret's owner UID gain access to known files. This removes an existing directory boundary and is not adopted. |
| Inspect existing running-container binds | Covers some files, but the worker bootstrap password is mounted only in the stopped DB initializer. Starting it is not a read-only check; inspect mount declarations alone do not attest host file metadata. Container bind metadata also need not describe a replaced host pathname. |
| Grant directory-search ACLs | Narrows which users gain traversal compared with `0711`, but still changes the protected directory's access policy. Not needed for this solution. |
| Grant a DAC-bypass capability | `CAP_DAC_READ_SEARCH` also permits content reads, not just metadata. `O_PATH` alone does not bypass parent search permissions. A capability on general Python or `stat` is not a fixed-path metadata service. |
| Add a privileged metadata daemon/socket | Can implement a similar policy but adds lifecycle, authentication, and deployment infrastructure. A fixed one-shot helper suffices here. |

Directory-FD anchoring and identity rechecks detect ordinary path substitution;
they do not lock a pathname through a subsequent Docker bind-mount operation or
prevent privileged administrators from replacing files between observations.
Retain the maintenance window and do not rotate secrets concurrently with
deployment. The existing Docker socket authorization is itself
[root-equivalent](https://docs.docker.com/engine/install/linux-postinstall/): this
design preserves ordinary filesystem unreadability and confines this helper,
not secrecy against a deliberately malicious Docker-authorized operator or root.

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
The separate [2026-10-02 real-host baseline](#first-time-host-provisioning-and-verified-production-baseline)
above records the subsequent successful production `--check`.

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

  Startup attestation resolves omitted or `null` command/entrypoint fields using
  the reviewed service-specific image defaults, not literal JSON equality with
  the container's inherited argv. These defaults are fixed review assumptions,
  not inferred from the existing container or a mutable image tag. Resolved
  Compose values must be `null` or argv lists; explicit argv is compared exactly.
  Explicit `[]` (including a source-level empty string normalized by Compose)
  remains an empty override, never an inheritance request. A non-null entrypoint
  suppresses the image's default command under the
  [declared Compose contract](https://docs.docker.com/reference/compose-file/services/#entrypoint).
  The reviewed Engine's
  [config merge](https://github.com/moby/moby/blob/docker-v29.5.2/daemon/commit.go#L69-L76)
  can nevertheless inherit image CMD in some explicit-empty cases. If actual
  startup then differs from the declared intent, preflight refuses it rather
  than accepting that fallback or weakening the startup check. This normalization
  does not authorize a launcher/database startup change.
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
| `--check` | Read-only preflight, including the fixed privileged secret-metadata helper, Engine/store/effective-API admission, existing-image/network capability probes, and metadata-only exec checks in the existing gate and dedicated PostgreSQL. No image build, candidate cleanup, validator container creation, service mutation, or initialization. |
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

## Preserving inactive homes during a coordinated launcher upgrade

Persistent-home availability and automatic launcher maintenance have separate
controls. For a cutover that must preserve inactive homes, explicitly set the
replacement launcher's operator-controlled configuration to:

```text
AXGT_PERSISTENT_STORAGE_ENABLED=true
AXGT_STORAGE_MAINTENANCE_ENABLED=false
```

The maintenance setting defaults to **true when absent**, preserving existing
behavior. It accepts `true/1/yes/on` or `false/0/no/off` (case-insensitive, trimmed).
Empty/invalid values fail startup and configuration checks. Compose deliberately
uses `${AXGT_STORAGE_MAINTENANCE_ENABLED-true}`, without `:`, so an explicit empty
value reaches that validation. This policy is launcher-only; tenants and session
payloads cannot override it. Older launcher images do not implement the setting
and must not be used for a storage-preserving cutover.

Disabled maintenance skips both startup capacity reconciliation (backing-image
scan, ext4 metadata probes and capacity-record upserts) and the periodic storage
billing/pruning worker, including its initial sweep, read-write size-probe
mounts and debt-triggered Docker-volume removals. Those background paths do not
delete backing images or resize filesystems; filesystem creation/growth and
loop attachment belong to the explicitly authorized session launch path.
An authenticated owner can still recover/use their matching home, record its
capacity, or request provisioning/growth. Startup does not rewrite inactive
wallets' storage mappings merely because maintenance is disabled. Session
compute accounting, capacity floors, pricing and debt thresholds are unchanged.

Do not run `scripts/prune_user_volumes.py` or independent storage jobs during
this cutover. That separate manual administrator utility is outside the launcher
policy, and its `--dry-run` still mounts volumes read-write. Session/network
cleanup removes containers/networks without deleting named wallet volumes and
remains enabled. Tenant startup/file operations affect only the selected home
under the existing session authorization rules.

Before shutdown, verify the **rendered** Compose configuration explicitly has
maintenance `false` and persistence `true` for the launcher. Record all wallet
volume names, driver/options/creation metadata, backing-image paths and
device/inode identity, logical/allocated sizes, ownership and timestamps, loop
associations and database storage mappings. Inventory unmatched/legacy entries
too; do not mount them or copy every inactive home. Before the launcher-start
checkpoint, repeat the configuration assertion and refuse startup on absence,
drift or invalid values. After startup verify its effective environment, without
printing secrets, and compare storage identities against the saved inventory.
Do not automatically re-enable maintenance after deployment.

The existing explicit provisioning path can create a missing backing image and
replace a mismatched volume definition. Review legacy/unmatched entries before
their owners launch; this policy preserves inactive homes but does not repair or
certify legacy storage. Disabling the worker also pauses sweep-based storage
charges. Explicitly re-enabling it can bill elapsed time under the existing
seven-day cap and prune at the unchanged debt limit; review balances and storage
before doing so.

Build and validate a new gate/launcher pair from the committed storage fix before
cutover. Keep private immutable image identities and production-equivalent full
builds (`AXONOS_SKIP_HEAVY=0`); do not substitute an older launcher merely because
its gate is secured. A security-only fallback should be a separately recorded
source ref containing the wallet-ownership security baseline plus only this
storage change, with its own matched image pair and isolated validation.

Prepare images, regression checks and inventories before the outage. The outage
should contain only authority shutdown verification, final database backup and
isolated restore verification, any experiment-home preservation that cannot be
finalized consistently beforehand, approved credential remediation/migrations,
pinned launcher/gate startup and minimum security/health checks. Continue broader
validation after service restoration; approximately 5–10 minutes remains a target
only when mandatory backup/preservation timings permit it.

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

## Wallet ownership security update

This release removes wallet-address-only prepaid reclaim through both public
listeners. Every ordinary-wallet session claim now requires the existing
wallet-bound auth token, including `/api/x402/session` with a signed x402 payment.
The EIP-3009 signature authorizes the USDC transfer; it does not authorize an SSH
key or session request and becomes publicly observable on-chain. Agents must
first use `/api/auth/challenge` and `/api/auth/verify-wallet`, then include
`X-AXGT-Auth-Token`. This sign-in works before funding. Payment pricing,
settlement, and credit accounting are unchanged. Unpaid discovery still returns
402, and authenticated clients can pay and claim in one subsequent request.

Both listeners authenticate before settlement/claim, recheck the token after a
long settlement wait, and issue a new token only after a granted claim. VNC
upgrades require authentication even from loopback; Flask forwards its validated
bearer on the internal connection. No new auth system or database migration is
introduced by this fix.

### Required remediation for an existing installation

Installing the code alone does **not** invalidate credentials obtained before
the fix. `axgt_auth_tokens` records wallet, issue/expiry times, status, and grace
expiry, but no ownership-proof or issuing-route provenance. An unsafe token may
already have been refreshed into a newer token through wallet-status. An
`issued_at` cutoff therefore cannot identify all affected descendants, and
ordinary token rotation leaves older credentials usable in grace. Existing
SSH/VNC/terminal sessions can outlive the wallet token used to establish them.

The following is an operator procedure requiring a separately approved
maintenance window. Adding this documentation does not execute any revocation,
session stop, database write, image rebuild, or production rollout.

1. Back up PostgreSQL and preserve protected audit evidence and wallet volumes.
   Do not print/export plaintext auth tokens or session secrets into logs.
   Explain the required sign-in renewal and possible session interruption to
   users. Unless reliable independent evidence narrows exposure, treat all
   existing ordinary-wallet tokens and sessions as potentially affected.
2. Block public traffic to **both 6080 and 8889**, including any alternate
   ingress. Pause session admissions, then stop/drain every old public gate
   worker so no in-flight request can issue or refresh a token after revocation.
   An admissions-only pause is insufficient: wallet-status can refresh tokens.
   Terminate established VNC and terminal connections by restarting the relevant
   gateway workers during this maintenance window. Keep both listeners closed
   until their patched versions and remediation are complete.
3. Through protected operator database access, revoke **all current and grace
   tokens**, not just recently issued tokens. The broad procedure also signs out
   guest/demo users; provision replacement guest access as needed. Clear unused
   terminal tickets when that table exists. Example SQL, for deliberate operator
   execution only after step 2:

   ```sql
   BEGIN;
   UPDATE axgt_auth_tokens
   SET status = 'revoked', expires_at = 0, grace_until = 0
   WHERE status IN ('current', 'grace');
   DO $$
   BEGIN
       IF to_regclass('axgt_terminal_tickets') IS NOT NULL THEN
           DELETE FROM axgt_terminal_tickets;
       END IF;
   END $$;
   COMMIT;
   ```

   Preserve credit balances, funding/payment records, and audit history. This is
   credential remediation, not a credit reversal or Migration 005 operation.
4. End potentially unauthorized active/credit-grace sessions through the
   existing operator/session lifecycle and confirm their runtime containers,
   SSH connections, terminal streams, and agents have stopped. Do not merely
   change a session's database status while leaving its runtime alive. Per-session
   `files_key` credentials authorize agents independently of wallet tokens;
   provision fresh runtime/session credentials when legitimate owners relaunch.
   Review/quarantine affected persistent homes before mounting them into a new
   runtime. Inspect SSH authorized keys, other login trust, startup hooks, and
   exposed application secrets. SSH host keys also persist under
   `~/.config/axonos/ssh`. A fresh wallet signature or overwriting
   `~/.ssh/authorized_keys` cannot undo other modifications to a compromised home.
   Rotate exposed credentials and restore trusted data according to the incident
   findings; do not automatically delete user volumes.
5. Start the patched listeners together using the deployment's established
   manual Compose procedure (the deployment controller and CAPI are not
   prerequisites). With controlled test access, verify that old tokens fail on
   **both** listeners and CARD, address-only and payment-signature-only claims
   fail without launching or minting a token, and a fresh signed challenge permits
   normal prepaid claim, x402 payment, and reconnect. Resume public traffic only
   after those checks and runtime/home review are complete.
6. Users reconnect their wallet and sign a new challenge; agents do the same
   programmatically. Existing balances and funding history remain available.
   Stop/recreate remediation can interrupt jobs, so coordinate recovery with
   owners. Clients must retain the returned auth token and renew it through the
   existing authentication flow; a public deposit hash or old payment signature
   is never a substitute for signing in.

Do not restore pre-fix auth/session tables from backup as an application rollback
step: that would restore revoked access. Keep patched ownership checks in place
or keep admissions/ingress closed if rolling back other changes. CARD's separate
pending-payment and webhook reconciliation requirements still apply.

## Optional CARD payments

CARD is an additional payment rail. Wallet signatures remain the technical
identity, Stripe represents the fiat payer, and the existing minute/credit
balance pays for compute and storage. The feature does not require CAPI or
adoption of the deployment controller described above. A deployment that still
uses manual Compose with `X_CAPI_MODE=off` can use its existing reviewed rollout
procedure; do not change its deployment topology as part of enabling CARD.
The following are operator steps after implementation review, not actions
performed by adding this documentation.

1. Back up PostgreSQL and review
   [`005_hybrid_funding.sql`](../axonos_gate/migrations/005_hybrid_funding.sql).
   The existing ledger initializer applies it automatically on first use after
   update; its role needs the same schema permissions as existing ledger
   initialization. It may also be applied manually after the existing ledger
   tables exist, using a single transaction (`psql --single-transaction`).
   Bootstrap and the migration use a transaction advisory lock so concurrent
   updated gate processes cannot race schema creation. Failed initialization
   rolls back its schema and backfill changes; the next attempt retries.
   This migration is independent of the optional X CAPI migration
   script and database. Allow a maintenance window for schema creation and the
   legacy-history backfill; existing spendable balances are unchanged.
   CARD requires the existing per-wallet container mode
   (`AXGT_USER_CONTAINER_ENABLED=true`) with a central gate separate from tenant
   desktops. Shared desktop users have passwordless sudo; that mode cannot
   safely hold Stripe secrets. Startup refuses to boot if any nonempty Stripe
   credential reaches a shared/tenant container or a central container with
   `AXGT_SSH_ENABLED=true`. Remove the credentials and recreate that container:
   `unset` and Supervisor restarts do not erase Docker's saved environment, which
   healthchecks and `docker exec` inherit. On a CARD gate, startup keeps the
   desktop disabled and idles central Jupyter, OpenCode, Ollama, and IPFS even
   when old bind overrides exist; these services remain available in tenant
   containers. Do not bypass the reviewed startup/Supervisor isolation policy.
2. In a Stripe test environment, obtain an API secret and register a webhook
   destination at `https://YOUR-AXONOS-HOST/api/payments/stripe/webhook`. Set its
   API version to **2025-08-27.basil**, matching the explicitly pinned backend
   API version. Subscribe to `checkout.session.completed`,
   `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed`, `checkout.session.expired`,
   `charge.refunded`, `charge.dispute.created`, `charge.dispute.updated`, and
   `charge.dispute.closed`. Use snapshot events. Copy that destination's signing
   secret into the private operator configuration.
3. Set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and the fixed HTTPS origin
   `AXGT_PUBLIC_BASE_URL`. Optionally set `STRIPE_MIN_AMOUNT_USD` (default `1`)
   and `STRIPE_MAX_AMOUNT_USD` (default `1000`). Review the existing
   `AXGT_USD_PER_HOUR`: CARD grants `60 / AXGT_USD_PER_HOUR` credits per USD,
   with no AXGT bonus or holder discount. A $50 purchase at $1/hour grants
   3,000 credits, including when dynamic crypto pricing is off. Checkout creation
   is capped at 10 requests per verified wallet per minute in the central gate.
   There is no separate frontend price, Stripe product setup,
   publishable key, or browser card SDK.
4. Rebuild the application image with the updated gate dependencies, then roll
   out the reviewed gate image and Compose/supervisor configuration through the
   deployment's established procedure. Both 6080 and 8889 serve these routes;
   public 6080 proxies payments to 8889 inside the central container. Keep both
   gate programs running and allow outbound HTTPS to Stripe. Preserve the raw
   webhook request body and `Stripe-Signature` at ingress; no browser login or
   interactive proxy challenge may block this exact endpoint. Apply the new
   launcher environment overrides whenever recreating the launcher; it must
   not inherit Stripe secrets from the shared `.env`.
5. In test mode, verify an unfunded wallet, select CARD in both the launch and
   top-up UI, complete hosted Checkout, and wait for webhook-confirmed credits.
   Repeat delivery of the same event and confirm only one credit entry; also
   check cancellation, failed payment, sign-in renewal after token expiry, and
   refund/dispute review state. Exercise ETH, USDC, and AXGT using their existing
   test procedures. A browser success URL alone must never change balance.
6. After reviewing test results, configure matching live API/webhook secrets
   and a live webhook destination. Keep test and live identifiers separate.
   Monitor failed webhook deliveries and the reconciliation queue. Disabling
   Checkout by removing credentials also disables webhook processing, so drain
   or reconcile outstanding payments before disabling the rail.

Stripe's [hosted Checkout fulfillment guidance](https://docs.stripe.com/checkout/fulfillment)
and [webhook destination documentation](https://docs.stripe.com/events/manage-webhook-endpoints)
describe provider delivery and retry behavior. AxonOS stores the wallet, cents,
currency, credits and pricing snapshot before creating Checkout; provider
metadata is traceability only. The webhook must match that record and prove a
completed, paid session before the database can issue credits. The transaction
locks the funding record and writes the common balance and audit entry together.
Payment/event identifiers are unique, so return-page refreshes, duplicate events,
and concurrent retries cannot issue the same purchase twice.

### Funding history and refund reconciliation

`axonos_funding_transactions` records fiat and crypto purchases separately from
the spendable balance, including wallet, payment method, actual amount, credits,
USD valuation/rate snapshot, and applicable crypto discount/bonus. Fiat records
also retain Checkout Session, PaymentIntent, customer and invoice references for
later billing integrations. No PAN, CVC or full webhook payload is stored.
`axonos_funding_events` records processed provider events and sanitized details.
Historical crypto records without recorded prices/amounts are marked incomplete;
the migration does not reconstruct them from today's rates. Existing
`axgt_deposits` and `axgt_ledger` remain the accounting authority.

Migration 005 is additive and safely repeatable; it does not use a separate
version registry or destructive down migration. Previously upgraded applications
can still read the original tables, and old crypto writers can use them, but
old writers do not populate the new funding ledger. A subsequent updated ledger
bootstrap backfills those transactions as incomplete legacy history; their
unrecorded rates and ETH/USDC amounts cannot be recovered from these tables.
Use a coordinated restart and avoid a prolonged mix of versions. Rolling back
application code after accepting CARD payments requires disabling new Checkout,
draining or manually reconciling pending Stripe payments/webhooks, and retaining
both funding tables. Old code cannot process CARD webhook retries. Do not drop
funding history or reverse already issued balances when rolling back.

Purchase quotes and funding records use Decimal/NUMERIC. The preexisting
spendable compute balance and credit audit deltas use DOUBLE PRECISION. CARD
converts the quoted amount conservatively (never increasing the nominal delta)
and records that exact delta separately from `expected_credits`. Existing
floating point balance addition still has finite precision: around 60,000
credits one step is approximately 0.0000000000073 credits; near the supported
extreme of 10^15 credits it is 0.125 credits. A future migration of the common
credit ledger would be needed for exact decimal accumulation at every scale.

Confirmed refunds record the cumulative refunded fiat amount; disputes record
their lifecycle. Both flag `reconciliation_required`, with no automatic debit of
compute credits. This prevents a refund of already consumed compute from
silently creating storage debt or pruning a wallet's data. It also means an
operator must assess outstanding credit and payment exposure promptly. If a
refund/dispute is known before credit issuance, the pending purchase is held for
reconciliation and receives no automatic credits. Review
the queue using the existing protected database access:

```sql
SELECT id, wallet_address, payment_method, status, credits_added,
       refunded_amount, stripe_payment_intent_id, updated_at
FROM axonos_funding_transactions
WHERE reconciliation_required = TRUE
ORDER BY updated_at;
```

After checking the Stripe record, funded credit, consumed compute, and current
wallet balance, reconcile deliberately through the existing authenticated admin
credit/balance adjustment APIs with an audit note referencing the funding ID.
Do not blindly debit the original purchase amount. Record the review outcome
and clear the flag through the protected operator database workflow; there is
no automated refund debit or new reconciliation dashboard in this change.
Pending/failed refunds do not count as refunded settled funds. Conventional tax
calculation, invoice issuance and institutional payer management remain future
work; customer and invoice references provide the integration points.

Stripe secrets are server-only, absent from public configuration and responses,
and explicitly cleared from launcher, desktop and assistant environments. Keep
`.env` private; do not print resolved Compose configuration or secret values.
CARD disappears cleanly if required configuration is absent, while existing
crypto rails remain available.
