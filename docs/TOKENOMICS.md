# AXGT Tokenomics Vision for AxonOS Desktop

*Note: AxonOS Desktop Tokenomics is under development and subject to progressive community feedback*

This document describes the token-based access model for AxonOS: how AXGT is used to gate and meter desktop access without debiting balances on-chain.

## Principles

### Hold-based access, not spend-based

Access is gated by **holding** a minimum amount of AXGT in an Ethereum wallet. Users do **not** send or transfer AXGT to use the platform. The balance is read on-chain (ERC-20 `balanceOf`); usage is metered **off-chain** in a server-side ledger. This approach:

- **Reduces fees**: No on-chain transfers for every session or minute.
- **Aligns incentives**: Holding more AXGT grants more capacity; long-term holders get more utility.
- **Preserves custody**: Users keep their tokens in their own wallet at all times.

### No trial period

Access is strictly conditional on holding the required AXGT threshold. There is no time-limited free trial; the only path to access is holding and proving ownership of AXGT.

---

## Access rules

### Minimum hold

- A wallet must hold at least **100 AXGT** (configurable via `AXGT_MIN_HOLD_AMOUNT`) to be eligible for access.
- The balance is checked on Ethereum mainnet against the official AXGT contract.

### Linear capacity (credit)

- Capacity is **linear in held AXGT**: every **100 AXGT** held grants **60 minutes** of total usable credit (default; configurable via `AXGT_CREDIT_PER_100_AXGT_MINUTES`).
- Examples:
  - 100 AXGT → 60 minutes total.
  - 200 AXGT → 120 minutes total.
  - 500 AXGT → 300 minutes total.
- Capacity is a **ceiling** on usage; it does not refill over time. Once a wallet has consumed minutes up to that ceiling, access locks until the user holds more AXGT (increasing the ceiling).

### Usage metering (off-chain)

- **Consumed minutes** are tracked per wallet in a persistent, off-chain usage ledger (e.g. a JSON file or future DB).
- Tracking is **global per wallet**: the same address shares one usage total across all sessions and devices.
- Elapsed time while connected is attributed to that wallet; the ledger is updated when the client polls wallet-status or when connections are gated.
- The ledger is **not** a blockchain; it is an operational store that can be backed up, audited, or migrated.

### Lock and warning

- When **remaining minutes** (capacity minus consumed) reach **0**, the wallet is **locked out**: no new connections until the user holds more AXGT (raising capacity).
- A **warning** is shown when remaining minutes fall at or below a threshold (e.g. **10 minutes**). The in-session overlay prompts the user to add more AXGT to avoid lockout.
- After lockout, the UI directs the user to verify again (e.g. after acquiring more AXGT); no on-chain “refill” transaction is required—the system re-reads the updated balance.

---

## Wallet ownership and security

### Sign-to-verify

- Access is contingent on **proving ownership** of the wallet. Users must sign a challenge (EIP-191 `personal_sign`) issued by the server; the server verifies the signature before granting a session.
- Unsigned verification is not accepted; there is no “paste address only” path to access.

### One-time, wallet-bound challenges

- Each challenge is bound to a specific wallet and contains a **one-time nonce**. Challenges cannot be reused or replayed for another wallet.
- This prevents signature replay and ensures that only the holder of the private key for the claimed address can obtain access.

### Session tokens

- On successful verification, the server issues a **short-lived auth token** (e.g. 5 minutes). WebSocket and API access (e.g. wallet-status) require this token.
- Tokens are carried via **HttpOnly cookie** by default; an optional query-parameter fallback exists for environments where cookies are not forwarded on WebSocket upgrades (e.g. some tunnels).
- Tokens **rotate** near expiry (with a short grace overlap) to limit exposure while avoiding unnecessary disconnects during refresh.

---

## User flow (summary)

1. User opens the AxonOS noVNC page and connects their wallet (e.g. MetaMask).
2. User signs the one-time challenge to prove ownership.
3. Server checks on-chain balance and off-chain usage; if the wallet holds ≥ minimum AXGT and has remaining capacity, the server issues an auth token and returns status (remaining minutes, etc.).
4. User is connected to the desktop (WebSocket gated by token + wallet).
5. During the session, the client polls wallet-status; the server updates consumed minutes and returns remaining time. If remaining ≤ warning threshold, the UI shows a warning overlay; if remaining = 0, the session is locked and the user is prompted to add more AXGT and verify again.
6. To regain access after lockout, the user adds AXGT (or already holds more), then verifies again; the system re-reads balance and computes a new capacity.

---

## Configuration (reference)

| Concept | Default | Env / config |
|--------|---------|--------------|
| Minimum hold | 100 AXGT | `AXGT_MIN_HOLD_AMOUNT` |
| Minutes per 100 AXGT | 60 | `AXGT_CREDIT_PER_100_AXGT_MINUTES` |
| Warning threshold | 10 minutes | `AXGT_WARNING_THRESHOLD_MINUTES` |
| Usage ledger path | `/var/lib/axonos_gate/usage.json` | `AXGT_USAGE_DB_PATH` |
| Auth token TTL | 300 s | `AXGT_AUTH_TOKEN_TTL_SECONDS` |
| Challenge TTL | 180 s | `AXGT_CHALLENGE_TTL_SECONDS` |

---

## References

- **AxonDAO**: [https://axondao.io](https://axondao.io)
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Implementation**: `axonos_gate/` (verifier, websockify gate, gate server); see `axonos_gate/README.md` for API and deployment details.
