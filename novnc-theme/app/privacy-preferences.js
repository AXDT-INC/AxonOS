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
    const trigger = document.getElementById('axonos-privacy-trigger');
    const listeners = new Set();
    let message = '';
    let busy = false;
    let expanded = false;
    let opened = false;
    let dismissed = false;
    let returnFocus = null;
    let unavailable = false;
    let lastRender = '';
    installStyles();
    host.classList.add('axp-host', embedded ? 'axp-embedded' : 'axp-app');
    // The app shell uses transforms, which would anchor a fixed card to the
    // footer instead of the viewport. Keep only its trigger in the footer.
    if (!embedded) document.body.append(host);
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
            height: host.childElementCount ? Math.ceil(host.getBoundingClientRect().height) : 0 }, config.landing_origin);
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
            opened = false;
            dismissed = true;
        } catch (_) {
            message = 'Your change could not be fully confirmed. Sharing stays off. Retry in Privacy settings.';
            unavailable = true;
        } finally {
            busy = false; notify(); render();
            if (!message) restoreFocus();
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

    function button(label, action, className = 'axp-button') {
        const b = document.createElement('button');
        b.type = 'button'; b.textContent = label; b.disabled = busy;
        b.className = className;
        b.addEventListener('click', action); return b;
    }

    function restoreFocus() {
        if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
        returnFocus = null;
    }

    function openPreferences() {
        if (busy) return;
        returnFocus = embedded ? null : document.activeElement;
        opened = true; expanded = false; render();
        host.querySelector('h2')?.focus({ preventScroll: true });
    }

    function closePreferences() {
        if (busy) return;
        opened = false; expanded = false; dismissed = true;
        // Dismissing is not a consent choice. Failed withdrawals remain visible.
        render(); restoreFocus();
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function render() {
        const visible = busy || opened || (!readChoice() && !dismissed) || !!message;
        const signature = JSON.stringify([readChoice(), permitted(), gpc(), busy, expanded, message, visible]);
        if (signature === lastRender) return;
        lastRender = signature;
        if (trigger) trigger.setAttribute('aria-expanded', String(visible));
        host.replaceChildren();
        if (!visible) { resize(); return; }
        const card = element('section', 'axp-card');
        card.setAttribute('role', 'dialog');
        card.setAttribute('aria-modal', 'false');
        card.setAttribute('aria-labelledby', 'axp-title');
        card.setAttribute('aria-describedby', 'axp-description');
        card.setAttribute('aria-busy', String(busy));
        const heading = element('div', 'axp-heading');
        const title = element('h2', 'axp-title', expanded ? 'Privacy preferences' : 'Your privacy, your choice');
        title.id = 'axp-title'; title.tabIndex = -1;
        const close = button('×', closePreferences, 'axp-close');
        close.setAttribute('aria-label', 'Close privacy preferences');
        heading.append(title, close); card.append(heading);
        const text = element('p', 'axp-description', gpc()
            ? 'Your browser privacy signal keeps optional X conversion sharing off.'
            : 'We use essential storage to run AxonOS. With your permission, X can measure which ads lead to activity in the app.');
        text.id = 'axp-description'; card.append(text);
        if (expanded) renderDetails(card);
        else {
            card.append(element('p', 'axp-caption', 'Optional sharing is your choice. You can use AxonOS either way.'));
            const actions = element('div', 'axp-actions');
            actions.append(button('Reject optional', () => choose('deny')));
            if (!gpc() && status.enabled) actions.append(button('Accept optional', () => choose('allow')));
            card.append(actions);
            const bottom = element('div', 'axp-bottom');
            bottom.append(element('span', 'axp-scope', 'One choice for the landing page and app.'),
                button('Preferences', () => { expanded = true; render(); host.querySelector('h2')?.focus({ preventScroll: true }); }, 'axp-link'));
            card.append(bottom);
        }
        if (message) {
            const error = element('p', 'axp-error', message);
            error.setAttribute('role', 'status'); card.append(error);
        }
        host.append(card); resize();
    }

    function renderDetails(card) {
        const categories = element('div', 'axp-categories');
        const essential = element('div', 'axp-category');
        const essentialCopy = element('div', 'axp-category-copy');
        essentialCopy.append(element('strong', '', 'Essential'), element('p', '', 'Keeps the app working and remembers your privacy choice.'));
        essential.append(essentialCopy, element('span', 'axp-badge', 'Always on'));
        const optional = element('label', 'axp-category');
        const optionalCopy = element('div', 'axp-category-copy');
        optionalCopy.append(element('strong', '', 'Advertising measurement'), element('p', '', 'Share eligible app activity with X to measure ads.'));
        const box = document.createElement('input'); box.type = 'checkbox';
        box.className = 'axp-switch'; box.setAttribute('role', 'switch');
        box.setAttribute('aria-label', 'Advertising measurement');
        box.checked = permitted(); box.disabled = gpc() || !status.enabled || busy;
        optional.append(optionalCopy, box); categories.append(essential, optional);
        card.append(categories);
        const details = element('details', 'axp-notice');
        details.append(element('summary', '', 'How your data is used'));
        details.append(element('p', '', 'If you allow sharing, X receives an ad-click ID, conversion type, time and deduplication ID for eligible wallet verifications, paid deposits and session starts. We do not send wallet addresses, payment amounts or desktop contents.'));
        details.append(element('p', '', 'Your choice is saved in an app-host cookie for up to ' + Math.floor(lifetime / 86400000) + ' days and applies to this browser on the landing page and app. Attribution uses encrypted browser-held contexts; withdrawal capabilities remain until their attribution periods expire.'));
        details.append(element('p', '', 'You can change your choice here at any time. Rejecting stops new sharing and cancels unsent events after server confirmation. Sent or in-flight events may not be recallable. Clearing browser storage removes its withdrawal capabilities. Global Privacy Control keeps sharing off.'));
        details.append(element('p', '', 'These preferences cover X attribution, not all AxonOS data processing.'));
        const link = element('a', '', 'X privacy policy'); link.href = 'https://x.com/en/privacy';
        link.target = '_blank'; link.rel = 'noopener noreferrer'; details.append(link);
        card.append(details);
        const actions = element('div', 'axp-actions');
        actions.append(button('Back', () => { expanded = false; render(); host.querySelector('h2')?.focus({ preventScroll: true }); }),
            button('Save preferences', () => choose(box.checked ? 'allow' : 'deny')));
        card.append(actions);
    }

    if (trigger) {
        trigger.hidden = false; trigger.classList.add('axp-trigger');
        trigger.setAttribute('aria-controls', host.id);
        trigger.addEventListener('click', openPreferences);
    }
    host.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); closePreferences(); }
    });
    if (embedded) window.addEventListener('message', event => {
        // A parent may only open the UI. It cannot supply a preference or grant.
        if (event.origin === config.landing_origin && event.source === window.parent &&
            event.data?.type === 'axonos-privacy-open-v1' && Object.keys(event.data).length === 1) {
            openPreferences();
        }
    });

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
        show: openPreferences, refresh });
}

