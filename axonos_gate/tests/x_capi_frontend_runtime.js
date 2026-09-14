'use strict';

// Minimal, dependency-free browser shim for privacy-state regressions. This is
// intentionally not a DOM implementation; it supplies only the primitives the
// first-party bridge uses so its real async code can run under Node.
const fs = require('fs');
const vm = require('vm');
const nodeCrypto = require('crypto');
const { TextEncoder } = require('util');

const bridgePath = process.argv[2];
if (!bridgePath) throw new Error('bridge path is required');
const bridgeSource = fs.readFileSync(bridgePath, 'utf8');
const pagePath = process.argv[3];
if (!pagePath) throw new Error('page path is required');
const pageSource = fs.readFileSync(pagePath, 'utf8');
const bootstrapMatch = pageSource.match(/<script>\s*([\s\S]*?)<\/script>/i);
if (!bootstrapMatch) throw new Error('synchronous URL bootstrap was not found');
const bootstrapSource = bootstrapMatch[1];

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

function response(body, ok = true) {
    return { ok, json: () => Promise.resolve(body) };
}

function delay(ms = 0) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function eventually(predicate, message) {
    for (let attempt = 0; attempt < 100; attempt += 1) {
        if (predicate()) return;
        await delay(2);
    }
    throw new Error(message);
}

