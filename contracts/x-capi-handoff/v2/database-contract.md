# Worker-authoritative handoff v2: database contract

Status: contract artifact for review and isolated synthetic implementation. Central custody and Policy B with a 30-second maximum freshness horizon and 10-second renewal target are provisionally approved for synthetic work only. This document does not authorize a migration, deployment, production collection or X delivery.

`MUST`, `MUST NOT` and `SHOULD` describe requirements on a future implementation. Tables described here do not exist at inspected backend commit `004aba17456f3a107ee20f8d3f635ca638f29b54`. Existing implementation facts and evidence appear at the end.

## Data ownership and common types

The existing isolated CAPI worker owns all source identifiers, app transactions, redemption, imported contexts and conversion eligibility. The public gate is a strict, bounded request/RPC adapter and receives no CAPI database credential. Landing owns only first-party browser-control state and its durable withdrawal queue; it MUST NOT persist raw `twclid`, raw app cookie `A`, raw transaction capability `H`, wallet identifiers, conversion facts or existing CAPI keys.

All new lifecycle records carry `generation UUID` and `policy_epoch BIGINT`. Generation is an explicitly configured UUID, not a timestamp. Epoch is an integer in `1..2147483647`; both must match the current, nonretired worker generation. Neither an HTTP caller nor a database restore can authorize a generation advance. The existing CAPI policy epoch and audience guard remain authoritative. Worker child tables have composite foreign keys `(generation, policy_epoch)` to a corresponding unique generation/epoch key. Wire `epoch` maps to database `policy_epoch`.

All new `*_ms` values are integer Unix milliseconds in `1..9007199254740991`; `NULL` is allowed only where specified. All new nonnegative counters/versions are `BIGINT` constrained to `0..9007199254740991`, with stricter positive/cap checks where specified. An increment that cannot be represented quarantines positive authority rather than wrapping, resetting or dropping an accepted close. JSON numbers representing these values MUST be integers; booleans, numeric strings, fractions, NaN and infinity are invalid. PostgreSQL uses `BIGINT` with the same range check. Canonical service request JSON contains no floating-point values.

`hash32` means `BYTEA CHECK(octet_length(value)=32)`. Wire hashes are exactly 64 lowercase hexadecimal characters and decode to `hash32`; a database hash column does not store the 64-character wire text. Ownership/capability hashes use exactly the purpose prefixes in `cryptographic-formats.md`; generation/epoch is additionally bound through keys and authenticated messages, not an invented extra hash concatenation. Landing wire `owner_hash` maps to database `landing_owner_hash`; app wire `browser_owner_hash` maps to `app_owner_hash`. Neither hash is accepted as a substitute for the browser cookie from an ordinary browser request.

`request_id UUID` is UUIDv4. `request_digest hash32` is exactly SHA256(CanonicalJSON(operation request body)); source bodies already contain `op`. There is no additional undocumented prefix or concatenation. Method/path/purpose separation belongs to the service MAC contract. An existing request ID in its documented operation/owner scope with a different digest is `409 IDEMPOTENCY_CONFLICT`. Raw high-entropy capabilities are 32 random bytes encoded as 43 unpadded base64url characters. `transaction_hash` is the domain-separated hash of raw `H`; neither raw `H` nor a reversible copy is stored.

Runtime database roles MUST NOT own schemas, create roles/tables/functions, change grants, bypass row controls, access non-CAPI data or administer backups. Migration ownership is separate. Existing catalog/privilege attestation must be extended to exactly the reviewed schema 5.

## Time definitions

The synthetic source window is seven days with a ten-second conservative uncertainty margin:

```text
source_expires_at_ms = observed_at_ms + 604800000 - 10000
observation_accept_before_ms = observed_at_ms + 120000 - 10000
```

The receipt MUST be accepted before `observation_accept_before_ms`; equality is expired. The supported deployment assumption is a measured absolute clock error of at most five seconds on each relevant host. The ten-second margin covers the maximum relative error. Unknown/excessive uncertainty closes positive attribution. Receipt timestamps do not prove an actual X ad click.

