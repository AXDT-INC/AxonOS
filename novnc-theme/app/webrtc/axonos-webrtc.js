/**
 * AxonOS WebRTC desktop path: tries low-latency peer video + input data channel,
 * falls back to classic noVNC when disabled, unsupported, or on failure.
 */

function _authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (window.verifiedWalletAuthToken) {
        h['X-AXGT-Auth-Token'] = window.verifiedWalletAuthToken;
    }
    if (window.verifiedWalletAddress) {
        h['X-Wallet-Address'] = window.verifiedWalletAddress;
    }
    return h;
}

async function _fetchJson(url, opt) {
    const r = await fetch(url, { credentials: 'include', ...opt });
    const t = await r.text();
    let j = {};
    try {
        j = t ? JSON.parse(t) : {};
    } catch {
        j = { _parseError: true, raw: t };
    }
    return { ok: r.ok, status: r.status, json: j };
}

function _setBanner(text, state) {
    let el = document.getElementById('axonos_webrtc_banner');
    if (!el) {
        el = document.createElement('div');
        el.id = 'axonos_webrtc_banner';
        el.setAttribute('aria-live', 'polite');
        el.style.cssText = 'position:fixed;bottom:12px;left:50%;transform:translateX(-50%);z-index:10000;padding:8px 14px;border-radius:8px;font:14px system-ui,sans-serif;max-width:90vw;text-align:center;';
        document.body.appendChild(el);
    }
    const colors = {
        connecting: 'background:#1e3a5f;color:#e0e8ff;border:1px solid #355;',
        connected: 'background:#153a1e;color:#d8ffd8;border:1px solid #2a3;',
        reconnecting: 'background:#5a4a1e;color:#fff7d0;border:1px solid #a82;',
        failed: 'background:#4a1e1e;color:#ffd0d0;border:1px solid #822;',
        fallback: 'background:#2a2a2a;color:#ffd89c;border:1px solid #a84;',
    };
    el.style.cssText += colors[state] || colors.connecting;
    el.textContent = text;
}

function _hideBanner() {
    const el = document.getElementById('axonos_webrtc_banner');
    if (el) {
        el.remove();
    }
}

function _normalizeSdp(sdp) {
    return String(sdp || '')
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n')
        .replace(/\n/g, '\r\n')
        .replace(/(\r\n)+$/g, '') + '\r\n';
}

function _waitIceGathering(pc, ms) {
    return new Promise((resolve) => {
        if (pc.iceGatheringState === 'complete') {
            resolve();
            return;
        }
        const t0 = Date.now();
        const iv = setInterval(() => {
            if (pc.iceGatheringState === 'complete' || Date.now() - t0 > ms) {
                clearInterval(iv);
                pc.removeEventListener('icegatheringstatechange', onchg);
                resolve();
            }
        }, 50);
        function onchg() {
            if (pc.iceGatheringState === 'complete') {
                clearInterval(iv);
                pc.removeEventListener('icegatheringstatechange', onchg);
                resolve();
            }
        }
        pc.addEventListener('icegatheringstatechange', onchg);
    });
}

/**
 * @param {object} opts
 * @param {import('../ui.js').UI} opts.UI - noVNC UI object
 * @returns {Promise<boolean>} true if WebRTC owns the session UI
 */
