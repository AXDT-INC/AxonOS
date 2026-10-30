# Landing-to-app attribution

## Shared browser privacy preferences

The optional shared flow collects one advertising-measurement choice for the
landing page and app. The landing embeds `/privacy-preferences.html` from the
app origin. Its controls offer **Accept optional**, **Reject optional**, and
**Preferences** with an X conversion-sharing category. A valid saved choice is
reused on arrival in the app; direct app visitors receive the same controls.
Privacy settings remain available on both pages. Navigation and wallet access
do not depend on accepting, and an unanswered choice does not permit sharing.

This uses the existing gate, context encryption, CAPI worker and schema 5. It
does not require landing keyrings, Vault objects, a landing database, a separate
privacy worker, or the legacy `X_HANDOFF_ENABLED=true` protocol. The X access
token remains worker-only. No new GitOps secret resources are needed.

The app-host `__Host-axonos_privacy_v1` cookie contains only the choice, policy
version/epoch and a fixed deadline (at most the configured attribution TTL,
capped at 90 days). It is Secure, SameSite=Strict, Path=/, with no Domain. It is
readable by the app's preference script, not the landing script. The frame sends
only bounded sizing information to its exact configured parent; it accepts no
postMessage commands to grant sharing. No click ID or grant is passed to it.

Each app tab still obtains its own server-encrypted attribution ticket and
passes the existing click, CSRF, policy and verified-wallet checks. Before a
grant can be used, its withdrawal capability is saved in a bounded app-origin
localStorage registry under an origin-wide Web Lock. The registry is used only
for withdrawal, never to restore a grant or wallet binding into another tab.
Rejecting on either page blocks browser producers, then revokes all registered
unexpired handles through the existing worker. Pending withdrawals persist and
remain suppressed until acknowledged. A new acceptance cannot erase them.
The registry holds at most 16 live handles; storage failure or exhaustion blocks
new sharing. Expired capabilities are removed at the next privacy check without
extending their lifetime. A separate rejection marker persists until a new
explicit acceptance, so a failed cookie write cannot resurrect an older allow
choice on reload. No new grant is issued while withdrawal is pending.

Choices apply to this browser and storage partition, not another device.
Blocked cookies/storage or missing Web Locks leave sharing off. If a browser
isolates frame storage, a missing choice in the app must be collected again;
it is never inferred from navigation. Clearing browser storage also removes
its withdrawal capabilities; previously sent/in-flight events cannot be
recalled. GPC suppresses sharing. These controls manage X attribution only,
not unrelated trackers or a complete site-wide privacy policy.

### Activating the shared flow

1. Build the updated backend through the production controller.
   Keep the browser-click-v1 contract and `X_HANDOFF_ENABLED=false`.
2. Set **one additional non-secret backend configuration value**, without a
   trailing slash: `X_CAPI_PRIVACY_LANDING_ORIGIN=https://your-domain.example`
   where `X_CAPI_ALLOWED_ORIGIN=https://app.your-domain.example`. Only this exact
   HTTPS same-site parent may frame the preferences document. Other app/API
   routes retain their frame denial. Invalid configured parents fail config
   validation. Empty preserves the app-only controls below.
3. Increment the existing consent policy epoch for this notice/storage change
   through the controller's normal worker recovery/configuration checks. Do not
   reuse an older epoch or reinterpret existing grants as shared preferences.
   This retires earlier grants that were not registered for browser-wide
   withdrawal. Keep the supported `x-capi-browser-v1` policy version. Deploy the
   backend with these settings before publishing the landing update.
4. Publish the landing build with the shared frame and browser-transfer
   selector. The landing's CSP permits frames only from the app origin in this
   mode. An ingress must preserve the preferences document's narrow frame CSP
   rather than add a blanket X-Frame-Options DENY to that document.
5. Verify a genuine acceptance -> app -> eligible conversion journey, rejection,
   direct app entry, cross-tab withdrawal and withdrawal retry. Confirm X
   acceptance separately; synthetic browser tests do not establish delivery or
   attribution in X Ads Manager. Do not change production credentials to test.

## Browser transfer (preferred for a new source)

Use `x-capi-browser-v1` when the landing page does not need to retain attribution
across visits. This path uses the existing AxonOS CAPI database, worker, encrypted
consent tickets and privacy fence. It needs **no new Vault objects, landing
control database, shared handoff keyrings, source generation or privacy sidecar**.
The existing X token remains backend-worker-only.

