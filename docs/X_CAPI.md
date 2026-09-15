# Privacy-minimized X Conversion API

## Status and protocol decision

The implementation is staged and defaults to `X_CAPI_MODE=off`. It has not
been migrated, deployed, activated, or tested against X. The operator must create a
new conversion source and supply the exact Events Manager source/Pixel ID and
exact IDs for `wallet_verified`, `deposit_completed`, and `session_started`.
IDs are copied verbatim; code never creates or rewrites them.

**Real live collection and delivery are structurally disabled.** The reviewed official X
materials provide no normative mechanism for proving the authenticity or
campaign provenance of a `twclid`. Consequently the normal worker invocation
(`transport=None`) returns
`live_delivery_blocked_untrusted_twclid_provenance` before opening any delivery
credential, claiming a row, or constructing an HTTP request. The gate also
treats `live` as producer-unready, so it cannot mint a grant or persist a raw
click ID merely because an operator selected an unavailable mode. Live
readiness is always false with that explicit reason. Environment settings
cannot override this.
Only an explicitly injected non-production transport together with the
isolated-test secret guard can exercise retry/idempotency code. `off` and
`dry_run` remain available for staging. Enabling real delivery requires a new
reviewed provenance contract and code change; it is not an operator toggle.

A dedicated Conversion API token is confirmed in this account's X Events
Manager configuration. The transport therefore implements X's official server-side
GTM token contract: `POST https://ads-api.x.com/12/measurement/conversions/{pixel_id}`,
`Content-Type: application/json`, and `X-Pixel-Token`. Its four-field JSON uses
`conversion_timestamp` as integral Unix milliseconds, preserving the original
event time across retries. It does not implement OAuth or `conversion_time`.
This account-specific authentication decision does not resolve the separate
click-provenance activation block above. The normal worker, direct adapter,
producer readiness, and Compose network/mount configuration remain blocked.
Automated tests replace HTTP with a fake and use generated synthetic tokens;
no real token is needed, read, or submitted during testing.

The adapter reads only the existing protected worker secret file
`/run/secrets/x_capi_access_token` (or `X_CAPI_ACCESS_TOKEN_FILE` below
`/run/secrets`) at dispatch. It rejects environment tokens, symlinks, multiple
hard links, non-regular files, unexpected owners, permissive modes, non-ASCII
or whitespace bytes, and invalid sizes. No delivery credential is mounted by
the current overlay. Future approved provisioning must mount it read-only to
the worker alone, owned by UID 10001 with mode 0400 or 0600 and no trailing
newline; never place the token in an environment variable or checkout.

Transport submission, an API response reporting `conversions_processed`, ad
attribution, reporting, and optimization are separate outcomes. Local/mock
acceptance is not evidence of X ingestion or campaign attribution.

Primary references:

- <https://github.com/twitter/x-ads-conversion-api-gtm-template/blob/e637e1c4af64d3b4ff1a4da9b97e950d13ad5dc9/template.tpl#L297-L368>
- <https://github.com/twitter/x-ads-conversion-api-gtm-template/blob/e637e1c4af64d3b4ff1a4da9b97e950d13ad5dc9/README.md>
- <https://docs.x.com/x-ads-api/measurement/web-conversions>
- <https://docs.x.com/x-ads-api/fundamentals/making-authenticated-requests>
- <https://business.x.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites>
- <https://business.x.com/en/help/ads-policies/campaign-considerations/policies-for-conversion-tracking-and-custom-audiences>

## Data flow and boundaries

```text
landing ?twclid=… -> app scrubs URL synchronously -> module-private memory only
   -> app-owned Allow / Decline
   -> status seals only a keyed commitment into an opaque memory-only ticket
   -> action-only POST + raw-click header + exact Origin + CSRF + opaque context
   -> successful wallet signature + auth token durably binds that context
   -> authoritative business transaction snapshots eligible event
   -> Postgres outbox (stable UUID/timestamp/event/source/click snapshot)
   -> isolated worker rechecks consent/expiry -> live delivery structurally blocked
```

The implemented, activation-blocked request payload is constrained to:

