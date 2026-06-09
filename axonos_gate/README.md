# AXGT Gate for AxonOS

This module implements **prepaid deposit-credit billing** for AxonOS remote desktop access. Users deposit **AXGT** or **native ETH** to a revenue wallet, submit the transaction hash for verification, and receive usage minutes. Sessions consume minutes via heartbeat-based incremental billing.

## Official References

- **AxonDAO website**: `https://axondao.io`
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Explorer**: `https://etherscan.io/address/0x6112C3509A8a787df576028450FebB3786A2274d`

## Overview

- **Authentication**: Wallet ownership is proven via signed challenge (`personal_sign`); one-time, wallet-bound nonces.
- **Deposit-credit**: Users deposit AXGT or native ETH to a configured **revenue wallet**. They submit the **transaction hash**; the backend verifies the tx on-chain (confirmations, contract/value, sender/recipient, amount) and credits prepaid minutes. No escrow, no oracle, no trust in client-reported amounts.
- **Billing**: Active session usage is deducted **incrementally** on each session heartbeat. When remaining minutes reach zero, access is denied until the user deposits again. Unused credits persist.
- **Persistent Storage Billing**: When offline, users are billed for persistent storage volume usage (defaults to `0.05` minutes per GB/hour). Charges accrue to the balance, allowing it to go negative (accumulating debt). Once the balance falls below a configurable negative debt threshold (default: `-1440.0` minutes / -24 hours), the persistent volume is pruned/deleted immediately to reclaim disk space.
- **Accounting**: Postgres-backed deposit ledger and audit ledger; all balance changes are logged.

## User flow

1. Connect wallet and sign the challenge (existing flow).
2. **Pay in-page** (recommended): use **Send min AXGT (wallet)** or **Send min ETH (wallet)** — the UI submits the tx and auto-calls verify-deposit until credited. Or deposit manually and paste the tx hash.
3. Submit the transaction hash via `POST /api/auth/verify-deposit` (requires auth token) if not using in-wallet pay.
4. Backend verifies the tx and credits minutes; response includes `remaining_minutes`.
5. Claim a session; during the session the client sends heartbeats; each heartbeat bills elapsed time since last billing checkpoint.
6. When remaining minutes reach zero, the session is terminated and access is denied. Offline storage continues to bill against the balance (accruing debt) unless the volume is deleted or balance drops below the negative debt limit threshold.

## Configuration

### Required

- `AXGT_CONTRACT_ADDRESS`: AXGT ERC-20 contract address.
- `AXGT_CHAIN_ID`: Ethereum chain ID.
- `AXGT_RPC_URL`: Ethereum RPC endpoint (for balance and tx/receipt verification).
- `AXGT_REVENUE_WALLET`: Wallet address that receives AXGT deposits; must match the recipient in verified transfer events.
- `AXGT_CHALLENGE_DB_URL`: Postgres connection string for challenges, auth tokens, sessions, **deposit ledger**, and **audit ledger**. Required for deposit-credit billing.

### Deposit verification

- `AXGT_DEPOSIT_MIN_CONFIRMATIONS`: Minimum block confirmations before crediting (default `6`).
- `AXGT_MIN_DEPOSIT`: Minimum AXGT amount per deposit to accept (default `100`).
- `AXGT_CREDIT_PER_100_AXGT_MINUTES`: Usage minutes granted per 100 AXGT deposited (default `60`).
- `AXGT_ENABLE_ETH_DEPOSITS`: Enable native ETH deposit rail (default `true`). Set `false` to disable ETH verification/crediting and hide ETH top-up UI.
- `ETH_MIN_DEPOSIT`: Minimum native ETH per deposit (default `0.0005`, ~parity with min AXGT tier at typical rates).
- `ETH_CREDIT_PER_ETH_MINUTES`: Minutes per 1 ETH (default `120000` → min ETH deposit ≈ same minutes as min AXGT).
- `AXGT_WARNING_THRESHOLD_MINUTES`: Warning threshold for UI (e.g. low balance).

### Persistent storage (new)

- `AXGT_PERSISTENT_STORAGE_ENABLED`: Enable user persistent named volumes (default `true`).
- `AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX`: Docker volume prefix (default `axgt-user-storage-`).
- `AXGT_PERSISTENT_STORAGE_MOUNT_PATH`: Mount path inside desktop sessions (default `/home/aXonian`).
- `AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS`: Billing sweep interval (default `3600` / 1 hour).
- `AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES`: Max negative credit debt allowed before volume pruning (default `-1440.0`).
- `AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES`: Offline storage charge rate per GB/hour (default `0.05` compute minutes).

### Optional

