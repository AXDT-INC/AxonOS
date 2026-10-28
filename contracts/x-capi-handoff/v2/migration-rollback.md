# Handoff v2: migration, restart and rollback contract

Status: documentation only. This artifact plans a future dedicated CAPI schema-5 migration named `008`; it is not a migration and does not authorize running one. Synthetic Policy B30/10 and central custody are provisional only. Production CAPI remains off; live delivery/provenance gates, financial protections and Stripe CARD restrictions remain unchanged.

## Baseline and distinct database domains

Inspected backend: `004aba17456f3a107ee20f8d3f635ca638f29b54`, branch `codex/x-capi-reconciliation`.

The current dedicated CAPI database has schema version 4. The worker pins its exact catalog, privileges and expected version. It is a single-host, single-worker design using local runtime/privacy volumes and Linux file locks. The gate has no CAPI database credentials.

Financial migrations `006_x402_settlement_recovery.sql` and `007_x402_admission_lifecycle.sql` target the gate/ledger database. 007 depends on 006. Future handoff `008` targets only the dedicated CAPI database and is not an instruction to run 006/007 there. Their separate deployment prerequisites remain intact. This document does not classify either financial migration as deployed.

## Future migration 008 requirements

The future migration MUST explicitly upgrade verified CAPI schema 4 to schema 5. It MUST:

1. Attest the dedicated target database and migration identity before any DDL.
2. Verify the exact expected schema-4 catalog/ACL preimage; a mutable version row alone is insufficient.
3. Use bounded lock/statement/connection timeouts and one reviewed transactional upgrade.
4. Create only the reviewed handoff tables/constraints/indexes/capacity state and any explicitly reviewed existing-CAPI additions needed by the contract.
5. Preserve financial, auth, session, payment, relayer and tenant-storage schemas untouched.
6. Install exact owner/runtime grants: worker runtime receives only required table/column rights; gate and landing receive no CAPI SQL rights.
7. Set schema metadata to 5 only as part of the same successful transaction.
8. Validate the exact new catalog/privilege postimage and its generated pinned digest before activation.

Schema version 5 is distinct from migration file number 008. The digest must be generated from the reviewed schema-5 catalog during implementation; no guessed digest is a valid contract artifact.

The existing scripts need explicit new branches. `apply_x_capi_migrations.sh` currently accepts only absent/v4 state and unconditionally invokes migration 004's schema-4 grants/postflight. `bootstrap_x_capi_db.sh` independently has the same absent/v4 restriction before calling that script. Merely appending 008 will fail restart or apply the wrong attestation.

Required future paths:

| Starting state | Required behavior |
|---|---|
| No dedicated CAPI schema | Reviewed fresh creation followed by schema-5 transition/postflight; no unsupported-version guessing |
| Exact reviewed schema 4 | Explicit 008 upgrade and schema-5 grants/catalog postflight |
| Exact reviewed schema 5 | Readiness/grants verification appropriate to schema 5; no rerun of a schema-4-only postflight |
| Any unexpected version/catalog/ACL | Fail closed and require review; do not provision over or silently repair it |

Historical migration definitions should not be silently reinterpreted. Implementation must choose and review explicit version-aware pre/postflight helpers or an equivalent auditable upgrade path. Restarting the old worker against the new schema is not supported.

## Coordinated future activation

No step here is currently authorized to execute. The eventual approved procedure must:

1. Keep mode off and live/provenance gates closed.
2. Confirm an exact reviewed baseline, rollback plan and protected secret provisioning without printing credentials.
3. Drain/stop the one CAPI worker before schema/privilege changes. Its successful schema attestation is cached for 30 seconds; a running old worker is not a safe migration monitor.
4. Apply only the dedicated CAPI migration with the separate migration owner.
5. Deploy producer and worker code that agree on schema 5, positive envelope v2, imported owner binding, Policy B30/10 and exact audience.
6. Configure an explicitly higher existing CAPI policy epoch and new handoff generation UUID. Old source generations/positive contexts are not migrated into consent or eligibility.
7. Start one worker with valid shared runtime/privacy volumes. Freshness starts invalid. Validate its existing config guard, schema/role attestation, listeners and privacy controls.
8. Start supervised landing queue retry/freshness renewal against the shared durable store. Only a fresh challenge, correct generation and zero pending withdrawals can grant a new lease.
9. Exercise generated-key isolated synthetic tests and the separately authorized staging privacy checks. No part enables real X delivery.

Rollback of application binaries after schema upgrade may leave attribution unavailable. That is safer than allowing an older binary to reinterpret new data. Recovery is a reviewed forward fix or explicit destructive attribution retirement, not a blind schema downgrade.

## Normal restart guarantees

PostgreSQL durability and uniqueness preserve source assignment and single-use redemption across process/pod restarts. A lost acknowledgement recovers the same committed import/context ciphertext where the recovery interval permits it. The hash-only begin result is intentionally unrecoverable: duplicate begin returns `409 RESULT_UNAVAILABLE`; a new attempt requires explicit bounded initiation.

