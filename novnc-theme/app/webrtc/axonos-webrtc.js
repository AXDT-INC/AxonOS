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

    if (typeof RTCPeerConnection === 'undefined') {
        if (cfgRes.json.webrtc_fallback_enabled !== false) {
            _setBanner('WebRTC not supported in this browser — using classic stream.', 'fallback');
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

    const pc = new RTCPeerConnection({ iceServers });
    const dc = pc.createDataChannel('axonos-input', { ordered: true });

    pc.addTransceiver('video', { direction: 'recvonly' });

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
        _setBanner('WebRTC negotiation failed — falling back.', 'fallback');
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
        if (j.has_answer && j.answer && j.answer.sdp) {
            await pc.setRemoteDescription(
                new RTCSessionDescription({ type: 'answer', sdp: j.answer.sdp })
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
        _setBanner('WebRTC timed out — falling back.', 'fallback');
        setTimeout(_hideBanner, 4000);
        return false;
    }

    await Promise.allSettled(pendingIce);

    _setBanner('WebRTC: Connected', 'connected');
    setTimeout(_hideBanner, 2000);

    if (container) {
        container.appendChild(video);
    } else {
        document.body.appendChild(video);
    }

    pc.ontrack = (ev) => {
        if (ev.streams && ev.streams[0]) {
            video.srcObject = ev.streams[0];
        }
    };

    let inputScaleX = 1;
    let inputScaleY = 1;
    let vidW = 1;
    let vidH = 1;

    const syncInputScale = () => {
        const rw = video.videoWidth || video.clientWidth || 1;
        const rh = video.videoHeight || video.clientHeight || 1;
        const cw = video.clientWidth || 1;
        const ch = video.clientHeight || 1;
        vidW = rw;
        vidH = rh;
        inputScaleX = rw / cw;
        inputScaleY = rh / ch;
    };

    video.addEventListener('loadeddata', syncInputScale);

    function sendInput(obj) {
        if (dc.readyState === 'open') {
            dc.send(JSON.stringify(obj));
        }
    }

    video.addEventListener('mousemove', (ev) => {
        const r = video.getBoundingClientRect();
        const x = (ev.clientX - r.left) * inputScaleX;
        const y = (ev.clientY - r.top) * inputScaleY;
        sendInput({ t: 'move', x: Math.round(x), y: Math.round(y) });
    });
    video.addEventListener('mousedown', (ev) => {
        sendInput({ t: 'click', button: ev.button + 1 });
    });

    window.addEventListener('keydown', (ev) => {
        if (!UI.connected) {
            return;
        }
        if (ev.target && ev.target.tagName === 'INPUT') {
            return;
        }
        if (ev.key && ev.key.length === 1) {
            ev.preventDefault();
            sendInput({ t: 'key', key: ev.key });
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
        video.focus();
    } catch {
        /* ignore */
    }

    window.axonosWebRtcTeardown = async () => {
        if (metricsTimer) {
            clearInterval(metricsTimer);
        }
        await _cleanup(pc, video, sessionId, wallet);
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
    _hideBanner();
}
