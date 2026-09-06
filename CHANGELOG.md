# Changelog

All notable changes to AxonOS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The first entry summarizes everything the `feat/webrtc-nvenc-stability` branch
(merged) added on top of `main` for the hosted multi-container stack.

## [1.0.0] - 2026-09-09 — Launch release

First production release, self-hosted by AxonDAO. Covers the `frontend-revamp`
branch work from 2026-06-20 to 2026-09-04 on top of the 0.9 stack.

### Added
- **Guest demo mode**: invite-code redemption for wallet-free sessions
  (`axonos_gate/guest_mode.py`, `/api/auth/guest`, `/api/auth/guest-invite`,
  admin `/api/admin/guest-invite*`, `scripts/guest_invite.py`, `AXONOS_GUEST_*`).
- **Secure web terminal** (`/api/terminal/ticket`, `/api/terminal/ws`,
  `novnc-theme/app/terminal/`) and SSH host-key fingerprint publishing.
- **Direct TLS file plane on 443**: `docker/files-tls/` nginx demuxes TLS to the
  gate file plane and TURN-over-TCP to coturn; adaptive 4-256 MB upload chunks.
- **Environment catalog "View all"** entry in Quick Launch and a browse-catalog modal.
- **Desktop <-> Console mode switch** ("Relaunch", formerly "Swap") with MOTD banner.
- **Persistent volume sizing**: wizard storage slider, growth-only resizing,
  authoritative wallet capacity from `wallet-status`.
- **AgentLink identity layer** (`axonos_gate/agentlink_verifier.py`); x402 v2
  Bazaar discovery and `/openapi.json`.
- **Live AXGT pricing** from a Uniswap v3 TWAP with Chainlink ETH/USD, CoinGecko
  as fallback/confirmation, guarded dashboard spot-price fallback.
- **AxonAI as a native local research agent**: OpenCode-backed agentic mode
  with approvals, `/agent` `/chat` `/vision` routing overrides.
- Gate liveness credit so live sessions survive control-plane redeploys.

### Changed
- Landing page stays home across wallet connect and reload; compact/mobile view
  is browse-only.
- Release-stage chip beside the brand removed from the default UI (`AXONOS_HIDE_BETA_BADGE`, default `true`).
- Payment dialog decluttered; holder discounts applied correctly in the
  workspace wizard.
- EIP-6963 wallet picker deduplicated; stale injected providers recovered safely.
- GPU ML stack pinned and advertised version corrected; session `python3` is
  `/usr/bin/python3` (conda stays off PATH).
- Port 443 is now shared between the TLS file plane and TURN-over-TCP.

### Fixed
- Fresh loop-ext4 home volumes are writable by the session user (self-heal on start).
- Service outages no longer render as a zero credit balance; transient zeroing removed.
- "Deposit not credited" after in-wizard AXGT payment; expired wallet auth tokens
  recover with one signature.
- SSH session extension outcome is explicit; extension errors isolated from the
  noVNC fallback.
- WebSocket close codes moved into the valid application range.
- GPU telemetry made fresh and session-scoped; forked noVNC workers reaped on restart.

## [0.9.0] - 2026-06-19

The 0.9 release turns AxonOS from a single shared noVNC desktop into a
multi-session, GPU-scheduled, WebRTC-streamed compute platform with a
multi-currency prepaid-billing rail (ETH / USDC / AXGT / x402) and headless
agent access over SSH.

### Added

#### WebRTC remote desktop
- Browser-native WebRTC desktop path alongside (and preferred over) noVNC, with
  optional noVNC-over-WebSockets fallback in legacy single-container mode
  (`WEBRTC_FALLBACK_ENABLED`). Launcher-managed tenant sessions fail visibly
  when WebRTC negotiation fails because their VNC listener is intentionally
  disabled and fallback is forced off.
- In-container capture agent with HTTP signaling (offer/answer/ICE) persisted in
  Postgres; session IDs are random 256-bit tokens gated by wallet auth + session
  ownership.
- Hardware H.264 capture backends: native **NvFBC → NVENC** streamer
  (`tools/nvfbc_nvenc_streamer.c`), FFmpeg `x11grab → h264_nvenc`, and a software
  `mss`/VP8 fallback, selectable via `WEBRTC_CAPTURE_BACKEND` (`auto` by default).
- Low-latency tuning: configurable bitrate, FPS, NVENC preset/tune, one-frame VBV,
  stale-frame dropping, and SDP H.264 codec preference to avoid VP8 mis-negotiation.
- Full remote input: pointer scaling, click-and-drag tracking, mouse-wheel
  forwarding, richer keyboard input, host-cursor embedding with optional browser
  overlay (`WEBRTC_LOCAL_CURSOR`), and context-menu suppression.
- Clipboard sync over a dedicated data channel (host ↔ remote, Ctrl+V and
  right-click paste), isolated from the input channel.
