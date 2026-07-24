# Host Launcher Service (Non-Nested Deployment)

Use this when AxonOS gate runs in a container and must **not** run Docker itself.
You can run the launcher either:

- manually on host (`python3 axonos_gate/session_launcher_service.py`), or
- as a dedicated docker-compose service (`axonos-launcher`) that has host Docker socket access.

## Architecture

- `axonos_gate` scheduler allocates exclusive GPU IDs and calls launcher HTTP API.
- The central gate mints a short-lived, signed WebRTC capability bound to the exact
  compute-session ID, wallet, and per-session file key.
- Host launcher (`axonos_gate/session_launcher_service.py`) runs on the Docker host.
- Host launcher creates a private `axgt-session-net-<session-id>` bridge, attaches
  the central `axonos` gate and the matching tenant container, and performs
  `docker run --gpus device=...` / `docker rm -f`.
- Postgres and the launcher stay on the control network. Tenant containers do not
  receive database, launcher, payment/RPC, or fleet WebRTC signing credentials.

## Configure Gate Container

Set in gate environment:

- `AXGT_USER_CONTAINER_ENABLED=true`
- `AXGT_SESSION_LAUNCHER_MODE=http`
- `AXGT_SESSION_LAUNCHER_URL=http://<host-or-service>:8090`
- `AXGT_SESSION_LAUNCHER_TOKEN=<shared-secret>`
- `AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS=90`
- `WEBRTC_ENABLED=true` for desktop sessions
- `WEBRTC_AGENT_INTERNAL_KEY=<central-signing-secret>` when WebRTC is enabled

`WEBRTC_AGENT_INTERNAL_KEY` is a control-plane signing secret. Do not put it in
the session environment passthrough. The gate sends each session a separately
signed `AXGT_WEBRTC_AGENT_TOKEN`; the launcher never copies the fleet secret into
the tenant container.

## Configure Host Launcher

Set on host:

- `AXGT_SESSION_LAUNCHER_TOKEN=<same-shared-secret>`
- `AXGT_HOST_SESSION_CONTAINER_IMAGE=<image to launch per user>`
- `AXGT_CHALLENGE_DB_URL=<control-database-url>` (required). The launcher checks
  the exact active row, wallet, GPU assignment, session file key, and runtime
  mode before every Docker launch, including shared-network compatibility mode.
  This credential is control-plane only and is never forwarded to tenants.
- optional `AXGT_SESSION_DB_CONNECT_TIMEOUT_SECONDS=5` (bounded to 1–30 seconds)
- optional `AXGT_HOST_SESSION_CONTAINER_COMMAND=/startup.sh`
- recommended `AXGT_HOST_SESSION_NETWORK_ISOLATION=true` (default)
- optional `AXGT_HOST_CENTRAL_GATE_CONTAINER=axonos` (default)
- optional `AXGT_HOST_SESSION_CONTAINER_NETWORK=<docker-network>` (compatibility
  fallback; required when per-session isolation is disabled)
