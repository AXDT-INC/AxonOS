# AxonOS Environment Variables

Complete reference for every environment variable read or propagated by the AxonOS codebase. Use this alongside [`env.example`](../env.example) (copy to `.env` for local/compose deploys).

---

## How configuration works

| Layer | Mechanism | Notes |
|-------|-----------|-------|
| **Operator `.env`** | `env_file: .env` in [`docker-compose.yml`](../docker-compose.yml) | Primary runtime config for gate, launcher, and postgres credentials |
| **Docker Compose overrides** | `environment:` blocks in compose | Injects DB URL, launcher URL, gate bind, WebRTC defaults |
| **Docker build args** | `docker build --build-arg …` / compose `build.args` | Image credentials and NVIDIA userspace pinning |
| **Runtime injection** | Session launcher `docker run -e …` | Per-session vars (`AXGT_SESSION_ID`, GPU assignment, passthrough list) |
| **Image defaults** | `ENV` in [`Dockerfile`](../Dockerfile), supervisord | NVIDIA, OpenMPI, VirtualGL — usually not overridden |

**Build-time vs run-time:** `AXONOS_VNC_PASSWORD` / `PASSWORD` and `NVIDIA_DRIVER_PKG_VERSION` affect the image at build. Most `AXGT_*` and `WEBRTC_*` vars are read at process start (gate, websockify, launcher, agent).

**Boolean convention:** Truthy values are `1`, `true`, `yes`, `on` (case-insensitive). Falsy for feature flags is often `0`, `false`, `no`, `off`.

---

## Quick reference

### Required for production gate + billing

| Variable | Default | Purpose |
|----------|---------|---------|
| `AXGT_CONTRACT_ADDRESS` | *(none)* | AXGT ERC-20 contract for balance checks / legacy AXGT deposits |
| `AXGT_CHAIN_ID` | *(none)* | Chain ID exposed to UI and wallet flows (`1` mainnet, `11155111` Sepolia, …) |
| `AXGT_RPC_URL` | *(none)* | JSON-RPC endpoint for deposits, receipts, `balanceOf` |
| `AXGT_REVENUE_WALLET` | *(none)* | Recipient for ETH / AXGT deposits |
| `AXGT_CHALLENGE_DB_URL` | *(none)* | Postgres for challenges, auth tokens, sessions, deposit ledger |

With repo `docker-compose.yml`, `AXGT_CHALLENGE_DB_URL` is **auto-set** on the `axonos` and `axonos-launcher` services — omit it from `.env` unless using an external DB.

### Compose / deploy (common)

| Variable | Default (compose) | Purpose |
|----------|-------------------|---------|
| `AXONOS_VNC_PASSWORD` | *(required in `.env`)* | Build arg `PASSWORD` — VNC + in-container sudo |
| `POSTGRES_USER` | `axonos_gate` | Bundled Postgres user |
| `POSTGRES_PASSWORD` | `axonos_gate_secret` | Bundled Postgres password |
| `POSTGRES_DB` | `axonos_gate` | Bundled Postgres database name |
| `AXONOS_PUBLISH_NOVNC` | `6080` | Host port → container 6080 (noVNC) |
| `AXONOS_PUBLISH_GATE` | `8889` | Host port → container 8889 (gate API) |
| `NVIDIA_DRIVER_PKG_VERSION` | *(empty)* | Pin NVIDIA userspace packages to host driver version |

---

## Build and image

### `AXONOS_VNC_PASSWORD`

- **When:** Docker build (`--build-arg PASSWORD=…`)
- **Used by:** [`Dockerfile`](../Dockerfile), [`docker-compose.yml`](../docker-compose.yml), [`scripts/build_axonos.sh`](../scripts/build_axonos.sh)
- **Purpose:** Sets the `aXonian` user password, VNC passwd file, and sudo access inside the desktop image.
- **Default in Dockerfile:** `axonpassword` (development only — always override in production).

### `PASSWORD`

- **When:** Docker build arg (same value as `AXONOS_VNC_PASSWORD` in compose).
- **Alias of:** Operator-facing name in scripts/docs vs internal Dockerfile arg name.

### `NVIDIA_DRIVER_PKG_VERSION`

- **When:** Docker build arg
- **Used by:** [`Dockerfile`](../Dockerfile), [`scripts/resolve-nvidia-driver-pkg-version.sh`](../scripts/resolve-nvidia-driver-pkg-version.sh), [`scripts/install-nvidia-xorg-userspace.sh`](../scripts/install-nvidia-xorg-userspace.sh)
- **Purpose:** Pins `xserver-xorg-video-nvidia`, `libnvidia-gl`, `libnvidia-cfg1`, and `libnvidia-common` to a single apt version matching the **host** `nvidia-smi` driver. Prevents Xorg GLX mismatch crashes.
- **Example:** `535.288.01-0ubuntu1` or triplet `535.288.01`
- **Default:** Empty — build resolves best available for major branch `535`.

### `NVIDIA_DRIVER_VERSION`

