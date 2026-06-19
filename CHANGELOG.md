# Changelog

All notable changes to AxonOS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is the first changelog entry; it summarizes everything the
`feat/webrtc-nvenc-stability` branch adds on top of `main` for the
**public-beta** stack (`axonos:public-beta`).

## [public-beta] - 2026-06-19

The public-beta release turns AxonOS from a single shared noVNC desktop into a
multi-session, GPU-scheduled, WebRTC-streamed compute platform with a
multi-currency prepaid-billing rail (ETH / USDC / AXGT / x402) and headless
agent access over SSH.

### Added

#### WebRTC remote desktop
- Browser-native WebRTC desktop path alongside (and preferred over) noVNC, with
  automatic fallback to noVNC over WebSockets when negotiation fails
  (`WEBRTC_FALLBACK_ENABLED`).
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
- Optional **dynamic USD-equivalent pricing** via a server-side CoinGecko price
  oracle cached in Postgres (`AXGT_DYNAMIC_PRICING`, `AXGT_USD_PER_HOUR`).
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

#### Documentation
- New guides: `docs/WEBRTC.md`, `docs/ENVIRONMENT_VARIABLES.md`,
  `docs/TOKENOMICS.md` (rewritten), `docs/HOST_LAUNCHER.md`, `docs/GROMACS.md`,
  `docs/VOLUME_RETENTION_POLICY.md`, `docs/WEBRTC_INPUT_VALIDATION.md`,
  `docs/X402_AGENT_TEST.md`, `docs/axonos_user_flow.md`, architecture SVGs, and an
  interactive flow wireframe. Annotated `env.example` covering every variable.
- Test harness `tools/x402-agent-test/` (JS + Python) for end-to-end x402 payment.

#### Tests
- New suites for WebRTC capture/config/input/X11, x402 verifier, discount tiers,
  deposit verifier, session launcher, Docker GPU CLI, and file-agent smoke.

### Changed
- Single, self-contained **public-beta compose stack** (`axonos:public-beta`,
  gate + Postgres + session launcher) on the fixed `axonos_stack` network; the
  base container is gate-only when per-user containers are enabled.
- Default payment configuration switched to **mainnet**: AXGT on Ethereum
  mainnet, USDC on Base mainnet; dynamic USD pricing on at $1/hour with the AXGT
  pay-in bonus.
- Default model switched to `gemma4:31b`; clipboard routed through the sidebar
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
- Deposits are verified server-side on-chain (Transfer event log, confirmations,
  replay protection); client-reported balances and amounts are never trusted.
- Per-session file-transfer and WebRTC signaling require wallet auth + session
  ownership; auth-token rotation and CORS/rate-limit controls are configurable.

[public-beta]: https://github.com/AxonDAO-AXGT/AxonOS/compare/main...feat/webrtc-nvenc-stability
