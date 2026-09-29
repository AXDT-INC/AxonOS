"""Exercise dedicated-token HTTP serialization without opening any socket.

The reviewed production activation gates remain closed. These adapter tests
provide synthetic ready configuration explicitly; all token material is
generated at runtime and never comes from deployment files or environment.
"""

import copy
import http.client
import io
import json
import os
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import unittest
import uuid
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from axonos_gate import x_capi, x_capi_worker as worker


ROOT = Path(__file__).resolve().parents[2]


class _RecordingSocket:
    """Only the socket interface consumed by real HTTP request preparation."""

    def __init__(self, response):
        self.writes = []
        self.response = response
        self.closed = False

    def sendall(self, data):
        self.writes.append(bytes(data))

    def makefile(self, _mode):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


class _RecordingHTTPSConnection(http.client.HTTPConnection):
    default_port = 443

    def __init__(self, *args, raw_response, context, **kwargs):
        super().__init__(*args, **kwargs)
        self.recording_socket = _RecordingSocket(raw_response)
        self.closed = False

    def connect(self):
        # Deliberately replace TLS/socket I/O, retaining Python's actual HTTP
        # request serialization and response parser above this boundary.
        self.sock = self.recording_socket

    def close(self):
        self.closed = True
        super().close()


class DedicatedTokenTransportTests(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)
        self.cfg = SimpleNamespace(
            mode="live", producer_ready=True, errors=(), pixel_id="pixel123",
            event_ids={"session_started": "event123"},
            twclid_charset="lower_alnum", twclid_min_length=20,
            twclid_max_length=64,
        )
        self.job = {
            "conversion_timestamp_ms": 1_789_387_200_603,
            "event_id": "event123", "twclid": "officialclick1234567890",
            "conversion_id": str(uuid.uuid4()),
        }
        self.payload = worker.build_payload(self.job)
        self.adapter = worker.RequestsTransport()
        # A missing fake anywhere must fail before any external connection.
        self.no_socket = patch.object(
            socket, "socket", side_effect=AssertionError("network forbidden")
        )
        self.no_socket.start()
        self.addCleanup(self.no_socket.stop)

    @contextmanager
    def ready(self, **overrides):
        cfg = SimpleNamespace(**{**vars(self.cfg), **overrides})
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            worker, "read_token", return_value=(self.token, None)
        ) as token_reader:
            yield token_reader

    def round_trip(self, status=200, body=None, headers=()):
        if body is None:
            body = b'{"data":{"conversions_processed":1,"debug_id":"safe-debug"}}'
        raw = (
            f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n".encode()
            + b"".join(f"{key}: {value}\r\n".encode() for key, value in headers)
            + b"\r\n" + body
        )
        connections = []

        def factory(*args, **kwargs):
            connection = _RecordingHTTPSConnection(
                *args, raw_response=raw, **kwargs
            )
            connections.append(connection)
            return connection

        with self.ready(), patch.object(
            worker.http.client, "HTTPSConnection", side_effect=factory
        ) as constructor:
            response = self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
        self.assertEqual(len(connections), 1)
        connection = connections[0]
        self.assertTrue(connection.closed)
        self.assertTrue(connection.recording_socket.closed)
        return response, b"".join(connection.recording_socket.writes), constructor

    def test_exact_prepared_request_headers_json_and_verified_tls(self):
        response, wire, constructor = self.round_trip()
        request_head, body = wire.split(b"\r\n\r\n", 1)
        lines = request_head.decode("ascii").split("\r\n")
        self.assertEqual(
            lines[0], "POST /12/measurement/conversions/pixel123 HTTP/1.1"
        )
        headers = dict(line.split(": ", 1) for line in lines[1:])
        self.assertEqual(headers, {
            "Host": "ads-api.x.com", "Accept-Encoding": "identity",
            "Content-Length": str(len(body)),
            "Content-Type": "application/json", "X-Pixel-Token": self.token,
        })
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.args, ("ads-api.x.com",))
        self.assertEqual(constructor.call_args.kwargs["port"], 443)
        self.assertEqual(constructor.call_args.kwargs["timeout"], 5.0)
        context = constructor.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIsNone(context.keylog_filename)
        self.assertEqual(body, json.dumps(
            self.payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii"))
        conversion = json.loads(body)["conversions"][0]
        self.assertEqual(set(conversion), {
            "conversion_timestamp", "event_id", "identifiers", "conversion_id"
        })
        self.assertIs(type(conversion["conversion_timestamp"]), int)
        self.assertEqual(conversion["conversion_timestamp"], 1_789_387_200_603)
        self.assertEqual(conversion["identifiers"], [{"twclid": self.job["twclid"]}])
        self.assertEqual(conversion["conversion_id"], self.job["conversion_id"])
        self.assertNotIn(self.token.encode(), body)
        self.assertNotIn(b"conversion_time\"", body)
        self.assertEqual(worker.classify_response(response, 100)["action"], "accepted")

    def test_ambient_proxy_credentials_and_ca_overrides_are_not_used(self):
        with patch.dict(os.environ, {
            "HTTPS_PROXY": "https://proxy.invalid:8443",
            "HTTP_PROXY": "http://proxy.invalid:8080",
            "ALL_PROXY": "socks5://proxy.invalid:1080",
            "NETRC": "/does-not-exist",
            "REQUESTS_CA_BUNDLE": "/does-not-exist",
            "CURL_CA_BUNDLE": "/does-not-exist",
        }, clear=False):
            _, wire, constructor = self.round_trip()
        self.assertEqual(constructor.call_args.args, ("ads-api.x.com",))
        for forbidden in (
            b"Authorization:", b"Proxy-Authorization:", b"Cookie:",
            b"User-Agent:", b"Referer:", b"Origin:", b"proxy.invalid",
        ):
            self.assertNotIn(forbidden, wire)

    def test_inherited_http_debug_logging_is_explicitly_disabled(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(http.client.HTTPConnection, "debuglevel", 1), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            self.round_trip()
            self.round_trip(403, body=self.token.encode())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_tls_keylog_and_trust_environment_overrides_fail_before_http(self):
        for variable in ("SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
            with self.subTest(variable=variable), self.ready(), patch.dict(
                os.environ, {variable: "/does-not-exist/forbidden-tls-output"}, clear=False
            ), patch.object(worker.ssl, "SSLContext") as context, patch.object(
                worker.http.client, "HTTPSConnection"
            ) as constructor:
                with self.assertRaisesRegex(ValueError, "^unsupported_transport_tls_environment$"):
                    self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
            context.assert_not_called()
            constructor.assert_not_called()

    def test_fresh_connection_and_runtime_secret_for_each_attempt(self):
        first, first_wire, first_constructor = self.round_trip()
        second, second_wire, second_constructor = self.round_trip()
        self.assertEqual(first_wire, second_wire)
        self.assertIsNot(first_constructor, second_constructor)
        self.assertEqual(first.status_code, second.status_code)
        self.assertIsNone(self.adapter._session)

    def test_only_retry_after_response_header_survives(self):
        response, _, _ = self.round_trip(status=429, headers=(
            ("Retry-After", "23"),
            ("Set-Cookie", "tracking=forbidden"),
            ("Location", "https://other.invalid/"),
            ("X-Request-Id", "private-response-metadata"),
        ))
        self.assertEqual(dict(response.headers), {"Retry-After": "23"})
        self.assertEqual(worker.classify_response(response, 100), {
            "action": "retry", "code": "rate_limited", "retry_at": 123,
        })

    def test_response_statuses_do_not_trigger_automatic_retries_or_redirects(self):
        cases = {
            201: ("permanent", "request_rejected"),
            204: ("permanent", "request_rejected"),
            301: ("permanent", "redirect_rejected"),
            302: ("permanent", "redirect_rejected"),
            307: ("permanent", "redirect_rejected"),
            308: ("permanent", "redirect_rejected"),
            400: ("permanent", "request_rejected"),
            401: ("pause", "authentication_failed"),
            403: ("pause", "authentication_failed"),
            404: ("permanent", "request_rejected"),
            408: ("retry", "transient_http"),
            425: ("retry", "transient_http"),
            429: ("retry", "rate_limited"),
            500: ("retry", "transient_http"),
            502: ("retry", "transient_http"),
            503: ("retry", "transient_http"),
            504: ("retry", "transient_http"),
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                response, wire, constructor = self.round_trip(
                    status, headers=(("Location", "https://other.invalid/"),)
                )
                result = worker.classify_response(response, 100)
                self.assertEqual((result["action"], result["code"]), expected)
                constructor.assert_called_once()
                self.assertEqual(wire.count(b"POST /12/"), 1)

    def test_success_body_must_unambiguously_report_one_processed_conversion(self):
        bodies = (
            b"", b"not-json", b"\xff", b"null", b"[]", b"{}",
            b'{"ok":true}', b'{"data":[]}',
            b'{"data":{"conversions_processed":true}}',
            b'{"data":{"conversions_processed":false}}',
            b'{"data":{"conversions_processed":"1"}}',
            b'{"data":{"conversions_processed":1.0}}',
            b'{"data":{"conversions_processed":0}}',
            b'{"data":{"conversions_processed":2}}',
            b'{"data":{"conversions_processed":-1}}',
        )
        for body in bodies:
            with self.subTest(body=body):
                response, _, _ = self.round_trip(body=body)
                result = worker.classify_response(response, 100)
                self.assertEqual(result["action"], "permanent")
                self.assertEqual(result["code"], "unknown_success_body")

    def test_reflected_response_identifiers_never_reach_completion_storage(self):
        # All of these matched the old debug-ID grammar, including a permitted
        # token shape. These are synthetic values, never production credentials.
        self.token = secrets.token_hex(32)
        for reflected in (self.token, self.job["twclid"], "opaque-vendor-debug"):
            with self.subTest(kind=("credential" if reflected == self.token else "identifier")):
                response, _wire, _constructor = self.round_trip(body=json.dumps({
                    "data": {"conversions_processed": 1, "debug_id": reflected}
                }).encode())
                result = worker.classify_response(response, 100)
                self.assertEqual(result["action"], "accepted")
                self.assertIsNone(result.get("debug_id"))
                self.assertNotIn(reflected, json.dumps(result))
                conn = MagicMock()
                cur = conn.cursor.return_value.__enter__.return_value
                cur.fetchone.return_value = (1,)
                job = dict(self.job, lease_owner="test-worker", lease_token="test-lease")
                # Completion is defensive even if a caller supplies the old
                # result shape. No arbitrary response field is DB-authoritative.
                result["debug_id"] = reflected
                self.assertTrue(worker.finish_job(conn, job, result, 101))
                update_sql, parameters = cur.execute.call_args_list[0].args
                self.assertIn("safe_debug_id=NULL", update_sql)
                self.assertNotIn(reflected, repr(parameters))

    def test_response_rate_limit_values_have_bounded_effect(self):
        for raw, expected in (
            ("0", 101), ("-1", 101), ("999999999", 3700),
            ("Thu, 01 Jan 1970 00:02:00 GMT", 120),
            ("bad-date", None), ("x" * 129, None),
        ):
            with self.subTest(value=raw):
                response, _, _ = self.round_trip(
                    429, headers=(("Retry-After", raw),)
                )
                result = worker.classify_response(response, 100)
                self.assertEqual(result["retry_at"], expected)

    def test_response_read_is_capped_and_connection_closes_on_oversize(self):
        fake_connection = MagicMock()
        fake_connection.getresponse.return_value.read.return_value = (
            b"x" * (worker.MAX_RESPONSE_BYTES + 1)
        )
        with self.ready(), patch.object(
            worker.http.client, "HTTPSConnection", return_value=fake_connection
        ), self.assertRaisesRegex(ValueError, "^response_too_large$"):
            self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
        fake_connection.getresponse.return_value.read.assert_called_once_with(
            worker.MAX_RESPONSE_BYTES + 1
        )
        fake_connection.close.assert_called_once()

    def test_response_at_size_limit_is_permitted_without_unbounded_read(self):
        response, _, _ = self.round_trip(body=b"x" * worker.MAX_RESPONSE_BYTES)
        self.assertEqual(len(response.body), worker.MAX_RESPONSE_BYTES)
        self.assertEqual(worker.classify_response(response, 100)["action"], "permanent")

    def test_socket_timeouts_and_protocol_failures_close_connection_once(self):
        for stage in ("request", "getresponse", "read"):
            for exception in (
                TimeoutError("synthetic timeout"),
                http.client.RemoteDisconnected("synthetic disconnect"),
                http.client.BadStatusLine("synthetic invalid status"),
                ssl.SSLCertVerificationError("synthetic certificate failure"),
            ):
                with self.subTest(stage=stage, kind=type(exception).__name__):
                    fake_connection = MagicMock()
                    target = (
                        fake_connection.getresponse.return_value.read
                        if stage == "read" else getattr(fake_connection, stage)
                    )
                    target.side_effect = exception
                    with self.ready(), patch.object(
                        worker.http.client, "HTTPSConnection", return_value=fake_connection
                    ), self.assertRaises(type(exception)):
                        self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
                    fake_connection.close.assert_called_once()
                    fake_connection.request.assert_called_once()

    def test_forbidden_fields_in_any_payload_container_never_reach_http(self):
        forbidden = (
            "wallet", "wallet_address", "wallet_hash", "hashed_wallet",
            "email", "hashed_email", "phone", "hashed_phone_number",
            "ip_address", "user_agent", "event_source_url", "url", "referrer",
            "transaction_id", "transaction_hash", "session_id", "payment",
            "amount", "value", "price_currency", "number_items", "contents",
            "gpu", "workload", "profile", "twpid", "telemetry",
            "conversion_time", "pixel_id", "X-Pixel-Token",
        )
        with self.ready() as token_reader, patch.object(
            worker.http.client, "HTTPSConnection"
        ) as constructor:
            for field in forbidden:
                for location in ("root", "conversion", "identifier"):
                    with self.subTest(field=field, location=location):
                        payload = copy.deepcopy(self.payload)
                        conversion = payload["conversions"][0]
                        target = {
                            "root": payload, "conversion": conversion,
                            "identifier": conversion["identifiers"][0],
                        }[location]
                        target[field] = "must-stay-internal"
                        with self.assertRaises(ValueError):
                            self.adapter.send(self.cfg.pixel_id, self.token, payload)
            token_reader.assert_not_called()
            constructor.assert_not_called()

    def test_job_metadata_is_dropped_before_payload_building(self):
        job = dict(self.job, wallet_address="0x" + "a" * 40,
                   source_key_hash="b" * 64, context_id=str(uuid.uuid4()),
                   session_id="internal-session", amount="99.99", gpu="internal-gpu")
        self.assertEqual(worker.build_payload(job), self.payload)

    def test_invalid_container_shapes_and_missing_fields_fail_before_secrets(self):
        bad_payloads = [None, [], {"conversions": []}, {"conversions": ()},
                        {"conversions": [self.payload["conversions"][0]] * 2}]
        for field in self.payload["conversions"][0]:
            payload = copy.deepcopy(self.payload)
            del payload["conversions"][0][field]
            bad_payloads.append(payload)
        for value in (None, {}, [], [{"twclid": self.job["twclid"]}] * 2,
                      [{"twclid": {"wallet": "injected"}}]):
            payload = copy.deepcopy(self.payload)
            payload["conversions"][0]["identifiers"] = value
            bad_payloads.append(payload)
        with self.ready() as token_reader, patch.object(
            worker.http.client, "HTTPSConnection"
        ) as constructor:
            for payload in bad_payloads:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.adapter.send(self.cfg.pixel_id, self.token, payload)
            token_reader.assert_not_called()
            constructor.assert_not_called()

    def test_identifier_type_path_and_header_injection_are_rejected(self):
        invalid = (
            None, 123, True, {}, [], "", "../other", "other?token=x", "x#fragment",
            "https://other.invalid", "other\r\nX-Extra: value", "a b", "é",
            "0x" + "1" * 40, "1" * 64, "someone.eth", "192.0.2.44",
        )
        with self.ready() as token_reader, patch.object(
            worker.http.client, "HTTPSConnection"
        ) as constructor:
            for value in invalid:
                for slot in ("pixel", "event", "twclid"):
                    payload = copy.deepcopy(self.payload)
                    pixel = self.cfg.pixel_id
                    conversion = payload["conversions"][0]
                    if slot == "pixel":
                        pixel = value
                    elif slot == "event":
                        conversion["event_id"] = value
                    else:
                        conversion["identifiers"][0]["twclid"] = value
                    with self.subTest(slot=slot, value=value), self.assertRaises(ValueError):
                        self.adapter.send(pixel, self.token, payload)
            token_reader.assert_not_called()
            constructor.assert_not_called()

    def test_pixel_and_event_must_match_current_configuration(self):
        with self.ready(), patch.object(worker.http.client, "HTTPSConnection") as constructor:
            with self.assertRaises(ValueError):
                self.adapter.send("otherpixel", self.token, self.payload)
            payload = copy.deepcopy(self.payload)
            payload["conversions"][0]["event_id"] = "otherevent"
            with self.assertRaises(ValueError):
                self.adapter.send(self.cfg.pixel_id, self.token, payload)
            constructor.assert_not_called()

    def test_timestamp_and_conversion_id_types_are_strict_and_stable(self):
        cases = {
            "conversion_timestamp": (None, True, False, 0, -1, 1.0, "123",
                                     float("nan"), Decimal(123), 253_402_300_800_000,
                                     type("TimestampSubclass", (int,), {})(123)),
            "conversion_id": (None, 123, {}, "", "opaque-but-not-uuid",
                              str(uuid.UUID(int=0, version=1)),
                              str(uuid.uuid5(uuid.NAMESPACE_DNS, "test")),
                              "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        }
        with self.ready() as token_reader, patch.object(
            worker.http.client, "HTTPSConnection"
        ) as constructor:
            for field, values in cases.items():
                for value in values:
                    payload = copy.deepcopy(self.payload)
                    payload["conversions"][0][field] = value
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        self.adapter.send(self.cfg.pixel_id, self.token, payload)
            token_reader.assert_not_called()
            constructor.assert_not_called()
        for timestamp in (1, 253_402_300_799_999):
            payload = worker.build_payload(dict(self.job, conversion_timestamp_ms=timestamp))
            self.assertIs(type(payload["conversions"][0]["conversion_timestamp"]), int)
            self.assertEqual(payload, worker.build_payload(
                dict(self.job, conversion_timestamp_ms=timestamp)
            ))

    def test_off_dry_run_and_real_live_activation_gates_precede_secret_access(self):
        for mode in ("off", "dry_run", "live"):
            with patch.dict(os.environ, {"X_CAPI_MODE": mode}, clear=True), patch.object(
                worker, "read_token"
            ) as token_reader, patch.object(
                worker.http.client, "HTTPSConnection"
            ) as constructor, self.assertRaisesRegex(RuntimeError, worker.LIVE_DELIVERY_BLOCK_REASON):
                self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
            token_reader.assert_not_called()
            constructor.assert_not_called()
        for overrides in (
            {"mode": "off"}, {"mode": "dry_run"}, {"producer_ready": False},
            {"errors": ("privacy gate failed",)},
        ):
            with self.ready(**overrides) as token_reader, patch.object(
                worker.http.client, "HTTPSConnection"
            ) as constructor, self.assertRaises(RuntimeError):
                self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
            token_reader.assert_not_called()
            constructor.assert_not_called()

    def test_supplied_or_rotated_token_cannot_replace_runtime_file(self):
        values = (None, b"bytes", secrets.token_urlsafe(32),
                  self.token + "\r\nX-Extra: injected")
        with self.ready(), patch.object(worker.http.client, "HTTPSConnection") as constructor:
            for token in values:
                with self.subTest(kind=type(token).__name__), self.assertRaisesRegex(
                    ValueError, "^invalid_transport_token$"
                ):
                    self.adapter.send(self.cfg.pixel_id, token, self.payload)
            constructor.assert_not_called()
        with self.ready(), patch.object(
            worker, "read_token", return_value=(None, "unreadable")
        ), patch.object(worker.http.client, "HTTPSConnection") as constructor:
            with self.assertRaisesRegex(ValueError, "^invalid_transport_token$"):
                self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
            constructor.assert_not_called()

    def test_no_transport_logging_on_success_or_failure(self):
        with patch.object(worker.logger, "log") as log, patch.object(
            worker.logger, "debug"
        ) as debug, patch.object(worker.logger, "info") as info, patch.object(
            worker.logger, "warning"
        ) as warning, patch.object(worker.logger, "error") as error:
            self.round_trip()
            self.round_trip(403, body=self.token.encode())
            self.round_trip(500, body=b"private-response-body")
        for method in (log, debug, info, warning, error):
            method.assert_not_called()

    def test_transport_rejects_background_thread_before_network(self):
        errors = []

        def send():
            try:
                self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
            except Exception as exc:
                errors.append(str(exc))

        with self.ready(), patch.object(worker.http.client, "HTTPSConnection") as constructor:
            thread = threading.Thread(target=send)
            thread.start()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["transport_requires_main_thread"])
        constructor.assert_not_called()

    def test_existing_alarm_is_not_overwritten(self):
        with self.ready(), patch.object(signal, "getitimer", return_value=(5.0, 0.0)), patch.object(
            signal, "setitimer"
        ) as timer, patch.object(worker.http.client, "HTTPSConnection") as constructor:
            with self.assertRaisesRegex(RuntimeError, "^transport_deadline_conflict$"):
                self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
        timer.assert_not_called()
        constructor.assert_not_called()

    def test_blocked_alarm_cannot_silently_disable_hard_deadline(self):
        with self.ready(), patch.object(
            signal, "pthread_sigmask", return_value={signal.SIGALRM}
        ) as mask, patch.object(signal, "setitimer") as timer, patch.object(
            worker.http.client, "HTTPSConnection"
        ) as constructor:
            with self.assertRaises(RuntimeError):
                self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
        mask.assert_called_once_with(signal.SIG_BLOCK, set())
        timer.assert_not_called()
        constructor.assert_not_called()

    def test_deadline_restores_signal_handler_after_success_and_failure(self):
        previous = signal.getsignal(signal.SIGALRM)
        self.round_trip()
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        with self.ready(), patch.object(
            worker.http.client, "HTTPSConnection", side_effect=TimeoutError
        ), self.assertRaises(TimeoutError):
            self.adapter.send(self.cfg.pixel_id, self.token, self.payload)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    @unittest.skipUnless(hasattr(signal, "ITIMER_REAL"), "requires POSIX deadline")
    def test_hard_deadline_terminates_hanging_sender_process(self):
        script = r'''
import resource
import secrets
import socket
import threading
import time
from unittest.mock import patch
from axonos_gate import x_capi_worker as worker
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
worker.TOTAL_REQUEST_SECONDS = 0.15
class HangingConnection:
    def __init__(self, *args, **kwargs):
        pass
    def set_debuglevel(self, level):
        assert level == 0
    def request(self, *args, **kwargs):
        print(threading.active_count(), flush=True)
        while True:
            time.sleep(0.01)
    def close(self):
        pass
with patch.object(worker.http.client, "HTTPSConnection", HangingConnection), patch.object(
    socket, "socket", side_effect=AssertionError("network forbidden")
):
    worker._post_conversion("pixel123", secrets.token_urlsafe(32), b"{}")
raise AssertionError("sender survived absolute deadline")
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, -signal.SIGALRM, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(result.stderr, "")


class ProtectedTokenFileTests(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)
        self.info = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_uid=os.geteuid(), st_nlink=1
        )

    @contextmanager
    def fake_file(self, contents=None, **stat_overrides):
        info = SimpleNamespace(**{**vars(self.info), **stat_overrides})
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            opened = stack.enter_context(patch.object(os, "open", return_value=17))
            stack.enter_context(patch.object(os, "fstat", return_value=info))
            stack.enter_context(patch.object(os, "read", side_effect=[
                self.token.encode() if contents is None else contents, b""
            ]))
            closed = stack.enter_context(patch.object(os, "close"))
            yield opened, closed

    def test_runtime_token_uses_only_private_nofollow_cloexec_file(self):
        with self.fake_file() as (opened, closed):
            value, error = worker.read_token()
        self.assertIsNone(error)
        self.assertEqual(value, self.token)
        self.assertEqual(opened.call_args.args[0], "/run/secrets/x_capi_access_token")
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_CLOEXEC)
        closed.assert_called_once_with(17)

    def test_file_metadata_failures_are_closed_and_do_not_read_secret(self):
        for overrides in (
            {"st_mode": stat.S_IFREG | 0o644},
            {"st_mode": stat.S_IFREG | 0o640},
            {"st_mode": stat.S_IFDIR | 0o700},
            {"st_mode": stat.S_IFIFO | 0o600},
            {"st_uid": os.geteuid() + 100000},
            {"st_nlink": 2},
        ):
            with self.subTest(metadata=overrides), self.fake_file(**overrides) as (_, closed), patch.object(
                os, "read"
            ) as read:
                value, error = worker.read_token()
            self.assertIsNone(value)
            self.assertIsNotNone(error)
            self.assertNotIn(self.token, error)
            read.assert_not_called()
            closed.assert_called_once_with(17)

    def test_token_bounds_control_bytes_and_non_ascii_are_rejected(self):
        for contents in (
            b"", secrets.token_bytes(8), b"a" * 8193,
            self.token.encode() + b"\n", self.token.encode() + b"\r\n",
            self.token.encode() + b"\x00", self.token.encode() + b" ",
            self.token.encode() + b"\xff",
        ):
            with self.subTest(length=len(contents)), self.fake_file(contents) as (_, closed):
                value, error = worker.read_token()
            self.assertIsNone(value)
            self.assertIsNotNone(error)
            self.assertNotIn(self.token, error)
            closed.assert_called_once_with(17)

    def test_missing_file_and_environment_token_fail_without_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            os, "open", side_effect=FileNotFoundError
        ):
            value, error = worker.read_token()
        self.assertIsNone(value)
        self.assertIsNotNone(error)
        with patch.dict(os.environ, {"X_CAPI_ACCESS_TOKEN": self.token}, clear=True), patch.object(
            os, "open"
        ) as opened:
            value, error = worker.read_token()
        self.assertIsNone(value)
        self.assertIn("prohibited", error)
        self.assertNotIn(self.token, error)
        opened.assert_not_called()

    def test_paths_outside_secret_mount_and_symlinks_fail_before_open(self):
        for path in ("/tmp/token", "/run/secrets/../token", "/etc/passwd"):
            with patch.dict(os.environ, {"X_CAPI_ACCESS_TOKEN_FILE": path}, clear=True), patch.object(
                os, "open"
            ) as opened:
                value, error = worker.read_token()
            self.assertIsNone(value)
            self.assertIsNotNone(error)
            opened.assert_not_called()
        realpath = os.path.realpath

        def resolve(path):
            return "/tmp/other" if path == "/run/secrets/x_capi_access_token" else realpath(path)

        with patch.dict(os.environ, {}, clear=True), patch.object(
            os.path, "realpath", side_effect=resolve
        ), patch.object(os, "open") as opened:
            value, error = worker.read_token()
        self.assertIsNone(value)
        self.assertIsNotNone(error)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
