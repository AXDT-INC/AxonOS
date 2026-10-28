"""Synthetic frozen-contract conformance; no service or external requests."""
import hashlib
import hmac
import json
import pathlib
import tempfile
import unittest
import uuid
from unittest import mock

from axonos_gate import x_capi_handoff as h

CONTRACT = pathlib.Path(__file__).resolve().parents[2] / 'contracts/x-capi-handoff/v2'
GOLDEN = json.loads((CONTRACT / 'fixtures/golden.json').read_text())
MAC = json.loads((CONTRACT / 'vectors/mac.json').read_text())
GENERATION = GOLDEN['fixture_generation']


def request(op, **extra):
    return dict(v=2, epoch=42, generation=GENERATION, request_id=str(uuid.uuid4()), op=op, **extra)


class FrozenSchemaTests(unittest.TestCase):
    def test_embedded_schemas_are_byte_equivalent_documents(self):
        files = sorted((CONTRACT / 'schemas').glob('*.json'))
        self.assertEqual(len(files), 36)
        self.assertEqual(set(h._SCHEMAS), {f.name for f in files})
        for path in files:
            self.assertEqual(h._SCHEMAS[path.name], json.loads(path.read_text()), path.name)

    def test_every_golden_schema_fixture(self):
        for case in GOLDEN['schema_cases']:
            with self.subTest(case=case['id']):
                name = pathlib.Path(case['schema']).name
                if case['expected_valid']:
                    h.validate_schema(name, case['value'])
                else:
                    with self.assertRaises(h.HandoffError):
                        h.validate_schema(name, case['value'])

    def test_unknown_schema_keywords_fail_not_ignore(self):
        with self.assertRaises(RuntimeError):
            h._schema_supported({'type': 'string', 'format': 'ignored'})

    def test_closed_schema_rejects_bool_integer_and_newline_key(self):
        body = dict(MAC['request']['body'])
        for changes in ({'epoch': True}, {'v': True}, {'request_id': body['request_id'] + '\n'}, {'unknown': 1}):
            with self.subTest(changes=changes), self.assertRaises(h.HandoffError):
                h.validate_schema('source-request', dict(body, **changes))
        with self.assertRaises(ValueError):
            h.ServiceKeyring('test-1\n', {'test-1\n': bytes(range(32))})

    def test_strict_json_and_canonical_browser_order(self):
        for raw in (b'{"v":2,"v":2}', b'{"x":{"y":1,"y":2}}', b'{"v":2.0}', b'{"v":2e0}', b'{"v":-0}', b'{"v":NaN}', b'{"v":9007199254740992}', b'{"v":"\\u00e9"}', b'\xef\xbb\xbf{}', b'[' * 25 + b']' * 25):
            with self.subTest(raw=raw), self.assertRaises(h.HandoffError):
                h.strict_json_loads(raw)
        self.assertEqual(h.strict_json_loads(b'{ "b": 2, "a": 1 }'), {'a': 1, 'b': 2})
        with self.assertRaises(h.HandoffError):
            h.strict_json_loads(b'{ "b": 2, "a": 1 }', canonical=True)
        self.assertEqual(h.canonical_json({'b': 2, 'a': 1}), b'{"a":1,"b":2}')

    def test_token_encoding_cookie_and_domain_separation(self):
        token = GOLDEN['identifiers']['A']
        self.assertEqual(h.token_hash(token, 'app-owner'), GOLDEN['identifiers']['app_owner_hash'])
        self.assertNotEqual(h.token_hash(token, 'app-owner'), h.token_hash(token, 'landing-owner'))
        self.assertEqual(h.parse_owner_cookie('other=x; ' + h.APP_COOKIE + '=' + token), token)
        self.assertIsNone(h.parse_owner_cookie(None))
        self.assertIsNone(h.parse_owner_cookie('other=x'))
        for bad in (h.APP_COOKIE + '=' + token + '; ' + h.APP_COOKIE + '=' + token,
                    h.APP_COOKIE + '=' + token + '\n', h.APP_COOKIE + '="' + token + '"',
                    h.APP_COOKIE + '=' + token + '; other=x, ' + h.APP_COOKIE + '=' + token,
                    h.APP_COOKIE + '=' + token + '=', h.APP_COOKIE + '=' + token[:-1] + '9'):
            with self.subTest(cookie=bad), self.assertRaises(h.HandoffError):
                h.parse_owner_cookie(bad)
        generated = h.new_token()
        self.assertEqual(len(h.b64url_decode(generated, length=32)), 32)


class CryptographicTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes.fromhex(MAC['key_hex'])
        self.keys = h.ServiceKeyring(MAC['key_id'], {MAC['key_id']: self.key})
        self.raw = MAC['request']['canonical_body_ascii'].encode('ascii')

    def test_exact_request_response_mac_and_tamper_negatives(self):
        kid = MAC['key_id']
        self.assertEqual(self.keys.request_mac(self.raw, kid), MAC['request']['mac_base64url'])
        self.assertEqual(self.keys.verify_request(self.raw, kid, MAC['request']['mac_base64url']), MAC['request']['body'])
        response_raw = MAC['response']['canonical_body_ascii'].encode('ascii')
        self.assertEqual(self.keys.response_mac(self.raw, 200, response_raw, kid), MAC['response']['mac_base64url'])
        self.assertEqual(self.keys.verify_response(self.raw, 200, response_raw, kid, MAC['response']['mac_base64url']), MAC['response']['body'])
        for group in ('negative_cases', 'response_negative_cases'):
            for vector in MAC[group]:
                actual = h.b64url_encode(hmac.new(self.key, vector['mac_input_ascii'].encode('ascii'), hashlib.sha256).digest())
                self.assertEqual(actual, vector['computed_mac_base64url'])
                self.assertNotEqual(actual, vector['supplied_mac_base64url'])
        for raw, kid_value, signature in ((self.raw.replace(b'withdrawn', b'gpc'), kid, MAC['request']['mac_base64url']),
                                          (self.raw, kid + '\n', MAC['request']['mac_base64url']),
                                          (self.raw, kid, MAC['response']['mac_base64url'])):
            with self.assertRaises(h.HandoffError):
                self.keys.verify_request(raw, kid_value, signature)
        with self.assertRaises(h.HandoffError):
            self.keys.verify_response(self.raw, 410, response_raw, kid, MAC['response']['mac_base64url'])

    def test_receipt_commitment_and_encoding_frozen(self):
        vector = json.loads((CONTRACT / 'vectors/receipt.json').read_text())
        self.assertEqual(h.canonical_json(vector['observation']).decode(), vector['canonical_observation_ascii'])
        self.assertEqual(h.b64url_encode(h.canonical_json(vector['observation'])), vector['receipt']['payload'])
        self.assertEqual(hmac.new(self.key, vector['click_commitment_input_ascii'].encode(), hashlib.sha256).hexdigest(), vector['click_commitment_hex'])
        self.assertEqual(h.b64url_encode(hmac.new(self.key, vector['mac_input_ascii'].encode(), hashlib.sha256).digest()), vector['receipt']['mac'])
        with self.assertRaises(h.HandoffError):
            h.b64url_decode(vector['receipt']['mac'] + '=')

    def test_bounded_keyring_file_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'keys.json'
            item = {'kid': 'test-1', 'key_base64url': h.b64url_encode(self.key)}
            path.write_text(json.dumps({'primary_kid': 'test-1', 'keys': [item]}))
            path.chmod(0o600)
            self.assertEqual(h.ServiceKeyring.from_file(str(path)).keys['test-1'], self.key)
            path.write_text(json.dumps({'primary_kid': 'test-1', 'keys': [item, item]}))
            with self.assertRaises(ValueError):
                h.ServiceKeyring.from_file(str(path))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                h.ServiceKeyring.from_file(str(path))


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = 100000
        self.lease = h.FreshnessLease(42, GENERATION, elapsed_ms=lambda: self.now)

    def activate(self, version=1):
        challenge_request = request('lease_challenge')
        challenge = self.lease.challenge(challenge_request)
        attestation = request('lease_attest', worker_boot=challenge['worker_boot'], challenge=challenge['challenge'], control_version=version, ready=True)
        return challenge_request, attestation

    def test_high_water_is_monitored_without_ledger_eviction(self):
        self.assertEqual(self.lease.ledger_status()['count'], 0)
        self.lease._seen_challenges.update({str(i): 'synthetic' for i in range(h.LEASE_LEDGER_HIGH_WATER)})
        self.assertTrue(self.lease.ledger_status()['recycle_required'])
        before = self.lease.worker_boot
        self.lease.retire_boot()
        self.assertEqual(self.lease.worker_boot, before)
        self.assertEqual(self.lease.ledger_status()['count'], h.LEASE_LEDGER_HIGH_WATER)
        with self.assertRaises(h.HandoffError):
            self.lease.challenge(request('lease_challenge'))
        self.assertFalse(self.lease.allows_dispatch())

    def test_delay_duplicate_and_strict_original_expiry(self):
        challenge_request, attestation = self.activate()
        self.assertFalse(self.lease.allows_dispatch())
        self.now += 12000
        self.assertEqual(self.lease.attest(attestation)['remaining_ms'], 18000)
        self.now += 10000
        self.assertEqual(self.lease.attest(attestation)['remaining_ms'], 8000)
        self.assertTrue(self.lease.allows_dispatch())
        self.now += 8000
        self.assertFalse(self.lease.allows_dispatch())
        with self.assertRaises(h.HandoffError):
            self.lease.attest(attestation)
        with self.assertRaises(h.HandoffError):
            self.lease.challenge(challenge_request)

    def test_expired_challenge_delayed_response_never_activates(self):
        _, attestation = self.activate()
        self.now += 30000
        with self.assertRaises(h.HandoffError):
            self.lease.attest(attestation)
        self.assertFalse(self.lease.allows_dispatch())

    def test_replaced_challenge_and_replay_cannot_get_new_deadline(self):
        challenge_request, attestation = self.activate()
        self.lease.challenge(request('lease_challenge'))
        for call in (lambda: self.lease.challenge(challenge_request), lambda: self.lease.attest(attestation)):
            with self.assertRaises(h.HandoffError):
                call()

    def test_new_challenge_does_not_destroy_active_lease(self):
        _, attestation = self.activate()
        self.lease.attest(attestation)
        self.now += 10000
        self.lease.challenge(request('lease_challenge'))
        self.assertEqual(self.lease.remaining_ms(), 20000)
        self.now += 20000
        self.assertFalse(self.lease.allows_dispatch())

    def test_restart_boot_and_control_version_regression(self):
        _, attestation = self.activate(version=9)
        self.lease.attest(attestation)
        new = h.FreshnessLease(42, GENERATION, elapsed_ms=lambda: self.now)
        with self.assertRaises(h.HandoffError):
            new.attest(attestation)
        with self.assertRaises(h.HandoffError):
            self.lease.attest(dict(attestation, control_version=8))
        self.assertFalse(self.lease.allows_dispatch())

    def test_durable_high_water_closes_lease(self):
        _, attestation = self.activate(version=8)
        with self.assertRaises(h.HandoffError):
            self.lease.attest(attestation, durable_control_version=9)
        self.assertFalse(self.lease.allows_dispatch())

    def test_suspend_elapsed_and_clock_failure(self):
        _, attestation = self.activate()
        self.lease.attest(attestation)
        self.now += 3600000
        self.assertFalse(self.lease.allows_dispatch())
        self.now = 1
        self.assertFalse(self.lease.allows_dispatch())
        self.assertIsNone(self.lease._active)

    def test_invalidation_preserves_replay_tombstone(self):
        old, attestation = self.activate()
        self.lease.attest(attestation)
        self.lease.invalidate()
        with self.assertRaises(h.HandoffError):
            self.lease.challenge(old)
        self.assertFalse(self.lease.allows_dispatch())


