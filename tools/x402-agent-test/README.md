# AxonOS x402 agent test harness

Drives a **generic x402 agent** against a **testnet** AxonOS gate and walks the
full agentic loop:

```
pay (x402 / EIP-3009)  ->  claim SSH session  ->  run commands  ->  heartbeat  ->  release
```

Two interchangeable agents are provided, one per official Coinbase SDK:

| Script | SDK | What it parses from the 402 |
|---|---|---|
| `agent.mjs` | JS `x402-fetch` + `viem` | the **v1 body** (`x402Version: 1`, `maxAmountRequired`, network name) |
| `agent.py` | Python `x402` (2.13.x) | the **v2 `PAYMENT-REQUIRED` header** (CAIP-2 network, `amount`, resource object) |

The gate always emits both (v1 body + v2 header), so each stock SDK finds the
shape it expects. The harness confirms that either agent can parse the 402,
settle USDC gaslessly, and get a usable SSH endpoint back in one call.

---

## ⚠️ Safety

- **Testnet only.** Never point `AXONOS_BASE_URL` at the mainnet gate.
- **Do not run the stack on the production host.** Its compose pins `container_name: axonos`
  / `axonos_postgres` and `env_file: .env`, so a second `docker compose up` there collides
  with prod, and `cp .env.testnet .env` would overwrite the live mainnet config.
- **Restarting the prod container flips it to mainnet** (it reloads the now-mainnet `.env`).
  Keep this test on a separate box.

## Prerequisites

1. **Node 18+** (built-in `fetch`) for `agent.mjs`, or **Python 3.10+** with
   `pip install 'x402[evm]' requests` for `agent.py`, plus an `ssh` client on the
   machine running the harness.
2. **A test box running the testnet stack.** For the **SSH-session** step it must have an
   **NVIDIA GPU** + Container Toolkit (the launcher always runs `docker run --gpus`). The
   payment / 402 / settle steps need no GPU.
3. **Two funded Base Sepolia wallets** (faucets, $0):
   | Wallet | Needs | Why |
   |---|---|---|
   | **Agent wallet** (`AGENT_PRIVATE_KEY`) | Base Sepolia **test USDC** (faucet.circle.com) | the asset the agent pays |
   | **Settlement wallet** `0x2035d8d3c1BdBfe4b4Ab885Bd30782EDf47864d6` | Base Sepolia **ETH** | the gate broadcasts the EIP-3009 tx and pays gas |

## 1. Bring up the testnet stack (on the test box)

```bash
cp .env.testnet .env          # compose hardcodes `env_file: .env`
docker compose up -d --build  # builds the gate image from source
```

## 2. Smoke-test the 402 (no SDK, no funds)

Confirms the gate emits both 402 shapes a generic agent can parse:

```bash
curl -s -D - -o /tmp/x402.json "http://localhost:6080/api/x402/access?minutes=60" | grep -i 'payment-required\|HTTP/'
# expect: HTTP/1.1 402 and a base64 PAYMENT-REQUIRED header (the v2 requirements)
cat /tmp/x402.json    # v1 body: x402Version: 1, accepts[0].maxAmountRequired, network "base-sepolia"
# v2 body (opt-in): add ?x402_version=2  -> x402Version: 2, accepts[0].amount, network "eip155:84532"
```

## 3. Run the agent

With Node 18+ on the box:
```bash
cd tools/x402-agent-test
npm install
AGENT_PRIVATE_KEY=0xYOUR_BASE_SEPOLIA_KEY \
AXONOS_BASE_URL=http://localhost:6080 \
node agent.mjs
```

No Node on the box? Run it in a container on the host network (reaches localhost
gate + session SSH port), mounting your SSH dir and the key:
```bash
docker run --rm --network host \
  -v "$PWD":/app -w /app -v "$HOME/.ssh":/root/.ssh \
  -e AGENT_PRIVATE_KEY=0xYOUR_BASE_SEPOLIA_KEY \
  -e AXONOS_BASE_URL=http://localhost:6080 \
  -e SSH_KEY=/root/.ssh/axonos_x402_test \
  node:20-bookworm bash -c "apt-get update -qq && apt-get install -y -qq openssh-client >/dev/null && npm install --silent && node agent.mjs"
```

Python alternative (same env knobs, v2-header path):
```bash
pip install 'x402[evm]' requests
AGENT_PRIVATE_KEY=0xYOUR_BASE_SEPOLIA_KEY AXONOS_BASE_URL=http://localhost:6080 python3 agent.py
```

Env knobs:
- `X402_NETWORK` (`agent.mjs` only) — `base-sepolia` (default) or `base` for mainnet.
- `MAX_USDC_BASE_UNITS` — SDK spend cap, default `5000000` (5 USDC). The SDK's own
  default is only 0.10 USDC, which is below a session's ~1 USDC — keep this set.
- `SSH_KEY` — path, default `~/.ssh/axonos_x402_test` (generated if absent).
- `REQUESTED_PROFILE` — `small`|`medium`|`large`|`max` (default `small`).
- `SSH_HOST_OVERRIDE` — e.g. `localhost` when the harness runs on the test box itself.
- `X402_BODY_VERSION` (`agent.mjs` only) — set `2` to request the v2 402 body via
  `?x402_version=2`; unset leaves the default v1 body that `x402-fetch` parses.

Fund the agent wallet with **≥ ~2 Base Sepolia USDC** so one session (~1 USDC) settles
with headroom.

### Expected output (happy path)
- `[x402] HTTP 200` with `granted: true`, `ssh_host/ssh_port/ssh_user`, `auth_token`,
  `remaining_minutes`, and a `payment.settlement_tx_hash`.
- `[ssh]` prints `whoami`, `uname -a`, `nvidia-smi -L`, `hello-from-x402-agent`.
- `[heartbeat] HTTP 200`, `[release] HTTP 200`.

## Troubleshooting

- **402 keeps repeating / SDK can't pay:** the agent wallet has no test USDC, or the
  SDK couldn't parse the requirements — re-check step 2. The gate logs the settle reason.
- **`granted: false` with a settlement error:** usually the **settlement wallet** is out of
  Base Sepolia ETH (gas), or the agent wallet's USDC balance < amount.
- **SSH connection refused/timeout:** the session container needs a moment to boot; the test
  box must publish the per-session port and have a GPU for the launch to succeed.
- **`x402-fetch` API mismatch:** if your installed version expects a viem *walletClient*
  instead of an account, wrap with `createWalletClient({ account, chain: baseSepolia, transport: http() })`.
- **`x402-fetch` rejects the requirements:** make sure `X402_BODY_VERSION` is unset —
  the JS SDK parses the v1 body only; the v2 shape is for the header / Python SDK.