```json
{
  "conversions": [{
    "conversion_timestamp": 1789387200000,
    "event_id": "exact-events-manager-id",
    "identifiers": [{"twclid": "consented-click-id"}],
    "conversion_id": "opaque-stable-uuid"
  }]
}
```

Wallets, transaction hashes, auth/session capabilities, IP address, user agent,
email, phone, `twpid`, URLs/referrers, GPU/profile/workload fields, notes,
commands, files, clipboard, SDP/ICE, and streaming telemetry remain internal.
Logical wallet/business keys are domain-separated, worker-keyed HMAC values in
the integration tables and never
leave AxonOS. Tokens never enter database jobs.

The intended future payload is nevertheless linkable: one `twclid` can connect
multiple milestone types, and each payload carries an exact event timestamp.
A recipient could correlate a deposit timestamp with public-chain activity and
thereby infer a wallet indirectly. This is a residual property of the requested
conversion signal, not a direct wallet field; it is one reason this build keeps
live collection and delivery structurally unavailable pending separate privacy
approval.

No consent, click ID, current binding, enabled mapping, or eligibility means
skip with a reason counter. There is no alternate identity, fingerprinting, or
wallet-history join. Headless/x402 activity therefore remains unreported unless
it originated from the same explicit, authorized browser context (the current
x402 agent path supplies none). Internal operational telemetry is unchanged.

## Consent and landing-page handoff

The app is authoritative. It shows equally usable Allow and Decline actions and
remains usable after denial. Global Privacy Control (`Sec-GPC: 1` or
`navigator.globalPrivacyControl`) forces denial. A `consent=true` query value is
ignored. The privacy control remains available for later revocation.

The raw `twclid` global used by the synchronous URL bootstrap is consumed and
deleted by the bridge before any later script runs. It is sent once to the
first-party status endpoint only when no context exists; the server seals only
a keyed commitment, never the recoverable raw click, in the authenticated opaque
ticket. Grant sends the same module-private value again in the dedicated header
so the server can verify the commitment; its JSON body remains action-only and
cannot replace the click. A new lifecycle likewise sends that immutable landing
candidate only on its explicit transition. A live memory-only unset context may
repeat the same header on a status revalidation (for example, a page-cache
resume); the backend accepts it only when it matches the existing commitment.
A visitor with no landing click, context, revoke capability, or GPC performs no
attribution request and creates no tab identifier.

An unset/pre-consent context is memory-only, so reload before a choice
deliberately loses attribution. Only a granted or closed/revocable ordinary
context may be tab-scoped in `sessionStorage`; raw `twclid` is never stored
there. The browser initially keeps every handle private: wallet, deposit, and
session requests cannot see it until status, current consent, GPC, and tab
ownership have been checked. A browser-enforced Web Lock detects storage cloned
by a duplicated/opener-created tab. The new tab discards the copied business
handle; browsers without that arbitration primitive keep ordinary contexts in
memory only rather than risk cross-tab reuse. A distinct
server-encrypted, CSRF-bound revoke-only capability survives reload and cloned
storage but is never published to business request builders. It is cleared only
after a successful response explicitly confirms `state=revoked`, so a failed
revocation stays retryable. The server stores only handle/CSRF hashes.

A successful ownership proof binds an unbound context. If wallet verification
and asynchronous consent restoration finish in the opposite order, a bounded,
fire-and-forget, authenticated first-party bind request closes the race. It is
once per wallet/context in the page, excludes clicks and CSRF, is worker-deduped,
and can never delay or fail auth or launch. Another wallet cannot inherit the
binding. A genuinely new lifecycle requires a separate explicit "Start a new
choice" transition (or a fresh tab), then a new grant and wallet verification;
it retires rather than mutates the old lifecycle. Logout does not silently move
the binding. Token rotation does not alter it.

Touch policy is first consented click per tab/context. Repeated visits do not
extend that click's expiry. The consent UI renders the authoritative lifetime
returned by the server rather than promising a hard-coded duration. A later
click is not silently substituted. Jobs contain an immutable event-time
snapshot, so later navigation never rewrites a queued event. GPC and an explicit
revoke suppress the public context immediately and persist a local revocation
intent before any asynchronous work. A server status of `gpc_applied` or
`revocation_required` is authoritative even when the browser JavaScript property
is absent; stale status/update responses are aborted and generation-fenced so
they cannot restore sharing or UI. If server-side deletion/cancellation cannot
be confirmed, the UI remains visibly pending and offers a retry even after a
status outage; it never reports success.
Already transmitted or in-flight requests may not be recallable.
The bridge also rechecks the browser GPC property on visible-page and page-cache
resume events. A newly enabled signal latches one-way suppression immediately;
it creates no state or polling traffic for an idle visitor, while an existing
private capability schedules its durable revocation.