Worker restart invalidates every availability lease and challenge. The new worker uses a fresh unpredictable boot identifier; it never restores an elapsed-time deadline from PostgreSQL or disk. Source/consent/transaction state persists, but dispatch requires a new challenge/attestation after startup privacy checks. Old boot responses are rejected.

Landing pod restart is safe only when every accepted withdrawal remains in the same durable store. A new pod does not start with an empty local queue. It must read shared state and resume bounded retry; positive lease signing uses a fresh primary transaction, not cached readiness. Landing replica failover must preserve accepted commits with RPO zero, or use the destructive recovery policy below.

## Restoration is not normal restart

There is no automatic safety claim for snapshot rollback. The following can resurrect closed authority:

- Restoring landing privacy state before an accepted withdrawal that worker never received.
- Restoring source/import/context/outbox data before a durable worker close.
- Restoring worker DB, privacy files and config attestation together to an older mutually consistent snapshot.
- Failing over to a replica that lost an acknowledged queue/closure commit.

Short leases, signatures, UUID generations and counters in the same restored database do not detect all of these failures. Worker remembered control versions detect only regressions it previously observed. A never-delivered withdrawal can disappear without any conflicting worker high-water.

Supported choices are:

1. **RPO-zero accepted privacy commits, with restore quarantine:** approved synchronous durability/failover and mandatory operator recovery for any rollback or uncertain loss.
2. **Potentially lossy restoration:** disable renewal/positive attribution first; durably retire/revoke the entire old landing source generation at worker; cancel its pending imports/outbox work under the existing dispatch fence; then activate a new higher policy epoch/new generation with fresh consent and binding. Do not guess which individual owners lost withdrawals.

If automatic safety across coordinated whole-stack rollback is required, a genuinely nonrollbackable external high-water/retirement anchor is necessary. This document neither claims such infrastructure exists nor silently adds it. Operator procedure with fail-closed startup is the required default until a reviewed anchor exists.

## Source generation retirement

Retirement is monotonic and runs under the existing privacy-close/exclusive dispatch boundary. It atomically marks generation retired, closes source/import/transaction authority, cancels unsent associated work, erases raw-bearing source/recovery data and preserves necessary replay/tombstone evidence. Acknowledgement is returned only after commit. A send already holding the dispatch boundary may be in flight and cannot be recalled.

Freshness memory is invalidated for the retired generation. Delayed source capture, authorization, redemption, renewal and positive events receive `410 GENERATION_RETIRED` or their internal fail-closed equivalent. Old cookies/ciphertexts do not authorize new generation ownership. Decrypt-only context keys remain available for authenticating legitimate historical withdrawal as required by existing key-retention rules.

## Backup, key and clock obligations

The landing queue/control/tombstones are privacy-critical durable state even though they contain no raw click. Backups/PITR policy must account for accepted withdrawals; a backup cannot be restored as if it were a fresh empty state. Encryption-at-rest status for either existing or proposed infrastructure is unknown until operational evidence is obtained.

Source ciphertext encryption keys, service authentication keys and existing CAPI context/hash keys have different purposes. Rotation/retirement procedures must preserve required decryption/authentication until all dependent lifetimes end or their generation is durably retired. Do not rotate the existing worker-only HMAC key as a shortcut: existing code pins its fingerprint and documents a separate destructive migration requirement.

Both relevant server clocks must have known absolute error at most five seconds for the candidate synthetic ten-second cross-server margin. This timing profile is a contract candidate, not an additional operator approval inferred from approval of Policy B30/10 and central custody. Unknown clock quality suspends positive capture/import. Availability freshness uses worker `CLOCK_BOOTTIME` or an equivalent suspend-aware elapsed clock, a new boot identifier after restart and fixed 30-second challenge deadlines; it does not depend on synchronized UTC clocks. Whole-VM snapshot restoration remains rollback, not a harmless clock event.

## Evidence

Backend-relative locations:

- `axonos_gate/migrations/006_x402_settlement_recovery.sql:1`: gate/ledger-only manual migration.
- `axonos_gate/migrations/007_x402_admission_lifecycle.sql:1`: financial migration after 006.
- `axonos_gate/migrations/apply_x_capi_migrations.sh:51`: target attestation; `:102`: absent/v4 branches; `:110`: unconditional 004 grants/postflight.
- `docker/x-capi-worker/bootstrap_x_capi_db.sh:150`: independent absent/v4 preflight restriction.
- `axonos_gate/x_capi_worker.py:47`: schema version 4; `:574`: strict role isolation; `:919`: 30-second cached schema attestation.
- `axonos_gate/x_capi_worker.py:1492`: durable config-guard verification; `:1539`: higher-epoch quarantine recovery; `:4985`: startup recovery ordering; `:5116`: worker lifecycle startup.
- `docs/X_CAPI.md:437`: single-host/single-worker requirement; `:563`: coordinated snapshot rollback warning and external-anchor limitation; `:571`: HMAC-key restoration constraints.

These are inspected repository facts, not assertions about the running production host.
