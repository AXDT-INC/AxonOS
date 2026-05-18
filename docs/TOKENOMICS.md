# AXGT Tokenomics for AxonOS Desktop — ETH-first model

*Note: AxonOS Desktop tokenomics is under development and subject to progressive community feedback.*

This document describes the **ETH-first, AXGT-discount** access model for AxonOS:

- **ETH** is the only direct payment currency for compute/session credits.
- **AXGT** is no longer a mandatory payment token. Instead, holders of AXGT
  receive a **usage discount** on the ETH price, scaled by their on-chain
  AXGT balance.
- Wallet ownership is still proven by an EIP-191 signed challenge before any
  payment or session is accepted.

> "Pay with ETH, save with AXGT."

## Principles

### ETH-only payment, AXGT-as-discount

Access is gated by **prepaid credit** denominated in minutes. Users **deposit
native ETH** to a configured **revenue wallet** and submit the **transaction
hash** for verification. The backend:

1. Verifies the tx on-chain (confirmations, sender = authenticated wallet,
   recipient = revenue wallet, value).
2. Re-fetches the sender's **AXGT balance** on Ethereum mainnet via direct
   JSON-RPC `balanceOf` against the configured AXGT contract.
3. Resolves the **eligible tier** from the configured tier table.
4. Applies a **discount-adjusted minimum** (`base_min_eth × (1 − discount)`)
   and **discount-adjusted credit rate** (`credit_per_eth ÷ (1 − discount)`).
5. Credits minutes in a server-side ledger.

Direct AXGT deposits are **disabled by default**. Operators can opt back in
for legacy / migration use cases by setting `AXGT_ENABLE_AXGT_DEPOSITS=true`,
in which case the legacy minutes-per-100-AXGT path is preserved alongside
ETH.

### No trial period

Access is strictly conditional on having **remaining_minutes > 0** from at
least one verified ETH deposit. There is no time-limited free trial.

---

## Discount tier system

Default tiers (all values in **whole AXGT units**; balances are floored
before the lookup so `99.999 AXGT` is Tier 0):

| Tier   | AXGT balance              | Discount on ETH price |
| ------ | ------------------------- | --------------------- |
| Tier 0 | 0 – 99                    | 0%                    |
| Tier 1 | 100 – 999                 | 5%                    |
| Tier 2 | 1,000 – 9,999             | 10%                   |
| Tier 3 | 10,000 – 99,999           | 15%                   |
| Tier 4 | 100,000+                  | 25%                   |

Tiers are **operator-configurable** via environment variables (see
`env.example` for full reference). Three formats are supported, in this
order of precedence:

1. `AXGT_DISCOUNT_TIERS_JSON` — full JSON array of tier objects:

   ```json
   [
     {"min_axgt": 0,      "discount_percent": 0,  "label": "Tier 0"},
     {"min_axgt": 100,    "discount_percent": 5,  "label": "Tier 1"},
     {"min_axgt": 1000,   "discount_percent": 10, "label": "Tier 2"}
   ]
   ```

2. `AXGT_DISCOUNT_TIERS_FILE` — path to a JSON file with the same shape
   (or `{ "tiers": [...] }`).

3. `AXGT_DISCOUNT_TIERS` — compact form `min:percent[,min:percent...]`,
   e.g. `0:0,100:5,1000:10,10000:15,100000:25`.

If all overrides are unset or malformed, the defaults above are used.
Malformed overrides are logged at WARNING and the system falls back to
defaults rather than failing closed.

### Trust model

- The discount **must always be calculated server-side** before any payment
  is finalised. The frontend displays the quote returned by the backend
  (`GET /api/discount/quote`) but never derives a discount from a balance
  it has fetched itself.
- At credit time the backend **re-fetches** the AXGT balance and **re-resolves
  the tier** independently of any client-supplied input. The client cannot
  influence which tier a wallet ends up in.
- On RPC failure during the quote or credit step, the system **defaults
  safely to no discount**. A user with a failing RPC simply pays the full
  ETH price; payment is never blocked outright.

---

## Pricing math

Let:

- `B` = base ETH minimum deposit (`ETH_MIN_DEPOSIT`, default `0.0005`)
- `R` = base credit rate in minutes per ETH (`ETH_CREDIT_PER_ETH_MINUTES`,
  default `120000`)
