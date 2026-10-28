# Human CAPI release scope

This release includes human Base USDC, native ETH on configured eligible chains,
and enabled direct AXGT deposit attribution. The first eligible deposit milestone
is deduplicated across rails. Agent x402 marketing attribution remains excluded;
existing financial, session, GPU and payment verification behavior is preserved.

Includes independent F2 original-bearer/browser ownership, consent/GPC/withdrawal,
privacy worker, durable outbox, replay suppression, schema008, frozen handoff v2,
secret-mounted account/VNC generation, and no-cache off-server build preparation.
CAPI defaults off; handoff remains absent/disabled. Financial migrations006/007
and proposed x402 recovery/admission/cutover are not included.

The retained launcher declaration preserves an existing deployment image and
settings; it does not authorize launcher recreation. These non-secret image IDs
are deployment-specific and must be validated before using this configuration.

Current reconciled source validation: 284 tests and 166 subtests passed, including
three-rail/F2, payment/x402 regressions, scientific build constraints, required
secret mounts, nonce/cache policy and checksum-before-extraction checks. The
archived CI recipe is tested without enabling workflows. No image build was run.

Historical source validation: 317 tests and257 subtests; disposable
PostgreSQL outbox64 passed with one intentional browser skip. These counts overlap
with narrower runs and are not proof of live readiness. Optional post-commit
attribution is best effort; financial success is independent of its delivery.

Validated build-source corrections are reconciled: scientific dependency installation
has a 30-minute timeout; Kubo v0.24.0 uses its official release archive with pinned
SHA-512 verification before extraction; synthetic prefix-cache reuse requires a
fresh non-secret nonce before both credential-consuming steps. Default builds and
worker builds retain --no-cache. No synthetic image is authorized for production.
Publication and deployment require separate operational approval.
Production password-derived files remain embedded in production-authentication
images. Synthetic-authentication images must not be deployed as production.
Historical local assessment inventories and obsolete transfer instructions are
not part of this publication branch. The frozen contract is copied byte-for-byte;
its historical design statements retain their frozen revision context.
