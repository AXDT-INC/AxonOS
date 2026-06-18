#!/usr/bin/env python3
"""
AxonOS x402 agent harness — Python Coinbase x402 SDK (the v2 PAYMENT-REQUIRED header path).

Mirror of agent.mjs but using the official Python `x402` SDK (PyPI 2.13.0), which
detects v2 from the `PAYMENT-REQUIRED` response header and pays via PAYMENT-SIGNATURE.
Walks: pay (x402 v2) -> claim SSH session -> run commands -> heartbeat -> release.

Testnet only. Needs:  pip install 'x402[evm]' requests
  AGENT_PRIVATE_KEY   Base Sepolia EOA holding test USDC, with NO prepaid minutes
  AXONOS_BASE_URL     default http://localhost:6080
"""
import os, sys, json, subprocess, time, pathlib

from eth_account import Account
from x402 import x402ClientSync, max_amount
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.http.clients.requests import x402_requests
import requests

BASE_URL = os.environ.get("AXONOS_BASE_URL", "http://localhost:6080").rstrip("/")
PRIVATE_KEY = os.environ.get("AGENT_PRIVATE_KEY", "")
SSH_KEY = os.path.expanduser(os.environ.get("SSH_KEY", "~/.ssh/axonos_x402_test"))
SSH_HOST_OVERRIDE = os.environ.get("SSH_HOST_OVERRIDE", "")
PROFILE = os.environ.get("REQUESTED_PROFILE", "small")
# SDK won't pay above this; a session is ~1 USDC. (6 decimals)
MAX_VALUE = int(os.environ.get("MAX_USDC_BASE_UNITS", "5000000"))

if not (PRIVATE_KEY.startswith("0x") and len(PRIVATE_KEY) == 66):
    sys.exit("FATAL: set AGENT_PRIVATE_KEY to a 0x-prefixed 32-byte hex key (Base Sepolia, holds test USDC).")


def log(step, msg, obj=None):
    print(f"\n[{step}] {msg}")
    if obj is not None:
        print(json.dumps(obj, indent=2))


acct = Account.from_key(PRIVATE_KEY)
log("identity", f"agent wallet = {acct.address}")

# SSH keypair to inject into the session
if not os.path.exists(SSH_KEY):
    os.makedirs(os.path.dirname(SSH_KEY), exist_ok=True)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", SSH_KEY, "-C", "axonos-x402-test-py"], check=True)
ssh_pubkey = pathlib.Path(SSH_KEY + ".pub").read_text().strip()

# x402 client — register the EVM "exact" scheme (v2 eip155:* + v1) with a spend cap.
client = x402ClientSync()
register_exact_evm_client(client, EthAccountSigner(acct), policies=[max_amount(MAX_VALUE)])
session = x402_requests(client)  # requests.Session that auto-handles 402

url = f"{BASE_URL}/api/x402/session"
log("x402", f"POST {url} (Python SDK auto-pays on 402 via PAYMENT-REQUIRED header)")
res = session.post(url, json={"wallet_address": acct.address, "ssh_pubkey": ssh_pubkey, "requested_profile": PROFILE}, timeout=180)
sess = res.json() if res.content else {}
log("x402", f"HTTP {res.status_code}", sess)
if not res.ok or not sess.get("granted"):
    sys.exit("FATAL: session not granted. Check funds (test USDC + settlement gas) and gate logs.")

ssh_host = SSH_HOST_OVERRIDE or sess["ssh_host"]
ssh_port, ssh_user, auth_token = sess["ssh_port"], sess["ssh_user"], sess["auth_token"]
auth_headers = {"X-AXGT-Auth-Token": auth_token}

remote = "whoami; uname -a; (nvidia-smi -L || echo 'no gpu in container'); echo 'hello-from-x402-agent-py'"
log("ssh", f"ssh -p {ssh_port} {ssh_user}@{ssh_host}  ->  {remote}")
ssh_ok = False
for attempt in range(1, 19):
    try:
        out = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-p", str(ssh_port),
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10", f"{ssh_user}@{ssh_host}", remote],
            capture_output=True, text=True, timeout=40)
        if out.returncode == 0:
            print(out.stdout)
            ssh_ok = True
            break
        err = (out.stderr or "").strip().split("\n")[0]
    except subprocess.TimeoutExpired:
        err = "ssh timeout"
    if attempt == 18:
        print("SSH failed after retries:", err)
        break
    print(f"  sshd not ready ({attempt}/18): {err} — heartbeat + retry in 5s")
    requests.post(f"{BASE_URL}/api/session/heartbeat", headers=auth_headers, json={"wallet_address": acct.address}, timeout=20)
    time.sleep(5)
log("ssh", "commands ran OK" if ssh_ok else "SSH never became reachable")

hb = requests.post(f"{BASE_URL}/api/session/heartbeat", headers=auth_headers, json={"wallet_address": acct.address}, timeout=20)
log("heartbeat", f"HTTP {hb.status_code}", hb.json() if hb.content else {})

rel = requests.post(f"{BASE_URL}/api/session/release", headers=auth_headers, json={"wallet_address": acct.address}, timeout=20)
log("release", f"HTTP {rel.status_code}", rel.json() if rel.content else {})

log("done", "x402 (Python SDK, v2 PAYMENT-REQUIRED path) loop complete.")