- **When:** Docker build (`ARG`, default `535`)
- **Purpose:** Major NVIDIA driver branch for package names (e.g. `535` → `libnvidia-gl-535`).

### `OLLAMA_INSTALL_SHA256`

- **When:** Docker build (optional)
- **Purpose:** If set, verifies SHA-256 of the Ollama install script before execution (supply-chain hardening).

### `USER` (Dockerfile `ENV`)

- **When:** Image build
- **Default:** `aXonian`
- **Purpose:** Primary desktop user inside the container. The AxonOS launcher CLI can rewrite this when generating custom Dockerfiles.

---

## Blockchain, deposits, and tokenomics

Read by [`axonos_gate/deposit_verifier.py`](../axonos_gate/deposit_verifier.py), [`axonos_gate/axgt_verifier.py`](../axonos_gate/axgt_verifier.py), [`axonos_gate/discount.py`](../axonos_gate/discount.py), [`axonos_gate/x402_verifier.py`](../axonos_gate/x402_verifier.py), [`axonos_gate/price_oracle.py`](../axonos_gate/price_oracle.py), and exposed via `GET /api/config`. See [`docs/TOKENOMICS.md`](TOKENOMICS.md) for the payment-rail model.

### Core chain config

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_CONTRACT_ADDRESS` | *(none)* | AXGT token contract (`0x…`). Used for discount tier `balanceOf` and legacy AXGT deposit verification. |
| `AXGT_CHAIN_ID` | *(none)* | Decimal chain ID; UI network banner and wallet network hints derive from this via `/api/config`. |
| `AXGT_RPC_URL` | *(none)* | HTTPS JSON-RPC URL for all on-chain reads and deposit verification. |
| `AXGT_REVENUE_WALLET` | *(none)* | Lowercase-normalized deposit destination for ETH and AXGT transfers. |
| `AXGT_TOKEN_DECIMALS` | `18` | ERC-20 decimals for in-page “Send min AXGT” UI when not fetched on-chain. |

### ETH deposits (primary payment rail)

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_ENABLE_ETH_DEPOSITS` | `true` | Enable native ETH deposit verification and UI. Set `false` to disable. |
| `ETH_MIN_DEPOSIT` | `0.0005` | Minimum ETH per deposit (before AXGT holder discount). |
| `ETH_CREDIT_PER_ETH_MINUTES` | `120000` | Minutes credited per 1 ETH (→ 0.0005 ETH ≈ 60 min at defaults). |

### AXGT-as-payment deposits (Model B)

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_ENABLE_AXGT_DEPOSITS` | `false` | Opt-in direct AXGT payment rail. Paying **in** AXGT is credited at live USD value with a flat bonus and **no** holder tier (tiers apply to ETH/USDC only). |
| `AXGT_MIN_DEPOSIT` | `100` | Minimum AXGT when direct deposits enabled. |
| `AXGT_CREDIT_PER_100_AXGT_MINUTES` | `60` | Fixed-rate fallback: minutes per 100 AXGT when dynamic pricing is off or the price feed is stale. |
| `AXGT_USD_BONUS_PERCENT` | `25` | Extra minutes (%) granted vs. the plain USD-equivalent when paying in AXGT (the "best deal" incentive). Applies only with `AXGT_DYNAMIC_PRICING=true`. |

### USDC deposits (stablecoin rail)

Self-verified on-chain (no facilitator) into the same deposit ledger. USDC lands in `AXGT_REVENUE_WALLET` **on the USDC chain (Base by default)**, which is independent of `AXGT_CHAIN_ID`. The in-page "Pay with USDC" button appears only when `USDC_CONTRACT_ADDRESS` is set. Read by [`axonos_gate/x402_verifier.py`](../axonos_gate/x402_verifier.py); exposed via `GET /api/config`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_ENABLE_USDC_DEPOSITS` | `true` | Enable the USDC rail + UI. Falsy (`0/false/no/off`) disables it. |
| `USDC_RPC_URL` | *(none)* | JSON-RPC endpoint for the USDC chain. Required for verification. |
| `USDC_CONTRACT_ADDRESS` | *(none)* | USDC token contract. Presence gates the "Pay with USDC" UI. |
| `USDC_CHAIN_ID` | `8453` | USDC chain ID (`8453` Base mainnet, `84532` Base Sepolia). |
| `USDC_NETWORK` | `base` | Network label (`base`, `base-sepolia`). |
| `USDC_MIN_DEPOSIT` | `1` | Minimum USDC per deposit. |
| `USDC_CREDIT_PER_USDC_MINUTES` | `60` | Minutes credited per 1 USDC (fixed at $1; no holder tier). |
| `USDC_DEPOSIT_MIN_CONFIRMATIONS` | `6` | Block confirmations before crediting a USDC transfer. |

### x402 protocol settlement

