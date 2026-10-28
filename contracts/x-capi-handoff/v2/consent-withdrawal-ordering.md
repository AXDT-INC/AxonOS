# Handoff v2: consent, withdrawal and freshness ordering

Status: normative draft for isolated synthetic implementation. Central worker custody and Policy B30/10 are provisionally approved for synthetic work only. No production migration/configuration/deployment, live collection or X request is authorized by this artifact.

## Independent consent and authority

Landing consent authorizes the described first-party source capture/central custody. App consent independently authorizes the described conversion attribution. Neither choice authenticates a wallet. The existing exact accepted-bearer F2 proof remains necessary for every positive wallet bind/event.

Source custody is central: worker PostgreSQL is the authority for source grant, immutable expiry, source assignment, app consent transaction, redemption, imported mapping and downstream closure. Landing's shared durable database holds prepared owner/control state and pending withdrawal, not another raw-click source ledger. No remote status check is made on payment/session paths.

The browser owner model requires landing cookie `L` to authorize the source, app cookie `A` to own the receiving transaction, and persistent app-owner verification of imported positive use. `A` is hashed by trusted request adapters and included in the v2 F2-covered envelope. Possession of encrypted context alone cannot establish another browser's first wallet binding. Cookies establish browser-state possession, not a person or wallet.

GPC observed through the browser or authenticated request suppresses positive attribution and begins closure for known state. Absence of a signal is not evidence of a human's privacy preference. A closed/offline browser cannot report a later GPC change; no server claims otherwise.

## Response semantics

Browser/source protocol successes are HTTP 200 unless explicitly stated otherwise. `202` on landing withdrawal means durable pending state was committed locally, not that worker suppression is complete.

| Result | Meaning |
|---|---|
| `200` completed withdrawal | Worker has durably closed the source/mappings under its dispatch boundary; later dispatch cannot start |
| `202` landing pending withdrawal | Landing committed retryable withdrawal and blocked new local positive operations; propagation remains pending |
| `409 RESULT_UNAVAILABLE` | Repeated hash-only begin cannot recover raw H; explicit bounded fresh attempt required |
| `409 IDEMPOTENCY_CONFLICT` | Same request identity with different canonical content |
| `410 WITHDRAWN` | Positive operation targets a closed lifecycle |
| `410 EXPIRED` | Positive operation or recovery exceeded its original deadline |
| `410 GENERATION_RETIRED` | Generation is no longer eligible |
| Unavailable/rate/capacity error | No positive attribution guarantee; ordinary app navigation/business actions continue |

No error echoes cookies, raw click, capabilities, proof, wallet identity or canonical body. A lost response does not authorize repeated creation. Privacy requests remain possible when positive admission/freshness is unavailable.

## Durable landing acceptance

For a verified owner withdrawal (including GPC) protected by exact same-origin JSON, `X-AxonOS-Handoff: 2` and the frozen schema (with existing context CSRF additionally required where that existing privacy API requires it):

1. Lock the singleton `privacy_control` row, then the owner's `privacy_owner` row.
2. If already `withdrawal_pending`/`withdrawn`, return the existing operation state; never generate another lifecycle.
3. Allocate one `withdrawal_id`, increment `control_version`, mark owner `withdrawal_pending`, record reason/version, and commit. This existing owner row is already reserved for closure.
4. Only after successful durable commit may landing return `202`. Positive capture/authorization for that owner is locally blocked immediately by this state.
5. A supervised bounded retry loop sends the same source-close identity independently of the browser. It continues after tab/browser/pod closure.
6. After authenticating worker's durable acknowledgement, record acknowledgement and state `withdrawn` under the same control-row ordering. Then `200` may be returned/recovered.

A failed/ambiguous local commit cannot be described as durably accepted. If targeted authenticated closure cannot be represented, use the reserved singleton generation-quarantine/close intent. No completed acknowledgement is returned until central retirement/closure is durable. Ordinary capture capacity must not decide whether a previously admitted owner's withdrawal can be recorded.

Source service transport is fixed HTTPS to `/api/x-attribution/handoff/source`; supported operations are `capture`, `status`, `authorize`, `revoke`, `lease_challenge` and `lease_attest`. Messages are authenticated and purpose/generation bound. The gate performs bounded strict forwarding into worker RPC and has no CAPI SQL credential. No arbitrary target URL or wallet authority is accepted from landing.