External landing contract:

1. After its own approved ad-consent flow, preserve X's unchanged, URL-encoded
   `twclid` when navigating to the exact app entry origin, e.g.
   `https://app.example/?twclid=<value>`.
2. Do not add a client `consent` assertion or signing secret. Do not store the
   ID in localStorage or unrelated analytics.
3. Configure and test CDN/reverse-proxy access-log query redaction before
   rollout, including mixed-case and percent-encoded parameter names. The app
   and Websockify canonicalize and redact `twclid`, `x_capi_context`, and
   `x_capi_handoff`, but an externally managed CDN/proxy sees the first request
   before app code.
4. The app synchronously canonicalizes query names, scrubs mixed-case and
   repeatedly percent-encoded aliases, accepts only one literally named
   `twclid`, rejects duplicate or implausible values, and removes every
   attribution/control parameter via `history.replaceState` before loading
   unrelated resources. If parsing or rewriting fails it replaces the URL with
   `pathname+hash`, drops the entire query, and refuses to parse the unsafe query
   again while replacement is pending. A page-wide `no-referrer` policy prevents
   the original query from propagating as a subresource referrer.

Neither bridge errors nor the bind request are logged with their headers or
payloads. Browser, gate, and Websockify query redaction does not protect the
first request as seen by an upstream CDN/reverse proxy; canonical redaction must
therefore be configured and verified at every upstream access-log boundary.
Upstream request-header logging must also be disabled or explicitly redact
`X-AxonOS-Landing-Click`, `X-AxonOS-Attribution`, `X-AxonOS-CSRF`, and
`X-AXGT-Auth-Token`; the commitment protocol does not make the transient landing
header safe to record.

The marketing/Framer site is outside this repository and was not changed.
Until its handoff and proxy redaction are installed and browser-tested, full
landing-to-deposit attribution is **not end-to-end validated**. Direct-to-app
entry is covered by local contract tests only.

## Business milestone semantics

- `wallet_verified`: a successful wallet ownership proof plus
  successful auth-token issuance under a valid bound context. A no-credit
  `verified:false` sign-in still qualifies. The keyed wallet dedupe prevents
  refresh/relogin repeats during its 90-day privacy-retention window; a wallet
  returning after that window can count again. AxonOS has no durable
  account-created concept, so this is not called signup/account creation and
  cannot prove the wallet is new.
- `deposit_completed`: the committed ledger transaction for `credit_source`
  `onchain`, paid rail `axgt|eth|usdc`, and positive chain block only. The
  verified-deposit primary key and CAPI dedupe key suppress callbacks/retries.
  Test, guest, demo, admin, sentinel, pending, failed, and rolled-back activity
  is excluded. This is the recommended primary paid signal.
- `session_started`: only after external tenant-container spawn and the DB
  transition to `allocation_status='allocated'`. Reservation, allocation
  failure, reconnect, resume, WebRTC signaling, reload, heartbeat, restart, and
  guest/demo sessions do not count. Distinct allocated session rows can count.
  All browser launch paths delegate to one attribution-aware claim builder; a
  page-level claim response is handed to the viewer instead of issuing a second
  claim with different headers or launch options.
  Legacy shared-desktop mode has no authoritative spawn-finalization milestone
  and is intentionally unsupported for reporting.

`session_started` is activation, not a purchase. Existing balances mix funding
sources and there is no credit-lot accounting. `X_CAPI_SEND_VALUES` remains
false: minutes, token quantities, bonuses, and free-text notes are not typed
USD revenue. Deposit funding is not necessarily recognized revenue.

## Delivery and retention

