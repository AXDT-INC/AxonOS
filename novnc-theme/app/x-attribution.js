/* App-owned, first-party X conversion-sharing consent bridge.
 * No X script, image, pixel, tag manager, cookie, or network request is used.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'axonos_x_attribution_context_v1';
    var REVOCATION_PENDING_KEY = 'axonos_x_attribution_revoke_pending_v1';
    var REVOCATION_CAPABILITY_KEY = 'axonos_x_attribution_revoke_capability_v1';
    var OWNER_KEY = 'axonos_x_attribution_tab_owner_v1';
    var BIND_WALLET_COMMITMENT_KEY = 'axonos_x_attribution_wallet_commitment_v1';
    var LOCK_PREFIX = 'axonos_x_attribution_tab_ownership_v1:';
    var OPAQUE_CONTEXT_RE = /^[A-Za-z0-9_-]{32,2048}$/;
    var CSRF_RE = /^[A-Za-z0-9_-]{32,128}$/;
    var OWNER_RE = /^[a-f0-9]{32}$/;
    var WALLET_RE = /^0x[0-9a-f]{40}$/i;
    var GUEST_WALLET_RE = /^0x6775657374[0-9a-f]{30}$/i;
    var REQUEST_TIMEOUT_MS = 10000;
    var BIND_TIMEOUT_MS = 5000;

    // The URL bootstrap is the only code allowed to put the raw click on a
    // global. Consume it immediately, before later scripts can read or replace
    // it, and retain only this module-private first-touch value.
    // The parser-blocking bootstrap already validates the exact decoded value.
    // Do not trim here: boundary whitespace/control characters must be rejected,
    // never normalized into an advertising identifier.
    var landingClick = String(window.axonosPendingTwclid || '');
    try { delete window.axonosPendingTwclid; } catch (e) {
        window.axonosPendingTwclid = '';
    }

    var landingClickEligible = !!landingClick;
    var landingClickStatusSent = false;
    var landingClickBoundToCurrentLifecycle = false;
    var context = '';
    var csrf = '';
    var state = 'unavailable';
    var enabled = false;
    var revocationAvailable = false;
    var newLifecycleAvailable = false;
    var attributionTtlDays = null;
    var attributionExpiresAt = null;
    var ownershipEstablished = false;
    var persistentContextSafe = false;
    var revokeOnlyLifecycle = false;
    var revocationPending = false;
    var gpcActive = false;
    var ownerKey = '';
    var attributionBootGeneration = 0;
    var stateGeneration = 0;
    var statusSequence = 0;
    var updateSequence = 0;
    var activeStatusController = null;
    var activeUpdateController = null;
    var activeUpdateSequence = 0;
    var updateInFlight = false;
    var suppressionRequestScheduled = false;
    var bindAttemptKey = '';
    var boundWallet = '';
    var bindWalletCandidate = '';
    var bindWalletCheck = null;
    var bindWalletCommitment = '';
    var bindWalletPending = false;
    var walletAuthenticationCandidate = '';
    var revokeCapability = { context: '', csrf: '' };

    try { gpcActive = navigator.globalPrivacyControl === true; } catch (e) { }
    try {
        var stored = String(sessionStorage.getItem(STORAGE_KEY) || '').trim();
        if (OPAQUE_CONTEXT_RE.test(stored)) context = stored;
        revocationPending = sessionStorage.getItem(REVOCATION_PENDING_KEY) === '1';
        var storedOwner = String(sessionStorage.getItem(OWNER_KEY) || '').trim();
        if (OWNER_RE.test(storedOwner)) ownerKey = storedOwner;
        var storedWalletCommitment = String(
            sessionStorage.getItem(BIND_WALLET_COMMITMENT_KEY) || ''
        ).trim().toLowerCase();
        if (/^[a-f0-9]{64}$/.test(storedWalletCommitment)) {
            bindWalletCommitment = storedWalletCommitment;
            // A restored context stays private until the currently verified
            // wallet proves it matches this one-way, context-scoped latch.
            bindWalletPending = true;
        }
    } catch (e) { }
    try {
        var storedRevokeCapability = JSON.parse(
            sessionStorage.getItem(REVOCATION_CAPABILITY_KEY) || 'null'
        );
        if (storedRevokeCapability &&
            OPAQUE_CONTEXT_RE.test(String(storedRevokeCapability.context || '')) &&
            CSRF_RE.test(String(storedRevokeCapability.csrf || ''))) {
            revokeCapability = {
                context: String(storedRevokeCapability.context),
                csrf: String(storedRevokeCapability.csrf)
            };
            revocationAvailable = true;
        }
    } catch (e) { }

    // Pre-release/invalid storage has no provable tab owner. Keep a separately
    // scoped server-encrypted revocation capability, but never promote it into
    // the context used by wallet, payment, or session requests.
    if (context && !ownerKey) {
        context = '';
        revokeOnlyLifecycle = !!revokeCapability.context;
    }
    var hasPrivateLifecycleAtLoad = !!(context || revokeCapability.context);
    var needsTabOwner = !!(
        landingClickEligible || hasPrivateLifecycleAtLoad || revocationPending
    );
    if (needsTabOwner && !ownerKey) ownerKey = randomNonce();
    try {
        if (needsTabOwner) sessionStorage.setItem(OWNER_KEY, ownerKey);
        else sessionStorage.removeItem(OWNER_KEY);
        if (!context) sessionStorage.removeItem(STORAGE_KEY);
        if (gpcActive && hasPrivateLifecycleAtLoad) {
            revocationPending = true;
            sessionStorage.setItem(REVOCATION_PENDING_KEY, '1');
        }
    } catch (e) { }
    if (gpcActive) discardLandingClick();

    // Fail closed during boot. Business code can read only this public value;
    // the revoke-only ticket and CSRF value never leave this closure.
    window.axonosAttributionContext = '';
    window.axonosAttributionReady = false;

    function randomNonce() {
        try {
            var values = new Uint32Array(4);
            crypto.getRandomValues(values);
            return Array.prototype.map.call(values, function (value) {
                return value.toString(16).padStart(8, '0');
            }).join('');
        } catch (e) {
            return (
                String(Date.now()) + String(Math.random()).replace(/\D/g, '') +
                '00000000000000000000000000000000'
            ).slice(0, 32);
        }
    }

    function abortRequest(controller) {
        if (!controller) return;
        try { controller.abort(); } catch (e) { }
    }

    function discardLandingClick() {
        // The raw vendor identifier is needed only between URL capture and an
        // explicit choice (or a requested new-lifecycle choice). Closed,
        // suppressed, or completed paths overwrite the module-private string
        // instead of retaining it in this page's closure.
        landingClick = '';
        landingClickEligible = false;
        landingClickBoundToCurrentLifecycle = false;
    }

    function invalidateAsyncRequests() {
        stateGeneration += 1;
        abortRequest(activeStatusController);
        abortRequest(activeUpdateController);
        activeStatusController = null;
        activeUpdateController = null;
        activeUpdateSequence = 0;
        updateInFlight = false;
        return stateGeneration;
    }

    function requestIsCurrent(generation, sequence, kind) {
        return generation === stateGeneration && (
            (kind === 'status' && sequence === statusSequence) ||
            (kind === 'update' && sequence === updateSequence)
        );
    }

    function isAbortError(error) {
        return !!(error && error.name === 'AbortError');
    }

    function rememberRevocationCapability(contextValue, csrfValue) {
        // Pre-consent contexts are intentionally memory-only. There is no
        // granted conversion stream to revoke, and persisting this capability
        // would make a reload retain attribution state before a choice.
        if (state === 'unset') return false;
        var candidateContext = String(contextValue || '').trim();
        var candidateCsrf = String(csrfValue || '').trim();
        if (!OPAQUE_CONTEXT_RE.test(candidateContext) || !CSRF_RE.test(candidateCsrf)) {
            return false;
        }
        revokeCapability = { context: candidateContext, csrf: candidateCsrf };
        revocationAvailable = true;
        try {
            sessionStorage.setItem(
                REVOCATION_CAPABILITY_KEY,
                JSON.stringify(revokeCapability)
            );
        } catch (e) { }
        return true;
    }

    function clearRevocationCapabilityAfterConfirmedRevoke() {
        revokeCapability = { context: '', csrf: '' };
        try { sessionStorage.removeItem(REVOCATION_CAPABILITY_KEY); } catch (e) { }
        clearBindWalletLatch();
    }

    function discardLegacyUnsetRevocationCapability() {
        revokeCapability = { context: '', csrf: '' };
        revocationAvailable = false;
        try { sessionStorage.removeItem(REVOCATION_CAPABILITY_KEY); } catch (e) { }
    }

    function setRevocationPending(value) {
        revocationPending = value === true;
        try {
            if (revocationPending) {
                sessionStorage.setItem(REVOCATION_PENDING_KEY, '1');
                // A legacy ticket may not have a separately persisted CSRF
                // capability yet. Keep that private ticket until status can
                // mint the revoke-only pair; it is never exposed publicly.
                if (revokeCapability.context || !context) {
                    sessionStorage.removeItem(STORAGE_KEY);
                }
            } else {
                sessionStorage.removeItem(REVOCATION_PENDING_KEY);
            }
        } catch (e) { }
        if (revocationPending) window.axonosAttributionContext = '';
        var control = document.getElementById('axonos-x-privacy-control');
        if (control) {
            control.textContent = revocationPending
                ? 'Conversion privacy action needed' : 'Conversion sharing';
        }
    }

    function latchSuppression(authoritativeGpc, invalidate) {
        if (invalidate !== false) invalidateAsyncRequests();
        if (authoritativeGpc) gpcActive = true;
        discardLandingClick();
        if (context && csrf && (state === 'granted' ||
            state === 'revocation_required' || revocationAvailable ||
            revokeCapability.context)) {
            rememberRevocationCapability(context, csrf);
        }
        if (state === 'granted') state = 'revocation_required';
        window.axonosAttributionContext = '';
        setRevocationPending(true);
        removeDialog();
        synchronizePublicContext();
    }

    function saveContext(value) {
        var candidate = String(value || '').trim();
        context = OPAQUE_CONTEXT_RE.test(candidate) ? candidate : '';
        try {
            // An unset ticket represents pre-consent state. Keep it in this
            // page's memory only: a reload before the choice deliberately loses
            // attribution. Granted and closed/revocable capabilities may remain
            // tab-scoped so consent or withdrawal survives navigation.
            var verifiedWallet = String(
                window.verifiedWalletAddress || ''
            ).trim().toLowerCase();
            var walletLatchSafe = !WALLET_RE.test(verifiedWallet) ||
                GUEST_WALLET_RE.test(verifiedWallet) ||
                (!bindWalletPending && boundWallet === verifiedWallet);
            if (context && state !== 'unset' && persistentContextSafe &&
                !revocationPending && !gpcActive && walletLatchSafe) {
                sessionStorage.setItem(STORAGE_KEY, context);
            } else if (!(revocationPending && context && !revokeCapability.context)) {
                sessionStorage.removeItem(STORAGE_KEY);
            }
            sessionStorage.setItem(OWNER_KEY, ownerKey);
        } catch (e) { }
    }

    function rotateTabOwner() {
        ownerKey = randomNonce();
        try { sessionStorage.setItem(OWNER_KEY, ownerKey); } catch (e) { }
    }

    function clearBindWalletLatch() {
        boundWallet = '';
        bindWalletCandidate = '';
        bindWalletCheck = null;
        bindWalletCommitment = '';
        bindWalletPending = false;
        walletAuthenticationCandidate = '';
        bindAttemptKey = '';
        try { sessionStorage.removeItem(BIND_WALLET_COMMITMENT_KEY); } catch (e) { }
    }

    function walletCommitment(contextValue, wallet) {
        try {
            if (!window.crypto || !window.crypto.subtle ||
                typeof window.crypto.subtle.digest !== 'function' ||
                typeof TextEncoder !== 'function') {
                return Promise.reject(new Error('secure wallet commitment unavailable'));
            }
            var material = new TextEncoder().encode(
                'AxonOS X attribution wallet latch v1\u0000' +
                String(contextValue) + '\u0000' + String(wallet)
            );
            return Promise.resolve(window.crypto.subtle.digest('SHA-256', material)).then(
                function (digest) {
                    return Array.prototype.map.call(new Uint8Array(digest), function (value) {
                        return value.toString(16).padStart(2, '0');
                    }).join('');
                }
            );
        } catch (e) {
            return Promise.reject(e);
        }
    }

    function rejectCrossWalletContext() {
        // Keep only the private revoke capability. A later wallet must never
        // receive this context even if the first bind datagram was dropped.
        latchSuppression(false, true);
        installPrivacyControl(true);
        scheduleSuppressionRequest();
    }

    function establishBindWalletLatch(wallet) {
        if (boundWallet) {
            if (boundWallet !== wallet) rejectCrossWalletContext();
            return Promise.resolve(boundWallet === wallet);
        }
        if (bindWalletCheck) {
            if (bindWalletCandidate !== wallet) rejectCrossWalletContext();
            return bindWalletCheck.then(function () {
                return boundWallet === wallet && !revocationPending && !gpcActive;
            });
        }

        var candidateContext = context;
        bindWalletCandidate = wallet;
        bindWalletPending = true;
        // If the page exits before WebCrypto completes, the granted context is
        // not restored without its wallet latch. The revoke capability remains.
        window.axonosAttributionContext = '';
        try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) { }
        bindWalletCheck = walletCommitment(candidateContext, wallet).then(
            function (commitment) {
                if (candidateContext !== context || bindWalletCandidate !== wallet ||
                    state !== 'granted' || gpcActive || revocationPending) {
                    return false;
                }
                if (bindWalletCommitment && bindWalletCommitment !== commitment) {
                    rejectCrossWalletContext();
                    return false;
                }
                bindWalletCommitment = commitment;
                boundWallet = wallet;
                bindWalletPending = false;
                bindWalletCheck = null;
                try {
                    sessionStorage.setItem(BIND_WALLET_COMMITMENT_KEY, commitment);
                } catch (e) {
                    // Without a durable tab latch, do not leave a restorable
                    // context which another wallet could claim after reload.
                    rejectCrossWalletContext();
                    return false;
                }
                saveContext(context);
                return true;
            },
            function () {
                bindWalletCheck = null;
                rejectCrossWalletContext();
                return false;
            }
        );
        return bindWalletCheck;
    }

    function abandonClonedContext() {
        if (context && csrf) rememberRevocationCapability(context, csrf);
        context = '';
        csrf = '';
        state = 'unavailable';
        revokeOnlyLifecycle = !!revokeCapability.context;
        ownershipEstablished = false;
        try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) { }
        rotateTabOwner();
        window.axonosAttributionContext = '';
    }

    function contextOwnershipReady() {
        if (!navigator.locks || typeof navigator.locks.request !== 'function') {
            // Without a browser-enforced per-tab lock, a restored context could
            // have been cloned. It remains usable only through the private
            // revocation path; a brand-new in-page lifecycle stays memory-only.
            if (context) abandonClonedContext();
            ownershipEstablished = true;
            return Promise.resolve();
        }
        return new Promise(function (resolve) {
            var settled = false;
            function finish(value) {
                if (settled) return;
                settled = true;
                ownershipEstablished = value === true;
                resolve();
            }
            function acquire(attempt) {
                var lockOwner = ownerKey;
                var request;
                try {
                    request = navigator.locks.request(
                        LOCK_PREFIX + lockOwner,
                        { mode: 'exclusive', ifAvailable: true },
                        function (lock) {
                            if (settled || lockOwner !== ownerKey) return undefined;
                            if (!lock) {
                                if (attempt < 3) {
                                    abandonClonedContext();
                                    acquire(attempt + 1);
                                } else {
                                    abandonClonedContext();
                                    finish(true);
                                }
                                return undefined;
                            }
                            persistentContextSafe = true;
                            revokeOnlyLifecycle = false;
                            finish(true);
                            // Hold ownership for the page lifetime. A duplicate
                            // tab receives null from its ifAvailable request.
                            return new Promise(function (release) {
                                window.addEventListener('pagehide', function () {
                                    release();
                                }, { once: true });
                            });
                        }
                    );
                } catch (e) {
                    request = Promise.reject(e);
                }
                Promise.resolve(request).catch(function () {
                    if (!settled) {
                        persistentContextSafe = false;
                        if (context) abandonClonedContext();
                        finish(true);
                    }
                });
            }
            acquire(0);
        });
    }

    function maybeBindVerifiedWallet() {
        var wallet = String(window.verifiedWalletAddress || '').trim().toLowerCase();
        var authToken = String(window.verifiedWalletAuthToken || '').trim();
        if (state !== 'granted' || gpcActive || revocationPending ||
            !WALLET_RE.test(wallet) || GUEST_WALLET_RE.test(wallet) ||
            !authToken || !OPAQUE_CONTEXT_RE.test(context)) return;

        // A wallet authentication may be in flight while the globals still
        // describe the previously verified wallet.  The synchronous request
        // hook below owns that transition; an old wallet notification must not
        // race it and revoke or reclaim the candidate's context.
        if (walletAuthenticationCandidate &&
            walletAuthenticationCandidate !== wallet) return;

        if (!boundWallet) {
            establishBindWalletLatch(wallet).then(function (allowed) {
                if (allowed) synchronizePublicContext();
            });
            return;
        }
        if (boundWallet !== wallet) {
            rejectCrossWalletContext();
            return;
        }
        var publicContext = String(window.axonosAttributionContext || '').trim();
        if (!OPAQUE_CONTEXT_RE.test(publicContext)) return;

        var attemptKey = publicContext + ':' + wallet;
        if (bindAttemptKey === attemptKey) return;
        bindAttemptKey = attemptKey;
        var attemptGeneration = stateGeneration;

        var controller = null;
        var timer = null;
        try {
            controller = typeof AbortController === 'function'
                ? new AbortController() : null;
            timer = controller ? setTimeout(function () {
                abortRequest(controller);
            }, BIND_TIMEOUT_MS) : null;
            var bindHeaders = {
                'Content-Type': 'application/json',
                'X-AXGT-Auth-Token': authToken,
                'X-AxonOS-Attribution': publicContext
            };
            // This is deliberately fire-and-forget: attribution can never delay
            // or fail auth, payments, session launch, or reconnect. Even a
            // synchronous Fetch/AbortController failure is contained here.
            Promise.resolve(fetch('/api/x-attribution/bind', {
                method: 'POST', credentials: 'same-origin', headers: bindHeaders,
                cache: 'no-store', referrerPolicy: 'no-referrer',
                body: JSON.stringify({ wallet_address: wallet }),
                signal: controller ? controller.signal : undefined
            })).then(function (response) {
                if (!response || !response.ok) return false;
                return Promise.resolve(response.json()).then(function (data) {
                    return !!(data && data.ok === true && data.accepted === true);
                }, function () { return false; });
            }, function () { return false; }).then(function (accepted) {
                if (timer) clearTimeout(timer);
                if (!accepted && bindAttemptKey === attemptKey &&
                    attemptGeneration === stateGeneration && context === publicContext &&
                    String(window.verifiedWalletAddress || '').trim().toLowerCase() === wallet) {
                    // Retry only on a later browser lifecycle/online signal. Do
                    // not spin when the isolated local worker is unavailable.
                    bindAttemptKey = '';
                }
            });
        } catch (e) {
            if (timer) clearTimeout(timer);
            if (bindAttemptKey === attemptKey && attemptGeneration === stateGeneration) {
                bindAttemptKey = '';
            }
        }
    }

    // This is the only API request builders may use to obtain attribution for
    // a wallet.  It synchronously withdraws the public value before starting
    // the asynchronous WebCrypto check, and returns a context only after that
    // exact wallet owns the private latch.  Thus a verify request for wallet B
    // can never inherit wallet A's previously public context.
    window.axonosAttributionContextForWalletCandidate = function (walletAddress) {
        var wallet = String(walletAddress || '').trim().toLowerCase();
        if (!WALLET_RE.test(wallet) || GUEST_WALLET_RE.test(wallet)) return '';

        recheckGlobalPrivacyControl();
        if (gpcActive || revocationPending || !enabled || state !== 'granted' ||
            !OPAQUE_CONTEXT_RE.test(context) || !ownershipEstablished ||
            revokeOnlyLifecycle) {
            return '';
        }

        if (walletAuthenticationCandidate &&
            walletAuthenticationCandidate !== wallet) {
            walletAuthenticationCandidate = wallet;
            rejectCrossWalletContext();
            return '';
        }
        if (boundWallet && boundWallet !== wallet) {
            walletAuthenticationCandidate = wallet;
            rejectCrossWalletContext();
            return '';
        }
        if (bindWalletCandidate && bindWalletCandidate !== wallet) {
            walletAuthenticationCandidate = wallet;
            rejectCrossWalletContext();
            return '';
        }

        if (!boundWallet) {
            // Withhold first, before digest() can yield.  A stored commitment
            // mismatch is discovered asynchronously, but no request can see
            // the context during that interval.
            walletAuthenticationCandidate = wallet;
            window.axonosAttributionContext = '';
            establishBindWalletLatch(wallet).then(function (allowed) {
                if (allowed) synchronizePublicContext();
            });
            return '';
        }

        if (bindWalletPending || boundWallet !== wallet) return '';
        return context;
    };

    // Wallet verification calls this after publishing its wallet and auth
    // globals. If attribution wins the race instead, synchronizePublicContext
    // invokes the same idempotent, once-per-context path.
    window.axonosNotifyWalletVerified = function (walletAddress) {
        var current = String(window.verifiedWalletAddress || '').trim().toLowerCase();
        if (String(walletAddress || '').trim().toLowerCase() !== current) return;
        if (walletAuthenticationCandidate === current) {
            walletAuthenticationCandidate = '';
        }
        synchronizePublicContext();
    };

    function synchronizePublicContext() {
        var verifiedWallet = String(window.verifiedWalletAddress || '').trim().toLowerCase();
        var walletCompatible = !bindWalletPending && (
            !WALLET_RE.test(verifiedWallet) ||
            (!!boundWallet && boundWallet === verifiedWallet)
        );
        var mayExpose = ownershipEstablished && !revokeOnlyLifecycle &&
            !gpcActive && !revocationPending && enabled &&
            state === 'granted' && !!context && walletCompatible;
        window.axonosAttributionContext = mayExpose ? context : '';
        window.axonosAttributionReady = true;
        // A wallet may have verified while status/grant was still in flight.
        // Start (or verify) its latch from the private context even when public
        // exposure is intentionally waiting on that latch.
        if (mayExpose || (state === 'granted' && !!context &&
            WALLET_RE.test(verifiedWallet) && !GUEST_WALLET_RE.test(verifiedWallet))) {
            maybeBindVerifiedWallet();
        }
    }

    function privateRequestHeaders(includeCsrf) {
        var result = {};
        // Never mix the ordinary ticket with the revoke-only capability's
        // CSRF. During startup a restored ordinary context may not have learned
        // its CSRF yet; privacy updates must then use the complete persisted
        // revoke pair rather than send an unauthenticated withdrawal.
        var useOrdinaryContext = !!context && (!includeCsrf || !!csrf);
        var requestContext = useOrdinaryContext ? context : revokeCapability.context;
        var requestCsrf = useOrdinaryContext ? csrf : revokeCapability.csrf;
        if (requestContext) result['X-AxonOS-Attribution'] = requestContext;
        if (includeCsrf && requestCsrf) result['X-AxonOS-CSRF'] = requestCsrf;
        return result;
    }

    function applyServerMetadata(data) {
        var ttl = Number(data && data.attribution_ttl_days);
        var expires = Number(data && (data.expires_at || data.attribution_expires_at));
        attributionTtlDays = Number.isFinite(ttl) && ttl > 0 ? ttl : null;
        attributionExpiresAt = Number.isFinite(expires) && expires > 0 ? expires : null;
        newLifecycleAvailable = data && data.new_lifecycle_available === true;
    }

    function serverRequiresGpcSuppression(data) {
        return !!(data && (
            data.gpc_applied === true || String(data.state || '') === 'revocation_required'
        ));
    }

    function browserGpcIsActive() {
        try { return navigator.globalPrivacyControl === true; } catch (e) { return false; }
    }

    function recheckGlobalPrivacyControl() {
        // Some browsers/extensions apply GPC after the initial document boot.
        // A one-way latch on lifecycle events closes that exposure without a
        // timer, a status poll, or creation of attribution state for an idle
        // visitor.
        if (gpcActive || !browserGpcIsActive()) return false;

        var hasTrackedLifecycle = !!(
            context || revokeCapability.context || revocationPending ||
            state === 'granted' || state === 'revocation_required'
        );
        if (!hasTrackedLifecycle) {
            invalidateAsyncRequests();
            gpcActive = true;
            discardLandingClick();
            enabled = false;
            state = 'denied';
            window.axonosAttributionContext = '';
            removeDialog();
            synchronizePublicContext();
            return true;
        }

        // Persist the withdrawal before any asynchronous work. The private
        // ticket is retained only to authenticate deletion; it is never made
        // available to wallet, payment, session, or reconnect request builders.
        latchSuppression(true, true);
        installPrivacyControl(true);
        if ((context && csrf) ||
            (revokeCapability.context && revokeCapability.csrf)) {
            scheduleSuppressionRequest();
        } else if (context) {
            // A restored ticket may still be waiting for status to return its
            // CSRF value. Refresh that existing ticket once; this cannot mint a
            // lifecycle because the context header is present.
            requestStatus().catch(function () {
                synchronizePublicContext();
                installPrivacyControl(true);
            });
        }
        return true;
    }

    function scheduleSuppressionRequest() {
        if (suppressionRequestScheduled || !revocationPending) return;
        suppressionRequestScheduled = true;
        setTimeout(function () {
            suppressionRequestScheduled = false;
            if (!revocationPending) return;
            var mustRevoke = gpcActive || state === 'granted' ||
                state === 'revocation_required' || !!revokeCapability.context;
            update(mustRevoke ? 'revoke' : 'decline').then(function (result) {
                if (!result || result.stale !== true) removeDialog();
            }).catch(function () {
                synchronizePublicContext();
                installPrivacyControl(true);
            });
        }, 0);
    }

    function requestStatus() {
        abortRequest(activeStatusController);
        var generation = stateGeneration;
        var sequence = ++statusSequence;
        var controller = null;
        try {
            controller = typeof AbortController === 'function'
                ? new AbortController() : null;
        } catch (e) { controller = null; }
        activeStatusController = controller;
        var timedOut = false;
        var timer = controller ? setTimeout(function () {
            timedOut = true;
            abortRequest(controller);
        }, REQUEST_TIMEOUT_MS) : null;
        var requestHeaders = privateRequestHeaders(false);
        var usedPrivateContext = !!requestHeaders['X-AxonOS-Attribution'];
        var usedRevokeCapability = !context && usedPrivateContext;
        var sentLandingClick = false;
        var revalidatingLiveUnset = usedPrivateContext && state === 'unset' &&
            landingClickBoundToCurrentLifecycle;
        if (landingClickEligible && landingClick &&
            ((!usedPrivateContext && !landingClickStatusSent) || revalidatingLiveUnset) &&
            !gpcActive && !revocationPending) {
            requestHeaders['X-AxonOS-Landing-Click'] = landingClick;
            landingClickStatusSent = true;
            sentLandingClick = true;
        }

        var statusRequest;
        try {
            statusRequest = fetch('/api/x-attribution/status', {
                method: 'GET', credentials: 'same-origin', headers: requestHeaders,
                cache: 'no-store', referrerPolicy: 'no-referrer',
                signal: controller ? controller.signal : undefined
            });
        } catch (error) {
            statusRequest = Promise.reject(error);
        }
        return Promise.resolve(statusRequest).then(function (response) {
            if (!response.ok) throw new Error('status unavailable');
            return response.json();
        }).then(function (data) {
            if (!requestIsCurrent(generation, sequence, 'status')) {
                return { stale: true };
            }
            data = data || {};
            var responseState = String(data.state || 'unavailable');
            var responseContext = String(data.context || '').trim();
            var responseCsrf = String(data.csrf || '').trim();
            var validPair = OPAQUE_CONTEXT_RE.test(responseContext) &&
                CSRF_RE.test(responseCsrf);
            // Re-read the browser signal at the last safe point as well as on
            // lifecycle events. If it changed while Fetch was in flight, this
            // response can refresh a private revoke capability but can never
            // publish sharing.
            var authoritativeGpc = serverRequiresGpcSuppression(data) ||
                browserGpcIsActive();

            enabled = data.enabled === true;
            state = responseState;
            applyServerMetadata(data);
            if (responseState !== 'granted' && responseState !== 'revocation_required') {
                clearBindWalletLatch();
            }
            if (usedRevokeCapability && responseState === 'unset' && validPair) {
                // Clean up a capability written by an older frontend. Current
                // code never stores pre-consent tickets.
                discardLegacyUnsetRevocationCapability();
            }
            if (validPair) {
                if (!usedRevokeCapability) {
                    if (authoritativeGpc) context = responseContext;
                    else saveContext(responseContext);
                    csrf = responseCsrf;
                    revokeOnlyLifecycle = false;
                }
                if (data.revocation_available === true ||
                    responseState === 'granted' ||
                    responseState === 'revocation_required' ||
                    String(data.prior_state || '') === 'granted') {
                    rememberRevocationCapability(responseContext, responseCsrf);
                }
            }
            landingClickBoundToCurrentLifecycle = !!(
                sentLandingClick && data.landing_click_accepted === true &&
                responseState === 'unset' && validPair
            );
            revocationAvailable = data.revocation_available === true ||
                !!revokeCapability.context;

            if (authoritativeGpc) {
                if (!validPair && !revokeCapability.context) {
                    // The authoritative server rejected the landing candidate
                    // under GPC without issuing or recognizing any lifecycle.
                    // There is nothing remote to revoke or persist.
                    invalidateAsyncRequests();
                    gpcActive = true;
                    discardLandingClick();
                    enabled = false;
                    context = '';
                    csrf = '';
                    revocationAvailable = false;
                    setRevocationPending(false);
                    ownerKey = '';
                    try {
                        sessionStorage.removeItem(STORAGE_KEY);
                        sessionStorage.removeItem(OWNER_KEY);
                    } catch (e) { }
                    removeDialog();
                    synchronizePublicContext();
                    return { authoritativeGpc: true, noRemoteState: true };
                }
                latchSuppression(true, true);
                installPrivacyControl(true);
                scheduleSuppressionRequest();
                return { authoritativeGpc: true };
            }
            // A local GPC/revoke latch dominates a successful but older-looking
            // status. It may refresh the private capability, never re-enable it.
            if (gpcActive || revocationPending) {
                enabled = false;
                window.axonosAttributionContext = '';
            }
            synchronizePublicContext();
            return data;
        }).catch(function (error) {
            if (!requestIsCurrent(generation, sequence, 'status')) {
                return { stale: true };
            }
            if (isAbortError(error) && !timedOut) return { stale: true };
            throw error;
        }).then(function (result) {
            if (timer) clearTimeout(timer);
            if (activeStatusController === controller) activeStatusController = null;
            return result;
        }, function (error) {
            if (timer) clearTimeout(timer);
            if (activeStatusController === controller) activeStatusController = null;
            throw error;
        });
    }

    function update(action) {
        var suppressing = action === 'revoke' || action === 'decline';
        if (!suppressing) {
            var newlyLatchedGpc = false;
            try {
                if (navigator.globalPrivacyControl === true && !gpcActive) {
                    latchSuppression(true, true);
                    newlyLatchedGpc = true;
                }
            } catch (e) { }
            // A detached/stale dialog callback can never override a local GPC
            // or an in-flight/retryable withdrawal.
            if (gpcActive || revocationPending) {
                if (newlyLatchedGpc) scheduleSuppressionRequest();
                return Promise.resolve({ stale: true, suppressed: true });
            }
            if (updateInFlight ||
                (action === 'grant' && (state !== 'unset' ||
                    !landingClickEligible || !landingClickBoundToCurrentLifecycle)) ||
                (action === 'new_lifecycle' && (!newLifecycleAvailable ||
                    !landingClickEligible))) {
                return Promise.resolve({ stale: true });
            }
        }
        if (suppressing) latchSuppression(false, true);
        else invalidateAsyncRequests();
        var generation = stateGeneration;
        var sequence = ++updateSequence;
        var controller = null;
        try {
            controller = typeof AbortController === 'function'
                ? new AbortController() : null;
        } catch (e) { controller = null; }
        activeUpdateController = controller;
        activeUpdateSequence = sequence;
        updateInFlight = true;
        var timedOut = false;
        var timer = controller ? setTimeout(function () {
            timedOut = true;
            abortRequest(controller);
        }, REQUEST_TIMEOUT_MS) : null;
        var requestHeaders = privateRequestHeaders(true);
        requestHeaders['Content-Type'] = 'application/json';
        if ((action === 'grant' || action === 'new_lifecycle') &&
            landingClickEligible && landingClick &&
            !gpcActive && !revocationPending) {
            requestHeaders['X-AxonOS-Landing-Click'] = landingClick;
        }

        var updateRequest;
        try {
            updateRequest = fetch('/api/x-attribution/consent', {
                method: 'POST', credentials: 'same-origin', headers: requestHeaders,
                cache: 'no-store', referrerPolicy: 'no-referrer',
                body: JSON.stringify({ action: action }),
                signal: controller ? controller.signal : undefined
            });
        } catch (error) {
            updateRequest = Promise.reject(error);
        }
        return Promise.resolve(updateRequest).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!requestIsCurrent(generation, sequence, 'update')) {
                    return { stale: true };
                }
                var authoritativeGpc = serverRequiresGpcSuppression(data) ||
                    browserGpcIsActive();
                if (!response.ok || !data.ok) {
                    if (authoritativeGpc) {
                        latchSuppression(true, true);
                        installPrivacyControl(true);
                        if (!suppressing) scheduleSuppressionRequest();
                        return { stale: true, authoritativeGpc: true };
                    }
                    throw new Error('consent update unavailable');
                }

                var responseState = String(data.state || state);
                var responseContext = String(data.context || '').trim();
                var responseCsrf = String(data.csrf || '').trim();
                var validPair = OPAQUE_CONTEXT_RE.test(responseContext) &&
                    CSRF_RE.test(responseCsrf);
                var confirmedRevoke = responseState === 'revoked' &&
                    (action === 'revoke' || authoritativeGpc);
                var expectedState =
                    (action === 'grant' && responseState === 'granted') ||
                    (action === 'decline' && responseState === 'denied') ||
                    (action === 'new_lifecycle' && responseState === 'unset') ||
                    confirmedRevoke;
                if (!validPair || !expectedState) {
                    if (authoritativeGpc) {
                        latchSuppression(true, true);
                        installPrivacyControl(true);
                        if (!suppressing) scheduleSuppressionRequest();
                        return { stale: true, authoritativeGpc: true };
                    }
                    throw new Error('invalid consent response');
                }
                if (action === 'new_lifecycle') clearBindWalletLatch();
                state = responseState;
                applyServerMetadata(data);
                if (action === 'new_lifecycle') {
                    landingClickBoundToCurrentLifecycle = true;
                }

                if (validPair && action !== 'revoke') {
                    if (authoritativeGpc) context = responseContext;
                    else saveContext(responseContext);
                    csrf = responseCsrf;
                    revokeOnlyLifecycle = false;
                    if (action === 'grant') {
                        rememberRevocationCapability(responseContext, responseCsrf);
                    }
                }
                if (authoritativeGpc) latchSuppression(true, false);

                if (confirmedRevoke) {
                    clearRevocationCapabilityAfterConfirmedRevoke();
                    revocationAvailable = false;
                    setRevocationPending(false);
                    saveContext(responseContext);
                    if (validPair) csrf = responseCsrf;
                    discardLandingClick();
                } else if (action === 'decline' && responseState === 'denied') {
                    clearBindWalletLatch();
                    setRevocationPending(false);
                    discardLandingClick();
                } else if (action === 'grant' && responseState === 'granted') {
                    if (validPair) rememberRevocationCapability(responseContext, responseCsrf);
                    discardLandingClick();
                }

                // Even a successful GPC decline/revoke remains locally
                // suppressed; only the retry marker is cleared on confirmation.
                if (gpcActive) {
                    enabled = false;
                    window.axonosAttributionContext = '';
                }
                synchronizePublicContext();
                if (authoritativeGpc && !suppressing) scheduleSuppressionRequest();
                return data;
            });
        }).catch(function (error) {
            if (!requestIsCurrent(generation, sequence, 'update')) {
                return { stale: true };
            }
            if (isAbortError(error) && !timedOut) return { stale: true };
            throw error;
        }).then(function (result) {
            if (timer) clearTimeout(timer);
            if (activeUpdateSequence === sequence) {
                activeUpdateController = null;
                activeUpdateSequence = 0;
                updateInFlight = false;
            }
            return result;
        }, function (error) {
            if (timer) clearTimeout(timer);
            if (activeUpdateSequence === sequence) {
                activeUpdateController = null;
                activeUpdateSequence = 0;
                updateInFlight = false;
            }
            throw error;
        });
    }

    function removeDialog() {
        var old = document.getElementById('axonos-x-consent');
        if (old && old.parentNode) old.parentNode.removeChild(old);
    }

    function makeButton(label, primary, onClick) {
        var button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.style.cssText = 'min-width:9rem;padding:.7rem 1rem;border-radius:.45rem;border:1px solid #8fa2b7;' +
            (primary ? 'background:#f6f8fa;color:#111827;' : 'background:#171d25;color:#f6f8fa;');
        button.addEventListener('click', onClick);
        return button;
    }

    function retentionCopy() {
        if (attributionExpiresAt) {
            try {
                var expiryDate = new Date(attributionExpiresAt * 1000);
                if (Number.isFinite(expiryDate.getTime())) {
                    return 'until ' + expiryDate.toLocaleString() +
                        ', without extending that deadline';
                }
            } catch (e) { }
        }
        if (attributionTtlDays) {
            return 'for at most ' + attributionTtlDays +
                (attributionTtlDays === 1 ? ' day' : ' days') + ', without renewal';
        }
        return 'for a limited, non-renewable attribution period';
    }

    function dialogError(message) {
        var error = document.getElementById('axonos-x-consent-error');
        if (!error) return;
        error.textContent = message;
        error.style.display = 'block';
        var buttons = document.querySelectorAll('#axonos-x-consent button');
        Array.prototype.forEach.call(buttons, function (button) { button.disabled = false; });
    }

    function submitChoice(action, failureMessage) {
        var buttons = document.querySelectorAll('#axonos-x-consent button');
        Array.prototype.forEach.call(buttons, function (button) { button.disabled = true; });
        return update(action).then(function (result) {
            if (!result || result.stale !== true) removeDialog();
            return result;
        }).catch(function () {
            synchronizePublicContext();
            if (action === 'revoke' || action === 'decline') showChoices(false);
            dialogError(failureMessage);
        });
    }

    function showChoices(hasClick) {
        removeDialog();
        // Snapshot only eligibility, never a mutable/global identifier. The
        // server already sealed the module-private landing click into the ticket.
        var dialogHasLandingClick = hasClick === true && landingClickEligible && !!landingClick;
        var root = document.createElement('div');
        root.id = 'axonos-x-consent';
        root.setAttribute('role', 'dialog');
        root.setAttribute('aria-modal', 'true');
        root.setAttribute('aria-labelledby', 'axonos-x-consent-title');
        root.style.cssText = 'position:fixed;inset:0;z-index:2147483000;background:rgba(4,7,12,.82);display:flex;align-items:center;justify-content:center;padding:1rem;';
        var panel = document.createElement('div');
        panel.style.cssText = 'max-width:36rem;background:#10151c;color:#f6f8fa;border:1px solid #526172;border-radius:.75rem;padding:1.4rem;font:16px/1.45 system-ui,sans-serif;';
        var title = document.createElement('h2');
        title.id = 'axonos-x-consent-title';
        var canChooseCurrent = dialogHasLandingClick &&
            landingClickBoundToCurrentLifecycle && enabled && state === 'unset' &&
            !gpcActive && !revocationPending;
        var canStartNewLifecycle = dialogHasLandingClick && enabled &&
            newLifecycleAvailable && !gpcActive && !revocationPending;
        title.textContent = (state === 'granted' || revocationPending || canStartNewLifecycle)
            ? 'Conversion sharing choices' : 'Share approved conversions with X?';
        title.style.marginTop = '0';
        var copy = document.createElement('p');
        copy.textContent = revocationPending
            ? 'Sharing is blocked in this tab, but AxonOS has not yet confirmed deletion of the stored click ID and cancellation of unsent conversions.'
            : (state === 'granted'
                ? 'Sharing is currently allowed for this tab. Revoking deletes the stored click ID and cancels unsent conversions. An event already sent or in flight may not be recallable.'
                : (canStartNewLifecycle
                    ? 'A previous attribution lifecycle in this tab is closed. Starting a new choice first retires that lifecycle; it does not reuse its click, consent time, wallet binding, or expiry.'
                    : (canChooseCurrent
                        ? 'If you allow, AxonOS will store this ad-click identifier ' + retentionCopy() + ' and its server may share only wallet verification, completed paid deposit, and newly started session conversions with X. AxonOS remains fully usable if you decline.'
                        : (revocationAvailable
                            ? 'Conversion sharing is off. You can still remove any stored click ID for this tab and cancel unsent conversions.'
                            : 'No X ad-click identifier is available in this tab. AxonOS will not manufacture or substitute another identifier.'))));
        var error = document.createElement('p');
        error.id = 'axonos-x-consent-error';
        error.setAttribute('role', 'alert');
        error.style.cssText = 'display:none;color:#ffb4ab;font-weight:600;';
        var actions = document.createElement('div');
        actions.style.cssText = 'display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1.2rem;';
        if (canChooseCurrent) {
            actions.appendChild(makeButton('Decline', false, function () {
                submitChoice('decline',
                    'Your decline could not be recorded. Sharing remains blocked; retry when the service is available.');
            }));
            actions.appendChild(makeButton('Allow sharing', true, function () {
                submitChoice('grant',
                    'Your choice could not be saved. No conversion sharing was enabled; you may retry.');
            }));
        } else if (canStartNewLifecycle) {
            actions.appendChild(makeButton('Ignore this click', false, function () {
                discardLandingClick();
                removeDialog();
            }));
            actions.appendChild(makeButton('Start a new choice', true, function () {
                var buttons = document.querySelectorAll('#axonos-x-consent button');
                Array.prototype.forEach.call(buttons, function (button) {
                    button.disabled = true;
                });
                update('new_lifecycle').then(function (result) {
                    if (result && result.stale === true) return;
                    showChoices(true);
                }).catch(function () {
                    dialogError('A new attribution choice could not be started. The previous lifecycle remains closed.');
                });
            }));
        } else if (state === 'granted' || revocationAvailable || revocationPending) {
            actions.appendChild(makeButton(
                revocationPending ? 'Retry revocation' : 'Revoke sharing', false,
                function () {
                    submitChoice('revoke',
                        'Revocation could not be confirmed. Sharing remains blocked in this tab; retry when the service is available.');
                }
            ));
            if (!revocationPending && !gpcActive) {
                actions.appendChild(makeButton('Keep sharing', true, removeDialog));
            }
        } else {
            actions.appendChild(makeButton('Close', true, removeDialog));
        }
        panel.appendChild(title);
        panel.appendChild(copy);
        panel.appendChild(error);
        panel.appendChild(actions);
        root.appendChild(panel);
        document.body.appendChild(root);
        var first = actions.querySelector('button');
        if (first) first.focus();
    }

    function installPrivacyControl(force) {
        if ((!force && !enabled && !revocationAvailable) ||
            document.getElementById('axonos-x-privacy-control')) return;
        if (!document.body) {
            document.addEventListener('DOMContentLoaded', function () {
                installPrivacyControl(force);
            }, { once: true });
            return;
        }
        var button = makeButton(
            revocationPending ? 'Conversion privacy action needed' : 'Conversion sharing',
            false,
            function () { showChoices(landingClickEligible && !!landingClick); }
        );
        button.id = 'axonos-x-privacy-control';
        button.style.cssText += 'position:fixed;left:.75rem;bottom:.75rem;z-index:2147482000;opacity:.92;min-width:auto;font-size:.78rem;';
        document.body.appendChild(button);
    }

    function initializeAttribution() {
        var bootGeneration = ++attributionBootGeneration;
        invalidateAsyncRequests();
        try {
            if (navigator.globalPrivacyControl === true && !gpcActive) {
                gpcActive = true;
                discardLandingClick();
            }
        } catch (e) { }

        var hasNetworkLifecycle = !!(
            landingClickEligible || context || revokeCapability.context
        );
        if (!hasNetworkLifecycle) {
            // Ordinary visitors do not create an attribution ticket, touch the
            // attribution limiter, or perform CAPI-related network I/O. GPC is
            // locally authoritative even when there is nothing to revoke.
            state = gpcActive ? 'denied' : 'unavailable';
            enabled = false;
            ownershipEstablished = true;
            window.axonosAttributionContext = '';
            window.axonosAttributionReady = true;
            if (revocationPending) installPrivacyControl(true);
            return;
        }

        if (gpcActive) {
            // Persist withdrawal before awaiting locks or the network.
            discardLandingClick();
            setRevocationPending(true);
            window.axonosAttributionContext = '';
        }
        contextOwnershipReady().then(function () {
            if (bootGeneration !== attributionBootGeneration) return { stale: true };
            // Ownership arbitration may have discarded a copied, non-revocable
            // context. Recheck laziness after that asynchronous decision.
            if (!landingClickEligible && !context && !revokeCapability.context) {
                state = gpcActive ? 'denied' : 'unavailable';
                enabled = false;
                window.axonosAttributionContext = '';
                window.axonosAttributionReady = true;
                return { idle: true };
            }
            return requestStatus();
        }).then(function (result) {
            if (bootGeneration !== attributionBootGeneration ||
                (result && result.stale === true)) return;
            installPrivacyControl(revocationPending || gpcActive);
            if (revocationPending) {
                scheduleSuppressionRequest();
                return;
            }
            if (!enabled && !revocationAvailable) {
                discardLandingClick();
                synchronizePublicContext();
                return;
            }
            if (landingClickEligible && enabled &&
                ((state === 'unset' && landingClickBoundToCurrentLifecycle) ||
                    newLifecycleAvailable) && !gpcActive) {
                showChoices(true);
            } else if (state !== 'unset' && !newLifecycleAvailable) {
                discardLandingClick();
            }
        }).catch(function () {
            if (bootGeneration !== attributionBootGeneration) return;
            // Fail closed for sharing, never for app access. A local revoke
            // capability keeps a visible retry path even when status is down.
            discardLandingClick();
            window.axonosAttributionContext = '';
            window.axonosAttributionReady = true;
            if (gpcActive || revocationPending || revokeCapability.context) {
                if (gpcActive) setRevocationPending(true);
                revocationAvailable = !!revokeCapability.context;
                installPrivacyControl(true);
            }
        });
    }

    document.addEventListener('DOMContentLoaded', initializeAttribution);
    window.addEventListener('pagehide', function () {
        attributionBootGeneration += 1;
        invalidateAsyncRequests();
        persistentContextSafe = false;
        ownershipEstablished = false;
        window.axonosAttributionContext = '';
    });
    window.addEventListener('pageshow', function (event) {
        if (recheckGlobalPrivacyControl()) return;
        if (event && event.persisted === true) initializeAttribution();
        else maybeBindVerifiedWallet();
    });
    window.addEventListener('online', function () {
        if (!recheckGlobalPrivacyControl()) maybeBindVerifiedWallet();
    });
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') {
            if (!recheckGlobalPrivacyControl()) maybeBindVerifiedWallet();
        }
    });
})();