- `d` = discount fraction for the resolved tier (e.g. `0.25` for Tier 4)

Then:

- **Discounted minimum payable**: `B × (1 − d)` — e.g. `0.0005 × 0.75 = 0.000375 ETH`
- **Effective credit rate**: `R ÷ (1 − d)` — e.g. `120000 ÷ 0.75 = 160000 min/ETH`
- **Minutes credited for an ETH deposit `e`**: `e × R ÷ (1 − d)`

This means a Tier 4 holder paying `0.000375 ETH` receives the same 60 minutes
that a Tier 0 holder receives for `0.0005 ETH`. Larger payments scale
linearly: a Tier 4 holder paying `0.0075 ETH` (10× the discounted min)
receives 1,200 minutes.

### Examples

| AXGT balance | Tier   | Base ETH | Final ETH    | Minutes |
| ------------ | ------ | -------- | ------------ | ------- |
| 0            | Tier 0 | 0.0005   | 0.000500     | 60      |
| 250          | Tier 1 | 0.0005   | 0.000475     | 60      |
| 5,000        | Tier 2 | 0.0005   | 0.000450     | 60      |
| 50,000       | Tier 3 | 0.0005   | 0.000425     | 60      |
| 500,000      | Tier 4 | 0.0005   | 0.000375     | 60      |

Replay protection (`axgt_verified_deposits`) and the audit ledger
(`axgt_ledger`) carry over from the previous deposit-credit model unchanged.

---

## User flow (summary)

1. User opens AxonOS noVNC and connects their EVM wallet (e.g. MetaMask).
2. User signs the one-time challenge to prove ownership; server issues an
   auth token.
3. UI shows:
   - **ETH price** (`ETH_MIN_DEPOSIT` by default)
   - **Connected wallet AXGT balance** (server-fetched via `balanceOf`)
   - **Eligible tier** + **discount percentage**
   - **Final ETH amount payable**
4. User clicks **Pay with ETH** — the wallet sends the discount-adjusted ETH
   amount to the revenue wallet. Manual flow (paste tx hash from another
   wallet/DEX) is also supported.
5. Server verifies the tx on-chain, **re-checks** the AXGT balance, applies
   the discount-adjusted credit rate, and credits minutes.
6. While connected, minutes are deducted incrementally on session
   heartbeats. When `remaining_minutes` reaches 0, the session is terminated
   and the user must top up again.

---

## API surface

### `GET /api/discount/quote`

Query parameters:

- `wallet_address` (required) — `0x…` wallet to quote.
- `base_eth` (optional) — override the ETH price; defaults to
  `ETH_MIN_DEPOSIT`.

Response (200 OK):

```json
{
  "ok": true,
  "wallet_address": "0xabc…",
  "base_eth": "0.0005",
  "final_eth": "0.000375",
  "discount_percent": 25,
  "tier_index": 4,
  "tier_label": "Tier 4",
  "tier_min_axgt": 100000,
  "axgt_balance": "120000",
  "axgt_balance_floor": 120000,
  "balance_check_ok": true,
  "balance_check_error": null,
  "tiers": [ /* full tier table */ ],
  "estimated_minutes": 60.0,
  "eth_credit_per_eth_minutes": 120000.0
}
```

`balance_check_ok=false` indicates an RPC failure; the response still
returns a quote (with `discount_percent=0`) so the UI can render and let the
user retry.

### `GET /api/config`

Now also exposes `axgt_discount_tiers` and `axgt_direct_deposits_enabled`.

### `POST /api/auth/verify-deposit`

Existing endpoint. Verifies a tx hash. The ETH path now includes a `tier`
object in the response body capturing the discount applied at credit time:

```json
{
  "verified": true,
  "deposit_currency": "ETH",
  "eth_amount": "0.000375",
  "base_eth_min": "0.0005",
  "applied_min_eth": "0.000375",
  "tier": {
    "tier_index": 4,
    "tier_label": "Tier 4",
    "tier_min_axgt": 100000,
    "discount_percent": 25.0,
    "axgt_balance_axgt": "120000",
    "balance_check_ok": true,
    "balance_check_error": null
  },
  "credited_minutes": 60.0,
  "remaining_minutes": 60.0
}
```

---

## Configuration reference