## Source capture versus close

Landing preparation durably reserves the owner row before raw capture is considered accepted. There is no additional central source-prepare operation: the existing `capture` operation reserves/inserts the first central source slot, validates the authenticated landing consent/observation claim and its independent timing bounds, and stores the encrypted identifier in one transaction. The central source identity is `(generation, landing_owner_hash)`. Capture, source authorization and closure synchronize on that identity.

Central close is valid before central capture/context creation. It inserts a closed source tombstone when needed. Later capture/authorization/redemption cannot turn it active. A source/context that does not yet exist is not a reason to treat withdrawal as a no-op. If safe targeted tombstone storage is unavailable, the entire generation is quarantined using reserved state.

A locally initiated capture already in flight when landing accepts withdrawal may reach worker first. It cannot escape the retained retry/tombstone ordering: central close eventually closes it, and Policy B limits new dispatch authorization during an undelivered close. No new local positive request is initiated after the durable landing close state.

First capture/first touch and original expiry never reset on refresh, duplicate messages, timeouts, GPC changes, withdrawal or lost acknowledgements. When an assigned H expires unconsumed, the same expiration transaction may release that one assignment and return source to `captured`; a later explicit bounded attempt may assign it without extending any original deadline. Consumed/closed sources cannot be released this way. Fresh consent in a new approved lifecycle does not reopen the closed one.

## Atomic redemption versus close

`H` is a single-use transaction capability stored only as a worker hash. App browser callback supplies H and its own HttpOnly A; adapter-derived ownership is checked centrally. Source authorization marks that exact transaction/source pair; H is not a wallet credential.

Redemption locks current generation/app owner/source/transaction and atomically creates one unbound context plus import mapping and one stored encrypted v4 result. Source becomes consumed and transaction redeemed. Worker returns only after commit. Identical same-owner recovery within the original recovery interval returns the same encrypted result; other-owner recovery fails. A duplicate begin cannot recover H because no reversible H is stored.

Source closure takes the same durable identity locks and the privacy-close boundary. If closure commits first, redemption fails closed. If redemption commits first, closure follows the source mapping and closes the imported context and queued work. There is no gap where a returned context is outside source withdrawal coverage.

## Worker completed-close boundary

1. Authenticate the source or app privacy capability and exact request identity. App privacy closure remains available without A so cookie loss does not destroy withdrawal authority.
2. Acquire the existing OS privacy-close boundary, which uses the same exclusive lock as dispatch.
3. Begin the bounded database transaction and lock the generation/source and related transaction/context/import identities in the documented order.
4. Mark source closed with monotonic close version; invalidate unredeemed transactions; erase raw-bearing source/recovery state; close mapped contexts; clear their identifiers and cancel unsent queued/leased work; create absent-object tombstones as required.
5. Commit, then acknowledge completion. An ambiguous commit response is retried idempotently, never interpreted as permission to reopen.
6. Release the boundary. An already completed close remains effective across ordinary worker restart.

The closure boundary is usable while global privacy quarantine/freshness is closed. A positive lease cannot override a source tombstone, app withdrawal, original expiry, F2 failure or an existing global privacy fence.

## Policy A versus provisionally selected Policy B

Policy A would allow previously authorized queued conversions while landing withdrawal remains undelivered. It still guarantees no new dispatch after completed worker closure.

Selected synthetic Policy B adds a conservative global availability lease for all landing-derived contexts. There is one lease per worker generation, not one per owner. Any unacknowledged landing withdrawal prevents positive renewal for the whole generation. This can interrupt unrelated optional attribution and does not affect ordinary app navigation, financial settlement, sessions or GPU work.

## Exact Policy B30/10 renewal

The renewal target is every 10 seconds. The maximum validity horizon is 30 seconds from worker challenge issuance. Ten seconds is not additional grace, and late scheduling does not extend the horizon.

