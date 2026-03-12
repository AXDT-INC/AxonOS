# AXGT Tokenomics Vision for AxonOS Desktop

*Note: AxonOS Desktop Tokenomics is under development and subject to progressive community feedback*

This document describes the **prepaid deposit-credit** access model for AxonOS: how users deposit **AXGT** or **native ETH** to a revenue wallet, submit a transaction hash for verification, and receive usage minutes. Sessions consume minutes via heartbeat-based incremental billing; no on-chain transfers occur during use.

## Principles

### Deposit-credit access, not hold-based

Access is gated by **prepaid credit**: users **deposit** AXGT or native ETH to a configured **revenue wallet**, then submit the **transaction hash** to the backend. The backend verifies the tx on-chain (confirmations, amount, sender/recipient) and credits usage minutes in a server-side ledger. Usage is deducted **incrementally** on each session heartbeat. When remaining minutes reach zero, access is denied until the user deposits again. This approach:

- **Reduces per-session fees**: One deposit can fund many minutes; no on-chain transfer per session or per minute.
- **Trustless verification**: Only on-chain data (tx, receipt, logs) is used to credit minutes; no trust in client-reported amounts.
- **Clear accounting**: Postgres-backed deposit ledger and audit ledger; all balance changes are logged.
- **Dual currency**: Users can fund access with AXGT or native ETH (configurable minimums and credit rates).

### No trial period

Access is strictly conditional on having **remaining_minutes > 0** from at least one verified deposit. There is no time-limited free trial; the only path to access is depositing (AXGT or ETH) to the revenue wallet and submitting the tx hash for verification.

---

## Access rules

### Minimum deposit per credit event

- **AXGT**: Each deposit must be at least **100 AXGT** (configurable via `AXGT_MIN_DEPOSIT`) to the revenue wallet. The backend verifies ERC-20 `Transfer` events to the revenue wallet and credits minutes.
- **ETH**: Each **native ETH** transfer to the revenue wallet of at least **0.01 ETH** (configurable via `ETH_MIN_DEPOSIT`) is also accepted; the backend verifies `tx.to` and `tx.value` and credits minutes at a configurable rate.

### Credit rates

- **AXGT**: Every **100 AXGT** deposited grants **60 minutes** of usage (default; configurable via `AXGT_CREDIT_PER_100_AXGT_MINUTES`).
- **ETH**: Every **1 ETH** deposited grants **60 minutes** of usage (default; configurable via `ETH_CREDIT_PER_ETH_MINUTES`).

Examples:

- 100 AXGT deposit → 60 minutes credited.
- 0.02 ETH deposit → 1.2 minutes credited (at 60 min/ETH).
- Credits are **additive**: multiple deposits increase total credited minutes; unused minutes persist until consumed.

### Usage metering (heartbeat-based)

- **Consumed minutes** are tracked per wallet in a **Postgres-backed** deposit ledger (`axgt_deposits`). An **audit ledger** (`axgt_ledger`) records every balance change (deposit_credit, usage_deduction, refund, admin_adjustment, etc.).
- **Billing is incremental**: on each session **heartbeat**, the server bills elapsed time since the last billing checkpoint and deducts it from `remaining_minutes`. Tracking is **global per wallet** across sessions and devices.
- **Replay protection**: Each transaction hash is credited at most once (`axgt_verified_deposits` table).

### Lock and warning

- When **remaining_minutes** reach **0**, the session is **terminated** and the wallet is **locked out** until the user makes another verified deposit.
- A **warning** is shown when remaining minutes fall at or below a threshold (e.g. **10 minutes**; `AXGT_WARNING_THRESHOLD_MINUTES`). The UI prompts the user to deposit again to avoid lockout.
- After lockout, the user must deposit (AXGT or ETH) to the revenue wallet and submit the new transaction hash via `POST /api/auth/verify-deposit` to receive additional minutes.

---

## Wallet ownership and security

### Sign-to-verify

- Access to the API (including verify-deposit and session claim) is contingent on **proving ownership** of the wallet. Users must sign a challenge (EIP-191 `personal_sign`) issued by the server; the server verifies the signature before issuing an auth token.
- Unsigned verification is not accepted; there is no “paste address only” path to access.

### One-time, wallet-bound challenges

- Each challenge is bound to a specific wallet and contains a **one-time nonce**. Challenges cannot be reused or replayed for another wallet.
- This prevents signature replay and ensures that only the holder of the private key for the claimed address can obtain access and submit deposit tx hashes for that wallet.

### Session tokens and verify-deposit

- On successful verification, the server issues a **short-lived auth token** (e.g. 5 minutes). WebSocket, wallet-status, and **verify-deposit** require this token (cookie or header).
- **Verify-deposit** (`POST /api/auth/verify-deposit`) requires the auth token; the `wallet_address` in the body must match the authenticated session. Only then does the server credit minutes for the submitted tx hash.
- Tokens **rotate** near expiry (with a short grace overlap) to limit exposure while avoiding unnecessary disconnects during refresh.

---

## User flow (summary)

1. User opens the AxonOS noVNC page and connects their wallet (e.g. MetaMask).
2. User signs the one-time challenge to prove ownership; server issues an auth token.
3. If the wallet has **no prepaid credit** (remaining_minutes = 0), the UI shows requirements: deposit at least X AXGT or Y ETH to the revenue wallet, then submit the transaction hash.
4. User deposits AXGT or ETH to the **revenue wallet** (any wallet or DEX). User submits the **transaction hash** via the verify-deposit API (or future UI). Server verifies the tx on-chain and credits minutes.
5. User claims a session; during the session the client sends **heartbeats**. Each heartbeat triggers incremental billing: elapsed time since last checkpoint is deducted from remaining_minutes.
6. When remaining_minutes reach **0**, the session is terminated and access is denied. User must deposit again and submit a new tx hash to regain access.

---

## Configuration (reference)

| Concept | Default | Env / config |
|--------|---------|--------------|
| Min AXGT per deposit | 100 AXGT | `AXGT_MIN_DEPOSIT` |
| Minutes per 100 AXGT deposited | 60 | `AXGT_CREDIT_PER_100_AXGT_MINUTES` |
| Min ETH per deposit | 0.01 ETH | `ETH_MIN_DEPOSIT` |
| Minutes per 1 ETH deposited | 60 | `ETH_CREDIT_PER_ETH_MINUTES` |
| Warning threshold | 10 minutes | `AXGT_WARNING_THRESHOLD_MINUTES` |
| Min block confirmations before credit | 6 | `AXGT_DEPOSIT_MIN_CONFIRMATIONS` |
| Revenue wallet | — | `AXGT_REVENUE_WALLET` (required for deposit verification) |
| Deposit/ledger DB | — | `AXGT_CHALLENGE_DB_URL` (Postgres; required for deposit-credit billing) |
| Auth token TTL | 300 s | `AXGT_AUTH_TOKEN_TTL_SECONDS` |
| Challenge TTL | 180 s | `AXGT_CHALLENGE_TTL_SECONDS` |

---

## References

- **AxonDAO**: [https://axondao.io](https://axondao.io)
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Implementation**: `axonos_gate/` — `axgt_verifier.py` (challenge/signature + deposit-credit access), `deposit_ledger.py` (Postgres deposits, ledger, billing), `deposit_verifier.py` (tx-hash verification for AXGT and ETH), `session_manager.py` (heartbeat billing), `gate_server.py` (HTTP API and WebSocket proxy). See `axonos_gate/README.md` for API, schema, and deployment details.