Landing consent must follow the observation: `observed_at_ms <= landing_consented_at_ms`. Landing verifies the receipt MAC and click commitment. The worker has no landing receipt key; it verifies the authenticated landing observation claim, policy, observation age and clock uncertainty independently. A temporarily future landing timestamp is not rewritten to the worker's current time and does not extend any deadline. If import would occur before an authenticated observation/consent timestamp according to the worker clock, import waits within existing bounded work or loses attribution; it never invents a future import timestamp.

App transaction lifetime is exactly 300000 milliseconds from its original creation, capped by app owner and, once assigned, source expiry. App owner cookies/records last at most seven days without sliding renewal; any source attachment uses the minimum of the existing owner deadline and original source deadline. No operation extends a deadline to compensate for processing, retry, clock uncertainty, network outage or browser navigation.

Imported event eligibility uses integer cutoffs stored below. Source eligibility ends at the exclusive original expiry. The availability lease uses the worker's suspend-aware elapsed clock and is a separate concept; wall-clock lifecycle timestamps cannot extend it.

## Landing store

The two landing tables may reside in a logically isolated database on an approved PostgreSQL service. A new PostgreSQL instance is not a security requirement. Shared transactional durability across all landing replicas is required. The current repository does not establish that this service or its RPO-zero failover exists.

### `privacy_control`

One row only; `singleton BOOLEAN PRIMARY KEY CHECK(singleton)`.

| Column | Type / constraint | Meaning |
|---|---|---|
| `generation` | UUID NOT NULL | Current configured source generation |
| `policy_epoch` | BIGINT NOT NULL, valid epoch | Matching CAPI policy epoch |
| `control_version` | BIGINT NOT NULL, >= 0 | Increases on each newly accepted withdrawal or quarantine transition; never decreases |
| `quarantined` | BOOLEAN NOT NULL | Prevents all positive source operations and lease attestations |
| `quarantine_reason` | TEXT NULL, fixed enum | NULL when healthy; `restore`, `capacity`, `durability_unknown`, `generation_mismatch` or `operator` |
| `owner_count` | BIGINT NOT NULL, >= 0 | Transactionally maintained admission counter |
| `generation_close_pending` | BOOLEAN NOT NULL | Reserved, allocation-free durable intent to close/retire the whole generation when targeted closure cannot be represented |
| `updated_at_ms` | BIGINT NOT NULL, valid ms | Last control mutation |

`generation_close_pending=true` implies `quarantined=true`. The row MUST exist before any positive source request is accepted. It is locked `FOR UPDATE` for owner admission, newly accepted withdrawal, acknowledgement, quarantine and positive lease attestation. No cached or read-replica copy authorizes a lease. A counter inconsistency is quarantine, not automatic counter repair.

### `privacy_owner`

Primary key `(generation, landing_owner_hash)`. This is the first-party prepared-owner record and the withdrawal queue in one row. No raw identifier is stored here.

