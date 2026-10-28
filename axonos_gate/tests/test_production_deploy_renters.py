"""Explicit renter preservation admission; synthetic metadata only."""
import copy
import json
import types
import unittest
from unittest import mock
from axonos_gate.tests.test_production_deploy_preparation import deploy


class RenterPreservationTests(unittest.TestCase):
    def setUp(self):
        self.c = deploy.Deployment.__new__(deploy.Deployment)
        self.c.options = types.SimpleNamespace(preserve_session=['549', '555'])
        self.c.mode = 'off'
        self.names = ['axgt-session-549', 'axgt-session-555']
        self.rows = [[str(i) * 64, '/' + name, 'sha256:' + 'a' * 64,
                      True, False, False, 'start', 0,
                      [{'Name': 'renter-volume', 'Destination': '/home'},
                       {'Name': 'renter-cache', 'Destination': '/cache'}], [{'DeviceIDs': [str(i)]}],
                      [], '', {'renter': {'NetworkID': str(i)}}]
                     for i, name in enumerate(self.names, 1)]
        def docker(*args, **kwargs):
            if args[0] == 'ps':
                return '\n'.join(json.dumps({'Names': n}) for n in self.names)
            self.assertEqual(args[:2], ('inspect', '--format'))
            self.assertNotIn('Config.Env', args[2])
            self.assertEqual(list(args[3:]), self.names)
            return '\n'.join(json.dumps(row) for row in self.rows)
        self.c.docker = mock.Mock(side_effect=docker)

    def test_repeated_checks_preserve_identity(self):
        self.c.active_sessions()
        self.c.active_sessions()

    def test_default_still_refuses_active_renters(self):
        self.c.options.preserve_session = []
        with self.assertRaises(deploy.Refusal):
            self.c.active_sessions()
        self.assertEqual(self.c.docker.call_count, 1)

    def test_mount_order_is_not_a_runtime_change(self):
        self.c.active_sessions()
        self.rows[0][8].reverse()
        self.c.active_sessions()

    def test_unexpected_missing_or_duplicate_sessions_refused(self):
        for ids in (['549'], ['549', '555', '556'], ['549', '549'], ['invalid']):
            self.c.options.preserve_session = ids
            with self.assertRaises(deploy.Refusal):
                self.c.active_sessions()

    def test_requires_off(self):
        self.c.mode = 'live'
        with self.assertRaises(deploy.Refusal):
            self.c.active_sessions()

    def test_runtime_mutations_refused(self):
        self.c.active_sessions()
        original = copy.deepcopy(self.rows)
        for index, value in ((0, 'f' * 64), (2, 'sha256:' + 'b' * 64),
                             (3, False), (4, True), (5, True), (6, 'restarted'),
                             (7, 1), (8, []), (9, []), (10, ['/dev/new']),
                             (11, 'host'), (12, {})):
            with self.subTest(field=index):
                self.rows = copy.deepcopy(original)
                self.rows[0][index] = value
                with self.assertRaises(deploy.Refusal):
                    self.c.active_sessions()


if __name__ == '__main__':
    unittest.main()