- Desktop **audio** over WebRTC (PulseAudio null-sink monitor → Opus) and opt-in
  browser→desktop **microphone** (`sendrecv`, `WEBRTC_MIC_ENABLED`, off by default).
- Host NAT support: media-port pinning (`WEBRTC_PORT_RANGE`) and SDP host-candidate
  rewriting (`WEBRTC_PUBLIC_IP`) so direct `srflx` works behind 1:1 NAT, with
  STUN/TURN (incl. TURN-over-TCP on 443) as fallback.
- WebRTC input-lifecycle validation harness and browser console runner.

#### Multi-session GPU scheduling
- Exclusive whole-GPU allocation scheduler with concurrent sessions (legacy FIFO
  GPU queue removed).
- GPU profiles `small`/`medium`/`large`/`max` (1/2/4/8 GPUs) and **GPU-weighted
  billing** (`wall_clock_minutes × GPU count`).
- GPU auto-detection via local `nvidia-smi`, with host enumeration via the launcher
  when the gate has no GPUs.
- Host **session launcher** service (HTTP mode) that spawns per-user desktop
  containers via the Docker socket, with launch-verify polling to avoid false
  "failed to start" on slow spawns.

#### Payments, tokenomics & billing
- ETH-first payment model with on-chain-verified **AXGT holder discount tiers**
  (server-side `balanceOf`, 0/5/10/15/25%).
- **USDC** stablecoin rail (fixed $1, tx-hash verified, Base by default,
  independent of the AXGT/ETH chain).
- **x402** agent-native HTTP-402 rail: `GET /api/x402/access`, `POST
  /api/x402/settle` (EIP-3009 `transferWithAuthorization`, gate pays gas),
  `POST /api/x402/session` (pay-and-provision), `GET /.well-known/x402`. Serves
  both the v1 body and v2 `PAYMENT-REQUIRED` header so off-the-shelf JS
  (`x402-fetch`) and Python (`x402`) SDKs both interoperate.
- **AXGT Model B**: paying in AXGT is credited at live USD value with a flat
  bonus (`AXGT_USD_BONUS_PERCENT`, +25%) and no holder tier.
- Optional **dynamic USD-equivalent pricing** via a server-side price
  oracle cached in Postgres (CoinGecko at the time; now Uniswap TWAP, see Unreleased) (`AXGT_DYNAMIC_PRICING`, `AXGT_USD_PER_HOUR`).
- Heartbeat-based incremental billing with sliding idle cap; deposit verification
  uses the ERC-20 **Transfer event log** (smart-account safe), returns HTTP 200
  while pending, and HTTP 400 for hard failures.

#### Persistent storage & session lifecycle
- Persistent named per-user volumes mounted into desktop sessions, with offline
  storage billing (per GB-hour) and negative-balance volume pruning.
- Pause-on-credit-exhaustion: sessions are preserved and resumable after top-up
  instead of being destroyed.
- Hard, non-sliding billing cap for headless/SSH sessions
  (`AXGT_SSH_MAX_SESSION_MINUTES`) plus an in-container heartbeat daemon.

#### Direct SSH sessions (agent-friendly)
- Landing-page toggle for headless GPU sessions reachable only over SSH (no
  X/WebRTC); user pastes a public key and receives an `ssh -p <port> user@host`
  connect-string. Per-session host port `42000 + id % 50` → container `:22`;
  customized login MOTD.

#### File transfer
- Browser ↔ desktop file upload/download (`/api/files/*`) proxied to an
  in-container agent, authenticated by a per-session key injected at claim time;
  free-space and per-file size guards.

#### Wallet & UX
- In-HUD wallet management (Manage → Switch wallet / Sign out) with EIP-6963
  multi-wallet provider binding and `accountsChanged` handling; Launch/Resume/Claim
  preflight ensures the session matches the exposed account.
- Session billing HUD with live remaining-time countdown, GPU-adjusted deposit
  previews, GPU profile picker, custom themed modals (replacing native dialogs),
  and credit-exhaustion overlay with top-up/exit.

#### Telemetry
- Public telemetry portal at `/telemetry` with live GPU and session monitoring.

#### Desktop image & scientific suite
- Scientific software packaging with desktop template auto-launch and selectable
  environment templates; added Audacity; GROMACS multi-GPU validation; OpenMPI
  pinned for CUDA compatibility with MCA defaults wired into XFCE shells and
  session `docker run`.

#### AxonAI (built-in AI assistant)

- Added a loopback-only OpenCode 1.18.26 backend using Qwen 3.8, with one
  persistent session per conversation, tool and subagent execution, live
  progress, user approvals and questions, and screenshot attachments.
- Added agent routing controls (`/agent`, `/chat`, `/vision`), server-side abort
  through **Stop**, clean-session **Reset**, and a Settings switch to disable
  agentic mode.
