# AxonOS noVNC Theme (Web Frontend)

This directory holds the AxonOS browser frontend. It started as a restyled noVNC
login page and has grown into the full production client: landing page,
dashboard, launch/payment wizard, WebRTC streaming client, file-transfer plane,
web terminal, and a public telemetry page. The classic noVNC client is still the
fallback path when WebRTC is unavailable.

The frontend is served by the gate (`axonos_gate/websockify_gate.py` on `:6080`)
from `/usr/share/novnc/` inside the container. Every `/api/...` path it calls is
implemented by the gate.

## What the frontend does

- **Landing page as home** (`vnc.html`): hero "A real GPU desktop, spun up in a
  minute", tagline "GPU-Native Scientific Computing", environment catalogue.
  Wallet connect and page reload both stay on the landing page; the hero CTA
  becomes "Open workspace" once a session exists.
- **Dashboard + Launch Wizard**: Quick Launch grid with a catalogue "View all"
  entry, GPU profile picker, storage sizing, payment rails (USDC / ETH / AXGT)
  with holder-discount quotes from `/api/discount/quote`.
- **Wallet auth**: EIP-6963 provider discovery with a deduplicated picker,
  challenge/verify flow (`/api/auth/challenge`, `/api/auth/verify-wallet`),
  short-lived auth tokens refreshed via `/api/auth/wallet-status`.
- **Guest demo mode**: invite-code redemption and invite generation
  (`/api/auth/guest`, `/api/auth/guest-invite`), enabled when `/api/config`
  reports `guest_mode_enabled`.
- **Sessions**: claim / heartbeat / release / restart / status under
  `/api/session/*`; "Relaunch as Console / Desktop" mode switch from the sidebar.
- **WebRTC client** (`app/webrtc/axonos-webrtc.js`): `/api/webrtc/session`,
  `offer`, `ice`, `status`, `metrics`, `close`. Falls back to noVNC.
- **File plane** (`app/files/axonos-files.js`): `/api/public/files-config` then
  `/api/files/*`, optionally on a separate files origin, with adaptive chunking.
- **Web terminal / SSH** (`app/terminal/axonos-terminal.js` + vendored xterm.js):
  `/api/terminal/ticket`, direct-SSH toggle with public-key entry, host-key
  fingerprint display.
- **Telemetry**: `telemetry.html` and the sidebar read
  `/api/public/telemetry/*`; CPU/RAM/disk come from the authenticated
  `/api/files/stats`. Unavailable values render as "—", never mocked.
- **Compact / mobile view**: browse-only. Launching requires a desktop browser.

Only the desktop layout is supported for launching sessions; breakpoints are at
992px (compact), 768px and 600px (progressive tightening).

## Files

| Path | Purpose |
|------|---------|
| `vnc.html` | Whole single-page frontend (landing, dashboard, wizard, modals, noVNC client). ~745 KB. |
| `ui.js` | Patched noVNC `ui.js` with AxonOS defaults, session/sidebar logic, SSH options. ~256 KB. |
| `axonos-theme.css` | Complete stylesheet ("AxonOS v2" design). ~200 KB. |
| `telemetry.html` | Public live-telemetry page. |
| `app/webrtc/axonos-webrtc.js` | WebRTC streaming client. |
| `app/webrtc/axonos-webrtc-input-validation.js` | Input-validation helpers (see `docs/WEBRTC_INPUT_VALIDATION.md`); not currently copied into the image. |
| `app/files/axonos-files.js` | File upload/download client. |
| `app/terminal/axonos-terminal.js` | Web terminal client. |
| `app/vendor/xterm/` | Vendored xterm.js 6.0.0 + addon-fit 0.11.0 (see its README). |
| `app/fonts/` | BrutalType (5 weights) + Orbitron woff2. |
| `icons/` | 13 PNG sizes (16–192 px), `files.svg`, `novnc-icon*.svg`, `Makefile`. |
| `icon.png`, `axon-x.png`, `images/linux.svg` | Page icon and UI images. |
| `axonos_assistant.png`, `talk_to_k.png` | Desktop pixmaps for the in-session assistants. |
| `descios-icon.svg` | Legacy icon, unused by the page. |
| `install-theme.sh` | Legacy helper; see below. |

