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
    const deadline = Date.now() + 90000;
    let serverIceCursor = 0;

    while (Date.now() < deadline && !answerApplied) {
        const st = await _fetchJson(
            `./api/webrtc/status?session_id=${encodeURIComponent(sessionId)}&wallet_address=${encodeURIComponent(wallet)}`,
            { headers: _authHeaders() }
        );
        if (!st.ok) {
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

    video.addEventListener('loadeddata', syncInputScale);
    window.addEventListener('resize', syncInputScale);

    function sendInput(obj) {
        if (dc.readyState === 'open') {
            dc.send(JSON.stringify(obj));
        }
    }

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
        if (UI && typeof UI.setClipboardTextarea === 'function') {
            UI.clipboardLastRemoteText = msg.text;
            UI.setClipboardTextarea(msg.text);
        }
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            navigator.clipboard.writeText(msg.text).catch(() => {});
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

    video.addEventListener('mousemove', (ev) => {
        ev.preventDefault();
        sendInput({ t: 'move', ...pointerToRemote(ev) });
    });
    video.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        focusPasteSink();
        sendInput({ t: 'click', button: ev.button + 1, ...pointerToRemote(ev) });
    });
    video.addEventListener('mouseenter', focusPasteSink);
    video.addEventListener('contextmenu', (ev) => {
        ev.preventDefault();
    });
    video.addEventListener('mouseleave', () => {
        cursor.style.transform = 'translate(-100px,-100px)';
    });

    function pasteTextToRemote(text) {
        sendInput({ t: 'paste', text: String(text || '') });
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
            pasteTextToRemote(text);
            // Drain the sink so subsequent pastes start clean and keep focus on
            // pasteSink so future Ctrl+V keystrokes also receive paste events.
            pasteSink.value = '';
            focusPasteSink();
        }
    }, true);

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
    });
    window.addEventListener('keyup', (ev) => {
        if (!UI.connected) {
            return;
        }
        if (isLocalTextTarget(ev.target)) {
            return;
        }
        if (ev.key) {
            ev.preventDefault();
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
    });
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

    window.axonosWebRtcTeardown = async () => {
        if (metricsTimer) {
            clearInterval(metricsTimer);
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
