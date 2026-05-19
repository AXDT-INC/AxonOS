# WebRTC input lifecycle — browser validation

Use this checklist before merging changes to `axonos-webrtc.js` or `webrtc_agent_main.py` that touch mouse/input lifecycle.

## Teardown mouseup safety (confirmed)

On disconnect, `axonosWebRtcTeardown` calls `resetMouseInputState(null, true)` **before** aborting listeners and closing the peer connection.

| Path | Coords in `mouseup` JSON | Agent behavior |
|------|---------------------------|----------------|
| User `mouseup` / drag end | Includes mapped `x`,`y` | `mousemove` then `mouseup` — normal |
| `pointercancel` mid-drag | Includes event coords | Same as user release |
| **Session teardown** (`ev === null`) | **Omitted** | `xdotool mouseup` only — **no pointer jump** |
| Data-channel `close` (agent) | N/A | `_reset_mouse_button_state(env)` releases stuck mask in place |

Sending `(0,0)` on teardown was removed intentionally: the agent always `mousemove`s before `mouseup` when coordinates are present, which would warp the remote cursor to the origin on disconnect.

## Semi-automated repeated spawn/teardown audit

**Prerequisites:** wallet verified, `WEBRTC_ENABLED=true`, desktop reachable on `vnc.html`.

1. Open DevTools → Console on the connected session page.
2. Run:

```javascript
const audit = await import('./app/webrtc/axonos-webrtc-input-validation.js');
await audit.runRepeatedSessionAudit({ cycles: 5 });
```

3. For each cycle the runner:
   - Waits until `#axonos_webrtc_video` appears (connect via UI if needed).
   - Asserts exactly one video / paste sink / cursor and teardown hooks present.
   - Dispatches synthetic left + right mousedown/mouseup (must not throw).
   - Waits until you **Disconnect** and DOM nodes are gone.
   - Asserts no leftover WebRTC elements or stale `window.axonosWebRtcTeardown`.

4. Expect `Summary: { passed: true, … }` after all cycles.

### Quick snapshot (manual)

```javascript
const v = await import('./app/webrtc/axonos-webrtc-input-validation.js');
v.captureWebRtcInputSnapshot();
v.describeTeardownMouseupSafety();
```

### Pass criteria

- Every cycle: `errors: []`
- No duplicate `#axonos_webrtc_video` or paste sinks after teardown
- Click-path smoke test completes without `ReferenceError` (regression guard for `clipboardBeforeClickMs` and async mousedown)
- Manual spot-check: after 3+ cycles, left click, right click, and click-drag still work on the remote desktop

## Agent unit tests (CI)

```bash
python -m unittest axonos_gate.tests.test_webrtc_input -v
# or, when pytest is available:
python -m pytest -q axonos_gate/tests/test_webrtc_input.py
```

Includes queue pressure, session mask reset, and `mouseup` without coordinates (no spurious `mousemove`).