- `AXGT_CORS_ORIGINS`: CORS allowlist for `/api/*`. Comma-separated origins or `*`.
- `AXGT_RATE_LIMIT_PER_MIN`: Per-client rate limit for verify calls; `0` to disable.
- `AXGT_AUTH_TOKEN_TTL_SECONDS`, `AXGT_CHALLENGE_TTL_SECONDS`, `AXGT_AUTH_COOKIE_NAME`, etc.: Auth/session tuning.
- `AXGT_SESSION_MAX_MINUTES`, `AXGT_HEARTBEAT_TIMEOUT_SECONDS`: Idle session cap (sliding on heartbeat) and stale-heartbeat timeout.
- `AXGT_ADMIN_SECRET`: If set, enables admin API (`/api/admin/*`) when request includes header `X-AXGT-Admin-Secret` or query `admin_secret`.
- `AXGT_EXPECTED_CONTRACT_ADDRESS`: If set, gate only accepts this contract address.

## Database schema (Postgres)

Created automatically when `AXGT_CHALLENGE_DB_URL` is set.

### axgt_deposits

- `wallet_address` (TEXT, PK)
- `deposited_amount_axgt`, `credited_minutes_total`, `consumed_minutes_total`, `remaining_minutes`
- `last_billed_at`, `created_at`, `updated_at`

### axgt_ledger (audit)

- `id`, `wallet_address`, `event_type`, `minutes_delta`, `axgt_delta`, `balance_after_minutes`
- `reference_tx_hash`, `reference_session_id`, `notes`, `created_at`, `created_by`

Event types: `deposit_credit`, `usage_deduction`, `refund`, `admin_adjustment`, `session_expiry`, `verification_reject`.

### axgt_verified_deposits (replay protection)

- `tx_hash` (PK), `wallet_address`, `sender_wallet`, `recipient_wallet`, `axgt_amount`, `credited_minutes`, `block_number`, `created_at`

## API Endpoints

### GET /api/auth/challenge?wallet_address=0x...

Returns a one-time challenge bound to the wallet.

### POST /api/auth/verify-wallet

Verify signed challenge and issue **auth token** (cookie + body) whenever the signature is valid—even if `verified` is false because there is no prepaid credit yet. Response includes `remaining_minutes`, `credited_minutes`, `auth_token`, etc. The UI needs the token to call **verify-deposit** and complete top-up on the **same origin** (e.g. tunnel to **6080**).

### GET /api/auth/wallet-status?wallet_address=0x...

Returns current deposit-credit status (no billing).

### POST /api/auth/verify-deposit

Verify a deposit by transaction hash. **Requires auth token** (cookie or header). Body: `{"wallet_address": "0x...", "tx_hash": "0x..."}`. The `wallet_address` must match the authenticated session. On success: `verified`, `credited_minutes`, `remaining_minutes`, `confirmations`, etc. While the tx is indexing or confirming, returns **HTTP 200** with `verified: false`, `pending: true`, `confirmations`, `required`, and `error` (client should poll). Hard failures (wrong sender, duplicate tx, below minimum) return **HTTP 400**.

### GET /api/config

Returns contract address, chain ID, revenue wallet, min deposit (AXGT and ETH), credit rates, warning threshold.

### Session and queue

- `GET /api/session/status`, `POST /api/session/claim`, `POST /api/session/heartbeat`, `POST /api/session/release`
- `POST /api/queue/join`, `POST /api/queue/leave`

Heartbeats trigger incremental billing; when remaining minutes reach zero the session is ended.
When `AXGT_GPU_PROFILES_ENABLED` and weighted billing are on (default), usage deducts
`wall_clock_minutes × assigned_gpu_count` from prepaid credit (e.g. Max = 8× burn rate).

### Admin (when AXGT_ADMIN_SECRET is set)

- `POST /api/admin/credit-minutes`: body `wallet_address`, `minutes`, optional `notes`
- `POST /api/admin/refund-minutes`: body `wallet_address`, `minutes`, optional `notes`
- `POST /api/admin/adjust-balance`: body `wallet_address`, `minutes_delta`, optional `notes`
- `GET /api/admin/ledger?wallet_address=0x...&limit=100`: audit ledger for wallet

## Components

- `axgt_verifier.py`: Challenge/signature verification; deposit-credit access (reads from deposit ledger).
- `deposit_ledger.py`: Postgres-backed deposits, ledger, verified-deposits; billing and admin helpers.
- `deposit_verifier.py`: Tx-hash verification (RPC, Transfer events, confirmations); credits via deposit_ledger.
- `session_manager.py`: Session scheduler + queue + heartbeat-based billing (calls deposit_ledger.deduct_usage). Supports private-beta single-session mode and feature-gated public-beta multi-session mode with exclusive GPU allocation.
- `session_launcher.py`: Adapter client for session runtime orchestration (`docker_cli` / `http` / `noop`).
- `session_launcher_service.py`: Host-side launcher API for non-nested deployments (`POST /launch`, `POST /stop`).
- `gate_server.py`: HTTP API and WebSocket proxy (port **8889** by default).
- `websockify_gate.py`: noVNC + WebSocket on **6080**; serves the same `/api/config`, `/api/auth/*` (challenge, verify-wallet, wallet-status, **verify-deposit**), and session/queue POSTs so a tunnel to 6080 alone can sign in and top up without hitting 8889.

