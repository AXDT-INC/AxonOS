/* Runs only inside a nonce-protected clean document. No analytics/resources. */
(function () {
    'use strict';
    const PAGE = AXONOS_HANDOFF_PAGE;
    const BASE = '/api/x-attribution/handoff/';
    const HKEY = 'axonos_x_handoff_pending_v2';
    const CONTEXT = 'axonos_x_attribution_context_v1';
    const OWNER = 'axonos_x_attribution_tab_owner_v1';
    const REVOKE = 'axonos_x_attribution_revoke_capability_v1';
    const PENDING = 'axonos_x_attribution_revoke_pending_v1';
    let generation = 0;
    let stopped = false;
    let activePositive = null;
    let choiceInFlight = false;
    let suppressionInFlight = null;
    function gpc() { return PAGE.server_gpc === true || navigator.globalPrivacyControl === true; }
    function uuid() { return crypto.randomUUID(); }
    function eraseH() { try { sessionStorage.removeItem(HKEY); } catch (_) {} }
    function fallback() {
        if (gpc() && !stopped) { suppress().finally(fallback); return; }
        stopped = true;
        generation += 1;
        if (activePositive) activePositive.abort();
        eraseH();
        window.axonosAttributionContext = '';
        document.getElementById('status').textContent = 'Sharing is unavailable. Continue to AxonOS normally.';
    }
    async function post(op, value, privacy) {
        if (!privacy && gpc()) { await suppress(); throw new Error('suppressed'); }
        if (!privacy && stopped) throw new Error('suppressed');
        const observed = generation;
        // Privacy requests never share a controller or cancellation epoch with
        // positive work. Its aborted callback, fallback, and pagehide must not
        // cancel a withdrawal that has already started.
        const controller = privacy ? null : new AbortController();
        if (controller) activePositive = controller;
        let timer;
        try {
            const pending = (async function () {
                const response = await fetch(BASE + op, {method: 'POST', credentials: 'same-origin',
                    cache: 'no-store', redirect: 'error', keepalive: privacy === true,
                    ...(controller ? {signal: controller.signal} : {}),
                    headers: {'Content-Type': 'application/json', 'X-AxonOS-Handoff': '2'},
                    body: JSON.stringify(value)});
                const result = await response.json();
                // A signal observed during any positive response is latched
                // before fallback, even without a later lifecycle event.
                if (!privacy && gpc()) { await suppress(); throw new Error('suppressed'); }
                if (!response.ok || !result || result.v !== 2 || result.ok !== true ||
                    (!privacy && (observed !== generation || stopped || gpc()))) throw new Error('unavailable');
                return result;
            }());
            return await Promise.race([pending, new Promise(function (_, reject) {
                timer = setTimeout(function () {
                    if (controller) controller.abort();
                    // Bound the UI wait without aborting an accepted privacy
                    // request. The pending marker survives an uncertain result.
                    reject(new Error('unavailable'));
                }, 5000);
            })]);
        } finally {
            clearTimeout(timer);
            if (activePositive === controller) activePositive = null;
        }
    }

    function privateCapability(result) {
        sessionStorage.setItem(REVOKE, JSON.stringify({context: result.context, csrf: result.csrf}));
    }
    async function suppress() {
        // Initial boot, pageshow and response-time GPC can overlap. Reuse the
        // same privacy request instead of aborting its own closure attempt.
        if (suppressionInFlight) return suppressionInFlight;
        generation += 1;
        stopped = true;
        if (activePositive) activePositive.abort();
        eraseH();
        window.axonosAttributionContext = '';
        try {
            sessionStorage.removeItem(CONTEXT);
            sessionStorage.setItem(PENDING, '1');
            if (PAGE.result) privateCapability(PAGE.result);
        } catch (_) {}
        suppressionInFlight = (async function () {
            try { await post('withdraw', {v: 2, request_id: uuid(), reason: gpc() ? 'gpc' : 'denied'}, true); }
            catch (_) { /* Capability and pending marker survive for ordinary app retry. */ }
        }());
        try { await suppressionInFlight; }
        finally { suppressionInFlight = null; }
    }
    async function install(result) {
        if (gpc() || stopped || !navigator.locks) { await suppress(); throw new Error('suppressed'); }
        if (!result || result.state !== 'redeemed' || result.expires_at_ms <= Date.now() ||
            !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(result.ownership_id)) {
            throw new Error('invalid');
        }
        const observed = generation;
        await navigator.locks.request('axonos_x_attribution_tab_ownership_v1:' + result.ownership_id,
            {mode: 'exclusive', ifAvailable: true}, async function (lock) {
                if (!lock || observed !== generation || gpc() || stopped) throw new Error('ambiguous');
                // The app reacquires this same stable lock and rechecks server
                // state before exposing context to independently authenticated work.
                privateCapability(result);
                if (sessionStorage.getItem(PENDING) === '1') throw new Error('withdrawal pending');
                sessionStorage.setItem(OWNER, result.ownership_id);
                sessionStorage.setItem(CONTEXT, result.context);
                eraseH();
            });
        if (observed !== generation || gpc() || stopped) { await suppress(); throw new Error('suppressed'); }
        window.location.replace('https://app.axonos.io/');
    }
    async function grant() {
        if (gpc()) return suppress().finally(fallback);
        if (choiceInFlight || stopped || !navigator.locks || !crypto.randomUUID) return fallback();
        choiceInFlight = true;
        const observed = generation;
        try {
            await navigator.locks.request('axonos_x_handoff_prepare_v2', {mode: 'exclusive', ifAvailable: true}, async function (lock) {
                if (!lock || observed !== generation || gpc() || stopped) throw new Error('ambiguous');
                await post('prepare', {v: 2, request_id: uuid(), choice: 'grant', policy: 'app-x-v2'});
                // This separate request confirms the HttpOnly cookie. Neither
                // prepare nor begin is automatically retried after ambiguity.
                const result = await post('begin', {v: 2, request_id: uuid(), choice: 'grant', policy: 'app-x-v2', destination: 'desktop'});
                if (observed !== generation || gpc() || stopped || result.state !== 'app_granted' ||
                    !/^[A-Za-z0-9_-]{43}$/.test(result.handoff) || result.expires_at_ms <= Date.now()) throw new Error('invalid');
                sessionStorage.setItem(HKEY, JSON.stringify({handoff: result.handoff, expires_at_ms: result.expires_at_ms}));
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = 'https://axonos.io/attribution/authorize';
                form.enctype = 'application/x-www-form-urlencoded';
                for (const pair of [['v', '2'], ['handoff', result.handoff]]) {
                    const field = document.createElement('input');
                    field.type = 'hidden'; field.name = pair[0]; field.value = pair[1]; form.appendChild(field);
                }
                document.body.appendChild(form);
                // H comes only from this begin result: no URL, message, stored
                // transaction, or caller-supplied H is forwarded by app origin.
                form.submit();
            });
        } catch (_) { fallback(); }
        finally { choiceInFlight = false; }
    }
    window.axonosAttributionContext = '';
    if (PAGE.kind === 'entry') {
        document.getElementById('grant').addEventListener('click', grant);
        document.getElementById('deny').addEventListener('click', function () {
            suppress().finally(function () { window.location.replace('https://app.axonos.io/'); });
        });
        // A saved H may only be recovered, never forwarded or silently replaced.
        try {
            const pending = JSON.parse(sessionStorage.getItem(HKEY) || 'null');
            if (pending && !gpc() && pending.expires_at_ms > Date.now()) {
                post('recover', {v: 2, request_id: uuid(), handoff: pending.handoff}).then(install).catch(fallback);
            } else eraseH();
        } catch (_) { fallback(); }
    } else if (PAGE.kind === 'callback') {
        install(PAGE.result).catch(fallback);
    }
    if (gpc()) suppress().finally(fallback);
    window.addEventListener('pagehide', function () {
        generation += 1; stopped = true; if (activePositive) activePositive.abort(); window.axonosAttributionContext = '';
    });
    window.addEventListener('pageshow', function (event) {
        if (event.persisted) fallback();
        if (gpc()) suppress().finally(fallback);
    });
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && gpc()) suppress().finally(fallback);
    });
}());
