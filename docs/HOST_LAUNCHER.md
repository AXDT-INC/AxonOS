# Host Launcher Service (Non-Nested Deployment)

Use this when AxonOS gate runs in a container and must **not** run Docker itself.
You can run the launcher either:

- manually on host (`python3 axonos_gate/session_launcher_service.py`), or
- as a dedicated docker-compose service (`axonos-launcher`) that has host Docker socket access.

## Architecture

- `axonos_gate` scheduler allocates exclusive GPU IDs and calls launcher HTTP API.
- Host launcher (`axonos_gate/session_launcher_service.py`) runs on the Docker host.
- Host launcher performs `docker run --gpus device=...` and `docker rm -f`.

## Configure Gate Container

Set in gate environment:

- `AXGT_USER_CONTAINER_ENABLED=true`
- `AXGT_SESSION_LAUNCHER_MODE=http`
- `AXGT_SESSION_LAUNCHER_URL=http://<host-or-service>:8090`
- `AXGT_SESSION_LAUNCHER_TOKEN=<shared-secret>`
- `AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS=10`

## Configure Host Launcher

Set on host:

- `AXGT_SESSION_LAUNCHER_TOKEN=<same-shared-secret>`
- `AXGT_HOST_SESSION_CONTAINER_IMAGE=<image to launch per user>`
- optional `AXGT_HOST_SESSION_CONTAINER_COMMAND=/startup.sh`
- optional `AXGT_HOST_SESSION_CONTAINER_NETWORK=<docker-network>`
- optional `AXGT_HOST_SESSION_CONTAINER_EXTRA_ARGS=...` (avoid duplicating `--shm-size`; use next line instead)
- optional `AXGT_HOST_SESSION_CONTAINER_SHM_SIZE=32g` (default when unset; matches main `axonos` `shm_size` intent for GLX)
- optional `AXGT_HOST_SESSION_ENV_PASSTHROUGH=AXGT_CHAIN_ID,AXGT_RPC_URL,...`
- optional bind:
  - `AXGT_SESSION_LAUNCHER_BIND_HOST=127.0.0.1`
  - `AXGT_SESSION_LAUNCHER_BIND_PORT=8090`

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
The main `axonos` gate container remains non-nested.

## API Contract

### `POST /launch`

Request JSON:

```json
{
  "session_id": 42,
  "wallet_address": "0x...",
  "requested_profile": "medium",
  "assigned_gpu_ids": [2, 3]
}
```

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

Response JSON:

```json
{
  "ok": true,
  "stopped": "abc123..."
}
```

### `GET /healthz`

Returns `{"ok": true}`.