function installStyles() {
    if (document.getElementById('axp-styles')) return;
    const style = document.createElement('style'); style.id = 'axp-styles';
    style.textContent = `
.axp-host { color:#e9edf5; font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; text-align:left; }
.axp-host * { box-sizing:border-box; }
.axp-app { display:contents; }
.axp-card { padding:24px; background:#101722; border:1px solid #2a3546; border-radius:16px; width:100%; }
.axp-app .axp-card { position:fixed; z-index:990; right:24px; bottom:24px; width:480px; max-width:calc(100vw - 32px); max-height:calc(100dvh - 48px); overflow:auto; box-shadow:0 16px 60px #0008; }
.axp-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:10px; }
.axp-host .axp-title { font:600 19px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#f4f7fb; margin:0; letter-spacing:-.3px; }
.axp-title:focus { outline:none; }
.axp-host p { margin:0; }
.axp-description { color:#bcc7d7; }
.axp-caption { color:#8f9eb2; font-size:12px; padding-top:8px; }
.axp-actions { display:flex; gap:10px; margin-top:20px; }
.axp-host button { font:inherit; cursor:pointer; transition:background .15s,border-color .15s; }
.axp-button { flex:1; min-height:44px; padding:10px 14px; border:1px solid #415369; border-radius:8px; background:#1b2839; color:#f0f5fb; font-weight:600!important; }
.axp-button:hover { background:#26394e; border-color:#66c7d3; }
.axp-host button:focus-visible,.axp-host input:focus-visible,.axp-host summary:focus-visible,.axp-host a:focus-visible,.axp-trigger:focus-visible { outline:2px solid #6dd5e1; outline-offset:3px; }
.axp-host button:disabled { opacity:.55; cursor:wait; }
.axp-close { flex:none; border:0; background:transparent; color:#9cabc0; padding:0; margin:-6px -6px 0 0; width:32px; height:32px; font-size:24px!important; line-height:1!important; border-radius:6px; }
.axp-close:hover { background:#243044; color:white; }
.axp-bottom { display:flex; gap:12px; justify-content:space-between; align-items:center; margin-top:14px; }
.axp-scope { color:#8f9eb2; font-size:11px; }
.axp-link { color:#a8dbe3; border:0; padding:6px 0; background:transparent; font-size:12px!important; text-decoration:underline; text-underline-offset:3px; }
.axp-trigger { font:inherit; font-size:12px; color:inherit; background:none; border:0; padding:4px 0; cursor:pointer; text-underline-offset:3px; }
.axp-trigger:hover { color:#b7e3ec; text-decoration:underline; }
.axp-categories { margin-top:18px; border:1px solid #2a3546; border-radius:10px; overflow:hidden; }
.axp-category { display:flex; align-items:center; gap:16px; justify-content:space-between; padding:15px; }
.axp-category + .axp-category { border-top:1px solid #2a3546; }
.axp-category-copy strong { font-weight:600; font-size:13px; color:#eef3fa; }
.axp-category-copy p { font-size:12px; color:#99a9bd; margin-top:3px; }
.axp-badge { font-size:10px; color:#9fbcbb; white-space:nowrap; }
.axp-switch { appearance:none; flex:none; width:36px; height:22px; margin:0; border:1px solid #65748a; border-radius:20px; background:#273448; position:relative; cursor:pointer; }
.axp-switch::after { content:""; position:absolute; width:14px; height:14px; top:3px; left:3px; border-radius:50%; background:#c7d1de; transition:transform .15s; }
.axp-switch:checked { background:#3aabbc; border-color:#3aabbc; }
.axp-switch:checked::after { transform:translateX(14px); background:#07151e; }
.axp-switch:disabled { opacity:.5; cursor:not-allowed; }
.axp-notice { color:#a9b7ca; font-size:12px; margin-top:16px; }
.axp-notice summary { cursor:pointer; padding:4px 0; color:#bbd0e3; }
.axp-notice p { margin:12px 0; }
.axp-notice a { color:#a8dbe3; text-underline-offset:3px; }
.axp-error { margin-top:14px!important; padding:12px; border:1px solid #725842; border-radius:8px; color:#f2cca9; font-size:12px; background:#30251d; }
@media(max-width:520px) { .axp-card { padding:20px; border-radius:12px; } .axp-app .axp-card { right:16px; bottom:16px; max-height:calc(100dvh - 32px); } .axp-host .axp-title { font-size:17px; } .axp-bottom { align-items:flex-start; } .axp-scope { max-width:190px; } }
@media(prefers-reduced-motion:reduce) { .axp-host *, .axp-host *::after { transition:none!important; } }
`;
    document.head.append(style);
}