The landing keeps an eligible `twclid` in memory for at most two minutes and
scrubs the URL before rendering/analytics. An ordinary app-link click navigates
directly to the fixed app origin in the same tab, without a landing prompt,
with the click in a fragment; fragments do not reach the HTTP server. The app
scrubs it before loading resources and exposes an optional Conversion sharing
control in the app's footer privacy section when shared preferences are disabled.
In that app-only fallback, no choices open automatically,
including on page-cache restoration.
Sharing stays off unless the visitor opens that control and explicitly allows it;
ignoring the control or declining leaves the app usable.
The visitor-requested choices expand in the normal page flow: no floating panel,
overlay or nested scroll area covers the wallet or app controls. Actions appear
before expandable data and retention details. Both pages include expandable X
attribution privacy information. In the app, “Not now” closes the choices with
sharing off; it does not record a decline. The Conversion sharing control allows
the visitor to reopen them or revoke a grant. Closing choices returns keyboard
focus to that control if focus was inside them. Existing tab-scoped choices and
expiry rules remain unchanged; this is not a site-wide consent manager.
These notices describe this integration, not a complete site-wide privacy policy.

Ordinary visitors see no attribution panel. Modified clicks, middle clicks,
context-menu links, expired clicks, reloads and restored pages use ordinary
navigation without attribution. The landing does not retain a click or consent
cookie across visits. Attribution withdrawal after an app grant is owned by the
app's Conversion sharing control and existing durable worker revocation path.
GPC suppresses transfer and conversion sharing. No X tracking script is loaded.
The click is untrusted matching data, not signed campaign provenance, wallet
identity or payment authority. Existing verified-human conversion checks remain.

### Configuration and rollout

Keep the existing pixel/event IDs, protected token and other CAPI configuration.
For the new backend release, explicitly select:

```dotenv
X_CAPI_MODE=live
X_CAPI_PRODUCTION_TRANSPORT=pixel-token-v1
X_CAPI_CONSENT_POLICY_VERSION=x-capi-browser-v1
X_CAPI_TWCLID_CONTRACT_VERSION=browser-click-v1
X_CAPI_ALLOWED_ORIGIN=https://app.axonos.io
X_CAPI_TWCLID_CHARSET=url_safe
X_CAPI_TWCLID_MIN_LENGTH=8
X_CAPI_TWCLID_MAX_LENGTH=256
X_HANDOFF_ENABLED=false
X_CAPI_SEND_VALUES=false
```

Increase `X_CAPI_CONSENT_POLICY_EPOCH` above the previously deployed epoch when
switching policies (for example, 1 to 2); do not reset it or reuse old consent.
The worker's durable configuration guard must accept the new epoch/audience.
The production controller selects the token-only live overlay for this policy.
The separate `docker-compose.x-capi-handoff.yml` remains specific to v2.

1. Confirm the source has no existing v2 landing obligations/generation before
   retiring its privacy service. An already-used v2 source needs a deliberate
   closure/retirement process; do not switch it off to hide pending withdrawals.
2. Test/build the backend release and deploy through the existing controller.
   Keep its admission, provenance, renter protection and recovery checks. No
   core/payment database migration is needed; preserve the existing schema-5 DB.
3. Build the updated landing image and set its runtime
   `X_ATTRIBUTION_TRANSFER=browser-click-v1` using the existing GitOps deployment.
   Do not activate the old handoff component or provision its Vault objects.
   Do not mix the browser selector with v2 keyrings/generation configuration.
4. Verify the real landing UI, transfer, app grant, wallet binding, revocation,
   worker health and renter continuity. `handoff_required=false` is expected for
   browser transfer; CAPI live readiness and the existing privacy controls remain
   mandatory. Also verify ingress logs do not retain landing click queries.
5. With operator-approved real traffic, separately verify event creation, worker
   delivery, X acceptance and ad attribution. Local tests or mocked acceptance
   cannot establish X account reporting. Page views are not conversion events in
   this implementation. Do not manufacture conversions to activate a dashboard.

Do not apply the changed production configuration to services until the corresponding images are ready.
A policy change makes prior consent ineligible; returning users need a fresh
eligible click and explicit choice. Retain revocation capability during rollback,
and do not lower the policy epoch.

## Existing server-to-server handoff v2

The instructions below apply only to `x-capi-handoff-v2`. They are not needed for
the browser transfer above.


`X_CAPI_MODE=live` and an X access token are not enough to establish attribution.
The landing must capture an eligible click with consent, transfer attribution to
the application, and keep its privacy state available. The application then
produces eligible wallet, deposit and session events. Ordinary visits and page
views are not conversion events in this implementation.

`env.example` is documentation for new installations, not a runtime input or an
instruction to turn an existing live deployment off. Editing it alone does not
require a rebuild. Repairing missing runtime mounts/configuration does require
service replacement. The current controller also requires images from the exact
release revision. Its fresh credential-build nonce is used early in the main
Dockerfile and can invalidate expensive downstream layers; do not assume a
worker-only source edit means a cheap full-image build. Finish remote provisioning
and activation planning before starting that build. Do not bypass provenance or
credential checks to reuse an older image.

## Backend deployment requirements