- Added acknowledged asynchronous dispatch, fail-closed cancellation fencing,
  and a root-managed permission policy so stopped or locally reconfigured turns
  cannot silently resume with broader tool authority. Ambiguous cleanup requires
  a full AxonOS session restart because detached tool processes cannot be fenced
  by deleting a session or restarting OpenCode alone. A runtime marker carries
  that fail-closed state across GUI crashes and multiple AxonAI windows. A
  trusted shell-environment bridge preserves normal desktop HOME/XDG/display
  behavior for approved tools.
- Rebranded the built-in assistant as **AxonAI** and aligned its GTK/WebKit UI
  with the AxonOS v2 near-black, purple, and teal design system.
- Made AxonAI and Talk to K single-instance, initially maximized applications
  with native move/minimize/maximize controls. Vision turns temporarily unmap
  AxonAI before capturing the desktop, then restore it maximized.

#### Documentation
- New guides: `docs/WEBRTC.md`, `docs/ENVIRONMENT_VARIABLES.md`,
  `docs/TOKENOMICS.md` (rewritten), `docs/HOST_LAUNCHER.md`, `docs/GROMACS.md`,
  `docs/VOLUME_RETENTION_POLICY.md`, `docs/WEBRTC_INPUT_VALIDATION.md`,
  `docs/X402_AGENT_TEST.md`, `docs/axonos_user_flow.md`, architecture SVGs, and an
  interactive flow wireframe. Annotated `env.example` covering nearly every variable
  (`WEBRTC_PORT_RANGE` is documented in `docs/ENVIRONMENT_VARIABLES.md` only).
- Test harness `tools/x402-agent-test/` (JS + Python) for end-to-end x402 payment.

#### Tests
- New suites for WebRTC capture/config/input/X11, x402 verifier, discount tiers,
  deposit verifier, session launcher, Docker GPU CLI, and file-agent smoke.

### Changed
- Single, self-contained **compose stack** (image tag set in `docker-compose.yml`,
  gate + Postgres + session launcher): Postgres and the Docker-socket launcher
  stay on `axonos_control`, coturn uses the shared media network, and each tenant
  gets a dynamic `axgt-session-net-<id>` bridge shared only with the central
  gate. The base container is gate-only when per-user containers are enabled.
- Default payment configuration switched to **mainnet**: AXGT on Ethereum
  mainnet, USDC on Base mainnet; dynamic USD pricing on at $1/hour with the AXGT
  pay-in bonus.
- Default model switched to `qwen3.8:latest`; clipboard routed through the sidebar
  panel; "Ending session…" / "Resuming session" loader copy clarified.
- Telemetry note: statistics recorded before 2026-06-18 11:19 UTC are testnet.
- Local env files (`.env.*`) are now gitignored; `env.example` stays tracked.

### Fixed
- WebRTC: SDP line-ending normalization, signaling Postgres commit/persistence,
  display-wait before capture, agent routing to the local gate, post-paste click
  stalls, clipboard ownership races, multi-second playback lag, stuck modifier
  keys, and input recovery across repeated session spawn/teardown.
- GPU/Xorg: NVIDIA userspace pinned to the host driver to stop GLX-mismatch Xorg
  crashes; libglx → NVIDIA GLX symlinking; avoid GLX double-registration and
  conflicting `--gpus` requests in nested `docker run`.
- Sessions/billing: slide `expires_at` on heartbeat to stop killing active
  sessions; restore billing poll and credit warnings during WebRTC sessions;
  relaunch cleanly after expiry/top-up; stop false session-start failures when a
  spawn outlives the launch timeout.
- Wallet/UI: bind `accountsChanged` to the selected provider (not
  `window.ethereum`); preflight claims against the exposed account; harden
  provider detection against injected extension conflicts; numerous landing-page
  layout, scrollbar, font, and mobile-scrolling fixes.
- Gate: serve no-cache for JS/CSS; cache-friendly Dockerfile gate COPY.

### Security
- TURN credentials, the WebRTC agent internal key, the x402 settlement signer
  key, Postgres credentials, and the launcher token are all configured via
  environment/secrets and never logged. The x402 settlement signer is a dedicated
  low-balance hot wallet, separate from the revenue/treasury wallet.
- Launcher-managed tenants receive only exact session identity, per-session
  bearer values, and allowlisted media tuning. Database, payment/RPC, launcher,
  and fleet-signing credentials remain in the control plane. Agent operations
  are accepted only on the unpublished `:8890` listener after signed capability,
  compute ID, wallet, file-key fingerprint, and live-allocation checks, then use
  session-scoped SQL.
- Deposits are verified server-side on-chain (Transfer event log, confirmations,
  replay protection); client-reported balances and amounts are never trusted.
- Per-session file-transfer and WebRTC signaling require wallet auth + session
  ownership; auth-token rotation and CORS/rate-limit controls are configurable.

[1.0.0]: https://github.com/AxonDAO-AXGT/AxonOS/compare/main...frontend-revamp
[0.9.0]: https://github.com/AxonDAO-AXGT/AxonOS/compare/main...feat/webrtc-nvenc-stability
