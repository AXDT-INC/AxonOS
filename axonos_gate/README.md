# AXGT Gate for AxonOS

This module implements **prepaid deposit-credit billing** for AxonOS remote desktop access. Users pay with **ETH** (primary), **USDC** (fixed-$1 stablecoin rail), or **AXGT** (Model B, +bonus) to a revenue wallet, submit the transaction hash for verification, and receive usage minutes. Autonomous agents can pay via the **x402** HTTP-402 rail (gate pays gas) and provision headless SSH sessions. Sessions consume minutes via heartbeat-based incremental billing. See [`docs/TOKENOMICS.md`](../docs/TOKENOMICS.md) for the full payment model and [`docs/ENVIRONMENT_VARIABLES.md`](../docs/ENVIRONMENT_VARIABLES.md) for every config var.

## Official References

- **AxonDAO website**: `https://axondao.io`
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Explorer**: `https://etherscan.io/address/0x6112C3509A8a787df576028450FebB3786A2274d`

## Overview

- **Authentication**: Wallet ownership is proven via signed challenge (`personal_sign`); one-time, wallet-bound nonces.
- **Deposit-credit**: Users deposit ETH, USDC, or AXGT to a configured **revenue wallet**. They submit the **transaction hash**; the backend verifies the tx on-chain (confirmations, contract/value, sender/recipient, amount) and credits prepaid minutes — verified against the ERC-20 **Transfer event log**, not `tx.from`, so smart-account payments are safe. No escrow, no trust in client-reported amounts. (Optional dynamic USD-equivalent pricing adds a server-side price cache in Postgres fed by a Uniswap v3 TWAP + Chainlink ETH/USD, with CoinGecko as fallback/confirmation; see TOKENOMICS.)
- **x402 rail**: Agent-native HTTP-402 payments — `GET /api/x402/access` returns the requirements, `POST /api/x402/settle` broadcasts an EIP-3009 `transferWithAuthorization` and the gate pays gas from a dedicated low-balance settlement wallet.
- **Billing**: Active session usage is deducted **incrementally** on each session heartbeat. When remaining minutes reach zero, access is denied until the user deposits again. Unused credits persist.
- **Persistent Storage Billing**: When offline, users are billed for persistent storage volume usage (defaults to `0.05` minutes per GB/hour). Charges accrue to the balance, allowing it to go negative (accumulating debt). Once the balance falls below a configurable negative debt threshold (default: `-1440.0` minutes / -24 hours), the persistent volume is pruned/deleted immediately to reclaim disk space.
- **Accounting**: Postgres-backed deposit ledger and audit ledger; all balance changes are logged.
- **Holder discounts**: AXGT holders receive tiered ETH/USDC price discounts based on on-chain AXGT balance (`discount.py`, `GET /api/discount/quote`).
- **Guest / demo sessions**: Admin-issued invite tokens can mint wallet-free demo identities (`guest_mode.py`).
- **Desktop plumbing**: Browser file transfer (`file_transfer.py` + in-container `file_agent.py`), browser terminal for SSH sessions (`terminal_gateway.py` + `terminal_agent.py`), WebRTC desktop streaming (`webrtc/`, `webrtc_agent_main.py`) and GPU telemetry (`gpu_telemetry_collector.py`) are all served through the same two gate processes.

## User flow

1. Connect wallet and sign the challenge (existing flow).
2. **Pay in-page** (recommended): use **Pay with ETH**, **Pay with USDC**, or **Send AXGT** — the UI submits the tx and polls the verify endpoint until credited. Or deposit manually and paste the tx hash; pasted hashes are routed to the correct rail server-side via `POST /api/auth/verify-deposit-auto`.
3. Submit the transaction hash via `POST /api/auth/verify-deposit` / `verify-usdc-deposit` / `verify-deposit-auto` (requires auth token) if not using in-wallet pay.
4. Backend verifies the tx and credits minutes; response includes `remaining_minutes`.
5. Claim a session; during the session the in-container heartbeat daemon (and optionally the browser) sends heartbeats; each heartbeat bills elapsed time since last billing checkpoint.
6. When remaining minutes reach zero, the session enters credit grace (or ends) and access is denied. Offline storage continues to bill against the balance (accruing debt) unless the volume is deleted or balance drops below the negative debt limit threshold.

## Configuration

### Required

