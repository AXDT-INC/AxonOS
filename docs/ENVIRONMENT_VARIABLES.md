# AxonOS Environment Variables

Complete reference for every environment variable read or propagated by the AxonOS codebase. Use this alongside [`env.example`](../env.example) (copy to `.env` for local/compose deploys).

---

## How configuration works

| Layer | Mechanism | Notes |
|-------|-----------|-------|
| **Operator `.env`** | `env_file: .env` in [`docker-compose.yml`](../docker-compose.yml) | Primary runtime config for gate, launcher, and postgres credentials |
| **Docker Compose overrides** | `environment:` blocks in compose | Injects DB URL, launcher URL, gate bind, WebRTC defaults |
| **Docker build args** | `docker build --build-arg …` / compose `build.args` | Image credentials and NVIDIA userspace pinning |
| **Runtime injection** | Session launcher `docker run -e …` | Per-session identity/capabilities plus a narrow media-tuning passthrough allowlist |
| **Image defaults** | `ENV` in [`Dockerfile`](../Dockerfile), supervisord | NVIDIA, OpenMPI, VirtualGL — usually not overridden |

**Build-time vs run-time:** `AXONOS_VNC_PASSWORD` / `PASSWORD` and `NVIDIA_DRIVER_PKG_VERSION` affect the image at build. Most `AXGT_*` and `WEBRTC_*` vars are read at process start (gate, websockify, launcher, agent).

**Boolean convention:** Truthy values are `1`, `true`, `yes`, `on` (case-insensitive). Falsy for feature flags is often `0`, `false`, `no`, `off`.

**Tenant boundary:** Launcher-managed `axgt-session-*` containers are data-plane
workloads. Database, chain/RPC, payment, launcher, and fleet signing credentials
remain in the control plane. The launcher injects only session-specific identity
and bearer values plus explicitly allowed media settings.