## Design tokens

Defined on `:root` in `axonos-theme.css`:

| Token | Value | Usage |
|-------|-------|-------|
| `--axonos-primary` | `#7b6cff` | Primary purple, buttons and accents |
| `--axonos-secondary` | `#8b7cff` | Gradients, secondary emphasis |
| `--axonos-warm` | `#f2c14e` | Warnings and warm highlights |
| `--axonos-dark` / `-2` / `-3` | `#080910` / `#0d0e18` / `#12131f` | Backgrounds |
| `--axonos-light` | `#e9ebf2` | Text on dark backgrounds |
| `--axonos-hover` | `#4fe0c0` | Hover / success feedback |
| `--axonos-shadow` | `rgba(123,108,255,.25)` | Glow shadows |

Override these tokens to recolour the UI. Background motion uses the
`grid-drift`, `rocket-fly-*`, `spaceship-cruise-*`, `planet-float-*`,
`comet-streak` and `satellite-orbit` keyframes, all disabled under
`prefers-reduced-motion`.

## noVNC defaults (`ui.js`)

| Setting | Value |
|---------|-------|
| Resize | `scale` (local scaling) |
| Quality | 9 |
| Compression | 9 |
| Auto-reconnect | off (5000 ms delay when enabled); session resume is handled by the gate |

## Installation

The root `Dockerfile` already installs the theme. The relevant block is:

```dockerfile
COPY novnc-theme/axonos-theme.css /usr/share/novnc/app/styles/
COPY novnc-theme/vnc.html /usr/share/novnc/
COPY novnc-theme/ui.js /usr/share/novnc/app/
COPY novnc-theme/app/fonts/ /usr/share/novnc/app/fonts/
COPY novnc-theme/app/webrtc/axonos-webrtc.js /usr/share/novnc/app/webrtc/axonos-webrtc.js
COPY novnc-theme/app/files/axonos-files.js /usr/share/novnc/app/files/axonos-files.js
COPY novnc-theme/app/terminal/ /usr/share/novnc/app/terminal/
COPY novnc-theme/app/vendor/xterm/ /usr/share/novnc/app/vendor/xterm/
COPY novnc-theme/icons/* /usr/share/novnc/app/images/icons/
COPY novnc-theme/icon.png /usr/share/novnc/icon.png
COPY novnc-theme/images/linux.svg /usr/share/novnc/app/images/linux.svg
COPY novnc-theme/telemetry.html /usr/share/novnc/
COPY novnc-theme/axon-x.png /usr/share/novnc/axon-x.png
```

`install-theme.sh` predates most of these files: it only inserts the CSS,
`vnc.html` and icon COPY lines, so running it against a fresh Dockerfile
produces a broken page. Do not use it; edit the Dockerfile block above instead.

Cache busting is done with query strings on the `<link>`/`<script>` tags in
`vnc.html` (for example `axonos-theme.css?v=...`). Bump them when shipping
frontend changes so browsers pick up the new assets.

## Client-side storage

`localStorage` keys used by the frontend: `axonos_wallet_provider_rdns`,
`axonos_last_wallet`, `axonos_nav`, `axonos_pay_rail`,
`axonosSelectedTemplateId`, `axonosSshEnabled`, `axonosSshPubkey`.

## Licensing

`vnc.html` and `ui.js` derive from noVNC and keep their MPL 2.0 headers.
`axonos-theme.css`, the `app/*.js` clients and `install-theme.sh` are MIT.
xterm.js in `app/vendor/xterm/` is MIT. See `LEGAL.md` at the repo root.