- `AXGT_CONTRACT_ADDRESS`: AXGT ERC-20 contract address.
- `AXGT_CHAIN_ID`: Ethereum chain ID (exposed to the UI via `/api/config`).
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
- `AXGT_WARNING_THRESHOLD_MINUTES`: Warning threshold for UI (e.g. low balance; default `10`).

### Persistent storage

- `AXGT_PERSISTENT_STORAGE_ENABLED`: Enable user persistent named volumes (default `true`).
- `AXGT_PERSISTENT_STORAGE_DIR`: Directory on Docker host for backing `.ext4` sparse image files (default `/var/lib/docker/axonos_storage`).
- `AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX`: Docker volume prefix (default `axgt-user-storage-`).
- `AXGT_PERSISTENT_STORAGE_MOUNT_PATH`: Mount path inside desktop sessions (default `/home/aXonian`).
- `AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS`: Billing sweep interval (default `3600` / 1 hour).
- `AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES`: Max negative credit debt allowed before volume pruning (default `-1440.0`).
- `AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES`: Offline storage charge rate per GB/hour (default `0.05` compute minutes).
- **Storage Slider & Dynamic Resizing**: Users configure requested capacity (10 GB – 500 GB, default 100 GB) in the launch wizard. The backend automatically initializes sparse `.ext4` loop images and expands existing volumes online (`truncate` + `losetup -c` + `resize2fs`). Billing is based on actual data stored (`du -s`), not virtual capacity.

### USDC / x402 (stablecoin + agent rail)

- `AXGT_ENABLE_USDC_DEPOSITS`: Enable the USDC rail + UI (default `true`).
- `USDC_RPC_URL`, `USDC_CONTRACT_ADDRESS`, `USDC_CHAIN_ID` (default `8453`), `USDC_NETWORK` (default `base`): USDC chain config — independent of `AXGT_CHAIN_ID` (Base by default).
- `USDC_MIN_DEPOSIT` (default `1`), `USDC_CREDIT_PER_USDC_MINUTES` (default `60`), `USDC_DEPOSIT_MIN_CONFIRMATIONS` (default `6`).
- `AXGT_ENABLE_X402_SETTLEMENT` (default `true`) and `X402_SETTLEMENT_PRIVATE_KEY`: dedicated low-balance signer that pays gas for `POST /api/x402/settle`. Unset = settle disabled (tx-hash rail still works). **Never the revenue/treasury key.**
- `USDC_EIP712_NAME` (default `USD Coin`) / `USDC_EIP712_VERSION` (default `2`): must match the token's on-chain EIP-712 domain.

### Settlement rail: self-settle (default) vs CDP facilitator (Bazaar listing)

The x402 rail has two interchangeable settlement backends. **Self-settle is the default and unchanged**; the facilitator rail is purely additive (opt-in).

