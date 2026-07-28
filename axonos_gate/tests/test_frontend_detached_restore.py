import unittest
from pathlib import Path


class FrontendSessionSemanticsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        cls.page_source = (repo / "novnc-theme" / "vnc.html").read_text(
            encoding="utf-8"
        )
        cls.ui_source = (repo / "novnc-theme" / "ui.js").read_text(
            encoding="utf-8"
        )

    def _page_between(self, start: str, end: str) -> str:
        self.assertIn(start, self.page_source)
        self.assertIn(end, self.page_source)
        return self.page_source.split(start, 1)[1].split(end, 1)[0]

    def test_detach_keeps_jobs_and_billing_alive_across_tab_close(self) -> None:
        detach = self.ui_source.split("async detach()", 1)[1].split(
            "async restartDesktopSession()", 1
        )[0]

        self.assertIn("desktop and jobs keep running", detach)
        self.assertIn("prepaid minutes keep counting", detach)
        self.assertIn("even if you close this tab", detach)
        self.assertNotIn("desktop pauses shortly", detach)
        self.assertNotIn("closing the tab then pauses", self.page_source)
        self.assertIn("jobs and compute billing continue, even if this tab closes", self.page_source)
        self.assertIn("Closing an attached tab requests session end", self.page_source)

    def test_credit_exhaustion_copy_describes_logical_top_up_grace(self) -> None:
        self.assertIn("Credit exhausted · 2h top-up grace", self.ui_source)
        self.assertIn("Jobs are still running; compute billing and viewer access have stopped", self.ui_source)
        self.assertIn("Credit exhausted · 2h top-up grace", self.page_source)
        self.assertIn("Jobs still running · compute billing and viewer access stopped", self.page_source)
        self.assertNotIn("Session paused — usage credit exhausted", self.ui_source)
        self.assertNotIn("Compute billing paused", self.page_source)
        self.assertNotIn("Docker-paused", self.page_source + self.ui_source)
        self.assertNotIn("freezes the container", self.page_source + self.ui_source)

    def test_credit_grace_api_is_preferred_with_paused_alias_fallback(self) -> None:
        helper = self._page_between(
            "function axonosCreditGraceActive(data)",
            "function axonosSessionProfileContextFromPayload(payload)",
        )
        apply_payload = self._page_between(
            "window.axonosApplyCreditGraceResumeFromPayload = function (payload)",
            "function axonosSetLaunchConnectButtonCopy(mode)",
        )
        status_apply = self._page_between(
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
        )
        dashboard = self._page_between(
            "function axonosOwnedDashboardSessions(status, wallet)",
            "function axonosScheduleDashboardRefresh(delayMs)",
        )

        self.assertIn("data.credit_grace === true", helper)
        self.assertIn("data.paused === true", helper)
        for preferred, compatibility in (
            ("credit_grace_requested_profile", "paused_requested_profile"),
            ("credit_grace_assigned_gpu_ids", "paused_assigned_gpu_ids"),
            ("credit_grace_gpu_count", "paused_gpu_count"),
            ("credit_grace_remaining_seconds", "paused_resume_seconds"),
            ("credit_grace_session_id", "paused_session_id"),
        ):
            combined = apply_payload + status_apply + dashboard
            self.assertIn(preferred, combined)
            self.assertIn(compatibility, combined)
            self.assertLess(combined.index(preferred), combined.index(compatibility))

        self.assertIn("credit_grace_ssh_enabled", dashboard)
        self.assertIn("dashboard_state: 'credit_grace'", dashboard)
        self.assertIn(
            "window.axonosApplyPausedResumeFromPayload = "
            "window.axonosApplyCreditGraceResumeFromPayload",
            apply_payload,
        )

    def test_eligible_dashboard_refill_is_one_click_and_never_starts_a_new_session(self) -> None:
        eligibility = self._page_between(
            "function axonosSyncTestCreditControls()",
            "function axonosPaymentIdentityIsCurrent(wallet)",
        )
        dashboard_action = self._page_between(
            "const dashTopupBtn = document.getElementById('axonos_dashboard_topup_btn')",
            "// Telemetry Sidebar Collapse Toggle",
        )
        success = self._page_between(
            "function axonosDepositVerifiedSuccess(d, wallet, operation, options)",
            "var AXONOS_VERIFY_DEPOSIT_ENDPOINT",
        )

        self.assertIn("Add up to ", eligibility)
        self.assertIn("test_credit_grant_minutes", self.page_source)
        self.assertIn("test_credit_max_balance_minutes", self.page_source)
        self.assertIn("if (window.axonosTestCreditEligible)", dashboard_action)
        self.assertIn("axonosRequestTestCredit('eth', null", dashboard_action)
        self.assertIn("resumeCreditGraceOnly: true", dashboard_action)
        self.assertIn("axonosStartWizard()", dashboard_action)
        self.assertIn("if (successOptions.resumeCreditGraceOnly)", success)
        self.assertIn("passiveWalletPreflight: successOptions.passiveWalletPreflight", success)
        self.assertIn("passiveWalletPreflight: true", self.page_source)
        self.assertIn("resumeDiscoveredAfterCredit", success)
        self.assertIn("Number(resumeHintSource.sessionId)", success)
        self.assertIn("shouldResumeRetainedSession", success)
        self.assertLess(
            success.index("if (successOptions.resumeCreditGraceOnly)"),
            success.index("claimSession().then"),
        )

    def test_resume_claim_is_bound_to_the_retained_session(self) -> None:
        page_claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )
        ui_claim = self.ui_source.split("_axonosFetchSessionClaim(options)", 1)[1].split(
            "_axonosReleaseSessionHeaders()", 1
        )[0]
        for source in (page_claim, ui_claim):
            self.assertIn("payload.resume_only = true", source)
            self.assertIn("payload.expected_session_id", source)
            self.assertIn("invalid_resume_request: true", source)
        resume_flow = self._page_between(
            "function axonosTryResumeDesktopAfterCredit(paymentOperation, expectedSessionIdOverride, options)",
            "window.axonosResumeDesktopConnectIfPaused = function (options)",
        )
        self.assertIn("claim.resume_expired === true", resume_flow)
        self.assertIn("Your new credits remain available; launch a new session", resume_flow)
        self.assertIn("window.axonosPendingResumeClaim", resume_flow)
        self.assertIn("expectedSessionId: expectedResumeSessionId", resume_flow)
        self.assertIn("preclaimedResumeAtConnectStart", self.ui_source)
        self.assertIn("Promise.resolve(preclaimedResumeAtConnectStart.claim)", self.ui_source)

    def test_resume_marker_survives_non_authoritative_status_and_wallet_failures(self) -> None:
        status = self._page_between(
            "function sessionStatusForWallet(wallet)",
            "function axonosSyncTestCreditControls()",
        )
        refresh = self._page_between(
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
            "function axonosProbePausedResumeOnLoad()",
        )
        preflight = self._page_between(
            "window.axonosEnsureWalletSessionCurrent = function (opts)",
            "// HUD wallet controls",
        )

        self.assertIn("_axonosHttpOk", status)
        self.assertIn("axonosSessionStatusIsAuthoritative", refresh)
        self.assertLess(
            refresh.index("axonosSessionStatusIsAuthoritative"),
            refresh.index("axonosApplySessionStatusToResumeUi"),
        )
        self.assertIn("axonosWalletProviderRequest", preflight)
        self.assertIn("account_mismatch", preflight)
        self.assertEqual(preflight.count("teardownSessionForWalletChange();"), 1)
        self.assertEqual(preflight.count("clearWalletIdentityAndUi();"), 1)

    def test_claim_and_post_claim_config_reads_are_bounded(self) -> None:
        page_claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )
        claim_deadline = self._page_between(
            "function axonosSessionClaimTimeoutMs(resumeRequested)",
            "window.axonosSessionClaimTimeoutMs = axonosSessionClaimTimeoutMs",
        )
        ui_fetch = self.ui_source.split("_axonosFetchJsonWithTimeout(url", 1)[1].split(
            "/** POST /api/session/claim", 1
        )[0]
        ui_claim = self.ui_source.split("_axonosFetchSessionClaim(options)", 1)[1].split(
            "_axonosReleaseSessionHeaders()", 1
        )[0]
        ui_connect = self.ui_source.split("connect(event, password)", 1)[1].split(
            "disconnect(options)", 1
        )[0]

        self.assertIn("if (resumeRequested) return 20000", claim_deadline)
        self.assertIn("150000", claim_deadline)
        self.assertIn("session_claim_timeout_seconds", claim_deadline)
        self.assertIn("session_launcher_timeout_seconds", claim_deadline)
        self.assertIn("timeoutMs: axonosSessionClaimTimeoutMs(resumeRequested)", page_claim)
        self.assertIn("resumeRequested ? 20000 : 150000", ui_claim)
        self.assertIn("AbortController", ui_fetch)
        self.assertIn("response.text()", ui_fetch)
        self.assertIn("UI._axonosFetchJsonWithTimeout(url", ui_claim)
        self.assertIn("'./api/config'", ui_connect)
        self.assertIn("UI._axonosFetchJsonWithTimeout(", ui_connect)

    def test_uncertain_claims_are_reconciled_without_releasing_sessions(self) -> None:
        recovery = self._page_between(
            "function axonosReconcileUncertainSessionClaim(options)",
            "window.axonosReconcileUncertainSessionClaim = axonosReconcileUncertainSessionClaim",
        )
        ui_connect = self.ui_source.split("connect(event, password)", 1)[1].split(
            "disconnect(options)", 1
        )[0]
        run_verify = self._page_between(
            "function runVerify(walletAddress, provider)",
            "var axonosPaymentOperationGeneration = 0",
        )
        wizard = self._page_between(
            "function axonosTriggerLaunchFromWizard()",
            "// Stepper click binding for unconnected wallet connect",
        )

        self.assertIn("sessionStatusForWallet(wallet)", recovery)
        self.assertIn("axonosSessionStatusIsAuthoritative(status)", recovery)
        self.assertIn("axonosCreditGraceActive(status)", recovery)
        self.assertIn("axonosOwnedActiveSessionFromStatus(status, wallet)", recovery)
        self.assertIn("retryDelays = [1000, 3000, 6000]", recovery)
        self.assertGreaterEqual(recovery.count("sessionStatusForWallet(wallet)"), 1)
        self.assertIn("readRecoverableStatus()", recovery)
        self.assertIn("window.axonosSessionDetached = true", recovery)
        self.assertNotIn("/api/session/release", recovery)
        self.assertIn("if (!connectAttemptIsCurrent())", ui_connect)
        self.assertIn("if (granted)", ui_connect)
        self.assertGreaterEqual(
            ui_connect.count("UI._axonosReconcileUncertainSessionClaim({"),
            2,
        )
        self.assertIn(".catch(function (error)", run_verify)
        self.assertIn("axonosReconcileUncertainSessionClaim({", run_verify)
        self.assertIn(".catch(function (error)", wizard)
        self.assertIn("axonosReconcileUncertainSessionClaim({", wizard)
        self.assertIn("retained session was not released", self.page_source)
        self.assertIn("running session exists and may be using credits", self.page_source)

    def test_wallet_verification_has_timeouts_cancellation_and_stale_attempt_guards(self) -> None:
        helpers = self._page_between(
            "var axonosWalletConnectGeneration = 0",
            "async function requestConnectedWallet(provider)",
        )
        verify = self._page_between(
            "function runVerify(walletAddress, provider)",
            "var axonosPaymentOperationGeneration = 0",
        )
        observer = self._page_between(
            "var hasActiveVerifiedSession =",
            "// Hide loader and branding when connection is established",
        )

        self.assertIn("axonosPromiseWithTimeout", helpers)
        self.assertIn("AbortController", helpers)
        self.assertIn("axonosInvalidateWalletVerification", verify)
        self.assertGreaterEqual(verify.count("axonosWalletVerifyFetch"), 2)
        self.assertGreaterEqual(verify.count("axonosWalletProviderRequest"), 2)
        self.assertIn("_axonosStaleWalletAttempt", verify)
        self.assertIn("axonosPromiseWithTimeout(", observer)
        self.assertIn("session status unavailable", observer)

    def test_credit_exhaustion_warning_remains_readable(self) -> None:
        warning = "Credit exhausted · 2h top-up grace. Jobs are still running"
        self.assertIn(warning, self.ui_source)
        self.assertGreaterEqual(self.ui_source.count("'warn',\n                12000"), 2)


if __name__ == "__main__":
    unittest.main()