| Column | Type / constraint | Meaning |
|---|---|---|
| `generation` | UUID NOT NULL | Source generation |
| `policy_epoch` | BIGINT NOT NULL, valid epoch | Immutable epoch |
| `landing_owner_hash` | hash32 NOT NULL | Hash of landing host-only cookie `L` |
| `state` | TEXT NOT NULL | `prepared`, `capture_pending`, `captured`, `withdrawal_pending`, `withdrawn`, `expired` |
| `created_at_ms` | BIGINT NOT NULL | Original owner creation |
| `owner_expires_at_ms` | BIGINT NOT NULL | Authenticated observation original source deadline; never extended |
| `provisional_expires_at_ms` | BIGINT NOT NULL | Creation +120000; no dependent capture if cookie confirmation never completes |
| `cookie_confirmed_at_ms` | BIGINT NULL | First successful same-origin cookie confirmation |
| `source_expires_at_ms` | BIGINT NULL | Original source deadline once known; never increased |
| `prepare_request_id` | UUID NOT NULL | First owner preparation idempotency ID |
| `prepare_request_digest` | hash32 NOT NULL | Request identity, excluding any raw persisted click |
| `capture_request_id` | UUID NULL | Capture attempt identity when one has started |
| `capture_request_digest` | hash32 NULL | Canonical capture request digest; not raw request content |
| `withdrawal_id` | UUID NULL | Fixed identity of the one accepted close for this owner |
| `withdrawal_version` | BIGINT NULL, > 0 | `privacy_control.control_version` assigned at durable acceptance |
| `withdrawal_reason` | TEXT NULL | `withdrawn`, `gpc` or `denied` |
| `withdrawal_accepted_at_ms` | BIGINT NULL | Landing commit decision timestamp |
| `worker_acknowledged_at_ms` | BIGINT NULL | Time landing recorded authenticated durable worker acknowledgement |
| `retry_after_ms` | BIGINT NULL | Next bounded retry eligibility |
| `retry_count` | BIGINT NOT NULL, >= 0 | Bounded diagnostic count; not a deletion threshold |
| `retry_claim_id` | UUID NULL | Short retry-worker claim identity |
| `retry_claim_until_ms` | BIGINT NULL | Claim expiry; not consent or dispatch authority |
| `updated_at_ms` | BIGINT NOT NULL | Last mutation |

Unique constraints: `(generation, prepare_request_id)`, and partial unique `(generation, withdrawal_id)` when non-NULL. Optional field pairs are both NULL or both populated. `withdrawal_pending` requires the complete withdrawal identity/reason/version/acceptance fields and NULL worker acknowledgement. `withdrawn` requires those fields plus worker acknowledgement. `prepared`/`capture_pending`/`captured` must not carry a completed withdrawal. Every close row remains a local block on positive source operations even after acknowledgement. New browser JSON uses the frozen exact Origin/custom-header protection; it does not introduce another persisted CSRF secret. Existing context privacy CSRF remains separate.

Owner preparation reserves enough existing row capacity for later targeted closure. Closure of an admitted owner updates its existing row and MUST NOT depend on new ordinary capture capacity. An authenticated close whose identity cannot be represented sets `generation_close_pending` and quarantines the generation using the preexisting control row; it MUST NOT return completed withdrawal. A supervised retry/operator recovery path must durably close/retire that generation at worker before it can become healthy again.

Retry selection uses bounded row locking/claims. Retries run independently of open browsers and incoming page traffic. Lost responses cause the same `withdrawal_id` to be retried. No row is discarded for exceeding a retry count. Pending withdrawal is NEVER removed merely because a source, cookie or deadline expired. Only authenticated worker closure or reviewed whole-generation retirement can discharge that obligation. Automatic restoration of an older empty table is forbidden.

## Worker-owned schema 5 tables

All new tables are in the existing dedicated CAPI database and within its exact schema/role attestation. The descriptions below are normative column/constraint requirements, not executable migration SQL.

### `x_capi_handoff_generations`

Primary key `generation UUID`; unique `policy_epoch` and `(generation, policy_epoch)`. Columns:

| Column | Type / constraint | Meaning |
|---|---|---|
| `generation` | UUID PK | Explicit source generation |
| `policy_epoch` | BIGINT NOT NULL, valid epoch | Existing config guard's epoch |
| `audience_scope` | hash32 NOT NULL | Exact current CAPI/handoff audience |
| `state` | TEXT NOT NULL | `active`, `quarantined`, `retired`; no reopening |
| `landing_control_version` | BIGINT NOT NULL, >= 0 | Highest authenticated observed landing control version |
| `source_count` | BIGINT NOT NULL, >= 0 | Bounded source/tombstone admission |
| `app_owner_count` | BIGINT NOT NULL, >= 0 | Bounded app-owner admission |
| `transaction_count` | BIGINT NOT NULL, >= 0 | Bounded transaction admission |
| `created_at_ms` | BIGINT NOT NULL | Activation record time |
| `retired_at_ms` | BIGINT NULL | Present exactly for retired state |
| `updated_at_ms` | BIGINT NOT NULL | Last mutation |

