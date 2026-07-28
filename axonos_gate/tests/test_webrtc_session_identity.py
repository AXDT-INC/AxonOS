import unittest
from unittest.mock import MagicMock, call, patch


class WebrtcSessionIdentityTests(unittest.TestCase):
    wallet = "0x1234567890123456789012345678901234567890"
    signing_secret = "central-test-signing-secret-with-sufficient-entropy"

    @staticmethod
    def _connection(row):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = row
        conn.cursor.return_value = cur
        return conn, cur

    def test_resolver_returns_minimal_normalized_active_desktop_identity(self):
        from axonos_gate import session_manager

        conn, cur = self._connection((73, self.wallet.upper()))
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.get_active_desktop_session_for_wallet(
                f"  {self.wallet.upper()}  "
            )

        self.assertEqual(result, {"id": 73, "wallet_address": self.wallet})
        self.assertNotIn("files_key", result)
        sql, params = cur.execute.call_args.args
        normalized_sql = " ".join(sql.split()).lower()
        self.assertIn("status = 'active'", normalized_sql)
        self.assertIn("allocation_status = 'allocated'", normalized_sql)
        self.assertIn("ssh_enabled = false", normalized_sql)
        self.assertIn("expires_at > %s", normalized_sql)
        self.assertEqual(params, (self.wallet, 1000.0))
        conn.close.assert_called_once_with()

    def test_resolver_returns_none_when_no_eligible_row_exists(self):
        from axonos_gate import session_manager

        conn, _cur = self._connection(None)
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn):
            result = session_manager.get_active_desktop_session_for_wallet(self.wallet)

        self.assertIsNone(result)
        conn.close.assert_called_once_with()

    def test_resolver_rejects_empty_wallet_without_database_access(self):
        from axonos_gate import session_manager

        with patch.object(session_manager, "_init_once") as init, \
             patch.object(session_manager, "_get_connection") as get_connection:
            result = session_manager.get_active_desktop_session_for_wallet("  ")

        self.assertIsNone(result)
        init.assert_not_called()
        get_connection.assert_not_called()

    def test_legacy_singleton_resolver_is_disabled_in_multi_session_mode(self):
        from axonos_gate import session_manager

        with patch.dict(
            "os.environ",
            {"AXGT_USER_CONTAINER_ENABLED": "true"},
            clear=True,
        ), \
             patch.object(session_manager, "_init_once") as init:
            result = session_manager.get_single_active_desktop_session()

        self.assertIsNone(result)
        init.assert_not_called()

    def test_legacy_singleton_resolver_requires_exactly_one_active_desktop(self):
        from axonos_gate import session_manager

        for rows, expected in (
            ([(73, self.wallet.upper())], {"id": 73, "wallet_address": self.wallet}),
            ([], None),
            ([(73, self.wallet), (74, self.wallet)], None),
        ):
            with self.subTest(rows=rows), patch.dict(
                "os.environ",
                {"AXGT_USER_CONTAINER_ENABLED": "false"},
                clear=True,
            ):
                conn = MagicMock()
                cur = MagicMock()
                cur.__enter__.return_value = cur
                cur.fetchall.return_value = rows
                conn.cursor.return_value = cur
                with patch.object(session_manager, "_init_once", return_value=True), \
                     patch.object(session_manager, "_get_connection", return_value=conn), \
                     patch.object(session_manager.time, "time", return_value=1000.0):
                    result = session_manager.get_single_active_desktop_session()

                self.assertEqual(result, expected)
                sql, params = cur.execute.call_args.args
                normalized_sql = " ".join(sql.split()).lower()
                self.assertIn("limit 2", normalized_sql)
                self.assertIn("status = 'active'", normalized_sql)
                self.assertEqual(params, (1000.0,))
                conn.close.assert_called_once_with()

    def _issue_capability(self, session_id=73, wallet=None, files_key="session-secret"):
        from axonos_gate.webrtc import capability

        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ):
            issued = capability.issue(session_id, wallet or self.wallet, files_key)
        self.assertIsNotNone(issued)
        return issued

    def test_agent_validator_requires_exact_scoped_row_and_constant_time_checks(self):
        from axonos_gate import session_manager

        issued = self._issue_capability()
        conn, cur = self._connection(
            (
                73,
                self.wallet,
                "session-secret",
                issued["jti_hash"],
                issued["expires_at"],
            )
        )
        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(session_manager.time, "time", return_value=1000.0), patch.object(
            session_manager.secrets,
            "compare_digest",
            wraps=session_manager.secrets.compare_digest,
        ) as compare_digest:
            result = session_manager.validate_webrtc_agent_identity(
                " 73 ", f"  {self.wallet.upper()}  ", f" {issued['token']} "
            )

        self.assertEqual(result, {"id": 73, "wallet_address": self.wallet})
        self.assertNotIn("files_key", result)
        self.assertEqual(compare_digest.call_count, 2)
        self.assertEqual(
            compare_digest.call_args_list[0],
            call(
                issued["files_key_fingerprint"],
                issued["files_key_fingerprint"],
            ),
        )
        self.assertEqual(
            compare_digest.call_args_list[1],
            call(issued["jti_hash"], issued["jti_hash"]),
        )
        sql, params = cur.execute.call_args.args
        normalized_sql = " ".join(sql.split()).lower()
        self.assertIn("id = %s", normalized_sql)
        self.assertIn("wallet_address = %s", normalized_sql)
        self.assertIn("status = 'active'", normalized_sql)
        self.assertIn("allocation_status = 'allocated'", normalized_sql)
        self.assertIn("ssh_enabled = false", normalized_sql)
        self.assertIn("expires_at > %s", normalized_sql)
        self.assertEqual(params, (73, self.wallet, 1000.0))
        conn.close.assert_called_once_with()

    def test_agent_validator_rejects_tampered_token_before_database_access(self):
        from axonos_gate import session_manager

        issued = self._issue_capability()
        header, payload, signature = issued["token"].split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = ".".join((header, payload, signature))
        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ), patch.object(session_manager, "_init_once") as init, patch.object(
            session_manager, "_get_connection"
        ) as get_connection:
            result = session_manager.validate_webrtc_agent_identity(
                73, self.wallet, tampered
            )

        self.assertIsNone(result)
        init.assert_not_called()
        get_connection.assert_not_called()

    def test_agent_validator_rejects_revoked_capability_jti(self):
        from axonos_gate import session_manager

        issued = self._issue_capability()
        conn, _cur = self._connection(
            (
                73,
                self.wallet,
                "session-secret",
                "0" * 64,
                issued["expires_at"],
            )
        )
        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.validate_webrtc_agent_identity(
                73, self.wallet, issued["token"]
            )

        self.assertIsNone(result)
        conn.close.assert_called_once_with()

    def test_agent_validator_rejects_mismatched_returned_identity(self):
        from axonos_gate import session_manager

        issued = self._issue_capability()
        other_wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        conn, _cur = self._connection(
            (
                74,
                other_wallet,
                "session-secret",
                issued["jti_hash"],
                issued["expires_at"],
            )
        )
        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(session_manager.secrets, "compare_digest") as compare_digest:
            result = session_manager.validate_webrtc_agent_identity(
                73, self.wallet, issued["token"]
            )

        self.assertIsNone(result)
        compare_digest.assert_not_called()
        conn.close.assert_called_once_with()

    def test_agent_validator_rejects_non_numeric_ids_before_database_access(self):
        from axonos_gate import session_manager

        invalid_ids = (None, "", "abc", "12.5", "-1", 0, -1, 1.0, True)
        for invalid_id in invalid_ids:
            with self.subTest(session_id=invalid_id), \
                 patch.object(session_manager, "_init_once") as init, \
                 patch.object(session_manager, "_get_connection") as get_connection:
                result = session_manager.validate_webrtc_agent_identity(
                    invalid_id, self.wallet, "not-inspected-for-an-invalid-id"
                )
                self.assertIsNone(result)
                init.assert_not_called()
                get_connection.assert_not_called()

    def test_capability_issuance_failure_never_persists_partial_metadata(self):
        from axonos_gate import session_manager

        for result in (None, RuntimeError("signer unavailable")):
            with self.subTest(result=result):
                codec = MagicMock()
                if isinstance(result, Exception):
                    codec.issue.side_effect = result
                else:
                    codec.issue.return_value = result
                cur = MagicMock()
                with patch.object(
                    session_manager,
                    "_import_webrtc_capability",
                    return_value=codec,
                ):
                    token = session_manager._issue_webrtc_agent_capability(
                        cur,
                        73,
                        self.wallet,
                        "session-secret",
                    )
                self.assertIsNone(token)
                cur.execute.assert_not_called()

    def test_agent_capability_refresh_is_row_locked_and_retry_safe(self):
        from axonos_gate import session_manager

        issued = self._issue_capability()
        conn, cur = self._connection(
            (
                73,
                self.wallet,
                "session-secret",
                issued["jti_hash"],
                issued["expires_at"],
            )
        )
        now = session_manager.time.time()
        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(session_manager.time, "time", return_value=now):
            refreshed = session_manager.refresh_webrtc_agent_capability(
                73,
                self.wallet,
                issued["token"],
            )

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertIsNotNone(
            self._verify_refreshed_token(refreshed["token"])
        )
        first_sql, first_params = cur.execute.call_args_list[0].args
        normalized = " ".join(first_sql.split()).lower()
        self.assertIn("for update", normalized)
        self.assertIn("status = 'credit_grace'", normalized)
        self.assertIn("credit_grace_started_at", normalized)
        self.assertEqual(first_params[0:3], (73, self.wallet, now))
        update_sql, update_params = cur.execute.call_args_list[1].args
        self.assertIn("webrtc_cap_expires_at", update_sql)
        self.assertEqual(update_params[1:], (73, self.wallet, issued["jti_hash"]))
        conn.commit.assert_called_once_with()

    def _verify_refreshed_token(self, token):
        from axonos_gate.webrtc import capability

        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ):
            return capability.verify(token, 73, self.wallet)

    def test_capability_refresh_rejects_tampering_before_database_access(self):
        from axonos_gate import session_manager

        issued = self._issue_capability()
        head, payload, signature = issued["token"].split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = ".".join((head, payload, signature))
        with patch.dict(
            "os.environ",
            {"WEBRTC_AGENT_INTERNAL_KEY": self.signing_secret},
        ), patch.object(session_manager, "_init_once") as init, patch.object(
            session_manager, "_get_connection"
        ) as get_connection:
            result = session_manager.refresh_webrtc_agent_capability(
                73,
                self.wallet,
                tampered,
            )

        self.assertIsNone(result)
        init.assert_not_called()
        get_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