export async function connectAxonOSWebRTC(opts) {
    const UI = opts.UI;
    const wallet = window.verifiedWalletAddress;
    const token = window.verifiedWalletAuthToken;

    if (!wallet || !token) {
        return false;
    }

    if (typeof window.axonosWebRtcTeardown === 'function') {
        try {
            await window.axonosWebRtcTeardown();
        } catch (e) {
            console.warn('AxonOS WebRTC prior session teardown failed', e);
        }
    }

    let cfgRes;
    try {
        cfgRes = await _fetchJson('./api/config', { headers: _authHeaders() });
    } catch {
        return false;
    }
    if (!cfgRes.ok || !cfgRes.json.webrtc_enabled) {
        return false;
    }

    /** When false, UI must not imply noVNC fallback; server blocks classic path. */
    const webrtcFallbackOk = cfgRes.json.webrtc_fallback_enabled !== false;
    const answerWaitMs = Number(cfgRes.json.webrtc_answer_wait_ms) > 0
        ? Number(cfgRes.json.webrtc_answer_wait_ms)
        : 180000;

    if (typeof RTCPeerConnection === 'undefined') {
        if (webrtcFallbackOk) {
            _setBanner('WebRTC not supported in this browser — using classic stream.', 'fallback');
            setTimeout(_hideBanner, 5000);
        } else {
            _setBanner('WebRTC not supported in this browser.', 'failed');
            setTimeout(_hideBanner, 5000);
        }
        return false;
    }

    _setBanner('WebRTC: Connecting…', 'connecting');

    const sessRes = await _fetchJson('./api/webrtc/session', {
        method: 'POST',
        headers: _authHeaders(),
        body: JSON.stringify({ wallet_address: wallet }),
    });
    if (!sessRes.ok || !sessRes.json.ok || !sessRes.json.session_id) {
        _hideBanner();
        return false;
    }
    const sessionId = sessRes.json.session_id;
    const iceServers = sessRes.json.ice_servers || [{ urls: 'stun:stun.l.google.com:19302' }];

    const container = document.getElementById('noVNC_container');
    const video = document.createElement('video');
    video.id = 'axonos_webrtc_video';
    video.autoplay = true;
    video.playsInline = true;
    video.muted = true;
    video.tabIndex = 0;
    video.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;background:#000;z-index:5;cursor:none;';

    const cursor = document.createElement('div');
    cursor.id = 'axonos_webrtc_cursor';
    cursor.style.cssText = [
        'position:absolute',
        'left:0',
        'top:0',
        'width:18px',
        'height:24px',
        'z-index:6',
        'pointer-events:none',
        'transform:translate(-100px,-100px)',
        'filter:drop-shadow(0 1px 1px #000)',
    ].join(';');
    cursor.innerHTML = '<svg width="18" height="24" viewBox="0 0 18 24" xmlns="http://www.w3.org/2000/svg"><path d="M1 1v18l5-5 3 8 3-1-3-8h7z" fill="white" stroke="black" stroke-width="1"/></svg>';

    const pasteSink = document.createElement('textarea');
    pasteSink.id = 'axonos_webrtc_paste_sink';
    pasteSink.setAttribute('aria-hidden', 'true');
    pasteSink.tabIndex = -1;
    pasteSink.style.cssText = [
        'position:fixed',
        'left:-10000px',
        'top:-10000px',
        'width:1px',
        'height:1px',
        'opacity:0',
        'pointer-events:none',
    ].join(';');

    const pc = new RTCPeerConnection({ iceServers });
    const dc = pc.createDataChannel('axonos-input', { ordered: true });
    window.axonosWebRtcPasteClipboard = (text, pasteNow) => {
        if (dc.readyState !== 'open') {
            return false;
        }
        dc.send(JSON.stringify({ t: pasteNow ? 'paste' : 'clipboard', text: String(text || '') }));
        return true;
    };

    pc.addTransceiver('video', { direction: 'recvonly' });
    window.axonosWebRtcPc = pc;
    window.axonosWebRtcVideo = video;

    pc.ontrack = (ev) => {
        console.log('AxonOS WebRTC track', ev.track && ev.track.kind, ev.streams);
        if (ev.streams && ev.streams[0]) {
            video.srcObject = ev.streams[0];
        } else {
            video.srcObject = new MediaStream([ev.track]);
        }
        video.play().catch((e) => console.warn('AxonOS WebRTC video.play failed', e));
    };

    const pendingIce = [];

    pc.onicecandidate = (ev) => {
        if (!ev.candidate) {
            return;
        }
        const body = {
            wallet_address: wallet,
            session_id: sessionId,
            candidate: ev.candidate.candidate,
            sdpMid: ev.candidate.sdpMid,
            sdpMLineIndex: ev.candidate.sdpMLineIndex,
        };
        pendingIce.push(
            _fetchJson('./api/webrtc/ice', {
                method: 'POST',
                headers: _authHeaders(),
                body: JSON.stringify(body),
            })
        );
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await _waitIceGathering(pc, 12000);

    const offerRes = await _fetchJson('./api/webrtc/offer', {
        method: 'POST',
        headers: _authHeaders(),
        body: JSON.stringify({
            wallet_address: wallet,
            session_id: sessionId,
            sdp: pc.localDescription.sdp,
            type: 'offer',
        }),
    });
    if (!offerRes.ok || !offerRes.json.ok) {
        await _cleanup(pc, video, sessionId, wallet);
        if (webrtcFallbackOk) {
            _setBanner('WebRTC negotiation failed — falling back.', 'fallback');
        } else {
            _setBanner('WebRTC negotiation failed.', 'failed');
        }
        setTimeout(_hideBanner, 4000);
        return false;
    }

    let answerApplied = false;
    const deadline = Date.now() + answerWaitMs;
    let serverIceCursor = 0;

    while (Date.now() < deadline && !answerApplied) {
        const st = await _fetchJson(
            `./api/webrtc/status?session_id=${encodeURIComponent(sessionId)}&wallet_address=${encodeURIComponent(wallet)}`,
            { headers: _authHeaders() }
        );
        if (!st.ok) {
            if (st.status === 401) {
                await _cleanup(pc, video, sessionId, wallet);
                const msg = 'WebRTC auth expired — sign in again or use classic VNC if enabled.';
                if (webrtcFallbackOk) {
                    _setBanner(msg + ' Falling back.', 'fallback');
                } else {
                    _setBanner(msg, 'failed');
                }
                setTimeout(_hideBanner, 8000);
                return false;
            }
            break;
        }
        const j = st.json;
        if (j.state === 'failed' || j.last_error) {
            await _cleanup(pc, video, sessionId, wallet);
            const detail = (j.last_error && String(j.last_error).trim()) || 'signaling failed';
            const msg = `WebRTC failed: ${detail}`;
            if (webrtcFallbackOk) {
                _setBanner(`${msg} — falling back.`, 'fallback');
            } else {
                _setBanner(msg, 'failed');
            }
            setTimeout(_hideBanner, 8000);
            return false;
        }
        if (j.state === 'closed') {
            await _cleanup(pc, video, sessionId, wallet);
            if (webrtcFallbackOk) {
                _setBanner('WebRTC session closed — falling back.', 'fallback');
            } else {
                _setBanner('WebRTC session closed.', 'failed');
            }
            setTimeout(_hideBanner, 5000);
            return false;
        }
        if (j.has_answer && j.answer && j.answer.sdp) {
            const answerSdp = _normalizeSdp(j.answer.sdp);
            await pc.setRemoteDescription(
                new RTCSessionDescription({ type: 'answer', sdp: answerSdp })
            );
            answerApplied = true;
        }
        const srv = j.server_ice || [];
        for (; serverIceCursor < srv.length; serverIceCursor += 1) {
            const c = srv[serverIceCursor];
            if (c && c.candidate) {
                try {
                    await pc.addIceCandidate(
                        new RTCIceCandidate({
                            candidate: c.candidate,
                            sdpMid: c.sdpMid,
                            sdpMLineIndex: c.sdpMLineIndex,
                        })
                    );
                } catch (e) {
                    console.warn('addIceCandidate', e);
                }
            }
        }
        if (!answerApplied) {
            await new Promise((r) => setTimeout(r, 280));
        }
    }

    if (!answerApplied) {
        await _cleanup(pc, video, sessionId, wallet);
        if (webrtcFallbackOk) {
            _setBanner('WebRTC timed out — falling back.', 'fallback');
        } else {
            _setBanner('WebRTC timed out.', 'failed');
        }
        setTimeout(_hideBanner, 4000);
        return false;
    }

    await Promise.allSettled(pendingIce);

    _setBanner('WebRTC: Connected', 'connected');
    setTimeout(_hideBanner, 2000);

    if (container) {
        container.appendChild(video);
        container.appendChild(cursor);
        container.appendChild(pasteSink);
    } else {
        document.body.appendChild(video);
        document.body.appendChild(cursor);
        document.body.appendChild(pasteSink);
    }

    let inputScaleX = 1;
    let inputScaleY = 1;
    let vidW = 1;
    let vidH = 1;
    let imageLeft = 0;
    let imageTop = 0;
    let imageWidth = 1;
    let imageHeight = 1;

    const syncInputScale = () => {
        const rw = video.videoWidth || video.clientWidth || 1;
        const rh = video.videoHeight || video.clientHeight || 1;
        const cw = video.clientWidth || 1;
        const ch = video.clientHeight || 1;
        const scale = Math.min(cw / rw, ch / rh);
        imageWidth = rw * scale;
        imageHeight = rh * scale;
        imageLeft = (cw - imageWidth) / 2;
        imageTop = (ch - imageHeight) / 2;
        vidW = rw;
        vidH = rh;
        inputScaleX = rw / imageWidth;
        inputScaleY = rh / imageHeight;
    };

    // AbortController removes every input listener on teardown so repeated
    // session spawns cannot accumulate duplicate window handlers.
    const inputAbort = new AbortController();
    const inputSignal = inputAbort.signal;
    const clipboardBeforeClickMs = 200;

    video.addEventListener('loadeddata', syncInputScale, { signal: inputSignal });
    window.addEventListener('resize', syncInputScale, { signal: inputSignal });

    let inputChannelOpen = dc.readyState === 'open';
    // RFB-style bitmask: 1=left, 2=middle, 4=right (1 << DOM button index).
    let currentMouseButtons = 0;
    // Deferred press: simple clicks send one atomic `click`; drags send mousedown after move.
    const DRAG_THRESHOLD_PX = 4;
    /** @type {{ button: number, clientX: number, clientY: number } | null} */
    let pendingPress = null;

    function sendInput(obj) {
        if (!inputChannelOpen || dc.readyState !== 'open') {
            return false;
        }
        try {
            dc.send(JSON.stringify(obj));
            return true;
        } catch (e) {
            console.warn('AxonOS WebRTC input send failed', e);
            return false;
        }
    }

    dc.addEventListener('open', () => {
        inputChannelOpen = true;
        currentMouseButtons = 0;
    });
    dc.addEventListener('close', () => {
        inputChannelOpen = false;
        currentMouseButtons = 0;
    });

    dc.onmessage = (ev) => {
        let msg = null;
        try {
            msg = JSON.parse(ev.data);
        } catch {
            return;
        }
        if (!msg || msg.t !== 'clipboard' || typeof msg.text !== 'string') {
            return;
        }
        const incoming = msg.text;
        if (UI && typeof UI.setClipboardTextarea === 'function') {
            UI.clipboardLastRemoteText = incoming;
            UI.setClipboardTextarea(incoming);
        }
        // Remote poll can push PRIMARY noise (e.g. "New File" from the desktop)
        // while the host user just copied real text. Unconditional writeText
        // stomps the OS clipboard so readText() pulls garbage for right-click
        // Paste; Ctrl+V often still sees the real clip via paste events.
        const protectMs = 8000;
        const pushAt = UI && typeof UI.webrtcHostPushAt === 'number' ? UI.webrtcHostPushAt : 0;
        const pushText = UI && typeof UI.webrtcHostPushText === 'string' ? UI.webrtcHostPushText : '';
        const recent = pushAt > 0 && Date.now() - pushAt < protectMs;
        // Avoid readText() here — it stacks with auto-sync/right-click pulls and can
        // hang after host paste, starving the browser clipboard API for clicks.
        if (recent && pushText && incoming !== pushText) {
            return;
        }
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            navigator.clipboard.writeText(incoming).catch(() => {});
        }
    };

    function pointerToRemote(ev) {
        const r = video.getBoundingClientRect();
        const localX = Math.max(0, Math.min(imageWidth, ev.clientX - r.left - imageLeft));
        const localY = Math.max(0, Math.min(imageHeight, ev.clientY - r.top - imageTop));
        cursor.style.transform = `translate(${imageLeft + localX}px, ${imageTop + localY}px)`;
        return {
            x: Math.round(localX * inputScaleX),
            y: Math.round(localY * inputScaleY),
        };
    }

    // Keeping pasteSink focused (rather than video) lets the browser deliver
    // native paste events to it on Ctrl+V without needing clipboard permission.
    function focusPasteSink() {
        try {
            pasteSink.focus({ preventScroll: true });
        } catch {
            try { pasteSink.focus(); } catch { /* ignore */ }
        }
    }

    // Push host clipboard into the remote X CLIPBOARD on user gestures. The
    // 1.5 s `startClipboardAutoSync` interval covers the steady state, but it
    // can lag user actions (e.g. user copies on host then immediately right-
    // clicks in remote — the context menu would open before the next tick),
    // and `navigator.clipboard.readText()` from setInterval also silently
    // rejects when the document briefly loses focus. Tying a pull to the
    // mousedown that OPENS the remote context menu (and to focus /
    // visibilitychange when the tab returns to foreground) makes the X
    // CLIPBOARD fresh by the time the user lands on "Paste".
    function kickClipboardSync() {
        if (typeof UI.pullLocalClipboardToRemote !== 'function') return;
        try {
            const p = UI.pullLocalClipboardToRemote({ timeoutMs: 800 });
            if (p && typeof p.catch === 'function') {
                p.catch(() => { /* readText can reject when document lost focus */ });
            }
        } catch { /* ignore */ }
    }

    function domButtonMask(button) {
        return 1 << button;
    }

    function domButtonToXdotool(button) {
        return button + 1;
    }

    /** Drop local mask and optionally emit matching mouseup events (session teardown / cancel). */
    function resetMouseInputState(ev, sendRelease) {
        if (currentMouseButtons === 0) {
            return;
        }
        // When `ev` is present, include remote coords (pointercancel mid-drag). When
        // absent (session teardown), omit x/y so the agent runs xdotool mouseup
        // only — no pointer jump to 0,0. Channel close also resets mask server-side.
        const coords = ev ? pointerToRemote(ev) : null;
        let remaining = currentMouseButtons;
        for (let btn = 0; btn < 3; btn += 1) {
            const bit = domButtonMask(btn);
            if (!(remaining & bit)) {
                continue;
            }
            remaining &= ~bit;
            if (sendRelease) {
                const payload = {
                    t: 'mouseup',
                    button: domButtonToXdotool(btn),
                    buttons: remaining,
                };
                if (coords) {
                    Object.assign(payload, coords);
                }
                sendInput(payload);
            }
        }
        currentMouseButtons = 0;
    }

    function sendRemoteClick(ev) {
        pendingPress = null;
        resetMouseInputState(null, true);
        sendInput({
            t: 'click',
            button: domButtonToXdotool(ev.button),
            ...pointerToRemote(ev),
        });
    }

    function beginDragPress(pending, ev) {
        pendingPress = null;
        pressMouseButton({
            button: pending.button,
            clientX: ev.clientX,
            clientY: ev.clientY,
        });
    }

    function pressMouseButton(ev) {
        const bit = domButtonMask(ev.button);
        if (currentMouseButtons & bit) {
            // Lost mouseup while focus was on the host OS (copy/paste) — release
            // remotely and accept this press so the click still registers.
            const coords = pointerToRemote(ev);
            currentMouseButtons &= ~bit;
            sendInput({
                t: 'mouseup',
                button: domButtonToXdotool(ev.button),
                buttons: currentMouseButtons,
                ...coords,
            });
        }
        currentMouseButtons |= bit;
        const sent = sendInput({
            t: 'mousedown',
            button: domButtonToXdotool(ev.button),
            buttons: currentMouseButtons,
            ...pointerToRemote(ev),
        });
        if (!sent) {
            currentMouseButtons &= ~bit;
        }
    }

    function releaseMouseButton(ev) {
        const bit = domButtonMask(ev.button);
        const coords = pointerToRemote(ev);
        if (!(currentMouseButtons & bit)) {
            // Orphan mouseup: mousedown may have been delayed, dropped on a closed
            // channel, or lost across session teardown — release remote anyway.
            sendInput({
                t: 'mouseup',
                button: domButtonToXdotool(ev.button),
                buttons: currentMouseButtons,
                ...coords,
            });
            return;
        }
        currentMouseButtons &= ~bit;
        sendInput({
            t: 'mouseup',
            button: domButtonToXdotool(ev.button),
            buttons: currentMouseButtons,
            ...coords,
        });
    }

    // Moves must keep firing during click-and-drag when the cursor leaves the
    // letterboxed video bounds; listeners on video alone stop dispatching moves
    // once pointer exits the element, which breaks dragging on the desktop.
    function clientPointOverVideo(ev) {
        const r = video.getBoundingClientRect();
        return (
            ev.clientX >= r.left &&
            ev.clientX <= r.right &&
            ev.clientY >= r.top &&
            ev.clientY <= r.bottom
        );
    }

    function onWindowMouseMove(ev) {
        if (pendingPress !== null) {
            const dx = ev.clientX - pendingPress.clientX;
            const dy = ev.clientY - pendingPress.clientY;
            if (dx * dx + dy * dy >= DRAG_THRESHOLD_PX * DRAG_THRESHOLD_PX) {
                beginDragPress(pendingPress, ev);
            }
        }
        const dragging = currentMouseButtons !== 0;
        if (!dragging && !clientPointOverVideo(ev)) {
            return;
        }
        ev.preventDefault();
        sendInput({ t: 'move', buttons: currentMouseButtons, ...pointerToRemote(ev) });
    }
    window.addEventListener('mousemove', onWindowMouseMove, { signal: inputSignal });

    function syncClipboardBeforeClick(ev) {
        // Only right-click needs host clipboard on X CLIPBOARD before the
        // remote context menu opens. Left/middle clicks must not call
        // `readText()` — after host paste the API can hang for seconds.
        // Never await on the click path; pointerdown may start this earlier.
        if (ev.button !== 2) {
            return;
        }
        if (typeof UI.pullLocalClipboardToRemote !== 'function') {
            return;
        }
        try {
            const p = UI.pullLocalClipboardToRemote({ timeoutMs: clipboardBeforeClickMs });
            if (p && typeof p.catch === 'function') {
                p.catch(() => { /* readText can reject when document lost focus */ });
            }
        } catch { /* ignore */ }
    }

    /** Retain pointer coords when dragging outside the letterboxed video bounds. */
    function onVideoPointerDown(ev) {
        if (ev.pointerType === 'touch') {
            return;
        }
        if (ev.button === 2) {
            syncClipboardBeforeClick(ev);
        }
        if (video.setPointerCapture && typeof video.setPointerCapture === 'function') {
            try {
                video.setPointerCapture(ev.pointerId);
            } catch {
                /* ignore */
            }
        }
    }
    video.addEventListener('pointerdown', onVideoPointerDown, { signal: inputSignal });

    /** Release capture so scroll / click outside behave normally once drag ends */
    function releaseCapturedPointer(ev) {
        if (
            !video.releasePointerCapture ||
            typeof video.releasePointerCapture !== 'function'
        ) {
            return;
        }
        if (!ev || typeof ev.pointerId !== 'number') {
            return;
        }
        try {
            video.releasePointerCapture(ev.pointerId);
        } catch {
            /* ignore: not capturing or unsupported */
        }
    }

    /**
     * Map WheelEvent deltas to discrete X11 scroll-button clicks (4=up, 5=down).
     * @param {number} delta
     * @param {number} deltaMode 0=pixel, 1=line, 2=page
     * @returns {number} signed step count, capped per event
     */
    function wheelSteps(delta, deltaMode) {
        const d = Number(delta) || 0;
        if (Math.abs(d) < 1e-6) {
            return 0;
        }
        let lines;
        switch (deltaMode) {
            case 1: // DOM_DELTA_LINE
                lines = Math.abs(d);
                break;
            case 2: // DOM_DELTA_PAGE
                lines = Math.abs(d) * 8;
                break;
            default: // DOM_DELTA_PIXEL
                lines = Math.abs(d) / 50;
                break;
        }
        const steps = Math.max(1, Math.min(25, Math.round(lines)));
        return Math.sign(d) * steps;
    }

    function onVideoWheel(ev) {
        ev.preventDefault();
        const dy = wheelSteps(ev.deltaY, ev.deltaMode);
        const dx = wheelSteps(ev.deltaX, ev.deltaMode);
        if (dy === 0 && dx === 0) {
            return;
        }
        sendInput({
            t: 'wheel',
            ...pointerToRemote(ev),
            dy,
            dx,
        });
    }
    video.addEventListener('wheel', onVideoWheel, { passive: false, signal: inputSignal });

    function onVideoMouseDown(ev) {
        ev.preventDefault();
        focusPasteSink();
        if (ev.button === 2) {
            syncClipboardBeforeClick(ev);
        }
        pendingPress = {
            button: ev.button,
            clientX: ev.clientX,
            clientY: ev.clientY,
        };
    }
    video.addEventListener('mousedown', onVideoMouseDown, { signal: inputSignal });

    function onMouseUp(ev) {
        if (pendingPress !== null && ev.button === pendingPress.button) {
            sendRemoteClick(ev);
            return;
        }
        pendingPress = null;
        releaseMouseButton(ev);
    }
    window.addEventListener('mouseup', onMouseUp, { signal: inputSignal });

    function onPointerUp(ev) {
        releaseCapturedPointer(ev);
    }
    function onPointerCancel(ev) {
        releaseCapturedPointer(ev);
        pendingPress = null;
        resetMouseInputState(ev, true);
    }
    window.addEventListener('pointerup', onPointerUp, { signal: inputSignal });
    window.addEventListener('pointercancel', onPointerCancel, { signal: inputSignal });

    video.addEventListener('mouseenter', focusPasteSink, { signal: inputSignal });
    video.addEventListener('contextmenu', (ev) => {
        ev.preventDefault();
    }, { signal: inputSignal });
    video.addEventListener('mouseleave', () => {
        if (currentMouseButtons !== 0) {
            return;
        }
        cursor.style.transform = 'translate(-100px,-100px)';
    }, { signal: inputSignal });

    function releaseMouseOnFocusLoss() {
        pendingPress = null;
        resetMouseInputState(null, true);
    }

    function onVisibilityChange() {
        if (document.visibilityState === 'visible') {
            kickClipboardSync();
            if (currentMouseButtons !== 0) {
                releaseMouseOnFocusLoss();
            }
        } else {
            releaseMouseOnFocusLoss();
        }
    }
    window.addEventListener('blur', releaseMouseOnFocusLoss, { signal: inputSignal });
    window.addEventListener('focus', kickClipboardSync, { signal: inputSignal });
    document.addEventListener('visibilitychange', onVisibilityChange, { signal: inputSignal });

    function pasteTextToRemote(text) {
        const s = String(text || '');
        sendInput({ t: 'paste', text: s });
        if (UI && s) {
            UI.clipboardLastLocalText = s;
            UI.clipboardLastRemoteText = s;
            if (typeof UI.markHostClipboardSentToRemote === 'function') {
                UI.markHostClipboardSentToRemote(s);
            }
        }
    }

    function isLocalTextTarget(target) {
        if (!target || target === pasteSink) {
            return false;
        }
        const tag = target.tagName ? target.tagName.toLowerCase() : '';
        return tag === 'input' || tag === 'textarea' || target.isContentEditable === true;
    }

    window.addEventListener('paste', (ev) => {
        if (!UI.connected) {
            return;
        }
        if (isLocalTextTarget(ev.target)) {
            return;
        }
        const text = ev.clipboardData ? ev.clipboardData.getData('text/plain') : '';
        if (text) {
            ev.preventDefault();
            ev.stopImmediatePropagation();
            pasteTextToRemote(text);
            // Drain the sink so subsequent pastes start clean and keep focus on
            // pasteSink so future Ctrl+V keystrokes also receive paste events.
            pasteSink.value = '';
            focusPasteSink();
        }
    }, { capture: true, signal: inputSignal });

    // Tracks keys whose keydown we deliberately suppressed (e.g. Ctrl+V's V) so
    // we can also drop the matching keyup and avoid sending the remote a stray
    // keyup for a key it never saw pressed. Keyed by ev.code for browser-stable
    // identity (independent of modifier-altered ev.key values).
    const suppressedKeyups = new Set();

    window.addEventListener('keydown', (ev) => {
        if (!UI.connected) {
            return;
        }
        if (isLocalTextTarget(ev.target)) {
            return;
        }
        if (ev.key) {
            if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'v') {
                // If pasteSink is focused (or we can focus it before the browser's
                // paste action), let the browser dispatch a native paste event with
                // clipboardData populated. The window paste listener forwards it to
                // the remote desktop. This path needs no clipboard permission and
                // therefore works even on freshly-rotated trycloudflare hostnames.
                if (ev.target !== pasteSink) {
                    pasteSink.value = '';
                    focusPasteSink();
                }
                if (ev.code) {
                    suppressedKeyups.add(ev.code);
                }
                return;
            }
            ev.preventDefault();
            sendInput({
                t: 'keydown',
                key: ev.key,
                code: ev.code,
                ctrlKey: ev.ctrlKey,
                altKey: ev.altKey,
                shiftKey: ev.shiftKey,
                metaKey: ev.metaKey,
                repeat: ev.repeat,
            });
        }
    }, { signal: inputSignal });
    window.addEventListener('keyup', (ev) => {
        if (!UI.connected) {
            return;
        }
        if (isLocalTextTarget(ev.target)) {
            return;
        }
        if (ev.key) {
            ev.preventDefault();
            if (ev.code && suppressedKeyups.delete(ev.code)) {
                // Matching keydown was intentionally not forwarded (paste shortcut).
                // Drop the orphan keyup so the remote keymap state stays consistent.
                return;
            }
            sendInput({
                t: 'keyup',
                key: ev.key,
                code: ev.code,
                ctrlKey: ev.ctrlKey,
                altKey: ev.altKey,
                shiftKey: ev.shiftKey,
                metaKey: ev.metaKey,
            });
        }
    }, { signal: inputSignal });
    let metricsTimer = null;
    const pollStats = () => {
        pc.getStats(null).then((report) => {
            let rtt = null;
            let pl = null;
            report.forEach((s) => {
                if (s.type === 'candidate-pair' && s.state === 'succeeded') {
                    if (typeof s.currentRoundTripTime === 'number') {
                        rtt = Math.round(s.currentRoundTripTime * 1000);
                    }
                }
                if (s.type === 'inbound-rtp' && typeof s.packetsLost === 'number') {
                    pl = s.packetsLost;
                }
            });
            _fetchJson('./api/webrtc/metrics', {
                method: 'POST',
                headers: _authHeaders(),
                body: JSON.stringify({
                    wallet_address: wallet,
                    session_id: sessionId,
                    rtt_ms: rtt,
                    packets_lost: pl,
                    connection_state: pc.connectionState,
                }),
            }).then((res) => {
                // Metrics are optional; stop polling if auth has expired or rotated.
                if ((res.status === 401 || res.status === 403) && metricsTimer) {
                    clearInterval(metricsTimer);
                    metricsTimer = null;
                }
            });
        }).catch(() => {});
    };

    pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'disconnected' || pc.connectionState === 'failed') {
            _setBanner('WebRTC disconnected.', 'failed');
        }
    };

    metricsTimer = setInterval(pollStats, 5000);
    pollStats();

    UI.connected = true;
    UI.inhibitReconnect = false;
    UI.updateVisualState('connected');
    UI.showStatus('Connected (WebRTC)');
    try {
        pasteSink.focus({ preventScroll: true });
    } catch {
        try { pasteSink.focus(); } catch { /* ignore */ }
    }

    // Host → remote clipboard auto-sync. Without this, the X CLIPBOARD inside
    // the axonos session is only updated when the user explicitly hits Ctrl+V
    // (which forwards clipboardData via the native paste event). Right-click →
    // Paste in a remote app would then paste a stale X selection. Starting the
    // poll here mirrors what the classic RFB path does in connectFinished.
    if (typeof UI.startClipboardAutoSync === 'function') {
        try {
            UI.startClipboardAutoSync();
        } catch (e) {
            console.warn('AxonOS WebRTC clipboard auto-sync start failed', e);
        }
    }

    if (typeof UI._axgtStartSessionBillingPoll === 'function') {
        UI._axgtStartSessionBillingPoll();
    }

    window.axonosWebRtcTeardown = async () => {
        resetMouseInputState(null, true);
        inputAbort.abort();
        if (metricsTimer) {
            clearInterval(metricsTimer);
        }
        if (typeof UI.stopClipboardAutoSync === 'function') {
            try { UI.stopClipboardAutoSync(); } catch { /* ignore */ }
        }
        await _cleanup(pc, video, sessionId, wallet);
        window.axonosWebRtcPasteClipboard = null;
        window.axonosWebRtcTeardown = null;
    };

    return true;
}

async function _cleanup(pc, video, sessionId, wallet) {
    try {
        await _fetchJson('./api/webrtc/close', {
            method: 'POST',
            headers: _authHeaders(),
            body: JSON.stringify({ wallet_address: wallet, session_id: sessionId }),
        });
    } catch {
        /* ignore */
    }
    try {
        pc.getSenders().forEach((s) => s.track && s.track.stop());
        await pc.close();
    } catch {
        /* ignore */
    }
    if (video && video.parentNode) {
        video.parentNode.removeChild(video);
    }
    const cursor = document.getElementById('axonos_webrtc_cursor');
    if (cursor && cursor.parentNode) {
        cursor.parentNode.removeChild(cursor);
    }
    const pasteSink = document.getElementById('axonos_webrtc_paste_sink');
    if (pasteSink && pasteSink.parentNode) {
        pasteSink.parentNode.removeChild(pasteSink);
    }
    _hideBanner();
}
