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
| Soft / blurry WebRTC video (MSS + VP8) | aiortc defaults to ~500 kbps VP8 (max 1.5 Mbps). Raise `WEBRTC_VP8_MAX_BITRATE` (e.g. `2500000`) on session containers. NVENC H.264 on `feat/webrtc-nvenc-stability` is sharper at 1080p. |

## Input lifecycle validation

Repeated WebRTC session spawn/teardown and teardown mouseup safety are documented in **[WEBRTC_INPUT_VALIDATION.md](./WEBRTC_INPUT_VALIDATION.md)**. Browser console runner:

```javascript
const audit = await import('./app/webrtc/axonos-webrtc-input-validation.js');
await audit.runRepeatedSessionAudit({ cycles: 5 });
```