Agent-native HTTP-402 rail: `GET /api/x402/access` returns payment requirements, `POST /api/x402/settle` broadcasts an EIP-3009 `transferWithAuthorization` (the gate pays gas). Read by [`axonos_gate/x402_verifier.py`](../axonos_gate/x402_verifier.py).

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_ENABLE_X402_SETTLEMENT` | `true` | Enable `POST /api/x402/settle`. Still requires a funded `X402_SETTLEMENT_PRIVATE_KEY`. |
| `X402_SETTLEMENT_PRIVATE_KEY` | *(none)* | Dedicated **low-balance hot wallet** that pays gas for settlement. Must hold ETH-on-Base on mainnet. Unset = `/settle` disabled (tx-hash rail still works). **Never** use the revenue/treasury key; never log. |
| `USDC_EIP712_NAME` | `USD Coin` | EIP-712 domain name of the USDC token. Base mainnet = `USD Coin`; Base Sepolia = `USDC`. Must match on-chain or signature recovery fails. |
| `USDC_EIP712_VERSION` | `2` | EIP-712 domain version of the USDC token. |
| `X402_RESOURCE` | `/api/x402/access` | Resource path advertised in the 402 challenge. |
| `X402_RESOURCE_URL` | *(none)* | Absolute base URL for the advertised resource (the JS `x402-fetch` SDK requires an absolute `resource`). Falls back to `AXGT_PUBLIC_BASE_URL`, then the first CORS origin. |
| `AXGT_PUBLIC_BASE_URL` | *(none)* | Public origin (e.g. `https://desktop.axonos.io`) used to build the absolute x402 resource URL. |

### Dynamic USD-equivalent pricing (price oracle)

When enabled, ETH and AXGT deposits credit at their **live USD value** (CoinGecko free API, cached in Postgres); USDC stays fixed at $1. On a feed outage the last-known price is used up to `PRICE_MAX_STALE_SECONDS`, after which the fixed crypto rates apply. Read by [`axonos_gate/price_oracle.py`](../axonos_gate/price_oracle.py).

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_DYNAMIC_PRICING` | `false` | Enable live USD-equivalent crediting (introduces a server-side price oracle). |
| `AXGT_USD_PER_HOUR` | `1.0` | USD price of one hour of `small` (1-GPU) desktop time → minutes per USD. |
| `AXGT_COINGECKO_ID` | `axondao-governance-token-2` | CoinGecko coin ID for the AXGT/USD quote. |
| `PRICE_POLL_INTERVAL_SECONDS` | `10800` | Price refresh cadence (3 h ≈ 8×/day). |
| `PRICE_MAX_STALE_SECONDS` | `86400` | Reject cached prices older than this (then fall back to fixed rates). |

### Deposit verification tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_DEPOSIT_MIN_CONFIRMATIONS` | `6` | Block confirmations required before crediting. |
| `AXGT_WARNING_THRESHOLD_MINUTES` | `10` | Low-balance warning threshold in wallet status / UI. |

### AXGT holder discount tiers

Configure **one** of (first non-empty wins; else built-in defaults matching [`docs/TOKENOMICS.md`](TOKENOMICS.md)):

| Variable | Format | Description |
|----------|--------|-------------|
| `AXGT_DISCOUNT_TIERS_JSON` | JSON array | `[{"min_axgt":0,"discount_percent":0,"label":"Tier 0"}, …]` |
| `AXGT_DISCOUNT_TIERS_FILE` | Path | File containing the same JSON shape. |
| `AXGT_DISCOUNT_TIERS` | Compact | `0:0,100:5,1000:10,10000:15,100000:25` (`min:percent` pairs). |

Discount logic uses `AXGT_RPC_URL` + `AXGT_CONTRACT_ADDRESS` for server-side `balanceOf` — never client-reported balances.

### Documented but not implemented

| Variable | Status |
|----------|--------|
| `AXGT_EXPECTED_CONTRACT_ADDRESS` | Mentioned in [`axonos_gate/README.md`](../axonos_gate/README.md) and [`env.example`](../env.example) but **not referenced in Python code**. Use `AXGT_CONTRACT_ADDRESS` as the single source of truth today. |

---

## Database (Postgres)

| Variable | Default (compose) | Description |
|----------|-------------------|-------------|
| `AXGT_CHALLENGE_DB_URL` | Auto: `postgresql://axonos_gate:…@postgres:5432/axonos_gate` | Connection string for challenges, auth tokens, sessions, WebRTC signaling store, deposit + audit ledgers. **Required** for deposit-credit billing. |
| `POSTGRES_USER` | `axonos_gate` | Postgres superuser for bundled service (compose only). |
| `POSTGRES_PASSWORD` | `axonos_gate_secret` | Postgres password (compose only). |
| `POSTGRES_DB` | `axonos_gate` | Database name (compose only). |

**Consumers:** `session_manager`, `axgt_verifier`, `deposit_ledger`, `deposit_verifier`, `webrtc/store`, `gate_server`, `websockify_gate`.

---

