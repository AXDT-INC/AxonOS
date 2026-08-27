# AXGT Reimplementation Tracker

- [x] Phase 1: Backend hold-based gating, signed challenge, token rotation/grace
- [x] Phase 1.5: Single-active-session lock + FIFO queue (backend)
  - [x] `session_manager.py` — Postgres-backed session table + queue table
  - [x] API endpoints in `websockify_gate.py` and `gate_server.py`
    - `POST /api/session/claim` — attempt to claim the desktop
    - `POST /api/session/heartbeat` — keep session alive
    - `POST /api/session/release` — explicitly end session
    - `GET  /api/session/status` — active session + queue position
    - `POST /api/queue/join` — join the waiting queue
    - `POST /api/queue/leave` — leave the queue
  - [x] WebSocket upgrade gated on session ownership
  - [x] Frontend queue overlay + auto-claim after wallet verify (Feature 3)
  - [x] Desktop reset script between session transitions (Feature 2 Option A)
- [x] Phase 2: Frontend Connect Wallet + strict sign-to-verify + status polling/overlay
- [x] Phase 3: Deployment helpers — parallel compose (16080/18889, deposit-preview image/volume)
- [x] Phase 3b: Docs + UX — ETH/AXGT parity defaults (0.0005 ETH ≈ 100 AXGT min tier); `/api/config` + vnc copy for credit rates
- [x] Wallet UI: revenue address from `/api/config`, tx-hash → verify-deposit; auth token after sign even with 0 prepaid
- [x] Fix `verify-wallet` 500: `get_wallet_access_status` must `import deposit_ledger` when gate runs with flat `/axonos_gate` on `sys.path` (not `axonos_gate` package)
- [x] Wallet pay (AXGT/ETH) + auto poll verify-deposit (Option C)
- [x] Wallet pay: optional amount inputs (≥ server min), Min reset, BigInt validation (`novnc-theme/vnc.html`)
- [x] Send ETH from wallet: explicit `gas: 0x5208` (21000) — fixes MetaMask/RPC “gas limit too high (cap 16777216, tx 21000000)” on PublicNode-style caps
- [x] Queue overlay: `try_claim_session` + `join_queue` return `queue_length`; poll refreshes overlay when desktop held by another wallet (`vnc.html` + `session_manager.py`)
- [x] Join Queue button: show errors (reason/error/HTTP), Joining… state, `white-space: pre-line` for multi-line message; `joinQueue()` uses fetch + `_ok`/`_httpStatus` (was silent on failure)
- [x] **websockify (6080) parity with gate**: `verify-wallet` issues `auth_token` when signed but 0 prepaid; `/api/config` includes revenue wallet + deposit policy; `POST /api/auth/verify-deposit` on 6080 (fixes tunnel-on-6080 “Could not complete sign-in” + missing top-up UI)
- [x] Queue join: `deposit_ledger` import + unique index migration on `axgt_queue` (fixes ON CONFLICT / Internal error); user-facing reason + vnc copy without raw "Internal error"
- [x] Queue overlay: unified styled Join / Leave buttons (gold border, dark gradient, hover/disabled) — `axonos-theme.css`
- [x] Post session-expiry relaunch: reset stale queue/WebRTC gate state on Launch; route claim-denied to recharge/resume instead of deprecated queue overlay (`ui.js`, `vnc.html`)
- [x] Deposit verify success: teardown stale WebRTC + restore access to a credit-grace session after top-up (`vnc.html` `axonosDepositVerifiedSuccess`)
- [x] WebRTC UI: defer `Connected (WebRTC)` and billing poll until ICE connected/completed (`axonos-webrtc.js`)
- [x] **Launch must `POST /api/session/claim` before WebSocket** — gate rejects WS if not session owner (was 1006 after leave queue + Launch); `axonosOnSessionClaimDenied` → queue overlay (`ui.js` + `vnc.html`)
- [x] noVNC landing + wallet dialog: network environment banner from `/api/config` `axgt_chain_id` (mainnet vs testnet); operator notes in `vnc.html` + `env.example`
- [x] ETH deposit feature flag: `AXGT_ENABLE_ETH_DEPOSITS` gates backend crediting + `/api/config` and hides ETH top-up controls in wallet UI (`deposit_verifier.py`, `axgt_verifier.py`, `gate_server.py`, `websockify_gate.py`, `vnc.html`)
- [ ] Phase 4: End-to-end runtime test checklist
- [ ] Phase 5: Public beta concurrency (exclusive whole-GPU)
  - [x] Feature-gated multi-session scheduler in `session_manager.py`
  - [x] Profile-aware queue (`small=1`, `medium=2`, `large=4`, `max=8`)
  - [x] GPU-weighted heartbeat billing (prepaid minutes × assigned GPU count)
  - [x] API payload support for `requested_profile` in claim/join
  - [x] Frontend profile selector + queue/allocation status messaging
  - [x] Mode B launcher adapter (`session_launcher.py`) with `http`/`docker_cli`/`noop`
  - [x] Host launcher service (`session_launcher_service.py`) + deployment doc
  - [x] Compose-managed launcher service (`axonos-launcher`) for one-command ops
  - [ ] Runtime validation with real concurrent wallets + GPU inventory
    - [ ] Queue dedupe smoke (`ed68d69`): two-wallet contention covering 30 s throttle, in-flight guard, fast path, network-error retry, profile carry-through (Small/Medium/Large)
- [x] Branch sync (main → public-beta, 2026-05-09):
  - [x] Cherry-pick `327131b` — harden noVNC wallet provider detection (TronLink/injected proxy guard)
  - [x] Cherry-pick `e7160f2` — preserve verified session when credentials dialog reopens
  - [x] Port `ae08db9` queue auto-join dedupe into public-beta's profile-aware claim-denied path (adapted in `ed68d69`, not cherry-picked, due to multi-session architecture introduced by `f75b634`)
  - [x] **Skip** `6b8d77e` (AXGT CTA testnet faucet flow) — intentionally not ported; functional duplicate of `34b49a3` already on public-beta with different variable names (`getAxgtHref`/`getAxgtLabel` vs `axgtCtaUrl`/`axgtCtaLabel`). Both implement env-driven mainnet-vs-testnet CTA from `AXGT_CHAIN_ID`; mainnet deployments will render Uniswap automatically.
- [x] **Tokenomics upgrade — ETH-first + AXGT discount tiers (2026-05-09)**:
  - [x] New `axonos_gate/discount.py` with tier config (env JSON / file / compact), on-chain `balanceOf`, RPC-failure-safe defaults
  - [x] `deposit_verifier.py` ETH path: server-side AXGT balance re-check + discount-adjusted min and credit rate; AXGT direct deposits gated behind `AXGT_ENABLE_AXGT_DEPOSITS` (default false)
  - [x] `/api/config` exposes `axgt_discount_tiers`, `axgt_direct_deposits_enabled`; new `GET /api/discount/quote` on both gate (8889) and websockify (6080) backends
  - [x] vnc.html: AXGT discount tier card (base ETH / wallet AXGT balance / tier / discount % / final ETH); ETH-only by default; copy switched to "Pay with ETH, save with AXGT"
  - [x] vnc.html: remove "Discounted" min button; live `/api/discount/quote?base_eth=` preview + pay hint on ETH input (pay-what-you-type model)
  - [x] Tests: 28-case `test_discount.py` covering all tier boundaries, RPC failure fallback, env overrides, and end-to-end ETH+AXGT verifier paths; existing deposit/ledger/access tests still pass
  - [x] Docs: rewrote `docs/TOKENOMICS.md` for ETH-first + tier system + deployment/testing checklist; updated `env.example` with tier-config knobs

