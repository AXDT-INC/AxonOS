import unittest
import fcntl
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from axonos_gate import security_utils


ROOT = Path(__file__).resolve().parents[2]


class XcapiFrontendPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")
        cls.ui = (ROOT / "novnc-theme" / "ui.js").read_text(encoding="utf-8")
        cls.bridge = (
            ROOT / "novnc-theme" / "app" / "x-attribution.js"
        ).read_text(encoding="utf-8")

    @staticmethod
    def between(source, start, end):
        if start not in source or end not in source:
            raise AssertionError(f"missing source boundary: {start!r} / {end!r}")
        return source.split(start, 1)[1].split(end, 1)[0]

    def test_encoded_and_mixed_case_sensitive_query_names_are_redacted(self):
        cases = (
            "GET /?twcl%69d=wallet-shaped&keep=yes HTTP/1.1",
            "GET /?TWCLID=upper-case&keep=yes HTTP/1.1",
            "GET /?x%5fcapi%5fcontext=opaque&keep=yes HTTP/1.1",
            "GET /?x%255fcapi%255fhandoff=double-encoded&keep=yes HTTP/1.1",
            "GET /?twcl%25252569d=four-level&keep=yes HTTP/1.1",
            "GET /?X%252525255FCAPI%252525255FCONTEXT=deep-mixed&keep=yes HTTP/1.1",
            "GET /?auth%5Ftoken=bearer&keep=yes HTTP/1.1",
            "GET /?%74wclid%ZZ=malformed-prefix&keep=yes HTTP/1.1",
            "GET /?twcl%69d%ZZ=malformed-suffix&keep=yes HTTP/1.1",
            "GET /?prefix_twclid_note=extended-name&keep=yes HTTP/1.1",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                safe = security_utils.redact_terminal_websocket_query(raw)
                self.assertIn("[redacted]", safe)
                self.assertIn("keep=yes", safe)
                for secret in (
                    "wallet-shaped", "upper-case", "opaque",
                    "double-encoded", "four-level", "deep-mixed", "bearer",
                    "malformed-prefix", "malformed-suffix", "extended-name",
                ):
                    self.assertNotIn(f"={secret}", safe)

    def test_query_redaction_does_not_damage_unrelated_fields(self):
        raw = "GET /?twilight=visible&keep=also-visible HTTP/1.1"
        self.assertEqual(security_utils.redact_terminal_websocket_query(raw), raw)

    def test_oversized_query_name_is_redacted_without_percent_decoding(self):
        raw = "GET /?" + ("%25" * 200) + "=attacker-value&keep=yes HTTP/1.1"
        observed_names = []
        original = security_utils._canonical_log_query_name
        with patch.object(
            security_utils, "_canonical_log_query_name",
            side_effect=lambda value: (
                observed_names.append(value) or original(value)
            ),
        ):
            safe = security_utils.redact_terminal_websocket_query(raw)
        self.assertNotIn("attacker-value", safe)
        self.assertIn("[redacted]", safe)
        self.assertIn("keep=yes", safe)
        self.assertTrue(observed_names)
        self.assertTrue(all(len(name) <= 256 for name in observed_names))

    def test_best_effort_rate_limiter_has_a_hard_bucket_bound(self):
        limiter = security_utils.SimpleRateLimiter(2, 10, max_buckets=2)
        with patch.object(security_utils.time, "time", return_value=100.0):
            self.assertTrue(limiter.allow("one"))
            self.assertTrue(limiter.allow("two"))
            self.assertFalse(limiter.allow("three"))
            self.assertLessEqual(len(limiter._buckets), 2)
        with patch.object(security_utils.time, "time", return_value=111.0):
            self.assertTrue(limiter.allow("three"))
            self.assertLessEqual(len(limiter._buckets), 2)

    def test_shared_limiter_is_cross_instance_bounded_and_fails_on_lock_contention(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            path = os.path.join(temporary, "rate.json")
            env = {"X_CAPI_ALLOW_TEST_RATE_FILE": "1"}
            with patch.dict(os.environ, env, clear=True):
                digest_key = b"unit-test-rate-key-32-bytes!!!!"
                first = security_utils.SharedFileRateLimiter(
                    2, 60, path=path, digest_key=digest_key
                )
                second = security_utils.SharedFileRateLimiter(
                    2, 60, path=path, digest_key=digest_key
                )
                self.assertTrue(first.allow("203.0.113.4"))
                self.assertTrue(second.allow("203.0.113.4"))
                self.assertFalse(first.allow("203.0.113.4"))
                descriptor = os.open(path, os.O_RDWR)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertFalse(second.allow("198.51.100.8"))
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

    def test_forwarded_client_is_used_only_for_an_explicit_trusted_chain(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                security_utils.client_ip_for_rate_limit(
                    "192.0.2.10", "198.51.100.7"
                ),
                "192.0.2.10",
            )
        trusted = {
            "X_CAPI_TRUSTED_PROXY_HOPS": "2",
            "X_CAPI_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
        }
        with patch.dict(os.environ, trusted, clear=True):
            self.assertEqual(
                security_utils.client_ip_for_rate_limit(
                    "192.0.2.10", "198.51.100.7, 192.0.2.11"
                ),
                "198.51.100.7",
            )
            # An untrusted immediate peer cannot opt itself into XFF handling.
            self.assertEqual(
                security_utils.client_ip_for_rate_limit(
                    "203.0.113.9", "198.51.100.7, 192.0.2.11"
                ),
                "203.0.113.9",
            )

    def test_all_server_access_log_paths_apply_redaction_or_disable_raw_access(self):
        gate = (ROOT / "axonos_gate" / "gate_server.py").read_text(encoding="utf-8")
        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(encoding="utf-8")
        self.assertIn('logging.getLogger("werkzeug").addFilter', gate)
        self.assertIn(
            "handler_class = _deadline_websocket_handler(WebSocketHandler, gevent)",
            gate,
        )
        self.assertIn("handler_class=handler_class, log=None", gate)
        self.assertIn("def log_message(self, format, *args):", proxy)
        self.assertIn("redact_terminal_websocket_query(format)", proxy)

    def test_url_scrub_is_canonical_duplicate_safe_and_precedes_resources(self):
        scrub = self.between(
            self.page,
            "// Capture X's click ID only in memory",
            "try {\n                var params = new URLSearchParams",
        )
        self.assertIn("canonicalAttributionKey", scrub)
        self.assertIn("tolerantAsciiQueryName", scrub)
        self.assertIn("rawExactTwclidCount === 1", scrub)
        self.assertIn("!rawConfusedTwclid", scrub)
        self.assertIn("clickCandidates.length === 1", scrub)
        self.assertIn("key === 'twclid'", scrub)
        self.assertIn("attributionParams.delete(name)", scrub)
        self.assertIn("window.location.replace(attributionFallbackClean)", scrub)
        self.assertIn("window.axonosUrlQueryScrubFailed = true", scrub)
        self.assertLess(
            scrub.index("var attributionFallbackClean"),
            scrub.index("new URLSearchParams"),
        )
        self.assertIn("window.location.pathname +\n                (window.location.hash || '')", scrub)
        self.assertIn("walletShaped", scrub)
        self.assertIn("phoneShaped", scrub)
        self.assertIn("secretShaped", scrub)
        self.assertIn("navigator.globalPrivacyControl === true", scrub)
        self.assertLess(self.page.index("canonicalAttributionKey"), self.page.index('<link rel="icon"'))
        self.assertIn('<meta name="referrer" content="no-referrer">', self.page)
        self.assertIn('<script src="app/x-attribution.js?v=4"', self.page)
        self.assertIn(
            "onerror=\"window.axonosPendingTwclid='';"
            "try{delete window.axonosPendingTwclid;}catch(e){}\"",
            self.page,
        )
        self.assertNotIn('<script defer src="app/x-attribution.js', self.page)
        guest_fallback = self.between(
            self.page,
            "function axonosTakeGuestInviteFromUrl()",
            "function axonosStoredGuestSessionIsValid",
        )
        self.assertLess(
            guest_fallback.index("window.axonosUrlQueryScrubFailed === true"),
            guest_fallback.index("new URLSearchParams"),
        )

    def test_gpc_and_pending_revocation_never_expose_context_during_boot(self):
        initial_blank = "window.axonosAttributionContext = '';"
        self.assertLess(self.bridge.index(initial_blank), self.bridge.index("DOMContentLoaded"))
        exposure = self.between(
            self.bridge,
            "function synchronizePublicContext()",
            "function privateRequestHeaders(includeCsrf)",
        )
        self.assertIn("!gpcActive", exposure)
        self.assertIn("!revocationPending", exposure)
        self.assertIn("!revokeOnlyLifecycle", exposure)
        self.assertIn("state === 'granted'", exposure)
        self.assertIn("REVOCATION_PENDING_KEY", self.bridge)
        self.assertIn("setRevocationPending(true)", self.bridge)
        self.assertIn("Sharing is blocked in this tab", self.bridge)
        self.assertIn("data.gpc_applied === true", self.bridge)
        self.assertIn("String(data.state || '') === 'revocation_required'", self.bridge)
        self.assertGreaterEqual(self.bridge.count("browserGpcIsActive();"), 2)
        choices = self.between(
            self.bridge, "function showChoices(hasClick)", "function installPrivacyControl(force)"
        )
        self.assertIn("if (!revocationPending && !gpcActive)", choices)

    def test_dynamic_gpc_is_event_driven_one_way_and_idle_safe(self):
        recheck = self.between(
            self.bridge,
            "function recheckGlobalPrivacyControl()",
            "function scheduleSuppressionRequest()",
        )
        self.assertIn("if (gpcActive || !browserGpcIsActive()) return false", recheck)
        self.assertIn("var hasTrackedLifecycle", recheck)
        self.assertIn("if (!hasTrackedLifecycle)", recheck)
        idle = self.between(recheck, "if (!hasTrackedLifecycle)", "latchSuppression(true, true)")
        self.assertIn("invalidateAsyncRequests()", idle)
        self.assertIn("window.axonosAttributionContext = ''", idle)
        self.assertNotIn("requestStatus()", idle)
        self.assertNotIn("scheduleSuppressionRequest()", idle)
        self.assertIn("latchSuppression(true, true)", recheck)
        self.assertIn("scheduleSuppressionRequest()", recheck)
        self.assertIn("else if (context)", recheck)
        self.assertIn("requestStatus()", recheck)
        self.assertIn("document.visibilityState === 'visible'", self.bridge)
        self.assertIn("window.addEventListener('pageshow'", self.bridge)
        self.assertNotIn("setInterval(", self.bridge)

    def test_failed_revoke_stays_visible_and_does_not_close_dialog(self):
        submit = self.between(
            self.bridge, "function submitChoice(action, failureMessage)",
            "function showChoices(hasClick)"
        )
        self.assertIn("dialogError(failureMessage)", submit)
        update = self.between(
            self.bridge, "function update(action)", "function removeDialog()"
        )
        self.assertIn("if (suppressing) latchSuppression(false, true)", update)
        self.assertNotIn("catch(removeDialog)", self.bridge)
        self.assertIn("role', 'alert", self.bridge)
        self.assertIn("Retry revocation", self.bridge)

    def test_cloned_tab_must_arbitrate_before_context_exposure(self):
        ownership = self.between(
            self.bridge, "function contextOwnershipReady()", "function synchronizePublicContext()"
        )
        self.assertIn("navigator.locks.request", ownership)
        self.assertIn("ifAvailable: true", ownership)
        self.assertIn("abandonClonedContext()", ownership)
        self.assertIn("persistentContextSafe = true", ownership)
        self.assertIn("OWNER_KEY", self.bridge)
        startup = self.bridge.split("document.addEventListener('DOMContentLoaded'", 1)[1]
        self.assertIn("initializeAttribution", startup)
        startup = self.between(
            self.bridge, "function initializeAttribution()", "document.addEventListener"
        )
        self.assertLess(startup.index("contextOwnershipReady()"), startup.index("requestStatus()"))
        self.assertIn("event.persisted === true", self.bridge)

    def test_consent_copy_uses_server_lifetime_not_a_hardcoded_claim(self):
        self.assertIn("data.attribution_ttl_days", self.bridge)
        self.assertIn("data.expires_at", self.bridge)
        self.assertIn("without renewal", self.bridge)
        self.assertNotIn("up to 7 days", self.bridge)

    def test_later_click_is_only_kept_for_unset_or_explicit_new_lifecycle(self):
        startup = self.bridge.split("function initializeAttribution()", 1)[1]
        self.assertIn("state === 'unset'", startup)
        self.assertIn("newLifecycleAvailable", startup)
        self.assertIn("landingClickEligible", startup)
        self.assertIn("var landingClick", self.bridge)
        self.assertIn("function discardLandingClick()", self.bridge)
        self.assertIn("landingClick = '';", self.bridge)
        self.assertIn("delete window.axonosPendingTwclid", self.bridge)
        # The bootstrap handoff is consumed once at module evaluation; no UI or
        # request callback ever re-reads a mutable global click value.
        self.assertEqual(self.bridge.count("window.axonosPendingTwclid"), 3)

    def test_new_lifecycle_requires_a_separate_explicit_transition(self):
        choices = self.between(
            self.bridge, "function showChoices(hasClick)", "function installPrivacyControl(force)"
        )
        self.assertIn("canStartNewLifecycle", choices)
        self.assertIn("Start a new choice", choices)
        self.assertIn("update('new_lifecycle')", choices)
        self.assertIn("it does not reuse its click, consent time", choices)

    def test_landing_click_is_first_party_header_only_and_never_in_grant_body(self):
        status = self.between(
            self.bridge, "function requestStatus()", "function update(action)"
        )
        update = self.between(
            self.bridge, "function update(action)", "function removeDialog()"
        )
        self.assertIn("!usedPrivateContext", status)
        self.assertIn("!landingClickStatusSent", status)
        self.assertIn("var revalidatingLiveUnset", status)
        self.assertIn("state === 'unset'", status)
        self.assertIn("landingClickBoundToCurrentLifecycle", status)
        self.assertIn("X-AxonOS-Landing-Click", status)
        self.assertIn("landingClickStatusSent = true", status)
        self.assertIn("data.landing_click_accepted === true", status)
        self.assertIn("landingClickBoundToCurrentLifecycle", self.bridge)
        self.assertIn("action === 'grant' || action === 'new_lifecycle'", update)
        self.assertIn("X-AxonOS-Landing-Click", update)
        self.assertIn("body: JSON.stringify({ action: action })", update)
        self.assertNotIn("twclid:", update)

    def test_unset_context_is_memory_only_until_a_choice(self):
        save = self.between(
            self.bridge, "function saveContext(value)", "function rotateTabOwner()"
        )
        self.assertIn("state !== 'unset'", save)
        self.assertLess(
            save.index("state !== 'unset'"),
            save.index("sessionStorage.setItem(STORAGE_KEY, context)"),
        )
        transition = self.between(
            self.bridge,
            "if (validPair && action !== 'revoke')",
            "if (authoritativeGpc) latchSuppression",
        )
        remember = self.between(
            transition, "revokeOnlyLifecycle = false", "if (authoritativeGpc)"
        )
        self.assertIn("if (action === 'grant')", remember)
        self.assertNotIn("action === 'new_lifecycle'", remember)
        capability = self.between(
            self.bridge,
            "function rememberRevocationCapability(contextValue, csrfValue)",
            "function clearRevocationCapabilityAfterConfirmedRevoke()",
        )
        self.assertIn("if (state === 'unset') return false", capability)
        status = self.between(
            self.bridge, "function requestStatus()", "function update(action)"
        )
        self.assertIn("discardLegacyUnsetRevocationCapability()", status)

    def test_stale_status_and_updates_are_aborted_and_fenced_before_mutation(self):
        status = self.between(
            self.bridge, "function requestStatus()", "function update(action)"
        )
        update = self.between(
            self.bridge, "function update(action)", "function removeDialog()"
        )
        self.assertIn("new AbortController()", status)
        self.assertIn("new AbortController()", update)
        self.assertIn("requestIsCurrent(generation, sequence, 'status')", status)
        self.assertIn("requestIsCurrent(generation, sequence, 'update')", update)
        self.assertIn("if (gpcActive || revocationPending)", update)
        self.assertLess(
            status.index("requestIsCurrent(generation, sequence, 'status')"),
            status.index("enabled = data.enabled === true"),
        )
        self.assertLess(
            update.index("requestIsCurrent(generation, sequence, 'update')"),
            update.index("state = responseState"),
        )
        self.assertIn("if (!validPair || !expectedState)", update)
        self.assertLess(
            update.index("if (!validPair || !expectedState)"),
            update.index("state = responseState"),
        )
        self.assertIn("invalidateAsyncRequests()", self.bridge)
        self.assertIn("REQUEST_TIMEOUT_MS", self.bridge)
        self.assertIn("result.stale !== true", self.bridge)

    def test_revoke_only_capability_survives_clone_and_clears_only_on_confirmed_revoke(self):
        self.assertIn("REVOCATION_CAPABILITY_KEY", self.bridge)
        self.assertIn("rememberRevocationCapability", self.bridge)
        self.assertIn("revokeCapability.context", self.bridge)
        abandon = self.between(
            self.bridge, "function abandonClonedContext()", "function contextOwnershipReady()"
        )
        self.assertNotIn("clearRevocationCapabilityAfterConfirmedRevoke", abandon)
        self.assertNotIn("setRevocationPending(false)", abandon)
        self.assertEqual(
            self.bridge.count("clearRevocationCapabilityAfterConfirmedRevoke();"), 1
        )
        confirmed = self.between(
            self.bridge,
            "if (confirmedRevoke)",
            "} else if (action === 'decline'",
        )
        self.assertIn("clearRevocationCapabilityAfterConfirmedRevoke()", confirmed)
        public = self.between(
            self.bridge,
            "function synchronizePublicContext()",
            "function privateRequestHeaders(includeCsrf)",
        )
        self.assertNotIn("revokeCapability.context", public)
        private_headers = self.between(
            self.bridge,
            "function privateRequestHeaders(includeCsrf)",
            "function applyServerMetadata(data)",
        )
        self.assertIn("var useOrdinaryContext", private_headers)
        self.assertIn("(!includeCsrf || !!csrf)", private_headers)
        self.assertIn(
            "useOrdinaryContext ? csrf : revokeCapability.csrf",
            private_headers,
        )

    def test_idle_visitors_do_not_create_an_identifier_or_call_status(self):
        startup = self.between(
            self.bridge, "function initializeAttribution()", "document.addEventListener"
        )
        self.assertIn("var hasNetworkLifecycle", startup)
        self.assertIn("if (!hasNetworkLifecycle)", startup)
        self.assertLess(startup.index("if (!hasNetworkLifecycle)"), startup.index("requestStatus()"))
        ownership_boot = self.bridge.split("function randomNonce()", 1)[0]
        self.assertIn("if (needsTabOwner) sessionStorage.setItem(OWNER_KEY", ownership_boot)
        self.assertIn("else sessionStorage.removeItem(OWNER_KEY)", ownership_boot)

    def test_wallet_bind_closes_both_ready_races_without_blocking_auth(self):
        bind = self.between(
            self.bridge, "function maybeBindVerifiedWallet()", "window.axonosNotifyWalletVerified"
        )
        self.assertIn("/api/x-attribution/bind", bind)
        self.assertIn("X-AXGT-Auth-Token", bind)
        self.assertIn("X-AxonOS-Attribution", bind)
        self.assertIn("BIND_TIMEOUT_MS", bind)
        self.assertIn("data.ok === true && data.accepted === true", bind)
        self.assertIn("function () { return false; }", bind)
        self.assertIn("bindAttemptKey = ''", bind)
        self.assertNotIn("X-AxonOS-CSRF", bind)
        self.assertNotIn("landingClick", bind)
        synchronize = self.between(
            self.bridge,
            "function synchronizePublicContext()",
            "function privateRequestHeaders(includeCsrf)",
        )
        self.assertIn("mayExpose || (state === 'granted'", synchronize)
        self.assertIn("maybeBindVerifiedWallet();", synchronize)
        self.assertGreaterEqual(
            self.page.count("window.axonosNotifyWalletVerified("), 3
        )

    def test_only_authorized_business_calls_attach_context(self):
        candidate_helper = self.between(
            self.page,
            "function axonosAttributionContextForWalletRequest(walletAddress)",
            "function axonosFetch(opts)",
        )
        self.assertIn("window.axonosAttributionContextForWalletCandidate", candidate_helper)
        self.assertNotIn("window.axonosAttributionContext ||", candidate_helper)
        generic = self.between(
            self.page, "function axonosFetch(opts)", "function axonosSessionClaimTimeoutMs"
        )
        self.assertIn("opts.includeAttribution === true", generic)
        self.assertIn("axonosAttributionContextForWalletRequest", generic)
        self.assertIn("String(name).toLowerCase() === 'x-axonos-attribution'", generic)
        self.assertNotIn(
            "if (window.axonosAttributionContext) headers['X-AxonOS-Attribution']",
            generic,
        )
        verify = self.between(
            self.page, "function runVerify(walletAddress, provider, options)",
            "// Silent reload restore",
        )
        self.assertLess(
            verify.index("axonosAttributionContextForWalletRequest(walletAddress)"),
            verify.index("new URL('/api/auth/challenge'"),
        )
        self.assertGreaterEqual(
            verify.count("axonosAttributionContextForWalletRequest(walletAddress)"), 2
        )
        self.assertNotIn(
            "verifyHeaders['X-AxonOS-Attribution'] = window.axonosAttributionContext",
            verify,
        )
        deposit = self.between(
            self.page, "function axonosVerifyDepositFetch(txHash, verifyEndpoint, operation)",
            "function axonosDepositVerifyPendingMessage",
        )
        self.assertIn("axonosAttributionContextForWalletRequest(wallet)", deposit)
        self.assertNotIn("window.axonosAttributionContext", deposit)
        claim = self.between(self.page, "function claimSession(options)", "function sessionStatus()")
        self.assertIn(
            "includeAttribution: !resumeRequested && reattachSessionId === null",
            claim,
        )
        restart = self.between(
            self.ui, "async restartDesktopSession()", "_axonosViewerViewOnly()"
        )
        self.assertNotIn("X-AxonOS-Attribution", restart)

    def test_ui_delegates_to_one_attribution_aware_claim_builder(self):
        page_claim = self.between(
            self.page, "function claimSession(options)", "function sessionStatus()"
        )
        ui_claim = self.between(
            self.ui, "_axonosFetchSessionClaim(options)",
            "/** Reconcile an ambiguous claim"
        )
        self.assertIn("window.axonosClaimSession = claimSession", self.page)
        self.assertIn("window.axonosClaimSession(options)", ui_claim)
        self.assertNotIn("/api/session/claim", ui_claim)
        self.assertEqual(page_claim.count("/api/session/claim"), 1)
        self.assertIn("axonosSessionClaimPostInFlight", page_claim)
        self.assertIn("var claimKey = JSON.stringify(payload)", page_claim)
        self.assertIn("axonosSessionClaimPostInFlight.key === claimKey", page_claim)
        self.assertIn("claim_in_progress: true", page_claim)
        self.assertIn("window.axonosPendingSessionClaim", self.page)
        self.assertIn("preclaimedSessionAtConnectStart", self.ui)

    def test_bridge_has_no_external_network_dependency(self):
        fetch_lines = [line for line in self.bridge.splitlines() if "fetch(" in line]
        self.assertTrue(fetch_lines)
        self.assertTrue(all("/api/x-attribution/" in line for line in fetch_lines))
        self.assertNotIn("ads-api.x.com", self.bridge)
        self.assertNotIn("platform.twitter.com", self.bridge)

    def test_bridge_runtime_privacy_transitions(self):
        node = shutil.which("node")
        if not node:
            try:
                import playwright

                candidate = Path(playwright.__file__).resolve().parent / "driver" / "node"
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    node = str(candidate)
            except (ImportError, OSError, TypeError):
                pass
        if not node:
            self.skipTest("Node runtime is unavailable")
        harness = ROOT / "axonos_gate" / "tests" / "x_capi_frontend_runtime.js"
        completed = subprocess.run(
            [
                node,
                str(harness),
                str(ROOT / "novnc-theme" / "app" / "x-attribution.js"),
                str(ROOT / "novnc-theme" / "vnc.html"),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("frontend runtime checks passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