1. Worker receives authenticated `lease_challenge`, validates current generation/policy/scope, and allocates one random nonce under a bounded runtime lock. It records fresh process boot identity and `deadline_elapsed = issued_elapsed + 30 seconds` using `CLOCK_BOOTTIME` or an equivalent suspend-aware elapsed clock.
2. Landing obtains this exact challenge, then starts a fresh primary-database transaction and locks `privacy_control`.
3. It verifies matching generation/epoch, no quarantine/restore uncertainty, healthy durable store, and **zero unacknowledged withdrawals** across all replicas. A cached count, earlier readiness receipt or another pod's local empty queue is insufficient.
4. Landing authenticates the exact challenge, boot identity, generation, epoch and `control_version` in `lease_attest`. It does not set or modify the deadline. It does not combine a previously signed all-clear with a new nonce.
5. Worker accepts an unused matching challenge only before its fixed original deadline and records the lease with that deadline. Repeating the same accepted response can recover acknowledgement but never refresh validity. Wrong boot, stale generation, control-version regression, consumed nonce or deadline equality fails closed.
6. Worker invalidates availability on restart and uses a new boot identity; old responses cannot restore it. No elapsed deadline is restored from disk or database.

There is at most one pending challenge and one active lease. Issuing/replacing a challenge does not invalidate a still-current lease unless a negative/quarantine condition does, and does not extend it. Admission saturation stops renewal rather than allocating an unbounded challenge history. Privacy-close service retains independent reserved admission.

Lease state is protected by a short runtime mutex. It is never held across HTTP or database waits. Dispatch/close operations take the OS privacy boundary before any brief lease-state access; lease handlers must not take the inverse lock order. Generation transitions invalidate lease memory before admitting new-generation work. Any failed validation/commit during transition leaves freshness invalid.

## Freshness correctness argument

Let landing durably accept withdrawal at time T. Any positive lease attestation serialized before that acceptance used a worker challenge issued even earlier; its fixed deadline is therefore at most T+30 seconds. Positive attestations serialized afterwards are prohibited while that withdrawal is unacknowledged. Once it is acknowledged, worker source closure is already durable.

Consequently, without source/store compromise or unsupported rollback, no **new dispatch authorization** for that source occurs later than 30 seconds after durable landing acceptance. Completed worker withdrawal gives the stronger immediate ordering guarantee: no subsequent dispatch authorization at all.

A response delayed in transit cannot revive authority because its deadline is anchored to worker challenge issuance, not response receipt. A network partition naturally expires the lease. This bounded guarantee does not require cross-server UTC clock synchronization; the independent original-source-expiry guarantee still has the documented clock assumptions.

## Dispatch boundary and in-flight limitation

Worker checks the global landing lease in addition to all existing dispatch predicates, inside the exclusive dispatch boundary and immediately before transport authorization. It rechecks after potentially delayed database/payload work; checking only when claiming a job is insufficient. Missing freshness pauses otherwise eligible work within its original expiry/event-age limits. It never moves event time forward or bypasses deduplication.

The precise lease guarantee concerns the final dispatch authorization boundary. A process can pause after a user-space check and before a network syscall. A future transport should carry/check the fixed absolute remaining deadline through preparation and before sending, but no contract claims physical packet delivery/acceptance can be recalled. A request already across the dispatch boundary may be in flight, including one authorized before a completed close could acquire the lock.

The existing worker holds the same exclusive boundary across claim, committed pre-send check, HTTP and completion. Source close must reuse that ordering, not create a second unrelated lock or a remote status-only check.

## App consent, F2 and no-retro boundaries

V4 ciphertext remains unchanged and may reside in the app browser; raw twclid does not. Imported positive use additionally requires the mapping's app-owner hash in each v2 F2-covered envelope. Same-site cookies alone are not CSRF protection: exact app Origin or an explicitly reviewed same-origin request rule is required for positive attribution. A mismatch suppresses attribution, not business behavior.

Independent wallet binding is required before deposit/session eligibility. Payment authorization alone cannot establish first binding. Worker first-binding requires the existing exact-bearer proof and matching owner. Wallet switching cannot transfer source/context ownership. Existing first-wallet checks and worker privacy fences remain in force.

All events must be at/after the stored import decision cutoff and before the exclusive effective expiry, which is min(original source expiry, existing app-owner deadline). Source original expiry is preserved separately; A is never extended. V4 `issued_at` is original observed milliseconds/1000.0, `consented_at` is import cutoff/1000.0 (effective imported grant activation), and `expires_at=lifecycle_expires_at` is effective expiry/1000.0. The original independent consent-choice timestamps remain separate evidence. Deposit/session event timestamps must be strictly after the stored first-binding decision cutoff. Same-millisecond ambiguity loses optional attribution. The contract does not label a timestamp sampled inside a transaction as the physical commit time, and does not reconstruct old events after import/binding.