- [ ] **WebRTC desktop streaming (2026)** — implementation landed; runtime validation on full GPU stack pending
  - [x] `axonos_gate/webrtc/` — config, Postgres signaling store, metrics logging, REST handlers
  - [x] Gate + websockify: `/api/webrtc/*` (session, offer, status, ice, metrics, close) + agent endpoints; `/api/config` exposes WebRTC flags
  - [x] `webrtc_agent_main.py` — aiortc + mss capture + supervisord `webrtc-agent` program
  - [x] Frontend `novnc-theme/app/webrtc/axonos-webrtc.js` + `ui.js` try-before-RFB; disconnect tears down WebRTC
  - [x] `docs/WEBRTC.md`, `env.example`, `docker-compose.yml` env wiring + launcher passthrough for session containers
  - [x] `axonos_gate/tests/test_webrtc_config.py` (unittest)
  - [ ] Compose E2E: negotiate WebRTC, verify video + input + fallback + unauthorized signaling denial
  - [x] WebRTC billing poll regression (`ff3171d`): tighten wallet-status credit-exhausted check (ignore 401/400), restart poll on Launch when session active, keep poll alive after queue leave on active WebRTC
  - [x] Credit exhaustion: logical 2-hour top-up grace retains the running container/jobs/GPU while access and billing stop (`AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST`, default true)
  - [x] Deposit verify success: show credited time as desktop minutes for selected GPU profile (`axonosFormatDesktopTimeLabel` in `vnc.html`)
  - [x] Top-up reconnect UI: hide GPU picker, show retained-session panel, claim reuses the credit-grace profile/GPUs (not a new selection)
  - [x] **GPU-accelerated WebRTC capture/encode (NVENC)** — `WEBRTC_CAPTURE_BACKEND=auto|nvenc|mss`; FFmpeg x11grab → h264_nvenc → aiortc H.264 passthrough; MSS/VP8 fallback
    - [x] Add `ffmpeg` to image; runtime probe for `h264_nvenc` + `libnvidia-encode`
    - [x] `axonos_gate/webrtc/capture.py` + env: `WEBRTC_CAPTURE_BITRATE`, `WEBRTC_CAPTURE_NVENC_PRESET`
    - [ ] Validate delivered FPS / scroll sharpness via client metrics on real GPU stack
    - [x] **Black screen fix (2026-05)**: NVENC sent H.264 but SDP negotiated VP8 — `prefer_h264_for_pc()` + browser `setCodecPreferences(H264)` before offer/answer
    - [ ] Longer term: PipeWire/DMA-BUF screencast for zero-copy GPU capture (requires desktop stack changes)
    - [ ] **NVENC runtime**: ensure session containers get `NVIDIA_DRIVER_CAPABILITIES` including `video` (required for `libnvidia-encode` / `h264_nvenc`); Dockerfile sets this at end of image build
  - [x] **Desktop audio over WebRTC (2026-06-11)** — PulseAudio null sink `axonos_out` (`pulse-default.pa`, supervisord `pulseaudio` program) + ffmpeg pulse capture of `axonos_out.monitor` → Opus track on the same peer connection (`capture.py` `open_audio_capture`); browser `recvonly` audio transceiver + gesture unmute (`axonos-webrtc.js`); live-verified on clu1 (inbound `audio/opus` 48 kHz stereo over direct srflx pair)
  - [x] **Microphone input (browser → desktop) (2026-06-13)** — operator gate `WEBRTC_MIC_ENABLED` (off by default) + per-user opt-in mic toggle (`getUserMedia` → `replaceTrack` on a `sendrecv` audio transceiver, no renegotiation); virtual mic via `axonos_mic` null-sink + `module-remap-source` → `axonos_microphone` default source (`pulse-default.pa`); agent `pc.on("track")` → `pump_inbound_audio_to_pulse` decodes/resamples Opus → `pacat` into the sink (`capture.py`), task torn down with the session; `webrtc_mic_enabled` exposed via `/api/config` (`config.py`); unit tests in `test_webrtc_capture.py` + `test_webrtc_config.py`
    - [x] Runtime validation on 8x v100 cluster (2026-06-13): clean speech recorded in Audacity from `axonos_microphone`. Required three fixes beyond the first cut: (1) async `pacat` subprocess + `await drain()` — a blocking pipe write was starving the aiortc event loop and dropping the session every 2-3s; (2) pin the Pulse null sinks to 48 kHz (`pulse-default.pa`) — daemon default 44.1 kHz forced continuous live resampling/drift; (3) extract PCM via `frame.to_ndarray().tobytes()` not `bytes(plane)` — the latter included FFmpeg buffer-alignment padding (3968 vs 3840 bytes/frame), garbling every frame. Also: forward `WEBRTC_MIC_ENABLED`/`WEBRTC_AUDIO_*` in `AXGT_HOST_SESSION_ENV_PASSTHROUGH` (docker-compose) or the session agent never sees the flag; mic toggle repositioned left of the session HUD.
  - [ ] **noVNC fallback audio — measure before building (likely never)**: count `webrtc_fallback_novnc` log lines vs total sessions first; if fallback is rare (<~5%), skip. The better fix for fallback users is WebRTC reachability — add `turns:`/TCP (ideally on 443) to coturn so UDP-blocked clients keep WebRTC (audio + video) instead of falling back. Building actual fallback audio means a parallel stack (per-session ffmpeg pulse → Opus/WebM → authed `websockify_gate.py` WebSocket → MediaSource/AudioContext playback, Kasm-style) — only justified if fallback usage proves common *after* TURN-TCP lands. Mic on fallback: out of scope permanently (mic rides the WebRTC `sendrecv` transceiver only)

- [ ] **Docker build — WhiteSur GTK theme step fails (2026-05)**
  - `docker compose build` fails at Dockerfile ~491: `WhiteSur-gtk-theme` `install.sh --silent-mode -c Dark` exits before themes land
  - Likely fixes to try: pin theme tag (e.g. `2024.09.02`), use `-c dark`, add `imagemagick`/`gawk` to that RUN, relax `grep -i white` verification
  - Workaround for now: build from cached image / skip layer until investigated
  - Related: duplicate `ENV NVIDIA_DRIVER_CAPABILITIES` lines in Dockerfile (early without `video`, late with `video` — late wins); consolidate when touching Dockerfile again

- [x] **Sidebar session controls — Detach vs End session (2026-05)**
  - **Product model**
    - [x] **End session** (power button): confirm → `POST /api/session/release` + viewer teardown
    - [x] **Detach** (sidebar chain icon): confirm → `skipRelease` + home panel, session stays **`active`**
    - [x] **Tab/window close**: while attached, `pagehide` + `fetch` keepalive releases (F5/Ctrl+R skips via `sessionStorage`); after explicit Detach, the durable container heartbeat keeps jobs running and billed
  - **Proposal 1 — Power button → End session**
    - [x] Removed `#noVNC_power` panel; single-click `UI.endSession()` → `UI.disconnect()`
    - [x] Renamed control copy to **End session**
    - [x] **Restart desktop services** moved to Settings → Advanced (`POST /api/session/restart`)
  - **Proposal 2 — Disconnect → Detach**
    - [x] `UI.detach()` + `window.axonosSessionDetached`; billing poll via `_axgtSessionBillingActive()`
    - [x] Ticker + wallet hints updated in `vnc.html`
  - **Tab close → release**
    - [x] `addAxonosSessionLifecycleHandlers()` in `ui.js`
    - [x] Reload detection (F5 / Ctrl+R / beforeunload)
  - **Audit**
    - [x] `_axgtUsageOverlayExitToHome()` uses `skipRelease` (credit-grace session retained)
    - [x] Credit exhaustion + deposit/resume `skipRelease` paths unchanged
  - [ ] Manual test checklist: Detach → home → heartbeats continue → reconnect; detached tab close → jobs/billing continue; End session → container gone; zero credit → top-up grace → reconnect