function makeElement(tagName, elementsById) {
    const listeners = Object.create(null);
    const element = {
        tagName: String(tagName || '').toUpperCase(),
        children: [],
        parentNode: null,
        style: { cssText: '' },
        textContent: '',
        disabled: false,
        setAttribute() {},
        addEventListener(type, listener) {
            (listeners[type] ||= []).push(listener);
        },
        appendChild(child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        removeChild(child) {
            const index = element.children.indexOf(child);
            if (index >= 0) element.children.splice(index, 1);
            child.parentNode = null;
        },
        querySelector(selector) {
            return findDescendant(element, selector)[0] || null;
        },
        focus() {},
        click() {
            for (const listener of listeners.click || []) listener.call(element);
        },
    };
    let id = '';
    Object.defineProperty(element, 'id', {
        get: () => id,
        set(value) {
            if (id) elementsById.delete(id);
            id = String(value || '');
            if (id) elementsById.set(id, element);
        },
    });
    return element;
}

function allDescendants(root) {
    const result = [];
    for (const child of root.children || []) {
        result.push(child, ...allDescendants(child));
    }
    return result;
}

function findDescendant(root, selector) {
    const candidates = allDescendants(root);
    if (selector === 'button') {
        return candidates.filter((item) => item.tagName === 'BUTTON');
    }
    return [];
}

function createRuntime({ landingClick = '', stored = {}, fetchImpl }) {
    const values = new Map(Object.entries(stored));
    const windowListeners = Object.create(null);
    const documentListeners = Object.create(null);
    const elementsById = new Map();
    const body = makeElement('body', elementsById);
    const document = {
        body,
        visibilityState: 'visible',
        createElement: (tag) => makeElement(tag, elementsById),
        getElementById: (id) => elementsById.get(String(id)) || null,
        querySelectorAll(selector) {
            if (selector === '#axonos-x-consent button') {
                const dialog = elementsById.get('axonos-x-consent');
                return dialog ? findDescendant(dialog, 'button') : [];
            }
            return [];
        },
        addEventListener(type, listener) {
            (documentListeners[type] ||= []).push(listener);
        },
    };
    const runtime = {
        console,
        setTimeout,
        clearTimeout,
        AbortController,
        TextEncoder,
        document,
        sessionStorage: {
            getItem(key) { return values.has(String(key)) ? values.get(String(key)) : null; },
            setItem(key, value) { values.set(String(key), String(value)); },
            removeItem(key) { values.delete(String(key)); },
        },
        crypto: {
            getRandomValues(array) {
                for (let index = 0; index < array.length; index += 1) {
                    array[index] = index + 1;
                }
                return array;
            },
            subtle: {
                digest(algorithm, value) {
                    if (String(algorithm).toUpperCase() !== 'SHA-256') {
                        return Promise.reject(new Error('unsupported digest'));
                    }
                    const digest = nodeCrypto.createHash('sha256')
                        .update(Buffer.from(value)).digest();
                    return Promise.resolve(
                        digest.buffer.slice(
                            digest.byteOffset, digest.byteOffset + digest.byteLength
                        )
                    );
                },
            },
        },
        navigator: {
            get globalPrivacyControl() { return runtime.__gpc === true; },
            locks: {
                request(_name, _options, callback) {
                    return Promise.resolve().then(() => callback({}));
                },
            },
        },
        fetch: fetchImpl,
        axonosPendingTwclid: landingClick,
        __gpc: false,
        addEventListener(type, listener) {
            (windowListeners[type] ||= []).push(listener);
        },
    };
    runtime.window = runtime;
    vm.createContext(runtime);
    vm.runInContext(bridgeSource, runtime, { filename: bridgePath });
    return {
        runtime,
        values,
        dispatchDocument(type, event = {}) {
            for (const listener of documentListeners[type] || []) listener(event);
        },
        dispatchWindow(type, event = {}) {
            for (const listener of windowListeners[type] || []) listener(event);
        },
        button(label) {
            return allDescendants(body).find(
                (item) => item.tagName === 'BUTTON' && item.textContent === label
            );
        },
    };
}

const CONTEXT = 'A'.repeat(64);
const CSRF = 'B'.repeat(43);
const OWNER = 'a'.repeat(32);
const CLICK = 'landing-click-123456789';
const STORAGE_KEY = 'axonos_x_attribution_context_v1';
const CAPABILITY_KEY = 'axonos_x_attribution_revoke_capability_v1';
const PENDING_KEY = 'axonos_x_attribution_revoke_pending_v1';
const OWNER_KEY = 'axonos_x_attribution_tab_owner_v1';
const WALLET_COMMITMENT_KEY = 'axonos_x_attribution_wallet_commitment_v1';

function runUrlBootstrap(search, { failRewrite = false } = {}) {
    const replacements = [];
    const rewrites = [];
    const values = new Map();
    const runtime = {
        URLSearchParams,
        navigator: { globalPrivacyControl: false },
        location: {
            pathname: '/vnc.html',
            search,
            hash: '#viewer',
            replace(value) { replacements.push(String(value)); },
        },
        history: {
            state: null,
            replaceState(_state, _title, value) {
                if (failRewrite) throw new Error('history disabled');
                rewrites.push(String(value));
            },
        },
        sessionStorage: {
            getItem(key) { return values.has(String(key)) ? values.get(String(key)) : null; },
            setItem(key, value) { values.set(String(key), String(value)); },
            removeItem(key) { values.delete(String(key)); },
        },
    };
    runtime.window = runtime;
    vm.createContext(runtime);
    vm.runInContext(bootstrapSource, runtime, { filename: pagePath });
    return { runtime, replacements, rewrites, values };
}

function testUrlScrubFallbackStopsUnsafeReparse() {
    const failed = runUrlBootstrap(
        '?twclid=official-click_1234567890&invite=secret-invite',
        { failRewrite: true },
    );
    assert(failed.runtime.axonosPendingTwclid === '',
        'history failure retained raw click globally');
    assert(failed.runtime.axonosPendingGuestInvite === '',
        'history failure reparsed the unsafe original query');
    assert(failed.runtime.axonosUrlQueryScrubFailed === true,
        'history failure was not latched');
    assert(failed.replacements.length === 1 && failed.replacements[0] === '/vnc.html#viewer',
        'history failure did not replace with pathname plus hash');

    const confused = runUrlBootstrap('?twcl%2569d=confused-click&keep=yes');
    assert(confused.runtime.axonosPendingTwclid === '',
        'encoded key confusion was accepted as a click');
    assert(confused.rewrites[0] === '/vnc.html?keep=yes#viewer',
        'encoded attribution alias was not canonically removed');

    let deepKey = 'TwCl%69d';
    for (let depth = 0; depth < 8; depth += 1) {
        deepKey = encodeURIComponent(deepKey);
    }
    const deeplyConfused = runUrlBootstrap(`?${deepKey}=deep-click&keep=yes`);
    assert(deeplyConfused.runtime.axonosPendingTwclid === '',
        'deep mixed-case encoded key was accepted as a click');
    assert(deeplyConfused.rewrites[0] === '/vnc.html?keep=yes#viewer',
        'deep mixed-case attribution alias remained in the URL');

    const malformed = runUrlBootstrap('?bad%zz_twclid=secret-click&keep=yes');
    assert(malformed.runtime.axonosPendingTwclid === '',
        'malformed sensitive key was accepted as a click');
    assert(malformed.runtime.axonosUrlQueryScrubFailed === true,
        'malformed sensitive key did not trigger conservative query removal');
    assert(malformed.replacements[0] === '/vnc.html#viewer',
        'malformed sensitive key survived fallback query removal');

    const mixedMalformed = runUrlBootstrap('?%74wclid%ZZ=secret-click&keep=yes');
    assert(mixedMalformed.runtime.axonosPendingTwclid === '',
        'valid escape beside malformed escape was accepted as a click');
    assert(mixedMalformed.runtime.axonosUrlQueryScrubFailed === true,
        'mixed malformed sensitive key did not trigger query removal');
    assert(mixedMalformed.replacements[0] === '/vnc.html#viewer',
        'mixed malformed sensitive key survived fallback query removal');

    const oversized = runUrlBootstrap(`?${'%25'.repeat(200)}=secret-click&keep=yes`);
    assert(oversized.runtime.axonosPendingTwclid === '',
        'oversized query name was accepted as a click');
    assert(oversized.runtime.axonosUrlQueryScrubFailed === true,
        'oversized query name did not trigger bounded conservative removal');
    assert(oversized.replacements[0] === '/vnc.html#viewer',
        'oversized query name survived fallback query removal');

    for (const identifierShaped of [
        `0x${'1'.repeat(40)}`,
        '1'.repeat(40),
        `0x${'a'.repeat(64)}`,
        'a'.repeat(64),
        '550e8400-e29b-41d4-a716-446655440000',
        '192.0.2.44',
        'alice.eth',
        'alice.btc',
        'sk-this-is-accidentally-a-secret',
    ]) {
        const rejected = runUrlBootstrap(
            `?twclid=${encodeURIComponent(identifierShaped)}&keep=yes`,
        );
        assert(rejected.runtime.axonosPendingTwclid === '',
            `identifier-shaped click entered the attribution bridge: ${identifierShaped}`);
        assert(rejected.rewrites[0] === '/vnc.html?keep=yes#viewer',
            `identifier-shaped click was not scrubbed: ${identifierShaped}`);
    }

    for (const boundaryConfused of [
        ' official-click_1234567890',
        'official-click_1234567890 ',
        '\tofficial-click_1234567890',
        'official-click_1234567890\n',
    ]) {
        const rejected = runUrlBootstrap(
            `?twclid=${encodeURIComponent(boundaryConfused)}&keep=yes`,
        );
        assert(rejected.runtime.axonosPendingTwclid === '',
            'boundary whitespace/control was normalized into a click');
        assert(rejected.rewrites[0] === '/vnc.html?keep=yes#viewer',
            'boundary-confused click was not scrubbed');
    }

    const accepted = runUrlBootstrap('?twclid=official-click_1234567890&keep=yes');
    assert(accepted.runtime.axonosPendingTwclid === 'official-click_1234567890',
        'one exact valid landing click was not captured');
    assert(accepted.rewrites[0] === '/vnc.html?keep=yes#viewer',
        'exact landing click was not scrubbed from the URL');
}

async function testUnsetIsEphemeralAndGrantUsesHeader() {
    const calls = [];
    const env = createRuntime({
        landingClick: CLICK,
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return Promise.resolve(response({
                    enabled: true,
                    state: 'unset',
                    context: CONTEXT,
                    csrf: CSRF,
                    landing_click_accepted: true,
                    revocation_available: false,
                    new_lifecycle_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            }
            return Promise.resolve(response({
                ok: true,
                enabled: true,
                state: 'granted',
                context: CONTEXT,
                csrf: CSRF,
                revocation_available: true,
                attribution_ttl_days: 7,
                expires_at: 1999999999,
            }));
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => !!env.button('Allow sharing'), 'allow choice did not render');
    assert(env.values.get(STORAGE_KEY) === undefined, 'unset context was persisted');
    assert(env.values.get(CAPABILITY_KEY) === undefined, 'unset revoke capability was persisted');
    assert(env.runtime.axonosPendingTwclid === undefined, 'raw click global survived bridge load');
    assert([...env.values.values()].every((value) => !String(value).includes(CLICK)),
        'raw click entered sessionStorage');
    assert(calls[0].options.headers['X-AxonOS-Landing-Click'] === CLICK,
        'status did not receive the immutable landing click');

    // A BFCache resume retains module memory. Its status revalidation must
    // repeat the same transient header so the server can verify the commitment.
    env.dispatchWindow('pagehide');
    env.dispatchWindow('pageshow', { persisted: true });
    await eventually(() => calls.length === 2, 'unset page-cache resume did not revalidate');
    assert(calls[1].options.headers['X-AxonOS-Landing-Click'] === CLICK,
        'live unset revalidation did not repeat the committed click');
    assert(env.values.get(STORAGE_KEY) === undefined,
        'page-cache resume persisted an unset context');

    // A real reload gets only sessionStorage. Since neither the raw click nor
    // unset ticket survived, the new bridge must stay idle and make no request.
    const reloadCalls = [];
    const reloaded = createRuntime({
        stored: Object.fromEntries(env.values),
        fetchImpl(...args) {
            reloadCalls.push(args);
            return Promise.reject(new Error('idle reload unexpectedly fetched'));
        },
    });
    reloaded.dispatchDocument('DOMContentLoaded');
    await eventually(() => reloaded.runtime.axonosAttributionReady === true,
        'idle reload did not finish');
    assert(reloadCalls.length === 0, 'reload before choice retained attribution');
    assert(reloaded.runtime.axonosAttributionContext === '',
        'reload before choice exposed a context');
    assert([...reloaded.values.values()].every((value) => !String(value).includes(CLICK)),
        'reload storage retained the raw click');

    env.button('Allow sharing').click();
    await eventually(() => calls.length === 3, 'grant request was not sent');
    const grant = calls[2];
    assert(grant.options.headers['X-AxonOS-Landing-Click'] === CLICK,
        'grant did not repeat the module-private landing click');
    assert(grant.options.body === '{"action":"grant"}',
        'grant body contained more than its action');
    await eventually(() => env.runtime.axonosAttributionContext === CONTEXT,
        'granted context was not published');
    assert(env.values.get(STORAGE_KEY) === CONTEXT, 'granted context was not tab-persisted');
}

async function testLateGpcSuppressesAndRevokes() {
    const calls = [];
    let finishRevoke;
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return Promise.resolve(response({
                    enabled: true,
                    state: 'granted',
                    context: CONTEXT,
                    csrf: CSRF,
                    revocation_available: true,
                    new_lifecycle_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            }
            return new Promise((resolve) => {
                finishRevoke = () => resolve(response({
                    ok: true,
                    enabled: false,
                    state: 'revoked',
                    context: CONTEXT,
                    csrf: CSRF,
                    gpc_applied: true,
                    revocation_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            });
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => env.runtime.axonosAttributionContext === CONTEXT,
        'granted context did not restore');

    env.runtime.__gpc = true;
    env.dispatchDocument('visibilitychange');
    assert(env.runtime.axonosAttributionContext === '',
        'late GPC did not synchronously suppress public context');
    assert(env.values.get(PENDING_KEY) === '1',
        'late GPC did not durably record pending withdrawal');
    await eventually(() => typeof finishRevoke === 'function',
        'late GPC did not schedule revocation');
    assert(calls[1].options.body === '{"action":"revoke"}',
        'late GPC sent the wrong transition');
    finishRevoke();
    await eventually(() => env.values.get(PENDING_KEY) === undefined,
        'confirmed GPC revocation did not clear pending state');
    assert(env.values.get(CAPABILITY_KEY) === undefined,
        'confirmed revocation retained its private capability');
    assert(env.runtime.axonosAttributionContext === '',
        'confirmed GPC revocation republished context');
}

async function testLateGpcUsesCompleteCapabilityDuringStatusRace() {
    const calls = [];
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return new Promise((_resolve, reject) => {
                    options.signal.addEventListener('abort', () => {
                        const error = new Error('aborted');
                        error.name = 'AbortError';
                        reject(error);
                    }, { once: true });
                });
            }
            return Promise.resolve(response({
                ok: true,
                enabled: false,
                state: 'revoked',
                context: CONTEXT,
                csrf: CSRF,
                gpc_applied: true,
                revocation_available: false,
                attribution_ttl_days: 7,
                expires_at: 1999999999,
            }));
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => calls.length === 1, 'startup status did not begin');
    env.runtime.__gpc = true;
    env.dispatchDocument('visibilitychange');
    await eventually(() => calls.length === 2, 'status-race GPC did not revoke');
    assert(calls[1].options.headers['X-AxonOS-Attribution'] === CONTEXT,
        'status-race revoke lost its private context');
    assert(calls[1].options.headers['X-AxonOS-CSRF'] === CSRF,
        'status-race revoke mixed or lost the revoke-only CSRF');
    assert(calls[1].options.body === '{"action":"revoke"}',
        'status-race GPC did not issue revoke');
    await eventually(() => env.values.get(PENDING_KEY) === undefined,
        'status-race confirmed revoke remained pending');
}

async function testStatusCannotPublishGpcThatChangedInFlight() {
    const calls = [];
    let finishStatus;
    let finishRevoke;
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return new Promise((resolve) => {
                    finishStatus = () => resolve(response({
                        enabled: true,
                        state: 'granted',
                        context: CONTEXT,
                        csrf: CSRF,
                        revocation_available: true,
                        new_lifecycle_available: false,
                        attribution_ttl_days: 7,
                        expires_at: 1999999999,
                    }));
                });
            }
            return new Promise((resolve) => {
                finishRevoke = () => resolve(response({
                    ok: true,
                    enabled: false,
                    state: 'revoked',
                    context: CONTEXT,
                    csrf: CSRF,
                    gpc_applied: true,
                    revocation_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            });
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => typeof finishStatus === 'function', 'deferred status did not start');
    env.runtime.__gpc = true;
    finishStatus();
    await eventually(() => typeof finishRevoke === 'function',
        'in-flight GPC change did not schedule revoke');
    assert(env.runtime.axonosAttributionContext === '',
        'status published a grant after GPC changed in flight');
    assert(env.values.get(PENDING_KEY) === '1',
        'in-flight GPC change did not persist withdrawal');
    finishRevoke();
    await eventually(() => env.values.get(PENDING_KEY) === undefined,
        'in-flight GPC revocation did not finish');
}

async function testLateGpcIsLocalForIdleVisitor() {
    const calls = [];
    const env = createRuntime({
        fetchImpl(...args) {
            calls.push(args);
            return Promise.reject(new Error('idle visitor unexpectedly fetched'));
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => env.runtime.axonosAttributionReady === true,
        'idle bridge did not finish');
    env.runtime.__gpc = true;
    env.dispatchWindow('pageshow', { persisted: false });
    assert(calls.length === 0, 'late GPC created state for an idle visitor');
    assert(env.values.get(PENDING_KEY) === undefined,
        'idle GPC wrote a remote-withdrawal marker');
    assert(env.runtime.axonosAttributionContext === '',
        'idle GPC exposed attribution context');
}

async function testWalletLatchRejectsCrossWalletAfterDroppedBind() {
    const calls = [];
    const walletA = `0x${'1'.repeat(40)}`;
    const walletB = `0x${'2'.repeat(40)}`;
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return Promise.resolve(response({
                    enabled: true,
                    state: 'granted',
                    context: CONTEXT,
                    csrf: CSRF,
                    revocation_available: true,
                    new_lifecycle_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            }
            if (String(url).endsWith('/bind')) {
                return Promise.resolve(response({ ok: true, accepted: false }));
            }
            return Promise.resolve(response({
                ok: true,
                enabled: false,
                state: 'revoked',
                context: CONTEXT,
                csrf: CSRF,
                revocation_available: false,
                attribution_ttl_days: 7,
                expires_at: 1999999999,
            }));
        },
    });
    env.runtime.verifiedWalletAddress = walletA;
    env.runtime.verifiedWalletAuthToken = 'T'.repeat(64);
    env.dispatchDocument('DOMContentLoaded');
    await eventually(
        () => calls.some((call) => call.url.endsWith('/bind')) &&
            /^[a-f0-9]{64}$/.test(env.values.get(WALLET_COMMITMENT_KEY) || ''),
        'first wallet was not latched before bind',
    );
    assert(env.runtime.axonosAttributionContext === CONTEXT,
        'first wallet context was not public before the switch regression');

    // runVerify invokes this hook before constructing wallet B's authenticated
    // verify POST.  The browser globals still identify wallet A at this point.
    const verifyAttribution =
        env.runtime.axonosAttributionContextForWalletCandidate(walletB);
    const verifyHeaders = { 'X-Wallet-Address': walletB };
    if (verifyAttribution) {
        verifyHeaders['X-AxonOS-Attribution'] = verifyAttribution;
    }
    assert(!Object.hasOwn(verifyHeaders, 'X-AxonOS-Attribution'),
        'wallet B verify request inherited wallet A attribution');
    assert(env.runtime.axonosAttributionContext === '',
        'wallet B candidate did not synchronously withhold wallet A context');

    env.runtime.verifiedWalletAddress = walletB;
    env.runtime.axonosNotifyWalletVerified(walletB);
    await eventually(
        () => calls.some((call) => call.url.endsWith('/consent')),
        'cross-wallet switch did not schedule revocation',
    );
    const bindBodies = calls.filter((call) => call.url.endsWith('/bind'))
        .map((call) => JSON.parse(call.options.body).wallet_address);
    assert(bindBodies.length === 1 && bindBodies[0] === walletA,
        'a dropped first bind let a second wallet claim the context');
    await eventually(() => env.values.get(PENDING_KEY) === undefined,
        'cross-wallet revocation did not finish');
    assert(env.runtime.axonosAttributionContext === '',
        'cross-wallet rejection left the context public');
    assert(env.values.get(WALLET_COMMITMENT_KEY) === undefined,
        'confirmed cross-wallet revocation retained wallet commitment');
}

async function testWalletCandidatePreservesFirstWalletLatchRace() {
    const calls = [];
    const wallet = `0x${'5'.repeat(40)}`;
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return Promise.resolve(response({
                    enabled: true,
                    state: 'granted',
                    context: CONTEXT,
                    csrf: CSRF,
                    revocation_available: true,
                    new_lifecycle_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            }
            return Promise.resolve(response({ ok: true, accepted: true }));
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => env.runtime.axonosAttributionContext === CONTEXT,
        'unbound granted context did not become ready');

    const initial = env.runtime.axonosAttributionContextForWalletCandidate(wallet);
    assert(initial === '' && env.runtime.axonosAttributionContext === '',
        'first candidate was exposed before its commitment completed');
    await eventually(
        () => env.runtime.axonosAttributionContextForWalletCandidate(wallet) === CONTEXT,
        'first wallet did not acquire its candidate-scoped context',
    );
    assert(!calls.some((call) => call.url.endsWith('/consent')),
        'first wallet candidate was incorrectly treated as a cross-wallet switch');
}

async function testRejectedBindRetriesOnlyOnLaterLifecycleSignal() {
    const calls = [];
    const wallet = `0x${'3'.repeat(40)}`;
    let bindAttempts = 0;
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return Promise.resolve(response({
                    enabled: true,
                    state: 'granted',
                    context: CONTEXT,
                    csrf: CSRF,
                    revocation_available: true,
                    new_lifecycle_available: false,
                    attribution_ttl_days: 7,
                    expires_at: 1999999999,
                }));
            }
            bindAttempts += 1;
            return Promise.resolve(response({ ok: true, accepted: bindAttempts > 1 }));
        },
    });
    env.runtime.verifiedWalletAddress = wallet;
    env.runtime.verifiedWalletAuthToken = 'U'.repeat(64);
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => bindAttempts === 1, 'initial bind was not attempted');
    await delay(5);
    assert(bindAttempts === 1, 'rejected bind spun without a lifecycle signal');
    env.dispatchWindow('online');
    await eventually(() => bindAttempts === 2, 'online recovery did not retry bind');
    assert(calls.filter((call) => call.url.endsWith('/bind')).every(
        (call) => JSON.parse(call.options.body).wallet_address === wallet
    ), 'bind retry changed wallets');
}

async function testWalletNotificationBeforeStatusStillBinds() {
    const calls = [];
    const wallet = `0x${'4'.repeat(40)}`;
    let finishStatus;
    const capability = JSON.stringify({ context: CONTEXT, csrf: CSRF });
    const env = createRuntime({
        stored: {
            [STORAGE_KEY]: CONTEXT,
            [CAPABILITY_KEY]: capability,
            [OWNER_KEY]: OWNER,
        },
        fetchImpl(url, options) {
            calls.push({ url: String(url), options });
            if (String(url).endsWith('/status')) {
                return new Promise((resolve) => {
                    finishStatus = () => resolve(response({
                        enabled: true,
                        state: 'granted',
                        context: CONTEXT,
                        csrf: CSRF,
                        revocation_available: true,
                        new_lifecycle_available: false,
                        attribution_ttl_days: 7,
                        expires_at: 1999999999,
                    }));
                });
            }
            return Promise.resolve(response({ ok: true, accepted: true }));
        },
    });
    env.dispatchDocument('DOMContentLoaded');
    await eventually(() => typeof finishStatus === 'function', 'status did not start');
    env.runtime.verifiedWalletAddress = wallet;
    env.runtime.verifiedWalletAuthToken = 'V'.repeat(64);
    env.runtime.axonosNotifyWalletVerified(wallet);
    assert(!calls.some((call) => call.url.endsWith('/bind')),
        'wallet notification bound before consent status was known');
    finishStatus();
    await eventually(() => calls.some((call) => call.url.endsWith('/bind')),
        'wallet verified before status was never bound afterward');
    assert(env.runtime.axonosAttributionContext === CONTEXT,
        'successful delayed latch did not expose the matching context');
}

function createSessionClaimRuntime({ stored = {}, fetchImpl, byteSeed = 0 }) {
    const marker = 'var axonosSessionClaimPostInFlight = null;';
    const endMarker = 'window.axonosClaimSession = claimSession;';
    const start = pageSource.indexOf(marker);
    const end = pageSource.indexOf(endMarker, start);
    if (start < 0 || end < 0) {
        throw new Error('session claim idempotency implementation was not found');
    }
    const source = pageSource.slice(start, end + endMarker.length);
    const values = new Map(Object.entries(stored));
    const requests = [];
    let randomCalls = 0;
    const runtime = {
        console,
        URL,
        location: { origin: 'https://app.example' },
        document: { getElementById() { return null; } },
        sessionStorage: {
            getItem(key) {
                return values.has(String(key)) ? values.get(String(key)) : null;
            },
            setItem(key, value) { values.set(String(key), String(value)); },
            removeItem(key) { values.delete(String(key)); },
        },
        crypto: {
            getRandomValues(array) {
                const offset = byteSeed + (randomCalls * 67);
                randomCalls += 1;
                for (let index = 0; index < array.length; index += 1) {
                    array[index] = (offset + index) & 0xff;
                }
                return array;
            },
        },
        axonosFetch(options) {
            requests.push(options);
            return fetchImpl(options);
        },
        axonosSessionClaimTimeoutMs() { return 150000; },
        axonosCurrentSessionId() { return null; },
        getRequestedProfile() { return 'small'; },
        axonosForgetStaleSessionBinding() {},
        axonosSeedSessionStartTime() {},
    };
    runtime.window = runtime;
    vm.createContext(runtime);
    vm.runInContext(source, runtime, { filename: `${pagePath}:claim-idempotency` });
    return {
        runtime,
        values,
        requests,
        randomCalls: () => randomCalls,
    };
}

function claimPayload(request) {
    return JSON.parse(String(request.body || '{}'));
}

async function rejected(promise) {
    try {
        await promise;
    } catch (_error) {
        return;
    }
    throw new Error('expected the simulated ambiguous transport failure');
}

async function testLaunchRequestIdSurvivesPreflightRetryReloadAndAmbiguity() {
    const walletA = `0x${'a'.repeat(40)}`;
    const walletB = `0x${'b'.repeat(40)}`;
    let preflights = 0;
    const first = createSessionClaimRuntime({
        fetchImpl: () => Promise.reject(new Error('connection reset')),
        byteSeed: 0,
    });
    first.runtime.verifiedWalletAddress = walletA.toUpperCase();
    first.runtime.axonosEnsureWalletSessionCurrent = () => {
        preflights += 1;
        return Promise.resolve(true);
    };

    await rejected(first.runtime.axonosClaimSession({ newSession: true }));
    assert(preflights === 1, 'wallet preflight was not followed recursively');
    assert(first.requests.length === 1, 'recursive preflight posted more than once');
    const firstId = claimPayload(first.requests[0]).launch_request_id;
    assert(/^[a-f0-9]{64}$/.test(firstId),
        'launch_request_id was not 32 random bytes encoded as lowercase hex');
    assert(first.randomCalls() === 1,
        'one explicit launch generated more than one source of randomness');
    const walletAKey = `axonos_new_session_request:${walletA}`;
    assert(first.values.get(walletAKey) === firstId,
        'ambiguous transport failure cleared the pending launch id');

    await rejected(first.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    }));
    assert(claimPayload(first.requests[1]).launch_request_id === firstId,
        'same-tab ambiguous retry minted a second launch id');
    assert(first.randomCalls() === 1,
        'same-tab retry consumed fresh randomness instead of stored intent');

    const reloaded = createSessionClaimRuntime({
        stored: Object.fromEntries(first.values),
        fetchImpl: () => Promise.reject(new Error('timeout')),
        byteSeed: 128,
    });
    reloaded.runtime.verifiedWalletAddress = walletA;
    await rejected(reloaded.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    }));
    assert(claimPayload(reloaded.requests[0]).launch_request_id === firstId,
        'reload did not reuse the durable pending launch id');
    assert(reloaded.randomCalls() === 0,
        'reload generated randomness despite a valid stored launch id');

    reloaded.runtime.verifiedWalletAddress = walletB;
    await rejected(reloaded.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    }));
    const walletBId = claimPayload(reloaded.requests[1]).launch_request_id;
    assert(walletBId !== firstId, 'a second wallet inherited the first wallet intent');
    assert(reloaded.values.get(walletAKey) === firstId,
        'launching for another wallet overwrote the first wallet intent');
    assert(reloaded.values.get(`axonos_new_session_request:${walletB}`) === walletBId,
        'second wallet did not receive its own storage slot');
}