- optional `AXGT_HOST_SESSION_CONTAINER_EXTRA_ARGS=...` (restricted; see
  [Security boundary](#security-boundary))
- optional `AXGT_HOST_SESSION_CONTAINER_SHM_SIZE=32g` (default when unset; matches main `axonos` `shm_size` intent for GLX)
- optional `AXGT_HOST_SESSION_ENV_PASSTHROUGH=WEBRTC_STUN_URLS,...`
  (media tuning only)
- optional bind:
  - `AXGT_SESSION_LAUNCHER_BIND_HOST=127.0.0.1`
  - `AXGT_SESSION_LAUNCHER_BIND_PORT=8090`

## Security boundary

The compose default treats every `axgt-session-*` container as a tenant data-plane
workload:

- Each session gets a dedicated Docker bridge named
  `axgt-session-net-<session-id>`. Only that tenant and the central `axonos`
  container join it. The network is removed when the session stops.
- The tenant agent reaches heartbeat on `http://axonos-gate:8889` and the internal-only
  WebRTC agent API on `http://axonos-gate:8890`. Port 8890 is not published on the host.
- Identity, file-transfer key, and signed WebRTC capability are injected explicitly
  for that session. The capability is checked against its signature and active
  database row; it is not a fleet-wide credential.
- A renewed capability is persisted inside the tenant at
  `/run/axonos/webrtc-agent-token` with an agent-root-owned `0700` parent and
  `0600` file. This lets the Supervisor-managed agent reload it after a process
  restart in the same container; it does not survive container replacement or
  bypass central signature, identity, and live-allocation validation on any
  request.
- `AXGT_HOST_SESSION_ENV_PASSTHROUGH` is for media configuration only. Keep chain,
  RPC, billing, database, launcher, and signing values in the control plane. Names
  outside the built-in media allowlist are rejected even when explicitly listed.
- Session containers are launched with `NET_RAW` dropped.
- Unsafe extra arguments cannot override networks, ports, host mounts/devices,
  Linux capabilities, namespaces, runtime/security settings, hostname, or
  environment/ownership labels; `--privileged`, `--use-api-socket`, label files,
  and host-side container-ID files are also ignored. Resource and logging
  knobs such as `--cpus` and `--memory` remain available. Configure shared memory with
  `AXGT_HOST_SESSION_CONTAINER_SHM_SIZE` rather than duplicating it in extra args.

Disabling `AXGT_HOST_SESSION_NETWORK_ISOLATION` restores a shared-network
compatibility mode and weakens tenant separation. Use it only for controlled
legacy deployments. The shared network must be explicitly configured so the
tenant can resolve the central `axonos` alias. Isolated direct-launch mode also
requires `AXGT_CENTRAL_GATE_CONTAINER` to name the running central gate
container; network attachment fails closed when that container is absent.

The boundary prevents one session from authenticating as or directly joining
another session through the launcher. It is not a substitute for host/kernel,
GPU-driver, or container-runtime isolation, and the signed capability remains a
bearer token inside its owning container. TURN media credentials may also be
shared media-plane values. The Docker-socket launcher is a trusted host control
service and must remain unreachable from tenant networks.

## First Upgrade to Isolated Sessions

This boundary intentionally does not adopt old `axgt-session-*` containers. They
lack the signed capability, ownership labels, and isolated network required by
the new launcher. Before the first upgrade:

1. End/drain every active compute session.
2. Confirm no legacy `axgt-session-*` container remains on the Docker host.
3. Rebuild and recreate the complete compose stack, not only the central gate.

In HTTP mode, launcher `GET /healthz` fails closed and names any legacy
unlabeled session container, so Compose will not start the central gate over a
mixed old/new runtime. In direct `docker_cli` mode the gate performs the same
scan at startup, and every launch repeats it so a later legacy container also
blocks new allocation. After this one-time drain, launcher retries are
idempotent and per-session lifecycle operations are serialized. Reuse also
requires an exact non-secret runtime-contract digest and exact Docker-network
membership. The digest covers compute ID, wallet, profile, GPU assignment,
file-key fingerprint, desktop/SSH mode, selected template, SSH public-key
fingerprint, image, and network. A running container from a different topology
or identity contract is removed and recreated instead of being adopted.

Treat later changes to network isolation, image, template, SSH identity, or
other runtime-contract inputs as drained maintenance too. Existing active
allocations are not live-migrated merely because an operator changes `.env`;
end them before rebuilding so every new claim is checked against one topology.

Because older tenant containers may have received control-plane values, audit
the complete historical value of `AXGT_HOST_SESSION_ENV_PASSTHROUGH` and treat
every credential it exposed as compromised. In particular, older defaults could
forward API-key-bearing `AXGT_RPC_URL` / `USDC_RPC_URL` values, and older example
configuration suggested forwarding `X402_SETTLEMENT_PRIVATE_KEY`. Rotate those
RPC-provider keys and settlement keys, plus any customized `AXGT_ADMIN_SECRET`,
`AXGT_SESSION_LAUNCHER_TOKEN`, and every other credential found in the old
passthrough. Also rotate the Postgres credential and
`WEBRTC_AGENT_INTERNAL_KEY` after the drained upgrade.
Coordinate a Postgres password rotation with the database and `.env`; rotating
the WebRTC signer invalidates outstanding capabilities, so do it only with zero
active sessions.

## Run (Manual Host Mode)

```bash
python3 axonos_gate/session_launcher_service.py
```

## Run (Compose-Managed Mode)

`docker-compose.yml` now includes an `axonos-launcher` service.

1. Set in `.env`:
   - `AXGT_SESSION_LAUNCHER_TOKEN=<shared-secret>`
   - `AXGT_USER_CONTAINER_ENABLED=true`
   - `AXGT_SESSION_LAUNCHER_MODE=http`
   - `AXGT_SESSION_LAUNCHER_URL=http://axonos-launcher:8090`
2. Start stack:
   - `docker compose up -d --build`
3. Verify launcher:
   - `docker compose ps`
   - `docker compose logs axonos-launcher`

In compose mode, only `axonos-launcher` has `/var/run/docker.sock`.
The main `axonos` gate container remains non-nested. Postgres and the launcher use
`axonos_control`; the central gate also joins tenant networks on demand.

## API Contract

### `POST /launch`

Request JSON:

```json
{
  "session_id": 42,
  "wallet_address": "0x...",
  "requested_profile": "medium",
  "assigned_gpu_ids": [2, 3],
  "files_key": "<per-session-secret>",
  "webrtc_agent_token": "<signed-per-session-capability>"
}
```

The gate supplies `files_key` and `webrtc_agent_token` automatically. The host
launcher verifies the request against the exact live scheduler row and reuses an
already-running, correctly labeled container for an idempotent retry. These are
sensitive bearer values and must not be logged or supplied by an end user. Protect
the launcher API with `AXGT_SESSION_LAUNCHER_TOKEN` and a trusted control network.

Response JSON:

```json
{
  "ok": true,
  "container_id": "abc123...",
  "container_name": "axgt-session-42"
}
```

### `POST /stop`

Request JSON:

```json
{
  "session_id": 42,
  "container_id": "abc123..."
}
```

`container_id` is only a compatibility hint. The launcher resolves and removes
the deterministic container only when its ownership labels match `session_id`;
it never passes an arbitrary request value to `docker rm`.

Response JSON:

```json
{
  "ok": true,
  "stopped": "abc123..."
}
```

### `GET /healthz`

Returns `{"ok": true}` only when required configuration is present, Docker can
be inspected, and no legacy unlabeled session container remains. Otherwise it
returns HTTP 503 with an `errors` list.