Only one generation may be active. Activation/retirement is not callable by arbitrary browser data. A landing control version below the durable observed high-water closes freshness; equal version is allowed for repeated readiness, but does not extend any challenge deadline. The high-water detects only previously observed regression and is not a complete rollback defense.

### `x_capi_handoff_sources`

Primary key `(generation, landing_owner_hash)`. Foreign key `generation` to generations. Every positive operation requires matching `policy_epoch` and active generation.

| Column | Type / constraint | Meaning |
|---|---|---|
| `generation` | UUID NOT NULL | Source generation |
| `policy_epoch` | BIGINT NOT NULL | Immutable epoch |
| `landing_owner_hash` | hash32 NOT NULL | Source ownership identity |
| `state` | TEXT NOT NULL | `captured`, `assigned`, `consumed`, `closed`, `expired` |
| `capture_request_id` | UUID NULL | Fixed first capture request; NULL only for close-before-capture tombstone |
| `capture_request_digest` | hash32 NULL | Canonical request identity |
| `observation_id` | UUID NULL | Receipt identity, unique in generation when present; needed for encryption AAD |
| `observed_at_ms` | BIGINT NULL | Original authenticated landing observation |
| `landing_consented_at_ms` | BIGINT NULL | Explicit landing grant time |
| `original_expires_at_ms` | BIGINT NULL | Exact immutable source expiry |
| `landing_policy_version` | TEXT NULL, policy regex | Approved source consent version |
| `twclid_ciphertext` | BYTEA NULL, length 24..272 when present | AES-256-GCM ciphertext plus 128-bit tag for an 8..256-byte validated click |
| `encryption_nonce` | BYTEA NULL, length 12 when present | Fresh random 96-bit nonce, never reused with the same data key |
| `encryption_key_id` | TEXT NULL, `[A-Za-z0-9_-]{1,32}` and pinned key allowlist | Worker-only independent source data key selector |
| `assigned_transaction_hash` | hash32 NULL | The source's single transaction assignment |
| `revocation_version` | BIGINT NOT NULL, >= 0 | Monotonic accepted close version |
| `close_request_id` | UUID NULL | First authenticated source-close request identity |
| `close_request_digest` | hash32 NULL | Exact first close request; duplicate identity with changed body is rejected |
| `closed_reason` | TEXT NULL | `withdrawn`, `gpc`, `denied`, `generation_retired`, `capacity` |
| `closed_at_ms` | BIGINT NULL | Worker source-close decision time |
| `tombstone_until_ms` | BIGINT NULL | Safe cleanup bound if one is known |
| `created_at_ms` | BIGINT NOT NULL | Initial source/tombstone creation |
| `updated_at_ms` | BIGINT NOT NULL | Last mutation |

Unique constraints: `(generation, capture_request_id)`, `(generation, close_request_id)` and `(generation, observation_id)` for populated values; unique `assigned_transaction_hash` when populated. Assignment references the matching generation/transaction identity and MUST be validated within the same transaction; corresponding composite unique keys support the cross-generation-safe foreign keys. Inverse source ownership is revalidated atomically during assignment/redemption. Revocation version is worker-maintained monotonic state, not an undeclared request field; the frozen revoke wire object carries its fixed `request_id`, owner, reason and generation/epoch. A closed owner can return an authenticated already-closed acknowledgement for a subsequent distinct close request without reopening or renewing its lifecycle.

