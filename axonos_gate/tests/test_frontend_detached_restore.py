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

    def _ui_between(self, start: str, end: str) -> str:
        self.assertIn(start, self.ui_source)
        self.assertIn(end, self.ui_source)
        return self.ui_source.split(start, 1)[1].split(end, 1)[0]

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

    def test_eligible_balance_card_refill_is_one_click_and_never_starts_a_new_session(self) -> None:
        eligibility = self._page_between(
            "function axonosSyncTestCreditControls()",
            "function axonosPaymentIdentityIsCurrent(wallet)",
        )
        refill = self._page_between(
            "function axonosRequestBalanceCardTestCredit()",
            "const dashTopupBtn = document.getElementById('axonos_dashboard_topup_btn')",
        )
        balance_actions = self._page_between(
            "const dashTopupBtn = document.getElementById('axonos_dashboard_topup_btn')",
            "// Live Telemetry Loop when Connected",
        )
        success = self._page_between(
            "function axonosDepositVerifiedSuccess(d, wallet, operation, options)",
            "var AXONOS_VERIFY_DEPOSIT_ENDPOINT",
        )

        self.assertIn("Add up to ", eligibility)
        self.assertIn("axonos_dashboard_topup_btn", eligibility)
        self.assertIn("axonos_sidebar_topup_btn", eligibility)
        self.assertIn("test_credit_grant_minutes", self.page_source)
        self.assertIn("test_credit_max_balance_minutes", self.page_source)
        self.assertIn("axonosRequestTestCredit('eth', null", refill)
        self.assertIn("resumeCreditGraceOnly: true", refill)
        self.assertGreaterEqual(balance_actions.count("if (window.axonosTestCreditEligible)"), 2)
        self.assertGreaterEqual(balance_actions.count("axonosRequestBalanceCardTestCredit()"), 2)
        self.assertIn("axonosStartWizard()", balance_actions)
        self.assertIn("window.axonosOpenWalletTopUpDialog(true)", balance_actions)
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

    def test_desktop_top_up_never_reuses_or_waits_forever_on_verifying_state(self) -> None:
        top_up = self._page_between(
            "window.axonosOpenWalletTopUpDialog = function (forcePayment)",
            "function setWalletUIState(state, data)",
        )

        self.assertIn("showPaymentState('Refreshing credit balance…')", top_up)
        self.assertIn("axonosFetchWalletAccessStatus(wallet)", top_up)
        self.assertLess(
            top_up.index("showPaymentState('Refreshing credit balance…')"),
            top_up.index("axonosFetchWalletAccessStatus(wallet)"),
        )
        self.assertIn("res.unavailable || res.ok === false", top_up)
        self.assertGreaterEqual(top_up.count("Credit status unavailable"), 2)
        self.assertNotIn("fetch(url.toString()", top_up)

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
        self.assertIn("onWalletAccountsChanged(accounts, eth)", preflight)
        self.assertIn("axonosMarkWalletProviderOutOfSync", preflight)
        self.assertNotIn("teardownSessionForWalletChange", preflight)
        self.assertNotIn("clearWalletIdentityAndUi", preflight)

    def test_wallet_identity_cleanup_clears_all_browser_owned_state(self) -> None:
        cleanup = self._page_between(
            "function clearWalletIdentityAndUi()",
            "function axonosDesktopSessionLive()",
        )

        for state_reset in (
            "window.verifiedWalletAddress = null",
            "window.verifiedWalletAuthToken = null",
            "window.axonosOwnedSession = null",
            "window.axonosDetachedSession = null",
            "window.axonosSessionDetached = false",
            "window.axonosPendingResumeClaim = null",
            "window.axonosPausedResume = null",
            "window.axonosSshEnabled = false",
        ):
            self.assertIn(state_reset, cleanup)
        self.assertIn("localStorage.removeItem('axonos_last_wallet')", cleanup)
        self.assertIn("clearInterval(UI._axgtStatusPollId)", cleanup)
        self.assertIn("UI._axgtStatusPollId = null", cleanup)
        self.assertIn("UI._axgtStopSessionTimer()", cleanup)
        self.assertIn("UI.hideAxonosSshCard()", cleanup)

    def test_explicit_wallet_sign_out_releases_before_identity_cleanup(self) -> None:
        sign_out = self._page_between(
            "window.axonosDisconnectWalletSession = function ()",
            "window.axonosBeginWalletSwitch = function (opts)",
        )

        release = "teardownSessionForWalletChange({ forceRelease: true });"
        clear = "clearWalletIdentityAndUi();"
        self.assertIn(release, sign_out)
        self.assertIn(clear, sign_out)
        self.assertLess(sign_out.index(release), sign_out.index(clear))

    def test_wallet_account_events_are_provider_aware_and_fail_closed(self) -> None:
        account_change = self._page_between(
            "function onWalletAccountsChanged(accounts, sourceProvider)",
            "// AUTHORITATIVE preflight:",
        )
        idle_check = self._page_between(
            "function axonosConfirmWalletIdentityIdle(wallet)",
            "function teardownSessionForWalletChange(options)",
        )
        binding = self._page_between(
            "function bindWalletAccountEvents(provider)",
            "bindWalletAccountEvents(getSafeWindowEthereum());",
        )

        self.assertIn("sourceProvider !== activeProvider", account_change)
        self.assertIn("window.axonosWalletAccountEventGeneration", account_change)
        self.assertIn("window.axonosPendingWalletAccountEvent", account_change)
        self.assertIn("axonosConfirmWalletIdentityIdle(oldAccount)", account_change)
        self.assertIn("clearWalletIdentityAndUi();", account_change)
        self.assertIn("window.axonosBeginWalletSwitch({", account_change)
        self.assertIn("useExposedAccount: true", account_change)
        self.assertNotIn("teardownSessionForWalletChange", account_change)
        self.assertIn("sessionStatusForWallet(wallet)", idle_check)
        self.assertIn("axonosSessionStatusIsAuthoritative(status)", idle_check)
        self.assertIn("onWalletAccountsChanged(accounts, provider)", binding)

    def test_page_claim_runs_passive_wallet_preflight_before_posting(self) -> None:
        claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )

        preflight = "window.axonosEnsureWalletSessionCurrent({ requestPermission: false })"
        payload = "const payload = { wallet_address: wallet };"
        self.assertIn(preflight, claim)
        self.assertIn("walletPreflightDone: true", claim)
        self.assertIn("wallet_preflight_failed: true", claim)
        self.assertIn(payload, claim)
        self.assertLess(claim.index(preflight), claim.index(payload))

    def test_authoritative_ssh_claim_never_falls_through_to_desktop_connect(self) -> None:
        route = self._page_between(
            "function tryConnectAfterClaim(claim)",
            "const connectButton = document.getElementById('noVNC_connect_button');",
        )

        remember = "window.axonosRememberOwnedSession(claim);"
        ssh_branch = "if (granted && claim.ssh_enabled === true)"
        desktop_loader = "showConnectionLoader();"
        desktop_click = "launchBtn.click()"
        self.assertIn(remember, route)
        self.assertIn(ssh_branch, route)
        self.assertIn("if (claim.ssh_port)", route)
        self.assertIn("axonosRestoreSshSessionUi(Object.assign({}, claim", route)
        self.assertIn("owner_hard_cap_remaining_seconds", route)
        self.assertIn("SSH session is running, but its connection endpoint is unavailable", route)
        self.assertGreaterEqual(route.count("return;"), 2)
        self.assertIn(desktop_loader, route)
        self.assertIn(desktop_click, route)
        self.assertLess(route.index(remember), route.index(ssh_branch))
        self.assertLess(route.index(ssh_branch), route.index(desktop_loader))
        self.assertNotIn("UI.axonosSshEnabled()", route)

    def test_ssh_restore_and_card_render_do_not_restart_poll_or_screen(self) -> None:
        restore = self._page_between(
            "function axonosRestoreSshSessionUi(st)",
            "window.axonosRestoreSshSessionUi = axonosRestoreSshSessionUi;",
        )
        card = self._ui_between(
            "showAxonosSshCard(claim, options = {})",
            "_axonosUpdateSshCardCap(payload)",
        )
        ui_connect = self._ui_between(
            "connect(event, password)",
            "disconnect(options)",
        )

        self.assertEqual(restore.count("UI._axgtStartSessionBillingPoll();"), 1)
        self.assertIn("!UI._axgtStatusPollId", restore)
        self.assertNotIn("axonosUpdateActiveScreen('landing')", restore)
        self.assertIn("landingAlreadyActive", card)
        self.assertIn("!landingAlreadyActive", card)
        self.assertIn("!preserveScreen", card)
        ssh_connect = ui_connect.split(
            "if (claim && claim.ssh_enabled === true)", 1
        )[1].split("if (claim && claim.resumed === true", 1)[0]
        self.assertIn("UI.openAxonosSshTerminal(claim)", ssh_connect)
        terminal_open = self._ui_between(
            "async openAxonosSshTerminal(claim, options = {})",
            "showAxonosTemplateDetails(t)",
        )
        self.assertIn("if (!UI._axgtStatusPollId)", terminal_open)

    def test_periodic_wallet_status_ignores_stale_identity_responses(self) -> None:
        poll = self._ui_between(
            "    _axgtPollWalletStatus() {",
            "    _axgtSetupUsageOverlayButton() {",
        )

        self.assertIn("const pollIdentityIsCurrent", poll)
        self.assertGreaterEqual(poll.count("if (!pollIdentityIsCurrent()) return;"), 2)

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
        self.assertLess(
            wizard.index("showConnectionLoader('preparing')"),
            wizard.index("axonosFetchWalletAccessStatus(wallet)"),
        )
        preflight = wizard.split("axonosFetchWalletAccessStatus(wallet)", 1)[0]
        self.assertNotIn("axonosTestCreditEligible", preflight)
        self.assertNotIn("is_whitelisted", preflight)
        self.assertIn("res.unavailable || res.ok === false", wizard)
        self.assertIn("hideConnectionLoader();", wizard)
        self.assertIn("Unable to check wallet credits", wizard)
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
