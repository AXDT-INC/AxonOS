import base64
import hashlib
import json
import os
import stat
import threading
import time
import unittest
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from axonos_gate import security_utils, x_capi, x_capi_worker


ROOT = Path(__file__).resolve().parents[2]


def _provision_test_privacy_controls(directory: Path) -> None:
    """Create the exact fixed control-file shape used by the privacy fence."""
    os.chmod(directory, 0o700)
    controls = (
        (x_capi._PRIVACY_FENCE_LOCK_NAME, x_capi._PRIVACY_FENCE_LOCK_MAGIC),
        (
            x_capi._PRIVACY_FENCE_QUARANTINE_NAME,
            x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
        ),
    )
    for name, contents in controls:
        path = directory / name
        path.write_bytes(contents)
        os.chmod(path, 0o600)
    for index in range(x_capi._PRIVACY_PENDING_SLOT_COUNT):
        path = directory / f"{x_capi._PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
        path.write_bytes(x_capi._PRIVACY_PENDING_EMPTY)
        os.chmod(path, 0o600)


class ConfigTests(unittest.TestCase):
    @staticmethod
    def enabled_env(mode="dry_run", **overrides):
        values = {
            "X_CAPI_MODE": mode,
            "X_CAPI_PIXEL_ID": "source-id",
            "X_CAPI_EVENT_WALLET_VERIFIED": "wallet-event",
            "X_CAPI_EVENT_DEPOSIT_COMPLETED": "deposit-event",
            "X_CAPI_EVENT_SESSION_STARTED": "session-event",
            "X_CAPI_ALLOWED_ORIGIN": "https://app.example",
            "X_CAPI_DEPLOYMENT_ID": "test-deployment",
            "X_CAPI_CONSENT_POLICY_VERSION": "privacy-v1",
            "X_CAPI_CONSENT_POLICY_EPOCH": "1",
            "X_CAPI_ATTRIBUTION_TTL_DAYS": "7",
            "X_CAPI_CONTEXT_KEY": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
            "X_CAPI_ALLOW_TEST_SECRETS": "1",
            "X_CAPI_ALLOW_TEST_CONFIG_GUARD_BYPASS": "1",
        }
        if mode == "live":
            values.update({
                "X_CAPI_TWCLID_CONTRACT_VERSION": "vendor-contract-2026-01",
                "X_CAPI_TWCLID_CHARSET": "lower_alnum",
                "X_CAPI_TWCLID_MIN_LENGTH": "20",
                "X_CAPI_TWCLID_MAX_LENGTH": "64",
            })
        values.update(overrides)
        return values

    def test_default_off_has_no_collection_or_delivery_readiness(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            x_capi, "get_connection"
        ) as connect:
            status = x_capi.attribution_status()
        self.assertEqual(status["mode"], "off")
        self.assertEqual(status["state"], "off")
        self.assertFalse(status["enabled"])
        connect.assert_not_called()

    def test_live_missing_configuration_is_diagnostic_not_ready(self):
        with patch.dict(os.environ, {"X_CAPI_MODE": "live"}, clear=True):
            status = x_capi.config_status()
        self.assertFalse(status["producer_ready"])
        self.assertIn("X_CAPI_PIXEL_ID is required", status["errors"])
        self.assertIn("X_CAPI_ALLOWED_ORIGIN is required", status["errors"])
        self.assertEqual(status["event_mappings"]["deposit_completed"], "disabled")

    def test_active_mode_without_any_event_mapping_collects_nothing(self):
        env = self.enabled_env(
            X_CAPI_EVENT_WALLET_VERIFIED="",
            X_CAPI_EVENT_DEPOSIT_COMPLETED="",
            X_CAPI_EVENT_SESSION_STARTED="",
        )
        click = "officialclick1234567890"
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_send_local_envelope"
        ) as send:
            config = x_capi.load_config()
            status = x_capi.attribution_status(landing_twclid=click)
            emitted = x_capi.emit_event_nonblocking(
                context_token="A" * 64,
                wallet_address="0x" + "1" * 40,
                milestone=x_capi.MILESTONE_WALLET_VERIFIED,
                source_key="0x" + "1" * 40,
                allow_context_binding=True,
            )

        self.assertFalse(config.producer_ready)
        self.assertIn("At least one X_CAPI_EVENT_* mapping is required", config.errors)
        self.assertEqual(status["state"], "unavailable")
        self.assertFalse(status["enabled"])
        self.assertNotIn("context", status)
        self.assertNotIn(click, json.dumps(status))
        self.assertFalse(emitted)
        send.assert_not_called()

    def test_origins_and_vendor_path_ids_are_strict_and_exception_safe(self):
        for origin in (
            "https://é.example",
            "https://example.com:99999",
            "https://[::1",
            "https://exa mple.com",
            "https://example.com\\attacker",
        ):
            self.assertFalse(x_capi._valid_origin(origin))
            self.assertFalse(x_capi.exact_origin_allowed(origin))

        for field in (
            "X_CAPI_PIXEL_ID",
            "X_CAPI_EVENT_WALLET_VERIFIED",
            "X_CAPI_EVENT_DEPOSIT_COMPLETED",
            "X_CAPI_EVENT_SESSION_STARTED",
        ):
            env = self.enabled_env(**{field: ".."})
            with patch.dict(os.environ, env, clear=True):
                self.assertFalse(x_capi.load_config().producer_ready)
            for leaked_value in (
                "0x" + "1" * 40,
                "1" * 40,
                "0x" + "a" * 64,
                "a" * 64,
                "550e8400-e29b-41d4-a716-446655440000",
                "192.0.2.44",
                "2001:db8::44",
                "alice.eth",
                "alice.btc",
                "sk-this-is-accidentally-a-secret",
                "xoxb-this-is-accidentally-a-secret",
            ):
                env = self.enabled_env(**{field: leaked_value})
                with patch.dict(os.environ, env, clear=True):
                    self.assertFalse(x_capi.load_config().producer_ready)

        invalid_live = self.enabled_env("live", X_CAPI_ALLOWED_ORIGIN="https://[::1")
        with patch.dict(os.environ, invalid_live, clear=True):
            cfg = x_capi.load_config()
            status = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        self.assertFalse(cfg.producer_ready)
        self.assertEqual(cfg.allowed_origin, "")
        self.assertIn(
            "X_CAPI_ALLOWED_ORIGIN must be one exact http(s) origin", cfg.errors
        )
        self.assertEqual(status["state"], "unavailable")

    def test_flask_hostile_unicode_origin_never_raises_or_gets_cors(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        with patch.dict(os.environ, self.enabled_env(), clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=True
        ):
            response = gate_server.app.test_client().get(
                "/api/x-attribution/status",
                headers={"Origin": "https://é.example"},
            )
        self.assertLess(response.status_code, 500)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_status_is_lazy_and_grant_is_stateless(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "get_connection"
        ) as connect, patch.object(x_capi, "_request_worker_consent") as rpc:
            idle = x_capi.attribution_status()
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            granted, status_code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="grant",
                landing_twclid="officialclick1234567890",
                origin="https://app.example",
            )
        self.assertEqual(idle["state"], "idle")
        self.assertNotIn("context", idle)
        with patch.dict(os.environ, env, clear=True):
            unset_ticket = x_capi.decode_context_ticket(issued["context"])
        self.assertNotIn("landing_twclid", unset_ticket)
        self.assertRegex(unset_ticket["landing_commitment"], r"^[0-9a-f]{64}$")
        self.assertIsNone(unset_ticket["twclid"])
        self.assertEqual(status_code, 200)
        self.assertEqual(granted["state"], "granted")
        with patch.dict(os.environ, env, clear=True):
            ticket = x_capi.decode_context_ticket(granted["context"])
        self.assertEqual(ticket["twclid"], "officialclick1234567890")
        self.assertTrue(
            x_capi._click_matches_commitment(ticket, "officialclick1234567890")
        )
        connect.assert_not_called()
        rpc.assert_not_called()

    def test_landing_click_is_bound_before_consent_and_body_cannot_replace_it(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="firstclick123456789012"
            )
            denied, status_code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"],
                action="grant", twclid="secondclick12345678901",
                landing_twclid="firstclick123456789012",
                origin="https://app.example",
            )
        self.assertEqual(status_code, 400)
        self.assertFalse(denied["ok"])

    def test_ticket_lifetime_does_not_expand_when_ttl_changes(self):
        first = self.enabled_env(X_CAPI_ATTRIBUTION_TTL_DAYS="1")
        now = 1_800_000_000.0
        with patch.dict(os.environ, first, clear=True), patch.object(
            x_capi.time, "time", return_value=now
        ):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        later = self.enabled_env(X_CAPI_ATTRIBUTION_TTL_DAYS="90")
        with patch.dict(os.environ, later, clear=True), patch.object(
            x_capi.time, "time", return_value=now + 10
        ):
            granted, code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"],
                action="grant", landing_twclid="officialclick1234567890",
                origin="https://app.example",
            )
        self.assertEqual(code, 409)
        self.assertEqual(granted["state"], "stale")

    def test_small_clock_rollback_cannot_issue_an_undecodable_grant(self):
        env = self.enabled_env()
        issued_at = 1_800_000_000.0
        click = "officialclick1234567890"
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi.time, "time", return_value=issued_at
        ):
            issued = x_capi.attribution_status(landing_twclid=click)
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi.time, "time", return_value=issued_at - 120
        ):
            granted, code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"],
                action="grant", landing_twclid=click,
                origin="https://app.example",
            )
            decoded = x_capi.decode_context_ticket(granted.get("context"))
        self.assertEqual(code, 200)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["consented_at"], issued_at)

    def test_audience_scope_change_stales_old_consent(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        changed = self.enabled_env(X_CAPI_PIXEL_ID="different-pixel")
        with patch.dict(os.environ, changed, clear=True):
            status = x_capi.attribution_status(issued["context"])
        self.assertEqual(status["state"], "stale")

        changed_mapping = self.enabled_env(
            X_CAPI_EVENT_DEPOSIT_COMPLETED="replacement-deposit-event"
        )
        with patch.dict(os.environ, changed_mapping, clear=True):
            status = x_capi.attribution_status(issued["context"])
        self.assertEqual(status["state"], "stale")

        exclusion_changed = self.enabled_env(
            X_CAPI_EXCLUDED_WALLETS="0x" + "2" * 40
        )
        with patch.dict(os.environ, exclusion_changed, clear=True):
            status = x_capi.attribution_status(issued["context"])
        self.assertEqual(status["state"], "stale")

    def test_worker_attestation_makes_epoch_rollback_irreversibly_unavailable(self):
        import tempfile

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            os.chmod(temporary, 0o700)
            path = Path(temporary) / "config-guard.json"
            common = self.enabled_env()
            common.pop("X_CAPI_ALLOW_TEST_CONFIG_GUARD_BYPASS")
            common.update({
                "X_CAPI_CONFIG_GUARD_FILE": str(path),
                "X_CAPI_ALLOW_TEST_CONFIG_GUARD_FILE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })

            def publish(env):
                cfg = x_capi.load_config(env)
                with patch.dict(os.environ, env, clear=True):
                    context_key_fingerprint = (
                        x_capi.primary_context_key_fingerprint()
                    )
                document = {
                    "v": 1,
                    "configured": True,
                    "max_policy_epoch": cfg.policy_epoch,
                    "deployment_id_hash": x_capi._hash(cfg.deployment_id),
                    "mode_scope": cfg.mode,
                    "policy_version": cfg.policy_version,
                    "audience_scope": cfg.audience_scope,
                    "context_key_fingerprint": context_key_fingerprint,
                }
                path.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                path.chmod(0o600)

            publish(common)
            with patch.dict(os.environ, common, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
            self.assertEqual(issued["state"], "unset")

            epoch_two = dict(common, X_CAPI_CONSENT_POLICY_EPOCH="2")
            publish(epoch_two)
            with patch.dict(os.environ, epoch_two, clear=True):
                self.assertEqual(
                    x_capi.attribution_status(issued["context"])["state"], "stale"
                )
            with patch.dict(os.environ, common, clear=True), patch.object(
                x_capi, "_send_local_envelope"
            ) as send:
                rolled_back = x_capi.attribution_status(issued["context"])
                emitted = x_capi.emit_event_nonblocking(
                    context_token=issued["context"],
                    wallet_address="0x" + "1" * 40,
                    milestone=x_capi.MILESTONE_WALLET_VERIFIED,
                    source_key="0x" + "1" * 40,
                    allow_context_binding=True,
                )
            self.assertEqual(rolled_back["state"], "unavailable")
            self.assertTrue(rolled_back["configuration_error"])
            # The core producer does not touch the durable guard. It may submit
            # one local datagram; the isolated worker enforces the high-water
            # before any row or outbound request can be created.
            self.assertTrue(emitted)
            send.assert_called_once()

    def test_live_requires_an_explicit_vendor_click_contract(self):
        env = self.enabled_env("live")
        for key in (
            "X_CAPI_TWCLID_CONTRACT_VERSION", "X_CAPI_TWCLID_CHARSET",
            "X_CAPI_TWCLID_MIN_LENGTH", "X_CAPI_TWCLID_MAX_LENGTH",
        ):
            env.pop(key)
        with patch.dict(os.environ, env, clear=True):
            status = x_capi.config_status()
        self.assertFalse(status["producer_ready"])
        self.assertTrue(any("TWCLID" in item for item in status["errors"]))

    def test_cors_gpc_https_and_rate_limit_helpers_fail_closed(self):
        self.assertIsNone(
            security_utils.cors_origin_for_request(
                "https://evil-example.com", "example.com", False, set()
            )
        )
        self.assertEqual(
            security_utils.cors_origin_for_request(
                "https://example.com", "example.com", False, set()
            ),
            "https://example.com",
        )
        self.assertTrue(security_utils.gpc_signal_active("1, 0"))
        self.assertTrue(security_utils.gpc_signal_active("malformed"))
        self.assertFalse(security_utils.gpc_signal_active(None))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                security_utils.request_is_effectively_https(
                    "192.0.2.1", "198.51.100.2", "https", "http"
                )
            )

    def test_shared_limiter_is_o1_keyed_and_corruption_is_local(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary, patch.dict(
            os.environ, {"X_CAPI_ALLOW_TEST_RATE_FILE": "1"}, clear=True
        ):
            path = str(Path(temporary) / "limit.bin")
            limiter = security_utils.SharedFileRateLimiter(
                2, 60, path=path, max_buckets=32, digest_key=b"r" * 32
            )
            self.assertTrue(limiter.allow("203.0.113.7"))
            self.assertTrue(limiter.allow("203.0.113.7"))
            self.assertFalse(limiter.allow("203.0.113.7"))
            stored = Path(path).read_bytes()
            self.assertNotIn(b"203.0.113.7", stored)
            self.assertEqual(
                len(stored), limiter._HEADER.size + 32 * limiter._RECORD.size
            )
            # A privacy request may bypass an unavailable limiter, while an
            # ordinary status/grant always fails closed.
            missing_key = security_utils.SharedFileRateLimiter(
                2, 60, path=path, max_buckets=32
            )
            self.assertFalse(missing_key.allow("ordinary"))
            self.assertTrue(missing_key.allow("privacy", fail_open_on_error=True))

            # Ordinary work admission fails closed under simultaneous
            # cross-process/file-lock contention. Privacy publication uses
            # the same limiter only for a redundant hint and explicitly asks
            # this primitive to fail open.
            import fcntl
            descriptor = limiter._open()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                contender = security_utils.SharedFileRateLimiter(
                    60, 60, path=path, max_buckets=32, digest_key=b"r" * 32
                )
                self.assertFalse(contender.allow("all-clients"))
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            limiter_source = (ROOT / "axonos_gate" / "security_utils.py").read_text(
                encoding="utf-8"
            )
            shared_limiter = limiter_source.split("class SharedFileRateLimiter", 1)[1].split(
                "def client_ip_for_rate_limit", 1
            )[0]
            self.assertIn("/dev/shm/axonos-x-capi/", shared_limiter)
            self.assertNotIn("os.fsync", shared_limiter)
            self.assertNotIn("os.fdatasync", shared_limiter)

    def test_paid_provenance_allowlist(self):
        self.assertTrue(x_capi.paid_deposit_eligible("onchain", "usdc", 42))
        for source, rail, block in (
            ("test_credit", "usdc", 42),
            ("guest_credit", "guest", 42),
            ("onchain", "unknown", 42),
            ("onchain", "eth", 0),
        ):
            self.assertFalse(x_capi.paid_deposit_eligible(source, rail, block))

    def test_origin_and_twclid_validation_fail_closed(self):
        cfg = x_capi.load_config(self.enabled_env())
        self.assertTrue(x_capi.exact_origin_allowed("https://app.example", cfg))
        self.assertFalse(x_capi.exact_origin_allowed("https://app.example.evil", cfg))
        self.assertEqual(x_capi.validate_twclid("abcdEFGH_123", cfg), "abcdEFGH_123")
        self.assertIsNone(x_capi.validate_twclid("bad click/id", cfg))
        self.assertIsNone(x_capi.validate_twclid("alice.eth", cfg))
        for confused in (
            " officialclick1234567890",
            "officialclick1234567890 ",
            "\tofficialclick1234567890",
            "officialclick1234567890\n",
        ):
            with self.subTest(confused=repr(confused)):
                self.assertIsNone(x_capi.validate_twclid(confused, cfg))
                self.assertIsNone(x_capi._validate_twclid_unscoped(confused))
        with patch.dict(os.environ, self.enabled_env(), clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            for confused in (
                " officialclick1234567890",
                "officialclick1234567890 ",
                "\tofficialclick1234567890",
                "officialclick1234567890\n",
            ):
                with self.subTest(status_confused=repr(confused)):
                    fresh = x_capi.attribution_status(landing_twclid=confused)
                    restored = x_capi.attribution_status(
                        issued["context"], landing_twclid=confused
                    )
                    self.assertEqual(fresh["state"], "invalid_click")
                    self.assertNotIn("context", fresh)
                    self.assertEqual(restored["state"], "invalid_click")
                    self.assertFalse(restored["landing_click_accepted"])
        wallet = "0x" + "11" * 20
        encoded_wallet = base64.urlsafe_b64encode(bytes.fromhex(wallet[2:])).decode().rstrip("=")
        self.assertTrue(x_capi.twclid_conflicts_with_wallet(encoded_wallet, wallet))
        encoded_text_wallet = base64.urlsafe_b64encode(
            wallet.encode("ascii")
        ).decode().rstrip("=")
        encoded_bare_text = base64.urlsafe_b64encode(
            wallet[2:].encode("ascii")
        ).decode().rstrip("=")
        self.assertIsNotNone(x_capi.validate_twclid(encoded_text_wallet, cfg))
        self.assertIsNotNone(x_capi.validate_twclid(encoded_bare_text, cfg))
        self.assertTrue(
            x_capi.twclid_conflicts_with_wallet(encoded_text_wallet, wallet)
        )
        self.assertTrue(
            x_capi.twclid_conflicts_with_wallet(encoded_bare_text, wallet)
        )
        encoded_wallet_digest = base64.urlsafe_b64encode(
            hashlib.sha256(wallet.encode("ascii")).digest()
        ).decode().rstrip("=")
        self.assertIsNotNone(x_capi.validate_twclid(encoded_wallet_digest, cfg))
        self.assertTrue(
            x_capi.twclid_conflicts_with_wallet(encoded_wallet_digest, wallet)
        )
        for digest in (
            hashlib.blake2s(wallet.encode("ascii")).digest(),
            hashlib.sha256(wallet.encode("ascii")).digest()[::-1],
            hashlib.sha512(wallet.encode("ascii")).digest()[:32],
        ):
            encoded_digest = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            self.assertIsNotNone(x_capi.validate_twclid(encoded_digest, cfg))
            self.assertTrue(
                x_capi.twclid_conflicts_with_wallet(encoded_digest, wallet)
            )
        self.assertTrue(
            x_capi.twclid_conflicts_with_wallet(
                "EnrGHeqCd5UQ2jTW2Mo32o6a2GG", wallet
            )
        )
        self.assertTrue(
            x_capi.twclid_conflicts_with_wallet(
                str(int(wallet[2:], 16)), wallet
            )
        )

    def test_gpc_forces_decline_and_cancels_without_storing_click(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as rpc, patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="published"
        ):
            result, status_code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"],
                action="grant", origin="https://app.example", gpc=True,
            )
        self.assertEqual(status_code, 200)
        self.assertEqual(result["state"], "revoked")
        self.assertTrue(result["gpc_applied"])
        with patch.dict(os.environ, env, clear=True):
            ticket = x_capi.decode_context_ticket(result["context"])
        self.assertIsNone(ticket["twclid"])
        rpc.assert_called_once()

    def test_gpc_status_preserves_an_already_closed_privacy_state(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ), patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="published"
        ):
            revoked, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="revoke",
                origin="https://app.example",
            )
        self.assertEqual(code, 200)
        with patch.dict(os.environ, env, clear=True):
            status = x_capi.attribution_status(revoked["context"], gpc=True)
        self.assertEqual(status["state"], "revoked")
        self.assertTrue(status["gpc_applied"])

    def test_gpc_dominates_click_mismatch_and_missing_config_attestation(self):
        env = self.enabled_env()
        click = "officialclick1234567890"
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(landing_twclid=click)
            granted, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="grant",
                landing_twclid=click,
                origin="https://app.example",
            )
        self.assertEqual(code, 200)

        for token in (issued["context"], granted["context"]):
            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi, "config_guard_current", return_value=False
            ):
                status = x_capi.attribution_status(
                    token,
                    landing_twclid="differentclick123456789",
                    gpc=True,
                )
            self.assertEqual(status["state"], "revocation_required")
            self.assertTrue(status["gpc_applied"])
            self.assertFalse(status["enabled"])

        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=True
        ), patch.object(
            gate_server, "_x_capi_privacy_rate_allowed", return_value=True
        ), patch.object(
            gate_server.x_capi,
            "observe_privacy_signal_nonblocking",
            return_value=True,
        ), patch.object(
            gate_server.x_capi, "config_guard_current", return_value=False
        ):
            response = gate_server.app.test_client().get(
                "/api/x-attribution/status",
                base_url="https://app.example",
                headers={
                    "Origin": "https://app.example",
                    "Sec-GPC": "1",
                    "X-AxonOS-Attribution": granted["context"],
                    "X-AxonOS-Landing-Click": "differentclick123456789",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "revocation_required")
        self.assertTrue(response.get_json()["gpc_applied"])

    def test_business_gpc_publishes_fixed_fence_when_datagram_and_worker_are_down(self):
        import tempfile

        env = self.enabled_env()
        click = "officialclick1234567890"
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(landing_twclid=click)
                granted, code = x_capi.update_consent(
                    context_token=issued["context"],
                    csrf_token=issued["csrf"],
                    action="grant",
                    landing_twclid=click,
                    origin="https://app.example",
                )
            self.assertEqual(code, 200)
            token = granted["context"]
            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi.time, "time", return_value=123.456
            ), patch.object(
                x_capi, "_send_local_envelope", return_value=False
            ) as send, patch.object(
                x_capi, "ThreadPoolExecutor",
                side_effect=AssertionError("business path created a thread pool"),
            ) as pool, patch.object(
                x_capi.os, "fsync",
                side_effect=AssertionError("business path called fsync"),
            ) as fsync:
                self.assertIsNone(x_capi.business_context_or_none(token, gpc=True))
                self.assertEqual(
                    x_capi.business_context_or_none(token, gpc=False), token
                )
            send.assert_called_once_with({
                "v": 1,
                "action": "revoke",
                "context_token": token,
                "event_timestamp_ms": 123456,
            })
            pool.assert_not_called()
            fsync.assert_not_called()
            occupied = [
                path for path in directory.glob("pending-*")
                if path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
            ]
            self.assertEqual(len(occupied), 1)
            with patch.dict(os.environ, env, clear=True):
                ticket = x_capi.decode_context_ticket(token)
            self.assertEqual(
                occupied[0].read_bytes(), x_capi._privacy_pending_record(ticket)
            )
            self.assertNotIn(click, json.dumps(send.call_args.args[0]))

    def test_non_gpc_core_context_and_event_path_never_reads_ticket_or_durable_guard(self):
        env = self.enabled_env()
        token = "A" * 64
        wallet = "0x" + "1" * 40
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "decode_context_ticket", side_effect=AssertionError("ticket read")
        ) as decode, patch.object(
            x_capi, "config_guard_current", side_effect=AssertionError("guard read")
        ) as guard, patch.object(
            x_capi, "_send_local_envelope", return_value=True
        ) as send:
            self.assertEqual(x_capi.business_context_or_none(token), token)
            self.assertTrue(
                x_capi.emit_event_nonblocking(
                    context_token=token,
                    wallet_address=wallet,
                    milestone=x_capi.MILESTONE_WALLET_VERIFIED,
                    source_key=wallet,
                    allow_context_binding=True,
                )
            )
        decode.assert_not_called()
        guard.assert_not_called()
        send.assert_called_once()

    def test_business_gpc_handoff_is_one_datagram_plus_preallocated_fence(self):
        import tempfile

        env = self.enabled_env()
        env.update({
            "X_CAPI_INGEST_SOCKET": "/tmp/x-capi-business-gpc.sock",
            "X_CAPI_ALLOW_TEST_SOCKET": "1",
        })
        sent = []

        class FakeSocket:
            def setblocking(self, value):
                self.blocking = value

            def connect(self, path):
                self.path = path

            def send(self, payload):
                sent.append(payload)
                return len(payload)

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
            token = issued["context"]
            fake_socket = FakeSocket()
            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi.socket, "socket", return_value=fake_socket
            ) as create_socket, patch.object(
                x_capi, "ThreadPoolExecutor",
                side_effect=AssertionError("business path created a thread pool"),
            ) as pool, patch.object(
                x_capi.os, "fsync",
                side_effect=AssertionError("business path called fsync"),
            ) as fsync:
                started = time.monotonic()
                self.assertIsNone(x_capi.business_context_or_none(token, gpc=True))
                self.assertLess(time.monotonic() - started, 0.05)

            create_socket.assert_called_once_with(
                x_capi.socket.AF_UNIX, x_capi.socket.SOCK_DGRAM
            )
            self.assertFalse(fake_socket.blocking)
            self.assertEqual(fake_socket.path, "/tmp/x-capi-business-gpc.sock")
            self.assertTrue(fake_socket.closed)
            self.assertEqual(len(sent), 1)
            envelope = json.loads(sent[0].decode("ascii"))
            self.assertEqual(envelope["action"], "revoke")
            self.assertEqual(envelope["context_token"], token)
            pool.assert_not_called()
            fsync.assert_not_called()
            self.assertEqual(
                sum(
                    path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
                    for path in directory.glob("pending-*")
                ),
                1,
            )

    def test_full_targeted_slot_capacity_never_activates_global_quarantine(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            for name, contents in (
                (x_capi._PRIVACY_FENCE_LOCK_NAME, x_capi._PRIVACY_FENCE_LOCK_MAGIC),
                (
                    x_capi._PRIVACY_FENCE_QUARANTINE_NAME,
                    x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
                ),
            ):
                path = directory / name
                path.write_bytes(contents)
                os.chmod(path, 0o600)
            expiry = time.time() + 3600
            for index in range(x_capi._PRIVACY_PENDING_SLOT_COUNT):
                record = (
                    x_capi._PRIVACY_PENDING_PREFIX
                    + f"{index:064x}".encode("ascii")
                    + f"{index + 1:064x}".encode("ascii")
                    + x_capi.struct.pack("!d", expiry)
                )
                path = directory / f"{x_capi._PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
                path.write_bytes(record)
                os.chmod(path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                with patch.object(
                    x_capi, "_activate_global_privacy_quarantine"
                ) as quarantine:
                    self.assertFalse(
                        x_capi.publish_privacy_fence_nonblocking(
                            issued["context"]
                        )
                    )
                quarantine.assert_not_called()
            self.assertEqual(
                (directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
            )

    def test_privacy_slot_collision_probes_exactly_four_deterministic_files(self):
        import tempfile

        env = self.enabled_env()
        ticket = {
            "handle": "h" * 32,
            "csrf": "c" * 32,
            "lifecycle_expires_at": time.time() + 3600,
        }
        record = x_capi._privacy_pending_record(ticket)
        self.assertIsNotNone(record)
        identity = x_capi._privacy_pending_record_identity(record)
        indices = x_capi._privacy_pending_slot_indices(identity)
        self.assertEqual(len(indices), 4)
        self.assertEqual(len(set(indices)), 4)
        self.assertEqual(indices, x_capi._privacy_pending_slot_indices(identity))

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            expiry = time.time() + 3600
            for ordinal, index in enumerate(indices):
                blocker_identity = hashlib.sha256(
                    f"collider-{ordinal}".encode("ascii")
                ).hexdigest().encode("ascii")
                self.assertNotEqual(blocker_identity, identity)
                blocker = (
                    x_capi._PRIVACY_PENDING_PREFIX
                    + blocker_identity
                    + (b"f" * 64)
                    + x_capi.struct.pack("!d", expiry)
                )
                path = directory / f"{x_capi._PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
                path.write_bytes(blocker)
                os.chmod(path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            original_validate = x_capi._validated_privacy_control
            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi, "_validated_privacy_control", wraps=original_validate
            ) as validate:
                self.assertIsNone(x_capi._claim_privacy_pending_slot(record))

            pending_names = [
                call.args[1]
                for call in validate.call_args_list
                if len(call.args) > 1
                and str(call.args[1]).startswith(x_capi._PRIVACY_PENDING_SLOT_PREFIX)
            ]
            self.assertEqual(
                pending_names,
                [f"{x_capi._PRIVACY_PENDING_SLOT_PREFIX}{index:02d}" for index in indices],
            )

    def test_invalid_opaque_privacy_input_never_claims_or_quarantines(self):
        with patch.object(
            x_capi, "decode_context_ticket", return_value=None
        ), patch.object(
            x_capi, "_claim_privacy_pending_slot"
        ) as claim, patch.object(
            x_capi, "_activate_global_privacy_quarantine"
        ) as durable_quarantine, patch.object(
            x_capi, "_activate_global_privacy_quarantine_nonblocking"
        ) as nonblocking_quarantine:
            self.assertFalse(
                x_capi.publish_privacy_fence_nonblocking("Z" * 64)
            )
            self.assertFalse(
                x_capi.publish_privacy_fence_nonblocking(
                    "Z" * 64, fail_closed=True
                )
            )
        claim.assert_not_called()
        durable_quarantine.assert_not_called()
        nonblocking_quarantine.assert_not_called()

    def test_business_gpc_full_slot_pool_trips_only_preallocated_global_control(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            expiry = time.time() + 3600
            for index in range(x_capi._PRIVACY_PENDING_SLOT_COUNT):
                record = (
                    x_capi._PRIVACY_PENDING_PREFIX
                    + f"{index:064x}".encode("ascii")
                    + f"{index + 1:064x}".encode("ascii")
                    + x_capi.struct.pack("!d", expiry)
                )
                (directory / f"pending-{index:02d}").write_bytes(record)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
            names_before = {path.name for path in directory.iterdir()}
            real_release = x_capi._release_privacy_dispatch_control
            quarantine_seen_before_release = []

            def release_after_check(directory_fd, descriptor):
                quarantine_seen_before_release.append(
                    (
                        directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME
                    ).read_bytes()
                )
                return real_release(directory_fd, descriptor)

            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi, "_emit_revocation_hint_nonblocking", return_value=False
            ) as hint, patch.object(
                x_capi.os, "fsync",
                side_effect=AssertionError("business path called fsync"),
            ) as fsync, patch.object(
                x_capi, "_release_privacy_dispatch_control",
                side_effect=release_after_check,
            ) as release:
                started = time.monotonic()
                self.assertTrue(
                    x_capi.observe_business_gpc_nonblocking(issued["context"])
                )
                self.assertLess(time.monotonic() - started, 0.05)
            hint.assert_called_once()
            fsync.assert_not_called()
            release.assert_called_once()
            self.assertEqual(
                quarantine_seen_before_release,
                [x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE],
            )
            self.assertEqual(
                (directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE,
            )
            self.assertEqual(
                {path.name for path in directory.iterdir()}, names_before
            )

    def test_business_gpc_dispatch_contention_trips_global_without_waiting(self):
        import fcntl
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
            descriptor = os.open(
                directory / x_capi._PRIVACY_FENCE_LOCK_NAME, os.O_RDWR
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.dict(os.environ, env, clear=True), patch.object(
                    x_capi, "_emit_revocation_hint_nonblocking", return_value=False
                ), patch.object(
                    x_capi.os, "fsync",
                    side_effect=AssertionError("business path called fsync"),
                ) as fsync:
                    started = time.monotonic()
                    self.assertTrue(
                        x_capi.observe_business_gpc_nonblocking(
                            issued["context"]
                        )
                    )
                    self.assertLess(time.monotonic() - started, 0.05)
                fsync.assert_not_called()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertEqual(
                (directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE,
            )

    def test_nonblocking_global_fallback_poisons_dispatch_if_sentinel_write_fails(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            real_pwrite = os.pwrite

            def fail_sentinel(descriptor, value, offset):
                if value == x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE:
                    raise OSError("synthetic sentinel failure")
                return real_pwrite(descriptor, value, offset)

            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi.os, "pwrite", side_effect=fail_sentinel
            ), patch.object(
                x_capi.os, "fsync",
                side_effect=AssertionError("business path called fsync"),
            ) as fsync:
                self.assertTrue(
                    x_capi._activate_global_privacy_quarantine_nonblocking()
                )
            fsync.assert_not_called()
            self.assertNotEqual(
                (directory / x_capi._PRIVACY_FENCE_LOCK_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_LOCK_MAGIC,
            )
            self.assertEqual(
                (directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
            )

    def test_repeated_privacy_publication_reuses_one_committed_slot(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            controls = (
                (x_capi._PRIVACY_FENCE_LOCK_NAME, x_capi._PRIVACY_FENCE_LOCK_MAGIC),
                (
                    x_capi._PRIVACY_FENCE_QUARANTINE_NAME,
                    x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
                ),
            )
            for name, contents in controls:
                path = directory / name
                path.write_bytes(contents)
                os.chmod(path, 0o600)
            for index in range(x_capi._PRIVACY_PENDING_SLOT_COUNT):
                path = directory / f"{x_capi._PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
                path.write_bytes(x_capi._PRIVACY_PENDING_EMPTY)
                os.chmod(path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                granted, code = x_capi.update_consent(
                    context_token=issued["context"],
                    csrf_token=issued["csrf"],
                    action="grant",
                    landing_twclid="officialclick1234567890",
                    origin="https://app.example",
                )
                self.assertEqual(code, 200)
                for _unused in range(x_capi._PRIVACY_PENDING_SLOT_COUNT + 1):
                    self.assertTrue(
                        x_capi.publish_privacy_fence_nonblocking(
                            granted["context"]
                        )
                    )
                self.assertFalse(
                    x_capi.publish_privacy_fence_nonblocking("Z" * 64)
                )

            occupied = [
                path for path in directory.glob("pending-*")
                if path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
            ]
            self.assertEqual(len(occupied), 1)
            self.assertEqual(occupied[0].read_bytes()[:2], x_capi._PRIVACY_PENDING_PREFIX)
            self.assertEqual(
                (directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
            )

    def test_pending_slot_claim_does_not_wait_or_quarantine_on_contention(self):
        ticket = {
            "handle": "h" * 32,
            "csrf": "c" * 32,
            "lifecycle_expires_at": time.time() + 3600,
        }
        with patch.object(
            x_capi,
            "decode_context_ticket",
            return_value=ticket,
        ), patch.object(
            x_capi,
            "_claim_privacy_pending_slot",
            return_value=x_capi._PRIVACY_SLOT_DISPATCH_BUSY,
        ) as claim, patch.object(
            x_capi.time, "sleep"
        ) as pause, patch.object(
            x_capi, "_activate_global_privacy_quarantine"
        ) as quarantine:
            self.assertFalse(
                x_capi.publish_privacy_fence_nonblocking("A" * 64)
            )
        claim.assert_called_once()
        pause.assert_not_called()
        quarantine.assert_not_called()

    def test_concurrent_same_handle_publishers_consume_exactly_one_slot(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            for name, contents in (
                (x_capi._PRIVACY_FENCE_LOCK_NAME, x_capi._PRIVACY_FENCE_LOCK_MAGIC),
                (
                    x_capi._PRIVACY_FENCE_QUARANTINE_NAME,
                    x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
                ),
            ):
                path = directory / name
                path.write_bytes(contents)
                os.chmod(path, 0o600)
            for index in range(x_capi._PRIVACY_PENDING_SLOT_COUNT):
                path = directory / f"{x_capi._PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
                path.write_bytes(x_capi._PRIVACY_PENDING_EMPTY)
                os.chmod(path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                granted, code = x_capi.update_consent(
                    context_token=issued["context"],
                    csrf_token=issued["csrf"],
                    action="grant",
                    landing_twclid="officialclick1234567890",
                    origin="https://app.example",
                )
                self.assertEqual(code, 200)
                ready = threading.Barrier(12)
                failures = []

                def publish():
                    try:
                        ready.wait(timeout=2.0)
                        x_capi.publish_privacy_fence_nonblocking(
                            granted["context"]
                        )
                    except BaseException as exc:  # captured for the parent assertion
                        failures.append(exc)

                threads = [threading.Thread(target=publish) for _unused in range(12)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5.0)

            self.assertFalse(failures)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            occupied = [
                path for path in directory.glob("pending-*")
                if path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
            ]
            self.assertEqual(len(occupied), 1)
            self.assertEqual(
                occupied[0].read_bytes()[:2], x_capi._PRIVACY_PENDING_PREFIX
            )

    def test_o_excl_race_with_partial_marker_never_acknowledges_publication(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            for name, contents in (
                (x_capi._PRIVACY_FENCE_LOCK_NAME, x_capi._PRIVACY_FENCE_LOCK_MAGIC),
                (
                    x_capi._PRIVACY_FENCE_QUARANTINE_NAME,
                    x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE,
                ),
            ):
                path = directory / name
                path.write_bytes(contents)
                os.chmod(path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                ticket = x_capi.decode_context_ticket(issued["context"])
                destination = directory / (x_capi._hash(ticket["handle"]) + ".json")
                real_open = os.open
                injected = False

                def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal injected
                    if (
                        not injected
                        and os.fspath(path) == os.fspath(destination)
                        and flags & os.O_EXCL
                    ):
                        injected = True
                        descriptor = real_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        try:
                            os.write(descriptor, b"{")
                        finally:
                            os.close(descriptor)
                        raise FileExistsError("simulated partial O_EXCL winner")
                    return real_open(path, flags, mode, dir_fd=dir_fd)

                with patch.object(x_capi.os, "open", side_effect=racing_open):
                    status = x_capi._persist_privacy_fence_status(
                        issued["context"]
                    )
            self.assertTrue(injected)
            self.assertEqual(status, "quarantined")
            self.assertEqual(destination.read_bytes(), b"{")
            self.assertEqual(
                (directory / x_capi._PRIVACY_FENCE_QUARANTINE_NAME).read_bytes(),
                x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE,
            )

    def test_core_datagram_send_drops_first_nonblocking_failure_without_retry_or_io(self):
        env = {
            "X_CAPI_INGEST_SOCKET": "/tmp/x-capi-dropped-datagram.sock",
            "X_CAPI_ALLOW_TEST_SOCKET": "1",
        }

        class FakeSocket:
            def __init__(self):
                self.connect_calls = 0
                self.send_calls = 0
                self.closed = False

            def setblocking(self, value):
                self.blocking = value

            def connect(self, _path):
                self.connect_calls += 1
                raise BlockingIOError("synthetic queue pressure")

            def send(self, _payload):
                self.send_calls += 1
                raise AssertionError("send followed failed connect")

            def close(self):
                self.closed = True

        fake_socket = FakeSocket()
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi.socket, "socket", return_value=fake_socket
        ) as create_socket, patch.object(
            x_capi, "decode_context_ticket",
            side_effect=AssertionError("core send decrypted a ticket"),
        ) as decode, patch.object(
            x_capi, "_claim_privacy_pending_slot",
            side_effect=AssertionError("core send claimed a fixed slot"),
        ) as claim, patch.object(
            x_capi, "ThreadPoolExecutor",
            side_effect=AssertionError("core send created a thread pool"),
        ) as pool, patch.object(
            x_capi.os, "lstat", side_effect=AssertionError("core send lstat")
        ) as lstat, patch.object(
            x_capi.os, "open", side_effect=AssertionError("core send open")
        ) as open_file, patch.object(
            x_capi.os, "stat", side_effect=AssertionError("core send stat")
        ) as stat_file, patch.object(
            x_capi.os, "pread", side_effect=AssertionError("core send pread")
        ) as pread, patch.object(
            x_capi.fcntl, "flock", side_effect=AssertionError("core send flock")
        ) as flock:
            started = time.monotonic()
            self.assertFalse(
                x_capi._send_local_envelope({"v": 1, "action": "event"})
            )
            self.assertLess(time.monotonic() - started, 0.05)

        create_socket.assert_called_once_with(x_capi.socket.AF_UNIX, x_capi.socket.SOCK_DGRAM)
        self.assertFalse(fake_socket.blocking)
        self.assertEqual(fake_socket.connect_calls, 1)
        self.assertEqual(fake_socket.send_calls, 0)
        self.assertTrue(fake_socket.closed)
        for forbidden in (decode, claim, pool, lstat, open_file, stat_file, pread, flock):
            forbidden.assert_not_called()

    def test_bind_producer_has_no_conversion_milestone_or_source_identifier(self):
        env = self.enabled_env()
        token = "A" * 64
        wallet = "0x" + "1" * 40
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_send_local_envelope", return_value=True
        ) as send:
            self.assertTrue(
                x_capi.emit_binding_nonblocking(
                    context_token=token,
                    wallet_address=wallet,
                    event_timestamp_ms=123456789,
                )
            )
        send.assert_called_once_with({
            "v": 1,
            "action": "bind",
            "context_token": token,
            "wallet_address": wallet,
            "event_timestamp_ms": 123456789,
        })

    def test_missing_consent_and_unauthorized_updates_fail_closed(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            denied, denied_code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"],
                action="grant",
                origin="https://app.example.evil",
            )
        self.assertEqual(denied_code, 403)
        self.assertEqual(denied["error"], "Origin not allowed")
        with patch.dict(os.environ, env, clear=True):
            denied, denied_code = x_capi.update_consent(
                context_token=issued["context"], csrf_token="c" * 43,
                action="grant",
                origin="https://app.example",
            )
        self.assertEqual(denied_code, 403)

    def test_signature_failure_does_not_create_wallet_milestone(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server
        client = gate_server.app.test_client()
        with patch.object(
            gate_server, "verify_signed_challenge", return_value=False
        ), patch.object(
            gate_server.x_capi, "emit_event_nonblocking", create=True
        ) as emit:
            response = client.post(
                "/api/auth/verify-wallet",
                headers={"X-AxonOS-Attribution": "h" * 40},
                json={
                    "wallet_address": "0x" + "1" * 40,
                    "message": "challenge", "signature": "0xbad",
                },
            )
        self.assertGreaterEqual(response.status_code, 400)
        emit.assert_not_called()

    def test_flask_gpc_is_observed_before_malformed_business_request_rejection(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_privacy_rate_allowed",
            side_effect=AssertionError("business GPC used attribution limiter"),
        ) as limiter, patch.object(
            gate_server.x_capi,
            "observe_business_gpc_nonblocking",
            return_value=True,
        ) as observe, patch.object(
            gate_server.x_capi,
            "observe_privacy_signal_nonblocking",
            side_effect=AssertionError("business GPC used fixed privacy fence"),
        ) as fixed_fence:
            response = gate_server.app.test_client().post(
                "/api/auth/verify-wallet",
                json={},
                headers={
                    "Sec-GPC": "1",
                    "X-AxonOS-Attribution": issued["context"],
                },
            )
        self.assertGreaterEqual(response.status_code, 400)
        observe.assert_called_once_with(issued["context"])
        limiter.assert_not_called()
        fixed_fence.assert_not_called()

    def test_websockify_gpc_observer_runs_before_business_dispatch(self):
        import ast

        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        method = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_observe_request_gpc_early"
        )
        namespace = {
            "x_capi": x_capi,
            "gpc_signal_active": security_utils.gpc_signal_active,
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                "websockify_gate.py",
                "exec",
            ),
            namespace,
        )

        class Headers(dict):
            pass

        class Request:
            def __init__(self, headers):
                self.headers = headers

            def _x_capi_privacy_rate_allowed(self, _key):
                raise AssertionError("business GPC used attribution limiter")

        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            with patch.object(
                x_capi, "observe_business_gpc_nonblocking", return_value=True
            ) as observe, patch.object(
                x_capi, "observe_privacy_signal_nonblocking",
                side_effect=AssertionError("business GPC used fixed privacy fence"),
            ) as fixed_fence:
                requests = []
                for path in (
                    "/api/auth/verify-wallet",
                    "/api/x-attribution/status",
                    "/api/x-attribution/status;malformed",
                    "/api/x-attribution/not-a-route",
                ):
                    request = Request(Headers({
                        "Sec-GPC": "1",
                        "X-AxonOS-Attribution": issued["context"],
                    }))
                    requests.append(request)
                    namespace["_observe_request_gpc_early"](request, path)
        self.assertEqual(
            observe.call_args_list,
            [call(issued["context"])] * len(requests),
        )
        fixed_fence.assert_not_called()
        self.assertTrue(all(
            request.headers._axonos_x_capi_gpc_observed
            for request in requests
        ))

        failed_request = Request(Headers({
            "Sec-GPC": "1",
            "X-AxonOS-Attribution": issued["context"],
        }))
        with patch.object(
            x_capi,
            "observe_business_gpc_nonblocking",
            side_effect=RuntimeError("worker unavailable"),
        ):
            self.assertIsNone(namespace["_observe_request_gpc_early"](
                failed_request, "/api/auth/verify-wallet"
            ))
        self.assertTrue(failed_request.headers._axonos_x_capi_gpc_observed)

    def test_flask_gpc_observer_covers_attribution_options_and_rejected_verbs(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        context = "A" * 64
        cases = (
            ("OPTIONS", "/api/x-attribution/status"),
            ("OPTIONS", "/api/x-attribution/status;malformed"),
            ("GET", "/api/x-attribution/status;malformed"),
            ("GET", "/api/x-attribution/not-a-route"),
            ("DELETE", "/api/x-attribution/consent"),
            ("BREW", "/api/auth/verify-wallet"),
        )
        with patch.object(
            gate_server.x_capi,
            "observe_business_gpc_nonblocking",
            return_value=True,
        ) as observe:
            client = gate_server.app.test_client()
            for method, path in cases:
                with self.subTest(method=method, path=path):
                    client.open(
                        path,
                        method=method,
                        headers={
                            "Sec-GPC": "1",
                            "X-AxonOS-Attribution": context,
                        },
                    )
        self.assertEqual(observe.call_args_list, [call(context)] * len(cases))

        # A failed local privacy handoff must suppress conversion context but
        # must not turn an otherwise ordinary miss into a 500 response.
        with patch.object(
            gate_server.x_capi,
            "observe_business_gpc_nonblocking",
            side_effect=RuntimeError("worker unavailable"),
        ):
            response = gate_server.app.test_client().get(
                "/definitely-not-a-route",
                headers={
                    "Sec-GPC": "1",
                    "X-AxonOS-Attribution": context,
                },
            )
        self.assertEqual(response.status_code, 404)

    def test_websockify_parse_request_observes_every_parsed_method(self):
        import ast

        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        parse_method = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "parse_request"
        )

        class Base:
            def parse_request(self):
                return self.parsed

        synthetic = ast.ClassDef(
            name="Request",
            bases=[ast.Name(id="Base", ctx=ast.Load())],
            keywords=[],
            body=[parse_method],
            decorator_list=[],
        )
        namespace = {"Base": Base}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[synthetic], type_ignores=[])
                ),
                "websockify_gate.py",
                "exec",
            ),
            namespace,
        )

        for command in ("GET", "POST", "PUT", "OPTIONS", "DELETE", "BREW"):
            with self.subTest(command=command):
                request = namespace["Request"]()
                request.command = command
                request.parsed = True
                request._observe_request_gpc_early = MagicMock()
                self.assertTrue(request.parse_request())
                request._observe_request_gpc_early.assert_called_once_with()

        rejected = namespace["Request"]()
        rejected.parsed = False
        rejected._observe_request_gpc_early = MagicMock()
        self.assertFalse(rejected.parse_request())
        rejected._observe_request_gpc_early.assert_not_called()

    def test_gpc_observer_precedes_listener_rejection_and_all_ws_bypasses(self):
        gate_source = (ROOT / "axonos_gate" / "gate_server.py").read_text(
            encoding="utf-8"
        )
        proxy_source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )

        # Flask stops evaluating before_request hooks as soon as one returns a
        # response, so the privacy hook must be registered before the internal
        # agent-listener admission hook.
        observe_hook = "def _observe_gpc_before_route_dispatch():"
        restrict_hook = "def _restrict_internal_agent_listener():"
        self.assertLess(gate_source.index(observe_hook), gate_source.index(restrict_hook))
        self.assertIn("@app.before_request\n" + observe_hook, gate_source)
        self.assertIn("@app.before_request\n" + restrict_hook, gate_source)

        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        hooks = gate_server.app.before_request_funcs.get(None, [])
        hook_names = [getattr(hook, "__name__", "") for hook in hooks]
        self.assertLess(
            hook_names.index("_observe_gpc_before_route_dispatch"),
            hook_names.index("_restrict_internal_agent_listener"),
        )

        # The WSGI websocket switch bypasses Flask entirely. It must observe
        # GPC before either websocket proxy receives the request.
        application = gate_source.split("def _application(environ, start_response):", 1)[1].split(
            "def _init_all_tables():", 1
        )[0]
        application_observe = "x_capi.observe_business_gpc_nonblocking(context_token)"
        self.assertIn(application_observe, application)
        self.assertLess(application.index(application_observe), application.index("_handle_terminal_proxy"))
        self.assertLess(application.index(application_observe), application.index("_handle_websockify_proxy"))

        observer = MagicMock()
        websocket = object()
        with patch.object(
            gate_server.x_capi, "observe_business_gpc_nonblocking", observer
        ), patch.object(
            gate_server, "_handle_websockify_proxy", return_value=[b"proxied"]
        ) as dispatch:
            result = gate_server._application(
                {
                    "PATH_INFO": "/websockify",
                    "HTTP_UPGRADE": "websocket",
                    "HTTP_SEC_GPC": "1",
                    "HTTP_X_AXONOS_ATTRIBUTION": "A" * 64,
                    "wsgi.websocket": websocket,
                },
                MagicMock(),
            )
        self.assertEqual(result, [b"proxied"])
        observer.assert_called_once_with("A" * 64)
        dispatch.assert_called_once()

        # Websockify's non-POST file path and websocket-upgrade path both sit
        # outside do_POST, so each needs its own first-step observer call.
        put = proxy_source.split("    def do_PUT(self):", 1)[1].split(
            "    def do_POST(self):", 1
        )[0]
        upgrade = proxy_source.split("    def handle_upgrade(self):", 1)[1].split(
            "\n_last_liveness_stamp_at =", 1
        )[0]
        for branch in (put, upgrade):
            self.assertIn("self._observe_request_gpc_early(request_path)", branch)
        self.assertLess(
            put.index("self._observe_request_gpc_early(request_path)"),
            put.index("self._handle_files_request('PUT')"),
        )
        self.assertLess(
            upgrade.index("self._observe_request_gpc_early(request_path)"),
            upgrade.index("_terminal_context_for_handler(self)"),
        )

    def test_off_mode_keeps_context_bound_revocation_available(self):
        enabled = self.enabled_env()
        with patch.dict(os.environ, enabled, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        off = dict(enabled, X_CAPI_MODE="off")
        with patch.dict(os.environ, off, clear=True), patch.object(
            x_capi, "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ), patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="published"
        ):
            result, status_code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"], action="revoke",
                origin="https://app.example",
            )
        self.assertEqual(status_code, 200)
        self.assertEqual(result["state"], "revoked")

    def test_prior_https_origin_can_revoke_after_origin_reconfiguration(self):
        old_env = self.enabled_env(X_CAPI_ALLOWED_ORIGIN="https://old.example")
        click = "officialclick1234567890"
        with patch.dict(os.environ, old_env, clear=True):
            issued = x_capi.attribution_status(landing_twclid=click)
            granted, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="grant",
                landing_twclid=click,
                origin="https://old.example",
            )
        self.assertEqual(code, 200)

        new_env = self.enabled_env(X_CAPI_ALLOWED_ORIGIN="https://new.example")
        with patch.dict(os.environ, new_env, clear=True):
            self.assertIsNotNone(
                x_capi.privacy_action_rate_key(
                    context_token=granted["context"],
                    csrf_token=granted["csrf"],
                    action="revoke",
                    origin="https://old.example",
                )
            )

        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server
        client = gate_server.app.test_client()
        with patch.dict(os.environ, new_env, clear=True), patch.object(
            gate_server._x_capi_privacy_rate_limiter, "allow", return_value=True
        ), patch.object(
            gate_server.x_capi,
            "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ), patch.object(
            gate_server.x_capi,
            "_persist_privacy_fence_status",
            return_value="published",
        ):
            preflight = client.options(
                "/api/x-attribution/consent",
                base_url="https://api.example",
                headers={"Origin": "https://old.example"},
            )
            response = client.post(
                "/api/x-attribution/consent",
                base_url="https://api.example",
                json={"action": "revoke"},
                headers={
                    "Origin": "https://old.example",
                    "X-AxonOS-Attribution": granted["context"],
                    "X-AxonOS-CSRF": granted["csrf"],
                },
            )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(
            preflight.headers.get("Access-Control-Allow-Origin"),
            "https://old.example",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "revoked")
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "https://old.example",
        )

    def test_direct_worker_db_url_requires_explicit_test_boundary(self):
        url = "postgresql://user:secret@disposable/test"
        with patch.dict(os.environ, {"X_CAPI_DB_URL": url}, clear=True):
            self.assertIsNone(x_capi._db_url(worker=True))
        with patch.dict(os.environ, {
            "X_CAPI_DB_URL": url, "X_CAPI_ALLOW_TEST_DB_URL": "1"
        }, clear=True):
            self.assertEqual(x_capi._db_url(worker=True), url)

    def test_structurally_invalid_opaque_ticket_never_loads_key_material(self):
        candidate = "A" * 64
        self.assertIsNotNone(x_capi._OPAQUE_RE.fullmatch(candidate))
        self.assertFalse(x_capi._fernet_wire_envelope_valid(candidate))
        with patch.object(
            x_capi, "_context_ciphers",
            side_effect=AssertionError("keyring load reached"),
        ) as ciphers, patch.object(
            x_capi, "_read_restricted_secret",
            side_effect=AssertionError("secret read reached"),
        ) as read_secret:
            self.assertIsNone(x_capi.decode_context_ticket(candidate))
        ciphers.assert_not_called()
        read_secret.assert_not_called()

    def test_production_context_keyring_is_cached_and_survives_fork_reset(self):
        cache_snapshot = (
            x_capi._context_cipher_cache_lock,
            x_capi._context_cipher_cache_pid,
            x_capi._context_cipher_cache_initialized,
            x_capi._context_cipher_cache,
            x_capi._context_cipher_cache_error,
        )
        consent_snapshot = (
            x_capi._consent_rpc_lock,
            x_capi._consent_rpc_pid,
            x_capi._consent_rpc_executor,
            x_capi._consent_rpc_slots,
        )
        cached_tuple = (object(),)
        try:
            x_capi._context_cipher_cache_lock = threading.Lock()
            x_capi._context_cipher_cache_pid = 0
            x_capi._context_cipher_cache_initialized = False
            x_capi._context_cipher_cache = None
            x_capi._context_cipher_cache_error = None
            production_env = {
                "X_CAPI_CONTEXT_KEY_FILE": "/run/secrets/x_capi_context_key"
            }
            with patch.dict(os.environ, production_env, clear=True), patch.object(
                x_capi, "_load_context_ciphers",
                return_value=(cached_tuple, None),
            ) as load, patch.object(x_capi.os, "getpid", return_value=4242):
                self.assertIs(x_capi._context_ciphers()[0], cached_tuple)
                self.assertIs(x_capi._context_ciphers()[0], cached_tuple)
                self.assertEqual(load.call_count, 1)

                inherited_lock = x_capi._context_cipher_cache_lock
                x_capi._reset_consent_rpc_after_fork()
                self.assertIsNot(x_capi._context_cipher_cache_lock, inherited_lock)
                self.assertIs(x_capi._context_ciphers()[0], cached_tuple)
                self.assertEqual(load.call_count, 1)

            # Direct in-process test secrets intentionally remain uncached so
            # tests may rotate them without pretending to restart a service.
            with patch.dict(
                os.environ, {"X_CAPI_CONTEXT_KEY": "test-key"}, clear=True
            ), patch.object(
                x_capi, "_load_context_ciphers",
                return_value=(cached_tuple, None),
            ) as direct_load:
                x_capi._context_ciphers()
                x_capi._context_ciphers()
            self.assertEqual(direct_load.call_count, 2)
        finally:
            (
                x_capi._context_cipher_cache_lock,
                x_capi._context_cipher_cache_pid,
                x_capi._context_cipher_cache_initialized,
                x_capi._context_cipher_cache,
                x_capi._context_cipher_cache_error,
            ) = cache_snapshot
            (
                x_capi._consent_rpc_lock,
                x_capi._consent_rpc_pid,
                x_capi._consent_rpc_executor,
                x_capi._consent_rpc_slots,
            ) = consent_snapshot

    def test_context_keyring_keeps_old_ticket_revocable(self):
        old_key = base64.urlsafe_b64encode(b"o" * 32).decode("ascii")
        new_key = base64.urlsafe_b64encode(b"n" * 32).decode("ascii")
        initial = self.enabled_env(X_CAPI_CONTEXT_KEY=old_key)
        with patch.dict(os.environ, initial, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            granted, grant_code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="grant",
                landing_twclid="officialclick1234567890",
                origin="https://app.example",
            )
        self.assertEqual(grant_code, 200)
        rotated = self.enabled_env(X_CAPI_CONTEXT_KEY=new_key + "\n" + old_key)
        with patch.dict(os.environ, rotated, clear=True), patch.object(
            x_capi, "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ), patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="published"
        ), patch.object(x_capi, "_send_local_envelope") as send:
            self.assertIsNotNone(x_capi.decode_context_ticket(issued["context"]))
            self.assertIsNone(
                x_capi.decode_context_ticket(
                    issued["context"], require_primary=True
                )
            )
            self.assertEqual(
                x_capi.attribution_status(granted["context"])["state"], "stale"
            )
            rejected_grant, rejected_code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="grant",
                landing_twclid="officialclick1234567890",
                origin="https://app.example",
            )
            self.assertEqual(rejected_code, 409)
            self.assertEqual(rejected_grant["state"], "stale")
            self.assertEqual(
                x_capi.business_context_or_none(granted["context"]),
                granted["context"],
            )
            self.assertTrue(
                x_capi.emit_event_nonblocking(
                    context_token=granted["context"],
                    wallet_address="0x" + "1" * 40,
                    milestone=x_capi.MILESTONE_WALLET_VERIFIED,
                    source_key="0x" + "1" * 40,
                    allow_context_binding=True,
                )
            )
            send.assert_called_once()
            result, code = x_capi.update_consent(
                context_token=granted["context"], csrf_token=granted["csrf"],
                action="revoke", origin="https://app.example",
            )
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "revoked")
        with patch.dict(os.environ, rotated, clear=True):
            self.assertIsNotNone(
                x_capi.decode_context_ticket(result["context"], require_primary=True)
            )

        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server
        with patch.dict(os.environ, rotated, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=True
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server, "_bind_attribution_nonblocking"
        ) as bind:
            response = gate_server.app.test_client().post(
                "/api/x-attribution/bind",
                base_url="https://app.example",
                json={"wallet_address": "0x" + "1" * 40},
                headers={
                    "Origin": "https://app.example",
                    "X-AxonOS-Attribution": granted["context"],
                },
            )
        self.assertEqual(response.status_code, 409)
        bind.assert_not_called()

    def test_failed_privacy_rpc_is_never_presented_as_success(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_request_worker_consent", return_value=None
        ):
            result, code = x_capi.update_consent(
                context_token=issued["context"], csrf_token=issued["csrf"],
                action="revoke", origin="https://app.example",
            )
        self.assertEqual(code, 503)
        self.assertFalse(result["ok"])

    def test_ambiguous_privacy_fence_never_allows_a_false_revoke_ack(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="unavailable"
        ), patch.object(
            x_capi, "_emit_revocation_hint_nonblocking", return_value=False
        ), patch.object(
            x_capi,
            "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as worker_rpc:
            result, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="revoke",
                origin="https://app.example",
            )
        self.assertEqual(code, 503)
        self.assertFalse(result["ok"])
        worker_rpc.assert_not_called()

    def test_unavailable_preallocated_privacy_controls_never_allow_false_ack(self):
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            missing = str(Path(temporary) / "missing-controls")
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": missing,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )

            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi,
                "_request_worker_consent",
                return_value={"v": 1, "ok": True, "state": "revoked"},
            ) as worker_rpc:
                result, code = x_capi.update_consent(
                    context_token=issued["context"],
                    csrf_token=issued["csrf"],
                    action="revoke",
                    origin="https://app.example",
                )
            self.assertEqual(code, 503)
            self.assertFalse(result["ok"])
            worker_rpc.assert_not_called()

    def test_verified_global_privacy_quarantine_permits_durable_revoke_ack(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="quarantined"
        ), patch.object(
            x_capi, "_emit_revocation_hint_nonblocking", return_value=False
        ), patch.object(
            x_capi,
            "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as worker_rpc:
            result, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="revoke",
                origin="https://app.example",
            )
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "revoked")
        worker_rpc.assert_called_once()

    def test_explicit_revoke_uses_fixed_controls_without_marker_or_fsync(self):
        import tempfile

        env = self.enabled_env()
        click = "officialclick1234567890"
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(landing_twclid=click)
                ticket = x_capi.decode_context_ticket(issued["context"])

            names_before = {path.name for path in directory.iterdir()}
            with patch.dict(os.environ, env, clear=True), patch.object(
                x_capi, "_request_worker_consent",
                return_value={"v": 1, "ok": True, "state": "revoked"},
            ) as worker_rpc, patch.object(
                x_capi, "_emit_revocation_hint_nonblocking", return_value=False
            ), patch.object(
                x_capi.os, "fsync",
                side_effect=AssertionError("explicit revoke called fsync"),
            ) as fsync, patch.object(
                x_capi.os, "write",
                side_effect=AssertionError("explicit revoke allocated a marker"),
            ) as write:
                result, code = x_capi.update_consent(
                    context_token=issued["context"],
                    csrf_token=issued["csrf"],
                    action="revoke",
                    origin="https://app.example",
                )
            self.assertEqual(code, 200)
            self.assertEqual(result["state"], "revoked")
            worker_rpc.assert_called_once()
            fsync.assert_not_called()
            write.assert_not_called()
            self.assertEqual(
                {path.name for path in directory.iterdir()}, names_before
            )
            self.assertEqual(list(directory.glob("*.json")), [])
            occupied = [
                path for path in directory.glob("pending-*")
                if path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
            ]
            self.assertEqual(len(occupied), 1)
            self.assertEqual(
                occupied[0].read_bytes(), x_capi._privacy_pending_record(ticket)
            )

    def test_request_safe_revoke_avoids_legacy_marker_capacity_scan(self):
        import fcntl
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            os.chmod(temporary, 0o700)
            lock_path = Path(temporary) / x_capi._PRIVACY_FENCE_LOCK_NAME
            lock_path.write_bytes(x_capi._PRIVACY_FENCE_LOCK_MAGIC)
            os.chmod(lock_path, 0o600)
            quarantine_path = (
                Path(temporary) / x_capi._PRIVACY_FENCE_QUARANTINE_NAME
            )
            quarantine_path.write_bytes(x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE)
            os.chmod(quarantine_path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                ticket = x_capi.decode_context_ticket(issued["context"])
            for index in range(x_capi._PRIVACY_FENCE_MARKER_CAP):
                marker = Path(temporary) / (f"{index:064x}.json")
                marker.write_text("{}", encoding="ascii")
                os.chmod(marker, 0o600)
            destination = Path(temporary) / (
                x_capi._hash(ticket["handle"]) + ".json"
            )
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                with patch.dict(os.environ, env, clear=True), patch.object(
                    x_capi,
                    "_request_worker_consent",
                    return_value={"v": 1, "ok": True, "state": "revoked"},
                ) as worker_rpc:
                    self.assertEqual(
                        x_capi._persist_privacy_fence_status(issued["context"]),
                        "dispatch_in_progress",
                    )
                    result, code = x_capi.update_consent(
                        context_token=issued["context"],
                        csrf_token=issued["csrf"],
                        action="revoke",
                        origin="https://app.example",
                    )
                self.assertEqual(code, 200)
                self.assertTrue(result["ok"])
                worker_rpc.assert_called_once()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertEqual(
                quarantine_path.read_bytes(), x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE
            )
            self.assertFalse(destination.exists())
            self.assertLessEqual(
                len(list(Path(temporary).iterdir())),
                x_capi._PRIVACY_FENCE_MARKER_CAP + 2,
            )

    def test_request_safe_revoke_never_uses_legacy_marker_write(self):
        import fcntl
        import tempfile

        env = self.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            os.chmod(temporary, 0o700)
            lock_path = Path(temporary) / x_capi._PRIVACY_FENCE_LOCK_NAME
            lock_path.write_bytes(x_capi._PRIVACY_FENCE_LOCK_MAGIC)
            os.chmod(lock_path, 0o600)
            quarantine_path = (
                Path(temporary) / x_capi._PRIVACY_FENCE_QUARANTINE_NAME
            )
            quarantine_path.write_bytes(x_capi._PRIVACY_FENCE_QUARANTINE_INACTIVE)
            os.chmod(quarantine_path, 0o600)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                with patch.dict(os.environ, env, clear=True), patch.object(
                    x_capi.os, "write",
                    side_effect=AssertionError("request-safe path used marker write"),
                ), patch.object(
                    x_capi,
                    "_request_worker_consent",
                    return_value={"v": 1, "ok": True, "state": "revoked"},
                ) as worker_rpc:
                    self.assertEqual(
                        x_capi._persist_privacy_fence_status(issued["context"]),
                        "dispatch_in_progress",
                    )
                    result, code = x_capi.update_consent(
                        context_token=issued["context"],
                        csrf_token=issued["csrf"],
                        action="revoke",
                        origin="https://app.example",
                    )
                self.assertEqual(code, 200)
                self.assertTrue(result["ok"])
                worker_rpc.assert_called_once()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertEqual(
                quarantine_path.read_bytes(), x_capi._PRIVACY_FENCE_QUARANTINE_ACTIVE
            )
            self.assertEqual(lock_path.read_bytes(), x_capi._PRIVACY_FENCE_LOCK_MAGIC)

    def test_consent_rpc_pool_is_bounded_and_yields_while_tasks_remain_queued(self):
        import types
        release = threading.Event()
        entered = []
        cooperative_sleeps = []

        def slow_rpc(*_args):
            entered.append(threading.get_ident())
            release.wait(timeout=1.0)
            return {"v": 1, "ok": True, "state": "revoked"}

        x_capi._reset_consent_rpc_after_fork()
        try:
            fake_gevent = types.ModuleType("gevent")
            fake_gevent.sleep = lambda seconds: (
                cooperative_sleeps.append(seconds), time.sleep(seconds)
            )[-1]
            with patch.object(
                x_capi, "_request_worker_consent_blocking", side_effect=slow_rpc
            ), patch.object(
                x_capi, "_CONSENT_RPC_WAIT_SECONDS", 0.03
            ), patch.dict(sys.modules, {"gevent": fake_gevent}):
                results = []
                jobs = [
                    threading.Thread(
                        target=lambda: results.append(
                            x_capi._request_worker_consent(
                                "revoke", "t" * 40, "c" * 43, 100.0
                            )
                        )
                    )
                    for _unused in range(x_capi._CONSENT_RPC_MAX_PENDING)
                ]
                for job in jobs:
                    job.start()
                for job in jobs:
                    job.join(timeout=0.2)
                self.assertTrue(all(not job.is_alive() for job in jobs))
                self.assertEqual(results, [None] * x_capi._CONSENT_RPC_MAX_PENDING)
                self.assertTrue(cooperative_sleeps)
                self.assertEqual(len(entered), x_capi._CONSENT_RPC_MAX_WORKERS)

                started = time.monotonic()
                saturated = x_capi._request_worker_consent(
                    "revoke", "t" * 40, "c" * 43, 100.0
                )
                self.assertIsNone(saturated)
                self.assertLess(time.monotonic() - started, 0.02)
        finally:
            release.set()
            executor = x_capi._consent_rpc_executor
            if executor is not None:
                executor.shutdown(wait=True)
            x_capi._reset_consent_rpc_after_fork()

    def test_websockify_child_uses_direct_bounded_rpc_not_a_per_fork_pool(self):
        env = self.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "_request_worker_consent"
        ) as pooled, patch.object(
            x_capi,
            "_request_worker_consent_blocking",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as direct, patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="published"
        ):
            result, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="revoke",
                origin="https://app.example",
                offload_worker_rpc=False,
            )
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "revoked")
        pooled.assert_not_called()
        direct.assert_called_once()
        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        worker = (ROOT / "axonos_gate" / "x_capi_worker.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("offload_worker_rpc=False", proxy)
        self.assertIn("listener.listen(16)", worker)
        self.assertIn("receive_batch(limit=1)", worker)

    def test_websockify_consent_route_executes_real_grant_and_gpc_revoke(self):
        """Exercise the Websockify branch without importing its optional package."""
        import ast
        import types
        from urllib.parse import urlparse

        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        do_post = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name == "do_POST"
        )
        namespace = {
            "urlparse": urlparse,
            "client_ip_for_rate_limit": lambda *_args: "192.0.2.1",
            "gpc_signal_active": security_utils.gpc_signal_active,
            "x_capi": x_capi,
            "_x_capi_privacy_rate_limiter": types.SimpleNamespace(
                allow=lambda *_args, **_kwargs: True
            ),
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[do_post], type_ignores=[])),
                "websockify_gate.py",
                "exec",
            ),
            namespace,
        )

        class Request:
            path = "/api/x-attribution/consent"
            client_address = ("192.0.2.1", 10000)

            def __init__(self, headers, body):
                self.headers = headers
                self.body = body

            def _read_bounded_json_body(self, *_args, **_kwargs):
                return self.body, None

            def _x_capi_rate_allowed(self, *_args):
                return True

            def _x_capi_privacy_rate_allowed(self, *_args):
                return True

            def _observe_request_gpc_early(self, *_args):
                return None

            def _x_capi_live_transport_allowed(self):
                return True

            def _send_json(self, code, payload, **_kwargs):
                return code, payload

        env = self.enabled_env()
        click = "officialclick1234567890"
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(landing_twclid=click)
            headers = {
                "Content-Type": "application/json",
                "Content-Length": "18",
                "Origin": "https://app.example",
                "X-AxonOS-Attribution": issued["context"],
                "X-AxonOS-CSRF": issued["csrf"],
                "X-AxonOS-Landing-Click": click,
            }
            grant_code, grant = namespace["do_POST"](
                Request(headers, {"action": "grant"})
            )
        self.assertEqual(grant_code, 200)
        self.assertEqual(grant["state"], "granted")

        gpc_headers = dict(headers)
        gpc_headers.update({
            "Sec-GPC": "1",
            "X-AxonOS-Attribution": grant["context"],
            "X-AxonOS-CSRF": grant["csrf"],
        })
        with patch.dict(os.environ, env, clear=True), patch.object(
            x_capi, "observe_privacy_signal_nonblocking", return_value=True
        ), patch.object(
            x_capi, "_request_worker_consent"
        ) as pooled, patch.object(
            x_capi,
            "_request_worker_consent_blocking",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as direct, patch.object(
            x_capi, "_persist_privacy_fence_status", return_value="published"
        ):
            revoke_code, revoked = namespace["do_POST"](
                Request(gpc_headers, {"action": "new_lifecycle"})
            )
        self.assertEqual(revoke_code, 200)
        self.assertEqual(revoked["state"], "revoked")
        self.assertTrue(revoked["gpc_applied"])
        pooled.assert_not_called()
        direct.assert_called_once()

    def test_public_gevent_handler_bounds_headers_and_only_capi_post_bodies(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        class Deadline:
            def __init__(self, seconds, callback):
                self.seconds = seconds
                self.callback = callback
                self.cancelled = False

            def kill(self, block=False):
                self.cancelled = block is False

        class FakeGevent:
            scheduled = []

            @classmethod
            def spawn_later(cls, seconds, callback):
                deadline = Deadline(seconds, callback)
                cls.scheduled.append(deadline)
                return deadline

        class BaseHandler:
            def read_requestline(self):
                return "POST /api/x-attribution/consent HTTP/1.1"

            def read_request(self, _raw):
                return True

            def handle_one_response(self):
                return "handled"

        class Connection:
            def __init__(self):
                self.closed = False

            def shutdown(self, _how):
                self.closed = True

            def close(self):
                self.closed = True

        handler_type = gate_server._deadline_websocket_handler(
            BaseHandler, FakeGevent
        )
        handler = handler_type()
        handler.socket = Connection()
        handler.close_connection = False

        self.assertIn("POST", handler.read_requestline())
        self.assertTrue(handler.read_request("request"))
        handler.environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/x-attribution/consent",
        }
        self.assertEqual(handler.handle_one_response(), "handled")
        self.assertEqual(
            [deadline.seconds for deadline in FakeGevent.scheduled],
            [10.0, 10.0, 2.0],
        )
        self.assertTrue(all(deadline.cancelled for deadline in FakeGevent.scheduled))

        body_deadline = FakeGevent.scheduled[-1]
        body_deadline.callback()
        self.assertTrue(handler.close_connection)

        FakeGevent.scheduled.clear()
        handler.environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/session/claim"}
        self.assertEqual(handler.handle_one_response(), "handled")
        self.assertEqual(FakeGevent.scheduled, [])

    def test_public_server_dependencies_are_explicit_and_fallback_is_fail_closed(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        requirements = (ROOT / "axonos_gate" / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertRegex(requirements, r"(?m)^gevent[<=>]")
        self.assertRegex(requirements, r"(?m)^gevent-websocket[<=>]")

        with patch.dict(os.environ, {}, clear=True), patch.object(
            gate_server.app, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "Refusing to start"):
                gate_server._serve_unsafe_flask_development_fallback(
                    "127.0.0.1", 5000, "test"
                )
        run.assert_not_called()

        with patch.dict(
            os.environ,
            {"GATE_ALLOW_UNSAFE_FLASK_DEVELOPMENT_SERVER": "1"},
            clear=True,
        ), patch.object(gate_server.app, "run") as run:
            gate_server._serve_unsafe_flask_development_fallback(
                "127.0.0.1", 5000, "test"
            )
        run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False
        )

        with patch.dict(
            os.environ, {"GATE_AGENT_ONLY": "true"}, clear=True
        ), patch.object(gate_server.app, "run") as run:
            gate_server._serve_unsafe_flask_development_fallback(
                "127.0.0.1", 8890, "internal agent API"
            )
        run.assert_called_once_with(
            host="127.0.0.1", port=8890, debug=False, use_reloader=False
        )

    def test_consent_rpc_fork_reset_discards_inherited_pool_state(self):
        x_capi._consent_rpc_pid = 123
        x_capi._consent_rpc_executor = MagicMock()
        x_capi._consent_rpc_slots = MagicMock()
        x_capi._reset_consent_rpc_after_fork()
        self.assertEqual(x_capi._consent_rpc_pid, 0)
        self.assertIsNone(x_capi._consent_rpc_executor)
        self.assertIsNone(x_capi._consent_rpc_slots)

    def test_gate_side_database_api_cannot_use_core_credential(self):
        with patch.dict(
            os.environ,
            {"AXGT_CHALLENGE_DB_URL": "postgresql://broad-core-secret@db/core"},
            clear=True,
        ):
            self.assertIsNone(x_capi._db_url(worker=False))
            self.assertIsNone(x_capi.get_connection(worker=False))

    def test_flask_status_is_exact_origin_gpc_authoritative_and_click_free(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = self.enabled_env()
        client = gate_server.app.test_client()
        click = "officialclick1234567890"
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=True
        ):
            issued = client.get(
                "/api/x-attribution/status",
                base_url="https://app.example",
                headers={
                    "Origin": "https://app.example",
                    "X-AxonOS-Landing-Click": click,
                },
            )
            gpc = client.get(
                "/api/x-attribution/status",
                base_url="https://app.example",
                headers={
                    "Origin": "https://app.example",
                    "Sec-GPC": "malformed",
                    "X-AxonOS-Attribution": issued.get_json()["context"],
                },
            )
            hostile = client.get(
                "/api/x-attribution/status",
                base_url="https://app.example",
                headers={
                    "Origin": "https://evil-app.example",
                    "X-AxonOS-Attribution": issued.get_json()["context"],
                },
            )
        self.assertEqual(issued.status_code, 200)
        self.assertNotIn(click, issued.get_data(as_text=True))
        self.assertEqual(gpc.get_json()["state"], "revocation_required")
        self.assertTrue(gpc.get_json()["gpc_applied"])
        self.assertEqual(
            issued.headers.get("Access-Control-Allow-Origin"),
            "https://app.example",
        )
        self.assertNotIn("Access-Control-Allow-Origin", hostile.headers)
        for response in (issued, gpc, hostile):
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", response.headers.get("Content-Security-Policy", ""))
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_flask_live_mode_is_unavailable_and_still_requires_https(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = self.enabled_env("live")
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=True
        ), patch.object(gate_server.x_capi, "update_consent") as update:
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            response = gate_server.app.test_client().post(
                "/api/x-attribution/consent",
                json={"action": "grant"},
                headers={
                    "Origin": "https://app.example",
                    "X-AxonOS-Attribution": "A" * 64,
                    "X-AxonOS-CSRF": "c" * 43,
                },
            )
        self.assertEqual(issued["state"], "unavailable")
        self.assertNotIn("context", issued)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "HTTPS is required")
        update.assert_not_called()

    def test_deposit_hook_is_post_commit_nonblocking_and_paid_only(self):
        from axonos_gate import deposit_ledger
        capi = MagicMock()
        capi.MILESTONE_DEPOSIT_COMPLETED = "deposit_completed"
        capi.production_chain_eligible.return_value = True
        capi.wallet_is_campaign_eligible.return_value = True
        capi.emit_event_nonblocking.return_value = True
        with patch.dict(os.environ, {"AXGT_CHAIN_ID": "1"}, clear=True), patch.object(
            deposit_ledger, "_x_capi", capi
        ):
            emitted = deposit_ledger._emit_paid_deposit_conversion(
                "0x" + "1" * 40,
                "0x" + "a" * 64,
                "onchain",
                "eth",
                7,
                60.0,
                "0.25",
                "context",
                123.456,
                1,
            )
            self.assertTrue(emitted)
            capi.emit_event_nonblocking.assert_called_once_with(
                context_token="context",
                wallet_address="0x" + "1" * 40,
                milestone="deposit_completed",
                source_key="0x" + "a" * 64,
                event_timestamp_ms=123456,
                allow_context_binding=True,
                credit_source="onchain",
                payment_rail="eth",
                chain_id=1,
            )
            capi.emit_event_nonblocking.reset_mock()
            self.assertFalse(deposit_ledger._emit_paid_deposit_conversion(
                "0x" + "1" * 40,
                "test-credit",
                "test_credit",
                "eth",
                7,
                60.0,
                "0.25",
                "context",
                123.456,
                1,
            ))
            capi.emit_event_nonblocking.assert_not_called()

    def test_deposit_credit_closes_financial_connection_before_optional_emit(self):
        from axonos_gate import deposit_ledger
        wallet = "0x" + "1" * 40
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (60.0,)
        ordered = []
        conn.commit.side_effect = lambda: ordered.append("commit")
        conn.close.side_effect = lambda: ordered.append("close")
        capi = MagicMock()
        capi.MILESTONE_DEPOSIT_COMPLETED = "deposit_completed"
        capi.wallet_is_campaign_eligible.return_value = True
        capi.production_chain_eligible.return_value = True

        def emitted(*_args, **kwargs):
            self.assertTrue(conn.commit.called)
            self.assertTrue(conn.close.called)
            self.assertEqual(kwargs["event_timestamp_ms"], 300000)
            ordered.append("emit")
            raise RuntimeError("optional emitter failed")

        capi.emit_event_nonblocking.side_effect = emitted

        with patch.dict(os.environ, {"AXGT_CHAIN_ID": "1"}, clear=True), patch.object(
            deposit_ledger, "init_once", return_value=True
        ), patch.object(
            deposit_ledger, "_get_connection", return_value=conn
        ), patch.object(
            deposit_ledger, "_x_capi", capi
        ), patch.object(
            deposit_ledger.time, "time", side_effect=(100.0, 200.0, 300.0)
        ):
            ok, remaining, error = deposit_ledger.credit_eth_deposit(
                wallet, Decimal("0.25"),
                60.0, "0x" + "a" * 64, 7, 1,
                attribution_context="context",
            )
        self.assertTrue(ok)
        self.assertEqual(remaining, 60.0)
        self.assertIsNone(error)
        self.assertEqual(ordered, ["commit", "close", "emit"])

    def test_campaign_subject_chain_and_noop_exclusions_fail_closed(self):
        from axonos_gate import deposit_ledger, session_manager
        wallet = "0x" + "1" * 40
        guest = "0x6775657374" + "2" * 30
        capi = MagicMock()
        capi.MILESTONE_DEPOSIT_COMPLETED = "deposit_completed"
        capi.MILESTONE_SESSION_STARTED = "session_started"
        capi.production_chain_eligible.return_value = True
        capi.wallet_is_campaign_eligible.return_value = True
        capi.emit_event_nonblocking.return_value = True

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(x_capi.wallet_is_campaign_eligible(wallet))
            self.assertFalse(x_capi.wallet_is_campaign_eligible(guest))
            self.assertFalse(x_capi.wallet_is_campaign_eligible("0x" + "0" * 40))
        for env_name in (
            "AXONOS_TEST_CREDIT_WALLETS",
            "AXONOS_WHITELISTED_WALLETS",
            "AXONOS_GUEST_INVITE_MINTERS",
            "AXGT_REVENUE_WALLET",
        ):
            with patch.dict(os.environ, {env_name: wallet}, clear=True):
                self.assertFalse(x_capi.wallet_is_campaign_eligible(wallet))
        with patch.dict(
            os.environ, {"X_CAPI_EXCLUDED_WALLETS": "not-a-wallet"}, clear=True
        ):
            self.assertFalse(x_capi.wallet_is_campaign_eligible(wallet))
            self.assertIn(
                "X CAPI wallet exclusion sources must contain valid wallet addresses",
                x_capi.load_config().errors,
            )

        with patch.dict(os.environ, {}, clear=True), patch.object(
            deposit_ledger, "_x_capi", capi
        ):
            self.assertFalse(deposit_ledger._emit_paid_deposit_conversion(
                wallet, "tx", "onchain", "eth", 7, 60.0, "0.25",
                "context", 123.0, 0,
            ))
            self.assertFalse(deposit_ledger._emit_paid_deposit_conversion(
                wallet, "0x" + "a" * 64, "onchain", "eth", True, 60.0,
                "0.25", "context", 123.0, 1,
            ))
        capi.production_chain_eligible.assert_not_called()
        capi.emit_event_nonblocking.assert_not_called()

        with patch.dict(
            os.environ, {"AXGT_SESSION_LAUNCHER_MODE": "noop"}, clear=True
        ), patch.object(session_manager, "_x_capi", capi):
            self.assertFalse(session_manager._emit_session_started_nonblocking(
                "context", wallet, 77, 123000,
            ))
        capi.emit_event_nonblocking.assert_not_called()

        capi.wallet_is_campaign_eligible.side_effect = RuntimeError("optional failure")
        with patch.dict(
            os.environ, {"AXGT_SESSION_LAUNCHER_MODE": "http"}, clear=True
        ), patch.object(session_manager, "_x_capi", capi):
            self.assertFalse(session_manager._emit_session_started_nonblocking(
                "context", wallet, 77, 123000,
            ))
        capi.emit_event_nonblocking.assert_not_called()

    def test_session_hook_cannot_bind_and_commit_confirmation_is_exact(self):
        from axonos_gate import session_manager
        wallet = "0x" + "1" * 40
        capi = MagicMock()
        capi.MILESTONE_SESSION_STARTED = "session_started"
        capi.wallet_is_campaign_eligible.return_value = True
        capi.emit_event_nonblocking.return_value = True
        with patch.dict(
            os.environ,
            {
                "AXGT_SESSION_LAUNCHER_MODE": "http",
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
            },
            clear=True,
        ), patch.object(session_manager, "_x_capi", capi):
            self.assertTrue(session_manager._emit_session_started_nonblocking(
                "context", wallet, 77, 123000,
            ))
        capi.emit_event_nonblocking.assert_called_once_with(
            context_token="context",
            wallet_address=wallet,
            milestone="session_started",
            source_key="77",
            event_timestamp_ms=123000,
            allow_context_binding=False,
        )

        cur = MagicMock()
        cur.fetchone.return_value = ("allocated", "container")
        self.assertTrue(session_manager._spawn_finalization_is_committed(cur, 77, "container"))
        self.assertIn("FOR UPDATE", cur.execute.call_args.args[0])
        self.assertEqual(cur.execute.call_args.args[1], (77,))
        cur.fetchone.return_value = None
        self.assertFalse(session_manager._spawn_finalization_is_committed(cur, 77, "container"))

    def test_legacy_shared_desktop_never_emits_session_started(self):
        from axonos_gate import session_manager

        capi = MagicMock()
        capi.MILESTONE_SESSION_STARTED = "session_started"
        capi.wallet_is_campaign_eligible.return_value = True
        capi.emit_event_nonblocking.return_value = True
        with patch.dict(
            os.environ,
            {
                "AXGT_SESSION_LAUNCHER_MODE": "http",
                "AXGT_USER_CONTAINER_ENABLED": "false",
                "AXGT_MULTI_SESSION_ENABLED": "false",
            },
            clear=True,
        ), patch.object(session_manager, "_x_capi", capi):
            self.assertFalse(
                session_manager._emit_session_started_nonblocking(
                    "context", "0x" + "1" * 40, 77, 123000
                )
            )
        capi.emit_event_nonblocking.assert_not_called()

        source = (ROOT / "axonos_gate" / "session_manager.py").read_text(
            encoding="utf-8"
        )
        legacy = source.split(
            "# Legacy single-session mode", 1
        )[1].split("def heartbeat", 1)[0]
        self.assertNotIn("_emit_session_started_nonblocking(", legacy)

    def test_ambiguous_session_commit_is_confirmed_before_cleanup_or_emit(self):
        from axonos_gate import session_manager
        wallet = "0x1234567890123456789012345678901234567890"
        primary = MagicMock()
        primary_cur = MagicMock()
        primary_cur.__enter__.return_value = primary_cur
        primary_cur.fetchone.side_effect = [(73,), ("allocated", "container-id")]
        primary.cursor.return_value = primary_cur
        finalizer = MagicMock()
        finalizer_cur = MagicMock()
        finalizer_cur.__enter__.return_value = finalizer_cur
        finalizer_cur.rowcount = 1
        finalizer.cursor.return_value = finalizer_cur
        finalizer.commit.side_effect = RuntimeError("connection lost during commit")
        capi = MagicMock()
        capi.MILESTONE_SESSION_STARTED = "session_started"
        capi.wallet_is_campaign_eligible.return_value = True
        capi.emit_event_nonblocking.side_effect = RuntimeError("optional emitter failed")

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
                "WEBRTC_ENABLED": "true",
            },
            clear=False,
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", side_effect=(primary, finalizer)
        ), patch.object(
            session_manager, "_run_stale_session_maintenance_locked"
        ), patch.object(
            session_manager, "_get_active_rows", return_value=[]
        ), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_prepaid_credit_allows_profile", return_value=(True, None)
        ), patch.object(
            session_manager, "_provisioned_storage_gb_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_choose_allocation", return_value=[0]
        ), patch.object(
            session_manager, "_issue_webrtc_agent_capability", return_value="capability"
        ), patch.object(
            session_manager,
            "_spawn_session_container",
            return_value=(True, "container-id", None),
        ), patch.object(
            session_manager, "_cleanup_session_container"
        ) as cleanup, patch.object(session_manager, "_x_capi", capi):
            result = session_manager.try_claim_session(
                wallet, "small", attribution_context="context"
            )

        self.assertTrue(result["granted"])
        cleanup.assert_not_called()
        capi.emit_event_nonblocking.assert_called_once()
        finalizer.rollback.assert_called_once()
        self.assertTrue(
            any(
                "allocation_status = 'allocating'" in call.args[0]
                and "container_id IS NULL" in call.args[0]
                for call in finalizer_cur.execute.call_args_list
            )
        )

    def test_unresolved_session_commit_is_retryable_without_cleanup_or_conversion(self):
        from axonos_gate import session_manager
        wallet = "0x1234567890123456789012345678901234567890"
        primary = MagicMock()
        primary_cur = MagicMock()
        primary_cur.__enter__.return_value = primary_cur
        primary_cur.fetchone.side_effect = [(73,), RuntimeError("primary unavailable")]
        primary.cursor.return_value = primary_cur
        finalizer = MagicMock()
        finalizer_cur = MagicMock()
        finalizer_cur.__enter__.return_value = finalizer_cur
        finalizer_cur.rowcount = 1
        finalizer.cursor.return_value = finalizer_cur
        finalizer.commit.side_effect = RuntimeError("connection lost during commit")
        capi = MagicMock()

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
                "WEBRTC_ENABLED": "true",
            },
            clear=False,
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", side_effect=(primary, finalizer)
        ), patch.object(
            session_manager, "_run_stale_session_maintenance_locked"
        ), patch.object(
            session_manager, "_get_active_rows", return_value=[]
        ), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_prepaid_credit_allows_profile", return_value=(True, None)
        ), patch.object(
            session_manager, "_provisioned_storage_gb_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_choose_allocation", return_value=[0]
        ), patch.object(
            session_manager, "_issue_webrtc_agent_capability", return_value="capability"
        ), patch.object(
            session_manager, "_spawn_session_container",
            return_value=(True, "container-id", None),
        ), patch.object(
            session_manager, "_cleanup_session_container"
        ) as cleanup, patch.object(session_manager, "_x_capi", capi):
            result = session_manager.try_claim_session(
                wallet, "small", attribution_context="context"
            )

        self.assertFalse(result["granted"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["allocation_status"], "allocating")
        cleanup.assert_not_called()
        capi.emit_event_nonblocking.assert_not_called()

    def test_no_credit_wallet_auth_still_records_honest_wallet_milestone(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server
        client = gate_server.app.test_client()
        with patch.object(gate_server, "verify_signed_challenge", return_value=True), patch.object(
            gate_server, "get_wallet_access_status",
            return_value={"verified": False, "remaining_minutes": 0.0},
        ), patch.object(
            gate_server, "_issue_gate_auth_token", return_value=("token", 300)
        ), patch.object(
            gate_server.x_capi, "wallet_is_campaign_eligible", return_value=True
        ), patch.object(
            gate_server.x_capi, "business_context_or_none", return_value="h" * 40
        ), patch.object(
            gate_server.x_capi,
            "emit_event_nonblocking",
            return_value=True,
            create=True,
        ) as emit:
            response = client.post(
                "/api/auth/verify-wallet",
                headers={"X-AxonOS-Attribution": "h" * 40},
                json={
                    "wallet_address": "0x" + "1" * 40,
                    "message": "challenge", "signature": "0xsig",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["verified"])
        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["context_token"], "h" * 40)
        self.assertEqual(emit.call_args.kwargs["wallet_address"], "0x" + "1" * 40)
        self.assertEqual(emit.call_args.kwargs["milestone"], x_capi.MILESTONE_WALLET_VERIFIED)
        self.assertTrue(emit.call_args.kwargs["allow_context_binding"])

    def test_optional_wallet_emitter_failure_cannot_change_auth_success(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server
        client = gate_server.app.test_client()
        with patch.object(
            gate_server, "verify_signed_challenge", return_value=True
        ), patch.object(
            gate_server,
            "get_wallet_access_status",
            return_value={"verified": True, "remaining_minutes": 60.0},
        ), patch.object(
            gate_server, "_issue_gate_auth_token", return_value=("token", 300)
        ), patch.object(
            gate_server.x_capi, "wallet_is_campaign_eligible", return_value=True
        ), patch.object(
            gate_server.x_capi, "business_context_or_none", return_value="h" * 40
        ), patch.object(
            gate_server.x_capi,
            "emit_event_nonblocking",
            side_effect=RuntimeError("optional emitter failed"),
        ) as emit:
            response = client.post(
                "/api/auth/verify-wallet",
                headers={"X-AxonOS-Attribution": "h" * 40},
                json={
                    "wallet_address": "0x" + "1" * 40,
                    "message": "challenge",
                    "signature": "0xsig",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["verified"])
        emit.assert_called_once()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "conversion_timestamp_ms": 1_789_387_200_000,
            "event_id": "exact-event",
            "twclid": "click_12345678",
            "conversion_id": "00000000-0000-4000-8000-000000000001",
        }

    def test_payload_is_strict_four_field_allowlist(self):
        payload = x_capi_worker.build_payload(self.job)
        conversion = payload["conversions"][0]
        self.assertEqual(
            set(conversion),
            {"conversion_time", "event_id", "identifiers", "conversion_id"},
        )
        self.assertEqual(conversion["conversion_time"], "2026-09-14T12:00:00.000Z")
        self.assertEqual(conversion["identifiers"], [{"twclid": "click_12345678"}])
        serialized = json.dumps(payload)
        for forbidden in (
            "wallet", "transaction", "session_id", "email", "phone",
            "ip_address", "user_agent", "twpid", "value", "currency",
            "gpu", "profile", "url", "referrer",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_success_requires_documented_processed_shape(self):
        response = x_capi_worker.TransportResponse(
            200, {}, b'{"data":{"conversions_processed":1,"debug_id":"safe"}}'
        )
        self.assertEqual(x_capi_worker.classify_response(response, 100)["action"], "accepted")
        for body in (b"", b"not-json", b'{"ok":true}', b'{"data":{"conversions_processed":0}}'):
            result = x_capi_worker.classify_response(
                x_capi_worker.TransportResponse(200, {}, body), 100
            )
            self.assertEqual(result["code"], "unknown_success_body")

    def test_http_failure_classes_are_bounded(self):
        self.assertEqual(x_capi_worker.classify_response(x_capi_worker.TransportResponse(401, {}, b""), 100)["action"], "pause")
        rate = x_capi_worker.classify_response(x_capi_worker.TransportResponse(429, {"Retry-After": "999999"}, b""), 100)
        self.assertEqual(rate["action"], "retry")
        self.assertEqual(rate["retry_at"], 3700)
        self.assertEqual(x_capi_worker.classify_response(x_capi_worker.TransportResponse(503, {}, b""), 100)["action"], "retry")
        self.assertEqual(x_capi_worker.classify_response(x_capi_worker.TransportResponse(302, {}, b""), 100)["code"], "redirect_rejected")
        self.assertEqual(x_capi_worker.classify_response(x_capi_worker.TransportResponse(400, {}, b""), 100)["action"], "permanent")

    def test_auth_failure_pauses_without_discarding_job_and_clear_can_retry(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        job = {
            "attempt_count": 0,
            "conversion_id": "job-id",
            "lease_owner": "worker-a",
            "lease_token": "fence-a",
        }
        x_capi_worker.finish_job(
            conn, job,
            {"action": "pause", "code": "authentication_failed"},
            100.0, rng=lambda: 0.5,
        )
        statements = [call.args[0] for call in cur.execute.call_args_list]
        retry_sql = next(sql for sql in statements if "SET status='retrying'" in sql)
        self.assertIn("status='leased'", retry_sql)
        self.assertIn("lease_owner=%s AND lease_token=%s", retry_sql)
        self.assertTrue(any("x_capi_worker_state" in sql for sql in statements))

    def test_maintenance_expires_dry_run_and_stale_policy_rows(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        x_capi_worker.expire_and_cleanup(
            conn, 1000.0, 24, "x-capi-v2", retention_days=30
        )
        statements = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any("policy_version<>%s" in sql for sql in statements))
        expiry_sql = next(sql for sql in statements if "event_too_old" in sql)
        self.assertIn("'dry_run'", expiry_sql)

    def test_stale_event_is_rejected_again_immediately_before_dispatch(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (
            1_000, 999_999.0, "granted", "live", "x-capi-v1", 1,
            "a" * 64, 999_999.0, "click_12345678", 999_999.0,
        )
        stale_job = dict(
            self.job,
            conversion_timestamp_ms=1_000,
            pixel_id="source",
            context_id="ctx",
            consent_policy_epoch=1,
            consent_audience_scope="a" * 64,
            lease_owner="worker-a",
            lease_token="fence-a",
        )
        with patch.object(
            x_capi_worker, "_config_guard_on_cursor", return_value=True
        ), patch.object(
            x_capi_worker, "_hash_key_fingerprint", return_value="h" * 64
        ), patch.object(
            x_capi_worker, "_context_key_fingerprint", return_value="c" * 64
        ):
            allowed = x_capi_worker.dispatch_still_allowed(
                conn, stale_job, now=200_000.0,
                policy_version="x-capi-v1", max_event_age_hours=24,
            )
        self.assertFalse(allowed)
        sql = cur.execute.call_args.args[0]
        self.assertIn("lease_owner=%s AND o.lease_token=%s", sql)

    def test_concrete_live_transport_is_structurally_blocked(self):
        fake_requests = MagicMock()
        with patch.dict("sys.modules", {"requests": fake_requests}):
            transport = x_capi_worker.RequestsTransport()
        self.assertIsNone(transport._session)
        with self.assertRaisesRegex(
            RuntimeError, x_capi_worker.LIVE_DELIVERY_BLOCK_REASON
        ):
            transport.send("source", "dedicated-secret", x_capi_worker.build_payload(self.job))
        fake_requests.Session.assert_not_called()

    def test_blocked_worker_has_no_http_client_or_response_buffer_path(self):
        source = (ROOT / "axonos_gate" / "x_capi_worker.py").read_text(
            encoding="utf-8"
        )
        image = (ROOT / "docker" / "x-capi-worker" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertFalse(hasattr(x_capi_worker, "MAX_RESPONSE_BYTES"))
        self.assertNotIn("import requests", source)
        self.assertNotIn(".post(", source)
        self.assertNotIn("iter_content", source)
        self.assertNotIn("requests", image.lower())

    def test_timeout_becomes_bounded_retry_without_losing_stable_job(self):
        job = dict(
            self.job,
            pixel_id="source",
            context_id="ctx",
            attempt_count=0,
            consent_policy_epoch=1,
            consent_audience_scope="a" * 64,
            lease_owner="worker-a",
            lease_token="fence-a",
        )
        conn = MagicMock()
        transport = MagicMock()
        transport.send.side_effect = TimeoutError("synthetic timeout")
        cfg = MagicMock(
            mode="live", producer_ready=True,
            max_event_age_hours=24, policy_version="x-capi-v1",
            policy_epoch=1, audience_scope="a" * 64,
            context_limit=10_000, deployment_id="test-deployment",
        )
        with patch.dict(
            os.environ, {"X_CAPI_ALLOW_TEST_SECRETS": "1"}, clear=True
        ), patch.object(
            x_capi, "load_config", return_value=cfg
        ), patch.object(
            x_capi, "_db_url", return_value="postgresql://isolated/test"
        ), patch.object(
            x_capi_worker, "_worker_db_target_is_isolated", return_value=True
        ), patch.object(
            x_capi_worker, "read_token", return_value=("dedicated-token", None)
        ), patch.object(
            x_capi, "get_connection", return_value=conn
        ), patch.object(
            x_capi_worker, "_configure_connection"
        ), patch.object(
            x_capi_worker, "_schema_ready", return_value=True
        ), patch.object(
            x_capi_worker, "verify_config_guard", return_value=True
        ), patch.object(
            x_capi_worker, "enforce_config_guard", return_value=True
        ), patch.object(
            x_capi_worker, "expire_and_cleanup"
        ), patch.object(
            x_capi_worker, "drain_consent"
        ), patch.object(
            x_capi_worker, "drain_ingest"
        ), patch.object(
            x_capi, "_context_cipher", return_value=(MagicMock(), None)
        ), patch.object(
            x_capi, "keyed_internal_hash", return_value="hash"
        ), patch.object(
            x_capi_worker, "_is_paused", return_value=False
        ), patch.object(
            x_capi_worker, "claim_job", return_value=job
        ), patch.object(
            x_capi_worker, "begin_dispatch", return_value="allowed"
        ), patch.object(x_capi_worker, "finish_job") as finish:
            finish.return_value = True
            outcome = x_capi_worker.run_once(
                transport, now_fn=lambda: 1_789_387_200.0, rng=lambda: 0.5
            )
        self.assertEqual(outcome, "retry")
        result = finish.call_args.args[2]
        self.assertEqual(result, {"action": "retry", "code": "transport_failure"})
        self.assertEqual(job["conversion_id"], self.job["conversion_id"])

    def test_token_source_rejects_environment_and_permissive_file(self):
        with patch.dict(os.environ, {"X_CAPI_ACCESS_TOKEN": "never-use-this"}, clear=True):
            token, error = x_capi_worker.read_token()
        self.assertIsNone(token)
        self.assertIn("prohibited", error)
        fake_stat = MagicMock(
            st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1
        )
        with patch.dict(os.environ, {}, clear=True), patch.object(
            x_capi_worker, "_token_path", return_value="/run/secrets/token"
        ), patch.object(os, "open", return_value=99), patch.object(
            os, "fstat", return_value=fake_stat
        ), patch.object(os, "close"):
            token, error = x_capi_worker.read_token()
        self.assertIsNone(token)
        self.assertIn("0600", error)

    def test_crash_retry_preserves_conversion_id_and_event_timestamp(self):
        first = x_capi_worker.build_payload(self.job)
        # Simulate remote acceptance followed by process loss: no local ack
        # mutates the durable job, so the recovered lease builds the same body.
        recovered = x_capi_worker.build_payload(dict(self.job))
        self.assertEqual(first, recovered)


class PrivacyBoundaryTests(unittest.TestCase):
    def test_access_log_redaction_covers_click_and_handoff(self):
        raw = "GET /?twclid=secret-click&x_capi_handoff=opaque&keep=yes HTTP/1.1"
        safe = security_utils.redact_terminal_websocket_query(raw)
        self.assertNotIn("secret-click", safe)
        self.assertNotIn("opaque", safe)
        self.assertIn("keep=yes", safe)

    def test_frontend_scrubs_before_resources_and_has_no_x_network_dependency(self):
        html = (ROOT / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")
        script = (ROOT / "novnc-theme" / "app" / "x-attribution.js").read_text(encoding="utf-8")
        self.assertLess(html.index("canonicalAttributionKey"), html.index('<link rel="icon"'))
        self.assertIn("history.replaceState", html)
        self.assertNotIn("localStorage.setItem", script)
        self.assertNotIn("ads-api.x.com", script)
        self.assertNotIn("platform.twitter.com", script)
        self.assertNotIn("twq(", script)
        self.assertIn("!gpcActive && !revocationPending", script)
        self.assertIn("setRevocationPending(true)", script)
        fetch_targets = [line for line in script.splitlines() if "fetch(" in line]
        self.assertTrue(fetch_targets)
        self.assertTrue(all("/api/x-attribution/" in line for line in fetch_targets))

    def test_launcher_forbids_every_x_and_worker_secret_name(self):
        from axonos_gate import session_launcher_service
        protected = {
            "X_CAPI_ACCESS_TOKEN", "X_CAPI_ACCESS_TOKEN_FILE", "X_CAPI_DB_URL",
            "X_CAPI_DB_URL_FILE", "X_CAPI_MODE", "X_CAPI_PIXEL_ID",
            "X_CAPI_EVENT_DEPOSIT_COMPLETED", "X_CAPI_EVENT_SESSION_STARTED",
            "X_CAPI_EVENT_WALLET_VERIFIED", "X_CAPI_ALLOWED_ORIGIN",
            "X_CAPI_CONTEXT_KEY", "X_CAPI_CONTEXT_KEY_FILE",
            "X_CAPI_HASH_KEY", "X_CAPI_HASH_KEY_FILE",
            "X_CAPI_CONFIG_GUARD_FILE", "X_CAPI_RUNTIME_HOST_DIR",
            "X_CAPI_PRIVACY_FENCE_DIR", "X_CAPI_ALLOW_TEST_PRIVACY_FENCE",
            "X_CAPI_PRIVACY_GLOBAL_RATE_LIMIT_PER_MIN", "X_CAPI_TEST_DB_URL",
            "X_CAPI_WORKER_DB_ROLE",
        }
        self.assertTrue(protected.issubset(session_launcher_service._FORBIDDEN_SESSION_ENV_NAMES))
        with patch.dict(os.environ, {"AXGT_HOST_SESSION_ENV_PASSTHROUGH": ",".join(protected)}, clear=True):
            self.assertEqual(session_launcher_service._env_passthrough_names(), [])

    def test_worker_is_small_isolated_and_secret_file_only(self):
        compose = (ROOT / "docker-compose.x-capi.yml").read_text(encoding="utf-8")
        worker = compose.split("  x-capi-worker:", 1)[1].split("\nnetworks:", 1)[0]
        image = (ROOT / "docker" / "x-capi-worker" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn('profiles: ["x-capi"]', worker)
        self.assertIn("- x_capi_db", worker)
        self.assertNotIn("x_capi_egress", worker)
        self.assertNotIn("- axonos_control", worker)
        self.assertNotIn("ports:", worker)
        self.assertNotIn("env_file:", worker)
        self.assertNotIn("docker.sock", worker)
        self.assertIn("read_only: true", worker)
        self.assertIn('cap_drop: ["ALL"]', worker)
        self.assertNotIn("X_CAPI_ACCESS_TOKEN_FILE", worker)
        self.assertNotIn("x_capi_access_token", worker)
        self.assertNotIn("X_CAPI_ACCESS_TOKEN:", worker)
        self.assertIn("FROM python:3.11-slim", image)
        self.assertIn("USER 10001:10001", image)
        self.assertNotIn("requests", image.lower())

    def test_both_frontends_and_authoritative_session_finalizer_are_hooked(self):
        gate = (ROOT / "axonos_gate" / "gate_server.py").read_text(encoding="utf-8")
        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(encoding="utf-8")
        sessions = (ROOT / "axonos_gate" / "session_manager.py").read_text(encoding="utf-8")
        for source in (gate, proxy):
            self.assertIn("emit_event_nonblocking", source)
            self.assertNotIn("bind_wallet_and_enqueue", source)
            self.assertIn('X-AxonOS-Attribution', source)
            self.assertIn("attribution_context=", source)
            self.assertIn("business_context_or_none", source)
            self.assertIn('gpc_signal_active', source)
        allocated = sessions.index("SET container_id = %s, allocation_status = 'allocated'")
        commit = sessions.index("conn2.commit()", allocated)
        emit = sessions.index("_emit_session_started_nonblocking(", allocated)
        self.assertLess(allocated, commit)
        self.assertLess(commit, emit)
        self.assertNotIn("enqueue_on_cursor", sessions)

    def test_flask_bind_turns_gpc_into_durable_revoke_before_wallet_binding(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        client = gate_server.app.test_client()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server.x_capi,
            "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as revoke, patch.object(
            gate_server.x_capi,
            "_persist_privacy_fence_status",
            return_value="published",
        ), patch.object(
            gate_server, "_x_capi_privacy_rate_limiter"
        ) as per_context, patch.object(
            gate_server, "_require_auth_token"
        ) as require_auth, patch.object(
            gate_server, "_bind_attribution_nonblocking"
        ) as bind:
            per_context.allow.return_value = True
            response = client.post(
                "/api/x-attribution/bind",
                base_url="https://app.example",
                json={"wallet_address": "0x" + "1" * 40},
                headers={
                    "Origin": "https://app.example",
                    "Sec-GPC": "1",
                    "X-AxonOS-Attribution": issued["context"],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "revoked")
        self.assertTrue(response.get_json()["gpc_applied"])
        revoke.assert_called_once()
        require_auth.assert_not_called()
        bind.assert_not_called()

    def test_authenticated_bind_never_synthesizes_wallet_verified_conversion(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        click = "officialclick1234567890"
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(landing_twclid=click)
            granted, code = x_capi.update_consent(
                context_token=issued["context"],
                csrf_token=issued["csrf"],
                action="grant",
                landing_twclid=click,
                origin="https://app.example",
            )
        self.assertEqual(code, 200)
        wallet = "0x" + "1" * 40
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=True
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server, "_bind_attribution_nonblocking", return_value=True
        ) as bind, patch.object(
            gate_server, "_emit_wallet_verified_nonblocking"
        ) as wallet_event:
            response = gate_server.app.test_client().post(
                "/api/x-attribution/bind",
                base_url="https://app.example",
                json={"wallet_address": wallet},
                headers={
                    "Origin": "https://app.example",
                    "X-AxonOS-Attribution": granted["context"],
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"ok": True, "accepted": True})
        bind.assert_called_once_with(granted["context"], wallet)
        wallet_event.assert_not_called()

        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        bind_branch = proxy.split(
            "if ponly == '/api/x-attribution/bind':", 1
        )[1].split("if ponly.startswith('/api/files/')", 1)[0]
        self.assertIn("_bind_attribution_nonblocking(context_token, wallet)", bind_branch)
        self.assertNotIn("_emit_wallet_verified_nonblocking", bind_branch)

    def test_gpc_bind_hint_limit_is_advisory_and_allows_prior_origin_cors(self):
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_privacy_rate_limiter"
        ) as per_context, patch.object(
            gate_server.x_capi, "observe_privacy_signal_nonblocking", return_value=True
        ) as fence, patch.object(
            gate_server.x_capi, "_persist_privacy_fence_status",
            return_value="published",
        ), patch.object(
            gate_server.x_capi, "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ) as worker_rpc:
            per_context.allow.return_value = False
            response = gate_server.app.test_client().post(
                "/api/x-attribution/bind",
                base_url="https://old.example",
                json={"wallet_address": "0x" + "1" * 40},
                headers={
                    "Origin": "https://old.example",
                    "Sec-GPC": "1",
                    "X-AxonOS-Attribution": issued["context"],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "revoked")
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "https://old.example",
        )
        fence.assert_called_once_with(issued["context"], emit_hint=False)
        worker_rpc.assert_called_once()
        per_context.allow.assert_called_once()

        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        bind_branch = proxy.split(
            "if ponly == '/api/x-attribution/bind':", 1
        )[1].split("if ponly.startswith('/api/files/')", 1)[0]
        self.assertIn("privacy_bearer_rate_key", bind_branch)
        self.assertIn("emit_hint=self._x_capi_privacy_rate_allowed", bind_branch)
        options = proxy.split("def do_OPTIONS", 1)[1].split("def do_GET", 1)[0]
        self.assertIn('"/api/x-attribution/bind"', options)

    def test_off_mode_status_with_capability_is_still_rate_limited(self):
        from axonos_gate import gate_server

        with patch.dict(os.environ, {"X_CAPI_MODE": "off"}, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=False
        ) as allowed:
            response = gate_server.app.test_client().get(
                "/api/x-attribution/status",
                headers={"X-AxonOS-Attribution": "h" * 40},
            )
        self.assertEqual(response.status_code, 429)
        allowed.assert_called_once_with("status")

    def test_gpc_status_fixed_fence_precedes_ordinary_rate_limit(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=False
        ) as allowed, patch.object(
            gate_server, "_x_capi_privacy_rate_allowed", return_value=True
        ), patch.object(
            gate_server.x_capi, "observe_privacy_signal_nonblocking", return_value=True
        ) as revoke:
            response = gate_server.app.test_client().get(
                "/api/x-attribution/status",
                base_url="https://app.example",
                headers={
                    "Origin": "https://app.example",
                    "Sec-GPC": "1",
                    "X-AxonOS-Attribution": issued["context"],
                },
            )
        self.assertEqual(response.status_code, 429)
        revoke.assert_called_once_with(issued["context"], emit_hint=True)
        allowed.assert_called_once_with("status")

        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        status_branch = proxy.split(
            "if request_path == '/api/x-attribution/status':", 1
        )[1].split("if _up_cfg(self.path).path in ('/telemetry'", 1)[0]
        self.assertIn(
            "emit_hint=self._x_capi_privacy_rate_allowed", status_branch
        )
        self.assertNotIn(
            "if not self._x_capi_privacy_rate_allowed", status_branch
        )

    def test_route_rejects_oversized_attribution_headers_before_decode(self):
        from axonos_gate import gate_server

        with patch.dict(os.environ, {"X_CAPI_MODE": "off"}, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed"
        ) as allowed, patch.object(
            gate_server.x_capi, "decode_context_ticket"
        ) as decode:
            response = gate_server.app.test_client().get(
                "/api/x-attribution/status",
                headers={"X-AxonOS-Attribution": "h" * 2049},
            )
        self.assertEqual(response.status_code, 400)
        allowed.assert_not_called()
        decode.assert_not_called()

    def test_fully_configured_live_mode_cannot_mint_or_emit(self):
        live = ConfigTests.enabled_env("live")
        with patch.dict(os.environ, live, clear=True), patch.object(
            x_capi, "_send_local_envelope"
        ) as send:
            config = x_capi.config_status()
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
            emitted = x_capi.emit_event_nonblocking(
                context_token="A" * 64,
                wallet_address="0x" + "1" * 40,
                milestone=x_capi.MILESTONE_WALLET_VERIFIED,
                source_key="0x" + "1" * 40,
                allow_context_binding=True,
            )
        self.assertFalse(config["producer_ready"])
        self.assertIn(x_capi.LIVE_MODE_UNAVAILABLE_REASON, config["errors"])
        self.assertEqual(issued["state"], "unavailable")
        self.assertTrue(issued["configuration_error"])
        self.assertNotIn("context", issued)
        self.assertNotIn("officialclick1234567890", json.dumps(issued))
        self.assertFalse(emitted)
        send.assert_not_called()

    def test_gpc_overrides_even_unknown_or_stale_action_semantics(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        with patch.dict(os.environ, env, clear=True):
            issued = x_capi.attribution_status(
                landing_twclid="officialclick1234567890"
            )
        headers = {
            "Origin": "https://app.example",
            "Sec-GPC": "1",
            "X-AxonOS-Attribution": issued["context"],
            "X-AxonOS-CSRF": issued["csrf"],
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=False
        ) as ordinary, patch.object(
            gate_server, "_x_capi_privacy_rate_limiter"
        ) as per_context, patch.object(
            gate_server.x_capi,
            "_persist_privacy_fence_status",
            return_value="published",
        ), patch.object(
            gate_server.x_capi,
            "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ):
            per_context.allow.return_value = True
            response = gate_server.app.test_client().post(
                "/api/x-attribution/consent",
                base_url="https://app.example",
                json={"action": "bogus"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "revoked")
        self.assertTrue(response.get_json()["gpc_applied"])
        ordinary.assert_not_called()
        per_context.allow.assert_called_once()

        with patch.dict(os.environ, env, clear=True), patch.object(
            gate_server, "_x_capi_request_rate_allowed", return_value=False
        ) as ordinary, patch.object(
            gate_server, "_x_capi_privacy_rate_limiter"
        ) as per_context, patch.object(
            gate_server.x_capi,
            "_request_worker_consent",
            return_value={"v": 1, "ok": True, "state": "revoked"},
        ), patch.object(
            gate_server.x_capi,
            "_persist_privacy_fence_status",
            return_value="published",
        ):
            per_context.allow.return_value = True
            response = gate_server.app.test_client().post(
                "/api/x-attribution/consent",
                base_url="https://app.example",
                json={"action": "new_lifecycle"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "revoked")
        self.assertTrue(response.get_json()["gpc_applied"])
        ordinary.assert_not_called()
        per_context.allow.assert_called_once()

    def test_hint_limiter_collision_cannot_prevent_gpc_fixed_fence(self):
        import tempfile
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                ticket = x_capi.decode_context_ticket(issued["context"])

            with patch.dict(os.environ, env, clear=True), patch.object(
                gate_server, "_x_capi_privacy_rate_limiter"
            ) as hint_limiter, patch.object(
                gate_server, "_x_capi_request_rate_allowed", return_value=True
            ), patch.object(
                gate_server.x_capi, "_emit_revocation_hint_nonblocking"
            ) as hint:
                # False covers a saturated bucket, a hash-bucket collision, or
                # lock contention inside the advisory limiter.
                hint_limiter.allow.return_value = False
                response = gate_server.app.test_client().get(
                    "/api/x-attribution/status",
                    base_url="https://app.example",
                    headers={
                        "Origin": "https://app.example",
                        "Sec-GPC": "1",
                        "X-AxonOS-Attribution": issued["context"],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["state"], "revocation_required")
            hint_limiter.allow.assert_called_once_with(
                "privacy:" + x_capi._hash(ticket["handle"]),
                fail_open_on_error=True,
            )
            # The universal pre-dispatch observer emits one nonblocking wakeup
            # hint. The saturated attribution hint limiter must prevent the
            # route-specific observer from adding a second one, while the
            # fixed fence remains authoritative either way.
            hint.assert_called_once()
            self.assertEqual(hint.call_args.args[0], issued["context"])
            pending = [
                path.read_bytes()
                for path in directory.glob("pending-*")
                if path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
            ]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0][:2], x_capi._PRIVACY_PENDING_PREFIX)
            self.assertEqual(
                pending[0][2:66].decode("ascii"), x_capi._hash(ticket["handle"])
            )

    def test_hint_limiter_rejection_cannot_prevent_durable_explicit_revoke(self):
        import tempfile
        from axonos_gate import gate_server

        env = ConfigTests.enabled_env()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            directory = Path(temporary)
            _provision_test_privacy_controls(directory)
            env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": temporary,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            with patch.dict(os.environ, env, clear=True):
                issued = x_capi.attribution_status(
                    landing_twclid="officialclick1234567890"
                )
                ticket = x_capi.decode_context_ticket(issued["context"])

            with patch.dict(os.environ, env, clear=True), patch.object(
                gate_server, "_x_capi_privacy_rate_limiter"
            ) as hint_limiter, patch.object(
                gate_server, "_x_capi_request_rate_allowed"
            ) as ordinary_limiter, patch.object(
                gate_server.x_capi, "_request_worker_consent",
                return_value={"v": 1, "ok": True, "state": "revoked"},
            ) as worker_rpc, patch.object(
                gate_server.x_capi, "_emit_revocation_hint_nonblocking"
            ) as hint:
                hint_limiter.allow.return_value = False
                response = gate_server.app.test_client().post(
                    "/api/x-attribution/consent",
                    base_url="https://app.example",
                    json={"action": "revoke"},
                    headers={
                        "Origin": "https://app.example",
                        "X-AxonOS-Attribution": issued["context"],
                        "X-AxonOS-CSRF": issued["csrf"],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["state"], "revoked")
            hint_limiter.allow.assert_called_once_with(
                "privacy:" + x_capi._hash(ticket["handle"]),
                fail_open_on_error=True,
            )
            ordinary_limiter.assert_not_called()
            # The route's advisory hint is suppressed, while update_consent's
            # worker-confirmed explicit-revoke workflow may emit its own
            # best-effort wake-up after the fixed record is present.
            hint.assert_called_once()
            worker_rpc.assert_called_once()
            self.assertEqual(list(directory.glob("*.json")), [])
            occupied = [
                path.read_bytes()
                for path in directory.glob("pending-*")
                if path.read_bytes() != x_capi._PRIVACY_PENDING_EMPTY
            ]
            self.assertEqual(occupied, [x_capi._privacy_pending_record(ticket)])

    def test_websockify_x_capi_preflight_and_body_deadline_are_narrow(self):
        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("'GET, POST, OPTIONS' if is_x_capi", source)
        self.assertGreaterEqual(source.count("read_timeout_seconds=2.0"), 2)
        self.assertIn("self.connection.settimeout", source)

    def test_all_real_payment_rails_share_the_committed_ledger_hook(self):
        ledger = (ROOT / "axonos_gate" / "deposit_ledger.py").read_text(
            encoding="utf-8"
        )
        for rail in ("axgt", "eth", "usdc"):
            self.assertIn(f'        "{rail}",\n        block_number,', ledger)
        self.assertGreaterEqual(ledger.count("_emit_paid_deposit_conversion("), 4)
        self.assertNotIn("enqueue_on_cursor", ledger)

    def test_optional_capi_does_not_change_the_core_verified_deposit_schema(self):
        from axonos_gate import deposit_ledger

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        deposit_ledger._ensure_tables(conn)
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        ).lower()

        self.assertNotIn("chain_id", statements)
        self.assertNotIn("x_capi", statements)
        conn.commit.assert_called_once_with()

    def test_browser_x402_payment_and_session_paths_propagate_context(self):
        gate = (ROOT / "axonos_gate" / "gate_server.py").read_text(encoding="utf-8")
        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(encoding="utf-8")

        flask_settle = gate.split("def api_x402_settle", 1)[1].split(
            "def api_x402_session", 1
        )[0]
        flask_access = gate.split("def api_x402_access", 1)[1].split(
            "def _attach_x402_v2_header", 1
        )[0]
        flask_agent = gate.split("def api_x402_session", 1)[1].split(
            "def api_config", 1
        )[0]
        self.assertIn("attribution_context=", flask_settle)
        self.assertIn("attribution_context=", flask_access)
        self.assertGreaterEqual(flask_agent.count("attribution_context="), 2)

        proxy_settle = proxy.split("if ponly == '/api/x402/settle'", 1)[1].split(
            "if ponly == '/api/x402/session'", 1
        )[0]
        proxy_access = proxy.split("if request_path == '/api/x402/access':", 1)[1].split(
            "if request_path == '/api/config':", 1
        )[0]
        proxy_agent = proxy.split("if ponly == '/api/x402/session'", 1)[1].split(
            "if ponly != '/api/auth/verify-wallet'", 1
        )[0]
        self.assertIn("attribution_context=", proxy_access)
        self.assertIn("attribution_context=", proxy_settle)
        self.assertGreaterEqual(proxy_agent.count("attribution_context="), 2)

    def test_capi_security_and_gpc_policy_are_symmetric_in_both_gate_paths(self):
        gate = (ROOT / "axonos_gate" / "gate_server.py").read_text(encoding="utf-8")
        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(encoding="utf-8")
        for source in (gate, proxy):
            self.assertIn("frame-ancestors 'none'", source)
            self.assertIn("X-Frame-Options", source)
            self.assertIn("Strict-Transport-Security", source)
            self.assertIn("request_is_effectively_https", source)
            self.assertIn("gpc_signal_active", source)
            self.assertIn("business_context_or_none", source)
            self.assertIn("X-AxonOS-Landing-Click", source)
            self.assertIn("X-AxonOS-CSRF", source)
            self.assertIn("fail_open_on_error=True", source)
            self.assertNotIn("_x_capi_privacy_global_limiter", source)


class LaunchRequestIdRouteContractTests(unittest.TestCase):
    """Both public claim handlers enforce one explicit launch intent."""

    WALLET = "0x1234567890123456789012345678901234567890"
    VALID_ID = "ab" * 32
    INVALID_IDS = (
        None,
        "",
        "a" * 31,
        "a" * 129,
        "a" * 31 + "!",
        " " + ("ab" * 32),
        ("ab" * 32) + "\n",
        "é" * 32,
        "contains whitespace but is definitely long enough",
        123,
    )

    @staticmethod
    def _flask_claim(payload):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        gate_server.app.testing = True
        claim = MagicMock(return_value={"granted": True})
        with patch.object(gate_server, "_session_mgr_available", True), patch.object(
            gate_server, "validate_wallet_address", return_value=True
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server, "_request_attribution_context", return_value=None
        ), patch.object(
            gate_server, "try_claim_session", claim
        ):
            response = gate_server.app.test_client().post(
                "/api/session/claim", json=payload
            )
        return response, claim

    @staticmethod
    def _websockify_do_post():
        import ast
        from axonos_gate.session_manager import validate_launch_request_id

        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        do_post = next(
            node
            for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name == "do_POST"
        )
        claim = MagicMock(return_value={"granted": True})
        namespace = {
            "client_ip_for_rate_limit": lambda *_args: "192.0.2.1",
            "x_capi": None,
            "webrtc_service": None,
            "_session_mgr_available": True,
            "validate_launch_request_id": validate_launch_request_id,
            "validate_wallet_address": lambda _wallet: True,
            "validate_ssh_public_key": lambda value: value,
            "_extract_auth_token_from_path_and_headers": (
                lambda *_args: "valid-auth-token"
            ),
            "_is_auth_token_valid": lambda *_args: True,
            "_request_attribution_context": lambda _headers: None,
            "try_claim_session": claim,
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[do_post], type_ignores=[])
                ),
                "websockify_gate.py",
                "exec",
            ),
            namespace,
        )
        return namespace["do_POST"], claim

    @classmethod
    def _websockify_claim(cls, payload, path="/api/session/claim"):
        do_post, claim = cls._websockify_do_post()

        class Request:
            headers = {}
            client_address = ("192.0.2.1", 10000)

            def __init__(self, request_path):
                self.path = request_path

            def _observe_request_gpc_early(self, _path):
                return None

            def _read_json_body(self):
                return payload

            def _send_json(self, code, body, **_kwargs):
                return code, body

            def send_error(self, code, message):
                return code, {"error": message}

        return do_post(Request(path)), claim

    def test_flask_rejects_missing_or_invalid_id_before_claiming(self):
        for invalid in self.INVALID_IDS:
            with self.subTest(launch_request_id=invalid):
                payload = {"wallet_address": self.WALLET, "new_session": True}
                if invalid is not None:
                    payload["launch_request_id"] = invalid
                response, claim = self._flask_claim(payload)
                self.assertEqual(response.status_code, 400)
                claim.assert_not_called()

    def test_websockify_rejects_missing_or_invalid_id_before_claiming(self):
        for invalid in self.INVALID_IDS:
            with self.subTest(launch_request_id=invalid):
                payload = {"wallet_address": self.WALLET, "new_session": True}
                if invalid is not None:
                    payload["launch_request_id"] = invalid
                (status, _body), claim = self._websockify_claim(payload)
                self.assertEqual(status, 400)
                claim.assert_not_called()

    def test_flask_forwards_valid_id_and_legacy_claim_needs_none(self):
        response, claim = self._flask_claim({
            "wallet_address": self.WALLET,
            "new_session": True,
            "launch_request_id": self.VALID_ID,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["launch_request_id"], self.VALID_ID)
        self.assertIs(response.get_json()["launch_request_consumed"], False)
        self.assertEqual(claim.call_args.args[0], self.WALLET)
        self.assertIs(claim.call_args.kwargs["new_session"], True)
        self.assertEqual(
            claim.call_args.kwargs["launch_request_id"], self.VALID_ID
        )

        response, claim = self._flask_claim({"wallet_address": self.WALLET})
        self.assertEqual(response.status_code, 200)
        self.assertIs(claim.call_args.kwargs["new_session"], False)
        self.assertIsNone(claim.call_args.kwargs["launch_request_id"])

    def test_websockify_forwards_the_same_valid_id_contract(self):
        (status, body), claim = self._websockify_claim({
            "wallet_address": self.WALLET,
            "new_session": True,
            "launch_request_id": self.VALID_ID,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["launch_request_id"], self.VALID_ID)
        self.assertIs(body["launch_request_consumed"], False)
        self.assertEqual(claim.call_args.args[0], self.WALLET)
        self.assertIs(claim.call_args.kwargs["new_session"], True)
        self.assertEqual(
            claim.call_args.kwargs["launch_request_id"], self.VALID_ID
        )

        (status, _body), claim = self._websockify_claim({
            "wallet_address": self.WALLET
        })
        self.assertEqual(status, 200)
        self.assertIs(claim.call_args.kwargs["new_session"], False)
        self.assertIsNone(claim.call_args.kwargs["launch_request_id"])

    def test_websockify_claim_uses_exact_parsed_path(self):
        payload = {"wallet_address": self.WALLET}

        (status, _body), claim = self._websockify_claim(
            payload, "/api/session/claim-suffix"
        )
        self.assertEqual(status, 404)
        claim.assert_not_called()

        (status, _body), claim = self._websockify_claim(
            payload, "/api/session/claim?source=viewer"
        )
        self.assertEqual(status, 200)
        claim.assert_called_once()

        (status, _body), claim = self._websockify_claim(
            payload, "/api/session/claim;path-parameter"
        )
        self.assertEqual(status, 404)
        claim.assert_not_called()

        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        for endpoint in (
            "status",
            "claim",
            "heartbeat",
            "release",
            "annotate",
            "restart",
        ):
            self.assertIn(
                f"ponly == '/api/session/{endpoint}'",
                proxy,
            )
            self.assertNotIn(
                f"self.path.startswith('/api/session/{endpoint}')",
                proxy,
            )

    def test_websockify_capi_capable_routes_reject_suffix_aliases(self):
        for route in (
            "/api/auth/verify-wallet",
            "/api/auth/verify-deposit",
            "/api/auth/verify-usdc-deposit",
            "/api/auth/verify-deposit-auto",
            "/api/session/claim",
            "/api/x402/settle",
            "/api/x402/session",
            "/api/x-attribution/consent",
            "/api/x-attribution/bind",
        ):
            for suffix in ("-suffix", ";path-parameter"):
                request_path = route + suffix
                with self.subTest(path=request_path):
                    (status, _body), claim = self._websockify_claim(
                        {}, request_path
                    )
                    self.assertEqual(status, 404)
                    claim.assert_not_called()

        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        for route in (
            "/api/auth/verify-deposit",
            "/api/auth/verify-usdc-deposit",
            "/api/auth/verify-deposit-auto",
            "/api/x402/settle",
            "/api/x402/session",
        ):
            self.assertIn(f"ponly == '{route}'", proxy)
            self.assertNotIn(f"self.path.startswith('{route}')", proxy)
        self.assertIn("ponly != '/api/auth/verify-wallet'", proxy)
        self.assertNotIn(
            "self.path.startswith('/api/auth/verify-wallet')", proxy
        )

    def test_websockify_get_routes_reject_suffix_and_path_parameter_aliases(self):
        import ast

        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        do_get = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name == "do_GET"
        )

        class Base:
            def do_GET(self):
                return 404, {"error": "not found"}

        synthetic = ast.ClassDef(
            name="Request",
            bases=[ast.Name(id="Base", ctx=ast.Load())],
            keywords=[],
            body=[do_get],
            decorator_list=[],
        )
        attribution_context = MagicMock(return_value="unexpected-context")
        namespace = {
            "Base": Base,
            "webrtc_service": None,
            "_session_mgr_available": False,
            "_request_attribution_context": attribution_context,
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[synthetic], type_ignores=[])
                ),
                "websockify_gate.py",
                "exec",
            ),
            namespace,
        )

        class Request(namespace["Request"]):
            headers = {}

            def __init__(self, path):
                self.path = path

            def _observe_request_gpc_early(self, *_args):
                return None

        for route in (
            "/api/x402/access",
            "/api/discount/quote",
            "/api/auth/challenge",
            "/api/auth/wallet-status",
        ):
            for suffix in ("-suffix", ";path-parameter"):
                path = route + suffix
                with self.subTest(path=path):
                    self.assertEqual(Request(path).do_GET()[0], 404)
        attribution_context.assert_not_called()

        for route in (
            "/api/x402/access",
            "/api/discount/quote",
            "/api/auth/challenge",
            "/api/auth/wallet-status",
        ):
            self.assertIn(f"request_path == '{route}'", source)
            self.assertNotIn(f"self.path.startswith('{route}')", source)

    def test_flask_and_websockify_reject_semicolon_aliases_for_capi_routes(self):
        gate_dir = str(ROOT / "axonos_gate")
        if gate_dir not in sys.path:
            sys.path.insert(0, gate_dir)
        from axonos_gate import gate_server

        cases = (
            ("POST", "/api/auth/verify-wallet"),
            ("POST", "/api/auth/verify-deposit"),
            ("POST", "/api/auth/verify-usdc-deposit"),
            ("POST", "/api/auth/verify-deposit-auto"),
            ("POST", "/api/session/claim"),
            ("POST", "/api/x402/settle"),
            ("POST", "/api/x402/session"),
            ("GET", "/api/x402/access"),
            ("GET", "/api/x-attribution/status"),
            ("POST", "/api/x-attribution/consent"),
            ("POST", "/api/x-attribution/bind"),
        )
        with patch.object(
            gate_server, "_request_attribution_context"
        ) as attribution_context:
            client = gate_server.app.test_client()
            for method, route in cases:
                with self.subTest(route=route):
                    response = client.open(
                        route + ";path-parameter",
                        method=method,
                        json={},
                    )
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(
                        client.open(
                            route + ";path-parameter",
                            method="OPTIONS",
                        ).status_code,
                        404,
                    )
                    self.assertEqual(
                        client.open(
                            route + "?preflight=1",
                            method="OPTIONS",
                        ).status_code,
                        200,
                    )
        attribution_context.assert_not_called()

        proxy = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from urllib.parse import parse_qs, urlparse, urlsplit", proxy)
        self.assertNotRegex(
            proxy,
            r"urlparse\((?:self|handler)\.path(?: or [^)]+)?\)\.path",
        )

    def test_websockify_options_query_and_semicolon_parity_for_capi_routes(self):
        import ast

        source = (ROOT / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        do_options = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name == "do_OPTIONS"
        )

        class Base:
            pass

        synthetic = ast.ClassDef(
            name="Request",
            bases=[ast.Name(id="Base", ctx=ast.Load())],
            keywords=[],
            body=[do_options],
            decorator_list=[],
        )
        namespace = {
            "Base": Base,
            "urlsplit": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit,
            "x_capi": None,
            "cors_origin_for_request": lambda *_args: None,
            "_allow_any": False,
            "_allowlist": (),
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[synthetic], type_ignores=[])
                ),
                "websockify_gate.py",
                "exec",
            ),
            namespace,
        )

        class Request(namespace["Request"]):
            headers = {}

            def __init__(self, path):
                self.path = path
                self.status = None

            def send_response(self, status):
                self.status = status

            def send_header(self, *_args):
                return None

            def end_headers(self):
                return None

            def send_error(self, status, _message):
                self.status = status
                return status

        routes = (
            "/api/auth/verify-wallet",
            "/api/auth/verify-deposit",
            "/api/auth/verify-usdc-deposit",
            "/api/auth/verify-deposit-auto",
            "/api/session/claim",
            "/api/x402/access",
            "/api/x402/settle",
            "/api/x402/session",
            "/api/x-attribution/status",
            "/api/x-attribution/consent",
            "/api/x-attribution/bind",
        )
        for route in routes:
            with self.subTest(route=route):
                canonical = Request(route + "?preflight=1")
                self.assertIsNone(canonical.do_OPTIONS())
                self.assertEqual(canonical.status, 200)

                malformed = Request(route + ";path-parameter")
                self.assertEqual(malformed.do_OPTIONS(), 404)
                self.assertEqual(malformed.status, 404)


class MigrationRunnerIsolationTests(unittest.TestCase):
    def test_manual_runner_attests_dedicated_target_before_any_migration(self):
        runner = (
            ROOT / "axonos_gate" / "migrations" / "apply_x_capi_migrations.sh"
        ).read_text(encoding="utf-8")
        target = '000_x_capi_bootstrap_target_preflight.sql'
        roles = '000_x_capi_roles_preflight.sql'
        schema_probe = "schema_present=$(psql"

        self.assertIn("SELECT current_user", runner)
        self.assertIn("SELECT current_database()", runner)
        self.assertEqual(runner.count(target), 1)
        self.assertLess(runner.index(target), runner.index(roles))
        self.assertLess(runner.index(target), runner.index(schema_probe))

        preflight = (
            ROOT / "axonos_gate" / "migrations" /
            "000_x_capi_bootstrap_target_preflight.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("BEGIN READ ONLY", preflight)
        self.assertIn("refuses a non-dedicated PostgreSQL cluster", preflight)
        self.assertIn("left(c.relname,7)<>'x_capi_'", preflight)


if __name__ == "__main__":
    unittest.main()