| Concept                                | Default     | Env / config                        |
| -------------------------------------- | ----------- | ----------------------------------- |
| AXGT contract (mainnet)                | —           | `AXGT_CONTRACT_ADDRESS` (use `0x6112C3509A8a787df576028450FebB3786A2274d`) |
| Mainnet RPC URL (for `balanceOf`)      | —           | `AXGT_RPC_URL`                      |
| Chain ID                               | 1           | `AXGT_CHAIN_ID`                     |
| Revenue wallet                         | —           | `AXGT_REVENUE_WALLET`               |
| Min ETH deposit                        | 0.0005 ETH  | `ETH_MIN_DEPOSIT`                   |
| Minutes per 1 ETH                      | 120000      | `ETH_CREDIT_PER_ETH_MINUTES`        |
| Discount tiers (compact form)          | 0:0,100:5,1000:10,10000:15,100000:25 | `AXGT_DISCOUNT_TIERS` |
| Discount tiers (rich JSON)             | (defaults)  | `AXGT_DISCOUNT_TIERS_JSON`          |
| Discount tiers (JSON file path)        | —           | `AXGT_DISCOUNT_TIERS_FILE`          |
| Legacy AXGT direct deposits enabled    | false       | `AXGT_ENABLE_AXGT_DEPOSITS`         |
| Legacy: minutes per 100 AXGT           | 60          | `AXGT_CREDIT_PER_100_AXGT_MINUTES`  |
| Legacy: min AXGT per direct deposit    | 100         | `AXGT_MIN_DEPOSIT`                  |
| Warning threshold                      | 10 minutes  | `AXGT_WARNING_THRESHOLD_MINUTES`    |
| Min block confirmations before credit  | 6           | `AXGT_DEPOSIT_MIN_CONFIRMATIONS`    |
| Auth token TTL                         | 300 s       | `AXGT_AUTH_TOKEN_TTL_SECONDS`       |
| Challenge TTL                          | 180 s       | `AXGT_CHALLENGE_TTL_SECONDS`        |

---

## Deployment & testing

The full app can be brought up via the bundled Docker Compose stack:

```bash
cp env.example .env
# Edit .env: set AXONOS_VNC_PASSWORD, AXGT_RPC_URL (mainnet), AXGT_CONTRACT_ADDRESS,
# AXGT_REVENUE_WALLET, optionally AXGT_DISCOUNT_TIERS for custom tiers.
docker compose build
docker compose up -d
```

Open `http://HOST:6080/vnc.html`. Verification steps:

- Wallet connection works (EIP-6963 / `window.ethereum`).
- Wallet AXGT balance is detected and printed in the **AXGT discount tier**
  card on the wallet dialog.
- Correct tier label + percentage shown.
- ETH payable amount updates from base → final after the wallet connects.
- Clicking **Pay with ETH** sends exactly the final discounted amount.
- Users with no AXGT (Tier 0) still see the card and can pay full ETH.
- After credit, `remaining_minutes` reflects discount-adjusted minutes
  (e.g. a Tier 4 holder paying `0.000375 ETH` gets 60 minutes, the same as
  a Tier 0 holder paying `0.0005 ETH`).

Operator-side checks:

- `GET /api/config` includes `axgt_discount_tiers`.
- `GET /api/discount/quote?wallet_address=0x…` returns a fresh quote.
- Logs include `balanceOf RPC` warnings on outages; the system continues
  serving traffic with `balance_check_ok=false` and no discount.

---

## References

- **AxonDAO**: [https://axondao.io](https://axondao.io)
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Implementation**:
  - `axonos_gate/discount.py` — tier config + on-chain `balanceOf` + discount math.
  - `axonos_gate/deposit_verifier.py` — ETH-first verification + discount-adjusted credit.
  - `axonos_gate/axgt_verifier.py` — challenge/signature + credit policy.
  - `axonos_gate/deposit_ledger.py` — Postgres deposit ledger + audit trail.
  - `axonos_gate/gate_server.py` / `axonos_gate/websockify_gate.py` — HTTP API
    (incl. `/api/config`, `/api/discount/quote`, `/api/auth/verify-deposit`).
  - `novnc-theme/vnc.html` — wallet dialog with discount tier panel.
  - `axonos_gate/tests/test_discount.py` — full tier + edge-case test suite.