For positive states, observation/consent/expiry/policy/capture identity fields are populated and obey the time rules. `captured` has NULL assignment. `assigned` and `consumed` have exactly one assignment. `captured`/`assigned` carry the complete encryption key/nonce/ciphertext tuple; `consumed`/`closed`/`expired` have it erased. Source ciphertext is retained only while needed to issue/redeem. AAD is CanonicalJSON of `{v:2,epoch,generation,owner_hash,observation_id,purpose:"pending-source"}` with canonical wire values. The data keyring is `X_HANDOFF_SOURCE_DATA_KEYRING_FILE`; key material never enters this table. Existing CAPI context/outbox plaintext columns remain a separately disclosed baseline; this source encryption does not encrypt them retroactively.

First touch is decided by the locked owner/source row, not last-arriving request. An identical capture retry returns the existing result without renewing time. A different capture for an existing source does not replace it. An interrupted navigation alone does not clear assignment. Once the assigned H actually expires unconsumed, one transaction may mark H expired and return source `assigned` to `captured`, clearing only that assignment. It preserves click/observation/consent/original expiry and never changes consumed/closed source state. A later explicitly initiated bounded app attempt may assign the still-current source; expired H cannot redeem or recover it.

Close-before-capture is represented by inserting `closed` with nullable observation/capture fields. Later capture, authorization or import MUST find and respect that tombstone. If a safe per-owner tombstone cannot be admitted, atomically quarantine the generation; never permit a source to bypass closure because its context does not yet exist. Minimum tombstone retention is 91 days from the closing decision, and never before a known dependent original deadline. Unknown/ambiguous dependent lifetimes require retention until reviewed generation retirement; admission is bounded and saturation fails closed for attribution.

### `x_capi_handoff_app_owners`

Primary key `(generation, app_owner_hash)`, generation/epoch FK. Columns: `generation UUID`, `policy_epoch BIGINT`, `app_owner_hash hash32`, `state TEXT` (`prepared`, `imported`, `closed`, `expired`), `prepare_request_id UUID`, `prepare_request_digest hash32`, `created_at_ms BIGINT`, `provisional_expires_at_ms BIGINT` (creation +120000), `cookie_confirmed_at_ms BIGINT NULL`, `owner_expires_at_ms BIGINT`, `open_transaction_count BIGINT CHECK BETWEEN 0 AND 4`, `closed_at_ms BIGINT NULL`, `updated_at_ms BIGINT`. Unique `(generation, prepare_request_id)`. All fields are NOT NULL unless explicitly marked NULL. Unconfirmed provisional rows have no dependent H and expire after 120 seconds. An existing creation ID without its valid cookie returns RESULT_UNAVAILABLE; it never reconstructs or replaces A.

The owner deadline is immutable or shortened, never extended. No cookie repair, context possession or browser-supplied hash reassigns ownership. A browser without valid `A` can navigate/use business APIs but cannot positively use an imported context. Ownership is browser state, not wallet authority or physical identity.

### `x_capi_handoff_transactions`

Primary key `transaction_hash hash32`, globally unique. Composite FK `(generation, app_owner_hash)` to app owners.

| Column | Type / constraint | Meaning |
|---|---|---|
| `transaction_hash` | hash32 PK | Hash of raw `H`; no recoverable raw capability |
| `generation` | UUID NOT NULL | Generation |
| `policy_epoch` | BIGINT NOT NULL | Immutable epoch |
| `app_owner_hash` | hash32 NOT NULL | Immutable browser owner |
| `state` | TEXT NOT NULL | `app_granted`, `source_approved`, `redeemed`, `cancelled`, `expired` |
| `begin_request_id` | UUID NOT NULL | Original begin idempotency identity |
| `begin_request_digest` | hash32 NOT NULL | Exact begin request digest |
| `app_policy_version` | TEXT NOT NULL, policy regex | Explicit independent app choice |
| `app_consented_at_ms` | BIGINT NOT NULL | App consent time |
| `created_at_ms` | BIGINT NOT NULL | Original transaction creation |
| `expires_at_ms` | BIGINT NOT NULL | Original creation +300000, capped as specified |
| `landing_owner_hash` | hash32 NULL | Source identity after approval |
| `authorize_request_id` | UUID NULL | Source authorization idempotency identity |
| `authorize_request_digest` | hash32 NULL | Exact authorization request digest |
| `redeemed_at_ms` | BIGINT NULL | Durable import decision cutoff |
| `cancelled_reason` | TEXT NULL | Fixed enum; no raw error content |
| `updated_at_ms` | BIGINT NOT NULL | Last mutation |