## Authentication and HTTP security

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_AUTH_TOKEN_TTL_SECONDS` | `300` | Lifetime of wallet auth tokens (seconds). |
| `AXGT_CHALLENGE_TTL_SECONDS` | `180` | Wallet signature challenge nonce TTL. |
| `AXGT_AUTH_COOKIE_NAME` | `axgt_auth_token` | HttpOnly cookie name for auth token. |
| `AXGT_AUTH_COOKIE_SECURE` | `true` | Set `Secure` flag on auth cookie. |
| `AXGT_AUTH_ROTATE_BEFORE_EXPIRY_SECONDS` | `60` | Rotate token when within this many seconds of expiry (websockify path). |
| `AXGT_AUTH_GRACE_SECONDS` | `15` | Grace window after expiry for in-flight requests (websockify path). |
| `AXGT_CORS_ORIGINS` | *(empty = same-origin only)* | CORS for `/api/*`. Comma-separated origins or `*`. Parsed by [`security_utils.parse_cors_allowlist`](../axonos_gate/security_utils.py). |
| `AXGT_RATE_LIMIT_PER_MIN` | `60` | Max verify/auth calls per client IP+wallet per minute; `0` disables. (`env.example` suggests `30` as an example override.) |
| `AXGT_ADMIN_SECRET` | *(none)* | Enables `POST/GET /api/admin/*` when sent as header `X-AXGT-Admin-Secret` or query `admin_secret`. |

---

## Sessions, GPU scheduling, and billing

Read primarily by [`axonos_gate/session_manager.py`](../axonos_gate/session_manager.py).

### Feature flags

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_MULTI_SESSION_ENABLED` | `true` | Multi-session scheduler (exclusive GPU allocation). |
| `AXGT_GPU_PROFILES_ENABLED` | `true` | GPU profile selection (`small`/`medium`/`large`/`max`). |
| `AXGT_GPU_WEIGHTED_BILLING` | `true`* | Bill `wall_clock_minutes × GPU count` on heartbeat when profiles enabled. Set `false` for 1× billing. (*Enabled when profiles enabled and var not explicitly off.) |
| `AXGT_USER_CONTAINER_ENABLED` | `false` in code; `true` in compose | One container per claimed session vs shared desktop. |
| `AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST` | `true` | Pause session on zero credit instead of destroying container. |

### GPU pool

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_DEFAULT_GPU_PROFILE` | `small` | Default profile if client omits one (`small`=1, `medium`=2, `large`=4, `max`=8 GPUs). |
| `AXGT_GPU_DEVICE_IDS` | *(auto)* | Comma-separated GPU indices, e.g. `0,1,2,3`. Overrides auto-detect. |
| `AXGT_GPU_TOTAL_COUNT` | *(auto)* | Alternative to explicit IDs: use GPUs `0 .. N-1`. |
| `AXGT_GPU_AUTO_DETECT` | `true` | Run `nvidia-smi` locally; if empty, try launcher `GET /enumerate-gpus`. |
| `AXGT_GPU_DEVICE_CACHE_SECONDS` | `120` | TTL for cached auto-detected GPU list. |
| `AXGT_GPU_ENUMERATE_VIA_LAUNCHER` | `true` | When gate has no GPUs, probe host via HTTP launcher. |

### Session lifecycle

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SESSION_MAX_MINUTES` | `60` | Idle cap — extended on each heartbeat; session ends when exceeded or heartbeat stale. |
| `AXGT_HEARTBEAT_TIMEOUT_SECONDS` | `120` | No heartbeat → session considered stale and released. |
| `AXGT_SSH_MAX_SESSION_MINUTES` | *(unset = affordability only)* | Hard, **non-sliding** billing ceiling (minutes) for headless/SSH sessions kept alive by the in-container heartbeat daemon. Effective cap = `min(this, affordable minutes)`. Does not affect desktop sessions. |
| `AXGT_SESSION_COOLDOWN_SECONDS` | `0` | Seconds before same wallet can reclaim after release. |
| `AXGT_SESSION_PAUSED_MAX_MINUTES` | `120` | Max time a credit-paused session may sit before expiry. |
| `AXGT_SESSION_RESET_SCRIPT` | `/usr/local/bin/reset_session.sh` | Script run between users (desktop cleanup). |

### Desktop mode (container runtime)

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_DESKTOP_ENABLED` | `true` (session); `false` (compose base) | When `false`, supervisord skips Xorg, XFCE, VNC, and local theme setup — gate-only container. Set automatically by [`startup.sh`](../startup.sh) based on `AXGT_SESSION_ID` / `AXGT_USER_CONTAINER_ENABLED`. |
| `AXONOS_DESKTOP_USER` | `aXonian` | User targeted by [`scripts/reset_session.sh`](../scripts/reset_session.sh). |

### Runtime-injected session identity

Set by session launcher on `axgt-session-*` containers (not operator `.env`):

| Variable | Set by | Description |
|----------|--------|-------------|
| `AXGT_SESSION_ID` | Launcher | Numeric session ID; triggers desktop + WebRTC agent in `startup.sh`. |
| `AXGT_WALLET_ADDRESS` | Launcher | Claiming wallet (lowercase). |
| `AXGT_REQUESTED_PROFILE` | Launcher | GPU profile name. |
| `AXGT_ASSIGNED_GPU_IDS` | Launcher | Comma-separated GPU indices assigned to this session. |
| `AXGT_SESSION_FILES_KEY` | Launcher | Per-session bearer secret for the in-container file-transfer agent (generated by the gate at claim time). |
| `AXGT_GATE_HEARTBEAT_URL` | Launcher | Gate base URL the in-container heartbeat daemon posts to (default `http://127.0.0.1:8889`). |
| `AXGT_HEARTBEAT_INTERVAL_SECONDS` | Launcher | Heartbeat post interval for headless/SSH sessions (default `30`). |

---

## Session launcher and per-user containers

### Gate-side launcher client

[`axonos_gate/session_launcher.py`](../axonos_gate/session_launcher.py) — used when `AXGT_USER_CONTAINER_ENABLED=true`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SESSION_LAUNCHER_MODE` | `docker_cli` | `docker_cli` \| `http` \| `noop`. Compose default: `http`. |
| `AXGT_SESSION_LAUNCHER_URL` | *(none)* | Base URL for HTTP mode, e.g. `http://axonos-launcher:8090`. |
| `AXGT_SESSION_LAUNCHER_TOKEN` | *(none)* | Shared bearer token for launcher API. Compose default: `change-me-launcher-token`. |
| `AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS` | `10` | HTTP launch/stop timeout. |
| `AXGT_SESSION_LAUNCHER_ENUMERATE_TIMEOUT_SECONDS` | `90` | Timeout for `GET /enumerate-gpus`. |
| `AXGT_SESSION_CONTAINER_IMAGE` | *(none)* | Image for `docker_cli` mode. |
| `AXGT_SESSION_CONTAINER_COMMAND` | *(none)* | Command after image name (e.g. `/startup.sh`). |
| `AXGT_SESSION_CONTAINER_EXTRA_ARGS` | *(none)* | Extra `docker run` args; `--gpus` stripped (injected separately). |

### Host launcher service

[`axonos_gate/session_launcher_service.py`](../axonos_gate/session_launcher_service.py) — `axonos-launcher` compose service.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SESSION_LAUNCHER_BIND_HOST` | `127.0.0.1` | Listen address (`0.0.0.0` in compose). |
| `AXGT_SESSION_LAUNCHER_BIND_PORT` | `8090` | Listen port. |
| `AXGT_SESSION_LAUNCHER_TOKEN` | *(see above)* | Bearer auth for `/launch`, `/stop`, `/enumerate-gpus`. Empty = no auth (dev only). |
| `AXGT_HOST_SESSION_CONTAINER_IMAGE` | *(none)* | Image for session desktops. Compose: `axonos:public-beta`. |
| `AXGT_HOST_SESSION_CONTAINER_COMMAND` | `/startup.sh` | Container entry command tokens. |
| `AXGT_HOST_SESSION_CONTAINER_EXTRA_ARGS` | *(empty)* | Extra `docker run` flags (no duplicate `--gpus`). |
| `AXGT_HOST_SESSION_CONTAINER_SHM_SIZE` | `32g` | `--shm-size` for session containers. Empty string omits flag. |
| `AXGT_HOST_SESSION_CONTAINER_NETWORK` | `axonos_stack` | Docker network for session containers. |
| `AXGT_LAUNCHER_GPU_ENUMERATE_IMAGE` | *(falls back to host session image)* | Image for one-shot `nvidia-smi` GPU enumeration. |
| `AXGT_HOST_SESSION_ENV_PASSTHROUGH` | *(see compose)* | Comma-separated env names copied from launcher into session containers. |

**Default passthrough (compose):**  
`AXGT_CHAIN_ID`, `AXGT_RPC_URL`, `AXGT_CONTRACT_ADDRESS`, `AXGT_REVENUE_WALLET`, `AXGT_CHALLENGE_DB_URL`, `AXGT_MIN_DEPOSIT`, `AXGT_CREDIT_PER_100_AXGT_MINUTES`, `ETH_MIN_DEPOSIT`, `ETH_CREDIT_PER_ETH_MINUTES`, `AXGT_ENABLE_ETH_DEPOSITS`, `WEBRTC_ENABLED`, `WEBRTC_AGENT_ENABLED`, `WEBRTC_AGENT_INTERNAL_KEY`, `WEBRTC_DISPLAY_WAIT_SECONDS`, `WEBRTC_ANSWER_WAIT_MS`, `WEBRTC_STUN_URLS`, `WEBRTC_TURN_URLS`, `WEBRTC_TURN_USERNAME`, `WEBRTC_TURN_CREDENTIAL`, `WEBRTC_FALLBACK_ENABLED`

**Note:** Launcher always forces `AXGT_DESKTOP_ENABLED=true` and `WEBRTC_AGENT_ENABLED=true` on session containers. Do not add `AXGT_DESKTOP_ENABLED` to passthrough.

**Nested Docker:** Launcher strips `NVIDIA_VISIBLE_DEVICES`, `NVDOCKER_VISIBLE_DEVICES`, and `CUDA_VISIBLE_DEVICES` from its environment before calling `docker run` to avoid conflicting GPU requests ([`docker_gpu_cli.py`](../axonos_gate/docker_gpu_cli.py)).

---

## Gate server and noVNC / websockify

### Gate API

[`axonos_gate/gate_server.py`](../axonos_gate/gate_server.py)

| Variable | Default | Description |
|----------|---------|-------------|
| `GATE_HOST` | `127.0.0.1` | Bind address. Compose: `0.0.0.0` so session agents reach `http://axonos:8889`. |
| `GATE_PORT` | `8889` | Gate HTTP/WebSocket API port. |
| `GATE_USE_GEVENT` | `1` | Use gevent WSGI server when truthy. |

### Websockify / noVNC proxy

[`axonos_gate/websockify_gate.py`](../axonos_gate/websockify_gate.py)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBSOCKIFY_HOST` | `0.0.0.0` | Websockify listen host. |
| `WEBSOCKIFY_PORT` | `6080` | Websockify listen port (noVNC UI). |
| `VNC_HOST` | `localhost` | Upstream VNC host. |
| `VNC_PORT` | `5901` | Upstream VNC port (x11vnc). |
| `NOVNC_WEB_DIR` | `/usr/share/novnc` | Static noVNC assets path. |

---

## WebRTC streaming

Configuration: [`axonos_gate/webrtc/config.py`](../axonos_gate/webrtc/config.py). Agent: [`axonos_gate/webrtc_agent_main.py`](../axonos_gate/webrtc_agent_main.py). Supervisord starts the agent when `WEBRTC_ENABLED` and `WEBRTC_AGENT_ENABLED` are truthy.

### Feature flags

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_ENABLED` | `false` (compose) | Prefer WebRTC over noVNC in UI. |
| `WEBRTC_FALLBACK_ENABLED` | `true` | Allow fallback to noVNC when WebRTC fails. |
| `WEBRTC_AGENT_ENABLED` | `true` | Run capture agent in this container. Base `axonos` with user containers: forced `false`; session containers: forced `true`. |

### Signaling and ICE

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_AGENT_INTERNAL_KEY` | *(none)* | Shared secret between gate and in-container agent (**required** for WebRTC). |
| `WEBRTC_GATE_INTERNAL_URL` | `http://127.0.0.1:8889` | Gate base URL for agent polling. Session containers use localhost; only set on base service if agent runs there. |
| `WEBRTC_STUN_URLS` | *(empty → Google STUN)* | Comma-separated `stun:…` URLs. |
| `WEBRTC_TURN_URLS` | *(none)* | Comma-separated `turn:` / `turns:` URLs. |
| `WEBRTC_TURN_USERNAME` | *(none)* | TURN long-term username. |
| `WEBRTC_TURN_CREDENTIAL` | *(none)* | TURN password (never log). |
| `WEBRTC_PORT_RANGE` | *(none)* | Pin the agent's UDP media ports to a range, e.g. `40000-41000`, to match host firewall/NAT rules. Empty = OS-assigned ephemeral ports. |
| `WEBRTC_PUBLIC_IP` | *(none)* | Rewrite the host candidate IP in the SDP to this public/NAT address so srflx works behind 1:1 NAT. Empty = use the locally bound address. |

### Timeouts and limits

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_SESSION_TIMEOUT_SECONDS` | `600` | Signaling session TTL (60–86400). |
| `WEBRTC_MAX_RECONNECT_ATTEMPTS` | `5` | Client reconnect cap (0–50). |
| `WEBRTC_ANSWER_WAIT_MS` | `180000` | Browser polls for SDP answer (90k–300k ms). |
| `WEBRTC_SIGNAL_RATE_LIMIT_PER_MIN` | `60` | Signaling POST rate limit per IP+wallet; `0` = unlimited. |
| `WEBRTC_DISPLAY_WAIT_SECONDS` | `120` | Agent waits for X11 `:0` before capture. |

### Capture and clipboard

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_CAPTURE_DISPLAY` | `:0` | X display to capture. |
| `WEBRTC_CAPTURE_MAX_WIDTH` | `1920` | Scale bound for capture. |
| `WEBRTC_CAPTURE_FPS` | `30` | Target capture frame rate. Use `15` for constrained TURN/mobile paths; watch `packetsLost` in webrtc-internals. |
| `WEBRTC_CAPTURE_BACKEND` | `auto` | `auto` (NvFBC streamer when installed, else NVENC, else MSS), `nvfbc`, `nvenc`, or `mss`. |
| `WEBRTC_CAPTURE_BITRATE` | `12000000` | NVENC H.264 target bitrate (1M–30M bps). 10-14 Mbps is the normal 1080p30 range; lower it when packet loss climbs. |
| `WEBRTC_CAPTURE_LOW_LATENCY` | `true` | When `true`, uses a minimal encoder buffer. Set `false` for a little more quality cushion at the cost of latency. |
| `WEBRTC_CAPTURE_NVENC_PRESET` | `p1` | FFmpeg `h264_nvenc` preset (`p1`–`p7`). `p1` is lowest latency; `p4` is cleaner but can lag. |
| `WEBRTC_CAPTURE_NVENC_TUNE` | `ll` | NVENC tune (`ll`, `ull`, `hq`, `lossless`). Use `ull` only when latency matters more than motion quality. |
| `WEBRTC_CAPTURE_NVFBC_BIN` | `/usr/local/bin/nvfbc_nvenc_streamer` | Native NvFBC→NVENC streamer path. Requires the NVIDIA Capture SDK-built helper. |
| `WEBRTC_CAPTURE_NVFBC_PRESET` | `llhp` | Native streamer preset (`llhp`, `llhq`, `ll`, `hp`, `hq`, `default`). `llhp` is the lowest-latency starting point. |
| `WEBRTC_CAPTURE_MAX_STALE_FRAMES` | `1` | NVENC live track: max extra frames to skip when the send queue runs ahead. `0` disables skip-ahead (may add latency). |
| `WEBRTC_LOCAL_CURSOR` | `auto` | Browser overlay cursor: `auto` (off for H.264 capture, on for MSS), `true`, or `false`. H.264 capture embeds the host cursor. |
| `WEBRTC_AUDIO_ENABLED` | `true` | Attach a desktop audio (Opus) track to WebRTC sessions. Requires the in-container PulseAudio daemon; degrades to video-only with a warning when capture is unavailable. |
| `WEBRTC_AUDIO_SOURCE` | `axonos_out.monitor` | PulseAudio source ffmpeg records (the null sink monitor from `pulse-default.pa`). Change only with a custom Pulse layout. |
| `WEBRTC_MIC_ENABLED` | `false` | Operator gate for browser→desktop microphone. When on, the browser offers a `sendrecv` audio transceiver and shows an opt-in mic toggle (still subject to the browser's `getUserMedia` prompt); the agent feeds the inbound track into the virtual `axonos_microphone` source. Off keeps audio one-directional. |
| `WEBRTC_MIC_SINK` | `axonos_mic` | PulseAudio sink the agent plays the browser mic into (its monitor is remapped to `axonos_microphone`). Change only with a custom Pulse layout. |
| `WEBRTC_CLIPBOARD_MAX_BYTES` | `524288` | Max clipboard payload (floor 4096). |
| `WEBRTC_CLIPBOARD_POLL_PRIMARY` | `false` | Include X PRIMARY selection (noisy); default CLIPBOARD only. |
| `XAUTHORITY` | `/home/aXonian/.Xauthority` | X11 auth file for capture subprocesses. |

Public subset exposed via `GET /api/config` and `GET /api/webrtc/config`.

---

## File transfer (browser ↔ desktop)

Browser ↔ desktop upload/download over `/api/files/*`, proxied by the gate to an in-container agent. Read by [`axonos_gate/file_transfer.py`](../axonos_gate/file_transfer.py) (gate side) and [`axonos_gate/file_agent.py`](../axonos_gate/file_agent.py) (in-container). Per-session auth is automatic — the gate mints a key at claim time and injects `AXGT_SESSION_FILES_KEY` into the session container; no shared secret to configure.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_FILES_ENABLED` | `true` | Enable the file-transfer rail + UI. |
| `AXGT_FILES_PORT` | `8767` | Port the in-container agent listens on. |
| `AXGT_FILES_ROOT` | `/home/aXonian` | Storage root the agent serves (the desktop home / persistent volume). |
| `AXGT_FILES_BIND_HOST` | `0.0.0.0` | Bind address for the in-container agent. |
| `AXGT_FILES_KEY_FILE` | `/tmp/.axgt_files_key` | File the agent reads the per-session key from. |
| `AXGT_FILES_MIN_FREE_BYTES` | `1073741824` | Reject uploads that would leave less than this free (default 1 GiB). |
| `AXGT_FILES_MAX_FILE_BYTES` | `0` | Hard per-file upload cap; `0` = unlimited (storage is billed per GB-hour). |

---

## Direct SSH sessions

Headless GPU sessions reachable only over SSH (no X desktop / WebRTC), launched from the landing-page toggle. The user pastes a public key and the gate returns an `ssh -p <port> <user>@<host>` connect-string. Each session publishes one host TCP port `42000 + (session_id % 50)` → container `:22`, so inbound TCP `42000-42049` must be open on the media-plane host. Read by [`axonos_gate/session_manager.py`](../axonos_gate/session_manager.py).

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SSH_PUBLIC_HOST` | *(empty = SSH toggle disabled)* | Public IP/host the per-session SSH ports NAT to (the **media-plane** IP, not the landing-page hostname). |
| `AXGT_SSH_USER` | `aXonian` | Login user shown in the connect-string (must be the in-container desktop user). |

See also `AXGT_SSH_MAX_SESSION_MINUTES` (hard billing cap, [Session lifecycle](#session-lifecycle)).

---

## IPFS (optional exposure)

Configured at container start in [`startup.sh`](../startup.sh):

| Variable | Default | Description |
|----------|---------|-------------|
| `IPFS_API_BIND` | `0.0.0.0` | IPFS API bind address (`ipfs config Addresses.API`). |
| `IPFS_API_PORT` | `5001` | IPFS API port. |
| `IPFS_GATEWAY_BIND` | `0.0.0.0` | IPFS gateway bind address. |
| `IPFS_GATEWAY_PORT` | `8080` | IPFS gateway port. |

Restrict to `127.0.0.1` unless intentionally exposing IPFS outside the container.

---

## Docker Compose–specific

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPOSE_PROJECT_NAME` | `axonos` (via `name: axonos`) | **Avoid changing** unless you also align `AXGT_HOST_SESSION_CONTAINER_NETWORK`. Default network is fixed `axonos_stack`, not `${COMPOSE_PROJECT_NAME}_default`. |

---

## NVIDIA / GPU runtime (image and host)

Set in [`Dockerfile`](../Dockerfile) — override only when debugging GPU issues:

| Variable | Default | Description |
|----------|---------|-------------|
| `NVIDIA_VISIBLE_DEVICES` | `all` | GPUs visible inside container. Stripped when launcher spawns nested `docker run`. |
| `NVIDIA_DRIVER_CAPABILITIES` | `graphics,utility,compute,display,video` | NVIDIA Container Toolkit capabilities (`video` required for NVENC WebRTC capture). |
| `__GLX_VENDOR_LIBRARY_NAME` | `nvidia` | Force NVIDIA GLX vendor. |
| `LIBGL_DRI3_DISABLE` | `1` | Disable DRI3 (VirtualGL / headless stability). |

### OpenMPI (session containers + image)

Injected by launcher and baked into image:

| Variable | Value | Description |
|----------|-------|-------------|
| `OMPI_MCA_btl` | `vader,self,tcp` | OpenMPI BTL selection (see [`docs/GROMACS.md`](GROMACS.md)). |
| `OMPI_MCA_btl_base_warn_component_unused` | `0` | Suppress unused BTL warnings. |

### VirtualGL / X11 (supervisord + profile)

| Variable | Value | Description |
|----------|-------|-------------|
| `DISPLAY` | `:0` | X display for desktop and capture. |
| `XAUTHORITY` | `/home/aXonian/.Xauthority` | X cookie file. |
| `VGL_DISPLAY` | `:0` | VirtualGL target display. |

---

## Standard Linux / desktop (read-only diagnostics)

Not AxonOS-specific configuration, but read by [`axonos_assistant/mcp_os_server.py`](../axonos_assistant/mcp_os_server.py) for MCP desktop context:

| Variable | Description |
|----------|-------------|
| `DESKTOP_SESSION` | Current desktop session name |
| `DISPLAY` | X11 display |
| `WAYLAND_DISPLAY` | Wayland display |
| `XDG_SESSION_TYPE` | `x11` / `wayland` / … |
| `XDG_CURRENT_DESKTOP` | e.g. `XFCE` |
| `WINDOW_MANAGER` | Window manager identifier |

Image also sets `IPFS_PATH=/home/aXonian/.ipfs` and `GRASS_PYTHON=/usr/bin/python3` in shell profiles (build-time, not operator env).

---

## Deployment topology cheat sheet

```text
┌─────────────────────────────────────────────────────────────┐
│  Host (.env)                                                 │
│  AXGT_* chain/billing  WEBRTC_*  AXONOS_VNC_PASSWORD        │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
       ┌───────▼───────┐              ┌───────▼────────┐
       │ axonos        │              │ axonos-launcher │
       │ gate-only     │  HTTP        │ docker.sock     │
       │ :6080 :8889   │─────────────►│ :8090           │
       └───────┬───────┘              └───────┬────────┘
               │                              │ docker run
               │ postgres                     ▼
               │◄─────────────────── axgt-session-N
               │                      (full desktop + WebRTC)
               └─────────────────── AXGT_CHALLENGE_DB_URL
```

**Single-container legacy:** `AXGT_USER_CONTAINER_ENABLED=false` — base container runs Xorg + VNC + gate together; set `AXGT_DESKTOP_ENABLED=true`.

**Public-beta compose default:** `AXGT_USER_CONTAINER_ENABLED=true`, base is gate-only, desktops in `axgt-session-*`.

---

## Related files

| File | Role |
|------|------|
| [`env.example`](../env.example) | Annotated template for `.env` |
| [`docker-compose.yml`](../docker-compose.yml) | Service wiring and compose defaults |
| [`docs/HOST_LAUNCHER.md`](HOST_LAUNCHER.md) | Session launcher deployment |
| [`docs/WEBRTC.md`](WEBRTC.md) | WebRTC architecture |
| [`docs/TOKENOMICS.md`](TOKENOMICS.md) | Discount tiers and payment rails |
| [`axonos_gate/README.md`](../axonos_gate/README.md) | Gate API and billing overview |

---

*Generated from codebase audit. When adding a new `os.getenv` call, update this document and `env.example`.*
