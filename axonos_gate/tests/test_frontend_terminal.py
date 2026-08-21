import unittest
from pathlib import Path


class FrontendTerminalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        theme = repo / "novnc-theme"
        cls.page = (theme / "vnc.html").read_text(encoding="utf-8")
        cls.ui = (theme / "ui.js").read_text(encoding="utf-8")
        cls.css = (theme / "axonos-theme.css").read_text(encoding="utf-8")
        cls.terminal = (theme / "app/terminal/axonos-terminal.js").read_text(
            encoding="utf-8"
        )
        cls.webrtc = (theme / "app/webrtc/axonos-webrtc.js").read_text(
            encoding="utf-8"
        )
        cls.dockerfile = (repo / "Dockerfile").read_text(encoding="utf-8")

    @staticmethod
    def _between(source: str, start: str, end: str) -> str:
        if start not in source or end not in source:
            raise AssertionError(f"missing source boundary: {start!r} / {end!r}")
        return source.split(start, 1)[1].split(end, 1)[0]

    def test_xterm_assets_are_self_hosted_and_pinned(self) -> None:
        self.assertIn('app/vendor/xterm/xterm.css?v=6.0.0', self.page)
        self.assertIn("from '../vendor/xterm/xterm.mjs'", self.terminal)
        self.assertIn("from '../vendor/xterm/addon-fit.mjs'", self.terminal)
        self.assertIn(
            "COPY novnc-theme/app/terminal/ /usr/share/novnc/app/terminal/",
            self.dockerfile,
        )
        self.assertIn(
            "COPY novnc-theme/app/vendor/xterm/ /usr/share/novnc/app/vendor/xterm/",
            self.dockerfile,
        )
        self.assertNotIn("cdn.jsdelivr", self.page + self.terminal)
        self.assertNotIn("unpkg.com", self.page + self.terminal)

    def test_ticket_exchange_never_puts_wallet_or_auth_token_on_websocket(self) -> None:
        request = self._between(
            self.terminal,
            "async function requestTerminalTicket(wallet, authToken, externalSignal)",
            "function terminalWebSocketUrl(ticketResponse)",
        )
        endpoint = self._between(
            self.terminal,
            "function terminalWebSocketUrl(ticketResponse)",
            "class AxonosXtermRenderer",
        )

        self.assertIn("'/api/terminal/ticket'", request)
        self.assertIn("credentials: 'include'", request)
        self.assertIn("'X-AXGT-Auth-Token'", self.terminal)
        self.assertIn("JSON.stringify({ wallet_address: wallet })", request)
        self.assertIn("url.host !== window.location.host", endpoint)
        self.assertIn("keys.length !== 1", endpoint)
        self.assertIn("keys[0] !== 'ticket'", endpoint)
        self.assertNotIn("wallet", endpoint)
        self.assertNotIn("auth_token", endpoint)

    def test_terminal_protocol_is_bounded_binary_framing_with_stream_accumulation(self) -> None:
        self.assertIn("socket.binaryType = 'arraybuffer'", self.terminal)
        self.assertIn("setUint32(1, body.byteLength, false)", self.terminal)
        self.assertIn("header.getUint32(1, false)", self.terminal)
        self.assertIn("this.receiveBuffer", self.terminal)
        self.assertIn("buffer.slice(offset)", self.terminal)
        self.assertIn("MAX_SERVER_PAYLOAD_BYTES", self.terminal)
        self.assertIn("MAX_RECEIVE_BUFFER_BYTES", self.terminal)
        self.assertIn("MAX_SOCKET_BUFFERED_BYTES", self.terminal)
        for frame_type in ("'I'", "'R'", "'P'", "'C'", "'O'", "'X'", "'E'"):
            self.assertIn(frame_type, self.terminal)

    def test_renderer_mounts_in_shared_viewer_and_tracks_resize_and_focus(self) -> None:
        open_viewer = self._between(
            self.ui,
            "async openAxonosSshTerminal(claim, options = {})",
            "showAxonosTemplateDetails(t)",
        )
        self.assertIn("document.getElementById('noVNC_container')", open_viewer)
        self.assertIn("new ResizeObserver", self.terminal)
        self.assertIn("this.fitAddon.fit()", self.terminal)
        self.assertIn("this.terminal.focus()", self.terminal)
        self.assertIn("#noVNC_container.axonos-terminal-active", self.css)
        self.assertIn(".axonos-terminal-viewer>.xterm", self.css)
        self.assertIn("html.axonos-terminal-active #noVNC_files_button", self.css)

    def test_terminal_has_explicit_state_without_impersonating_rfb(self) -> None:
        open_viewer = self._between(
            self.ui,
            "async openAxonosSshTerminal(claim, options = {})",
            "showAxonosTemplateDetails(t)",
        )
        self.assertIn("connectionKind: null", self.ui)
        self.assertIn("terminalState: 'idle'", self.ui)
        self.assertIn("UI.connectionKind = 'terminal'", open_viewer)
        self.assertIn("UI.terminalState = 'connected'", open_viewer)
        self.assertIn("UI.connected = false", open_viewer)
        self.assertNotIn("UI.connected = true", open_viewer)
        self.assertIn("UI.connectionKind = 'rfb'", self.ui)
        self.assertIn("UI.connectionKind = 'webrtc'", self.webrtc)

    def test_authoritative_ssh_claim_shows_endpoint_before_optional_terminal(self) -> None:
        ui_claim = self._between(
            self.ui,
            "connect(event, password)",
            "disconnect(options)",
        )
        page_route = self._between(
            self.page,
            "function tryConnectAfterClaim(claim)",
            "const connectButton = document.getElementById('noVNC_connect_button');",
        )
        fallback = self._between(
            self.ui,
            "_axonosFallbackToSshCard(claim, error, options = {})",
            "/** Open a granted SSH-only allocation",
        )

        marker = "if (claim && claim.ssh_enabled === true)"
        self.assertIn(marker, ui_claim)
        self.assertIn("UI.showAxonosSshCard(claim)", ui_claim)
        self.assertNotIn("UI.openAxonosSshTerminal(claim)", ui_claim)
        self.assertIn("if (granted && claim.ssh_enabled === true)", page_route)
        self.assertIn("UI.showAxonosSshCard(claim)", page_route)
        self.assertNotIn("UI.openAxonosSshTerminal(claim)", page_route)
        self.assertIn("copy the command or open the web terminal", ui_claim)
        self.assertIn("copy the command or open the web terminal", page_route)
        self.assertIn("UI.showAxonosSshCard", fallback)
        self.assertIn("Web terminal unavailable", fallback)
        self.assertIn("axonos_ssh_connect_cmd", self.page)
        self.assertIn("axonos_ssh_host_fingerprint", self.page)
        self.assertIn("UI._axonosLoadSshHostFingerprint({", self.ui)
        self.assertIn("ssh_host_key_fingerprint", self.ui)
        self.assertIn("do not accept an unverified host key", self.ui)
        self.assertIn("deadline: Date.now() + 60000", self.ui)
        self.assertIn("waiting for verification", self.ui)
        self.assertIn("UI._axonosSshFingerprintRetryTimer = setTimeout", self.ui)
        self.assertIn("generation !== UI._axonosSshFingerprintGeneration", self.ui)
        self.assertIn("axonos_ssh_copy_btn", self.page)
        self.assertIn("axonos_ssh_web_terminal_btn", self.page)

    def test_end_detach_wallet_clear_and_credit_exhaustion_close_terminal(self) -> None:
        disconnect = self._between(
            self.ui,
            "disconnect(options)",
            "/** Final billing heartbeat enters credit grace",
        )
        credit = self._between(
            self.ui,
            "_axgtDisconnectForCreditExhaustion(overlayMessage)",
            "reconnect()",
        )
        cleanup = self._between(
            self.page,
            "function clearWalletIdentityAndUi()",
            "function axonosDesktopSessionLive()",
        )

        self.assertIn("const terminalDisconnect", disconnect)
        self.assertIn("UI._axonosCloseTerminalClient()", disconnect)
        self.assertLess(
            disconnect.index("UI._axonosCloseTerminalClient()"),
            disconnect.index("UI._axonosReleaseSessionBestEffort(releaseContext)"),
        )
        self.assertIn("UI._axonosCloseTerminalClient()", credit)
        self.assertIn("UI._axonosCloseTerminalClient()", cleanup)
        self.assertIn("terminal: terminalDisconnect", disconnect)

    def test_terminal_billing_and_tab_close_preserve_the_ssh_allocation(self) -> None:
        ownership = self._between(
            self.ui,
            "_axonosSessionOwnsServerSlot()",
            "/** Fire-and-forget release for tab close",
        )
        billing = self._between(
            self.ui,
            "_axgtSessionBillingActive()",
            "/** True only on successful wallet-status",
        )
        controls = self._between(
            self.ui,
            "_axonosViewerAttached()",
            "/** @deprecated alias */",
        )

        self.assertIn("UI.connectionKind === 'terminal'", ownership)
        self.assertIn("return false", ownership)
        self.assertIn("UI.connectionKind === 'terminal'", billing)
        self.assertIn("UI.terminalState === 'connected'", billing)
        self.assertIn("const viewerAttached", controls)
        self.assertIn("showDetach = viewerAttached", controls)

    def test_frontend_module_cache_tokens_stay_in_lockstep(self) -> None:
        self.assertIn("axonos-theme.css?v=20.3&t=20260819a", self.page)
        self.assertIn("app/ui.js?v=20260821b", self.page)
        self.assertIn("./webrtc/axonos-webrtc.js?v=20260821b", self.ui)
        self.assertIn("./terminal/axonos-terminal.js?v=20260729d", self.ui)

    def test_terminal_fallback_does_not_start_a_dashboard_status_refresh(self) -> None:
        fallback = self._between(
            self.ui,
            "_axonosFallbackToSshCard(claim, error, options = {})",
            "/** Open a granted SSH-only allocation",
        )
        workspace = self._between(
            self.page,
            "function axonosReturnToWorkspace(options)",
            "window.axonosReturnToWorkspace = axonosReturnToWorkspace;",
        )

        self.assertIn("refresh: false", fallback)
        self.assertIn("if (opts.refresh === false)", workspace)
        self.assertIn("return Promise.resolve(null)", workspace)
        self.assertLess(
            workspace.index("if (opts.refresh === false)"),
            workspace.index("return axonosLoadDashboard()"),
        )

    def test_automatic_terminal_waits_for_wallet_auth_and_retries(self) -> None:
        restore = self._between(
            self.page,
            "function axonosRestoreSshSessionUi(st)",
            "window.axonosRestoreSshSessionUi = axonosRestoreSshSessionUi;",
        )
        terminal_open = self._between(
            self.ui,
            "async openAxonosSshTerminal(claim, options = {})",
            "showAxonosTemplateDetails(t)",
        )
        verified = self._between(
            self.page,
            "function axonosOnWalletVerified(walletAddress, data)",
            "window.axonosOnWalletVerified = axonosOnWalletVerified;",
        )

        self.assertIn("!!window.verifiedWalletAddress", restore)
        self.assertIn("!!window.verifiedWalletAuthToken", restore)
        self.assertIn("terminalAuthReady", restore)
        self.assertIn("deferAxonosSshTerminalUntilAuthenticated", restore)
        self.assertIn(
            "const authToken = String(window.verifiedWalletAuthToken || '').trim()",
            terminal_open,
        )
        self.assertIn("if (!wallet || !authToken)", terminal_open)
        self.assertIn("authToken,", terminal_open)
        self.assertIn("resumePendingAxonosSshTerminal()", verified)

    def test_terminal_fallback_keeps_a_visible_recovery_surface_and_safe_detail(self) -> None:
        fallback = self._between(
            self.ui,
            "_axonosFallbackToSshCard(claim, error, options = {})",
            "/** Open a granted SSH-only allocation",
        )
        detail = self._between(
            self.ui,
            "_axonosTerminalErrorDetail(error)",
            "_axonosFallbackToSshCard(claim, error, options = {})",
        )

        self.assertIn("refresh: false", fallback)
        self.assertIn("UI._axonosSshDashboardActive()", fallback)
        self.assertIn("if (preserveWorkspace", fallback)
        self.assertIn("preserveScreen: preserveWorkspace", fallback)
        self.assertIn("External SSH remains available ${preserveWorkspace", fallback)
        self.assertIn("UI._axonosTerminalErrorDetail(error)", fallback)
        self.assertIn("[redacted]", detail)
        self.assertIn("slice(0, 180)", detail)
        self.assertIn("axonos-ssh-card-active", self.ui)
        self.assertIn(
            ".axonos-state-landing.axonos-ssh-card-active .axonos-connect-wrapper",
            self.css,
        )
        self.assertIn("display: block !important", self.css)

    def test_websocket_close_preserves_server_error_detail(self) -> None:
        connect = self._between(
            self.terminal,
            "    async connect()",
            "    _disposeRenderer()",
        )
        socket_error = self._between(
            connect,
            "socket.addEventListener('error'",
            "socket.addEventListener('close'",
        )

        self.assertIn("this.lastError", self.terminal)
        self.assertNotIn("reject(", socket_error)
        self.assertIn("if (!this.lastError)", socket_error)
        self.assertIn("const closeReason", connect)
        self.assertIn("error: this.lastError", connect)

        exit_frame = self._between(
            self.terminal,
            "if (frame.type === 'X')",
            "if (frame.type === 'E')",
        )
        self.assertIn("!this.disposed", exit_frame)
        self.assertIn("!this.intentionalClose", exit_frame)