Unique `(generation, app_owner_hash, begin_request_id)` and populated `(generation, authorize_request_id)`. Source reference `(generation, landing_owner_hash)` is an FK when populated. Exact inverse assignment checks connect transaction and source. State/field checks require source approval fields only after source approval, and a redemption timestamp only for redeemed state. No client-provided redirect URL is stored; destination is the contract's fixed app destination.

Begin creates and stores only the hash of a fresh `H`, then returns raw `H` once. If an acknowledgement is lost, retrying the same begin identity cannot reconstruct it: return `409 RESULT_UNAVAILABLE`. The same ID with changed body returns `409 IDEMPOTENCY_CONFLICT`. A fresh attempt uses a fresh request ID and explicitly initiated bounded attempt; no automatic loop allocates capabilities. Admission under the app-owner lock enforces at most four nonterminal transactions per owner. Superseded/lost attempts keep their original deadline and expire through cleanup; they do not cause source reassignment.

### `x_capi_handoff_imports`

Primary key `context_id UUID`, FK to existing `x_capi_attribution_contexts.id`. Unique `transaction_hash`, unique `(generation, landing_owner_hash)`, unique `(generation, app_owner_hash)`, unique existing `context_handle_hash CHAR(64)` with lowercase-hex check. Additional composite source/app-owner FKs. Thus one A can consume at most one source in its generation; a later attempt does not replace its first imported source.

| Column | Type / constraint | Meaning |
|---|---|---|
| `context_id` | UUID PK | Existing unbound CAPI context identity |
| `ownership_id` | UUID UNIQUE NOT NULL | Stable random nonauthorizing browser/tab coordination identity, returned unchanged on recovery |
| `context_handle_hash` | CHAR(64) UNIQUE | Existing handle-hash encoding, not a new binary encoding |
| `transaction_hash` | hash32 UNIQUE NOT NULL | Redeemed transaction |
| `generation` | UUID NOT NULL | Generation |
| `policy_epoch` | BIGINT NOT NULL | Immutable epoch |
| `landing_owner_hash` | hash32 NOT NULL | Immutable source ownership |
| `app_owner_hash` | hash32 NOT NULL | Immutable receiving-browser ownership |
| `observed_at_ms` | BIGINT NOT NULL | Original observation |
| `landing_consented_at_ms` | BIGINT NOT NULL | Source grant evidence |
| `app_consented_at_ms` | BIGINT NOT NULL | App grant evidence |
| `import_cutoff_ms` | BIGINT NOT NULL | Import decision cutoff, not a claimed physical commit timestamp |
| `original_expires_at_ms` | BIGINT NOT NULL | Original source expiry, never extended |
| `effective_expires_at_ms` | BIGINT NOT NULL | Minimum of original source expiry and immutable/shortened app-owner deadline |
| `binding_cutoff_ms` | BIGINT NULL | First independent-wallet binding decision cutoff; write once |
| `state` | TEXT NOT NULL | `active`, `closed`, `expired` |
| `context_ciphertext` | TEXT NULL, ASCII URL-safe, <=2048 chars | Exact returned encrypted v4 ticket for bounded retry recovery |
| `result_recover_until_ms` | BIGINT NOT NULL | At most original transaction expiry and effective import expiry |
| `created_at_ms` | BIGINT NOT NULL | Creation |
| `updated_at_ms` | BIGINT NOT NULL | Last mutation |

Import transaction atomically: checks all source/app/transaction state and deadlines and the absence of another import for A; chooses context handle/CSRF/ownership ID; builds one exact v4 ciphertext; inserts the unbound CAPI context and this mapping; marks source consumed, app owner imported and transaction redeemed; erases source ciphertext; updates admission counters; commits. No encrypted result is returned before commit. Failure rolls back all these changes. No import creates wallet authority or a wallet binding. Competing H for the same A cannot consume another source, even if both passed an earlier source-approval step.

