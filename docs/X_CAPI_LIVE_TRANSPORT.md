# X CAPI production transport activation

This source adds an opt-in transport. It does not activate a deployment. Default
`X_CAPI_MODE=off` and an unset `X_CAPI_PRODUCTION_TRANSPORT` cannot collect or send
live conversions. Mock acceptance is not evidence of X ingestion, attribution,
reporting or optimization. Existing deployment overlays remain off and isolated;
this change does not alter images, networks, secret mounts or GitOps.

## Transport and eligibility

The worker sends one conversion to
`https://ads-api.x.com/12/measurement/conversions/{pixel_id}` using the dedicated
Events Manager Conversion API token in `X-Pixel-Token`. It sends only
`conversion_timestamp` (original integer Unix milliseconds), `event_id`,
`identifiers: [{twclid: ...}]` and stable `conversion_id`. This is the token
contract in [X's official template](https://github.com/twitter/x-ads-conversion-api-gtm-template/blob/e637e1c4af64d3b4ff1a4da9b97e950d13ad5dc9/template.tpl).
It is not the OAuth Ads API credential flow. Before provisioning, the operator
must confirm the account's token permissions cover the selected pixel and event
IDs and that this direct token contract remains supported for the account.

X documents URL capture of `twclid` as matching input in its
[web conversions guide](https://docs.x.com/x-ads-api/measurement/web-conversions).
The local parser rejects ambiguous or forbidden identifier shapes; it cannot
cryptographically prove X issued a click. A click never authorizes a wallet,
payment, grant, or session. Live mode requires frozen handoff v2, signed landing
observation, independent landing/application consent, GPC suppression, current
browser ownership and original F2 authority. Only existing verified human USDC,
ETH and enabled AXGT milestones qualify; agent x402 remains excluded. No value,
wallet, bearer, IP, user-agent, email or full URL is sent to X.

## Operator-controlled credentials and configuration

Provisioning is a separate approval-controlled action. Do not put secrets in a
checkout, image, command line, build argument, browser bundle or environment.
No existing environment file needs to be inspected to prepare this change.

1. In X Events Manager, an authorized account operator selects the correct event
   source/pixel and creates or verifies the intended conversion events. Obtain a
   dedicated **Conversion API pixel access token**, not a general API bearer or
   OAuth consumer secret. Verify account permission and token scope with X.
2. Through the deployment's secret manager, mount that token read-only to the
   dedicated worker at `/run/secrets/x_capi_access_token`. Alternative
   `X_CAPI_ACCESS_TOKEN_FILE` must remain beneath `/run/secrets`. Use worker UID
   ownership (or root), mode 0400/0600, one regular file, no symlink/hard link,
   no newline, printable non-whitespace ASCII, 16–8192 bytes. The gate and landing
   must not receive this token. Missing/invalid files fail closed before claim.
3. Provision independent protected files for the existing context encryption,
   internal hash, handoff service MAC, source-data encryption and landing receipt
   keys. Share only the handoff service keyring between landing and worker.
   Configure the dedicated least-privilege databases and verified recovery
   assurance through their existing contracts. Never reuse one key for purposes.
4. Set the non-secret backend contract consistently on gate and worker:
   `X_CAPI_PRODUCTION_TRANSPORT=pixel-token-v1`,
   `X_CAPI_CONSENT_POLICY_VERSION=x-capi-handoff-v2`,
   `X_CAPI_ALLOWED_ORIGIN=https://app.axonos.io`, pixel/event IDs, explicit positive
   policy epoch (`X_CAPI_CONSENT_POLICY_EPOCH`), deployment ID
   (`X_CAPI_DEPLOYMENT_ID`), `X_CAPI_PIXEL_ID` and the desired mappings
   `X_CAPI_EVENT_WALLET_VERIFIED`, `X_CAPI_EVENT_DEPOSIT_COMPLETED`,
   `X_CAPI_EVENT_SESSION_STARTED`. Pin `X_CAPI_TWCLID_CONTRACT_VERSION` to the
   reviewed observation policy (for example `handoff-v2-observation`),
   `X_CAPI_TWCLID_CHARSET=url_safe`, `X_CAPI_TWCLID_MIN_LENGTH=8` and
   `X_CAPI_TWCLID_MAX_LENGTH=256`, matching the frozen observation parser. Preserve
   production chain IDs and wallet exclusions. Keep `X_CAPI_SEND_VALUES=false`.
   Remove every active `X_CAPI_ALLOW_TEST_*` setting; any one blocks production.
   Keep `X_CAPI_MODE=off` until the final activation step.
5. On landing and its privacy worker, set `X_HANDOFF_TRANSPORT=production` and
   `X_HANDOFF_SYNTHETIC_ONLY=false`. Do not configure a synthetic transport file.
   Preserve the frozen generation, epoch, policy B, fixed source URL, keyrings,
   local dedicated control database, RPO-zero assurance and trusted clock.
   `X_HANDOFF_ENABLED=false` is the initial state. Even when disabled, keep the
   privacy worker and authenticated revoke/status transport available.

## Network, privacy and supervision prerequisites

An approved later deployment change must permit worker DNS and outbound verified
TLS 1.2+ to `ads-api.x.com:443`, and landing outbound verified TLS to the exact
`app.axonos.io:443` source endpoint. The source endpoint must route to the
strict handoff adapter and authenticate request/response MACs. No configurable
vendor URL, redirect, proxy or ambient cookie authentication is used. Never
turn off hostname/certificate validation. TLS key logging and trust overrides
are rejected; install the normal system CA roots. Landing also rejects
`NODE_TLS_REJECT_UNAUTHORIZED`, `NODE_EXTRA_CA_CERTS`, Node HTTP debugging,
TLS-keylogging and inspector startup options.

Preserve no-store/private caching, no-referrer, nosniff, frame denial, restrictive
CSP and exact-origin/Fetch-Metadata controls on attribution routes. Reverse
proxies, CDNs, tracing and error collectors must not log query strings, raw URLs,
bodies, cookies, authorization, service MACs, pixel tokens, receipts or click IDs.
Disable cache, buffering-based body capture, redirects and request mirroring for
these routes. Preserve bounded request/response sizes and upstream timeouts.

Supervise the dedicated worker externally, including exit 75 for handoff process
replacement and termination at the 20-second absolute X request deadline. The
per-socket timeout is five seconds. Preserve the shared OS privacy fence, secure
RPC/ingest sockets, schema/config attestations and the fresh 30-second handoff
lease. A worker must never restart with fabricated freshness. The normal loop
allows at most four attempts/second; use one supervised sender per privacy fence.
Confirm the account's current X limits before activation.

Monitor aggregate readiness, queue depth/oldest age, retry count, accepted/failed/
cancelled counts, worker heartbeat, authentication pause, cooldown, handoff lease,
clock trust, quarantine and landing withdrawal backlog. Never attach identifiers
to metrics. HTTP 401/403 durably pauses sending until an operator repairs access.
HTTP 429 stores a global durable cooldown: `paused_reason=rate_limited` and
`paused_at` is its retry deadline (for other pause reasons it remains start time).
A valid Retry-After is honored beyond one hour; missing/invalid values use bounded
exponential jitter. No job, including newly arrived work, bypasses this cooldown.
Expiry cleanup and revocation continue while paused. Network errors and
408/425/5xx retry with the same conversion ID and original timestamp, subject to
age/consent expiry. Redirects, oversized responses and unrecognized success bodies
fail closed; alert on `unknown_success_body` and do not blindly replay them.
Only HTTP 200 with integer `data.conversions_processed == 1` is acknowledged.

Local durable uniqueness prevents creating a second conversion for the same
milestone. Delivery is at-least-once: an X acceptance followed by lost response
or local crash may resend the same ID. Confirm vendor deduplication behavior and
window for this account before activation; mock tests cannot prove remote
exactly-once counting. Do not manually create a new ID for uncertain delivery.

## Activation order, recovery and rollback

1. Review both source commits and disposable evidence; approve a separate release
   and deployment plan. Confirm contract manifest/hash equality and all existing
   database/role, ingress, logging and recovery prerequisites. No migration is
   included in this transport change.
2. Provision protected credentials and networks as above with both features off.
   Establish healthy cleanup, authenticated revoke/status, clock assurance and
   privacy worker supervision before allowing capture.
3. Select a fresh, consistent deployment epoch/generation using the existing
   handoff recovery procedure. A dry-run audience is not promoted or replayed as
   live. Verify durable configuration guards and privacy fences for the live
   scope, and establish fresh landing-control attestation.
4. Only after operator approval, set backend `X_CAPI_MODE=live`, verify readiness,
   enable the gate handoff adapter with `X_HANDOFF_ENABLED=true`,
   then enable landing `X_HANDOFF_ENABLED=true`. Stop if any dependency, clock,
   consent, freshness, token or scope check fails. Do not bypass an attestation.
5. Separately authorize a limited real, consenting-human validation and verify X
   Events Manager ingestion and attribution. Synthetic credentials/mock requests
   are never sent to X by this validation procedure.

For immediate disablement set backend mode off (or remove the production
transport opt-in), then stop positive landing capture. Keep revoke/status,
privacy retry, cleanup and key access running until pending withdrawals commit.
A send already inside the privacy dispatch fence may finish; disablement cannot
recall an event X already accepted. Withdrawal committed before dispatch wins;
a busy/failed withdrawal remains pending, never falsely acknowledged. Restore
connectivity and drain closure before considering reactivation. After uncertain
restore, quarantine the old generation and use the existing epoch/recovery
procedure; never reconstruct consent or replay expired events. Do not clear an
authentication pause until the operator fixes the underlying credential problem.
Rollback to off-mode code/config without deleting queues, tombstones or privacy
state. Never run old sending code against an active live generation.

## Disposable verification

Run backend unit transport/control tests and the existing payment/session tests.
`axonos_gate/tests/run_x_capi_handoff_postgres.sh` provisions only a fresh private
/tmp PostgreSQL 15 cluster. Set `PG15_BIN` to an installed PG15 directory. With
`X_CAPI_TEST_LANDING_REPO` pointing to the companion source checkout and
`X_CAPI_TEST_NODE` to a supported Node binary, it also runs cross-repository
source TLS/MAC, F2/outbox retry/ack and withdrawal tests. The test replaces only
network destinations with loopback or recording sockets and uses synthetic keys.
Landing runs `npm run test:handoff` and the disposable PostgreSQL suite. These
checks prove local transport behavior, not account permissions or X ingestion.
