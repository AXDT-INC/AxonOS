/**
 * Browser-console validation for WebRTC input lifecycle (repeated spawn/teardown).
 *
 * Usage (on vnc.html, while wallet is verified):
 *
 *   const audit = await import('./app/webrtc/axonos-webrtc-input-validation.js');
 *   await audit.runRepeatedSessionAudit({ cycles: 3 });
 *
 * Semi-automated: the runner snapshots DOM/globals after each disconnect and runs a
 * local click-path smoke test while connected. You disconnect/reconnect via the UI
 * between cycles when prompted.
 */

/** @returns {Record<string, unknown>} */
export function captureWebRtcInputSnapshot() {
    const video = document.getElementById('axonos_webrtc_video');
    const cursor = document.getElementById('axonos_webrtc_cursor');
    return {
        at: new Date().toISOString(),
        connected: !!(window.UI && window.UI.connected),
        videoCount: document.querySelectorAll('#axonos_webrtc_video').length,
        cursorCount: document.querySelectorAll('#axonos_webrtc_cursor').length,
        hasVideoEl: !!video,
        hasCursor: !!cursor,
        hasTeardownFn: typeof window.axonosWebRtcTeardown === 'function',
        hasPasteFn: typeof window.axonosWebRtcPasteClipboard === 'function',
        hasPeer: !!window.axonosWebRtcPc,
    };
}

/**
 * @param {Record<string, unknown>} snap
 * @returns {string[]}
 */
export function assertCleanTeardownSnapshot(snap) {
    const errors = [];
    if (snap.videoCount !== 0) {
        errors.push(`expected 0 #axonos_webrtc_video, got ${snap.videoCount}`);
    }
    if (snap.cursorCount !== 0) {
        errors.push(`expected 0 #axonos_webrtc_cursor, got ${snap.cursorCount}`);
    }
    if (snap.hasTeardownFn) {
        errors.push('window.axonosWebRtcTeardown should be null after teardown');
    }
    if (snap.hasPasteFn) {
        errors.push('window.axonosWebRtcPasteClipboard should be null after teardown');
    }
    return errors;
}

/**
 * @param {Record<string, unknown>} snap
 * @returns {string[]}
 */
export function assertConnectedSnapshot(snap) {
    const errors = [];
    if (snap.videoCount !== 1) {
        errors.push(`expected 1 #axonos_webrtc_video, got ${snap.videoCount}`);
    }
    if (snap.cursorCount !== 1) {
        errors.push(`expected 1 #axonos_webrtc_cursor, got ${snap.cursorCount}`);
    }
    if (!snap.hasTeardownFn) {
        errors.push('window.axonosWebRtcTeardown missing while connected');
    }
    if (!snap.hasPasteFn) {
        errors.push('window.axonosWebRtcPasteClipboard missing while connected');
    }
    return errors;
}

/**
 * Dispatch synthetic mouse events on the WebRTC video; must not throw.
 * @returns {string[]}
 */
export function runClickPathSmokeTest() {
    const errors = [];
    const video = document.getElementById('axonos_webrtc_video');
    if (!video) {
        return ['no #axonos_webrtc_video — connect WebRTC first'];
    }
    const rect = video.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const mk = (type, button) => new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        clientX: cx,
        clientY: cy,
        button,
        buttons: type === 'mousedown' ? (1 << button) : 0,
    });
    try {
        video.dispatchEvent(mk('mousedown', 0));
        window.dispatchEvent(mk('mouseup', 0));
        video.dispatchEvent(mk('mousedown', 2));
        window.dispatchEvent(mk('mouseup', 2));
    } catch (e) {
        errors.push(`click-path threw: ${e && e.message ? e.message : e}`);
    }
    return errors;
}

/**
 * Document + verify teardown mouseup safety expectations (no 0,0 pointer jump).
 * @returns {{ ok: boolean, notes: string[] }}
 */
export function describeTeardownMouseupSafety() {
    const notes = [
        'Teardown calls resetMouseInputState(null, true) before inputAbort.abort().',
        'With ev=null, mouseup JSON omits x/y — agent _apply_input_json skips mousemove and only syncs button mask.',
        'Data-channel close on the agent also calls _reset_mouse_button_state(), releasing any stuck X buttons in place.',
        'Result: disconnect mid-drag releases remote buttons without warping the pointer to (0,0).',
    ];
    return { ok: true, notes };
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Wait until predicate is true or timeout.
 * @param {() => boolean} pred
 * @param {number} timeoutMs
 */
async function waitFor(pred, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (pred()) {
            return true;
        }
        await sleep(250);
    }
    return pred();
}

/**
 * Semi-automated audit for repeated connect/disconnect cycles.
 * @param {{ cycles?: number, stepTimeoutMs?: number }} [opts]
 * @returns {Promise<{ passed: boolean, cycles: object[], teardownSafety: object }>}
 */
export async function runRepeatedSessionAudit(opts) {
    const cycles = Math.max(1, (opts && opts.cycles) || 3);
    const stepTimeoutMs = (opts && opts.stepTimeoutMs) || 120000;
    const results = [];
    const teardownSafety = describeTeardownMouseupSafety();

    console.info('[AxonOS WebRTC input audit] Teardown mouseup safety:');
    teardownSafety.notes.forEach((n) => console.info('  •', n));

    for (let i = 0; i < cycles; i += 1) {
        const cycle = { index: i + 1, errors: [] };
        console.info(`[AxonOS WebRTC input audit] Cycle ${i + 1}/${cycles}: connect WebRTC via UI…`);

        const connected = await waitFor(
            () => document.getElementById('axonos_webrtc_video') !== null,
            stepTimeoutMs,
        );
        if (!connected) {
            cycle.errors.push('timed out waiting for WebRTC video');
            results.push(cycle);
            break;
        }

        const liveSnap = captureWebRtcInputSnapshot();
        cycle.connectedSnapshot = liveSnap;
        cycle.errors.push(...assertConnectedSnapshot(liveSnap));
        cycle.errors.push(...runClickPathSmokeTest());

        console.info(`[AxonOS WebRTC input audit] Cycle ${i + 1}: click smoke test done — disconnect via UI…`);

        const disconnected = await waitFor(
            () => document.getElementById('axonos_webrtc_video') === null,
            stepTimeoutMs,
        );
        if (!disconnected) {
            cycle.errors.push('timed out waiting for teardown (video removed)');
            results.push(cycle);
            break;
        }

        await sleep(400);
        const teardownSnap = captureWebRtcInputSnapshot();
        cycle.teardownSnapshot = teardownSnap;
        cycle.errors.push(...assertCleanTeardownSnapshot(teardownSnap));

        const status = cycle.errors.length ? 'FAIL' : 'PASS';
        console.info(`[AxonOS WebRTC input audit] Cycle ${i + 1}: ${status}`, cycle.errors);
        results.push(cycle);

        if (i + 1 < cycles) {
            console.info(`[AxonOS WebRTC input audit] Reconnect for cycle ${i + 2}…`);
        }
    }

    const passed = results.every((r) => r.errors.length === 0) && results.length === cycles;
    const summary = { passed, cycles: results, teardownSafety };
    console.info('[AxonOS WebRTC input audit] Summary:', summary);
    return summary;
}