Retry of the same `H` by the same valid app owner within the recovery interval returns the same ciphertext and context identity. The worker decrypts that exact v4 ciphertext if it must reconstruct the CSRF response field; no plaintext CSRF database column is necessary. A different owner, altered request, closed lifecycle, expired recovery interval or expired transaction cannot recover/redeem it. Successful retry is not a second redemption. Result storage is cleared on closure or recovery expiry; the mapping/tombstone persists for replay/withdrawal safety.

V4 remains unchanged: exactly `v`, `handle`, `csrf`, `state`, `policy_version`, `policy_epoch`, `mode_scope`, `audience_scope`, `issued_at`, `lifecycle_expires_at`, `landing_commitment`, `consented_at`, `expires_at`, `twclid`. It uses seconds internally. Seal `issued_at=observed_at_ms/1000.0`, `consented_at=import_cutoff_ms/1000.0`, and `expires_at=lifecycle_expires_at=effective_expires_at_ms/1000.0`. This consent field represents effective imported grant activation, not a claim that either user choice or physical commit occurred then; the original independent choice timestamps remain in the mapping. The source's original expiry remains unchanged. The mapping's integer fields, not a round-trip conversion of binary floating-point fields, determine imported-path cutoff checks. Owner/source metadata MUST NOT be appended to v4.

## Positive worker ingestion and binding

New imported positive event/bind envelopes are version 2 and include the server-derived `browser_owner_hash`. Existing `exact-bearer-v1` proof purpose, 30-second duration, exact accepted-bearer revalidation, primary-key verification and full-envelope digest remain unchanged. New handoff/envelope/owner-binding requirements are pinned in audience/configuration scope and a higher policy epoch. V1 positive envelopes and contexts without a current mapping fail closed under this scope. Existing V1 privacy-close/revoke messages remain supported.

Before any positive durable binding/event mutation, the worker verifies the primary ticket, current source/import/app state, original expiry, matching owner hash, GPC/closure fences, F2 proof and wallet eligibility. The owner hash is computed from cookie `A` in trusted listener code and captured in immutable `_AuthenticatedContext`; background work never rereads request/cookie objects. Absence or ambiguity drops optional attribution only.

For all imported positive events, `event_timestamp_ms >= import_cutoff_ms`, each consent/observation timestamp is at or before that cutoff, and `event_timestamp_ms < effective_expires_at_ms <= original_expires_at_ms`. Worker current time must also be before effective expiry. For deposit/session, a matching first wallet must already be bound and `event_timestamp_ms > binding_cutoff_ms`. The strict comparison drops same-millisecond ambiguity. Original business timestamps are never moved forward to fit a later binding.

`binding_cutoff_ms` is captured when the worker makes its first independent-wallet binding decision under the source/handle/wallet locks and is committed with that binding. It is not represented as the physical PostgreSQL commit time. These checks establish the contract's decision ordering; exact physical post-commit chronology would require another protocol. Explicit bind creates no conversion. The bind-only normalization path must preserve the verified owner field.

## Locking and concurrency

Operations that acknowledge privacy closure take the existing OS privacy-close boundary before starting their database transaction. That boundary is held through commit and orders closure against dispatch. No database transaction or row lock is held across cross-server HTTP. Ordinary authentication/payment/session handlers do not acquire these DB locks or perform CAPI RPC.

