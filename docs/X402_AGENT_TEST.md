# x402 Agent Test — verify from your local machine

Confirm an **off-the-shelf x402 payment SDK** can discover and pay AxonOS over
the **public URL** (`https://app.axonos.io`), using the official Coinbase
`x402` Python SDK. Discovery and payment settlement follow x402; obtaining an
AxonOS session additionally requires the existing wallet ownership challenge.

Everything runs in throwaway Docker containers — nothing installed on your host.

- **Test 1** (no money): discovery + 402 — proves the public endpoints work.
- **Test 2** (spends ~1 USDC): full SDK payment end-to-end.
- **Test 3** (optional): agent verifies ownership, then pays and gets an SSH GPU
  session in one authenticated call (or reclaims already-purchased credit).
- **Test 4**: verification — SSH in & run GPU commands, confirm the tx on-chain,
  re-check credit, and view live pricing.

> **Network:** the public gate settles on **Base mainnet** (`eip155:8453`,
> USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`). Tests 2–4 spend **real USDC**
> (about $1 per test). To rehearse with test funds instead, run your own stack
> from `.env.testnet` (Base Sepolia, `eip155:84532`) and point `GATE` at it — see
> the **Testnet variant** notes in each step and
> [`tools/x402-agent-test/README.md`](../tools/x402-agent-test/README.md).
>
> The wire format is the same on both networks: the 402 carries an x402 **v1
> body** (for JS `x402-fetch`) plus a **v2 `PAYMENT-REQUIRED` header** (for the
> Python `x402` SDK used below).

---

## Test 1 — Discovery + 402 (no wallet, no money)

```bash
# Discovery descriptor (should print JSON with 3 endpoints)
docker run --rm curlimages/curl:latest -s https://app.axonos.io/.well-known/x402

# 402 with the v2 PAYMENT-REQUIRED header (proves the canonical resource gate)
docker run --rm curlimages/curl:latest -s -D - -o /dev/null \
  "https://app.axonos.io/api/x402/access?wallet_address=0x1111111111111111111111111111111111111111" \
  | grep -iE "HTTP/|payment-required"
```

**Expect:** the descriptor JSON, then `HTTP/… 402` and a `PAYMENT-REQUIRED:` header.
If you get those, the public ingress is serving x402 correctly.

---

## Test 2 — Full payment with the official x402 SDK (spends ~1 USDC)

### a) Create + fund a throwaway wallet

```bash
# Generate a throwaway wallet; print only its address. Keep the key file private.
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.11-slim sh -c \
  "pip install -q --target /tmp/agent-deps eth-account >/dev/null 2>&1 && PYTHONPATH=/tmp/agent-deps python -c 'import os; from eth_account import Account; a=Account.create(); f=os.open(\"agent-private-key.txt\", os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(f, a.key.hex().encode()); os.close(f); print(a.address)'"
```

Save the displayed **address** and protect `agent-private-key.txt` (never commit
or log its contents). Send **~1–2 USDC on
Base mainnet** to the address (from an exchange or another wallet). No ETH(Base)
needed — AxonOS pays gas.

> **Testnet variant:** fund with Base Sepolia test USDC instead (Circle faucet:
> https://faucet.circle.com → Base Sepolia) and target your self-hosted
> `.env.testnet` gate.

### b) Run the official SDK against the public URL

Save this as `x402_agent_test.py`:

```python
import json, os, sys, requests
from eth_account import Account
from x402.client import x402ClientSync
from x402.http import x402HTTPClientSync, PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme

GATE = os.environ.get("GATE", "https://app.axonos.io").rstrip("/")
# CAIP-2 network the gate advertises: Base mainnet for the public gate,
# eip155:84532 for a self-hosted Base Sepolia (.env.testnet) stack.
NETWORK = os.environ.get("X402_NETWORK", "eip155:8453")
acct = Account.from_key(os.environ["EVM_PRIVKEY"])
print(f"Generic x402 agent wallet: {acct.address}\nGate: {GATE}\nNetwork: {NETWORK}\n")

client = x402ClientSync()
# v2 scheme, registered under the CAIP-2 network AxonOS advertises
client.register(NETWORK, ExactEvmClientScheme(signer=EthAccountSigner(acct)))
http = x402HTTPClientSync(client)

url = f"{GATE}/api/x402/access?wallet_address={acct.address}"
r1 = requests.get(url, timeout=30)
print(f"GET access -> HTTP {r1.status_code}")
if r1.status_code != 402:
    print(r1.text[:300]); sys.exit(1)

# SDK reads the 402, signs an EIP-3009 USDC payment, returns the payment header
add_headers, _ = http.handle_402_response(dict(r1.headers), r1.content)
print("SDK created payment header:", list(add_headers.keys()))

# Retry the SAME resource with the payment (canonical x402 loop)
r2 = requests.get(url, headers=add_headers, timeout=180)
body = r2.json() if r2.content else {}
print(f"\nGET access + payment -> HTTP {r2.status_code}")
print(json.dumps(body, indent=2)[:500])
ok = r2.status_code == 200 and (body.get("access") or (body.get("payment") or {}).get("verified"))
print("\n=== RESULT:", "PASS — off-the-shelf x402 SDK paid AxonOS" if ok else "FAIL", "===")
sys.exit(0 if ok else 1)
```

Run it (replace the key with your funded throwaway key). The snippet targets the
Python `x402` SDK 2.13.x; if a newer release renames these imports, compare with
`tools/x402-agent-test/agent.py`, which is kept in step with the SDK:

```bash
read -r EVM_PRIVKEY < agent-private-key.txt || test -n "$EVM_PRIVKEY"
export EVM_PRIVKEY
docker run --rm -e EVM_PRIVKEY \
  -v "$PWD/x402_agent_test.py:/t.py:ro" \
  python:3.11-slim sh -c "pip install -q 'x402[evm]' requests >/dev/null 2>&1; python /t.py"

# Testnet variant (self-hosted .env.testnet stack):
#   add  -e GATE=http://<testbox>:6080 -e X402_NETWORK=eip155:84532
```

**Expect:**
```
GET access -> HTTP 402
SDK created payment header: ['PAYMENT-SIGNATURE']
GET access + payment -> HTTP 200
{ "access": true, "remaining_minutes": 60.0,
  "payment": { "verified": true, "credited_minutes": 60.0, "settlement_tx_hash": "0x..." } }
=== RESULT: PASS — off-the-shelf x402 SDK paid AxonOS ===
```

That `settlement_tx_hash` is a real Base transaction — verify it on
https://basescan.org (or https://sepolia.basescan.org for a testnet stack).

---

## Test 3 — Agent pays AND gets an SSH GPU session (optional)

The AxonOS-native flow: verify wallet ownership, then pay and claim SSH in one
authenticated call. It requires an SSH keypair. The payment authorization alone
does not authenticate the SSH requester: EIP-3009 signatures become public on
chain and do not bind the SSH key. A funded wallet address is never sufficient.

```bash
# SSH keypair for the agent
ssh-keygen -t ed25519 -f ./agent_key -N "" -q

# The Python example below uses the same wallet account and SDK client as Test 2.
```

Use the existing challenge API to prove ownership. The signed challenge is
one-time and wallet-bound; keep the resulting bearer in memory. An unfunded
wallet can authenticate: a response with `verified: false` can still contain
the ownership token. Both Flask and websockify enforce the same requirement.

Then request a session with the token. If Test 2 already funded the wallet, this
reclaims its credit without another payment. Only a new 402 response should
cause a fresh payment authorization; do not reuse Test 2's submitted signature.

```python
from eth_account.messages import encode_defunct

challenge_response = requests.get(f"{GATE}/api/auth/challenge",
    params={"wallet_address": acct.address}, timeout=30)
challenge_response.raise_for_status()
challenge = challenge_response.json()["challenge"]
assert challenge.startswith(f"AxonOS verify\nWallet: {acct.address.lower()}\nNonce: ")
signature = acct.sign_message(encode_defunct(text=challenge)).signature.hex()
if not signature.startswith("0x"):
    signature = "0x" + signature
verified_response = requests.post(f"{GATE}/api/auth/verify-wallet",
    json={"wallet_address": acct.address, "message": challenge, "signature": signature}, timeout=30)
verified = verified_response.json()
assert verified_response.status_code in (200, 403) and verified.get("auth_token")
assert verified["wallet_address"].lower() == acct.address.lower()
auth_headers = {"X-AXGT-Auth-Token": verified["auth_token"]}

ssh_pub = open("agent_key.pub").read().strip()
session_body = {"wallet_address": acct.address, "ssh_pubkey": ssh_pub}
session_url = f"{GATE}/api/x402/session"
r = requests.post(session_url, headers=auth_headers, json=session_body, timeout=180)
if r.status_code == 402:
    payment_headers, _ = http.handle_402_response(dict(r.headers), r.content)
    r = requests.post(session_url, headers={**payment_headers, **auth_headers},
        json=session_body, timeout=180)
d = r.json()
# Never print the whole response: it can contain a refreshed authentication token.
print({k: d[k] for k in ("granted", "ssh_host", "ssh_port", "ssh_user") if k in d})
if d.get("auth_token"):
    auth_headers["X-AXGT-Auth-Token"] = d["auth_token"]
if not d.get("granted"):
    tx_hash = (d.get("payment") or {}).get("settlement_tx_hash")
    print("Session not granted; reconcile payment before retrying. Transaction:", tx_hash)
# then: ssh -i agent_key -p <d['ssh_port']> <d['ssh_user']>@<d['ssh_host']>
```

**Expect:** `granted: true` with `ssh_host` / `ssh_port` / `ssh_user`, then you can
`ssh` in and run commands on the GPU box.

If authentication expires during settlement, get a new challenge/token first.
For a pending payment, POST `/api/auth/verify-usdc-deposit` with that token and
`{"wallet_address": acct.address, "tx_hash": tx_hash}` until it is confirmed;
do not resubmit the payment signature. If verification says already credited,
check authenticated `/api/auth/wallet-status`. Retry `/api/x402/session` with
the SSH body and token **without** a payment header or automatic-payment retry.
Do not authorize another payment merely because a session was denied or a
response was lost. A transaction hash permits reconciliation, never login.

---

## Test 4 — Verification & follow-up checks

Extra checks you can run from your laptop after Test 2 / Test 3.

### 4a — SSH in and run GPU commands (the full capstone)

After Test 3 returns `ssh_host` / `ssh_port` / `ssh_user`, connect and run a job.
Replace `<PORT>` / `<USER>` / `<HOST>` with the values from Test 3:

```bash
ssh -i ./agent_key -p <PORT> \
  -o StrictHostKeyChecking=accept-new \
  <USER>@<HOST> \
  'echo READY && hostname && nproc && nvidia-smi -L && python3 -c "print(sum(i*i for i in range(1_000_000)))"'
```

**Expect:** `READY`, the container hostname, CPU count, one `GPU 0: …` line per
assigned GPU (model depends on the host), and `333332833333500000`. That's an
agent running a real workload on a GPU it paid for.

> `ssh_host` is whatever the operator set in `AXGT_SSH_PUBLIC_HOST` (the public
> DNS name of the compute host) on the per-session port. If a direct `ssh` is
> blocked by your network, the same key works from anywhere with outbound access
> to that host/port.

### 4b — Verify the settlement on-chain (independent of AxonOS)

Take the `settlement_tx_hash` from Test 2/3 and confirm it on Base (use
`https://sepolia.base.org` for a testnet stack):

```bash
TX=0xYOUR_SETTLEMENT_TX_HASH
docker run --rm curlimages/curl:latest -s -X POST https://mainnet.base.org \
  -H 'content-type: application/json' \
  --data "{\"jsonrpc\":\"2.0\",\"method\":\"eth_getTransactionReceipt\",\"params\":[\"$TX\"],\"id\":1}" \
  | docker run --rm -i python:3.11-slim python -c \
    "import sys,json; r=json.load(sys.stdin)['result']; print('status', r['status'], '(0x1=success)'); print('block', int(r['blockNumber'],16)); print('USDC Transfer logs:', len(r['logs']))"
```

**Expect:** `status 0x1`, a block number, and `USDC Transfer logs: 2`. Or just open
`https://basescan.org/tx/<TX>` in a browser — you'll see the USDC transfer
to the revenue wallet. Proof the payment is real, not AxonOS's say-so.

### 4c — Re-check access (credit persisted, no second payment)

Confirm the minutes you bought stuck — request access again with **no** payment:

```bash
docker run --rm curlimages/curl:latest -s \
  "https://app.axonos.io/api/x402/access?wallet_address=0xYOUR_AGENT_WALLET"
```

**Expect:** `200` with `{"access": true, "remaining_minutes": <something > 0>}` —
i.e. you're funded now, no new 402. (Minutes tick down as a session runs.)

### 4d — Live pricing / discount quotes

See the live USD-equivalent pricing and the AXGT bonus from your side (no payment):

```bash
W=0xYOUR_AGENT_WALLET
for CUR in usdc eth axgt; do
  echo "== $CUR =="
  docker run --rm curlimages/curl:latest -s \
    "https://app.axonos.io/api/discount/quote?currency=$CUR&wallet_address=$W"
  echo
done
```

**Expect:** USDC ≈ fixed ($1 → 60 min), ETH priced at live USD value, AXGT showing
the **+25% bonus** (`estimated_minutes` higher per USD-equivalent). A wallet holding
AXGT will also show a non-zero `discount_percent` on the ETH/USDC quotes.

---

## Notes

- **Costs:** Test 1 = free. Test 2 = ~1 USDC (AxonOS pays the gas). Test 3 uses
  existing prepaid credit, or pays if none remains, and starts a real billable
  GPU session. Wallet verification itself does not cost cryptocurrency.
- **Default pricing:** 1 USDC ≈ 60 minutes ($1/hour). AXGT holders get a discount
  on ETH/USDC; paying in AXGT gets a +25% bonus.
- **Security:** use a *throwaway* wallet with only a little USDC. The private
  key goes in an env var for the test container only.
- **Testnet:** the SDK reads chain/asset/amount from the 402, so the same test
  works against a self-hosted Base Sepolia stack (`cp .env.testnet .env` on a
  non-production box) — register the scheme under `eip155:84532` and fund with
  test USDC. Never copy `.env.testnet` onto the production host.
