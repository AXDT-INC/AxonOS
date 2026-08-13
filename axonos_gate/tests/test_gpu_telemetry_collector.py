"""Focused tests for the supervisor-managed GPU telemetry collector."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from axonos_gate import gpu_telemetry_collector as collector


MIB = 1024 * 1024


class FakeFunction:
    """ctypes-like callable whose ABI attributes can be configured by the client."""

    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeNvmlLibrary:
    def __init__(
        self,
        *,
        temperature_rc=collector.NVML_SUCCESS,
        power_rc=collector.NVML_SUCCESS,
        utilization_rc=collector.NVML_SUCCESS,
    ):
        self.temperature_rc = temperature_rc
        self.power_rc = power_rc
        self.utilization_rc = utilization_rc
        self.shutdown_calls = 0
        self.handles = [101, 202]
        self.indices = {101: 3, 202: 7}
        self.names = {101: b"Tesla V100-SXM2-32GB", 202: b"Test GPU 7"}
        self.utilization = {101: (25, 11), 202: (75, 22)}
        self.memory = {
            101: (1536 * MIB, 32768 * MIB),
            202: (256 * MIB, 16384 * MIB),
        }
        self.temperature = {101: 47, 202: 39}
        self.power_mw = {101: 56789, 202: 12345}

        self.nvmlInit_v2 = FakeFunction(lambda: collector.NVML_SUCCESS)
        self.nvmlShutdown = FakeFunction(self._shutdown)
        self.nvmlErrorString = FakeFunction(lambda rc: b"fake NVML failure")
        self.nvmlDeviceGetCount_v2 = FakeFunction(self._get_count)
        self.nvmlDeviceGetHandleByIndex_v2 = FakeFunction(self._get_handle)
        self.nvmlDeviceGetIndex = FakeFunction(self._get_index)
        self.nvmlDeviceGetName = FakeFunction(self._get_name)
        self.nvmlDeviceGetUtilizationRates = FakeFunction(self._get_utilization)
        self.nvmlDeviceGetMemoryInfo = FakeFunction(self._get_memory)
        self.nvmlDeviceGetTemperature = FakeFunction(self._get_temperature)
        self.nvmlDeviceGetPowerUsage = FakeFunction(self._get_power)

    @staticmethod
    def _handle_value(handle):
        return int(handle.value)

    def _shutdown(self):
        self.shutdown_calls += 1
        return collector.NVML_SUCCESS

    def _get_count(self, count_pointer):
        count_pointer._obj.value = len(self.handles)
        return collector.NVML_SUCCESS

    def _get_handle(self, ordinal, handle_pointer):
        handle_pointer._obj.value = self.handles[int(ordinal)]
        return collector.NVML_SUCCESS

    def _get_index(self, handle, index_pointer):
        index_pointer._obj.value = self.indices[self._handle_value(handle)]
        return collector.NVML_SUCCESS

    def _get_name(self, handle, output_buffer, _buffer_length):
        output_buffer.value = self.names[self._handle_value(handle)]
        return collector.NVML_SUCCESS

    def _get_utilization(self, handle, utilization_pointer):
        if self.utilization_rc != collector.NVML_SUCCESS:
            return self.utilization_rc
        gpu, memory = self.utilization[self._handle_value(handle)]
        utilization_pointer._obj.gpu = gpu
        utilization_pointer._obj.memory = memory
        return collector.NVML_SUCCESS

    def _get_memory(self, handle, memory_pointer):
        used, total = self.memory[self._handle_value(handle)]
        memory_pointer._obj.total = total
        memory_pointer._obj.free = total - used
        memory_pointer._obj.used = used
        return collector.NVML_SUCCESS

    def _get_temperature(self, handle, sensor_type, temperature_pointer):
        self.last_temperature_sensor = int(sensor_type)
        if self.temperature_rc != collector.NVML_SUCCESS:
            return self.temperature_rc
        temperature_pointer._obj.value = self.temperature[self._handle_value(handle)]
        return collector.NVML_SUCCESS

    def _get_power(self, handle, power_pointer):
        if self.power_rc != collector.NVML_SUCCESS:
            return self.power_rc
        power_pointer._obj.value = self.power_mw[self._handle_value(handle)]
        return collector.NVML_SUCCESS


class TelemetryIntervalTests(unittest.TestCase):
    def test_default_invalid_and_clamped_intervals(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                collector.telemetry_interval_seconds(),
                collector.DEFAULT_INTERVAL_SECONDS,
            )

        self.assertEqual(
            collector.telemetry_interval_seconds("not-a-number"),
            collector.DEFAULT_INTERVAL_SECONDS,
        )
        self.assertEqual(
            collector.telemetry_interval_seconds("nan"),
            collector.DEFAULT_INTERVAL_SECONDS,
        )
        self.assertEqual(
            collector.telemetry_interval_seconds("inf"),
            collector.DEFAULT_INTERVAL_SECONDS,
        )
        self.assertEqual(
            collector.telemetry_interval_seconds("0.01"),
            collector.MIN_INTERVAL_SECONDS,
        )
        self.assertEqual(
            collector.telemetry_interval_seconds("999"),
            collector.MAX_INTERVAL_SECONDS,
        )
        self.assertEqual(collector.telemetry_interval_seconds("2.5"), 2.5)


class NvmlClientTests(unittest.TestCase):
    def test_sample_converts_nvml_values_to_api_units(self):
        library = FakeNvmlLibrary()
        client = collector.NvmlClient(library=library)
        try:
            sample = client.sample()
        finally:
            client.close()

        self.assertEqual(len(sample), 2)
        self.assertEqual(
            sample[0],
            {
                "index": 3,
                "name": "Tesla V100-SXM2-32GB",
                "utilization_pct": 25.0,
                "memory_used_mb": 1536.0,
                "memory_total_mb": 32768.0,
                "temperature_c": 47.0,
                "power_draw_w": 56.789,
            },
        )
        self.assertEqual(sample[1]["index"], 7)
        self.assertEqual(sample[1]["memory_used_mb"], 256.0)
        self.assertEqual(sample[1]["memory_total_mb"], 16384.0)
        self.assertEqual(sample[1]["power_draw_w"], 12.345)
        self.assertEqual(library.last_temperature_sensor, 0)
        self.assertEqual(library.shutdown_calls, 1)

    def test_unsupported_optional_metrics_are_null(self):
        library = FakeNvmlLibrary(
            temperature_rc=collector.NVML_ERROR_NOT_SUPPORTED,
            power_rc=collector.NVML_ERROR_NOT_SUPPORTED,
        )
        client = collector.NvmlClient(library=library)
        try:
            sample = client.sample()
        finally:
            client.close()

        self.assertTrue(sample)
        self.assertTrue(all(gpu["temperature_c"] is None for gpu in sample))
        self.assertTrue(all(gpu["power_draw_w"] is None for gpu in sample))

    def test_mandatory_metric_error_propagates_with_context(self):
        library = FakeNvmlLibrary(utilization_rc=999)
        client = collector.NvmlClient(library=library)
        try:
            with self.assertRaisesRegex(
                collector.NvmlError,
                r"get GPU 3 utilization: fake NVML failure \(NVML 999\)",
            ):
                client.sample()
        finally:
            client.close()


class PublishSampleTests(unittest.TestCase):
    def test_publish_atomically_replaces_with_valid_world_readable_json(self):
        gpus = [{"index": 0, "utilization_pct": 42.0}]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "gpu.json"
            destination.parent.mkdir()
            destination.parent.chmod(0o755)
            destination.write_text("not JSON", encoding="utf-8")

            real_replace = os.replace
            with patch.object(
                collector.os,
                "replace",
                side_effect=real_replace,
            ) as replace:
                collector.publish_sample(str(destination), gpus, sampled_at=1234.5)

            replace.assert_called_once()
            temporary, replaced_destination = replace.call_args.args
            self.assertEqual(Path(replaced_destination), destination)
            self.assertNotEqual(Path(temporary), destination)
            self.assertFalse(Path(temporary).exists())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)

            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["gpus"], gpus)
            self.assertEqual(payload["ts"], 1234.5)
            self.assertIsInstance(payload["monotonic_ts"], float)
            self.assertGreater(payload["monotonic_ts"], 0)
            self.assertEqual(list(destination.parent.glob(".*.tmp.*")), [])

    def test_refuses_to_publish_empty_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "gpu.json"
            with self.assertRaisesRegex(ValueError, "empty GPU sample"):
                collector.publish_sample(str(destination), [], sampled_at=1234.5)
            self.assertFalse(destination.exists())


class DeterministicStopEvent:
    """A threading.Event stand-in that stops after a fixed number of waits."""

    def __init__(self, stop_after_waits):
        self.stop_after_waits = stop_after_waits
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


class SequenceClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.sample_calls = 0
        self.close_calls = 0

    def sample(self):
        self.sample_calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.close_calls += 1


class RunCollectorTests(unittest.TestCase):
    @staticmethod
    def _sample(utilization=20.0):
        return [{
            "index": 0,
            "name": "Tesla V100-SXM2-32GB",
            "utilization_pct": utilization,
            "memory_used_mb": 512.0,
            "memory_total_mb": 32768.0,
            "temperature_c": 35.0,
            "power_draw_w": 56.0,
        }]

    def test_sample_failure_preserves_last_good_file_and_reinitializes(self):
        first_sample = self._sample(42.0)
        first_client = SequenceClient(
            [first_sample, collector.NvmlError("GPU disappeared")]
        )
        factory_calls = []

        def client_factory():
            factory_calls.append(True)
            if len(factory_calls) == 1:
                return first_client
            raise collector.NvmlError("NVML still unavailable")

        stop = DeterministicStopEvent(stop_after_waits=3)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o755)
            cache = Path(directory) / "gpu.json"
            with patch.dict(
                os.environ,
                {
                    "AXGT_GPU_TELEMETRY_FILE": str(cache),
                    "AXGT_GPU_TELEMETRY_INTERVAL_SECONDS": "0.5",
                },
            ):
                collector.run_collector(stop, client_factory=client_factory)

            payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual(payload["gpus"], first_sample)
        self.assertEqual(len(factory_calls), 2)
        self.assertEqual(first_client.sample_calls, 2)
        self.assertEqual(first_client.close_calls, 1)
        self.assertEqual(len(stop.waits), 3)

    def test_publish_failure_keeps_initialized_client_for_next_sample(self):
        client = SequenceClient([self._sample(10.0), self._sample(20.0)])
        factory_calls = []

        def client_factory():
            factory_calls.append(True)
            return client

        stop = DeterministicStopEvent(stop_after_waits=2)
        with patch.object(
            collector,
            "publish_sample",
            side_effect=OSError("read-only runtime directory"),
        ) as publish, patch.dict(
            os.environ,
            {"AXGT_GPU_TELEMETRY_INTERVAL_SECONDS": "0.5"},
        ):
            collector.run_collector(stop, client_factory=client_factory)

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(client.sample_calls, 2)
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(len(stop.waits), 2)


if __name__ == "__main__":
    unittest.main()