Authoritative business commits make one bounded, nonblocking Unix datagram
attempt. The gate never opens the CAPI database and never waits for X. A full or
unavailable local socket can lose marketing telemetry, by design; it cannot
roll back authentication, money, billing, launch, WebRTC, or heartbeat work.
GPC is the privacy-only exception to the datagram-only producer rule: before
business validation, a valid signed lifecycle is written to fixed preallocated
local controls with nonblocking locks and no fsync. Failure is swallowed by the
business route, but the same missing/corrupt controls make worker dispatch fail
closed.
Consent grants are stateless. Only a credential-authenticated wallet proof or
an authoritative committed paid-deposit event with verified on-chain
provenance may allocate and bind a durable attribution context. A `/bind`
handoff binds only; it never fabricates a wallet conversion. Session events
require an already-bound context and never bind.

No real conversion is sent in this build. In isolated tests, one synthetic
conversion is processed per injected fake-transport call. A claim atomically
installs both a worker owner and a random per-claim lease token with `FOR UPDATE
SKIP LOCKED`. Before serialization the worker rechecks that exact fence and the
immutable mode, policy version/epoch, deployment audience, click, event
timestamp, and lifecycle fields, then commits before invoking the fake. The OS
dispatch lock—not a PostgreSQL row lock—spans that call. Every completion is
conditional on the same owner/token.
Revocation takes the same rows, cancels all unsent and dry-run work, and clears
identifiers. A dedicated consent-service thread owns a separate, short-lived
worker-role DB connection, so a close committed before the dispatch lock wins;
it never shares a psycopg connection with delivery. If dispatch already holds
the lock, the event is explicitly in flight and the close returns failure
rather than ambiguous success. An absent-row close creates a bounded,
identifier-free handle tombstone so a queued old granted ticket cannot bind
after revocation. If that bounded table is full, the singleton capacity row
instead extends a monotonic, durable fail-closed deadline through the unstored
ticket's expiry. New contexts, new outbox work, claims, and the final dispatch
check remain blocked while saturated. Cleanup can reopen only after both that
deadline has elapsed and tombstone capacity is available, so freeing an older
tombstone cannot resurrect a later denied lifecycle.

The delivery state machine treats an ambiguous result after hypothetical
remote acceptance as an at-least-once retry. The stable conversion UUID and
original event time survive SIGTERM, SIGKILL, OOM, and lease reclaim. This is a
local correctness property, not evidence of remote idempotency or end-to-end
exactly-once delivery. A clean shutdown additionally removes the ingest
pathname, drains datagrams already visible in the receive queue, and records
diagnostic CLEAN. Core event producers deliberately take no CAPI filesystem
lock, so a sender connected at the unlink boundary can be dropped; optional
attribution loss is preferred to holding auth, payment, or session responses
behind worker shutdown. The bounded preallocated GPC fence is the privacy-only
exception and never controls the business response.
An absent/DIRTY lifecycle remains diagnostic and does not discard queued work.
Complete targeted privacy tuples are synchronously published into preallocated
slots and consumed before dispatch. Because that hot path intentionally does
not fsync, a host power loss before the page cache reaches durable storage is a
documented residual; process-only crashes preserve the tuple and pending work.

The retained response classifier treats timeouts, connection failures,
408/425/5xx, and 429 as bounded jittered retries; validated `Retry-After` is
capped at one hour. It models 401/403 as a pause and refuses unrecognized 200
bodies. This remains deliberately stricter than the GTM template's acceptance
of 2xx/3xx without body validation; redirects are rejected and never followed.
`RequestsTransport.send` first enforces the existing live-readiness block, then
independently checks the exact payload keys, configured pixel/event IDs, click
validation, canonical UUIDv4 conversion ID, integer timestamp and runtime file
token. The standard-library HTTPS client verifies TLS and does not use ambient
proxies, netrc, cookies, default user-agent headers, or automatic retries. It
disables HTTP wire debugging and rejects `SSLKEYLOGFILE`, `SSL_CERT_FILE`, and
`SSL_CERT_DIR` overrides; the TLS client uses the container's default trust store.
The client reads at most 16 KiB plus one byte and retains only `Retry-After` from response
headers. Bodies, credentials and client exceptions are never logged.
Socket operations have a five-second timeout. A separate 20-second kernel
SIGALRM deadline terminates a wedged worker, including blocked DNS or slow-drip
responses. This is a process crash: the existing lease recovery retries the
same conversion after restart. No background sender survives release of the
privacy lock. Delivery requires the main thread and refuses an existing alarm
or a blocked `SIGALRM` signal.
The current activation block is unchanged and these paths are tested with
fake HTTP only. Database statements, locks, queue size,
cleanup batches, and per-worker pacing are bounded. Queue admission uses
an O(1), transactionally maintained singleton counter rather than scanning the
outbox. Events age out at 24
hours by product default without changing their timestamps. Terminal outbox
rows purge after 30 days, keyed dedupe entries after 90 days, and context
tombstones no sooner than 91 days. Revocation tombstones purge only after their
immutable lifecycle expires. Click IDs are nulled earlier on
acceptance/failure/expiry/revocation. Align these defaults with the approved
campaign/privacy policy before live activation; they are not claims about
X-mandated windows.