Within worker transactions, use a fixed order: generation guard; source; app owner when needed; transaction; existing context handle advisory lock; wallet advisory lock for wallet operations; context/import/outbox rows; bounded capacity counters. Source identity may be read without locking solely to locate its immutable lock key, then must be revalidated under locks. Begin touches app owner/transaction only and must not subsequently acquire a source lock in the same transaction. Multi-source app-owner closure takes the exclusive generation guard while discovering affected sources, locks their identities in sorted order before the app owner, and revalidates the complete mapping set; it cannot acquire an unseen source after locking the owner. Generation retirement likewise explicitly uses `FOR UPDATE`. Ordinary operations use `FOR KEY SHARE` on that generation guard, with non-key counter/high-water updates compatible with `FOR NO KEY UPDATE`; they do not first take `FOR SHARE` and later attempt a conflicting lock upgrade. The explicit exclusive retirement/closure guard blocks new ordinary operations until its complete mapping-set mutation commits. Implementations must not mix the old handle-first ordering with new source-first privacy closure in a way that creates a cycle.

Redemption and source closure lock the same source and transaction identities. Closed state is terminal. Concurrent redemption has one committing winner; a same-owner identical retry reads the existing committed result. Lost commit acknowledgement does not authorize regeneration. Privacy closure before source/context creation inserts tombstone state under the same identity lock.

## Capacity, cleanup and retention

All stores and queues require finite configured global caps, bounded database pools, short statement/lock timeouts and aggregate-only diagnostics. The synthetic profile fixes 10000 total landing owner rows and 10000 total worker source rows, four open transactions per app owner and one assigned transaction per source. Generation/control rows are preallocated outside those ordinary row caps. Worker app-owner and total-transaction admission must also be finite (at most 10000 owner rows and 40000 transaction rows under this profile) and reserve safe expiry/closure state. Disposable saturation fixtures may explicitly reduce limits, never increase them. Production capacity remains a separately approved provisioning decision.

Ordinary admission must reserve closure capacity. Positive saturation loses attribution. Authenticated closure that cannot be represented must atomically quarantine its generation using preallocated state. Never discard an accepted withdrawal to recover capacity. Cleanup runs in bounded batches and cannot reset original deadlines, capabilities, counters or generations.

Cleanup runs at least every 60000 milliseconds while the subsystem is healthy, in bounded batches; inability to clean does not extend validity. Expired raw source ciphertext and encrypted recovery results are erased promptly. Closed/expired source/import/owner tombstones are retained for at least 91 days from their closure/expiry decision and through every dependent original deadline; ambiguous lifetimes require reviewed generation retirement. An unconfirmed, never-used provisional owner can be cleaned after its 120-second deadline only if no capture/H/withdrawal dependency exists and replay cannot recreate positive authority. Existing context, outbox and deduplication retention remains intact except for authorized privacy erasure. A backup retains erased data unless separately handled: encryption and backup/PITR retention are deployment decisions, not assumed facts.

## Evidence and remaining implementation work

Backend paths relative to its repository root:

- `axonos_gate/x_capi.py:62`: opaque/nonce/policy bounds; `:75`: context v4 and F2 constants; `:84`: producer datagram maximum 4096 bytes.
- `axonos_gate/x_capi.py:977`: context plaintext maximum 1024; `:1038`: exact v4 fields; `:1101`: existing handle-keyed click commitment; `:1118`: generated 32-byte handle/CSRF.
- `axonos_gate/x_capi.py:1933`: whole-envelope digest; `:1940`: unchanged F2 proof; `:2005`: bounded asynchronous exact-bearer authorization.
- `axonos_gate/x_capi_worker.py:1855`: current strict V1 schemas; `:2116`: proof checked before durable ingest; `:2120`: bind-only normalization needing owner preservation; `:2321`: existing unbound context first-wallet path.
- `axonos_gate/x_capi_worker.py:2531`: durable context closure; `:2630`: existing absent-context tombstone, which does not replace new source-before-context tombstones.
- `axonos_gate/x_capi_worker.py:574` and `:805`: exact relation/column privilege checks; `:919`: catalog/readiness and 30-second attestation cache.

Schema-5 SQL, catalog digest, role grants, encryption-key provisioning, actual landing store, clock monitoring, backup policy, network reachability and durable retry supervision remain implementation/provisioning prerequisites. None is claimed present by this document.
