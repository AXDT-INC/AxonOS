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
- [ ] Phase 2: Frontend Connect Wallet + strict sign-to-verify + status polling/overlay
- [ ] Phase 3: Deployment helpers (`docker-compose.yml`, tunnel helper script) and docs touch-up
- [ ] Phase 4: End-to-end runtime test checklist