- **Self-settle (default).** The gate broadcasts the EIP-3009 `transferWithAuthorization` itself (`X402_SETTLEMENT_PRIVATE_KEY`) and pays gas. No facilitator, no third party. This rail is **private** — it does **not** appear in the [Coinbase x402 Bazaar](https://docs.cdp.coinbase.com/x402/bazaar), because CDP only indexes resources it has settled at least once.
- **CDP facilitator (opt-in, Bazaar-listable).** Set `AXGT_X402_FACILITATOR_ENABLED=true` to route `verify`+`settle` through CDP (`CDP_FACILITATOR_URL`, default `https://api.cdp.coinbase.com/platform/v2/x402`). CDP broadcasts the tx and pays gas, and **catalogs the resource on first settlement**, making AxonOS discoverable in the Bazaar. The gate still runs its own EIP-3009 checks first (recipient/sender/amount/signature) before handing off, and the settled tx is re-verified on-chain through the same ledger path — so credit, replay protection, and accounting are identical to self-settle.

Config (facilitator mode only):

- `AXGT_X402_FACILITATOR_ENABLED` (default `false`): master switch. Off ⇒ self-settle.
- `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`: CDP API key (from `https://portal.cdp.coinbase.com`). Auth is a short-lived Bearer JWT minted per request via `cdp-sdk` if installed, else the `PyJWT`+`cryptography` fallback.
- `CDP_FACILITATOR_URL`: facilitator base URL (default CDP platform v2).
- `AXGT_X402_BAZAAR_DISCOVERABLE` (default `true` when facilitator on), `AXGT_X402_BAZAAR_CATEGORY` (default `compute`), `AXGT_X402_BAZAAR_TAGS` (default `gpu,compute,ssh,linux`): listing metadata advertised in the 402 `accepts[].extensions.bazaar` discovery declaration.

The two rails are mutually exclusive at runtime (the flag picks one), so you can flip a deployment between private and Bazaar-listed without code changes. Implemented in [`x402_facilitator.py`](x402_facilitator.py); self-settle remains in `x402_verifier.py`.

### Dynamic USD-equivalent pricing (price oracle)

- `AXGT_DYNAMIC_PRICING` (default `false`): credit ETH/AXGT at live USD value; USDC stays fixed at $1.
- AXGT uses a 30-minute Uniswap v3 TWAP (× Chainlink ETH/USD) first; when pool history is unavailable, the dashboard-compatible spot quote requires CoinGecko agreement before billing accepts it. Prices are refreshed on a poll interval and cached in Postgres so both gate processes share them.
- `AXGT_USD_PER_HOUR` (default `1.0`), `AXGT_USD_BONUS_PERCENT` (default `25`, AXGT Model-B bonus), `ETHEREUM_RPC_URL` (default `https://ethereum-rpc.publicnode.com`, falls back to `AXGT_RPC_URL`), `AXGT_UNISWAP_V3_POOL`, `AXGT_TWAP_WINDOW_SECONDS` (default `1800`), `AXGT_MAX_PRICE_DEVIATION_PERCENT` (default `25`), `AXGT_COINGECKO_ID` (default `axondao-governance-token-2`; fallback/confirmation), `PRICE_POLL_INTERVAL_SECONDS` (default `300`), `PRICE_MAX_STALE_SECONDS` (default `86400`).

### Optional

- `AXGT_CORS_ORIGINS`: CORS allowlist for `/api/*`. Comma-separated origins or `*`.
- `AXGT_RATE_LIMIT_PER_MIN`: Per-client rate limit for verify calls (default `60`); `0` to disable.
- `AXGT_AUTH_TOKEN_TTL_SECONDS` (default `300`), `AXGT_CHALLENGE_TTL_SECONDS`, `AXGT_AUTH_COOKIE_NAME` (default `axgt_auth_token`), etc.: Auth/session tuning.
- `AXGT_SESSION_MAX_MINUTES` (default `60`), `AXGT_HEARTBEAT_TIMEOUT_SECONDS` (default `120`): Sliding runtime lease and stale-heartbeat timeout.
- `AXGT_ADMIN_SECRET`: If set, enables admin API (`/api/admin/*`) when request includes header `X-AXGT-Admin-Secret` or query `admin_secret`.

## Database schema (Postgres)

Created automatically when `AXGT_CHALLENGE_DB_URL` is set.

### axgt_deposits

- `wallet_address` (TEXT, PK)
- `deposited_amount_axgt`, `credited_minutes_total`, `consumed_minutes_total`, `remaining_minutes`
- `last_billed_at`, `created_at`, `updated_at`

### axgt_ledger (audit)

- `id`, `wallet_address`, `event_type`, `minutes_delta`, `axgt_delta`, `balance_after_minutes`
- `reference_tx_hash`, `reference_session_id`, `notes`, `created_at`, `created_by`

Event types: `deposit_credit`, `test_credit`, `guest_credit`, `usage_deduction`, `refund`, `admin_adjustment`, `session_expiry`, `verification_reject`.

### axgt_verified_deposits (replay protection)

- `tx_hash` (PK), `wallet_address`, `sender_wallet`, `recipient_wallet`, `axgt_amount`, `credited_minutes`, `block_number`, `credit_source` (default `onchain`; test-credit provenance otherwise), `payment_rail` (default `unknown`), `created_at`

### Session table

- `id`, `wallet_address`, `requested_profile`, `gpu_ids`, `container_id`, `allocation_status`, `started_at`, `last_heartbeat`, `last_billed_at`, `expires_at` (sliding), `status`, `files_key`, `ssh_enabled`, `credit_grace_started_at`
- Added by in-place migration: `webrtc_cap_jti_hash`, `webrtc_cap_expires_at`, `hard_expires_at` (non-sliding cap for headless/SSH sessions), `requested_storage_gb` (default `100`)

## API Endpoints

Both gate processes serve the `/api/*` routes below unless noted: `websockify_gate.py` on **6080** and `gate_server.py` on **8889**. Admin routes are served **only** by `gate_server.py` (8889).

### GET /api/auth/challenge?wallet_address=0x...

Returns a one-time challenge bound to the wallet.

### POST /api/auth/verify-wallet

Verify signed challenge and issue **auth token** (cookie + body) whenever the signature is valid—even if `verified` is false because there is no prepaid credit yet. Response includes `remaining_minutes`, `credited_minutes`, `auth_token`, etc. The UI needs the token to call **verify-deposit** and complete top-up on the **same origin** (e.g. tunnel to **6080**).

### GET /api/auth/wallet-status?wallet_address=0x...

Returns current deposit-credit status (no billing).

### POST /api/auth/verify-deposit

Verify a deposit by transaction hash. **Requires auth token** (cookie or header). Body: `{"wallet_address": "0x...", "tx_hash": "0x..."}`. The `wallet_address` must match the authenticated session. On success: `verified`, `credited_minutes`, `remaining_minutes`, `confirmations`, etc. While the tx is indexing or confirming, returns **HTTP 200** with `verified: false`, `pending: true`, `confirmations`, `required`, and `error` (client should poll). Hard failures (wrong sender, duplicate tx, below minimum) return **HTTP 400**.

### POST /api/auth/verify-usdc-deposit

Verify a USDC transfer by tx hash (same shape as verify-deposit). Credits at the fixed USDC rate.

### POST /api/auth/verify-deposit-auto

Verify a pasted tx hash against **both** rails (ETH/AXGT and USDC) server-side and return a single result with correct precedence (`deposit_router.py`). This is what the UI uses for manually pasted hashes.

### POST /api/auth/test-credit

Issue bounded, auditable test credit to an authenticated eligible wallet (ledger event `test_credit`). Disabled unless the operator configures eligibility.

### Guest / demo access

- `POST /api/auth/guest` — redeem an admin-issued invite token for a wallet-free demo identity (`guest_mode.py`).
- `POST /api/auth/guest-invite` — mint an invite from an authenticated wallet that is allowed to mint (`guest_invite_minter` in the verify-wallet response).

### x402 rail

- `GET /api/x402/access` — returns **HTTP 402** with payment requirements (v1 body with absolute `resource` URL + v2 `PAYMENT-REQUIRED` header, so both JS `x402-fetch` and Python `x402` SDKs work).
- `POST /api/x402/settle` — settles an EIP-3009 `transferWithAuthorization`; the gate broadcasts and pays gas, then credits minutes.
- `POST /api/x402/session` — pay-and-provision a headless SSH session in one call (agent flow).
- `GET /.well-known/x402` — discovery document.

### GET /api/config

Returns contract address, chain ID, revenue wallet, min deposit (AXGT and ETH), credit rates, warning threshold, GPU profiles, storage settings and discount tiers. Also exposes USDC contract/chain/network and dynamic-pricing flags when configured.

### GET /api/discount/quote?wallet_address=0x...&currency=eth|axgt|usdc

Returns the AXGT-holder discount tier, base/final amount and `estimated_minutes` for a wallet based on its on-chain AXGT balance. Never hardcode price→minutes in clients; use this quote.

### Session

- `GET /api/session/status`, `POST /api/session/claim`, `POST /api/session/heartbeat`, `POST /api/session/release`, `POST /api/session/restart`

Heartbeats trigger incremental billing; when remaining minutes reach zero the session enters credit grace or is ended.
When `AXGT_GPU_PROFILES_ENABLED` and weighted billing are on (default), usage deducts
`wall_clock_minutes × assigned_gpu_count` from prepaid credit (e.g. Max = 8× burn rate).
There is no queue: a claim that cannot be scheduled (no free GPUs for the requested profile) is **denied immediately** with an explanatory error, and the client may retry later.

### Other route families

- **Files**: `GET /api/public/files-config` (optional direct file plane base URL), `/api/files/*` — wallet-authenticated browser ↔ desktop file transfer streamed to the in-container `file_agent.py` (`file_transfer.py`).
- **Terminal**: `POST /api/terminal/ticket` mints a one-use ticket for an exact active SSH session; `/api/terminal/ws` (8889) upgrades to the browser terminal WebSocket (`terminal_gateway.py`, `terminal_agent.py`).
- **WebRTC**: `/api/webrtc/{config,offer,ice,status,close,metrics,session}` for the browser; `/api/webrtc/agent/{next,answer,fail,refresh,row}` for in-container agents, served **only** by the internal agent gate instance on **8890** (see Components). See `docs/WEBRTC.md`.
- **Public telemetry** (6080, no auth): `/api/public/telemetry/{summary,sessions,wallets,events,webrtc,live}`.

### Admin (when AXGT_ADMIN_SECRET is set; 8889 only)

- `POST /api/admin/credit-minutes`: body `wallet_address`, `minutes`, optional `notes`
- `POST /api/admin/refund-minutes`: body `wallet_address`, `minutes`, optional `notes`
- `POST /api/admin/adjust-balance`: body `wallet_address`, `minutes_delta`, optional `notes`
- `GET /api/admin/ledger?wallet_address=0x...&limit=100`: audit ledger for wallet
- `POST /api/admin/guest-invite`, `POST /api/admin/guest-invite/revoke`, `GET /api/admin/guest-invites`: demo invite management
- `GET /api/admin/telemetry/{summary,sessions,wallets,events,webrtc}`: operator telemetry

## Components

- `axgt_verifier.py`: Challenge/signature verification; deposit-credit access (reads from deposit ledger); test-credit policy.
- `deposit_ledger.py`: Postgres-backed deposits, ledger, verified-deposits; billing and admin helpers.
- `deposit_verifier.py`: Tx-hash verification (RPC, Transfer events, confirmations); credits via deposit_ledger.
- `deposit_router.py`: Rail auto-detection for pasted tx hashes (`verify-deposit-auto`).
- `x402_verifier.py`: USDC/x402 rail — 402 payment requirements (v1+v2), EIP-3009 self-settlement, tx-hash USDC verification.
- `x402_facilitator.py`: Opt-in CDP facilitator settlement (Bazaar listing) — CDP JWT auth + `verify`/`settle` + discovery extension. Inactive unless `AXGT_X402_FACILITATOR_ENABLED=true`.
- `agentlink_verifier.py`: Agent-link payment verification helper.
- `price_oracle.py`: USD price oracle (Uniswap TWAP + Chainlink, CoinGecko fallback), Postgres-cached.
- `discount.py`: AXGT-holder discount tiers and on-chain balance lookup.
- `guest_mode.py`: Invite-gated, wallet-free demo identities.
- `session_manager.py`: Session scheduler + heartbeat-based billing (calls deposit_ledger.deduct_usage). Supports single-session (shared desktop) mode and feature-gated multi-session mode with exclusive GPU allocation.
- `session_launcher.py`: Adapter client for session runtime orchestration (`docker_cli` / `http` / `noop`).
- `session_launcher_service.py`: Host-side launcher API for non-nested deployments (`POST /launch`, `POST /stop`, `GET /healthz`, `GET /enumerate-gpus`, `GET /list-containers`); also runs the persistent-storage billing/prune sweep.
- `session_heartbeat_daemon.py`: In-container heartbeat daemon; the durable source of compute liveness independent of any browser tab.
- `docker_gpu_cli.py`: Helpers for nested `docker run` with NVIDIA device requests.
- `file_agent.py` / `file_transfer.py`: In-container file API and gate-side streaming proxy for browser file transfer.
- `terminal_agent.py` / `terminal_gateway.py`: Per-session PTY broker and the ticketed browser-terminal WebSocket gateway.
- `gpu_telemetry_collector.py`: Supervisor-managed NVML sampler publishing a JSON snapshot for all gate workers.
- `webrtc/` (`capability.py`, `capture.py`, `config.py`, `metrics.py`, `service.py`, `store.py`, `x11_input.py`) and `webrtc_agent_main.py`: WebRTC signaling and the in-container streaming agent.
- `security_utils.py`: CORS parsing, rate limiter, log redaction helpers.
- `gate_server.py`: Flask HTTP API and WebSocket proxy (port **8889** by default, `GATE_PORT`). When `WEBRTC_ENABLED=true`, supervisord starts a **second** instance on internal port **8890** (`GATE_AGENT_ONLY=true`) that serves only the `/api/webrtc/agent/*` routes for in-container agents; agent routes are kept off 8889/6080 and public routes off 8890.
- `websockify_gate.py`: noVNC + WebSocket on **6080** (`WEBSOCKIFY_PORT`); serves `/api/config`, `/api/auth/*`, `/api/session/*`, `/api/x402/*`, `/.well-known/x402`, `/api/discount/quote`, `/api/files/*`, `/api/public/*`, `/api/terminal/ticket` and browser `/api/webrtc/*` so a tunnel to 6080 alone can sign in, top up and run a session without hitting 8889. Does **not** serve `/api/admin/*`.
- `server.py`, `websockify_wrapper.py`: Legacy entry points (simple gate server / WebSocket wrapper).
- `scripts/mock_verify_harness.py`: Offline harness for the x402/facilitator verify path.
- `tests/`: unittest suite covering billing, verifiers, session manager, launcher, WebRTC, terminal, file agent and frontend contracts.

## Concurrency Architecture

Feature gating:

- `AXGT_USER_CONTAINER_ENABLED=true` (default `false`) — **required** for multi-session mode; each claim gets its own runtime container. Without it the deployment is treated as single-session (shared desktop) regardless of the other flags.
- `AXGT_MULTI_SESSION_ENABLED` (default `true`) — set `false` to force single-session even with user containers.
- `AXGT_GPU_PROFILES_ENABLED` (default `true`) — enables GPU profiles and weighted billing.

### What changed

- Session records include `requested_profile`, `gpu_ids`, `container_id`, `allocation_status`, `ssh_enabled`, `hard_expires_at`, `credit_grace_started_at` and `requested_storage_gb` (see Session table above).
- Claim API accepts `requested_profile` (`small|medium|large|max`), `requested_template`, `requested_storage_gb` and SSH opt-in.
- Session status includes active sessions, assigned GPU IDs, requested profile, allocation status and credit-grace/resume information.
- Session runtime orchestration uses a launcher adapter (`session_launcher.py`) so scheduling is decoupled from container runtime.

### Scheduler policy

- Profiles are fixed: `small=1`, `medium=2`, `large=4`, `max=8` GPUs. `AXGT_DEFAULT_GPU_PROFILE` (default `small`) applies when the client omits one; a paused/owned session's profile always wins over a new UI pick.
- Billing scales with profile GPU count when `AXGT_GPU_WEIGHTED_BILLING` is enabled (default on with profiles).
- Allocation is **exclusive per physical GPU ID**; a GPU ID can be present in at most one active session.
- If enough free GPUs exist, scheduler assigns exact count from free GPU IDs.
- If not enough GPUs are free, the claim is **denied immediately** with an explanatory reason (there is no server-side queue); the client retries later.

### GPU exclusivity guarantee

- Active allocations are tracked as explicit GPU ID sets per session.
- Scheduler derives free GPUs as `configured_gpus - union(active_session_gpu_ids)`.
- New session can only be created from free IDs; overlapping GPU IDs are not allowed.
- GPU IDs are released when a session ends (release, timeout, container failure). With `AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST=true` (default), zero credit instead starts a logical top-up grace (120 minutes by default): viewer access and compute billing stop, while the same running container, jobs, desktop, and GPU assignment are retained. Top-up restores access to that allocation; if no top-up arrives before `AXGT_SESSION_CREDIT_GRACE_MINUTES` elapses, cleanup stops the container and releases its GPUs. The old `AXGT_SESSION_PAUSED_MAX_MINUTES` name remains a fallback during migration. Demo (guest) sessions are never preserved.
- Detach only disconnects the viewer. The session container keeps sending its authenticated heartbeat, so jobs continue and usage remains billed across reload/tab close until the owner ends the session or the balance reaches zero. A stale heartbeat means the runtime is unhealthy, not that a browser detached.

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

Compose note: `docker-compose.yml` includes a dedicated `axonos-launcher` service (HTTP launcher, default bind port `8090`) so you can manage the full stack via compose while keeping Docker control isolated from the main gate container.

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
3. Run **both** gate processes: `python3 websockify_gate.py` (noVNC + API on 6080, `WEBSOCKIFY_PORT`) and `python3 gate_server.py` (API + WebSocket proxy on 8889, `GATE_PORT`). In the container image these, plus the optional 8890 agent gate, `gpu_telemetry_collector.py` and `session_heartbeat_daemon.py`, are started by supervisord (`supervisord.conf` at repo root).
