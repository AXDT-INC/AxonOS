/* Exercise production wallet routing and startup races without real credentials,
 * wallet extensions, network access, or compute allocations. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const WALLET = '0x' + '1'.repeat(40);
const OTHER_WALLET = '0x' + '2'.repeat(40);
const noop = () => {};
const flush = () => new Promise((resolve) => setImmediate(resolve));

function between(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, start);
    return source.slice(from, to);
}

function exportedFunction(name) {
    return between('function ' + name + '(', 'window.' + name + ' = ' + name + ';');
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function element(...initialClasses) {
    const classes = new Set(initialClasses);
    return {
        classList: {
            add: (...values) => values.forEach((value) => classes.add(value)),
            remove: (...values) => values.forEach((value) => classes.delete(value)),
            contains: (value) => classes.has(value),
        },
        attributes: {}, dataset: {}, style: {}, textContent: '', disabled: false,
        children: [], innerHTML: '',
        setAttribute(name, value) { this.attributes[name] = value; },
    };
}

function browser(screen = 'landing') {
    const elements = new Map([
        ['noVNC_connect_dlg', element('axonos-state-' + screen)],
        ['noVNC_credentials_dlg', element('noVNC_open')],
        ['axonos_catalog_modal', element('active')],
        ['axonos_template_modal', element('active')],
        ['axonos_dashboard_container', element()],
        ['axonos_wizard_container', element()],
        ['axonos_hero_browse_btn', element()],
        ['axonos_dashboard_credits', element()],
        ['axonos_dashboard_sessions_list', element()],
        ['axonos_dashboard_empty_state', element()],
        ['axonos_wallet_state_insufficient', element('axonos-wallet-state--hidden')],
        ['axonos_wallet_state_connect_error', element('axonos-wallet-state--hidden')],
    ]);
    const state = {
        refreshes: 0, status: 'checking', verifies: [], providerRequests: [],
        observers: [], fetches: [], refresh: deferred(), fetch: deferred(),
        account: deferred(), guest: false, now: 0, nextTimer: 0, timers: new Map(),
        warningUpdates: [], statusReads: [], authRequests: [],
    };
    const window = {
        location: { origin: 'https://app.example.test' },
        verifiedWalletAddress: null, verifiedWalletAuthToken: null,
        axonosGuestEntryPendingOrActive: () => state.guest,
        axonosHideConnectionLoader: noop,
        setTimeout: (callback, delay) => {
            const id = ++state.nextTimer;
            state.timers.set(id, { callback, deadline: state.now + delay });
            return id;
        },
        clearTimeout: (id) => { state.timers.delete(id); },
    };
    const root = element();
    const context = vm.createContext({
        window, document: {
            getElementById: (id) => elements.get(id) || null,
            querySelector: () => null,
            documentElement: root,
        },
        console: { log: noop, warn: noop, error: noop }, Promise, URL, AbortController,
        localStorage: { getItem: () => WALLET, setItem: noop },
        verifiedWalletAddress: null,
        axonosWalletConnectGeneration: 0, axonosWalletVerifyGeneration: 0,
        axonosDashboardRequestGeneration: 0,
        axonosSetGuestInviteEligibility: noop, axonosSetTestCreditEligibility: noop,
        axonosPaintSessionCardRemaining: noop, axonosTryResumeDesktopAfterCredit: noop,
        axonosInvalidateWalletVerification: () => { context.axonosWalletVerifyGeneration += 1; },
        setWalletUIState: (value) => { state.status = value; },
        bindWalletAccountEvents: noop, axonosFinishWalletSwitchInProgress: noop,
        selectedEthereumProvider: null,
        requestConnectedWallet: () => state.account.promise,
        resolveEthereumProvider: () => ({ request: noop }),
        axonosWalletProviderRequest: (provider, request) => {
            state.providerRequests.push(request.method);
            return Promise.resolve([WALLET]);
        },
        runVerify: (account) => { state.verifies.push(account); return Promise.resolve(); },
        UI: { connected: false, showStatus: noop },
        MutationObserver: class {
            constructor(callback) { state.observers.push(callback); }
            observe() {}
        },
        fetch: (url, options) => {
            state.fetches.push({ url, options });
            return state.fetch.promise;
        },
    });
    context.axonosLoadDashboard = () => {
        state.refreshes += 1;
        context.axonosDashboardRequestGeneration += 1;
        return state.refresh.promise;
    };
    vm.runInContext(between('function axonosUpdateActiveScreen(',
        'function axonosEscapeHtml('), context);
    vm.runInContext(exportedFunction('axonosSyncHeroCta'), context);
    context.axonosSyncTopbar = (wallet) => context.axonosSyncHeroCta(wallet);
    vm.runInContext(exportedFunction('axonosOnWalletVerified'), context);
    vm.runInContext(exportedFunction('onConnectWalletClick'), context);
    vm.runInContext(between('function axonosWalletTimeoutError(',
        '// Prompt-opening requests must never stack wallet-side:'), context);
    function authenticate(wallet = WALLET) {
        context.verifiedWalletAddress = wallet;
        window.verifiedWalletAddress = wallet;
        window.verifiedWalletAuthToken = 'local-test-token';
    }
    function restore() {
        elements.get('axonos_hero_browse_btn').disabled = true;
        elements.get('axonos_hero_browse_btn').textContent = 'Restoring session…';
        const start = source.indexOf('(function () {', source.indexOf('// Silent reload restore:'));
        const end = source.indexOf('})();', start) + '})();'.length;
        assert.ok(start >= 0 && end > start);
        vm.runInContext(source.slice(start, end), context);
    }
    function observeCredentials() {
        const marker = source.indexOf('var obs = new MutationObserver(function () {');
        const start = source.lastIndexOf('(function () {', marker);
        const end = source.indexOf('})();', marker) + '})();'.length;
        assert.ok(start >= 0 && end > start);
        vm.runInContext(source.slice(start, end), context);
        assert.equal(state.observers.length, 1);
        return state.observers[0];
    }
    function response(ok, data = {}) {
        state.fetch.resolve({ ok, status: ok ? 200 : 401, json: () => Promise.resolve(data) });
    }
    function currentScreen(expected) {
        assert.ok(elements.get('noVNC_connect_dlg').classList.contains('axonos-state-' + expected), expected);
    }
    function advanceTime(milliseconds) {
        state.now += milliseconds;
        for (const [id, timer] of state.timers) {
            if (timer.deadline > state.now) continue;
            state.timers.delete(id);
            timer.callback();
        }
    }
    function useRealDashboard() {
        context.UI.updateScheduledStopWarnings = (sessions, replace) => {
            state.warningUpdates.push({ sessions: Array.from(sessions), replace });
        };
        window.UI = context.UI;
        context.sessionStatusForWallet = (wallet) => {
            state.statusReads.push(wallet);
            return state.refresh.promise;
        };
        context.axonosSessionStatusIsAuthoritative = () => true;
        context.axonosSyncDashboardPausedResume = noop;
        context.axonosOwnedDashboardSessions = (status) => status.sessions || [];
        context.axonosWithoutEndingDashboardSessions = (sessions) => sessions;
        context.axonosFetchWalletAccessStatus = () => Promise.resolve({ ok: true, data: { remaining_minutes: 71 } });
        vm.runInContext(exportedFunction('axonosLoadDashboard'), context);
    }
    function useRealVerify() {
        context.axonosAttributionContextForWalletRequest = noop;
        context.axonosPaymentOperationGeneration = 0;
        context.axonosTestCreditBusy = false;
        context.setPayDepositButtonsBusy = noop;
        context.axonosClearWalletProviderOutOfSync = noop;
        context.axonosRememberWalletProvider = noop;
        context.axonosWalletVerifyFetch = (url) => {
            const path = new URL(url).pathname;
            state.authRequests.push(path);
            return Promise.resolve({ status: 200, ok: true, data:
                path === '/api/auth/challenge'
                    ? { challenge: 'local-test-challenge' }
                    : { verified: true, auth_token: 'signed-test-token', remaining_minutes: 71 },
            });
        };
        vm.runInContext(between('function axonosRequireWalletVerifyAttempt(',
            'function axonosWalletVerifyFetch('), context);
        vm.runInContext(between('function axonosResetIdentityAfterFailedWalletAttempt(',
            'function axonosActivateGuestSession('), context);
        vm.runInContext(between('function runVerify(',
            'var axonosPaymentOperationGeneration = 0;'), context);
    }
    return { context, window, state, elements, root, authenticate, restore, observeCredentials, response, currentScreen, advanceTime, useRealDashboard, useRealVerify };
}

async function run() {
    // Initial/switch-wallet dashboard setup runs synchronously inside wallet
    // verification. An exception here would be caught as failed authentication
    // and erase the freshly verified identity, requiring another signature.
    for (const previousWallet of ['', OTHER_WALLET]) {
        const b = browser();
        b.elements.get('axonos_dashboard_sessions_list').dataset.wallet = previousWallet;
        b.useRealDashboard();
        b.authenticate();
        assert.doesNotThrow(() => b.context.axonosOnWalletVerified(WALLET, { remaining_minutes: 71 }));
        b.currentScreen('dashboard');
        assert.deepEqual(b.state.statusReads, [WALLET]);
        assert.deepEqual(b.state.warningUpdates, [{ sessions: [], replace: true }]);
        b.state.refresh.resolve({ sessions: [] });
        await flush();
        assert.equal(b.window.verifiedWalletAddress, WALLET);
        assert.deepEqual(b.state.warningUpdates, [
            { sessions: [], replace: true }, { sessions: [], replace: true },
        ]);
        assert.equal(b.elements.get('axonos_dashboard_sessions_list').attributes['aria-busy'], 'false');
    }

    // Run the whole real verification response chain as well: dashboard setup
    // must not fall into runVerify's catch and clear successfully issued auth.
    for (const previousWallet of ['', OTHER_WALLET]) {
        const b = browser();
        b.elements.get('axonos_dashboard_sessions_list').dataset.wallet = previousWallet;
        b.useRealDashboard();
        b.useRealVerify();
        await b.context.runVerify(WALLET, { request: noop });
        assert.equal(b.window.verifiedWalletAddress, WALLET);
        assert.equal(b.window.verifiedWalletAuthToken, 'signed-test-token');
        assert.equal(b.window.axonosAllowVncConnect, true);
        b.currentScreen('dashboard');
        assert.notEqual(b.state.status, 'axonos_wallet_state_connect_error');
        assert.deepEqual(b.state.authRequests, ['/api/auth/challenge', '/api/auth/verify-wallet']);
        assert.deepEqual(b.state.providerRequests, ['eth_requestAccounts', 'personal_sign']);
    }

    // Warning reconciliation uses the fetched owned rows. Omit the card host
    // here because rendering individual session controls is a separate concern.
    {
        const b = browser();
        const session = { session_id: 41, gpu_count: 1, scheduled_stop_at: 123456 };
        b.elements.delete('axonos_dashboard_sessions_list');
        b.useRealDashboard();
        b.authenticate();
        b.context.axonosOnWalletVerified(WALLET, {});
        assert.deepEqual(b.state.warningUpdates, [{ sessions: [], replace: true }]);
        b.state.refresh.resolve({ sessions: [session] });
        await flush();
        assert.deepEqual(b.state.warningUpdates, [
            { sessions: [], replace: true }, { sessions: [session], replace: true },
        ]);
    }

    // Completing sign-in immediately reveals the workspace, even while its
    // session refresh is still pending and catalog/details were left open.
    {
        const b = browser();
        b.authenticate();
        b.context.axonosOnWalletVerified(WALLET, { remaining_minutes: 71 });
        b.currentScreen('dashboard');
        assert.equal(b.state.refreshes, 1);
        assert.equal(b.elements.get('axonos_dashboard_credits').textContent, '71');
        for (const id of ['axonos_catalog_modal', 'axonos_template_modal']) {
            assert.equal(b.elements.get(id).classList.contains('active'), false, id);
            assert.equal(b.elements.get(id).attributes['aria-hidden'], 'true', id);
        }
        assert.equal(b.elements.get('noVNC_credentials_dlg').classList.contains('noVNC_open'), false);
        assert.deepEqual(b.state.verifies, []);
    }

    // Re-authentication within an active launch or viewer must preserve it.
    for (const screen of ['wizard', 'viewer']) {
        const b = browser(screen === 'viewer' ? 'landing' : screen);
        b.authenticate();
        if (screen === 'viewer') b.root.classList.add('noVNC_connected');
        b.context.axonosOnWalletVerified(WALLET, {});
        b.currentScreen(screen === 'viewer' ? 'landing' : screen);
        assert.equal(b.state.refreshes, 1);
    }

    // A valid cookie restores identity and opens the workspace without any
    // provider prompt; verified:false still authenticates a zero-credit wallet.
    for (const verified of [true, false]) {
        const b = browser();
        b.restore();
        assert.equal(b.window.axonosWalletRestorePending, true);
        assert.equal(b.state.fetches[0].options.credentials, 'include');
        assert.equal(b.state.fetches[0].options.headers['X-Wallet-Address'], WALLET);
        b.response(true, { verified, auth_token: 'restored-test-token' });
        await flush();
        b.currentScreen('dashboard');
        assert.equal(b.window.verifiedWalletAddress, WALLET);
        assert.equal(b.window.verifiedWalletAuthToken, 'restored-test-token');
        assert.equal(b.window.axonosWalletRestorePending, false);
        assert.equal(b.elements.get('axonos_hero_browse_btn').textContent, 'Open workspace');
        assert.equal(b.elements.get('axonos_hero_browse_btn').disabled, false);
        assert.equal(b.state.timers.size, 0);
        assert.deepEqual(b.state.verifies, []);
        assert.deepEqual(b.state.providerRequests, []);
    }

    // An expired session returns to signed-out browsing without signing itself.
    {
        const b = browser();
        b.restore();
        b.response(false);
        await flush();
        b.currentScreen('landing');
        assert.equal(b.window.verifiedWalletAddress, null);
        assert.equal(b.window.axonosWalletRestorePending, false);
        assert.equal(b.elements.get('axonos_hero_browse_btn').textContent, 'Browse environments');
        assert.deepEqual(b.state.verifies, []);
    }

    // Network failure or an incomplete success body never turns a remembered
    // address into an authenticated identity.
    for (const failure of ['network', 'missing-token']) {
        const b = browser();
        b.restore();
        if (failure === 'network') b.state.fetch.reject(new Error('Connection unavailable'));
        else b.response(true, { verified: true });
        await flush();
        assert.equal(b.window.verifiedWalletAddress, null, failure);
        assert.equal(b.window.axonosWalletRestorePending, false, failure);
        assert.equal(b.elements.get('axonos_hero_browse_btn').disabled, false, failure);
        assert.equal(b.state.refreshes, 0, failure);
        assert.deepEqual(b.state.verifies, [], failure);
    }

    // A stalled restore releases the entry button, and a late response cannot
    // authenticate after its deadline.
    {
        const b = browser();
        b.restore();
        b.advanceTime(14999);
        await flush();
        assert.equal(b.window.axonosWalletRestorePending, true);
        assert.equal(b.elements.get('axonos_hero_browse_btn').disabled, true);
        b.advanceTime(1);
        await flush();
        assert.equal(b.window.axonosWalletRestorePending, false);
        assert.equal(b.elements.get('axonos_hero_browse_btn').disabled, false);
        assert.equal(b.state.fetches[0].options.signal.aborted, true);
        b.response(true, { verified: true, auth_token: 'late-test-token' });
        await flush();
        assert.equal(b.window.verifiedWalletAddress, null);
        b.currentScreen('landing');
    }

    // Receiving HTTP headers is not completion: the body can stall too.
    {
        const b = browser();
        const body = deferred();
        b.restore();
        b.state.fetch.resolve({ ok: true, json: () => body.promise });
        await flush();
        b.advanceTime(15000);
        await flush();
        assert.equal(b.window.axonosWalletRestorePending, false);
        body.resolve({ verified: true, auth_token: 'late-body-test-token' });
        await flush();
        assert.equal(b.window.verifiedWalletAddress, null);
        assert.equal(b.state.refreshes, 0);
    }

    // A new connection, sign-out, or demo entry supersedes a pending remembered
    // wallet response; a newer wallet must never be replaced by that response.
    for (const action of ['connect', 'verification', 'signout', 'new-wallet', 'guest']) {
        const b = browser();
        b.restore();
        if (action === 'guest') b.state.guest = true;
        else if (action === 'verification') b.context.axonosWalletVerifyGeneration += 1;
        else b.context.axonosWalletConnectGeneration += 1;
        if (action === 'new-wallet') b.authenticate(OTHER_WALLET);
        b.response(true, { verified: true, auth_token: 'stale-test-token' });
        await flush();
        assert.equal(b.window.verifiedWalletAddress, action === 'new-wallet' ? OTHER_WALLET : null, action);
        assert.equal(b.window.axonosWalletRestorePending, false, action);
        assert.equal(b.state.refreshes, 0, action);
    }

    // Merely opening a dialog cannot start a signature or reset an explicit
    // flow's current picker/checking/error state, including during restore.
    for (const pendingRestore of [false, true]) {
        const b = browser();
        b.window.axonosWalletRestorePending = pendingRestore;
        const notify = b.observeCredentials();
        for (const status of ['checking', 'choose-wallet', 'connect-error']) {
            b.state.status = status;
            notify();
            await flush();
            assert.equal(b.state.status, status);
        }
        assert.deepEqual(b.state.providerRequests, []);
        assert.deepEqual(b.state.verifies, []);
        assert.equal(b.context.axonosWalletConnectGeneration, 0);
    }

    // The observer must not cancel a pending explicit Connect Wallet attempt
    // and replace it with its own remembered-wallet signature request.
    {
        const b = browser();
        const notify = b.observeCredentials();
        b.context.onConnectWalletClick({ request: noop });
        const generation = b.context.axonosWalletConnectGeneration;
        notify();
        await flush();
        assert.equal(b.context.axonosWalletConnectGeneration, generation);
        b.state.account.resolve(WALLET);
        await flush();
        assert.deepEqual(b.state.verifies, [WALLET]);
        assert.deepEqual(b.state.providerRequests, []);
    }

    console.log('wallet entry runtime checks passed');
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
