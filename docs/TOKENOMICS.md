# AXGT Tokenomics for AxonOS Desktop — ETH-first model

*Note: AxonOS Desktop tokenomics is under development and subject to progressive community feedback.*

This document describes the **ETH-first, AXGT-discount** access model for AxonOS:

- **ETH** is the primary direct payment currency for compute/session credits.
- **AXGT** is no longer a mandatory payment token. Instead, holders of AXGT
  receive a **usage discount** on the ETH price, scaled by their on-chain
  AXGT balance.
- Wallet ownership is still proven by an EIP-191 signed challenge before any
  payment or session is accepted.

> "Pay with ETH, save with AXGT."

Three additional rails sit alongside ETH and feed the **same prepaid-minutes
ledger** (see [Additional payment rails](#additional-payment-rails)):

- **USDC** — a fixed-$1 stablecoin rail (tx-hash verified, no holder tier).
- **x402** — an agent-native HTTP-402 rail for off-the-shelf x402 clients.
- **AXGT (Model B)** — paying *in* AXGT, credited at live USD value with a flat
  bonus and no holder tier (the "best deal").

When **dynamic USD-equivalent pricing** is enabled, ETH and AXGT credits are
valued at their live USD price; USDC always stays fixed at $1.

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

## Additional payment rails

All rails below credit the **same prepaid-minutes ledger** and are subject to the
same wallet-ownership challenge and server-side verification (no client-reported
amounts). The ETH discount tiers above apply to **ETH and USDC only** — paying in
AXGT uses the Model-B bonus instead.

### USDC (stablecoin rail)

USDC is a fixed **$1** rail, self-verified on-chain with no facilitator. USDC lands
in the same `AXGT_REVENUE_WALLET` but **on the USDC chain (Base by default)**, which
is independent of `AXGT_CHAIN_ID` — so the stablecoin rail can run on Base while the
ETH/AXGT rail runs on Ethereum mainnet. Two ways to pay:

1. **tx-hash rail** — the user sends a USDC transfer and submits the hash to
   `POST /api/auth/verify-usdc-deposit`. Drives the in-page "Pay with USDC" button,
   which appears only when `USDC_CONTRACT_ADDRESS` is configured.
2. **x402 protocol** (below).

Defaults: 1 USDC → 60 minutes (`USDC_CREDIT_PER_USDC_MINUTES`), minimum 1 USDC,
6 confirmations. Holder discount tiers do **not** apply to USDC.

### x402 (agent-native HTTP-402)

For autonomous agents using an off-the-shelf x402 client. `GET /api/x402/access`
returns **HTTP 402** with payment requirements; the client signs an EIP-3009
`transferWithAuthorization`; `POST /api/x402/settle` broadcasts it and **the gate
pays the gas** from a dedicated low-balance settlement wallet
(`X402_SETTLEMENT_PRIVATE_KEY`). The verified payment credits minutes exactly like
the USDC tx-hash rail. The agent can then provision a headless SSH session for
fully unattended compute.

> SDK interop: the gate serves both the v1 402 **body** (absolute `resource` URL,
> for JS `x402-fetch`) and the v2 `PAYMENT-REQUIRED` **header** (for the Python
> `x402` SDK). See [`docs/X402_AGENT_TEST.md`](X402_AGENT_TEST.md).

### AXGT-as-payment (Model B)

Paying directly **in AXGT** (`AXGT_ENABLE_AXGT_DEPOSITS=true`) is credited at the
live USD value of the deposit plus a flat **`AXGT_USD_BONUS_PERCENT`** bonus
(default +25%) and carries **no holder tier** — it is intentionally the best deal.
When dynamic pricing is off (or the price feed is stale) it falls back to the fixed
`AXGT_CREDIT_PER_100_AXGT_MINUTES` rate.

---

## Dynamic USD-equivalent pricing (price oracle)

Off by default (`AXGT_DYNAMIC_PRICING`). When enabled, ETH and AXGT deposits are
credited at their **live USD value** rather than the fixed crypto rates, so a fixed
USD price of compute holds as token prices move. Mechanics:

- Minutes are priced at **`AXGT_USD_PER_HOUR`** (default $1/hour = 60 min/$).
- Token→USD quotes come from the **CoinGecko free API** (`AXGT_COINGECKO_ID` for
  AXGT), polled ~8×/day (`PRICE_POLL_INTERVAL_SECONDS`) and cached **server-side**
  in Postgres. Client-reported prices are never trusted.
- On a feed outage the last-known price is used up to `PRICE_MAX_STALE_SECONDS`
  (default 24 h); beyond that the system falls back to the fixed crypto rates above.
- **USDC stays fixed at $1** regardless of this setting.

This is a deliberate departure from the prior no-oracle stance; the oracle is
read-only server-side cache and never blocks payment.

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

### GPU-weighted session billing

When multi-GPU profiles are enabled (`AXGT_GPU_PROFILES_ENABLED`), usage billing
multiplies wall-clock time by the number of GPUs in the active session:

| Profile | GPUs | Billed per 1 wall-clock minute |
|---------|------|-------------------------------|
| Small   | 1    | 1 prepaid minute              |
| Medium  | 2    | 2 prepaid minutes             |
| Large   | 4    | 4 prepaid minutes             |
| Max     | 8    | 8 prepaid minutes             |

Deposits still credit **prepaid minutes** at the ETH/AXGT rates above; larger
profiles consume that balance faster. Claim/queue requires at least as many
prepaid minutes as GPUs in the selected profile (so one billing tick can run).

Disable with `AXGT_GPU_WEIGHTED_BILLING=false` (not recommended for production).

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

Existing endpoint. Verifies a tx hash.

**Pending (poll): HTTP 200**

```json
{
  "verified": false,
  "pending": true,
  "confirmations": 2,
  "required": 6,
  "error": "Insufficient confirmations (have 2, need 6)",
  "wallet_address": "0x…",
  "tx_hash": "0x…"
}
```

Also used while the tx is not yet indexed or not yet included in a block.
**Hard errors** (wrong wallet, duplicate credit, below minimum) return **HTTP 400**.

The ETH path on success includes a `tier` object in the response body capturing
the discount applied at credit time:

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

### Additional rail endpoints

- `POST /api/auth/verify-usdc-deposit` — verify a USDC transfer by tx hash (USDC rail).
- `GET /api/x402/access` — returns HTTP 402 with payment requirements (v1 body + v2 header).
- `POST /api/x402/settle` — settles an EIP-3009 `transferWithAuthorization` (gate pays gas).
- `GET /.well-known/x402` — x402 discovery document.
- `GET /api/config` additionally exposes the USDC contract/chain and dynamic-pricing flags when configured.

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
| USDC rail enabled                      | true        | `AXGT_ENABLE_USDC_DEPOSITS`         |
| USDC contract / chain                  | — / 8453    | `USDC_CONTRACT_ADDRESS` / `USDC_CHAIN_ID` |
| Minutes per 1 USDC                     | 60          | `USDC_CREDIT_PER_USDC_MINUTES`      |
| x402 settlement enabled                | true        | `AXGT_ENABLE_X402_SETTLEMENT`       |
| x402 settlement signer (pays gas)      | —           | `X402_SETTLEMENT_PRIVATE_KEY`       |
| AXGT pay-in bonus (Model B)            | +25%        | `AXGT_USD_BONUS_PERCENT`            |
| Dynamic USD pricing                    | false       | `AXGT_DYNAMIC_PRICING`              |
| USD price per hour                     | $1.00       | `AXGT_USD_PER_HOUR`                 |

> Full variable reference: [`docs/ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md).

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
