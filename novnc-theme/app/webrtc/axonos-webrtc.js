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

function _iceConnected(state) {
    return state === 'connected' || state === 'completed';
}

/** Resolve when ICE reaches connected/completed, or reject on failed/closed/timeout. */
function _waitIceConnected(pc, timeoutMs) {
    return new Promise((resolve) => {
        if (_iceConnected(pc.iceConnectionState)) {
            resolve(true);
            return;
        }
        let timer = null;
        const finish = (ok) => {
            pc.removeEventListener('iceconnectionstatechange', onIce);
            if (timer) {
                clearTimeout(timer);
            }
            resolve(ok);
        };
        const onIce = () => {
            const st = pc.iceConnectionState;
            if (_iceConnected(st)) {
                finish(true);
            } else if (st === 'failed' || st === 'closed') {
                finish(false);
            }
        };
        pc.addEventListener('iceconnectionstatechange', onIce);
        timer = setTimeout(() => finish(_iceConnected(pc.iceConnectionState)), timeoutMs);
    });
}

function _setBanner(text, state) {
    let el = document.getElementById('axonos_webrtc_banner');
    if (!el) {
        el = document.createElement('div');
        el.id = 'axonos_webrtc_banner';
        el.setAttribute('aria-live', 'polite');
        // bottom: 44px clears the footer banner ("Built with ♥ by AxonDAO", fixed at
        // bottom: 10px) so it stays readable while the desktop session loads.
        el.style.cssText = 'position:fixed;bottom:44px;left:50%;transform:translateX(-50%);z-index:10000;padding:8px 14px;border-radius:8px;font:14px system-ui,sans-serif;max-width:90vw;text-align:center;';
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

/** Bumped on cancel and at each new connect attempt — stale loops must not touch UI. */
let _negotiationGeneration = 0;
/** @type {{ pc: RTCPeerConnection, video: HTMLVideoElement, sessionId: string, wallet: string, generation: number, pasteClipboard?: Function, releasePointer?: Function } | null} */
let _inFlightNegotiation = null;
/** Structured reason for the most recently completed, unsuccessful connect attempt. */
let _lastConnectFailure = null;
const _terminalSignalingFailures = new Set(['display_not_ready']);

function _normalizeFailureCode(value, fallback = 'signaling_failed') {
    const text = String(value || '').trim().toLowerCase();
    if (/^[a-z][a-z0-9_-]*$/.test(text)) {
        return text;
    }
    const prefixedCode = text.match(/^([a-z][a-z0-9_-]*)\s*:/);
    return prefixedCode ? prefixedCode[1] : fallback;
}

function _recordSignalingFailure(payload, sessionId) {
    const rawError = payload && payload.last_error;
    const structuredError = rawError && typeof rawError === 'object' ? rawError : null;
    const detail = String(
        (structuredError && (
            structuredError.detail || structuredError.message ||
            structuredError.error || structuredError.code
        )) || rawError || 'signaling failed'
    ).trim() || 'signaling failed';
    const explicitCode = payload && (
        payload.error_code || payload.failure_code || payload.last_error_code
    );
    const code = _normalizeFailureCode(
        explicitCode || (structuredError && structuredError.code) || rawError
    );
    const terminal = _terminalSignalingFailures.has(code);
    _lastConnectFailure = Object.freeze({
        code,
        detail,
        message: `WebRTC failed: ${detail}`,
        stage: 'signaling',
        state: String((payload && payload.state) || 'failed'),
        sessionId: String(sessionId || ''),
        terminal,
        retryable: !terminal,
    });
    return _lastConnectFailure;
}

/** Let the UI distinguish a terminal server failure from a retryable false result. */
export function getAxonOSWebRTCFailure() {
    return _lastConnectFailure ? { ..._lastConnectFailure } : null;
}

function _negotiationCancelled(generation) {
    return generation !== _negotiationGeneration;
}

/** Clear only globals still owned by this peer; a replacement may already exist. */
function _clearOwnedPeerGlobals(owner) {
    if (typeof window === 'undefined' || !owner) {
        return;
    }
    if (owner.pc && window.axonosWebRtcPc === owner.pc) {
        window.axonosWebRtcPc = null;
    }
    if (owner.video && window.axonosWebRtcVideo === owner.video) {
        window.axonosWebRtcVideo = null;
    }
    if (owner.pasteClipboard && window.axonosWebRtcPasteClipboard === owner.pasteClipboard) {
        window.axonosWebRtcPasteClipboard = null;
    }
    if (owner.releasePointer && window.axonosWebRtcReleasePointerState === owner.releasePointer) {
        window.axonosWebRtcReleasePointerState = null;
    }
    if (owner.teardown && window.axonosWebRtcTeardown === owner.teardown) {
        window.axonosWebRtcTeardown = null;
    }
}

/**
 * Stop in-flight WebRTC negotiation (poll loop, ICE wait) and tear down peer resources.
 * Safe to call from disconnect, detach, or before a new Launch.
 */
export function cancelAxonOSWebRTCNegotiation() {
    _negotiationGeneration += 1;
    if (typeof window !== 'undefined') {
        window.axonosWebRtcConnectAborted = true;
    }
    const snap = _inFlightNegotiation;
    _inFlightNegotiation = null;
    const cleanupPromises = [];
    if (snap && snap.pc) {
        cleanupPromises.push(
            _cleanup(snap.pc, snap.video, snap.sessionId, snap.wallet)
                .finally(() => _clearOwnedPeerGlobals(snap))
        );
    }
    if (typeof window.axonosWebRtcTeardown === 'function') {
        const teardown = window.axonosWebRtcTeardown;
        try {
            cleanupPromises.push(Promise.resolve(teardown()));
        } catch (err) {
            cleanupPromises.push(Promise.reject(err));
        }
    }
    _hideBanner();
    // Existing callers may remain fire-and-forget; disconnect() can now await this
    // result so the workspace is not reopened in the middle of peer cleanup.
    return Promise.allSettled(cleanupPromises);
}

async function _finishNegotiationCancelled(generation, pc, video, sessionId, wallet) {
    if (!_negotiationCancelled(generation)) {
        return false;
    }
    if (pc) {
        await _cleanup(pc, video, sessionId, wallet);
        _clearOwnedPeerGlobals({ pc, video });
    } else {
        _hideBanner();
    }
    if (_inFlightNegotiation && _inFlightNegotiation.generation === generation) {
        _inFlightNegotiation = null;
    }
    return true;
}

function _normalizeSdp(sdp) {
    return String(sdp || '')
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n')
        .replace(/\n/g, '\r\n')
        .replace(/(\r\n)+$/g, '') + '\r\n';
}

/**
 * @param {object} opts
 * @param {import('../ui.js').UI} opts.UI - noVNC UI object
 * @param {number|string} opts.computeSessionId - claimed compute-session identifier
 * @param {(phase: string) => void} [opts.onProgress] - truthful loader phase callback
 * @param {boolean} [opts.deferFailureUi] - keep a retryable attempt's failure inside the loader
 * @returns {Promise<boolean>} true if WebRTC owns the session UI
 */
export async function connectAxonOSWebRTC(opts) {
    const UI = opts.UI;
    const wallet = window.verifiedWalletAddress;
    const token = window.verifiedWalletAuthToken;
    const computeSessionId = Number(opts.computeSessionId);
    const deferFailureUi = opts.deferFailureUi === true;
    const showFailure = (text, state, timeoutMs) => {
        if (deferFailureUi) {
            _hideBanner();
            return;
        }
        _setBanner(text, state);
        setTimeout(_hideBanner, timeoutMs);
    };
    const reportProgress = (phase) => {
        const callback = typeof opts.onProgress === 'function'
            ? opts.onProgress
            : window.axonosSetConnectionLoaderPhase;
        if (typeof callback === 'function') {
            try {
                callback(phase);
            } catch (e) {
                console.warn('AxonOS WebRTC progress callback failed', e);
            }
        }
    };

    // A caller reads this after each false result. Never let a prior attempt's
    // terminal reason suppress the retry policy for a newer attempt.
    _lastConnectFailure = null;

    if (!wallet || !token || !Number.isSafeInteger(computeSessionId) || computeSessionId <= 0) {
        return false;
    }

    if (typeof window.axonosWebRtcTeardown === 'function') {
        try {
            await window.axonosWebRtcTeardown();
        } catch (e) {
            console.warn('AxonOS WebRTC prior session teardown failed', e);
        }
    }

    // Start this attempt only after the previous peer's single-flight teardown;
    // that teardown advances the generation to cancel its own stale callbacks.
    window.axonosWebRtcConnectAborted = false;
    const negotiationGeneration = ++_negotiationGeneration;

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
            showFailure('WebRTC not supported in this browser — using classic stream.', 'fallback', 5000);
        } else {
            showFailure('WebRTC not supported in this browser.', 'failed', 5000);
        }
        return false;
    }

    /** When false, NVENC embeds the host cursor — skip the browser overlay. */
    const localCursor = cfgRes.json.webrtc_local_cursor === true;
    /** Operator gate: only offer a sendable mic transceiver + toggle when on. */
    const micCapable = cfgRes.json.webrtc_mic_enabled === true;

    _setBanner('WebRTC: Connecting…', 'connecting');

    const sessRes = await _fetchJson('./api/webrtc/session', {
        method: 'POST',
        headers: _authHeaders(),
        body: JSON.stringify({
            wallet_address: wallet,
            compute_session_id: computeSessionId,
        }),
    });
    if (!sessRes.ok || !sessRes.json.ok || !sessRes.json.session_id) {
        _hideBanner();
        return false;
    }
    const sessionId = sessRes.json.session_id;
    const iceServers = sessRes.json.ice_servers || [{ urls: 'stun:stun.l.google.com:19302' }];

    const container = document.getElementById('noVNC_container');
    const prevContainerCursor = container ? container.style.cursor : '';
    if (container) {
        container.style.cursor = 'none';
    }
    const video = document.createElement('video');
    video.id = 'axonos_webrtc_video';
    video.autoplay = true;
    video.playsInline = true;
    video.muted = true;
    video.tabIndex = 0;
    video.style.cssText =
        'position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;background:#000;z-index:5;cursor:none;';

    /** @type {HTMLDivElement | null} */
    let cursor = null;
    if (localCursor) {
        cursor = document.createElement('div');
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
    }
    video._axonosWebRtcCursor = cursor;

    if (container) {
        container.appendChild(video);
        if (cursor) {
            container.appendChild(cursor);
        }
    } else {
        document.body.appendChild(video);
        if (cursor) {
            document.body.appendChild(cursor);
        }
    }

    function ensureVideoPlaying() {
        if (!video.isConnected || !video.srcObject) {
            return;
        }
        const playPromise = video.play();
        if (playPromise && typeof playPromise.catch === 'function') {
            playPromise.catch((e) => {
                if (e && e.name === 'AbortError') {
                    return;
                }
                console.warn('AxonOS WebRTC video.play failed', e);
            });
        }
    }

    const pc = new RTCPeerConnection({
        iceServers,
        bundlePolicy: 'max-bundle',
        rtcpMuxPolicy: 'require',
    });
    _inFlightNegotiation = { pc, video, sessionId, wallet, generation: negotiationGeneration };
    const CLIPBOARD_MAX_CHARS = 512 * 1024;
    function clampClipboardText(text) {
        const s = String(text || '');
        return s.length <= CLIPBOARD_MAX_CHARS ? s : s.slice(0, CLIPBOARD_MAX_CHARS);
    }
    // Separate channels so multi-MB clipboard JSON cannot queue ahead of clicks
    // on the ordered SCTP stream (the root cause when host clipboard is large).
    const dcMoves = pc.createDataChannel('axonos-input-moves', {
        ordered: false,
        maxRetransmits: 0,
    });
    const dcInput = pc.createDataChannel('axonos-input', { ordered: false, maxRetransmits: 2 });
    const dcClip = pc.createDataChannel('axonos-clipboard', { ordered: true });
    function sendClipboard(obj) {
        if (dcClip.readyState !== 'open') {
            return false;
        }
        try {
            const payload = { ...obj };
            if (payload.text !== undefined) {
                payload.text = clampClipboardText(payload.text);
            }
            dcClip.send(JSON.stringify(payload));
            return true;
        } catch (e) {
            console.warn('AxonOS WebRTC clipboard send failed', e);
            return false;
        }
    }
    const pasteClipboard = (text, pasteNow) => {
        return sendClipboard({ t: pasteNow ? 'paste' : 'clipboard', text });
    };
    window.axonosWebRtcPasteClipboard = pasteClipboard;
    if (_inFlightNegotiation && _inFlightNegotiation.generation === negotiationGeneration) {
        _inFlightNegotiation.pasteClipboard = pasteClipboard;
    }

    const videoTx = pc.addTransceiver('video', { direction: 'recvonly' });
    try {
        if (
            videoTx &&
            typeof videoTx.setCodecPreferences === 'function' &&
            typeof RTCRtpReceiver !== 'undefined' &&
            typeof RTCRtpReceiver.getCapabilities === 'function'
        ) {
            const caps = RTCRtpReceiver.getCapabilities('video');
            const h264 = (caps.codecs || []).filter(
                (c) => String(c.mimeType || '').toLowerCase() === 'video/h264'
            );
            if (h264.length) {
                videoTx.setCodecPreferences(h264);
            }
        }
    } catch (e) {
        console.warn('AxonOS WebRTC H264 codec preference failed', e);
    }
    // Desktop audio (Opus, agent→browser). When the mic feature is enabled the
    // same transceiver is bidirectional: the browser receives desktop audio and
    // can send mic audio via replaceTrack later (no renegotiation needed). When
    // disabled it stays recvonly — identical to before, no mic path at all.
    const audioTx = pc.addTransceiver('audio', {
        direction: micCapable ? 'sendrecv' : 'recvonly',
    });
    window.axonosWebRtcPc = pc;
    window.axonosWebRtcVideo = video;

    const remoteStream = new MediaStream();
    pc.ontrack = (ev) => {
        console.log('AxonOS WebRTC track', ev.track && ev.track.kind, ev.streams);
        if (ev.receiver && 'playoutDelayHint' in ev.receiver) {
            ev.receiver.playoutDelayHint = 0;
        }
        if (ev.receiver && 'jitterBufferTarget' in ev.receiver) {
            ev.receiver.jitterBufferTarget = 0;
        }
        // Audio and video can arrive as separate remote streams; collect the
        // tracks into one local stream so the later track does not replace the
        // already-attached earlier one on the element.
        remoteStream.addTrack(ev.track);
        if (video.srcObject !== remoteStream) {
            video.srcObject = remoteStream;
        }
        ensureVideoPlaying();
    };

    const pendingIce = [];

    pc.onicecandidate = (ev) => {
        if (!ev.candidate) {
            return;
        }
        const body = {
            wallet_address: wallet,
            session_id: sessionId,
            compute_session_id: computeSessionId,
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

    reportProgress('webrtc-offer');
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    // Do not serialize the server-side display startup behind full browser ICE
    // gathering. onicecandidate above already trickles every route to the gate,
    // so the offer can be posted immediately and both operations can overlap.

    const offerRes = await _fetchJson('./api/webrtc/offer', {
        method: 'POST',
        headers: _authHeaders(),
        body: JSON.stringify({
            wallet_address: wallet,
            session_id: sessionId,
            compute_session_id: computeSessionId,
            sdp: pc.localDescription.sdp,
            type: 'offer',
        }),
    });
    if (!offerRes.ok || !offerRes.json.ok) {
        if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
            return false;
        }
        await _cleanup(pc, video, sessionId, wallet);
        if (webrtcFallbackOk) {
            showFailure('WebRTC negotiation failed — falling back.', 'fallback', 4000);
        } else {
            showFailure('WebRTC negotiation failed.', 'failed', 4000);
        }
        return false;
    }
    reportProgress('webrtc-waiting');

    let answerApplied = false;
    const deadline = Date.now() + answerWaitMs;
    let serverIceCursor = 0;
    let lastSignalingState = '';

    while (Date.now() < deadline && !answerApplied) {
        if (_negotiationCancelled(negotiationGeneration)) {
            await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet);
            return false;
        }
        const st = await _fetchJson(
            `./api/webrtc/status?session_id=${encodeURIComponent(sessionId)}&wallet_address=${encodeURIComponent(wallet)}&compute_session_id=${encodeURIComponent(computeSessionId)}`,
            { headers: _authHeaders() }
        );
        if (!st.ok) {
            if (st.status === 401) {
                if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
                    return false;
                }
                await _cleanup(pc, video, sessionId, wallet);
                const msg = 'WebRTC auth expired — sign in again or use classic VNC if enabled.';
                if (webrtcFallbackOk) {
                    showFailure(msg + ' Falling back.', 'fallback', 8000);
                } else {
                    showFailure(msg, 'failed', 8000);
                }
                return false;
            }
            break;
        }
        const j = st.json;
        const signalingState = String(j.state || '');
        if (signalingState !== lastSignalingState) {
            lastSignalingState = signalingState;
            if (signalingState === 'agent_processing') {
                reportProgress('webrtc-agent');
            } else if (signalingState === 'scoped_offer_received') {
                reportProgress('webrtc-waiting');
            }
        }
        if (j.state === 'failed' || j.last_error) {
            if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
                return false;
            }
            // Capture the server reason before teardown so even an unexpected cleanup
            // exception cannot downgrade a terminal failure into a generic client error.
            const failure = _recordSignalingFailure(j, sessionId);
            await _cleanup(pc, video, sessionId, wallet);
            const msg = failure.message;
            if (webrtcFallbackOk) {
                showFailure(`${msg} — falling back.`, 'fallback', 8000);
            } else {
                showFailure(msg, 'failed', 8000);
            }
            return false;
        }
        if (j.state === 'closed') {
            if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
                return false;
            }
            await _cleanup(pc, video, sessionId, wallet);
            if (webrtcFallbackOk) {
                showFailure('WebRTC session closed — falling back.', 'fallback', 5000);
            } else {
                showFailure('WebRTC session closed.', 'failed', 5000);
            }
            return false;
        }
        if (j.has_answer && j.answer && j.answer.sdp) {
            const answerSdp = _normalizeSdp(j.answer.sdp);
            await pc.setRemoteDescription(
                new RTCSessionDescription({ type: 'answer', sdp: answerSdp })
            );
            answerApplied = true;
            reportProgress('webrtc-ice');
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
            await new Promise((r) => setTimeout(r, 800));
        }
    }

    if (!answerApplied) {
        if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
            return false;
        }
        await _cleanup(pc, video, sessionId, wallet);
        if (webrtcFallbackOk) {
            showFailure('WebRTC timed out — falling back.', 'fallback', 4000);
        } else {
            showFailure('WebRTC timed out.', 'failed', 4000);
        }
        return false;
    }

    if (_negotiationCancelled(negotiationGeneration)) {
        await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet);
        return false;
    }

    await Promise.allSettled(pendingIce);

    reportProgress('webrtc-ice');
    _setBanner('WebRTC: negotiating ICE…', 'connecting');
    if (typeof UI.updateVisualState === 'function') {
        UI.updateVisualState('connecting');
    }
    UI.showStatus('WebRTC: connecting…');

    let inputScaleX = 1;
    let inputScaleY = 1;
    let vidW = 1;
    let vidH = 1;
    let imageLeft = 0;
    let imageTop = 0;
    let imageWidth = 1;
    let imageHeight = 1;

    let cursorHotX = 1;
    let cursorHotY = 1;
    let lastLocalX = -100;
    let lastLocalY = -100;

    const updateCursorShape = (data) => {
        if (!cursor) return;
        if (data && data.img) {
            cursor.innerHTML = '';
            cursor.style.width = data.width + 'px';
            cursor.style.height = data.height + 'px';
            cursor.style.backgroundImage = `url(data:image/png;base64,${data.img})`;
            cursor.style.backgroundSize = 'contain';
            cursor.style.backgroundRepeat = 'no-repeat';
            cursorHotX = Number(data.xhot);
            cursorHotY = Number(data.yhot);
        } else {
            // Default fallback
            cursor.innerHTML = '<svg width="18" height="24" viewBox="0 0 18 24" xmlns="http://www.w3.org/2000/svg"><path d="M1 1v18l5-5 3 8 3-1-3-8h7z" fill="white" stroke="black" stroke-width="1.5" stroke-linejoin="round"/></svg>';
            cursor.style.width = '18px';
            cursor.style.height = '24px';
            cursor.style.backgroundImage = 'none';
            cursorHotX = 1;
            cursorHotY = 1;
        }
        if (lastLocalX >= 0 && lastLocalY >= 0) {
            cursor.style.transform = `translate(${imageLeft + lastLocalX - cursorHotX}px, ${imageTop + lastLocalY - cursorHotY}px)`;
        }
    };

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

    video.addEventListener('loadeddata', syncInputScale, { signal: inputSignal });
    video.addEventListener('loadeddata', ensureVideoPlaying, { signal: inputSignal });
    window.addEventListener('resize', syncInputScale, { signal: inputSignal });

    // Autoplay policy requires the element to start muted; lift the mute on the
    // first real gesture so desktop audio becomes audible without extra UI.
    const unmuteOnGesture = () => {
        video.muted = false;
        ensureVideoPlaying();
    };
    video.addEventListener('pointerdown', unmuteOnGesture, { once: true, signal: inputSignal });
    window.addEventListener('keydown', unmuteOnGesture, { once: true, signal: inputSignal });

    // Microphone (browser→desktop). Only when the operator enabled it; the user
    // still opts in per session via this toggle (which triggers the browser's
    // own getUserMedia permission prompt). The mic stays off until clicked.
    /** @type {MediaStream | null} */
    let micStream = null;
    const micBtn = document.getElementById('axonos_mic_button');
    if (micCapable && micBtn && typeof navigator !== 'undefined' && navigator.mediaDevices) {
        // Reflect mic state on the deck button via CSS classes, and mirror a
        // page-level `axonos-mic-live` class so a persistent indicator stays
        // visible even when the control bar is collapsed (the mic streams to a
        // remote desktop, so its live state must always be glanceable).
        const setMicState = (state) => {
            micBtn.classList.remove('is-live', 'is-pending', 'is-denied');
            if (state === 'live') {
                micBtn.classList.add('is-live');
                micBtn.title = 'Microphone on — click to mute';
                micBtn.setAttribute('aria-label', 'Microphone on');
                micBtn.setAttribute('aria-pressed', 'true');
                document.documentElement.classList.add('axonos-mic-live');
                return;
            }
            document.documentElement.classList.remove('axonos-mic-live');
            micBtn.setAttribute('aria-pressed', 'false');
            if (state === 'pending') {
                micBtn.classList.add('is-pending');
                micBtn.title = 'Requesting microphone…';
                micBtn.setAttribute('aria-label', 'Requesting microphone');
            } else if (state === 'denied') {
                micBtn.classList.add('is-denied');
                micBtn.title = 'Microphone blocked — allow it in the browser to use it';
                micBtn.setAttribute('aria-label', 'Microphone blocked');
            } else {
                micBtn.title = 'Microphone — click to send your mic to the desktop';
                micBtn.setAttribute('aria-label', 'Microphone off');
            }
        };

        const stopMic = () => {
            if (micStream) {
                micStream.getTracks().forEach((t) => t.stop());
                micStream = null;
            }
            const sender = audioTx && audioTx.sender;
            if (sender && typeof sender.replaceTrack === 'function') {
                sender.replaceTrack(null).catch(() => {});
            }
            setMicState('off');
        };

        let micBusy = false;
        const toggleMic = async () => {
            if (micBusy) {
                return;
            }
            micBusy = true;
            try {
                if (micStream) {
                    stopMic();
                    return;
                }
                setMicState('pending');
                const stream = await navigator.mediaDevices.getUserMedia({
                    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                });
                // Negotiation may have been torn down while the prompt was open.
                if (pc.connectionState === 'closed' || !audioTx || !audioTx.sender) {
                    stream.getTracks().forEach((t) => t.stop());
                    setMicState('off');
                    return;
                }
                micStream = stream;
                await audioTx.sender.replaceTrack(stream.getAudioTracks()[0]);
                setMicState('live');
            } catch (e) {
                console.warn('AxonOS WebRTC mic enable failed', e);
                setMicState(e && (e.name === 'NotAllowedError' || e.name === 'SecurityError') ? 'denied' : 'off');
            } finally {
                micBusy = false;
            }
        };

        setMicState('off');
        micBtn.classList.add('is-available');
        micBtn.addEventListener('click', toggleMic, { signal: inputSignal });
        // Release the mic and reset the deck button when the session tears down.
        inputSignal.addEventListener('abort', () => {
            stopMic();
            micBtn.classList.remove('is-available', 'is-live', 'is-pending', 'is-denied');
            document.documentElement.classList.remove('axonos-mic-live');
        });
    }

    let inputChannelOpen = dcInput.readyState === 'open';
    // RFB-style bitmask: 1=left, 2=middle, 4=right (1 << DOM button index).
    let currentMouseButtons = 0;
    // Track currently pressed keys to avoid stuck modifier states.
    const activeKeys = new Map();
    // Deferred press: simple clicks send one atomic `click`; drags send mousedown after move.
    const DRAG_THRESHOLD_PX = 4;
    /** @type {{ button: number, clientX: number, clientY: number } | null} */
    let pendingPress = null;
    let capturedPointerId = null;

    function releaseAllKeys() {
        if (activeKeys.size === 0) {
            return;
        }
        for (const [id, keyObj] of activeKeys.entries()) {
            sendPriorityInput({
                t: 'keyup',
                key: keyObj.key,
                code: keyObj.code,
                ctrlKey: false,
                altKey: false,
                shiftKey: false,
                metaKey: false,
            });
        }
        activeKeys.clear();
    }

    function sendOnChannel(ch, obj) {
        if (!ch || ch.readyState !== 'open') {
            return false;
        }
        try {
            ch.send(JSON.stringify(obj));
            return true;
        } catch (e) {
            console.warn('AxonOS WebRTC input send failed', e);
            return false;
        }
    }

    function moveChannel() {
        return dcMoves.readyState === 'open' ? dcMoves : dcInput;
    }

    function sendInput(obj) {
        return sendOnChannel(dcInput, obj);
    }

    /** @type {Record<string, unknown> | null} */
    let pendingMovePayload = null;
    let moveFlushTimer = null;
    let moveFlushRaf = null;
    const MOVE_FLUSH_MS = 8;
    const MAX_INPUT_BUFFERED = 8192;
    const CONGESTED_INPUT_BUFFERED = 4096;

    function inputCongested() {
        if (dcInput.bufferedAmount > CONGESTED_INPUT_BUFFERED) {
            return true;
        }
        return dcMoves.readyState === 'open' && dcMoves.bufferedAmount > CONGESTED_INPUT_BUFFERED;
    }

    function clearMoveFlushTimer() {
        if (moveFlushTimer !== null) {
            clearTimeout(moveFlushTimer);
            moveFlushTimer = null;
        }
        if (moveFlushRaf !== null) {
            cancelAnimationFrame(moveFlushRaf);
            moveFlushRaf = null;
        }
    }

    function flushPendingMove() {
        moveFlushTimer = null;
        moveFlushRaf = null;
        if (!pendingMovePayload) {
            return;
        }
        const ch = moveChannel();
        if (ch.bufferedAmount > MAX_INPUT_BUFFERED) {
            if (moveFlushTimer === null) {
                moveFlushTimer = setTimeout(flushPendingMove, 16);
            }
            return;
        }
        const payload = pendingMovePayload;
        pendingMovePayload = null;
        sendOnChannel(ch, payload);
    }

    function queueMove(payload, urgent) {
        pendingMovePayload = payload;
        if (moveFlushTimer !== null || moveFlushRaf !== null) {
            return;
        }
        if (urgent) {
            moveFlushRaf = requestAnimationFrame(flushPendingMove);
        } else {
            moveFlushTimer = setTimeout(flushPendingMove, MOVE_FLUSH_MS);
        }
    }

    /** Send click/key/wheel without flushing coalesced moves into a congested SCTP queue. */
    function sendPriorityInput(obj) {
        clearMoveFlushTimer();
        pendingMovePayload = null;
        return sendInput(obj);
    }

    function sendMove(payload, urgent) {
        const ch = moveChannel();
        pendingMovePayload = payload;
        if (!urgent && inputCongested()) {
            return;
        }
        if (ch.bufferedAmount > MAX_INPUT_BUFFERED) {
            if (moveFlushTimer === null && moveFlushRaf === null) {
                moveFlushTimer = setTimeout(() => {
                    moveFlushTimer = null;
                    if (
                        pendingMovePayload &&
                        moveChannel().bufferedAmount <= MAX_INPUT_BUFFERED
                    ) {
                        flushPendingMove();
                    }
                }, 16);
            }
            return;
        }
        queueMove(payload, urgent);
    }

    // Connection-state, data-channel close, and health timeout often arrive for
    // the same outage. Funnel them through one bounded recovery so they cannot
    // tear down/reconnect the peer multiple times concurrently.
    let recoveryTimer = null;
    let recoveryStarted = false;
    function returnToWorkspaceAfterRecoveryFailure() {
        UI.connected = false;
        if (UI.connectionKind === 'webrtc') UI.connectionKind = null;
        if (typeof UI._axonosReturnToHomeAfterDisconnect === 'function') {
            UI._axonosReturnToHomeAfterDisconnect();
            return;
        }
        if (typeof UI.updateVisualState === 'function') {
            UI.updateVisualState('disconnected');
        }
        if (typeof UI.openConnectPanel === 'function') {
            UI.openConnectPanel();
        }
    }

    function scheduleWebRtcRecovery(reason) {
        if (!UI.connected || UI.inhibitReconnect || recoveryStarted || recoveryTimer !== null) {
            return;
        }
        _setBanner(`${reason} — reconnecting…`, 'reconnecting');
        // A control-plane redeploy leaves the gate unreachable for ~40s. Start
        // after a short delay but let UI.connect's own retry/backoff absorb a
        // listener that is still coming up, rather than retrying instantly.
        const recoveryDelayMs = (typeof UI._axonosRecoveryDelayMs === 'function')
            ? UI._axonosRecoveryDelayMs()
            : 1000;
        recoveryTimer = setTimeout(async () => {
            recoveryTimer = null;
            if (!UI.connected || UI.inhibitReconnect || recoveryStarted) {
                return;
            }
            recoveryStarted = true;
            try {
                if (typeof window.axonosWebRtcTeardown === 'function') {
                    await window.axonosWebRtcTeardown();
                } else {
                    await _cleanup(pc, video, sessionId, wallet);
                    _clearOwnedPeerGlobals({
                        pc,
                        video,
                        pasteClipboard,
                        releasePointer: releaseMouseOnFocusLoss,
                    });
                }
            } catch (err) {
                console.warn('AxonOS WebRTC recovery teardown failed', err);
            }
            if (UI.inhibitReconnect) {
                return;
            }
            UI.connected = false;
            if (UI.connectionKind === 'webrtc') UI.connectionKind = null;
            try {
                if (typeof UI.connect === 'function') {
                    // UI.connect owns wallet preflight, claim, the one bounded
                    // negotiation retry, fallback, and final workspace routing.
                    UI.connect();
                } else {
                    returnToWorkspaceAfterRecoveryFailure();
                }
            } catch (err) {
                console.warn('AxonOS WebRTC recovery connect failed', err);
                returnToWorkspaceAfterRecoveryFailure();
            }
        }, recoveryDelayMs);
    }

    dcInput.addEventListener('open', () => {
        inputChannelOpen = true;
        currentMouseButtons = 0;
    });
    dcInput.addEventListener('close', () => {
        inputChannelOpen = false;
        currentMouseButtons = 0;
        activeKeys.clear();
        clearMoveFlushTimer();
        pendingMovePayload = null;
        if (inputHealthTimer) {
            clearInterval(inputHealthTimer);
            inputHealthTimer = null;
        }
        // Only auto-reconnect on an unexpected channel loss. Intentional teardowns
        // (account switch, user disconnect, credit exhaustion) set UI.inhibitReconnect,
        // and must drop to the landing/connect screen — not show "reconnecting…".
        scheduleWebRtcRecovery('Input channel lost');
    });

    // --- Input health check (ping/pong) --------------------------------
    // Send a ping every 10s.  If 3 consecutive pings go unanswered the
    // channel is treated as dead and we trigger reconnection.
    let inputHealthTimer = null;
    let inputPingPending = 0;
    const INPUT_PING_INTERVAL_MS = 10000;
    const INPUT_PING_MAX_MISSED = 3;

    dcInput.addEventListener('message', (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg && msg.t === 'pong') {
            inputPingPending = 0;
        }
        if (msg && msg.t === 'cursor') {
            updateCursorShape(msg);
        }
    });

    function startInputHealthCheck() {
        if (inputHealthTimer) return;
        inputPingPending = 0;
        inputHealthTimer = setInterval(() => {
            if (dcInput.readyState !== 'open') {
                clearInterval(inputHealthTimer);
                inputHealthTimer = null;
                return;
            }
            if (inputPingPending >= INPUT_PING_MAX_MISSED) {
                console.warn('AxonOS WebRTC: input health check failed (%d missed pongs)', inputPingPending);
                clearInterval(inputHealthTimer);
                inputHealthTimer = null;
                // Don't fight an intentional teardown (account switch / user disconnect).
                if (UI.inhibitReconnect) {
                    return;
                }
                scheduleWebRtcRecovery('Input stalled');
                return;
            }
            inputPingPending += 1;
            try { dcInput.send(JSON.stringify({ t: 'ping' })); } catch { /* ignore */ }
        }, INPUT_PING_INTERVAL_MS);
    }

    dcClip.onmessage = (ev) => {
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
        if (typeof UI.pushRemoteClipboardToLocal === 'function') {
            UI.pushRemoteClipboardToLocal(incoming);
        } else if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            navigator.clipboard.writeText(incoming).catch(() => {});
        }
    };

    function pointerToRemote(ev) {
        const r = video.getBoundingClientRect();
        const localX = Math.max(0, Math.min(imageWidth, ev.clientX - r.left - imageLeft));
        const localY = Math.max(0, Math.min(imageHeight, ev.clientY - r.top - imageTop));
        lastLocalX = localX;
        lastLocalY = localY;
        if (cursor) {
            cursor.style.transform = `translate(${imageLeft + localX - cursorHotX}px, ${imageTop + localY - cursorHotY}px)`;
        }
        return {
            x: Math.round(localX * inputScaleX),
            y: Math.round(localY * inputScaleY),
        };
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
                sendPriorityInput(payload);
            }
        }
        currentMouseButtons = 0;
    }

    function sendRemoteClick(ev) {
        pendingPress = null;
        currentMouseButtons = 0;
        const sent = sendPriorityInput({
            t: 'click',
            button: domButtonToXdotool(ev.button),
            ...pointerToRemote(ev),
        });
        if (!sent) {
            console.warn('AxonOS WebRTC click dropped — input channel unavailable');
        }
    }

    function beginDragPress(pending, ev) {
        pendingPress = null;
        pressMouseButton({
            button: pending.button,
            clientX: pending.clientX,
            clientY: pending.clientY,
        });
    }

    function pressMouseButton(ev) {
        const bit = domButtonMask(ev.button);
        if (currentMouseButtons & bit) {
            // Lost mouseup while focus was on the host OS (copy/paste) — release
            // remotely and accept this press so the click still registers.
            const coords = pointerToRemote(ev);
            currentMouseButtons &= ~bit;
            sendPriorityInput({
                t: 'mouseup',
                button: domButtonToXdotool(ev.button),
                buttons: currentMouseButtons,
                ...coords,
            });
        }
        currentMouseButtons |= bit;
        const sent = sendPriorityInput({
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
            sendPriorityInput({
                t: 'mouseup',
                button: domButtonToXdotool(ev.button),
                buttons: currentMouseButtons,
                ...coords,
            });
            return;
        }
        currentMouseButtons &= ~bit;
        sendPriorityInput({
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
        sendMove(
            { t: 'move', buttons: currentMouseButtons, ...pointerToRemote(ev) },
            dragging
        );
    }
    window.addEventListener('mousemove', onWindowMouseMove, { signal: inputSignal });

    /** Retain pointer coords when dragging outside the letterboxed video bounds. */
    function onVideoPointerDown(ev) {
        if (ev.pointerType === 'touch') {
            return;
        }
        if (video.setPointerCapture && typeof video.setPointerCapture === 'function') {
            try {
                video.setPointerCapture(ev.pointerId);
                capturedPointerId = ev.pointerId;
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
            if (capturedPointerId === ev.pointerId) {
                capturedPointerId = null;
            }
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
        sendPriorityInput({
            t: 'wheel',
            ...pointerToRemote(ev),
            dy,
            dx,
        });
    }
    video.addEventListener('wheel', onVideoWheel, { passive: false, signal: inputSignal });

    function onVideoMouseDown(ev) {
        ev.preventDefault();
        if (typeof video.focus === 'function') {
            video.focus();
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

    video.addEventListener('contextmenu', (ev) => {
        ev.preventDefault();
    }, { signal: inputSignal });
    video.addEventListener('mouseleave', () => {
        if (currentMouseButtons !== 0) {
            return;
        }
        lastLocalX = -100;
        lastLocalY = -100;
        if (cursor) {
            cursor.style.transform = 'translate(-100px,-100px)';
        }
    }, { signal: inputSignal });

    function releaseMouseOnFocusLoss() {
        pendingPress = null;
        if (
            capturedPointerId !== null &&
            video.releasePointerCapture &&
            typeof video.releasePointerCapture === 'function'
        ) {
            try {
                video.releasePointerCapture(capturedPointerId);
            } catch { /* ignore */ }
            capturedPointerId = null;
        }
        resetMouseInputState(null, true);
        releaseAllKeys();
    }

    function hasActivePointerCapture() {
        return (
            currentMouseButtons !== 0 ||
            pendingPress !== null ||
            capturedPointerId !== null
        );
    }

    /** Focus moved off the video within the page (e.g. clipboard panel); window blur does not fire. */
    function onVideoFocusOut(ev) {
        const next = ev.relatedTarget;
        if (next && video.contains(next)) {
            return;
        }
        if (!hasActivePointerCapture()) {
            return;
        }
        releaseMouseOnFocusLoss();
    }
    video.addEventListener('focusout', onVideoFocusOut, { signal: inputSignal });

    window.axonosWebRtcReleasePointerState = releaseMouseOnFocusLoss;
    if (_inFlightNegotiation && _inFlightNegotiation.generation === negotiationGeneration) {
        _inFlightNegotiation.releasePointer = releaseMouseOnFocusLoss;
    }

    function onVisibilityChange() {
        if (document.visibilityState === 'visible') {
            if (currentMouseButtons !== 0) {
                releaseMouseOnFocusLoss();
            }
        } else {
            releaseMouseOnFocusLoss();
        }
    }
    window.addEventListener('blur', releaseMouseOnFocusLoss, { signal: inputSignal });
    document.addEventListener('visibilitychange', onVisibilityChange, { signal: inputSignal });

    function isLocalTextTarget(target) {
        if (!target) {
            return false;
        }
        const tag = target.tagName ? target.tagName.toLowerCase() : '';
        return tag === 'input' || tag === 'textarea' || target.isContentEditable === true;
    }

    window.addEventListener('keydown', (ev) => {
        if (!UI.connected) {
            return;
        }
        if (isLocalTextTarget(ev.target)) {
            return;
        }
        if (ev.key) {
            ev.preventDefault();
            activeKeys.set(ev.code || ev.key, { key: ev.key, code: ev.code });
            sendPriorityInput({
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
            activeKeys.delete(ev.code || ev.key);
            sendPriorityInput({
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
            }).catch(() => {
                // Optional telemetry: a transient network/gate blip must never
                // surface as a fatal noVNC popup (unhandled rejection). Swallow it.
            });
        }).catch(() => {});
    };

    let disconnectGraceTimer = null;
    let disconnectBannerShown = false;
    pc.onconnectionstatechange = () => {
        if (!UI.connected) {
            return;
        }
        const st = pc.connectionState;
        if (st === 'failed') {
            if (disconnectGraceTimer) {
                clearTimeout(disconnectGraceTimer);
                disconnectGraceTimer = null;
            }
            disconnectBannerShown = true;
            _setBanner('WebRTC disconnected.', 'failed');
            scheduleWebRtcRecovery('WebRTC disconnected');
        } else if (st === 'disconnected') {
            // Transient by spec (missed consent checks); usually self-heals in
            // seconds. Escalate to the red banner only if it persists.
            disconnectBannerShown = true;
            _setBanner('WebRTC: reconnecting…', 'reconnecting');
            if (!disconnectGraceTimer) {
                disconnectGraceTimer = setTimeout(() => {
                    disconnectGraceTimer = null;
                    if (pc.connectionState === 'disconnected') {
                        _setBanner('WebRTC disconnected.', 'failed');
                        scheduleWebRtcRecovery('WebRTC disconnected');
                    }
                }, 5000);
            }
        } else if (st === 'connected' && disconnectBannerShown) {
            if (disconnectGraceTimer) {
                clearTimeout(disconnectGraceTimer);
                disconnectGraceTimer = null;
            }
            disconnectBannerShown = false;
            _setBanner('WebRTC: Connected', 'connected');
            setTimeout(() => {
                if (!disconnectBannerShown && pc.connectionState === 'connected') {
                    _hideBanner();
                }
            }, 2000);
        }
    };

    const iceOk = await _waitIceConnected(pc, 90000);
    if (!iceOk) {
        inputAbort.abort();
        if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
            return false;
        }
        await _cleanup(pc, video, sessionId, wallet);
        _clearOwnedPeerGlobals({
            pc,
            video,
            pasteClipboard,
            releasePointer: releaseMouseOnFocusLoss,
        });
        if (webrtcFallbackOk) {
            showFailure('WebRTC ICE failed — falling back.', 'fallback', 5000);
        } else {
            showFailure('WebRTC ICE failed.', 'failed', 5000);
        }
        if (!deferFailureUi && typeof UI.updateVisualState === 'function') {
            UI.updateVisualState('disconnected');
        }
        if (!deferFailureUi) {
            UI.showStatus('WebRTC connection failed (ICE).', 'error');
        }
        return false;
    }

    if (await _finishNegotiationCancelled(negotiationGeneration, pc, video, sessionId, wallet)) {
        return false;
    }

    metricsTimer = setInterval(pollStats, 5000);
    pollStats();
    startInputHealthCheck();

    _inFlightNegotiation = null;
    UI.connected = true;
    UI.connectionKind = 'webrtc';
    window.axonosSessionDetached = false;
    // Mark the page as WebRTC-active so the theme hides RFB-only controls
    // (viewport drag, modifier keys, RFB encoding/transport settings) that do
    // nothing on this path. Removed in _cleanup.
    document.documentElement.classList.add('axonos-webrtc-active');
    UI.inhibitReconnect = false;
    // Recovery succeeded (or a fresh connect landed): the next outage starts
    // from the shortest delay, and a later user-initiated connect is no longer
    // mistaken for a recovery (which would skip the wallet prompt).
    if (typeof UI._axonosResetReconnectBackoff === 'function') {
        UI._axonosResetReconnectBackoff();
    }
    if (typeof window.axonosHideConnectionLoader === 'function') {
        window.axonosHideConnectionLoader(true);
    }
    UI.updateVisualState('connected');
    UI.showStatus('Connected (WebRTC)');
    _setBanner('WebRTC: Connected', 'connected');
    setTimeout(_hideBanner, 2000);
    ensureVideoPlaying();

    if (typeof UI._axgtStartSessionBillingPoll === 'function') {
        UI._axgtStartSessionBillingPoll();
    }
    if (typeof UI.updateSessionControlButtons === 'function') {
        UI.updateSessionControlButtons();
    }

    const connectedPeerOwner = {
        pc,
        video,
        pasteClipboard,
        releasePointer: releaseMouseOnFocusLoss,
        teardown: null,
    };
    let connectedTeardownPromise = null;
    const teardownWebRtcSession = () => {
        if (connectedTeardownPromise) {
            return connectedTeardownPromise;
        }
        connectedTeardownPromise = (async () => {
            try {
                try { stopMic(); } catch { /* ignore */ }
                _negotiationGeneration += 1;
                resetMouseInputState(null, true);
                releaseAllKeys();
                inputAbort.abort();
                clearMoveFlushTimer();
                pendingMovePayload = null;
                if (metricsTimer) {
                    clearInterval(metricsTimer);
                }
                if (inputHealthTimer) {
                    clearInterval(inputHealthTimer);
                    inputHealthTimer = null;
                }
                if (disconnectGraceTimer) {
                    clearTimeout(disconnectGraceTimer);
                    disconnectGraceTimer = null;
                }
                if (recoveryTimer) {
                    clearTimeout(recoveryTimer);
                    recoveryTimer = null;
                }
                if (typeof UI.stopClipboardAutoSync === 'function') {
                    try { UI.stopClipboardAutoSync(); } catch { /* ignore */ }
                }
                if (container) {
                    container.style.cursor = prevContainerCursor;
                }
                await _cleanup(pc, video, sessionId, wallet);
            } finally {
                _clearOwnedPeerGlobals(connectedPeerOwner);
                if (_inFlightNegotiation && _inFlightNegotiation.pc === pc) {
                    _inFlightNegotiation = null;
                }
            }
        })();
        return connectedTeardownPromise;
    };
    connectedPeerOwner.teardown = teardownWebRtcSession;
    window.axonosWebRtcTeardown = teardownWebRtcSession;

    return true;
}

if (typeof window !== 'undefined') {
    window.axonosCancelWebRtcNegotiation = cancelAxonOSWebRTCNegotiation;
}

async function _cleanup(pc, video, sessionId, wallet) {
    if (sessionId && wallet) {
        try {
            await _fetchJson('./api/webrtc/close', {
                method: 'POST',
                headers: _authHeaders(),
                body: JSON.stringify({ wallet_address: wallet, session_id: sessionId }),
            });
        } catch {
            /* ignore */
        }
    }
    if (pc) {
        try {
            pc.getSenders().forEach((s) => s.track && s.track.stop());
            await pc.close();
        } catch {
            /* ignore */
        }
    }
    const ownsPageState = typeof window === 'undefined' ||
        window.axonosWebRtcPc === pc || window.axonosWebRtcVideo === video;
    if (video && video.parentNode) {
        video.parentNode.removeChild(video);
    }
    const cursor = video && video._axonosWebRtcCursor;
    if (cursor && cursor.parentNode) {
        cursor.parentNode.removeChild(cursor);
    }
    if (ownsPageState) {
        document.documentElement.classList.remove('axonos-webrtc-active', 'axonos-mic-live');
        _hideBanner();
    }
}