The first deployment of this boundary requires zero active sessions and no
legacy unlabeled `axgt-session-*` containers; see
[`HOST_LAUNCHER.md`](HOST_LAUNCHER.md#first-upgrade-to-isolated-sessions).

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
| `AXONOS_PUBLISH_NOVNC` | `6080` | Host port → central web UI/browser API on container 6080. It proxies VNC only in legacy single-container mode; multi-user tenants have no VNC fallback. |
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
- **Purpose:** Pins `xserver-xorg-video-nvidia`, `libnvidia-gl`, `libnvidia-cfg1`, and `libnvidia-common` to a reproducible apt version matching the host driver branch. At container startup, AxonOS also relinks Xorg GLX to NVIDIA's host-matched runtime module so automatic host patch updates do not leave a stale build-time link.
- **Example:** `535.288.01-0ubuntu1` or triplet `535.288.01`
- **Default:** Empty — build resolves the best available set for major branch `580`.

### `NVIDIA_DRIVER_VERSION`

- **When:** Docker build (`ARG`, default `580`)
- **Purpose:** Major NVIDIA driver branch for package names (e.g. `580` → `libnvidia-gl-580`).

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

### Test credits (non-payment release rail)

Test credit is issued only by authenticated `POST /api/auth/test-credit`; the real
ETH, USDC, and AXGT payment controls always submit on-chain transactions. Grants are
recorded with separate ledger/provenance fields and are disabled unless both the
feature flag and wallet eligibility list permit them.

For an eligible signed-in wallet, the dashboard balance card becomes a one-click
test-credit action and displays the configured grant. The wallet button uses the
additive grant path: every new request adds `AXONOS_TEST_CREDIT_GRANT_MINUTES`
(0→60, 25→85, 60→120). A successful grant reconnects an existing
credit-grace session only; it never silently starts a new compute session.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXONOS_TEST_CREDITS_ENABLED` | `false` | Explicit fail-closed switch for token-free test credit. A wallet list alone never enables it. |
| `AXONOS_TEST_CREDIT_WALLETS` | *(none)* | Comma-separated wallets eligible to request test credit. |
| `AXONOS_TEST_CREDIT_GRANT_MINUTES` | `60` | Minutes requested per grant; finite hard maximum `1440`. |
| `AXONOS_TEST_CREDIT_MAX_BALANCE_MINUTES` | `60` | Balance cap for bounded (non-additive) grant paths such as guest funding; finite hard maximum `10080`. The wallet test-credit button is additive and is **not** capped by this value. |
| `AXONOS_WHITELISTED_WALLETS` | *(none)* | Legacy wallet-list alias only. It does not enable test credits. |

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
| `AXGT_ENABLE_USDC_DEPOSITS` | `true` | Enable the USDC rail + UI. Unset or `1/true/yes/on` enables verification; any other value disables it. `/api/config` uses the inverse falsy list (`0/false/no/off`), so keep to canonical boolean values. |
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
| `AXGT_PUBLIC_BASE_URL` | *(none)* | Exact public origin (e.g. `https://app.axonos.io`) used for absolute x402 resource URLs and as the sole accepted terminal ticket/WebSocket browser Origin. For that exact Origin, a syntactically valid internal proxy `Host` is allowed and `X-Forwarded-Proto` may describe the internal hop. Without this setting, Origin, Host, and any supplied forwarded scheme must agree exactly. |
| `AXONOS_PUBLIC_ORIGIN` | *(none)* | Public origin advertised in gate responses (trailing slash stripped). |
| `AXGT_CONTACT_EMAIL` | *(none)* | Merchant contact included in x402 payment requirements; omitted when unset. |
| `X402_ACCESS_RESOURCE_URL` | *(none)* | Explicit AgentLink resource URI for the access endpoint; defaults to `<base_url>/api/x402/access`. |

### x402 facilitator, Bazaar discovery, and AgentLink

Optional Coinbase CDP facilitator settlement and directory listing. Read by [`axonos_gate/x402_facilitator.py`](../axonos_gate/x402_facilitator.py) and [`axonos_gate/agentlink_verifier.py`](../axonos_gate/agentlink_verifier.py). Off by default; the gate self-settles when disabled.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_X402_FACILITATOR_ENABLED` | `false` | Route settlement through the CDP facilitator instead of the local `X402_SETTLEMENT_PRIVATE_KEY` hot wallet. |
| `CDP_FACILITATOR_URL` | `https://api.cdp.coinbase.com/platform/v2/x402` | Facilitator API base URL. |
| `CDP_API_KEY_ID` | *(none)* | CDP API key ID (control plane only; never log). |
| `CDP_API_KEY_SECRET` | *(none)* | CDP API key secret (control plane only; never log). |
| `AXGT_X402_BAZAAR_DISCOVERABLE` | inherits `AXGT_X402_FACILITATOR_ENABLED` | Advertise the resource in the x402 Bazaar directory. |
| `AXGT_X402_BAZAAR_CATEGORY` | `compute` | Bazaar listing category. |
| `AXGT_X402_BAZAAR_TAGS` | `gpu,compute,ssh,linux` | Comma-separated Bazaar tags. An explicitly empty value clears the list. |
| `AXGT_X402_DEBUG` | `false` | Verbose facilitator request/response logging. |
| `AXGT_AGENTLINK_ENABLED` | `false` | Enable AgentLink resource advertisement/verification. |
| `AXGT_AGENTLINK_MODE` | `advertise_only` | `advertise_only` or `verify_only`. |
| `AXGT_AGENTLINK_RPC_URL` | *(falls back to `USDC_RPC_URL`)* | RPC used for AgentLink registry reads. |
| `AXGT_AGENTLINK_REGISTRY_ADDRESS` | *(none)* | AgentLink registry contract. |
| `AXGT_AGENTLINK_MAX_AGE_SECONDS` | `300` | Maximum age of an AgentLink attestation. |

When neither `X402_RESOURCE_URL` nor `AXGT_PUBLIC_BASE_URL` is set, the AgentLink resource falls back to a hard-coded `https://app.axonos.io`; set one of them for self-hosted deployments.

### Dynamic USD-equivalent pricing (price oracle)

When enabled, ETH and AXGT deposits credit at their **live USD value**, cached in Postgres; USDC stays fixed at $1. AXGT uses a 30-minute TWAP from the same Uniswap v3 AXGT/WETH pool and Chainlink ETH/USD sources as the axondao.io dashboard. If the pool lacks TWAP observations, its dashboard-compatible spot quote is accepted only when CoinGecko agrees within the configured deviation limit. A large TWAP change requires the same independent confirmation; CoinGecko is otherwise an outage fallback. On a feed outage the last-known price is used up to `PRICE_MAX_STALE_SECONDS`, after which the fixed crypto rates apply. Read by [`axonos_gate/price_oracle.py`](../axonos_gate/price_oracle.py).

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_DYNAMIC_PRICING` | `false` | Enable live USD-equivalent crediting (introduces a server-side price oracle). |
| `AXGT_USD_PER_HOUR` | `1.0` | USD price of one hour of `small` (1-GPU) desktop time → minutes per USD. |
| `ETHEREUM_RPC_URL` | `https://ethereum-rpc.publicnode.com` | Ethereum mainnet RPC used for the Uniswap and Chainlink price reads. Falls back to `AXGT_RPC_URL` when set. |
| `AXGT_UNISWAP_V3_POOL` | `0xf9c56A9CcC1398Bed3C519ef2F0B42CE52AaA440` | AXGT/WETH Uniswap v3 pool used by the dashboard price path. |
| `AXGT_TWAP_WINDOW_SECONDS` | `1800` | Uniswap observation window used for billing; minimum 60 seconds. |
| `AXGT_MAX_PRICE_DEVIATION_PERCENT` | `25` | A TWAP move this far from the cached price must also agree with CoinGecko within this percentage. |
| `AXGT_COINGECKO_ID` | `axondao-governance-token-2` | CoinGecko coin ID used only if the on-chain AXGT quote fails. |
| `PRICE_POLL_INTERVAL_SECONDS` | `300` | Price refresh cadence (5 minutes, matching the dashboard). |
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
| `AXGT_MULTI_SESSION_ENABLED` | `true` when user containers are enabled | Multi-session scheduler (exclusive GPU allocation). Forced off when `AXGT_USER_CONTAINER_ENABLED=false` so multiple wallets can never share one desktop. |
| `AXGT_GPU_PROFILES_ENABLED` | `true` | GPU profile selection (`small`/`medium`/`large`/`max`). |
| `AXONOS_HIDE_BETA_BADGE` | `true` | Hide the release-stage chip beside the AxonOS brand in the UI (served to the client via `/api/config`). Set `false` to show the chip. The `:8889` gate accepts only `1/true/yes/on` as "hide"; the `:6080` listener hides unless `0/false/no/off` — use canonical values. |
| `AXGT_GPU_WEIGHTED_BILLING` | `true`* | Bill `wall_clock_minutes × GPU count` on heartbeat when profiles enabled. Set `false` for 1× billing. (*Enabled when profiles enabled and var not explicitly off.) |
| `AXGT_USER_CONTAINER_ENABLED` | `false` in code; `true` in compose | One container per claimed session vs shared desktop. |
| `AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST` | `true` | On zero credit, stop viewer access and compute billing but retain the same running container, jobs, and GPU assignment for the configured top-up grace. When `false`, end the session immediately. Demo/guest identities are never preserved and end at their hard cap. |

### GPU pool

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_DEFAULT_GPU_PROFILE` | `small` | Default profile if client omits one (`small`=1, `medium`=2, `large`=4, `max`=8 GPUs). |
| `AXGT_GPU_DEVICE_IDS` | *(auto)* | Comma-separated GPU indices, e.g. `0,1,2,3`. Overrides auto-detect. |
| `AXGT_GPU_TOTAL_COUNT` | *(auto)* | Alternative to explicit IDs: use GPUs `0 .. N-1`. |
| `AXGT_GPU_AUTO_DETECT` | `true` | Run `nvidia-smi` locally; if empty, try launcher `GET /enumerate-gpus`. |
| `AXGT_GPU_DEVICE_CACHE_SECONDS` | `120` | TTL for cached auto-detected GPU list. |
| `AXGT_GPU_TELEMETRY_INTERVAL_SECONDS` | `1.0` | Central persistent-NVML sampling interval in seconds (clamped to 0.5–60). One sampler covers every GPU and every viewer. |
| `AXGT_GPU_TELEMETRY_FILE` | `/run/axonos/gpu-telemetry.json` | Root-owned atomic GPU snapshot read by forked gate workers. The Compose base uses NVIDIA `utility` access only; GPU device access remains shareable with tenant sessions. |
| `AXGT_GPU_ENUMERATE_VIA_LAUNCHER` | `true` | When gate has no GPUs, probe host via HTTP launcher. Only used when `AXGT_SESSION_LAUNCHER_MODE=http`. |
| `AXGT_GPU_TELEMETRY_LOG_LEVEL` | `INFO` | Log level of the central GPU telemetry sampler process. |

### Session lifecycle

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SESSION_MAX_MINUTES` | `60` | Sliding runtime lease — extended by healthy container/browser heartbeats; session ends when the lease or stale-heartbeat timeout is exceeded. |
| `AXGT_HEARTBEAT_TIMEOUT_SECONDS` | `120` | No heartbeat → session considered stale and released. |
| `AXGT_SSH_MAX_SESSION_MINUTES` | *(unset = affordability only)* | Hard, **non-sliding** billing ceiling (minutes) for headless/SSH sessions kept alive by the in-container heartbeat daemon. Effective cap = `min(this, affordable minutes)`. Does not affect desktop sessions. |
| `AXGT_SESSION_COOLDOWN_SECONDS` | `0` | Seconds before same wallet can reclaim after release. |
| `AXGT_SESSION_CREDIT_GRACE_MINUTES` | `120` | Top-up grace after credit exhaustion. The container and jobs keep running, compute billing/viewer access remain stopped, and cleanup stops the container when this grace expires. |
| `AXGT_SESSION_PAUSED_MAX_MINUTES` | *(unset)* | Legacy fallback for `AXGT_SESSION_CREDIT_GRACE_MINUTES`; ignored when the canonical variable is set. |
| `AXGT_SESSION_GRACE_SECONDS` | `60` | Grace window applied to lease/heartbeat expiry checks before a session is released (accepts `>= 0`). Also read by both launchers when reconciling session networks. |
| `AXGT_GATE_LIVENESS_INTERVAL_SECONDS` | `15` | How often the gate stamps its own liveness row (must be `> 0`). Measured control-plane downtime is credited back to live sessions so they survive gate restarts/redeploys. |
| `AXGT_SESSION_RESET_SCRIPT` | `/usr/local/bin/reset_session.sh` | Script run between users (desktop cleanup). Ignored when the file does not exist on disk. |

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
| `AXGT_WEBRTC_AGENT_TOKEN` | Gate via launcher | Signed bearer capability bound to this session ID, wallet, and file-key fingerprint. Generated automatically; never configure or forward it globally. |
| `WEBRTC_GATE_INTERNAL_URL` | Launcher | Internal-only agent API. Launcher-managed sessions use `http://axonos-gate:8890`; legacy single-container mode uses loopback. |
| `AXGT_GATE_HEARTBEAT_URL` | Launcher | Central gate URL the heartbeat daemon posts to. Launcher-managed sessions use `http://axonos-gate:8889`. |
| `AXGT_HEARTBEAT_INTERVAL_SECONDS` | Launcher (only when set on the launcher) | Durable in-container heartbeat interval for every tenant session, including desktops. Injected only if the launcher process itself has it set; otherwise the in-container daemon's own default of `30` applies. Keep it below `AXGT_HEARTBEAT_TIMEOUT_SECONDS`; this keeps detached compute alive and billed independently of the browser. |
| `AXGT_DESKTOP_ENABLED` | Launcher | `true` for desktop sessions, `false` for SSH-only sessions. Selected by session mode; cannot be overridden via passthrough. |
| `WEBRTC_ENABLED` / `WEBRTC_AGENT_ENABLED` | Launcher | `true` for desktop sessions; `WEBRTC_AGENT_ENABLED=false` for SSH-only sessions. Selected by session mode; cannot be overridden via passthrough. |
| `WEBRTC_PORT_RANGE` | Launcher | Per-session UDP media port slice derived from the session ID (desktop sessions). |
| `AXGT_SSH_ENABLED` | Launcher | `true` on SSH-only sessions; enables `sshd` in supervisord and disables the desktop stack. |
| `AXGT_SSH_PUBKEY` | Launcher | The user-supplied SSH public key (validated by the gate) written to `authorized_keys` by `startup.sh`. |
| `AXONOS_SELECTED_TEMPLATE` | Launcher | Selected science template; `startup.sh` persists it to `~/.config/axonos/selected_template` and [`scripts/apply_session_template.sh`](../scripts/apply_session_template.sh) applies it (override the file path with `AXONOS_TEMPLATE_FILE`). |

---

## Session launcher and per-user containers

### Gate-side launcher client

[`axonos_gate/session_launcher.py`](../axonos_gate/session_launcher.py) — used when `AXGT_USER_CONTAINER_ENABLED=true`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SESSION_LAUNCHER_MODE` | `docker_cli` | `docker_cli` \| `http` \| `noop`. Compose default: `http`. |
| `AXGT_SESSION_LAUNCHER_URL` | *(none)* | Base URL for HTTP mode, e.g. `http://axonos-launcher:8090`. |
| `AXGT_SESSION_LAUNCHER_TOKEN` | *(none)* | Shared bearer token for launcher API. Compose default: `change-me-launcher-token`. |
| `AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS` | `90` | HTTP launch/stop timeout. After an inconclusive timeout, the gate retries the identical idempotent launch request so only the host launcher's exact identity/digest/network contract can confirm success. |
| `AXGT_SESSION_LAUNCH_VERIFY_ATTEMPTS` | `5` | Maximum idempotent launch-contract checks after an inconclusive launcher response. |
| `AXGT_SESSION_LAUNCH_VERIFY_INTERVAL_SECONDS` | `2` | Delay between those verification attempts. |
| `AXGT_SESSION_LAUNCHER_ENUMERATE_TIMEOUT_SECONDS` | `90` | Timeout for `GET /enumerate-gpus`. |
| `AXGT_SESSION_CONTAINER_IMAGE` | *(none)* | Image for `docker_cli` mode. |
| `AXGT_SESSION_CONTAINER_COMMAND` | *(none)* | Command after image name (e.g. `/startup.sh`). |
| `AXGT_SESSION_NETWORK_ISOLATION` | `true` | `docker_cli` mode: create `axgt-session-net-<id>` and attach only the central gate plus that tenant. |
| `AXGT_CENTRAL_GATE_CONTAINER` | `axonos` | Central container attached to each isolated `docker_cli` session network. |
| `AXGT_SESSION_CONTAINER_NETWORK` | *(empty)* | Compatibility network used—and required—when `AXGT_SESSION_NETWORK_ISOLATION=false`; an empty default fails closed because the tenant could not resolve the central gate. |
| `AXGT_SESSION_CONTAINER_EXTRA_ARGS` | *(none)* | Restricted extra `docker run` args. Network, ports, mounts/devices, namespaces, capabilities, runtime/security settings, environment injection, and privileged mode are stripped. |

`GET /api/config` exposes `session_claim_timeout_seconds`, derived from the
launcher timeout plus its bounded verification envelope (with a 150-second
minimum). Browser clients use it for fresh launches; exact retained-session
resume requests keep a separate short deadline because they never spawn a new
container.

### Host launcher service

[`axonos_gate/session_launcher_service.py`](../axonos_gate/session_launcher_service.py) — `axonos-launcher` compose service.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_SESSION_LAUNCHER_BIND_HOST` | `127.0.0.1` | Listen address (`0.0.0.0` in compose). |
| `AXGT_SESSION_LAUNCHER_BIND_PORT` | `8090` | Listen port. |
| `AXGT_SESSION_LAUNCHER_TOKEN` | *(see above)* | Bearer auth for `/launch`, `/stop`, `/enumerate-gpus`. Empty = no auth (dev only). |
| `AXGT_HOST_SESSION_CONTAINER_IMAGE` | *(none)* | Image for session desktops. Compose: the image tag set in `docker-compose.yml`. |
| `AXGT_HOST_SESSION_CONTAINER_COMMAND` | *(empty; image `CMD`)* | Container entry command tokens. Compose sets `/startup.sh`. |
| `AXGT_HOST_SESSION_CONTAINER_EXTRA_ARGS` | *(empty)* | Restricted extra `docker run` flags. Network, ports, mounts/devices, namespaces, capabilities, runtime/security settings, environment injection, conflicting GPUs, and privileged mode are stripped. |
| `AXGT_HOST_SESSION_CONTAINER_SHM_SIZE` | `32g` | `--shm-size` for session containers. Empty string omits flag. |
| `AXGT_HOST_SESSION_NETWORK_ISOLATION` | `true` | Create a labeled bridge `axgt-session-net-<id>` for each tenant and remove it on stop. |
| `AXGT_HOST_CENTRAL_GATE_CONTAINER` | `axonos` | Central gate attached to each isolated tenant network. |
| `AXGT_HOST_SESSION_CONTAINER_NETWORK` | `axonos_stack` (compose) | Shared compatibility network used—and required—when per-session isolation is disabled. |
| `AXGT_LAUNCHER_GPU_ENUMERATE_IMAGE` | *(falls back to host session image)* | Image for one-shot `nvidia-smi` GPU enumeration. |
| `AXGT_HOST_SESSION_ENV_PASSTHROUGH` | *(see compose)* | Comma-separated media-tuning names copied from launcher into session containers. Control-plane and launcher-injected names are rejected. |
| `AXGT_CHALLENGE_DB_URL` | *(required)* | Control-plane DB used to authorize the exact live allocation before every host-launcher spawn and to reconcile only live session networks. Never forwarded to tenants. Compose supplies it automatically. |
| `AXGT_SESSION_DB_CONNECT_TIMEOUT_SECONDS` | `5` | Launcher/reconciler Postgres connect timeout (1–30 seconds). |
| `AXGT_SESSION_NETWORK_RECONCILE_SECONDS` | `10` | How often the host launcher reattaches a recreated central gate and removes ended-session networks (minimum 5 seconds). |

**Default media passthrough (compose):**

`WEBRTC_DISPLAY_WAIT_SECONDS`, `WEBRTC_STUN_URLS`, `WEBRTC_TURN_URLS`, `WEBRTC_TURN_USERNAME`,
`WEBRTC_TURN_CREDENTIAL`, capture backend/rate/size/preset tuning,
`WEBRTC_CLIPBOARD_MAX_BYTES`, `WEBRTC_CLIPBOARD_POLL_PRIMARY`,
`WEBRTC_PUBLIC_IP`, and WebRTC audio/microphone
settings. See [`docker-compose.yml`](../docker-compose.yml) for the exact list.

The launcher explicitly injects `AXGT_SESSION_ID`, wallet, assigned GPUs,
per-session file key, signed `AXGT_WEBRTC_AGENT_TOKEN`, and central gate URLs.
Do not add those names to passthrough. Never add database, chain/RPC, billing,
payment/settlement, launcher bearer, or `WEBRTC_AGENT_INTERNAL_KEY` values.
Names outside the built-in media allowlist are rejected even if an operator adds
them. Desktop and agent enablement are selected by the session mode, so
`AXGT_DESKTOP_ENABLED`, `WEBRTC_ENABLED`, and `WEBRTC_AGENT_ENABLED` cannot
override that decision.

With isolation enabled, only the central gate and one tenant join each dynamic
session bridge. Postgres and the launcher remain on `axonos_control`. Every tenant
also drops `NET_RAW`. Disabling isolation selects the shared compatibility network
and weakens the tenant boundary.

**Nested Docker:** Launcher strips `NVIDIA_VISIBLE_DEVICES`, `NVDOCKER_VISIBLE_DEVICES`, and `CUDA_VISIBLE_DEVICES` from its environment before calling `docker run` to avoid conflicting GPU requests ([`docker_gpu_cli.py`](../axonos_gate/docker_gpu_cli.py)).

---

## Gate server and noVNC / websockify

### Gate API

[`axonos_gate/gate_server.py`](../axonos_gate/gate_server.py)

| Variable | Default | Description |
|----------|---------|-------------|
| `GATE_HOST` | `127.0.0.1` | Bind address. Compose: `0.0.0.0` so browsers and session heartbeats reach the central gate. |
| `GATE_PORT` | `8889` | Gate HTTP/WebSocket API port. |
| `GATE_USE_GEVENT` | `1` | Use gevent WSGI server when truthy (`1`, `true`, `yes` — `on` is not accepted here). |
| `GATE_AGENT_API_ENABLED` | `false` | Enables WebRTC agent-only endpoints. Set only on the supervisor-managed internal `:8890` listener. |
| `GATE_AGENT_ONLY` | `false` | Reject every non-agent path on that listener. Set with `GATE_AGENT_API_ENABLED`; do not enable on the public gate. |

### Websockify / noVNC proxy

[`axonos_gate/websockify_gate.py`](../axonos_gate/websockify_gate.py)

In multi-user mode this central listener serves the browser shell and browser
APIs, but it cannot proxy a tenant desktop over VNC: tenant `x11vnc` is disabled
and `WEBRTC_FALLBACK_ENABLED` is forced off. The VNC target settings below are
for legacy single-container mode.

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
| `WEBRTC_ENABLED` | `false` (compose) | Enable the WebRTC desktop path. Launcher-managed desktops have no VNC fallback; legacy mode prefers WebRTC over noVNC. |
| `WEBRTC_FALLBACK_ENABLED` | `true` (legacy only) | Allow fallback to noVNC when WebRTC fails. Forced off when `AXGT_USER_CONTAINER_ENABLED` is truthy because tenant sessions intentionally have no VNC listener. |
| `WEBRTC_AGENT_ENABLED` | `true` | Run capture agent in this container. Base `axonos` with user containers: forced `false`; session containers: forced `true`. |

### Signaling and ICE

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_AGENT_INTERNAL_KEY` | *(none)* | Central WebRTC capability signing secret (**required** for WebRTC). Launcher-managed tenants never receive it. Legacy single-container mode also uses it directly over loopback. |
| `WEBRTC_AGENT_CAPABILITY_TTL_SECONDS` | `86400` | Lifetime of each signed per-session agent capability; clamped to 600–604800 seconds. The tenant renews before expiry through the internal listener while preserving the revocable session JTI. Its newest renewal is stored in agent-root-owned mode `0600` at `/run/axonos/webrtc-agent-token` so a Supervisor agent-process restart can reload it; the central gate still validates every request. |
| `AXGT_WEBRTC_AGENT_TOKEN` | *(runtime-injected)* | Signed per-session bearer capability. The gate mints it and the launcher injects it; operators must not configure or passthrough it. |
| `WEBRTC_GATE_INTERNAL_URL` | agent: `http://axonos-gate:8890`; gate helper: `http://127.0.0.1:8890` | Agent-only gate URL. The capture agent defaults to the compose service alias and the launcher injects that same value; compose pins the base `axonos` container to loopback. The internal listener is not host-published. |
| `WEBRTC_STUN_URLS` | *(empty → Google STUN, unless TURN URLs are configured)* | Comma-separated `stun:…` URLs. |
| `WEBRTC_TURN_URLS` | *(none)* | Comma-separated `turn:` / `turns:` URLs. In multi-user mode use a hostname/IP reachable from isolated session networks, not the compose-only `coturn` alias. |
| `WEBRTC_TURN_USERNAME` | *(none)* | TURN long-term username. |
| `WEBRTC_TURN_CREDENTIAL` | *(none)* | TURN password (never log). |
| `WEBRTC_PORT_RANGE` | *(none)* | Pin the agent's UDP media ports to a range, e.g. `40000-41000`, to match host firewall/NAT rules. Empty = OS-assigned ephemeral ports. |
| `WEBRTC_PUBLIC_IP` | *(none)* | Rewrite the host candidate IP in the SDP to this public/NAT address so srflx works behind 1:1 NAT. Empty = the agent auto-detects its public IP through outbound HTTPS lookups (ipify, ifconfig.me, ipinfo, icanhazip; cached). Set explicitly on egress-restricted hosts. |

### Timeouts and limits

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_SESSION_TIMEOUT_SECONDS` | `600` | Signaling session TTL (60–86400). |
| `WEBRTC_MAX_RECONNECT_ATTEMPTS` | `5` | Client reconnect cap (0–50). |
| `WEBRTC_ANSWER_WAIT_MS` | `180000` | Browser polls for SDP answer (90k–300k ms). |
| `WEBRTC_AGENT_CLAIM_LEASE_SECONDS` | derived | Reclaims an abandoned scoped offer after 30–540 seconds; default is the larger of answer/display wait plus 30 seconds. |
| `WEBRTC_SIGNAL_RATE_LIMIT_PER_MIN` | `60` | Signaling POST rate limit per IP+wallet; `0` (or negative) = unlimited; other values are clamped to 5–600. |
| `WEBRTC_DISPLAY_WAIT_SECONDS` | `120` | Agent waits for X11 `:0` before capture (clamped 5–300). |
| `WEBRTC_DESKTOP_READY_FILE` | `/tmp/axonos-desktop-ready` | Marker file the agent waits for before starting capture so the branded XFCE pass has finished. Enforced only when `AXGT_SESSION_ID` is set. |

The public gate on `:8889` and websockify/noVNC on `:6080` do not accept agent
poll/answer/ICE operations. Those routes are served only by the central internal
listener on `:8890`, reached through the owning session's isolated network. Every
agent operation is checked against the signed capability and exact active compute
session before scoped signaling SQL is run.

### Capture and clipboard

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBRTC_CAPTURE_DISPLAY` | `:0` | X display to capture. |
| `WEBRTC_CAPTURE_MAX_WIDTH` | `1920` | Scale bound for capture (clamped 320–3840). |
| `WEBRTC_CAPTURE_FPS` | `30` | Target capture frame rate (not clamped — passed to the encoder as given). Use `15` for constrained TURN/mobile paths; watch `packetsLost` in webrtc-internals. |
| `WEBRTC_CAPTURE_BACKEND` | `auto` | `auto` (NvFBC streamer when installed, else NVENC, else MSS), `nvfbc`, `nvenc`, or `mss`. |
| `WEBRTC_CAPTURE_BITRATE` | `12000000` | NVENC H.264 target bitrate (1M–30M bps). 10-14 Mbps is the normal 1080p30 range; lower it when packet loss climbs. |
| `WEBRTC_CAPTURE_LOW_LATENCY` | `true` | When `true`, uses a minimal encoder buffer. Set `false` for a little more quality cushion at the cost of latency. |
| `WEBRTC_CAPTURE_NVENC_PRESET` | `p1` | FFmpeg `h264_nvenc` preset (`p1`–`p7`). `p1` is lowest latency; `p4` is cleaner but can lag. |
| `WEBRTC_CAPTURE_NVENC_TUNE` | `ll` | NVENC tune (`ll`, `ull`, `hq`, `lossless`). Use `ull` only when latency matters more than motion quality. |
| `WEBRTC_CAPTURE_NVFBC_BIN` | `/usr/local/bin/nvfbc_nvenc_streamer` | Native NvFBC→NVENC streamer path. Requires the NVIDIA Capture SDK-built helper. |
| `WEBRTC_CAPTURE_NVFBC_PRESET` | derived (`llhp`) | Native streamer preset (`llhp`, `llhq`, `ll`, `hp`, `hq`, `default`). When unset it is derived from `WEBRTC_CAPTURE_NVENC_PRESET`: `p1`/`p2`/`hp` → `llhp` (the lowest-latency starting point), anything else → `llhq`. |
| `WEBRTC_CAPTURE_MAX_STALE_FRAMES` | `1` | NVENC live track: max extra frames to skip when the send queue runs ahead (0–60). `0` disables skip-ahead (may add latency). |
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

## File transfer and session telemetry (browser ↔ session)

Browser upload/download over `/api/files/*` and CPU/RAM/storage telemetry over `/api/files/stats` are proxied by the gate to an in-container agent. The agent runs for desktop and SSH-only sessions; sessions with both surfaces disabled leave it stopped. Read by [`axonos_gate/file_transfer.py`](../axonos_gate/file_transfer.py) (gate side) and [`axonos_gate/file_agent.py`](../axonos_gate/file_agent.py) (in-container). Per-session auth is automatic — the gate mints a key at claim time and injects `AXGT_SESSION_FILES_KEY` into the session container; no shared secret to configure. Its port remains private on the per-session container network and is never host-published.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_FILES_ENABLED` | `true` | Enable the file-transfer rail + UI. |
| `AXGT_FILES_PORT` | `8767` | Port the in-container agent listens on. |
| `AXGT_FILES_ROOT` | `/home/aXonian` | Storage root the agent serves (the session home / persistent volume). |
| `AXGT_FILES_BIND_HOST` | `0.0.0.0` | Bind address for the in-container agent. |
| `AXGT_FILES_KEY_FILE` | `/tmp/.axgt_files_key` | File the agent reads the per-session key from. |
| `AXGT_FILES_MIN_FREE_BYTES` | `1073741824` | Reject uploads that would leave less than this free (default 1 GiB). |
| `AXGT_FILES_MAX_FILE_BYTES` | `0` | Hard per-file upload cap; `0` = unlimited (storage is billed per GB-hour). |
| `AXGT_FILES_PUBLIC_BASE` | *(empty = proxy through the gate)* | Public HTTPS base of the direct TLS file plane (the `files-tls` compose sidecar on `:443`, demuxed from TURN-TCP). When set, bulk uploads/downloads bypass the landing-page proxy. Stays HTTP/1.1 by design. |
| `FILES_TLS_SERVER_NAME` | `axonconsole.io` (compose) | Certificate hostname the `files-tls` sidecar waits for under `/etc/letsencrypt/live/<name>/fullchain.pem` ([`docker/files-tls/40-wait-for-cert.sh`](../docker/files-tls/40-wait-for-cert.sh)). Set to your own domain. |

`AXGT_FILES_MIN_FREE_BYTES` and `AXGT_FILES_MAX_FILE_BYTES` have no compose `environment:` default; they reach the gate only through `.env` (`env_file: .env`).

---

## Web terminal

Browser terminal over `/api/terminal/*`, proxied by the gate to an in-container agent. Read by [`axonos_gate/terminal_gateway.py`](../axonos_gate/terminal_gateway.py) (gate side) and [`axonos_gate/terminal_agent.py`](../axonos_gate/terminal_agent.py) (in-container).

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_TERMINAL_ENABLED` | `true` | Enable the web terminal rail. Falsy (`0/false/no/off`) disables it. |
| `AXGT_TERMINAL_TICKET_TTL_SECONDS` | `30` | One-shot WebSocket ticket lifetime (5–60). |
| `AXGT_TERMINAL_REVALIDATE_SECONDS` | `5` | Session/auth revalidation cadence while connected (1–30). |
| `AXGT_TERMINAL_PRESENCE_SECONDS` | `10` | Presence heartbeat interval (2–30). |
| `AXGT_TERMINAL_CONNECT_TIMEOUT_SECONDS` | `5` | Gate → agent connect timeout (1–15). |
| `AXGT_TERMINAL_MAX_CLIENTS` | `1` | Concurrent terminal clients per session accepted by the agent (1–4). |

---

## Persistent storage (per-wallet volumes)

Loop-backed ext4 volumes mounted over the session home. Read by [`axonos_gate/session_launcher_service.py`](../axonos_gate/session_launcher_service.py), [`axonos_gate/session_launcher.py`](../axonos_gate/session_launcher.py), and exposed via `GET /api/config`. See [`docs/VOLUME_RETENTION_POLICY.md`](VOLUME_RETENTION_POLICY.md) for the billing/pruning policy.

| Variable | Default | Description |
|----------|---------|-------------|
| `AXGT_PERSISTENT_STORAGE_ENABLED` | `true` | Master switch for per-wallet persistent home volumes and the launcher's billing/prune sweep. |
| `AXGT_PERSISTENT_STORAGE_DIR` | `/var/lib/docker/axonos_storage` | Host directory holding the sparse `.ext4` images (launcher free-space preflight). No compose default; set via `.env`. Must match the bind mount in `docker-compose.yml`. |
| `AXGT_PERSISTENT_STORAGE_MOUNT_PATH` | `/home/aXonian` | In-container mount point; must be absolute and free of shell metacharacters or the default is used. |
| `AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX` | `axgt-user-storage-` | Docker volume name prefix (sanitized to `[A-Za-z0-9_-]`). |
| `AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES` | `0.05` | Credit-minutes charged per GB-hour of retained storage. |
| `AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS` | `3600` | Billing/prune sweep period inside the launcher (minimum 60 s). |
| `AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES` | `-1440.0` | Balance floor (negative = permitted overdraft in minutes) below which a wallet's volume is pruned. |

Requested capacity is 10–500 GB (default 100) and volumes are growth-only. Demo/guest sessions and launches flagged `ephemeral_storage` receive no persistent volume.

---

## Guest demo mode (wallet-free invites)

Sponsored, time-boxed sessions without a wallet. Read by [`axonos_gate/guest_mode.py`](../axonos_gate/guest_mode.py) and [`scripts/guest_invite.py`](../scripts/guest_invite.py). None of these has a compose `environment:` default; they reach the gate through `.env` (`env_file: .env`).

| Variable | Default | Description |
|----------|---------|-------------|
| `AXONOS_GUEST_MODE_ENABLED` | `false` | Fail-closed switch for guest invites and guest sessions. |
| `AXONOS_GUEST_INVITE_MINTERS` | falls back to `AXONOS_TEST_CREDIT_WALLETS`, then `AXONOS_WHITELISTED_WALLETS` | Comma-separated wallets allowed to mint invites. Guests can never mint. |
| `AXONOS_GUEST_SESSION_MINUTES` | `30` | Guest session length (1–240). Guests are never preserved past this hard cap. |
| `AXONOS_GUEST_WARN_MINUTES` | `5` | Minutes before the cap at which the UI warns the guest. |
| `AXONOS_GUEST_CREDIT_BUFFER_MINUTES` | `5` | Credit headroom funded beyond the session length (max 120). |
| `AXONOS_GUEST_ALLOWED_PROFILES` | `small` | Comma-separated GPU profiles a guest may launch; invalid names are dropped. |
| `AXONOS_GUEST_ALLOWED_TEMPLATES` | *(empty = any)* | Comma-separated science templates guests may select. |
| `AXONOS_GUEST_MAX_LIVE_PER_SPONSOR` | `2` | Concurrent live guest sessions per sponsoring wallet. |
| `AXONOS_GUEST_MAX_INVITES_PER_DAY` | `20` | Invite mint rate limit per sponsor. |
| `AXONOS_GUEST_INVITE_TTL_HOURS` | `168` | Invite validity. |
| `AXONOS_GUEST_DATA_RETENTION_DAYS` | `30` | Retention of guest identity rows. |

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
| `IPFS_API_BIND` | Tenant/multi-user base: `127.0.0.1`; standalone: `0.0.0.0` | IPFS API bind address (`ipfs config Addresses.API`). |
| `IPFS_API_PORT` | `5001` | IPFS API port. |
| `IPFS_GATEWAY_BIND` | Tenant/multi-user base: `127.0.0.1`; standalone: `0.0.0.0` | IPFS gateway bind address. |
| `IPFS_GATEWAY_PORT` | `8080` | IPFS gateway port. |

Launcher-managed tenants and the gate-only multi-user base default to loopback so
IPFS control surfaces are not reachable across session bridges. Standalone mode
keeps the historical all-interface default. Override only when intentionally
exposing IPFS.

---

## Docker Compose–specific

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPOSE_PROJECT_NAME` | `axonos` (via `name: axonos`) | Compose project label. Control (`axonos_control`), shared media (`axonos_stack`), and dynamic `axgt-session-net-<id>` networks have explicit names. |

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
                       axonos_control
       ┌──────────────────────┬──────────────────────┐
       │                      │                      │
┌──────▼───────┐      ┌───────▼────────┐      ┌──────▼───────┐
│ postgres     │      │ axonos-launcher │      │ axonos       │
│ DB only      │      │ :8090           │ HTTP │ central gate │
└──────────────┘      │ docker.sock     │◄─────│ :6080/:8889 │
                      └───────┬────────┘      │ internal 8890│
                              │ docker run     └──────┬───────┘
                              │                      │
                              └── axgt-session-net-N ┘
                                      │
                              ┌───────▼────────┐
                              │ axgt-session-N │
                              │ media config + │
                              │ scoped secrets │
                              └────────────────┘
```

The launcher creates one `axgt-session-net-N` per tenant. Postgres and the
launcher are not attached; the central gate joins so that scoped heartbeat,
signaling, and file operations can reach it.

**Single-container legacy:** set both `AXGT_USER_CONTAINER_ENABLED=false` and
`AXGT_MULTI_SESSION_ENABLED=false`. The base container runs Xorg + VNC + gate
together; set `AXGT_DESKTOP_ENABLED=true`. Code also forces multi-session
scheduling off whenever user containers are disabled.

**Compose default:** `AXGT_USER_CONTAINER_ENABLED=true`, base is gate-only, desktops in `axgt-session-*`.

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

**`env.example` vs compose:** the `axonos` and `axonos-launcher` services load `env_file: .env`, so every `.env` line reaches both containers. Variables that appear in `env.example` but in no compose `environment:` block (`AXGT_PERSISTENT_STORAGE_DIR`, `AXGT_FILES_MIN_FREE_BYTES`, `AXGT_FILES_MAX_FILE_BYTES`, all `AXONOS_GUEST_*`) therefore work from `.env` but have no compose-level default — the code default applies when the `.env` line is absent or commented out. Services without `env_file` (`postgres`, `coturn`, `files-tls`) see only their explicit `environment:` entries.

*Generated from codebase audit (last reconciled 2026-09-06). When adding a new `os.getenv` call, update this document, `env.example`, and the relevant compose `environment:` block.*
