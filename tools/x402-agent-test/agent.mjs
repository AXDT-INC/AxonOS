// AxonOS x402 agent end-to-end test harness.
//
// Drives a GENERIC x402 agent (Coinbase x402 SDK: `x402-fetch` + `viem`) against
// a *testnet* AxonOS gate to confirm the full agentic loop:
//   pay (x402 / EIP-3009)  ->  claim SSH session  ->  run commands  ->  heartbeat  ->  release
//
// It is network-agnostic AxonOS-wise: the SDK reads the 402 PaymentRequirements
// the gate returns (v2 body by default) and auto-signs the USDC payment.
//
// Run against a LOCAL testnet stack only — never point this at the mainnet gate.
//
//   AGENT_PRIVATE_KEY=0x...   (Base Sepolia EOA holding test USDC; signs the payment)
//   AXONOS_BASE_URL=http://localhost:6080
//   node agent.mjs
//
// See README.md for funding + bring-up steps.

import { wrapFetchWithPayment, createSigner } from "x402-fetch";
import { privateKeyToAccount } from "viem/accounts";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";

const BASE_URL = (process.env.AXONOS_BASE_URL || "http://localhost:6080").replace(/\/$/, "");
const PRIVATE_KEY = process.env.AGENT_PRIVATE_KEY;
const SSH_KEY = resolve(process.env.SSH_KEY || `${homedir()}/.ssh/axonos_x402_test`);
const PROFILE = process.env.REQUESTED_PROFILE || "small";
const X402_NETWORK = process.env.X402_NETWORK || "base-sepolia"; // "base" for mainnet
// SDK refuses to pay above this (its default is only 0.10 USDC); a session is ~1 USDC.
const MAX_VALUE = BigInt(process.env.MAX_USDC_BASE_UNITS || "5000000"); // 5 USDC (6 decimals)
const SSH_HOST_OVERRIDE = process.env.SSH_HOST_OVERRIDE || ""; // "localhost" for same-host tests

if (!PRIVATE_KEY || !/^0x[0-9a-fA-F]{64}$/.test(PRIVATE_KEY)) {
  console.error("FATAL: set AGENT_PRIVATE_KEY to a 0x-prefixed 32-byte hex key (Base Sepolia, holds test USDC).");
  process.exit(1);
}

const log = (step, msg, obj) => {
  console.log(`\n[${step}] ${msg}`);
  if (obj !== undefined) console.log(JSON.stringify(obj, null, 2));
};

// --- 0. Identity: payment account + an SSH keypair to inject into the session ---
const account = privateKeyToAccount(PRIVATE_KEY);
log("identity", `agent wallet = ${account.address}`);

if (!existsSync(SSH_KEY)) {
  mkdirSync(dirname(SSH_KEY), { recursive: true });
  log("ssh-keygen", `generating ed25519 keypair at ${SSH_KEY}`);
  execFileSync("ssh-keygen", ["-t", "ed25519", "-N", "", "-f", SSH_KEY, "-C", "axonos-x402-test"], { stdio: "inherit" });
}
const sshPubkey = readFileSync(`${SSH_KEY}.pub`, "utf8").trim();

// --- 1. Pay + claim an SSH session in one call (the agent-native endpoint) ---
// wrapFetchWithPayment intercepts the 402, signs the EIP-3009 authorization from
// the gate's PaymentRequirements, and retries with the X-PAYMENT header.
const signer = await createSigner(X402_NETWORK, PRIVATE_KEY);
const fetchWithPay = wrapFetchWithPayment(fetch, signer, MAX_VALUE);

const sessionUrl = new URL(`${BASE_URL}/api/x402/session`);
if (process.env.X402_BODY_VERSION) sessionUrl.searchParams.set("x402_version", process.env.X402_BODY_VERSION);
log("x402", `POST ${sessionUrl} (SDK auto-pays on 402)`);
const res = await fetchWithPay(sessionUrl, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    wallet_address: account.address,
    ssh_pubkey: sshPubkey,
    requested_profile: PROFILE,
  }),
});

const session = await res.json().catch(() => ({}));
log("x402", `HTTP ${res.status}`, session);
if (!res.ok || !session.granted) {
  console.error("FATAL: session not granted. Check funds (test USDC + settlement-wallet gas) and the gate logs.");
  process.exit(2);
}

const { ssh_host, ssh_port, ssh_user, auth_token } = session;
const sshHost = SSH_HOST_OVERRIDE || ssh_host;
const authHeaders = { "Content-Type": "application/json", "X-AXGT-Auth-Token": auth_token };

// --- 2. SSH in and run commands (retry: sshd in the fresh container needs a moment) ---
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const remote = "whoami; uname -a; (nvidia-smi -L || echo 'no gpu in container'); echo 'hello-from-x402-agent'";
const sshArgs = [
  "-i", SSH_KEY, "-p", String(ssh_port),
  "-o", "StrictHostKeyChecking=no",
  "-o", "UserKnownHostsFile=/dev/null",
  "-o", "ConnectTimeout=10",
  `${ssh_user}@${sshHost}`, remote,
];
log("ssh", `ssh -p ${ssh_port} ${ssh_user}@${sshHost}  ->  ${remote}`);
let sshOk = false;
for (let attempt = 1; attempt <= 18; attempt++) {
  try {
    console.log(execFileSync("ssh", sshArgs, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));
    sshOk = true;
    break;
  } catch (e) {
    const msg = (e.stderr || e.message || "").trim().split("\n")[0];
    if (attempt === 18) { console.error("SSH failed after retries:", msg); break; }
    console.log(`  sshd not ready (attempt ${attempt}/18): ${msg} — heartbeat + retry in 5s`);
    await fetch(`${BASE_URL}/api/session/heartbeat`, { method: "POST", headers: authHeaders, body: JSON.stringify({ wallet_address: account.address }) }).catch(() => {});
    await sleep(5000);
  }
}
log("ssh", sshOk ? "commands ran OK" : "SSH never became reachable");

// --- 3. Heartbeat (keep the session billable/alive) ---
const hb = await fetch(`${BASE_URL}/api/session/heartbeat`, {
  method: "POST", headers: authHeaders, body: JSON.stringify({ wallet_address: account.address }),
});
log("heartbeat", `HTTP ${hb.status}`, await hb.json().catch(() => ({})));

// --- 4. Release ---
const rel = await fetch(`${BASE_URL}/api/session/release`, {
  method: "POST", headers: authHeaders, body: JSON.stringify({ wallet_address: account.address }),
});
log("release", `HTTP ${rel.status}`, await rel.json().catch(() => ({})));

log("done", "x402 agent loop complete: pay -> session -> ssh -> heartbeat -> release.");