The worker is the only runtime process with the dedicated CAPI PostgreSQL
login and hash key. No X delivery credential is mounted in this blocked build.
It has no connection to the core PostgreSQL
service or `axonos_control`, Docker socket, tenant, GPU, or media networks. The
overlay provisions a separate pinned PostgreSQL container on an internal-only
database network; its bootstrap credential is mounted only into the one-shot
database initializer. The shared gate receives only the context encryption key
and local runtime/privacy volumes. The privileged launcher and tenant
containers receive none of these values.
Both the grants migration and worker readiness compare the complete pinned
PostgreSQL-15 catalog against the reviewed v4 schema: relation kinds, ordered
columns/types/defaults, checks/keys/FK, indexes/predicates, internal FK triggers,
and absence of user triggers, rules, RLS policies, inheritance, unexpected
relations, and logical publication. A version-4 label alone is never trusted.

The current worker joins only the internal dedicated-database network and has
no Internet-capable Compose network or third-party HTTP client dependency. A future live
implementation must additionally enforce outbound TCP/443 to the reviewed X
API destination through a separately managed allowlisted firewall/proxy and
prove that every other Internet/private destination is denied. Docker bridge
naming and application host checks alone would not be a destination allowlist.

This version is a **single-host, single-worker Compose design**. Its final
privacy/dispatch fence and listener singleton use host-local files and Linux
`flock`; they are not a distributed lock. Do not scale the worker, share the
dedicated database with a worker on another host, or deploy the gate and worker
with non-shared runtime/privacy volumes. A future multi-host design must replace
the local boundary with a reviewed distributed fence and a trustworthy common
clock before any live transport is added. Off/dry-run staging must retain the
one-worker topology shown here.

After reading or advancing the database-backed configuration high-water, the
worker atomically publishes `/run/axonos-x-capi/config-guard.json` as a
worker-owned mode-0600 canonical JSON attestation. It contains only version,
configured state, maximum policy epoch, deployment-ID hash, dry/live mode,
policy version, audience-scope hash, and a one-way primary context-key
fingerprint—never raw pixel/event/click IDs or a secret. The worker-only HMAC
key fingerprint remains solely in PostgreSQL. A generation advance first replaces it with a fail-closed
`configured:false` marker, commits the database change, then publishes the new
durable row. Thus a crash cannot leave an older generation attested. The gate
must reject active attribution unless this file exactly matches its current
validated scope; a rejected rollback still republishes the durable high-water.

## Operator runbook (do not execute without approval)

1. This overlay uses a new, dedicated PostgreSQL data volume and database; it
   never migrates or joins the existing AxonOS production database/network.
   Back up the dedicated volume before every later schema/image change and keep
   `X_CAPI_MODE=off` until postflight succeeds. There is no down migration.
   A pre-v4 X CAPI database is deliberately **not** upgraded in place: preserve
   its backup for audit, provision a fresh dedicated v4 database, and cut over
   only after validation. Do not copy old click IDs, contexts, dedupe keys, or
   pending jobs into v4, because doing so could re-export identifiers or replay
   conversions. The runner accepts only a fresh schema or exact version 4.
