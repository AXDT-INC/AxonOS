/* App-origin privacy preferences shared with the landing through a restricted
 * iframe. No click, wallet or attribution capability is sent to its parent.
 * The cookie contains choices only; the local registry is used only to revoke.
 */
const COOKIE = '__Host-axonos_privacy_v1';
const REGISTRY = 'axonos_privacy_revocations_v1';
const SIGNAL = 'axonos_privacy_change_v1';
const BLOCKED = 'axonos_privacy_blocked_v1';
const LOCK = 'axonos_privacy_preferences_v1';
const LIMIT = 16;
const TOKEN = /^[A-Za-z0-9_-]{32,2048}$/;
const NONCE = /^[A-Za-z0-9_-]{32,128}$/;
let singleton;

export function initializePrivacyPreferences() {
    if (!singleton) singleton = start();
    return singleton;
}

async function start() {
    if (document.readyState === 'loading') await new Promise(resolve =>
        document.addEventListener('DOMContentLoaded', resolve, { once: true }));
    const host = document.getElementById('axonos-attribution-preferences');
    if (!host) throw new Error('Privacy settings host unavailable');
    const response = await fetch('/api/x-attribution/status', {
        credentials: 'same-origin', cache: 'no-store', referrerPolicy: 'no-referrer',
        signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error('Privacy configuration unavailable');
    const status = await response.json();
    const config = status.preferences;
    if (!config || !config.enabled) return null;
    if (location.origin !== config.app_origin) throw new Error('Privacy origin mismatch');
    const embedded = window.parent !== window;
    const banner = document.getElementById('axonos-privacy-banner');
    const listeners = new Set();
    let message = '';
    let busy = false;
    let expanded = false;
    let unavailable = false;
    let lastRender = '';
    const lifetime = Math.min(Number(config.ttl_days) * 86400000, 90 * 86400000);
    if (!(lifetime > 0) || !navigator.locks) {
        host.textContent = 'Optional sharing is unavailable in this browser. You can still use AxonOS.';
        throw new Error('Privacy storage coordination unavailable');
    }
    const now = () => Date.now();
    const gpc = () => navigator.globalPrivacyControl === true || status.gpc_applied === true;

    function readChoice() {
        try {
            const values = document.cookie.split(';').map(s => s.trim()).filter(s => s.startsWith(COOKIE + '='));
            if (values.length !== 1) return null;
            const p = JSON.parse(decodeURIComponent(values[0].slice(COOKIE.length + 1)));
            if (p.v !== 1 || p.policy !== config.policy_version || p.epoch !== config.policy_epoch ||
                !['allow', 'deny'].includes(p.choice) || !Number.isFinite(p.at) ||
                !Number.isFinite(p.until) || p.at > now() || p.until <= now() ||
                p.until > p.at + lifetime || Object.keys(p).length !== 6) return null;
            return p;
        } catch (_) { return null; }
    }

    function saveChoice(choice) {
        const at = now();
        const p = { v: 1, policy: config.policy_version, epoch: config.policy_epoch,
            choice, at, until: at + lifetime };
        document.cookie = COOKIE + '=' + encodeURIComponent(JSON.stringify(p)) +
            '; Path=/; Secure; SameSite=Strict; Max-Age=' + Math.floor(lifetime / 1000);
        if (readChoice()?.choice !== choice) throw new Error('Preference cookie blocked');
    }

    function registry() {
        const raw = localStorage.getItem(REGISTRY);
        if (raw && raw.length > 60000) throw new Error('Invalid privacy registry');
        const entries = JSON.parse(raw || '[]');
        if (!Array.isArray(entries) || entries.length > LIMIT || entries.some(p =>
            !p || !TOKEN.test(p.context) || !NONCE.test(p.csrf) ||
            !Number.isFinite(p.expires) || typeof p.pending !== 'boolean')) {
            throw new Error('Invalid privacy registry');
        }
        return entries.filter(p => p.expires * 1000 > now());
    }

    function writeRegistry(entries) {
        const raw = JSON.stringify(entries);
        localStorage.setItem(REGISTRY, raw);
        if (localStorage.getItem(REGISTRY) !== raw) throw new Error('Privacy storage unavailable');
    }

    function permitted() {
        try {
            return !unavailable && !gpc() && status.enabled === true &&
                localStorage.getItem(BLOCKED) !== '1' &&
                readChoice()?.choice === 'allow' && !registry().some(p => p.pending);
        } catch (_) { return false; }
    }

    function notify() {
        for (const fn of listeners) { try { fn(permitted()); } catch (_) {} }
        try { localStorage.setItem(SIGNAL, String(now()) + ':' + Math.random()); } catch (_) {}
        resize();
    }

    function resize() {
        if (embedded) window.parent.postMessage({ type: 'axonos-privacy-size-v1',
            height: Math.ceil(document.body.getBoundingClientRect().height) }, config.landing_origin);
    }

    async function revokeAll() {
        const entries = registry();
        // Persist the complete pending set before the first network request.
        for (const item of entries) item.pending = true;
        writeRegistry(entries);
        notify();
        for (const item of entries) {
            const headers = { 'Content-Type': 'application/json',
                'X-AxonOS-Attribution': item.context, 'X-AxonOS-CSRF': item.csrf };
            if (gpc()) headers['Sec-GPC'] = '1';
            const r = await fetch('/api/x-attribution/consent', {
                method: 'POST', credentials: 'same-origin', cache: 'no-store',
                referrerPolicy: 'no-referrer', headers, body: JSON.stringify({ action: 'revoke' }),
                signal: AbortSignal.timeout(10000),
            });
            const result = await r.json();
            if (!r.ok || result.ok !== true || !['revoked', 'denied'].includes(result.state)) {
                throw new Error('Withdrawal awaiting confirmation');
            }
            writeRegistry(registry().filter(p => p.csrf !== item.csrf));
        }
    }

    function locked(fn) {
        return navigator.locks.request(LOCK, { mode: 'exclusive', signal: AbortSignal.timeout(30000) }, fn);
    }

    async function choose(choice) {
        if (busy) return;
        busy = true; message = ''; render();
        let cookieFailed = false;
        try {
            if (choice === 'deny' || gpc()) {
                // Block browser producers immediately, even while an older
                // grant request still owns the registry lock.
                unavailable = true;
                try { localStorage.setItem(BLOCKED, '1'); } catch (_) {}
                try { saveChoice('deny'); } catch (_) { cookieFailed = true; }
                notify();
            }
            await locked(async () => {
                if (gpc()) choice = 'deny';
                // A new acceptance cannot erase an unconfirmed withdrawal.
                if (choice === 'allow' && registry().some(p => p.pending)) await revokeAll();
                if (choice === 'deny') {
                    try { saveChoice('deny'); } catch (_) { cookieFailed = true; }
                } else {
                    saveChoice(choice);
                    localStorage.removeItem(BLOCKED);
                    if (localStorage.getItem(BLOCKED) === '1') throw new Error('Sharing remains blocked');
                }
                notify();
                if (choice === 'deny') await revokeAll();
            });
            if (cookieFailed) throw new Error('Preference could not be saved');
            unavailable = false;
            expanded = false;
        } catch (_) {
            message = 'Your change could not be fully confirmed. Sharing stays off. Retry in Privacy settings.';
            unavailable = true;
        } finally {
            busy = false; notify(); render();
            host.querySelector('button')?.focus();
        }
    }

    async function track(capability, fn) {
        return locked(async () => {
            if (!permitted()) throw new Error('Sharing not permitted');
            if (!TOKEN.test(capability.context) || !NONCE.test(capability.csrf) ||
                !Number.isFinite(capability.expires) || capability.expires * 1000 <= now()) {
                throw new Error('Invalid revocation capability');
            }
            const entries = registry();
            if (!entries.some(p => p.csrf === capability.csrf)) {
                if (entries.length >= LIMIT) throw new Error('Privacy registry full');
                // The unset ticket can revoke this handle even if the grant response
                // is lost or the tab closes. Registration precedes any grant request.
                entries.push({ ...capability, pending: false });
                writeRegistry(entries);
            }
            return fn ? await fn() : undefined;
        });
    }

    function button(label, action) {
        const b = document.createElement('button');
        b.type = 'button'; b.textContent = label; b.disabled = busy;
        b.style.cssText = 'padding:.5rem .75rem;min-height:44px;border:1px solid #8fa2b7;border-radius:6px;background:#171d25;color:#f6f8fa;cursor:pointer;font:inherit;';
        b.addEventListener('click', action); return b;
    }

    function render() {
        const signature = JSON.stringify([readChoice(), permitted(), gpc(), busy, expanded, message]);
        if (signature === lastRender) return;
        lastRender = signature;
        if (banner && !readChoice() && host.parentNode !== banner) {
            banner.append(host);
        }
        host.replaceChildren();
        const toggle = button('Privacy settings', () => {
            expanded = !expanded; render(); host.querySelector('button')?.focus();
        });
        toggle.setAttribute('aria-expanded', String(expanded || !readChoice()));
        host.append(toggle);
        if (readChoice() && !expanded && !message && !gpc()) {
            const saved = document.createElement('span');
            saved.textContent = permitted() ? ' Advertising measurement allowed.' : ' Optional sharing is off.';
            host.append(saved);
        }
        if (!readChoice() || expanded || message || gpc()) {
            const text = document.createElement('p');
            text.textContent = gpc()
                ? 'Your browser privacy signal keeps optional X conversion sharing off.'
                : 'Optional X measurement connects ads to app activity. Accepting covers this landing page and the app; your choice is saved in a cookie. AxonOS works if you reject.';
            host.append(text);
            const actions = document.createElement('div');
            actions.style.cssText = 'display:flex;flex-wrap:wrap;gap:.5rem;';
            if (!gpc() && status.enabled) actions.append(button('Accept optional', () => choose('allow')));
            actions.append(button('Reject optional', () => choose('deny')));
            actions.append(button('Preferences', () => { expanded = true; renderDetails(); }));
            host.append(actions);
            if (expanded) renderDetails();
        }
        if (message) {
            const error = document.createElement('p'); error.setAttribute('role', 'status');
            error.textContent = message; host.append(error);
        }
        resize();
    }

    function renderDetails() {
        if (host.querySelector('[data-privacy-details]')) return;
        const section = document.createElement('div'); section.dataset.privacyDetails = '';
        const label = document.createElement('label');
        const box = document.createElement('input'); box.type = 'checkbox';
        box.checked = permitted(); box.disabled = gpc() || !status.enabled || busy;
        label.append(box, ' Advertising measurement — X conversion sharing');
        const info = document.createElement('p');
        info.textContent = 'Essential app storage stays enabled. Optional sharing sends X a click ID, event type, time and deduplication ID, not wallet addresses, payment amounts or desktop contents. Your choice is saved in an app-host cookie for up to ' +
            Math.floor(lifetime / 86400000) + ' days across the landing and app. Encrypted withdrawal capabilities stay in app-origin browser storage until their attribution periods expire. Rejecting cancels unsent events after server confirmation; sent or in-flight events may not be recallable. Clearing browser storage removes these browser-held withdrawal capabilities. This preference covers this browser, not other devices.';
        const link = document.createElement('a'); link.href = 'https://x.com/en/privacy';
        link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'X privacy policy';
        section.append(label, info, link, button('Save preferences', () => choose(box.checked ? 'allow' : 'deny')));
        host.append(section); resize();
    }

    async function refresh() {
        try {
            if (gpc()) {
                try { localStorage.setItem(BLOCKED, '1'); } catch (_) {}
                try { saveChoice('deny'); } catch (_) {}
            }
            await locked(async () => {
                const entries = registry();
                // Remove expired capabilities on the next privacy check even
                // if no new grant or withdrawal is requested.
                if (localStorage.getItem(REGISTRY) !== null) writeRegistry(entries);
                if (gpc() || localStorage.getItem(BLOCKED) === '1' ||
                    readChoice()?.choice !== 'allow' || entries.some(p => p.pending)) {
                    if (entries.length) await revokeAll();
                }
            });
        } catch (_) { unavailable = true; message = 'Withdrawal needs confirmation. Open Privacy settings to retry.'; }
        for (const fn of listeners) { try { fn(permitted()); } catch (_) {} }
        render();
    }
    window.addEventListener('storage', event => {
        if ([REGISTRY, SIGNAL, BLOCKED].includes(event.key)) {
            for (const fn of listeners) { try { fn(permitted()); } catch (_) {} }
            render();
        }
    });
    window.addEventListener('pageshow', () => { void refresh(); });
    window.addEventListener('focus', () => {
        // A normal focus event precedes click. Re-rendering here would remove
        // its target before the visitor's click handler can run.
        if (gpc() && readChoice()?.choice !== 'deny') void refresh();
    });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') void refresh();
    });
    if (embedded) new ResizeObserver(resize).observe(host);
    await refresh();
    return Object.freeze({ permitted, track, choice: () =>
        localStorage.getItem(BLOCKED) === '1' ? 'deny' : readChoice()?.choice || 'unset',
        subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
        reportError() {
            unavailable = true;
            message = 'Optional sharing is unavailable. Your app access is unchanged. Retry in Privacy settings.';
            notify(); render();
        },
        show() { expanded = true; render(); }, refresh });
}