class ClockAssuranceTests(unittest.TestCase):
    def test_kernel_flags_error_bounds_and_unavailable(self):
        for sample, expected in [((0, 0, 5000000, 1), True), ((5, 0, 1, 1), False),
                                 ((0, 0x40, 1, 1), False), ((0, 0x1000, 1, 1), False),
                                 ((0, 0, 5000001, 1), False), ((0, 0, 1, -1), False)]:
            clock = h.LinuxClockAssurance(sample=lambda: sample, wall_ms=lambda: 10000, elapsed_ms=lambda: 1000)
            self.assertEqual(clock(), expected)
        clock = h.LinuxClockAssurance(sample=lambda: (_ for _ in ()).throw(OSError('unavailable')))
        self.assertFalse(clock())

    def test_step_is_sticky_but_suspend_advances_both_clocks(self):
        wall, elapsed = [10000], [1000]
        clock = h.LinuxClockAssurance(sample=lambda: (0, 0, 1, 1), wall_ms=lambda: wall[0], elapsed_ms=lambda: elapsed[0])
        self.assertTrue(clock())
        wall[0] += 3600000
        elapsed[0] += 3600000
        self.assertTrue(clock())
        wall[0] += 5001
        self.assertFalse(clock())
        wall[0] -= 5001
        self.assertFalse(clock())


if __name__ == '__main__':
    unittest.main()