async function testConcurrentLaunchCallersShareOneInFlightPostAndIntent() {
    const wallet = `0x${'d'.repeat(40)}`;
    let finishRequest;
    const env = createSessionClaimRuntime({
        fetchImpl: () => new Promise((resolve) => { finishRequest = resolve; }),
        byteSeed: 64,
    });
    env.runtime.verifiedWalletAddress = wallet;

    const first = env.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    });
    const second = env.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    });
    assert(env.requests.length === 1,
        'equivalent concurrent launch callers posted twice');
    assert(env.randomCalls() === 1,
        'equivalent concurrent launch callers minted two intents');
    const launchId = claimPayload(env.requests[0]).launch_request_id;
    finishRequest({
        granted: false,
        retryable: true,
        launch_request_consumed: true,
        launch_request_id: launchId,
        allocation_status: 'allocating',
    });
    const replies = await Promise.all([first, second]);
    assert(replies.every((reply) => reply.launch_request_id === launchId),
        'in-flight callers did not share the exact server response');
    assert(env.values.get(`axonos_new_session_request:${wallet}`) === launchId,
        'retryable in-flight result did not retain its shared intent');
}

async function testLaunchRequestIdClearsOnlyOnDefinitiveMatchingEvidence() {
    const wallet = `0x${'c'.repeat(40)}`;
    let reply = null;
    const env = createSessionClaimRuntime({
        fetchImpl: () => Promise.resolve(reply),
        byteSeed: 32,
    });
    env.runtime.verifiedWalletAddress = wallet;

    // A parsed but explicitly retryable response describes the original
    // in-progress allocation. It must keep the same key for the next poll.
    reply = {
        granted: false,
        retryable: true,
        launch_request_consumed: true,
        launch_request_id: 'placeholder',
        allocation_status: 'allocating',
    };
    const pendingPromise = env.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    });
    const pendingId = claimPayload(env.requests[0]).launch_request_id;
    reply.launch_request_id = pendingId;
    await pendingPromise;
    const storageKey = `axonos_new_session_request:${wallet}`;
    assert(env.values.get(storageKey) === pendingId,
        'retryable allocation response cleared the replay key');

    // A definitive server response bearing the exact id clears it; an
    // unrelated late response cannot clear a newer pending intent.
    reply = {
        granted: false,
        retryable: false,
        launch_request_consumed: false,
        launch_request_id: 'unrelated',
    };
    await env.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    });
    const secondId = claimPayload(env.requests[1]).launch_request_id;
    assert(env.values.get(storageKey) === secondId,
        'mismatched response cleared a newer pending intent');

    reply = {
        granted: false,
        retryable: false,
        launch_request_consumed: true,
        launch_request_terminal: true,
        launch_request_id: secondId,
    };
    await env.runtime.axonosClaimSession({
        newSession: true,
        walletPreflightDone: true,
    });
    assert(!env.values.has(storageKey),
        'definitive terminal replay did not clear its matching intent');
}

(async () => {
    testUrlScrubFallbackStopsUnsafeReparse();
    await testUnsetIsEphemeralAndGrantUsesHeader();
    await testLateGpcSuppressesAndRevokes();
    await testLateGpcUsesCompleteCapabilityDuringStatusRace();
    await testStatusCannotPublishGpcThatChangedInFlight();
    await testLateGpcIsLocalForIdleVisitor();
    await testWalletLatchRejectsCrossWalletAfterDroppedBind();
    await testWalletCandidatePreservesFirstWalletLatchRace();
    await testRejectedBindRetriesOnlyOnLaterLifecycleSignal();
    await testWalletNotificationBeforeStatusStillBinds();
    await testLaunchRequestIdSurvivesPreflightRetryReloadAndAmbiguity();
    await testConcurrentLaunchCallersShareOneInFlightPostAndIntent();
    await testLaunchRequestIdClearsOnlyOnDefinitiveMatchingEvidence();
    process.stdout.write('x-capi frontend runtime checks passed\n');
})().catch((error) => {
    process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
    process.exitCode = 1;
});