## Public Beta Concurrency Architecture (v1)

Feature-gated by:

- `AXGT_MULTI_SESSION_ENABLED=true`
- `AXGT_GPU_PROFILES_ENABLED=true`

Optional user-container mode:

- `AXGT_USER_CONTAINER_ENABLED=true`

### What changed

- Session records now include `requested_profile`, `gpu_ids`, `container_id`, and `allocation_status`.
- Queue records are profile-aware (`requested_profile`, `requested_gpus`, `queue_reason`).
- Claim/queue APIs accept `requested_profile` (`small|medium|large|max`).
- Session status includes active sessions, assigned GPU IDs, requested profile, allocation/queue status, and queue reason.
- Session runtime orchestration uses a launcher adapter (`session_launcher.py`) so scheduling is decoupled from container runtime.

### Scheduler policy

- Profiles are fixed: `small=1`, `medium=2`, `large=4`, `max=8` GPUs.
- Billing scales with profile GPU count when `AXGT_GPU_WEIGHTED_BILLING` is enabled (default on with profiles).
- Allocation is **exclusive per physical GPU ID**; a GPU ID can be present in at most one active session.
- If enough free GPUs exist, scheduler assigns exact count from free GPU IDs.
- If not enough GPUs are free, request is queued with reason `insufficient free GPUs for requested profile`.

### Queue policy

- Queue is FIFO but **schedulability-aware**:
  - Earlier queued requests only block later ones if they are currently schedulable.
  - Large unschedulable requests do not block smaller requests that can run safely.
- This preserves practical throughput while maintaining fairness when requests are runnable.

### GPU exclusivity guarantee

- Active allocations are tracked as explicit GPU ID sets per session.
- Scheduler derives free GPUs as `configured_gpus - union(active_session_gpu_ids)`.
- New session can only be created from free IDs; overlapping GPU IDs are not allowed.
- GPU IDs are released when a session ends (release, timeout, container failure). Credit exhaustion pauses the session by default (`AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST=true`) so the same container/desktop can resume after top-up; paused sessions still reserve GPUs until `AXGT_SESSION_PAUSED_MAX_MINUTES` elapses.

### Launcher adapter (Mode B)

`session_manager.py` delegates user-session launch/stop to `session_launcher.py`:

- `AXGT_SESSION_LAUNCHER_MODE=docker_cli` (default): local `docker run` / `docker rm -f`.
- `AXGT_SESSION_LAUNCHER_MODE=http`: call external launcher service:
  - `POST {AXGT_SESSION_LAUNCHER_URL}/launch`
  - `POST {AXGT_SESSION_LAUNCHER_URL}/stop`
  - optional bearer auth via `AXGT_SESSION_LAUNCHER_TOKEN`.
- `AXGT_SESSION_LAUNCHER_MODE=noop`: scheduler-only dry run (no runtime spawn/stop).

This Mode B split is the recommended production path when AxonOS itself is already running in a container and should not directly control host Docker.

See also: `docs/HOST_LAUNCHER.md` for end-to-end non-nested deployment.

Compose note: `docker-compose.yml` includes a dedicated `axonos-launcher` service (HTTP launcher) so you can manage the full stack via compose while keeping Docker control isolated from the main gate container.

## Security

- Wallet ownership required (signed challenge); no unsigned verification.
- Deposit verification uses only on-chain data (tx, receipt, logs); no trust in client-reported amount.
- Replay protection: each tx hash is credited at most once.
- Server clocks should be NTP-synchronized for consistent billing timestamps.

## Tunnel tips (Gradio / HTTP proxies)

- **6080** (websockify): Full wallet + top-up flow works on one origin after signing (see `websockify_gate.py` above).
- **8889** (gate): Proxies WebSocket to 6080. Some tunnels drop or mishandle WebSocket upgrades → **1006** on connect. Try loading noVNC with **`?axgt_ws_auth=query`** or **`?axgt_ws_auth=both`** so the auth token is sent on the WebSocket URL (see `novnc-theme/ui.js`).

## Installation

1. Install dependencies: `pip3 install -r requirements.txt`
2. Set environment variables (see Configuration and `env.example` in repo root).
3. Run the gate server (e.g. `python3 gate_server.py` or via your WSGI setup).
