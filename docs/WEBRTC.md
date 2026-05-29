# WebRTC remote desktop (AxonOS)

AxonOS can deliver the GPU desktop over **WebRTC** (low-latency video + optional data channel input) instead of or in addition to **noVNC over WebSockets**. Signaling and ICE configuration are served from the same process as the wallet/session APIs; the streaming agent runs **inside the desktop container** and talks to the local gate on `127.0.0.1`.

## Feature flags

| Variable | Meaning |
|----------|---------|
| `WEBRTC_ENABLED` | If true, the UI attempts WebRTC after a successful session claim. |
| `WEBRTC_FALLBACK_ENABLED` | If true (default), failure to negotiate WebRTC falls back to classic noVNC. If false, the user sees an error when WebRTC fails. |

## Required secrets

- **`WEBRTC_AGENT_INTERNAL_KEY`**: Long random string shared only by the gate (Flask + websockify handlers) and the `webrtc-agent` supervisord process. Never commit this value. Unauthorized callers cannot claim offers without it.

## ICE / STUN / TURN

- **`WEBRTC_STUN_URLS`**: Comma-separated `stun:` URLs (optional; a public default may apply if unset).
- **`WEBRTC_TURN_URLS`**: Comma-separated `turn:` / `turns:` URLs.
- **`WEBRTC_TURN_USERNAME`** / **`WEBRTC_TURN_CREDENTIAL`**: Optional; only applied to TURN entries (never logged by the server in ICE config responses).

Users behind **symmetric NATs** or **strict firewalls** often need a **TURN** server reachable on UDP/TCP as configured. For production, run your own coturn (or a managed TURN) and allow the browser to reach it on the published host/ports.

## Reverse proxies and ports

- Browsers load noVNC from **`/vnc.html`** on the **websockify** port (default `6080` in compose).
- The gate API used by tunnels may be on **`8889`** (mapped in `docker-compose.yml`).
- WebRTC media is **peer-to-peer** (or via TURN); the HTTP signaling endpoints are on the **same origin** as the page users load (typically `:6080`). Ensure your proxy forwards:
  - `GET`/`POST` under `/api/webrtc/*`
  - Standard session/auth APIs used before connect

For **WebSocket-only** proxies, signaling still uses **HTTPS fetch** on the same host; no separate WebSocket is required for the MVP negotiation path.

## Session security

- Signaling **create/offer/status/ice** require a valid **AXGT auth token** and **session ownership** (`POST /api/session/claim` succeeded for that wallet).
- WebRTC session IDs are **random 256-bit** tokens stored in Postgres; they are not derivable from the wallet address.
- The agent only processes offers after the gate atomically marks a row; another user cannot steal a session without the token and wallet auth.

## Observability

- Logger name **`axonos.webrtc`** emits negotiation and lifecycle lines (`webrtc_negotiation_*`, `webrtc_agent_answer`, `webrtc_fallback_novnc`, etc.).
- Clients may `POST /api/webrtc/metrics` with RTT / packet loss (best-effort; used for operations dashboards later).

## Docker Compose test path

1. Add to `.env`: `WEBRTC_ENABLED=true`, a strong `WEBRTC_AGENT_INTERNAL_KEY`, and optional `WEBRTC_STUN_URLS` / TURN credentials.
2. `docker compose build && docker compose up -d`
3. Open `http://localhost:6080/vnc.html` (or mapped port), complete wallet + session claim, launch desktop.
4. Confirm in logs: gate posts answer, agent reports `WebRTC answer stored`, browser shows “Connected (WebRTC)” or falls back to classic stream if configured.

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Immediate fallback to noVNC | `WEBRTC_ENABLED`, Postgres reachable, agent running (`supervisorctl status webrtc-agent`). |
| Stuck on “Connecting” | STUN/TURN reachability; restrictive NAT → configure TURN. |
| 403 on signaling | Auth token or session claim missing/expired. |
| Agent idle | `WEBRTC_AGENT_INTERNAL_KEY` must match between environment for gate and agent. |
| Scroll blur / hazy video | Default H.264 capture is tuned for **lowest-latency 1080p desktop** (`p1`/`llhp`, 12 Mbps, one-frame buffer). If latency is clean, try `WEBRTC_CAPTURE_BITRATE=14000000`; if loss appears, use `WEBRTC_CAPTURE_MAX_WIDTH=1600` or `1280` rather than pushing bitrate higher. |
| Lag / clicks stop / jitter buffer climbs | Path saturated or buffering. Confirm `packetsLost`, `nackCount`, and jitter buffer delay in `chrome://webrtc-internals`. Keep `WEBRTC_CAPTURE_NVENC_PRESET=p1` and `WEBRTC_CAPTURE_LOW_LATENCY=true`, then try `WEBRTC_CAPTURE_BITRATE=8000000` or `WEBRTC_CAPTURE_MAX_WIDTH=1600`; reconnect after deploy and hard-refresh the page. |
| Black screen, ICE connected | In `chrome://webrtc-internals`, if **inbound video codec is VP8** while the agent runs H.264 capture, SDP negotiated the wrong codec. Agent + browser must prefer **H.264** (fixed in `capture.prefer_h264_for_pc` and `axonos-webrtc.js`). Hard-refresh the page after deploy. |
| Two cursors / sluggish clicks | H.264 capture embeds the host cursor; disable the browser overlay with `WEBRTC_LOCAL_CURSOR=auto` (default) or `false`. Click lag from mousemove floods is reduced by server-side move coalescing and client throttling. |
| Multi-second video/input lag | aiortc `MediaPlayer(mpegts pipe)` treated live NVENC as a file and paced frames to timestamps; combined with large `thread_queue_size` this stacked ~10s delay. Fixed via `_throttle_playback = false`, `thread_queue_size=4`, and optional stale-packet dropping (`WEBRTC_CAPTURE_MAX_STALE_FRAMES`, default `1`). Reconnect after deploy. |
| Still frame loss at 1080p30 | Confirm ffmpeg shows the intended `-framerate` and bitrate. If `packetsLost` climbs through TURN, try `WEBRTC_CAPTURE_BITRATE=6000000` to `8000000` and/or `WEBRTC_CAPTURE_MAX_WIDTH=1280`. |

## Capture backends

| Backend | Path | When |
|---------|------|------|
| `nvfbc` | Native NvFBC → NVENC → MPEG-TS → WebRTC H.264 | Best low-latency GPU desktop path when the Capture SDK helper is installed |
| `nvenc` | FFmpeg `x11grab` → `h264_nvenc` → WebRTC H.264 | GPU with NVENC (`libnvidia-encode`); sharp motion, ~100–300 MB VRAM |
| `mss` | Python `mss` → software VP8 (~0.5–1.5 Mbps) | Fallback when NVENC unavailable |
| `auto` | Try NvFBC, then NVENC, else MSS | **Default** |

Set `WEBRTC_CAPTURE_BACKEND=nvfbc` to require the native GPU capture path (falls back with a warning if the helper is missing). Build `tools/nvfbc_nvenc_streamer.c` against the NVIDIA Capture SDK headers and install it at `/usr/local/bin/nvfbc_nvenc_streamer`.

## Input lifecycle validation

Repeated WebRTC session spawn/teardown and teardown mouseup safety are documented in **[WEBRTC_INPUT_VALIDATION.md](./WEBRTC_INPUT_VALIDATION.md)**. Browser console runner:

```javascript
const audit = await import('./app/webrtc/axonos-webrtc-input-validation.js');
await audit.runRepeatedSessionAudit({ cycles: 5 });
```