The reviewed live Compose overlay now supplies the handoff protocol, withdrawal
policy B, fixed timing parameters and worker-only keyring mounts. Set
`X_HANDOFF_SOURCE_GENERATION` to the operator-provisioned canonical UUIDv4 shared
with the landing. Preserve the configured consent policy epoch on both sides.
Do not invent a replacement generation for an existing source or restore.

Provision these regular files outside the checkout, owned by UID/GID 10001 with
mode 0400 or 0600:

| Backend path | Purpose | Shared with landing? |
| --- | --- | --- |
| `/etc/axonos/x-handoff-service-keyring` | Authenticate source requests and responses | Yes |
| `/etc/axonos/x-handoff-source-data-keyring` | Encrypt backend source data | No |
| `/etc/axonos/x-capi-access-token` | X delivery credential, raw token only | No |

Both handoff keyrings use the existing format
`{"primary_kid":"KEY_ID","keys":[{"kid":"KEY_ID","key_base64url":"BASE64URL_32_BYTES"}]}`.
The displayed values are placeholders. Generate independent keys through the
operator's protected provisioning workflow. Never place them in Git, build
arguments, `.env`, browser configuration or terminal output.

The privileged metadata helper must be updated using the installation procedure
in [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md). It inspects file metadata
only. Older installations without handoff file metadata cannot admit a live
deployment. Off deployments do not require the new optional keyring files.

Generation activation is a separate persistent operation. The existing
`HandoffStore.provision_generation` requires the matching configuration guard and
refuses an existing generation. An authorized operator must establish the
generation and recovery state before positive traffic. Do not insert a fabricated
recovery acknowledgement or bypass the guard. No core/payment migrations are
needed for these wiring and readiness changes.

## Landing requirements

Use a landing image containing `server/launch.mjs`, the production source
transport and `server/handoff/privacy-worker.mjs`. Configure both landing and the
separately supervised privacy worker with:

- `X_HANDOFF_TRANSPORT=production`, `X_HANDOFF_SYNTHETIC_ONLY=false`;
- `X_HANDOFF_PROTOCOL=axonos-first-party-handoff-v2`, `X_HANDOFF_WITHDRAWAL_POLICY=B`;
- the same source generation and policy epoch as the backend;
- protected service keyring, local control DB URL and recovery assurance files.

The web process also needs its own distinct receipt keyring. The privacy worker
must not receive that receipt key. Neither process receives the X token, backend
DB credentials, source-data keyring or context/hash keys.

The dedicated landing control DB must be reachable on loopback. Apply the landing
repository's `database/handoff-control.sql` as a separate migration owner and
grant only the runtime privileges specified there. The singleton control row and
recovery assurance require explicit provisioning; schema creation does not
activate them. Persist this DB across pod replacements. An empty DB, restored
snapshot or an arbitrary `rpo-zero-verified` file is not evidence of safe recovery.

In Kubernetes, mount each protected file with `subPath`: the reader intentionally
rejects symlink paths. Use root ownership, mode 0440 and the pod's restricted
filesystem group so UID 1001 can read it. File rotation requires a coordinated
pod replacement because `subPath` mounts do not refresh in place.

## Activation and verification order

1. Review and build the backend and landing fixes; record immutable image IDs or
   digests. Prepare secrets and the dedicated landing DB without activating
   browser capture. Confirm ingress does not log sensitive query strings, cookies,
   bodies or authentication headers.
2. Establish matching generation, epoch and verified recovery state on both
   sides. Prepare the landing privacy worker with handoff enabled, keeping the
   landing web process's positive capture disabled.
3. Use the existing production controller for backend deployment. Its existing
   gate must expose the compatible source route while the worker acquires its
   lease. If the gate is still off or lacks that route, coordinated activation is
   a blocker: do not bypass admission or the worker health check.
4. Confirm the running privacy worker acknowledges freshness and backend health
   reports `handoff_required=true`, `handoff_ready=true`,
   `handoff_clock_trusted=true`, `handoff_lease_fresh=true` and `live_ready=true`.
   A missing runtime or expired lease must fail live readiness. A health probe
   reads the running worker's lease; it never creates or renews one.
5. Enable landing capture only after those checks pass. With explicit approval,
   validate a genuine consenting click-to-application journey and a real eligible
   conversion. Observe aggregate outbox/delivery outcomes and the X event
   dashboard. Do not send fake conversions to make the dashboard look active.

Keep `X_CAPI_SEND_VALUES=false`; revenue values remain unsupported in live mode.
Follow [X_CAPI_LIVE_TRANSPORT.md](X_CAPI_LIVE_TRANSPORT.md) for token, event mapping,
delivery and rollback requirements. Disable positive capture/delivery first on
rollback, while retaining privacy closure and pending withdrawal recovery.

The code and manifest fixes alone do not prove a deployed landing, fresh lease,
accepted X conversion or successful attribution. Record each separately.
