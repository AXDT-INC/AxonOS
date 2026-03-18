# AXGT Gate for AxonOS

This module implements **prepaid deposit-credit billing** for AxonOS remote desktop access. Users deposit **AXGT** or **native ETH** to a revenue wallet, submit the transaction hash for verification, and receive usage minutes. Sessions consume minutes via heartbeat-based incremental billing.

## Official References

- **AxonDAO website**: `https://axondao.io`
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Explorer**: `https://etherscan.io/address/0x6112C3509A8a787df576028450FebB3786A2274d`

## Overview

- **Authentication**: Wallet ownership is proven via signed challenge (`personal_sign`); one-time, wallet-bound nonces.
- **Deposit-credit**: Users deposit AXGT or native ETH to a configured **revenue wallet**. They submit the **transaction hash**; the backend verifies the tx on-chain (confirmations, contract/value, sender/recipient, amount) and credits prepaid minutes. No escrow, no oracle, no trust in client-reported amounts.
- **Billing**: Usage is deducted **incrementally** on each session heartbeat. When remaining minutes reach zero, access is denied until the user deposits again. Unused credits persist.
- **Accounting**: Postgres-backed deposit ledger and audit ledger; all balance changes are logged.

## User flow

1. Connect wallet and sign the challenge (existing flow).
2. **Pay in-page** (recommended): use **Send min AXGT (wallet)** or **Send min ETH (wallet)** — the UI submits the tx and auto-calls verify-deposit until credited. Or deposit manually and paste the tx hash.
3. Submit the transaction hash via `POST /api/auth/verify-deposit` (requires auth token) if not using in-wallet pay.
4. Backend verifies the tx and credits minutes; response includes `remaining_minutes`.
5. Claim a session; during the session the client sends heartbeats; each heartbeat bills elapsed time since last billing checkpoint.
6. When remaining minutes reach zero, the session is terminated and access is denied until the user deposits again.

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
- `ETH_MIN_DEPOSIT`: Minimum native ETH amount per deposit, in ETH (default `0.01`).
- `ETH_CREDIT_PER_ETH_MINUTES`: Usage minutes granted per 1 ETH deposited (default `60`).
- `AXGT_WARNING_THRESHOLD_MINUTES`: Warning threshold for UI (e.g. low balance).

### Optional

- `AXGT_CORS_ORIGINS`: CORS allowlist for `/api/*`. Comma-separated origins or `*`.
- `AXGT_RATE_LIMIT_PER_MIN`: Per-client rate limit for verify calls; `0` to disable.
- `AXGT_AUTH_TOKEN_TTL_SECONDS`, `AXGT_CHALLENGE_TTL_SECONDS`, `AXGT_AUTH_COOKIE_NAME`, etc.: Auth/session tuning.
- `AXGT_SESSION_MAX_MINUTES`, `AXGT_HEARTBEAT_TIMEOUT_SECONDS`: Session and heartbeat timeout.
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

Verify signed challenge and issue auth token. Returns deposit-credit status: `verified`, `access_type` (`deposit_credit`), `remaining_minutes`, `consumed_minutes`, `credited_minutes`, `auth_token_expires_in_seconds`.

### GET /api/auth/wallet-status?wallet_address=0x...

Returns current deposit-credit status (no billing).

### POST /api/auth/verify-deposit

Verify a deposit by transaction hash. **Requires auth token** (cookie or header). Body: `{"wallet_address": "0x...", "tx_hash": "0x..."}`. The `wallet_address` must match the authenticated session. On success: `verified`, `credited_minutes`, `remaining_minutes`, `confirmations`, etc.

### GET /api/config

Returns contract address, chain ID, revenue wallet, min deposit (AXGT and ETH), credit rates, warning threshold.

### Session and queue

- `GET /api/session/status`, `POST /api/session/claim`, `POST /api/session/heartbeat`, `POST /api/session/release`
- `POST /api/queue/join`, `POST /api/queue/leave`

Heartbeats trigger incremental billing; when remaining minutes reach zero the session is ended.

### Admin (when AXGT_ADMIN_SECRET is set)

- `POST /api/admin/credit-minutes`: body `wallet_address`, `minutes`, optional `notes`
- `POST /api/admin/refund-minutes`: body `wallet_address`, `minutes`, optional `notes`
- `POST /api/admin/adjust-balance`: body `wallet_address`, `minutes_delta`, optional `notes`
- `GET /api/admin/ledger?wallet_address=0x...&limit=100`: audit ledger for wallet

## Components

- `axgt_verifier.py`: Challenge/signature verification; deposit-credit access (reads from deposit ledger).
- `deposit_ledger.py`: Postgres-backed deposits, ledger, verified-deposits; billing and admin helpers.
- `deposit_verifier.py`: Tx-hash verification (RPC, Transfer events, confirmations); credits via deposit_ledger.
- `session_manager.py`: Single active session, queue, heartbeat-based billing (calls deposit_ledger.deduct_usage).
- `gate_server.py`: HTTP API and WebSocket proxy.

## Security

- Wallet ownership required (signed challenge); no unsigned verification.
- Deposit verification uses only on-chain data (tx, receipt, logs); no trust in client-reported amount.
- Replay protection: each tx hash is credited at most once.
- Server clocks should be NTP-synchronized for consistent billing timestamps.

## Installation

1. Install dependencies: `pip3 install -r requirements.txt`
2. Set environment variables (see Configuration and `env.example` in repo root).
3. Run the gate server (e.g. `python3 gate_server.py` or via your WSGI setup).
