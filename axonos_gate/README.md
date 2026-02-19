# AXGT Gate for AxonOS

This module implements AXGT token-based gating for AxonOS remote desktop access.

## Official References

- **AxonDAO website**: `https://axondao.io`
- **AXGT contract (Ethereum mainnet)**: `0x6112C3509A8a787df576028450FebB3786A2274d`
- **Explorer**: `https://etherscan.io/address/0x6112C3509A8a787df576028450FebB3786A2274d`

## Overview

AxonOS access follows a hold-based credit policy:

- User must hold at least `AXGT_MIN_HOLD_AMOUNT` (default `100` AXGT).
- Usage is metered off-chain per wallet (global across sessions/devices).
- Capacity is linear: each `100 AXGT` contributes `AXGT_CREDIT_PER_100_AXGT_MINUTES` (default `60`) total usable minutes.
- Access locks when remaining minutes reach `0`; user must increase held AXGT to increase capacity.
- Wallet ownership proof is strict via signed challenge (`personal_sign`) and one-time, wallet-bound nonces.

## Configuration

Required environment variables:

- `AXGT_CONTRACT_ADDRESS`: AXGT ERC-20 contract address
- `AXGT_CHAIN_ID`: Ethereum chain ID
- `AXGT_RPC_URL`: Ethereum RPC endpoint

Optional hardening environment variables:

- `AXGT_CORS_ORIGINS`: CORS allowlist for `/api/auth/verify-wallet`. Use comma-separated origins (exact match) or `*` to allow any. Default: same-origin only.
- `AXGT_RATE_LIMIT_PER_MIN`: Best-effort per-client rate limit for verify calls. Default: `60`. Set `0` to disable.
- `AXGT_EXPECTED_CONTRACT_ADDRESS`: Optional safety check; if set, the gate will only accept this contract address.
- `AXGT_MIN_HOLD_AMOUNT`: Minimum AXGT required to enter hold-based access.
- `AXGT_CREDIT_PER_100_AXGT_MINUTES`: Capacity minutes granted per 100 AXGT held.
- `AXGT_WARNING_THRESHOLD_MINUTES`: Warning threshold used by UI overlays.
- `AXGT_USAGE_DB_PATH`: Persistent usage ledger path (JSON). Default: `/var/lib/axonos_gate/usage.json`.
- `AXGT_AUTH_TOKEN_TTL_SECONDS`: Short-lived auth token TTL.
- `AXGT_CHALLENGE_TTL_SECONDS`: Signed challenge TTL.
- `AXGT_AUTH_COOKIE_NAME`: HttpOnly cookie key for auth token.
- `AXGT_AUTH_COOKIE_SECURE`: Whether auth cookie is marked Secure (recommended `true` for HTTPS).
- `AXGT_AUTH_ROTATE_BEFORE_EXPIRY_SECONDS`: Rotate current token only near expiry.
- `AXGT_AUTH_GRACE_SECONDS`: Keep previous token briefly valid to prevent rotation races.

Gate server (when tunnel points at gate):

- `GATE_HOST`: Bind address for gate (default: `127.0.0.1`). Set to `0.0.0.0` when exposing the gate for a tunnel.
- `GATE_PORT`: Port for the gate (default: `8889`).

Additional configuration for websockify:

- `WEBSOCKIFY_HOST`: Bind address for websockify (default: `0.0.0.0`).
- `WEBSOCKIFY_PORT`: Port for websockify server (default: `6080`)
- `VNC_HOST`: VNC server host (default: `localhost`)
- `VNC_PORT`: VNC server port (default: `5901`)
- `NOVNC_WEB_DIR`: Directory containing noVNC web files (default: `/usr/share/novnc`)

**Gradio-tunneling (and other HTTP-only tunnels):** Many tunnels (including gradio-tunneling’s FRP “http” proxy) do not forward WebSocket upgrades. If you get “WebSocket connection refused” when the tunnel points at 6080, point the tunnel at the **gate** (port 8889) instead:

1. Expose the gate: `-p 8889:8889` (and keep `-p 6080:6080` for the backend).
2. Set `GATE_HOST=0.0.0.0` so the gate listens on all interfaces.
3. Run the tunnel to the gate: `gradio-tun 8889` (not `gradio-tun 6080`).

The gate will serve the noVNC page and `/api/auth/*`, issue `auth_token`, and proxy WebSocket `/websockify` to 6080 (connections from 127.0.0.1 are accepted by websockify_gate without a separate secret).

## Components

- `axgt_verifier.py`: Core wallet verification logic using Ethereum RPC
- `websockify_gate.py`: WebSocket gate wrapper for websockify
- `gate_server.py`: HTTP server for serving HTML and API endpoints; when the tunnel points at 8889, also handles WebSocket `/websockify` and proxies to websockify_gate on 6080

## API Endpoints

### GET /api/auth/challenge?wallet_address=0x...

Returns a one-time challenge bound to the wallet address.

**Response:**
```json
{
  "challenge": "AxonOS verify\nWallet: 0x...\nNonce: ...\nIssuedAt: ...",
  "challenge_expires_in_seconds": 180
}
```

### POST /api/auth/verify-wallet

Verify signed wallet challenge and issue short-lived session token.

**Request:**
```json
{
  "wallet_address": "0x...",
  "message": "AxonOS verify\nWallet: ...",
  "signature": "0x..."
}
```

**Response:**
```json
{
  "verified": true,
  "access_type": "holding_credit",
  "remaining_minutes": 54.2,
  "consumed_minutes": 5.8,
  "capacity_minutes": 60,
  "auth_token_expires_in_seconds": 300
}
```

### GET /api/auth/wallet-status?wallet_address=0x...

Returns current usage status and consumes elapsed usage time. Requires valid auth token via HttpOnly cookie (or `X-AXGT-Auth-Token` header).

## Security

- Wallet ownership is required (signed challenge); unsigned verification is rejected.
- Challenges are one-time and wallet-bound to prevent replay.
- WebSocket auth token is carried by HttpOnly cookie instead of query string.
- `wallet-status` requires auth token, preventing unauthenticated usage drain.
- Near-expiry rotation with grace overlap reduces websocket disconnect races.

## Installation

1. Install dependencies:
```bash
pip3 install -r requirements.txt
```

2. Set environment variables (see Configuration above)

3. Run the gate server:
```bash
python3 websockify_gate.py
```

The server will start on port 6080 (or configured port) and gate all WebSocket connections.