2. Database bootstrap creates exactly two application roles: a NOLOGIN owner
   and a distinct least-privilege LOGIN worker. The public gate has no CAPI DB
   credential or grants. For manual DBA execution against the dedicated
   database, authenticate as its dedicated bootstrap superuser with
   `PGPASSFILE` and pass exactly two role names:

   ```sh
   sh axonos_gate/migrations/apply_x_capi_migrations.sh \
     axonos_x_capi_owner axonos_x_capi_worker
   ```

   The standalone runner resolves `current_user` and `current_database()` and
   runs the same read-only dedicated-cluster/database/object attestation before
   its role preflight or any schema/ACL mutation. It therefore refuses the core
   AxonOS production database even when no `x_capi_*` table exists there.
   Normal Compose deployment runs the same script through
   `x-capi-db-init`; owner/bootstrap credentials are not present in the runtime
   worker. Before its first persistent change, the initializer requires the
   exact configured superuser/database, PostgreSQL 15, only the stock
   administrative databases plus the CAPI database, only the expected roles,
   and no unrelated user schema/object/default state. It then runs the fresh or
   exact-v4 catalog preflight read-only. Public database ACL changes are bounded
   to the configured database plus `postgres` and `template1`; there is no
   open-ended cluster sweep. A new worker login has no usable password, and an
   existing password is not rotated until every schema/ACL postflight succeeds.
   `001` refuses every pre-existing reviewed object,
   `004` rebuilds and exactly postflights owner/table/column ACLs, and all
   connections/statements/locks have fixed time ceilings and disable password
   prompts. If any stage fails, leave the feature off, inspect the dedicated
   database, and rerun only after correcting the cause. Never point the scripts
   or tests at the core production database.
3. Provision five single-link regular secret files outside the repository/build
   context. The worker DB URL, Fernet context key, and HMAC key must be readable
   only by worker UID/GID `10001` (`0400` or `0600`). The bootstrap and worker
   database-password files must be root-owned (`0400` or `0600`) and are mounted
   only into PostgreSQL/database-init as appropriate. None may contain a trailing
   newline or non-visible-ASCII byte. The DB URL hostname must be
   exactly `x-capi-postgres`, its database/username must match the configured
   dedicated DB and worker role, and its password must be URL-encoded.

   Compose named volumes `axonos_x_capi_runtime` and
   `axonos_x_capi_privacy_fence` hold sockets, the monotonic guard attestation,
   fixed publisher controls, and deny markers. Do not replace, independently
   restore, prune, chmod, or mount these volumes into tenants/launcher. The
   one-shot privacy initializer safely provisions UID `10001`; operators must
   not create host runtime directories or control files by hand. Only the
   context-key bind and these two volumes are shared with the gate. DB URL,
   HMAC key, and bootstrap/worker DB password files remain worker/DB side only.
   `.dockerignore` excludes conventional secret paths but is not a
   substitute for keeping secrets outside the checkout.
   A core event producer performs only bounded in-memory
   validation/serialization and one nonblocking Unix-datagram connect/send
   after its authoritative commit; it does not read CAPI files or acquire CAPI
   locks. A missing, full, corrupt, or restarting worker drops optional
   attribution and cannot roll back or wait behind the core operation. Graceful
   worker shutdown removes the socket pathname, drains records already visible
   to the receiver, and marks the lifecycle diagnostically clean; an emission
   racing unlink may be dropped.

   Every GPC-bearing business request with a valid signed lifecycle is handled
   before business validation and additionally writes a complete targeted
   revoke tuple into one of 64 fixed preallocated slots while attempting the
   dispatch boundary with `LOCK_NB`. The worker applies stable slot records and
   cancels retained dry-run/outbox work before any later dispatch, even if its
   socket/process was unavailable when GPC arrived. If contention, deterministic
   slot collisions, or capacity prevent that targeted write, the producer flips
   the single preallocated global control without waiting or fsync; if that
   write cannot complete, it poisons the preallocated dispatch magic. Either
   state blocks all later marketing dispatch until the reviewed higher-epoch
   recovery procedure. This deliberately lets a holder of a valid lifecycle
   pause the bounded marketing subsystem under capacity pressure; it cannot
   allocate files/rows or fail the auth, payment, billing, session, WebRTC, or
   heartbeat request. A send that acquired the dispatch boundary before GPC is
   already in flight and cannot be retroactively withdrawn.

   Early best-effort GPC handoffs on dedicated attribution endpoints use the
   same targeted slots and can return unavailable when the pool is full; the
   frontend keeps GPC latched and retries. Once an explicit decline/revoke has
   authenticated its ticket, CSRF value, action, and origin, its pre-fence uses
   the same bounded global fallback as an ordinary GPC request. Even then the
   endpoint reports success only after the worker acknowledges the committed
   database close. A per-capability limiter can suppress only the redundant
   wake-up datagram; it cannot suppress fixed-slot publication or the
   worker-confirmed explicit revoke.

   Neither independent nor coordinated snapshot rollback is a supported
   recovery mechanism. Restoring the database, runtime attestation, and privacy
   volumes together to an older matching snapshot can resurrect a conversion
   accepted after that snapshot and erase a later revocation. Stop the worker
   before any restore; afterward require a reviewed strictly newer policy epoch
   and the destructive global-quarantine recovery procedure before dispatch is
   re-enabled. A genuinely non-rollbackable external high-water anchor is
   required if operators need automatic safety across whole-stack rollback.
   Treat the HMAC key as immutable for the lifetime of every retained context,
   outbox, and dedupe row. The worker pins a one-way key fingerprint in the
   database guard and rejects readiness, ingest, and dispatch after replacement,
   even at a higher policy epoch. Recover the original key from the approved
   secret backup; rotation requires a separately reviewed destructive data
   retirement/migration procedure.
   Context-key rotation is different: place the new primary first and retain
   every decrypt-only predecessor until its last 90-day lifecycle plus clock
   skew has expired. Changing the primary requires a strict policy-epoch
   advance and durable worker attestation; reordering back at the same or a
   lower epoch fails closed. Removing a predecessor early prevents holders of
   its old capability from authenticating a revoke.
