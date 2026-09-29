/* Execute real connect/teardown methods with fake local browser/server state.
 * No network, wallet provider, real credentials, or compute is used. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const uiSource = fs.readFileSync(process.argv[2], 'utf8');
const pageSource = fs.readFileSync(process.argv[3], 'utf8');

function uiMethod(name, next) {
    const start = uiSource.indexOf('\n    ' + name + '(');
    const end = uiSource.indexOf('\n    ' + next + '(', start);
    assert.ok(start >= 0 && end > start, name);
    return uiSource.slice(start, end);
}

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const noop = () => {};

function browser() {
    const state = {
        now: 100000, remembers: [], fetchClaims: [], preflight: true,
        loader: false, workspaceReturns: 0,
    };
    const window = {
        verifiedWalletAddress: 'same-wallet',
        verifiedWalletAuthToken: 'local-test-auth',
        axonosOwnedSession: { sessionId: 101 },
        axonosEnsureWalletSessionCurrent: () => Promise.resolve(state.preflight),
        axonosRefreshPausedResumeStatus: () => Promise.resolve({ is_owner: false }),
        showConnectionLoader: () => { state.loader = true; },
        axonosHideConnectionLoader: () => { state.loader = false; },
        axonosCurrentSessionId: () => window.axonosOwnedSession?.sessionId ?? null,
        axonosRememberOwnedSession: (claim) => {
            state.remembers.push(claim.session_id);
            window.axonosOwnedSession = { sessionId: claim.session_id };
        },
        axonosClearDetachedSession: () => {
            window.axonosOwnedSession = null;
            window.axonosDetachedSession = null;
        },
    };
    const context = vm.createContext({
        window, document: { getElementById: () => null },
        localStorage: { removeItem: noop },
        Log: { Warn: noop, Error: noop, Info: noop },
        WebUtil: { getConfigVar: () => undefined, readSetting: () => undefined },
        _: (value) => value, Date: { now: () => state.now },
        Promise, Number, String, Array, setTimeout, clearTimeout, clearInterval,
        axonosPaymentOperationGeneration: 0, axonosWalletConnectGeneration: 0,
        axonosInvalidateWalletVerification: noop, setPayDepositButtonsBusy: noop,
        axonosSetTestCreditEligibility: noop, axonosSetGuestInviteEligibility: noop,
        axonosApplyResumeConnectUi: noop, setWalletUIState: noop,
        axonosSyncWalletUnavailableIndicator: noop,
    });
    const UI = vm.runInContext('({' + [
        uiMethod('_axonosInvalidateConnectAttempt', '_axonosConnectAttemptIsCurrent'),
        uiMethod('_axonosConnectAttemptIsCurrent', '_axonosReturnToWorkspace'),
        uiMethod('_axonosApplyConfirmedSessionRelease', '_axonosSessionOwnsServerSlot'),
        uiMethod('_axonosOnServerSessionEnded', '_axonosCompleteDetachUI'),
        uiMethod('connect', 'disconnect'),
        uiMethod('disconnect', '_axgtDisconnectForCreditExhaustion'),
        uiMethod('cancelReconnect', 'connectFinished'),
    ].join('\n') + '})', context);
    Object.assign(UI, {
        _axonosConnectGeneration: 0, _axgtSessionDesktopActive: () => false,
        getSetting: (key) => key === 'host' ? 'app.example.test' : undefined,
        hideStatus: noop, showStatus: noop, updateVisualState: noop,
        updateSessionControlButtons: noop, axonosSshEnabled: () => false,
        closeConnectPanel: noop, openConnectPanel: noop, openControlbar: noop,
        _axonosReturnToWorkspace: () => { state.workspaceReturns += 1; },
        _axonosReturnToHomeAfterDisconnect: noop,
        _axonosCancelWebRtcClient: () => Promise.resolve(),
        _axonosAwaitWebRtcCleanup: (promise) => Promise.resolve(promise),
        _axonosSessionReleaseContext: () => ({
            wallet: window.verifiedWalletAddress,
            sessionId: window.axonosCurrentSessionId(), hadServerSession: true,
        }),
        _axonosReleaseSessionBestEffort: () => Promise.resolve(state.release ?? true),
        _axonosNotifySessionReleaseResult: noop,
        _axonosFetchSessionClaim: () => {
            state.fetchClaims.push(window.axonosCurrentSessionId());
            return Promise.resolve({ granted: true, session_id: 202 });
        },
        // Stop at config lookup; viewer/network transport is outside this test.
        _axonosFetchJsonWithTimeout: () => new Promise(() => {}),
    });
    context.UI = UI;
    const clearStart = pageSource.indexOf('function clearWalletIdentityAndUi()');
    const clearEnd = pageSource.indexOf('function axonosDesktopSessionLive()', clearStart);
    assert.ok(clearStart >= 0 && clearEnd > clearStart);
    vm.runInContext(pageSource.slice(clearStart, clearEnd), context);
    function stage(id = 101) {
        const pending = {
            wallet: 'same-wallet', createdAt: state.now,
            claim: { granted: true, session_id: id, ssh_enabled: false },
        };
        window.axonosOwnedSession = { sessionId: id };
        window.axonosPendingSessionClaim = pending;
        return pending;
    }
    stage();
    return { UI, window, state, stage, clearIdentity: context.clearWalletIdentityAndUi };
}

async function run() {
    // The normal handoff must still reuse its authoritative response exactly once.
    {
        const { UI, window, state } = browser();
        UI.connect(null, 'local-test-password');
        await flush();
        assert.deepEqual(state.remembers, [101]);
        assert.deepEqual(state.fetchClaims, []);
        assert.equal(window.axonosPendingSessionClaim, null);
    }

    // A rejected preflight followed by confirmed End or sign-out cannot revive
    // the ended response during same-wallet reauthentication within 30 seconds.
    for (const signOut of [false, true]) {
        const { UI, window, state, clearIdentity } = browser();
        state.preflight = false;
        UI.connect(null, 'local-test-password');
        await flush();
        assert.ok(window.axonosPendingSessionClaim);
        await UI.disconnect();
        await flush();
        if (signOut) {
            clearIdentity();
            window.verifiedWalletAddress = 'same-wallet';
            window.verifiedWalletAuthToken = 'local-test-auth';
        }
        assert.equal(window.axonosPendingSessionClaim, null);
        state.preflight = true;
        UI.connect(null, 'local-test-password');
        await flush();
        assert.deepEqual(state.fetchClaims, [null]);
        assert.deepEqual(state.remembers, [202]);
    }

    // An already-snapshotted launch is cancelled by teardown before preflight
    // resolves; no fallback spawn request may replace that cancelled operation.
    for (const action of ['disconnect', 'identity-clear', 'cancel-reconnect', 'discard']) {
        const { UI, window, state, clearIdentity } = browser();
        const preflight = deferred();
        state.preflight = preflight.promise;
        UI.connect(null, 'local-test-password');
        if (action === 'disconnect') await UI.disconnect();
        else if (action === 'identity-clear') clearIdentity();
        else if (action === 'cancel-reconnect') UI.cancelReconnect();
        else window.axonosPendingSessionClaim = null; // Exact-session dashboard End.
        window.verifiedWalletAddress = 'same-wallet';
        preflight.resolve(true);
        await flush();
        assert.deepEqual(state.fetchClaims, [], action);
        assert.deepEqual(state.remembers, [], action);
        assert.equal(window.axonosPendingSessionClaim, null, action);
    }

    // A newer pending response must not be consumed by an older snapshot.
    {
        const { UI, window, state, stage } = browser();
        const preflight = deferred();
        state.preflight = preflight.promise;
        UI.connect(null, 'local-test-password');
        const newer = stage(202);
        preflight.resolve(true);
        await flush();
        assert.deepEqual(state.fetchClaims, []);
        assert.deepEqual(state.remembers, []);
        assert.equal(window.axonosPendingSessionClaim, newer);
    }

    // Release clears at initiation, not on its late response: preserve a newer
    // generation's authoritative pending response during its own preflight.
    {
        const { UI, window, state, stage } = browser();
        const release = deferred();
        state.release = release.promise;
        const ending = UI.disconnect();
        assert.equal(window.axonosPendingSessionClaim, null);
        const newer = stage(202);
        const preflight = deferred();
        state.preflight = preflight.promise;
        UI.connect(null, 'local-test-password');
        release.resolve(true);
        await ending;
        await flush();
        assert.equal(window.axonosPendingSessionClaim, newer);
        preflight.resolve(true);
        await flush();
        assert.deepEqual(state.fetchClaims, []);
        assert.deepEqual(state.remembers, [202]);
    }

    // Selecting S2 on the dashboard must not reuse a still-cached S1 response.
    {
        const { UI, window, state } = browser();
        window.axonosOwnedSession = { sessionId: 202 };
        UI.connect(null, 'local-test-password');
        await flush();
        assert.deepEqual(state.fetchClaims, [202]);
        assert.deepEqual(state.remembers, [202]);
    }

    // A delayed heartbeat for S1 must not discard S2's newer response.
    for (const endedId of [101, 202]) {
        const { UI, window, stage } = browser();
        const pending = stage(202);
        UI._axonosOnServerSessionEnded(endedId);
        assert.equal(window.axonosPendingSessionClaim, endedId === 202 ? null : pending);
    }

    // Eligibility must still hold when asynchronous preflight finally finishes.
    for (const clockDelta of [30001, -1]) {
        const { UI, window, state } = browser();
        const preflight = deferred();
        state.preflight = preflight.promise;
        UI.connect(null, 'local-test-password');
        state.now += clockDelta;
        preflight.resolve(true);
        await flush();
        assert.deepEqual(state.fetchClaims, []);
        assert.deepEqual(state.remembers, []);
        assert.equal(window.axonosPendingSessionClaim, null);
        assert.equal(state.loader, false);
        assert.equal(state.workspaceReturns, 1);
    }
    // A binding changed during preflight must also cancel the old snapshot,
    // without leaving its loader visible or allocating another session.
    {
        const { UI, window, state } = browser();
        const preflight = deferred();
        state.preflight = preflight.promise;
        UI.connect(null, 'local-test-password');
        window.axonosOwnedSession = { sessionId: 202 };
        preflight.resolve(true);
        await flush();
        assert.deepEqual(state.fetchClaims, []);
        assert.deepEqual(state.remembers, []);
        assert.equal(window.axonosPendingSessionClaim, null);
        assert.equal(state.loader, false);
        assert.equal(state.workspaceReturns, 1);
    }
    console.log('session claim runtime checks passed');
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