## Failures, closure after browser exit and restoration

| Failure | Required behavior |
|---|---|
| Landing browser closes after 202 | Durable retry continues; pending owner blocks new local positives |
| Worker/network unavailable | Landing retains pending; no renewal while pending; old lease expires |
| Worker closes source but acknowledgement is lost | Landing retries same withdrawal; worker returns existing durable closed result |
| Landing DB unavailable/unknown commit | No honest durable-acceptance claim; no positive lease attestation |
| Worker restarts | Existing source/close state remains; freshness invalid until a new boot-bound handshake |
| Source/transaction expired | No redemption, event backfill or TTL renewal; cleanup keeps required replay/close evidence |
| Authenticated tombstone capacity exhausted | Quarantine generation; no new dispatch; no false completed acknowledgement |
| Restored/lossy landing queue | Quarantine renewal and retire old source generation centrally; new nonce alone is insufficient |
| Entire CAPI state restored | Existing stopped-worker, higher-epoch destructive recovery restrictions apply |

RPO-zero accepted privacy commits or destructive retirement on possible loss is mandatory. An external nonrollbackable anchor is required only if automatic whole-stack rollback safety is promised; none is assumed available.

## Synthetic validation obligations

Required generated-key/disposable tests include:

- Withdrawal before source capture/context creation; delayed capture cannot reopen.
- Concurrent capture/authorize/redeem/close with deterministic ordering and one winner.
- Lost begin acknowledgement returns RESULT_UNAVAILABLE without recreating H; explicit fresh attempts obey the four-open cap.
- Lost redemption acknowledgement recovers the same encrypted result only for the same A.
- Two browsers, stolen H/ciphertext, owner-hash substitution, duplicate cookies, malicious sibling Origin and wallet switching.
- V2 complete-envelope owner proof and bind-only normalization; missing mapping/legacy-positive downgrade rejected; old privacy close remains usable.
- Non-wallet event before binding, same-millisecond event, delayed ingestion and immutable event timestamp.
- Positive lease response delayed beyond deadline, duplicate response, wrong boot and restarted worker.
- Withdrawal enqueue races lease attestation in separate landing replicas under one control row.
- Partition after durable acceptance; no new dispatch authorization after the 30-second horizon.
- Lease expiration during database/payload work and suspend/resume clock handling.
- Full queue/tombstone capacity still permits reserved close/quarantine; renewal/capture flood cannot starve privacy service.
- Pending withdrawal survives browser/pod closure; restored empty queue cannot automatically renew.
- All existing F1/F2, wallet-status, x402 settlement/recovery, session and off-mode protections remain unchanged.

No tests in this artifact assert live public-ingress privacy, deployed key/database durability or real X delivery. Those require separate operational authorization/evidence.

## Evidence

Backend-relative source at the inspected baseline:

- `axonos_gate/x_capi_worker.py:2531`: durable lifecycle close; `:2595`: identifier/binding erasure; `:2609`: cancellation; `:2630`: absent-context tombstone.
- `axonos_gate/x_capi_worker.py:3171`: dispatch boundary; `:3215`: privacy-close boundary; `:3751`: concurrent consent service with independent lock FD and connection.
- `axonos_gate/x_capi_worker.py:4280`: committed pre-network validation; `:4855`: exclusive boundary through full outbound operation; `:4911`: payload construction before transport.
- `axonos_gate/x_capi_worker.py:4877`: normal live delivery remains structurally blocked before protected token access.
- `axonos_gate/x_capi.py:1890`: immutable server-only authority; `:1933`: whole-envelope proof digest; `:2005`: bounded asynchronous exact-bearer check.
- `docs/X_CAPI.md:437`: single-host/single-worker limit; `:553`: completed withdrawal requires worker DB acknowledgement; `:563`: snapshot rollback limitations.

Linux `CLOCK_MONOTONIC` excludes suspension; `CLOCK_BOOTTIME` includes it. See [Python time documentation](https://docs.python.org/3/library/time.html#time.CLOCK_BOOTTIME) and [Linux clock documentation](https://www.man7.org/linux/man-pages/man2/clock_gettime.2.html). This clock choice does not make VM snapshot rollback safe.
