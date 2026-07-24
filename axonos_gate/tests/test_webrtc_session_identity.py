import unittest
from unittest.mock import MagicMock, patch


class WebrtcSessionIdentityTests(unittest.TestCase):
    wallet = "0x1234567890123456789012345678901234567890"

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

        with patch.object(session_manager, "_multi_session_enabled", return_value=True), \
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
            with self.subTest(rows=rows):
                conn = MagicMock()
                cur = MagicMock()
                cur.__enter__.return_value = cur
                cur.fetchall.return_value = rows
                conn.cursor.return_value = cur
                with patch.object(session_manager, "_multi_session_enabled", return_value=False), \
                     patch.object(session_manager, "_init_once", return_value=True), \
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

    def test_agent_validator_requires_exact_scoped_row_and_constant_time_key_check(self):
        from axonos_gate import session_manager

        conn, cur = self._connection((73, self.wallet, "session-secret"))
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager.time, "time", return_value=1000.0), \
             patch.object(
                 session_manager.secrets,
                 "compare_digest",
                 wraps=session_manager.secrets.compare_digest,
             ) as compare_digest:
            result = session_manager.validate_webrtc_agent_identity(
                " 73 ", f"  {self.wallet.upper()}  ", " session-secret "
            )

        self.assertEqual(result, {"id": 73, "wallet_address": self.wallet})
        self.assertNotIn("files_key", result)
        compare_digest.assert_called_once_with("session-secret", "session-secret")
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

    def test_agent_validator_rejects_wrong_secret(self):
        from axonos_gate import session_manager

        conn, _cur = self._connection((73, self.wallet, "session-secret"))
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn):
            result = session_manager.validate_webrtc_agent_identity(
                73, self.wallet, "wrong-secret"
            )

        self.assertIsNone(result)
        conn.close.assert_called_once_with()

    def test_agent_validator_rejects_mismatched_returned_identity(self):
        from axonos_gate import session_manager

        other_wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        conn, _cur = self._connection((74, other_wallet, "session-secret"))
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager.secrets, "compare_digest") as compare_digest:
            result = session_manager.validate_webrtc_agent_identity(
                73, self.wallet, "session-secret"
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
                    invalid_id, self.wallet, "session-secret"
                )
                self.assertIsNone(result)
                init.assert_not_called()
                get_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
