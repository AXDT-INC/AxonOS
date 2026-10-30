# Completing production handoff activation

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
