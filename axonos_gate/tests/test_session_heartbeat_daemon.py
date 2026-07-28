import inspect
import unittest


class SessionHeartbeatDaemonContractTests(unittest.TestCase):
    def test_desktop_runtime_does_not_disable_container_heartbeat(self):
        from axonos_gate import session_heartbeat_daemon

        source = inspect.getsource(session_heartbeat_daemon.main)
        self.assertNotIn("_desktop_enabled", source)
        self.assertIn("_send_heartbeat()", source)

    def test_runtime_heartbeat_uses_session_scoped_credential(self):
        from axonos_gate import session_heartbeat_daemon

        source = inspect.getsource(session_heartbeat_daemon._send_heartbeat)
        self.assertIn('"X-AXGT-Session-Key": FILES_KEY', source)
        self.assertIn('GATE + "/api/session/heartbeat"', source)


if __name__ == "__main__":
    unittest.main()