if __name__ == "__main__":
    unittest.main()


class FrontendModeSwapContractTests(unittest.TestCase):
    """Sidebar mode-swap button: desktop <-> SSH console via release + re-claim."""

    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        theme = repo / "novnc-theme"
        cls.page = (theme / "vnc.html").read_text(encoding="utf-8")
        cls.ui = (theme / "ui.js").read_text(encoding="utf-8")

    def test_sidebar_swap_button_sits_above_detach(self) -> None:
        controls = self.page.split('class="axonos-sidebar-controls"', 1)[1]
        controls = controls.split("axonos_sidebar_toggle", 1)[0]
        swap_pos = controls.index("axonos_sidebar_swap_btn")
        detach_pos = controls.index("axonos_sidebar_detach_btn")
        end_pos = controls.index("axonos_sidebar_end_btn")
        self.assertLess(swap_pos, detach_pos)
        self.assertLess(detach_pos, end_pos)
        self.assertIn("axonos_sidebar_swap_title", controls)
        self.assertIn("axonos_sidebar_swap_desc", controls)
        self.assertIn("UI.swapSessionMode()", self.page)

    def test_swap_labels_follow_viewer_mode(self) -> None:
        updater = self.ui.split("updateAxonosSwapButton()", 1)[1].split(
            "async swapSessionMode()", 1
        )[0]
        self.assertIn("UI.connectionKind === 'terminal'", updater)
        self.assertIn("Relaunch as Desktop", updater)
        self.assertIn("Relaunch as Console", updater)
        # Refreshed alongside the other session control buttons.
        control_updater = self.ui.split("updateSessionControlButtons() {", 1)[1].split(
            "updateAxonosSwapButton()", 1
        )[0]
        self.assertIn("noVNC_hidden", control_updater)

    def test_swap_sets_intent_then_requires_confirmed_release_before_reclaim(self) -> None:
        swap = self.ui.split("async swapSessionMode()", 1)[1].split(
            "async restartDesktopSession()", 1
        )[0]
        # Intent must be written before the release so both claim builders
        # (ui.js and vnc.html's) read the new mode.
        intent_pos = swap.index("window.axonosSshEnabled = toSsh")
        release_pos = swap.index("await UI.disconnect(")
        self.assertGreater(release_pos, intent_pos)
        # An unconfirmed release must abort the swap (no second session on top
        # of a possibly-still-owned one) and restore the previous intent.
        self.assertIn("if (!released)", swap)
        self.assertIn("window.axonosSshEnabled = previousIntent", swap)
        # The relaunch goes through the single claim/connect choke point.
        self.assertIn("UI.connect()", swap)
        # Desktop -> console requires a usable public key up front.
        self.assertIn("axonosSshKeyLooksValid", swap)