- [x] **Launch button dead after Detach / End / server expiry (2026-05)**
  - [x] `cancelAxonOSWebRTCNegotiation()` aborts in-flight signaling (no stale timeout banners on home)
  - [x] Reset client state on disconnect, detach home, and heartbeat `No active session`
  - [x] `_axgtSessionDesktopActive()` requires live RFB/WebRTC media (not teardown fn alone)
  - [x] Connection loader on Launch; errors cleared via `axonosPrepareDesktopLaunch`
- [x] **Environment variables reference** — `docs/ENVIRONMENT_VARIABLES.md` (full codebase audit vs `env.example`)

- [ ] **Structural cleanups from the template-launch debugging hunt (2026-06-10)** — either would have prevented most of it
  - [ ] Consolidate the two `/api/session/claim` client implementations (`vnc.html` inline `claimSession()` and `ui.js` `_axonosFetchSessionClaim()`) into one shared function — the inline one spawned sessions without `requested_template` while the `ui.js` one was repeatedly fixed in vain
  - [ ] Gate should send `Cache-Control: no-cache` for `vnc.html` — browsers heuristically cache documents served with only `Last-Modified`, so deployed fixes (including inline JS) silently never reached the browser

- [x] **USDC + x402 payment rail + dynamic USD pricing + agent SSH (2026-06-16)** — Base Sepolia
  - [x] USDC tx-hash rail (`x402_verifier.py` `verify_usdc_deposit`) + `POST /api/auth/verify-usdc-deposit`; self-verified on Base, same revenue EOA, credits the shared ledger
  - [x] **Verify the ERC-20 Transfer event log, not `tx.from`/`tx.to`** — smart-account/delegated payments (EIP-7702, MetaMask Smart Accounts, "Redeem Delegation") submit via a relayer, so `tx.from` is the relayer. The original sender check wrongly rejected valid payments
  - [x] x402 protocol: `GET /api/x402/access` (402 + terms), `POST /api/x402/settle` (EIP-3009 self-settlement, needs `X402_SETTLEMENT_PRIVATE_KEY` = funded Base hot wallet); EIP-712 domain probe warns on `USDC_EIP712_NAME` mismatch (Base Sepolia name() is "USDC", not "USD Coin")
  - [x] `deposit_router.py` — `POST /api/auth/verify-deposit-auto` tries both rails server-side (verified > already-credited > pending > fail precedence); avoids polling the wrong rail while a tx is briefly unconfirmed
  - [x] Dynamic USD pricing `price_oracle.py` — CoinGecko free API (ETH `ethereum`, AXGT `axondao-governance-token-2`), Postgres-cached, lazy-polled ~8x/day, last-known + 24h staleness fallback; opt-in `AXGT_DYNAMIC_PRICING`. ETH/AXGT charged at live USD value (`AXGT_USD_PER_HOUR`, $1/hr default); USDC fixed
  - [x] **Model B for AXGT**: paying IN AXGT = live USD value + flat `AXGT_USD_BONUS_PERCENT` (25%) bonus, NO holder tier. Holder tiers still apply to ETH/USDC. `/api/discount/quote?currency=eth|usdc|axgt`
  - [x] Frontend: segmented USDC | ETH | AXGT toggle (default USDC, remembers choice), per-rail discount/rate panels with live quotes, auto-detecting "Credit deposit", model-accurate marquee/subheading copy
  - [x] **Agent-native one-shot**: `POST /api/x402/session` — pay (X-PAYMENT) AND claim an SSH session in one call; the EIP-3009 payment signature is the auth (no browser wallet sign-in). Returns `ssh_host`/`ssh_port`/`remaining_minutes`/`auth_token`. 402 + terms when unfunded
  - [x] `GET /.well-known/x402` discovery descriptor (capabilities, pricing, endpoints, session lifecycle); SSH only — desktop intentionally not advertised to agents
  - [x] All routes/config added to BOTH `gate_server.py` (:8889) and `websockify_gate.py` (:6080, browser path). Chains by design: AXGT+ETH on Ethereum L1, USDC+x402 on Base L2 (same revenue EOA; UI notes the network switch)
  - [x] Tests: x402 EIP-712 fixtures + router precedence + ABI-decode (146 passing); baked via `docker compose --build` (image == git == running)
  - [x] **Heartbeat for headless / SSH-only sessions (2026-06-16)** — browser-less sessions (human SSH-toggle + agent SSH) had NO heartbeat driver (only novnc ui.js sends them), so they were reaped after AXGT_HEARTBEAT_TIMEOUT_SECONDS (~120s) and barely billed. Confirmed live (session 142 ended at ~100s with ~57min paid left). Fix: `/api/session/heartbeat` now also accepts the per-session `files_key` (X-AXGT-Session-Key header) via `validate_session_files_key`, and `session_heartbeat_daemon.py` (supervisord `heartbeat-daemon`) sends heartbeats from inside headless containers (idles when AXGT_DESKTOP_ENABLED=true). Gate-side proven: files_key heartbeat authenticates, resets last_heartbeat, bills correctly; wrong/cross-wallet keys → 401. **Needs session-image rebuild** to ship the daemon + supervisord.conf into containers.
  - [x] **SSH billing hard cap + self-release (Vast-aligned, 2026-06-16)** — the daemon keeps headless sessions alive with no "user left" signal, and `expires_at` SLIDES on every heartbeat (idle timeout, not a cap), so an abandoned SSH session would drain the whole prepaid balance. Fix (no activity-guessing, so a live headless job is never falsely killed): new non-sliding `hard_expires_at` column set at SSH claim to `now + min(affordable minutes, AXGT_SSH_MAX_SESSION_MINUTES ceiling)`; reaper ends the session when `hard_expires_at <= now`. Proven: hard cap does NOT slide on heartbeat (expires_at does). Also: `/api/session/release` now accepts the per-session files_key (X-AXGT-Session-Key) so an agent/headless user can self-release (the explicit "stop") — proven, wrong key → 401. Desktop unaffected (no hard_expires_at; browser presence bounds it).
  - [ ] **GUI desktop for agents (computer-use bridge)** — DEFERRED. SSH is agent-usable (text I/O); the XFCE/WebRTC desktop is a pixel stream and needs a screenshot surface + synthetic input API (click/type/key via xdotool, hooking into the WebRTC agent's input layer) + optional a11y tree. Only worth it for GUI-only scientific tools with no headless mode. Lead with SSH; add the desktop bridge if/when GUI-only agent workloads demand it

- [x] **False "Could not start session / Failed to start user container / timed out" while the container actually spawns (2026-06-18)** — root cause: in `AXGT_SESSION_LAUNCHER_MODE=http`, the gate→launcher `/launch` call (`session_launcher.py` `_http_json`) defaulted to a **10s** timeout, but the launcher's `/launch` runs `docker run -d --gpus … --shm-size 32g` synchronously. Cold image cache / nvidia-runtime init / daemon contention push `docker run` past 10s → `urllib` raises `"timed out"` → `_launch_via_http` returns failure → `try_claim_session` marks the row `ended`/`failed` and returns "Failed to start user container" + `container_error: "timed out"`, *while the launcher's `docker run` finishes a moment later and `axgt-session-N` is live*. Hard-refresh/reconnect and `docker compose up -d --build` "fixed" it only by re-claiming with a warm image cache (<10s).
  - [x] **Verify-before-fail** (keystone): on an inconclusive `/launch` (timeout/transient), poll the launcher's `/list-containers` (`_verify_container_started_via_http`, default 5×2s); if the session container is up, treat as success and return its id instead of false-failing
  - [x] Raise default launch HTTP timeout 10s → **90s** (`AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS`); `_http_json` now takes an optional per-call timeout (verify uses 5s) and supports body-less GET
  - [x] Launcher Flask `app.run(..., threaded=True)` so a slow `/launch` and the volume-prune/enumerate `docker run`s don't head-of-line block concurrent launches
  - [x] Reap orphan container by deterministic name on **confirmed** spawn failure (`try_claim_session`) so a partial spawn can't leak a GPU/ports and later starve claims ("No GPUs available")
  - [x] Per-wallet `pg_advisory_xact_lock` at claim time — kills the double-spawn race where the UI's two racing claims (vnc.html + ui.js) both pass "no active session" and spawn duplicates (one leaks, one branch can false-fail)
  - [x] Tests: timeout-but-running → success, timeout-and-absent → real failure, clean-success-skips-verify (149 passing). **Needs gate rebuild** (`docker compose up -d --build axonos-gate axonos-launcher`) to ship; new env knobs documented in `env.example`

- [x] **Frontend revamp: axonos-web v2 design ported into vnc.html (2026-07-05, branch `frontend-revamp`)** — replaced the navy/cyan landing with the Claude-Design `~/axonos-web` look while keeping every functional hook: zero element IDs removed (219 before → 222 after; only `axonos_hero_globe`, `axonos_hero_launch_btn`, `axonos_hero_browse_btn` added), the ~3.8k-line inline wallet/x402/session JS untouched except color literals, `ui.js` untouched. Landing restructured inside `#noVNC_connect_dlg`: topbar (axon-x logo + wordmark + BETA + network pill), hero with rotating dotted-globe canvas (rAF loop paused on hidden tab / offscreen / connected / reduced-motion), env catalog + sticky "YOUR SESSION" rail (selected env, GPU profile, SSH switch, gradient launch CTA), how-it-works, trust strip, footer credit. Theme retint via scripted RGB-triplet mapping (old cyan `#4ec3d4`→purple `#7b6cff`, green→teal `#4fe0c0`, warm→gold, navy bgs→`#080910` family) across CSS + inline styles + data-URI chevrons; fonts now Hanken Grotesk / JetBrains Mono (Google Fonts, graceful system fallback) + local BrutalType display. New asset `novnc-theme/axon-x.png` (+ Dockerfile COPY). Verified in headless Chromium against the extracted container web root + stubbed `/api/config`: landing/rail/wallet-pay/modal/About/mobile screenshots clean, no page errors, scroll behavior identical to old (internal `.noVNC_center` scroll). **Needs image rebuild to deploy.** Trust-strip copy avoids the prototype's false "files persist" claim (sessions are ephemeral).
- [x] **Frontend revamp fixes following tester feedback (2026-07-08)**:
  - [x] Payment verification overlay: added a full-screen "Payment sent" overlay (`payment` phase) to prevent a blank/dead screen during the on-chain confirmation wait (`vnc.html`, `ui.js`).
  - [x] WebRTC connection reliability & messaging: added an automatic negotiation retry on initial failure, and updated the error status message to clearly guide the user to click Resume (avoiding duplicate payments) (`ui.js`).
  - [x] Keyboard input focus: fixed video click behavior by explicitly focusing the `video` element on `mousedown` so local text inputs are blurred and remote typing works immediately (`axonos-webrtc.js`).
  - [x] Localization space-collapse fix: added `translate="no"` to AxonOS-custom DOM roots to prevent noVNC's translation engine from trimming whitespace around inline elements (`vnc.html`).
- [x] **Storage/SSH/telemetry regression fixes on `frontend-revamp` (2026-07-09)** — verified the v0.2 investigation report, fixed the confirmed items (one claim was already fixed):
  - [x] **Tab close no longer kills detached/SSH sessions**: `_axonosSessionOwnsServerSlot()` now returns false while `axonosSessionDetached` (report's proposed one-liner was insufficient — the billing poll keeps `_axgtStatusPollId` set while detached, which would still have fired the release beacon). SSH sessions stay alive via the in-container heartbeat daemon; detached desktops are handled server-side (next item).
  - [x] **Durable heartbeat for detached desktops**: the in-container daemon now heartbeats desktop sessions as well as SSH sessions, so Detach/reload/tab close does not change runtime liveness or billing. Browser loss is not a credit-grace transition; only actual zero credit starts the logical top-up grace. **Needs session-image rebuild** because existing containers do not gain the daemon change.
  - [x] **Wallet switch / sign-out releases non-viewer sessions**: `teardownSessionForWalletChange()` no longer early-returns when no live viewer — owned SSH, detached, and credit-grace state also triggers `UI.disconnect()` (releases the server session) + SSH card hide.
  - [x] **Wizard SSH toggle card dead on repeat opens**: `axonosStartWizard()` re-wired listeners on every open; the click-to-toggle card flipped the checkbox N times per click (even N = visible no-op). All static-DOM listeners now bind once behind `axonosWizardEventsWired`; per-open state syncs (search reset, card visual default, template render) still run each open.
  - [x] **`ssh_enabled` persisted in DB** (`axgt_sessions` column + migration): claim INSERT stores it; owned-claim returns SSH connect fields from the STORED flag (stale localStorage SSH toggle can no longer mint an ssh:// string for a desktop container); `session_status` returns `owner_ssh_enabled` + `owner_remaining_seconds` + ssh host/port/user + per-session `ssh_enabled`. Client restores the SSH card on reload (`axonosRestoreSshSessionUi`), and the dashboard renders "Direct SSH Session ⌨️ / SSH details" instead of a doomed "Open desktop" for headless sessions (fixes the unresponsive Open-desktop symptom).
  - [x] NOT a bug (report claim 5): the SSH "End session" button path — `UI.disconnect()` already resets `axonosSessionDetached` before routing UI (ui.js:3086), so no stale-detach fix was needed. Dashboard `axonosEndSession` DID leak that state; it now clears detached/SSH client state after release.
  - [x] **Sidebar telemetry was `Math.random()` and storage was hardcoded ("100 GB"/12%)** — now real: GPU/VRAM from `/api/public/telemetry/live` (nvidia-smi cache) filtered to the session's assigned GPUs; CPU/RAM/storage from new authenticated `/api/files/stats` (file_agent reads its own cgroup v2/v1 + `shutil.disk_usage`; new `stats` route in `file_transfer.ROUTES`, served by both gates). Rows show "—" when a source is unavailable (e.g. old container image without /stats) instead of fake numbers. Poll relaxed 3s → 5s.
  - [x] Copy updated to match new semantics (ticker, SSH card "keep this tab open" note, detach confirm, wallet-dialog hints).
  - [x] Verified: 220 gate tests pass; inline JS + ui.js syntax-checked (node container); end-to-end vs throwaway Postgres: legacy-schema migration, SSH field round-trip, zero-credit grace retains the container, release ends retained sessions, preserve-off still ends; file_agent `/stats` HTTP round-trip (403 without key, real cgroup values with key). **Needs session-image rebuild** (file_agent /stats and durable heartbeat) + gate rebuild to deploy.
- [x] **"Credits disappearing" root-cause fixes (2026-07-09, follow-up to tester ledger audit)** — ledger showed NO leak: tester's 3× "100 AXGT" top-ups credited 23.24/23.24/25.01 min (dynamic Model-B pricing) while the UI promised 60–75, then normal 1 credit/min session billing consumed them. Three fixes:
  - [x] **Live pricing everywhere the UI quotes AXGT/ETH→minutes**: `get_credit_policy()` (`axgt_verifier.py`) now computes `min_axgt_deposit_minutes` / `min_eth_deposit_minutes` from the price oracle when `AXGT_DYNAMIC_PRICING` is on (None when the oracle is down — never the legacy 60/100 rate); wizard pay calculator (`axonosUpdatePayCalculator`) had HARDCODED fantasy rates (100 AXGT → 75 credits shown, 23 credited; USDC ×120 vs real 60) — now uses config rates for fixed rails and a debounced `/api/discount/quote?currency=…&base_amount=…` live quote for dynamic rails, '—' until it lands; killed the "(100 AXGT → 60 min typical)" fallback copy + null-safe min-deposit labels ("desktop time at the live AXGT rate").
  - [x] **Storage billing anchored to real elapsed time** (`session_launcher_service._run_volume_cleanup`): was charging a fixed `interval/3600` hours per sweep — the sweep 30s after every launcher restart billed a full hour. Now bills `now − max(last volume_billing_daemon ledger charge, volume CreatedAt)` (7-day clock-skew cap, <60s rides over, legacy one-interval only when no anchor exists). Mocked 7-scenario test: restart-no-charge, 1h, 5h catch-up, first-charge-at-creation, recreated-volume re-anchor, no-anchor legacy, 30d→7d cap.
  - [x] **SSH hard-cap ceiling 1440 → 240 min** in `.env` (+ env.example rationale): SSH sessions now survive tab close, so the ceiling is the main bound on a forgotten browser-launched SSH session; 240 caps the worst case at 4h × GPUs instead of a full day's balance. Takes effect on gate restart; applies at claim time to NEW sessions.
  - [x] Verified: 220 tests pass, inline JS syntax-checked, policy exercised in all 3 modes (fixed 60 / oracle 23.25 (= tester's real 23.24) / oracle-down None).
- [x] **SSH hard cap made renewable (2026-07-09, follow-up: flat 240-min ceiling would kill legitimate >4h jobs)** — cap stays an anti-drain bound but now renews on explicit or presence signals; abandoned sessions still expire:
  - [x] **Owner re-claim renews the cap** (`try_claim_session` owned path): `hard_expires_at = max(current, now + min(affordable_now, ceiling))` — extend-only (never shortens, never caps an uncapped session). Gives browsers an Extend button and agents/x402 a working "pay more, run longer" flow (a repeat `/api/x402/session` payment now extends instead of being dead weight).
  - [x] **Deadline surfaced end-to-end**: `hard_cap_remaining_seconds` in claim/status (`owner_hard_cap_remaining_seconds`)/heartbeat responses; SSH card shows the REAL deadline ("Session ends in ~Xh Ym — renews while you're connected…", amber ≤30 min) instead of the misleading sliding idle TTL, updates live from heartbeats, and has an **Extend session** button (re-claim; card + CSS in vnc.html/axonos-theme.css, wiring in ui.js).
  - [x] **Presence-based auto-renewal**: `session_heartbeat_daemon.py` reads /proc/net/tcp{,6} for ESTABLISHED :22 connections and sends `ssh_active` with each heartbeat; both gates pass it through; `heartbeat(ssh_active=)` slides the cap (same extend-only min(affordable, ceiling) rule) — interactive users never think about the cap, disconnected batch jobs keep their runway and renew via Extend/re-claim. Spoofing only lets an owner keep paying for their own session; billing exhaustion remains the backstop. **Daemon change needs session-image rebuild**; old daemons simply never renew (fixes 1–2 still work).
  - [x] Tests: fixtures updated for the widened heartbeat SELECT + new `test_heartbeat_ssh_active_renews_hard_cap` (renew / no-presence-no-move / uncapped-stays-uncapped); 221 passing. End-to-end vs throwaway Postgres: re-claim 1h→4h, extend-only 10h untouched, presence slide, desktop unaffected, status field, affordability bound (15-min wallet → 15-min renewal).
  - [x] **Wizard-launched SSH card was invisible** (found while answering "does it reveal the ssh command?"): a granted SSH claim rendered the connect-string card into the LANDING screen section while the dialog was still in the wizard screen state (`.axonos-landing-main` display:none) — almost certainly the tester's "ssh enable is not working" screenshot. `showAxonosSshCard` now forces `axonosUpdateActiveScreen('landing')` whenever the card is shown. Also verified `AXGT_SSH_PUBLIC_HOST=axonconsole.io` resolves to 206.41.207.99 (clu1 direct) — connect-string host is correct.
- [x] **x402 SSH-session audit of the last two commits (2026-07-09)** — verified `/api/x402/session` against cbd4c13 + 535cc54; found and fixed two gaps:
  - [x] Audit result: cbd4c13 (WebRTC/UI) is browser/desktop-only — zero agent impact. 535cc54 is compatible-to-beneficial for agents: `out = dict(claim)` passes the new `hard_cap_remaining_seconds`/`ssh_enabled` fields through additively; repeat x402 payments now EXTEND the running session's cap (was dead weight); 240-min ceiling only binds >240-min balances and renews on re-claim/presence; old daemon images simply send no `ssh_active` (no renewal, no breakage). Verified prod gate runs the new code (live oracle config value 24.43, migration applied, 0 in-flight sessions at deploy).
  - [x] **Migration backfill**: rows predating the `ssh_enabled` column got the FALSE default — an SSH session active across a future upgrade would lose connect-string recovery and cap renewal. `_ensure_tables` now backfills `ssh_enabled = TRUE WHERE hard_expires_at IS NOT NULL` (hard cap was only ever set for SSH claims) when the column is first added.
  - [x] **Credit-grace SSH reconnect was broken for both rails**: restoring access returned no SSH fields (an agent re-claiming after top-up lost its endpoint) and the browser path blind-called `tryConnectAfterClaim()` → desktop connect against a headless container. Reconnect now renews the cap (extend-only), returns `ssh_*` + `hard_cap_remaining_seconds`, and routes SSH sessions to the connect-card restore.
  - [x] Verified: backfill (capped row → TRUE, desktop row → FALSE), SSH resume returns port/host + 15min→4h cap renewal, desktop resume shape unchanged; 221 tests pass; JS syntax clean.

## Direct file plane on clu1:443 (2026-08-14) — DEPLOYED & WORKING

Root cause chain for "slow uploads" (112MB @ ~200KB/s from Kolkata, 400Mbps line):
1. ~~1MB sequential upload chunks~~ → adaptive 4–256MB + XHR progress (813a5cc)
2. ~~OKE↔clu1 NetBird link relayed over TCP~~ → P2P via WG port 41000 (see memory)
3. ~~"OCI ingress upload throttling"~~ — WRONG THEORY. Actual root cause:
   **nginx HTTP/2 request-body flow control** caps each h2 stream's upload
   window at 64KB → throughput ≈ 64KB/RTT ≈ 280KB/s at 230ms RTT. Explains
   every observation: OKE uploads 1.6Mbps but downloads 26Mbps (response
   direction has no such window), scp 6.3MB/s (no h2), single-stream
   speedtest 90Mbps (not nginx-h2). Fix on the direct plane: files vhost is
   deliberately HTTP/1.1 (TCP window governs). Local full-chain PUT
   (443→demux→gate→agent): 16.8MB/s.
4. files-tls demux shares public TCP 443 (ssl_preread: TLS→gate /api/files/*,
   plain TURN bytes→coturn), frontend auto-discovers via /api/public/files-config,
   falls back to same-origin. axonconsole.io A-record already → 206.41.207.99.

Deployed 2026-08-14 (cert issued manual DNS-01, expires Nov 12). Bugs found
during rollout, all fixed: entrypoint skipped template render under command
override (wait moved to /docker-entrypoint.d), duplicate CORS header (gate
already does CORS — nginx side removed), JS read wrong token global
(verifiedWalletAuthToken), gate success-relay lacked CORS headers (only its
error paths had them).

Remaining follow-ups:
- [ ] Cert renewal ~mid-October (manual DNS-01 TXT dance; LE emails warn at
      ~day 70). GoDaddy API is tier-blocked (token authenticates but domain
      list empty / ACCESS_DENIED — small-account restriction). For automation:
      move axonconsole.io NS to Cloudflare free (certbot-dns-cloudflare,
      non-expiring token) or Expedient ticket for TCP 80 (HTTP-01, no creds).
      Revoke the useless GoDaddy PAT + delete ~/godaddy-token.txt.
- [ ] Tell devops the real OKE finding: h2 upload flow-control window, not LB
      shape — same-origin fallback uploads stay ~1.6Mbps for far users until
      the ingress disables h2 for /api/files/ or bulk traffic uses the direct
      plane (now default).
- [ ] Optional: gate relay ceiling is ~16MB/s (forked Python proxy) — fine for
      now; revisit if multi-user concurrent uploads saturate it.

## Auth-token expiry dead-ends after session end (2026-08-14) — FIXED, pending deploy

Report: after credit-exhausted top-up grace kicked in, "Use test credit" and
End session both failed with "Valid auth token required" (401 loops in
console). Not whitelist-specific: the 5-min wallet auth token only rotates via
wallet-status in its last 60s, and the only continuous wallet-status poller
(sidebar telemetry loop) stops with the VNC viewer — so the token died ~5 min
after the viewer disconnected and every authenticated route 401'd.

Fixes (all in novnc-theme/vnc.html, contract tests in
test_frontend_detached_restore.py::test_expired_auth_token_recovers_without_dead_ends):
1. 401 auto-recovery: shared axonosRecoverWalletAuthToken (runVerify
   releaseOnly — token only, no claim/resume; preserves prior identity if the
   signature is declined). Used by test-credit (one replay), deposit-verify
   poll (restarts poll; replay-protection makes re-checking the tx hash safe),
   and the release banner (auto-retries the confirmed End after re-auth;
   sessionMismatch still needs the explicit button).
   axonosReauthenticateWalletForSessionRelease now delegates to the helper.
2. Keep-alive: 60s wallet-status poll while signed in, skipped when hidden,
   when the 5s telemetry loop runs, or after the token is known-dead (single
   401 marks it; per-action recovery mints the replacement). wallet-status
   consume_usage is a no-op (billing is heartbeat-based) — polling is
   billing-safe.
3. axonosFetchWalletAccessStatus now adopts rotated auth_token into
   window.verifiedWalletAuthToken (was cookie-only; the JS copy is the only
   credential on the cross-origin file plane).

- [ ] Deploy: vnc.html is inline-script only (no ui.js change, no version bump
      needed); restart/redeploy the gate static serve on clu1.

## "Deposit not credited" after in-wizard AXGT payment (2026-08-19) — FIXED, pending deploy

Report (wallet 0x34d9…4eca, 2026-08-18 ~16:13 UTC): paid 400 AXGT in the new-
session wizard, then "cannot reduce storage from 200 to 100 GB", bounced to
the main screen, deposit looked lost. Ledger truth: the deposit WAS credited
(+50.445 min, correct at the live oracle price +25% bonus) and the 16:17 retry
launched session 333, which consumed all of it — nothing to refund.

Root cause chain (all confirmed in code):
1. The wizard's storage-floor fetch silently degraded (wallet-status without
   capacity fields / floor keyed to '' when wizard opened pre-connect), yet
   axonosApplyWizardStorageContext stamped the floor "resolved" at 10 GB.
2. The post-payment auto-claim fabricated an explicit requested_storage_gb=100
   from the slider default; the gate's growth-only guard rejected it (HTTP 200,
   granted:false, error_code storage_below_provisioned) with NO server log.
3. The structured recovery in handleSessionClaimDenied refused to apply (floor
   keyed to the wrong wallet) → generic "Cannot connect" overlay; the
   "Credited 50.4 min." dialog had been closed ~1s earlier by the auto-claim.

Fixes:
- session_manager.billing_context_for_wallet always emits minimum_storage_gb
  (10 when no volume) on successful queries → clients can tell "no volume"
  from "degraded context"; both storage claim rejections now log WARNING.
- vnc.html: floorResolved only set when a capacity field actually arrived;
  wizard-open/step-3 handlers discard unavailable/non-2xx wallet-status;
  axonosRequestedStorageGbForClaim returns null (claim OMITS storage — server
  preserves the volume via the existing max() clamp) when the floor is not
  resolved-and-keyed, unless the user edited the slider this wizard;
  handleSessionClaimDenied re-keys the floor before applying the rejection's
  storage context so recovery always lands in wizard step 2; warning copy now
  says the setting was corrected and the credit balance is unaffected.
- ui.js claim builder mirrors the omit-on-null contract and passes the
  claim-time wallet into the denial handler (a denial that outlives a wallet
  switch must never re-key the new wallet's floor/preferences — found by
  adversarial review); recovery runs only while the claim wallet is current.
- When the floor is unknown the claim falls back to the wallet's saved
  preference before omitting (so a degraded status doesn't shrink the volume
  a fresh wallet's UI promised); step 3 re-keys a ''-keyed floor (wizard
  opened pre-connect no longer leaves Launch a silent no-op) and shows the
  balance rail as "—" on degraded status.
- Tests: test_access_and_billing.py (explicit 10 GB floor, WARNING on reject),
  test_frontend_detached_restore.py (resolved-only-with-capacity, omit-on-null
  in both claim builders, recovery re-key + identity guard, preference
  fallback, split copy). Full suite green (558 + 205 subtests).

- [ ] Deploy: rebuild the axonos image (vnc.html + ui.js + session_manager.py
      are baked in) and restart the gate container.
- [ ] Optional follow-up: consider clamping explicit shrink requests up
      server-side (guard becomes informative note) — launcher-safe per review,
      but changes an intentional tested contract; decide separately.

## Web terminal: show AxonOS banner (2026-08-19)

- Root cause: the browser terminal execs `bash --login` on a raw PTY
  (terminal_agent.py), bypassing sshd/PAM, so pam_motd never prints /etc/motd;
  real SSH logins got the banner via the PAM session stack.
- Fix: new scripts/axonos-motd-profile.sh installed by the Dockerfile as
  /etc/profile.d/99-axonos-motd.sh — prints /etc/motd only for interactive
  login shells with a tty and no $SSH_CONNECTION (so SSH doesn't double-print
  and `bash -lc` launchers / supervisord `su -` services stay unchanged).
- Verified the guard in all four scenarios (web-terminal-style interactive
  login PTY prints; SSH-env, non-interactive `-lc`, and no-tty all silent).
- Updated `scripts/axonos-motd` to separate Session State & Storage descriptors (~ home, headless) from verified executable CLI commands on PATH (nvidia-smi, nvcc, mpirun, python3, jupyter-lab, gmx, pw.x, nextflow, ipfs, etc.).
- [ ] Deploy: rebuild the axonos image (profile.d snippet and updated /etc/motd are baked in).

## Sidebar mode-swap button (2026-08-19)

- New sidebar button below End session: "Swap to Console" on desktop sessions,
  "Swap to Desktop" on web-terminal (SSH console) sessions; label follows
  UI.connectionKind via updateAxonosSwapButton() (refreshed with the other
  session control buttons).
- UI.swapSessionMode() (ui.js): mode is baked into the container runtime digest,
  so a swap = confirmed release + fresh claim with the opposite requested_ssh
  (home volume + credits are wallet-keyed and carry over). Intent
  (window.axonosSshEnabled + toggle) is set BEFORE the release so both racing
  claim builders read the new mode; an unconfirmed release aborts the swap and
  restores the previous intent; relaunch goes through UI.connect. Desktop→
  console requires a valid saved SSH pubkey up front.
- No backend changes (reuses /api/session/release + /api/session/claim on both
  gates). ui.js cache token bumped to 20260819b; webrtc module token aligned
  (both lockstep tests were already stale at HEAD and are updated).
- Tests: FrontendModeSwapContractTests in test_frontend_terminal.py; full suite
  green (561 + 205 subtests).
- Hot-deployed vnc.html + ui.js into the running gate container
  (/usr/share/novnc) for immediate testing.
- [ ] Deploy: image rebuild still pending (motd snippet, vnc.html, ui.js).

## Session shell Python interpreter (conda shadowing)

- [x] Fixed: the Miniconda prefix installed for PyMOL was prepended to PATH via
      `/etc/profile.d/conda.sh` and `~/.bashrc`, so `python`/`python3` in the session
      terminal resolved to the conda interpreter (3.14, no torch) instead of
      `/usr/bin/python3` (3.10, torch 2.3.1+cu121, CUDA available) — contradicting the
      MOTD's "python3 = PyTorch CUDA runtime" line. PyMOL never needed the PATH entry;
      it is reached through the existing `/usr/local/bin/pymol` symlink.
- [ ] Requires an image rebuild to take effect for newly provisioned homes.
- [x] Startup-time sweep added to `startup.sh`: the wallet home volume mounts over the
      image's `/home/aXonian`, so a rebuild can never reach already-provisioned homes.
      The sweep runs as root after the mount and before any user shell, so such homes
      (including any restored from a backup) self-heal at launch. Anchored to the exact
      line the image wrote, so a user's own conda setup is untouched; idempotent.
- [ ] All 23 live volumes are currently clean; 19 `-backup` volumes still carry the
      line, but the startup sweep now covers them if restored. Remove the sweep once no
      such volume remains.

## PyTorch stack version drift

- [x] Pinned torch/torchvision/torchaudio in the Dockerfile. The index URL alone
      only fixes the CUDA channel, so an unrelated rebuild silently moved the
      stack 2.3.1 -> 2.5.1 (torchvision 0.18 -> 0.20.1) with no code change and
      no signal in the build log. Pinned to the versions now running.
- [x] Smoke-tested 2.5.1 on GPU before pinning: device matmul, cuDNN 9.1 conv +
      backward + optimizer step, and fp16 autocast all pass.
- [x] Updated the advertised version in the MOTD and the landing-page template
      card from "PyTorch 2.3+" to "2.5+". The CUDA 12.1 claim is correct (the
      wheels are cu121 on the 12.2 runtime base).
- [ ] Revisit the pin deliberately when a newer stack is wanted, re-running the
      GPU smoke test before moving it.

## Session survival across control-plane redeploys (2026-08-19)

Recreating the gate container ended every live session ~120s later: the stale
sweep compared `last_heartbeat` to wall-clock, but in-container heartbeat
daemons post *through* the gate, so a redeploy made healthy sessions look dead.
Observed on session 362 — container and daemon were fine, gate was absent.

Done:
- `axgt_gate_liveness` table; gate stamps presence every 15s
  (`AXGT_GATE_LIVENESS_INTERVAL_SECONDS`).
- Staleness now measured in gate-observed time: the predecessor gap is
  measured once at startup (`prime_gate_liveness`) via an atomic
  claim-and-read (upsert + RETURNING pre-update snapshot), published to
  `axgt_gate_absence` so BOTH API server processes inherit it, and credited
  in full to the heartbeat cutoff. Credit lifetime is bounded by the
  uptime>timeout branch (~120s of process life), after which genuinely dead
  sessions are reaped normally. Expiry branches (`expires_at`,
  `hard_expires_at`) deliberately unchanged — a session that genuinely runs
  out mid-redeploy still ends.
- Three defects found only by live testing (killed sessions 369, 370): the
  primer was wired into gate_server only while websockify_gate runs the sweep
  (heartbeat daemons post to :6080); read-then-stamp raced the peer's
  stamping thread and erased the gap; an uptime clamp at prime time floored
  every measured credit to ~0 (and a "stamped recently" shortcut zeroed it
  again at sweep time). Verified end-to-end 2026-08-19: session 371 survived
  a 150s gate outage, 180s credited, heartbeats resumed cleanly.
- Viewer recovery: unclean RFB drops now reconnect instead of dropping to the
  landing screen (the old `else if` made 1006 unreachable); exponential backoff
  2s→20s capped at 12 attempts, shared by the WebRTC path; wallet preflight
  runs without `requestPermission` on recovery so an outage no longer triggers
  a wallet popup.

Follow-ups (not done):
- `test_frontend_terminal.py::test_frontend_module_cache_tokens_stay_in_lockstep`
  asserts a hardcoded CSS token (`20.2&t=20260812b`) that no longer exists —
  pre-existing failure, unrelated to this work.
- Gate is still a single point of failure for the viewer: ~40s from container
  start to serving, and the ingress has one upstream with no failover. Sessions
  now survive it, but the user still sees a reconnect. A second gate instance
  would be needed to make redeploys invisible.

## Post-migration wallet volumes: root-owned home (2026-08-21)

- Fixed: fresh loop-ext4 volumes were mkfs'd root:root and Docker skipped its
  skeleton copy-up (lost+found makes the volume look non-empty), so wallets
  onboarded after the loop-ext4 migration got an unwritable $HOME — xfce4 and
  jupyterlab crash-looped and the desktop streamed black. mkfs now passes
  -E root_owner=1000:1000; startup.sh self-heals already-broken volumes.
- Repaired by hand: the two affected volumes (0x34d9…4eca, 0x1e87…7df).
- The startup.sh self-heal ships in the image: it takes effect only after an
  image rebuild + redeploy. Until then, new wallets are covered by the mkfs
  fix (gate-side, live on restart) but a volume broken in the interim would
  need the manual chown.
- Copy-up no longer populates home skeleton on new volumes (only .bashrc/
  .profile/.bash_logout restored from /etc/skel); check whether anything else
  in the image home (Desktop dirs, .vnc, .config defaults) is actually needed
  first-run or is created on demand by the session.

## Wallet-free demo sessions (guest mode) (2026-08-27)

Wallet connect was both identity and payment, so a sales prospect could not see a
desktop without first installing a wallet, funding it, and signing. Demo sessions
now launch from an invite link with no wallet at all, and the wallet becomes an
upsell afterwards. Whole feature behind `AXONOS_GUEST_MODE_ENABLED`, off by
default; catalog browsing is unchanged.

- Identity is a synthetic EVM-shaped address with a reserved `0x6775657374…`
  ("guest") prefix, not a `guest:<uuid>` string. `validate_wallet_address()` is a
  hard `^0x[a-fA-F0-9]{40}$` and runs before the auth-token check at ~53 call
  sites across both gate servers, the auth-token table is NOT NULL, and sessions
  are keyed by wallet — so reusing the address shape kept the token, session,
  heartbeat and expiry machinery entirely unchanged. The alternative (nullable
  wallet column) would have meant relaxing the one validator that also guards
  every wallet-gated endpoint. The prefix makes guest-ness decidable offline,
  with no DB round trip, which matters because the deny-list checks run in hot
  paths and inside the proxy gate, which has no Flask context.
- The namespace is closed at the door: both SIWE routes refuse guest-shaped
  addresses, so no signature can ever mint a token for one.
- Minting is authorized by the operator's invite-minter list, which defaults to
  the existing test-credit wallet list — not by `AXGT_ADMIN_SECRET`. The people
  who hand a prospect a demo should not hold a credential that also unlocks the
  ledger and telemetry APIs, and because the minter is signed in every link is
  attributable to a wallet. Deliberately does NOT call `is_wallet_whitelisted()`,
  which returns False when `AXONOS_TEST_CREDITS_ENABLED` is off and would have
  silently coupled demo mode to an unrelated feature. The admin route and
  `scripts/guest_invite.py` remain for hosts with no signed-in wallet.
- **Quotas live on the sponsor, not the identity.** Each redemption mints a NEW
  identity with its own fresh ledger row, so the test-credit balance cap — which
  bounds a single identity — cannot bound a member who mints many links. Without
  a sponsor quota one wallet could light up the whole fleet. Enforced as a daily
  mint cap and, more importantly, a concurrent-live-demo cap checked at both
  redemption and the final locked claim. The latter closes delayed/replayed
  identity loopholes; different invite links from one sponsor serialize on the
  same quota lock.
- Guest credit reuses `credit_test_grant` with parameterised provenance rather
  than a forked function (the fork was ~170 lines duplicating its advisory-lock
  and replay logic). Demo minutes are written as `guest_credit` on a `guest` rail,
  so free compute handed to a prospect stays distinguishable from a team member's
  own test credit. Demo minutes are NOT drawn from the sponsor's balance: that
  would kill a prospect's session when the rep spent their own credit.
- Invite-only, never self-serve. `axgt_guest_invites` stores **only**
  `sha256(token)` — the token is a bearer credential in a URL, shown once at mint
  time. Redemption is one transaction under an advisory lock per invite:
  revoked/expired/exhausted are refused, and an invite that already has a live
  demo (or a just-issued identity whose claim is still in flight) is refused, so
  one invite means one concurrent session and a double-click cannot open two.
- Redemption setup is retry-safe. A non-secret per-tab `attempt_id` identifies
  the one use already consumed by that browser, so a transient ledger/auth error
  retries the same synthetic identity instead of burning a single-use sales link.
  Large/high-GPU grants are split into independently idempotent chunks below the
  ledger's per-grant ceiling.
- Demo minutes are real ledger credit (`guest_credit` provenance), sized *above*
  the wall-clock cap on purpose. If credit ran out first the session would enter
  the credit-grace path and hold a GPU for the grace TTL; making the hard cap the
  binding limit means a demo ends by teardown. `_preserve_for_wallet()` also
  disables credit-grace for guest identities outright.
- The time cap is written to BOTH expiry columns. `hard_expires_at` reuses the
  existing non-sliding column (the expiry sweep was already generic rather than
  SSH-specific, and every renewal path is SSH-gated, so the cap cannot be
  extended by re-claiming or reloading). But that branch fires at
  `hard_expires_at + session_grace_seconds()`, so a grace period longer than the
  demo would have let a 30-minute demo run for the full grace — the subtraction
  floors at zero. `expires_at` is compared with no grace allowance, so it pins
  the exact deadline whatever the grace is set to; the heartbeat's idle-TTL slide
  is clamped to it so a demo cannot walk its deadline forward.
- Demos get no persistent volume: an `ephemeral_storage` flag threads claim →
  launcher and skips the mount, so no ext4 image or capacity row accumulates per
  invite. The launcher service derives it from the identity as well as the flag,
  so a dropped flag cannot create a volume. It participates in the runtime
  config digest (storage topology is part of what makes a container reusable) but
  is added to the payload only when set — a digest change means `docker rm -f`,
  so a blanket change would have torn down live sessions on the first claim after
  deploy.
- Frontend: `?invite=` is captured synchronously and stripped from the URL before
  remembered-wallet paint or asynchronous restore can start
  (the single-use invitation secret must not sit in history, a Referer, or any
  browser store). Identity, deadline, allowlists, and the separate short-lived
  guest auth bearer are kept in sessionStorage — per-tab and never localStorage.
  Reload revalidates that identity-bound bearer while retaining the HttpOnly
  cookie as a same-identity compatibility fallback. This avoids the origin-wide
  cookie collision where a wallet or second demo tab could strand the first demo;
  guest RFB reconnects likewise force the tab bearer in default cookie mode. The
  proxy access-log scrubber redacts both `invite` and query-mode `auth_token`
  capabilities while retaining ordinary query diagnostics. The
  countdown banner is fed by `guest_remaining_seconds` from claim/status/
  heartbeat rather than a purely local timer. Both gate implementations avoid
  writing or clearing the shared cookie on guest redemption/status (an older
  guest cookie remains accepted once for migration), and guest token rotation
  preserves the absolute demo-plus-API-grace lifetime. Tab-close release keeps
  its fetch header and gives the sendBeacon fallback the guest bearer only in
  the JSON body; both release handlers accept that field only for a guest-shaped,
  identity-bound token, so paid-wallet auth is unchanged and no URL/log secret is
  introduced. A warning fires ~5 min out
  (`AXONOS_GUEST_WARN_MINUTES`, clamped below short demos) while the prospect is
  still in the desktop, then the expiry upsell offers wallet connect.
- Demo entry takes precedence over any wallet remembered by the same browser.
  Old-wallet status, paused-session and provider reconnect probes are skipped
  while guest entry is pending/active, and stale async results cannot overwrite
  the demo. After the prospect chooses an allowed environment and hardware tier,
  Step 2 launches directly without showing the payment step. Disallowed choices
  are hidden/disabled client-side and still rejected authoritatively by claim.
- The single highest-risk line: `axonosEnsureWalletSessionCurrent` needed a guest
  short-circuit. It asks the wallet provider for `eth_accounts` and fails closed
  when there is none — without the short-circuit no demo claim can ever succeed.
  That short-circuit then created a regression, caught in review: connecting a
  wallet DURING a demo left the guest flag set, so the preflight short-circuited
  for a PAID session (bypassing the wallet-drift check that is its sole gate),
  and the demo clock later cleared that wallet's credentials mid-session. Fixed
  in both directions — a wallet sign-in ends the demo before taking the globals,
  and clearing a demo only tears down the shared globals when the demo is still
  the live identity.
- The SIWE namespace guard fails closed. The reserved prefix is 10 hex nibbles =
  40 bits, which is within reach of vanity mining (hours on a GPU rig), so the
  refusal at `/api/auth/challenge` and `/api/auth/verify-wallet` is load-bearing
  rather than defence-in-depth. Both gates mirror the prefix locally so an
  unimportable `guest_mode` cannot silently reopen the namespace.
- Surfaces: `POST /api/auth/guest-invite` (signed-in team member, the everyday
  path), `/api/admin/guest-invite{,/revoke}` and `/api/admin/guest-invites`
  behind the admin secret, plus `scripts/guest_invite.py --sponsor` for hosts
  with no signed-in wallet.
- Template IDs are canonicalized against one backend catalog before any claim DB
  or launcher side effect, on paid-wallet and guest paths alike. The catalog is
  regression-checked against both the frontend cards and runtime apply script.
- Revocation is serialized with redemption, expires issued-but-unclaimed guest
  identities, and shortens live session deadlines in one transaction. After the
  commit it invokes the ordinary exact-session release path so Docker teardown
  happens before the GPU is exposed; failed external cleanup remains durably
  closed for the periodic reconciler.
- Expired per-demo identity and credit rows are reaped in bounded, skip-locked
  batches after session-expiry hooks. Retention defaults to 30 days via
  `AXONOS_GUEST_DATA_RETENTION_DAYS`; `0` keeps rows indefinitely. Invite and
  generic session history remain for audit.

Follow-ups (not done):
- Demo expiry ends the container, so the upsell offers a *fresh* session. Letting
  a prospect keep the same desktop by connecting a wallet would need an
  ownership-transfer mechanism, since sessions are keyed by wallet.