4. Configure an explicit deployment ID, exact origin/pixel/event IDs, positive
   monotonic consent-policy epoch, production chain IDs, context/queue caps, and
   the vendor-confirmed click-ID contract version, character set, and length
   bounds. Changing origin, pixel, deployment ID, contract, or epoch changes the
   cryptographic audience scope and stales old contexts; never lower/reuse an
   epoch. `X_CAPI_PRIVACY_RATE_LIMIT_PER_MIN` controls only redundant,
   non-blocking worker wake-up datagrams. It never gates publication of the
   complete fixed privacy fence or the worker-confirmed explicit revoke: a
   shared/global privacy admission cap would let one visitor deny another's
   withdrawal.
   The first active worker validation durably registers this complete scope in
   `x_capi_config_guard`. Its policy epoch is an irreversible high-water mark:
   never lower it, and increment it for any deployment, dry/live mode, policy,
   origin/pixel/event mapping, attribution window/chain, or click-ID contract
   change. Reusing an epoch with different scope and rolling 2→1 both fail
   readiness, ingest, and dispatch. `off` is a non-collecting kill switch and
   does not advance the guard.

   A malformed/partial publisher slot, replaced privacy volume, or marker
   overflow intentionally activates global quarantine. Recovery is destructive
   to attribution state and requires approval: choose a strictly higher policy
   epoch (which invalidates every old stateless ticket), update the complete
   scope, set `X_CAPI_PRIVACY_RECOVERY_EPOCH` to that exact epoch for one worker
   start, confirm the guard/controls are healthy, then remove the recovery
   variable and recreate only the worker. The gate attestation stays
   `configured:false` until the database advance, cancellation, and
   fixed-control reset finish under the dispatch lock. Never clear files or
   lower/reuse an epoch manually. A crash during recovery remains fail closed
   and may require another reviewed epoch advance.
5. Validate the merged overlay before changing containers:

   ```sh
   docker compose --project-directory . \
     -f docker-compose.yml -f docker-compose.x-capi.yml \
     --profile x-capi config --quiet
   docker compose --project-directory . \
     -f docker-compose.yml -f docker-compose.x-capi.yml \
     --profile x-capi pull x-capi-postgres x-capi-db-init
   docker compose --project-directory . \
     -f docker-compose.yml -f docker-compose.x-capi.yml \
     --profile x-capi build x-capi-privacy-init x-capi-worker
   ```

   Inspect the rendered mounts, environment, networks, users, images, and
   secret paths before start. The overlay adds `x-capi-postgres`,
   `x-capi-db-init`, `x-capi-privacy-init`, and `x-capi-worker`; it does not
   attach the worker to or modify the base `postgres` service, its volumes, or
   `axonos_control`. Never use a profile-wide unnamed `up` during rollout.
   Start only the named CAPI services, preserving their dependency ordering:

   ```sh
   docker compose --project-directory . \
     -f docker-compose.yml -f docker-compose.x-capi.yml \
     --profile x-capi up -d \
     x-capi-postgres x-capi-privacy-init x-capi-db-init x-capi-worker
   ```

   Do not use `--no-deps` for the worker: it must wait for database health,
   successful schema/role initialization, and privacy-volume provisioning.
   Enabling the feature also requires a separately scheduled central-gate
   recreation to add only its context-key/runtime/privacy mounts. Use the
   normal gate rollout command with the same two Compose files and
   `--no-deps axonos`; the gate has no dependency on worker/database readiness,
   so a failed optional CAPI deployment cannot prevent core startup.
   This is intentionally one host and one `x-capi-worker`; do not use Compose
   scaling, another project/host against the same database, or independent
   runtime/privacy volumes.
6. Stage the newly built worker and gate images. The frontend and producer
   hooks require the central gate image to roll; check active
   sessions and use the normal minimal-interruption deployment procedure. Do
   not rebuild or recreate tenant desktop images solely for the worker.
7. Leave `X_CAPI_MODE=off`; run
   `python3 axonos_gate/x_capi_cli.py validate`, `status`, and `demo-payload`.
   `status` reports only aggregate queue/reason counts and readiness. Confirm no X
   requests and that core auth, deposit, session, WebRTC, and
   credential-boundary tests pass. A configured `live` mode must report
   `live_ready=false` and
   `live_delivery_blocked_untrusted_twclid_provenance`.
8. Use `dry_run` with synthetic browser
   fixtures, inspect only sanitized status/counters, then purge test contexts.
   Dry-run rows are permanently `mode_scope='dry_run'` and never become live.
9. Do not provision or mount an X delivery credential in this build. The
   dedicated-token HTTP adapter is implemented and fake-tested, but the
   independent provenance and network activation gates remain unchanged.
   Future approved provisioning uses the worker-only secret file described
   above; no OAuth credentials are required for this account-specific path.
10. Do not switch to live or submit a canary in this build. Before any future
   live activation, obtain and independently review a normative click-ID
   authenticity/provenance contract, implement a non-operator-overridable
   verifier, repeat the privacy/security review, obtain privacy/legal and account-owner
   approval, and enforce a tested destination-restricted egress policy for
   `ads-api.x.com:443`. Verify Events Manager ingestion separately from
   attribution. Do not invent a test flag, fabricate a purchase, assume sandbox
   support, or spend funds.
11. Monitor pending/retrying/expired/failed/skipped counters, worker pause state,
   DB/storage pressure, and ordinary auth/payment/session health. A processed
   request alone is not proof of attribution or optimization.

Emergency kill switch and rollback: set gate and worker `X_CAPI_MODE=off`. The
worker performs local expiry/retention maintenance but cannot send; stopping
only `axonos_x_capi_worker` also stops that maintenance and synchronous
revocation acknowledgements. The dedicated database may remain running for
forensics/rollback, but never expose its internal network or port.
Keep the configured non-secret exact origin while retained contexts exist so
the app's context-bound, exact-origin revocation remains available. Roll back the gate image through the
normal staged deployment; do not drop integration tables, ledger/business data,
or dedupe rows. Token rotation changes only the mounted worker file and worker
process; after an authentication pause, run `x_capi_cli.py clear-pause` once the
new file is mounted. It never changes queued IDs/timestamps. Source/event-map changes need
an explicit disposition for existing queued rows (drain, expire, or cancel);
the worker always uses each row's frozen source/event IDs and never remaps them.

The existing privileged Docker-socket launcher remains host-root-equivalent:
blank environment variables and absent mounts prevent routine distribution but
cannot protect worker secrets against a launcher or host compromise. If that is
inside the deployment threat model, place session orchestration behind a
constrained Docker API/authorization boundary or separate daemon/host before
enabling X CAPI.
