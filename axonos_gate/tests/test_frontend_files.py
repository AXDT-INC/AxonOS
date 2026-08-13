import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
FILES_JS = ROOT / "novnc-theme" / "app" / "files" / "axonos-files.js"


class FrontendFileTransferContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FILES_JS.read_text(encoding="utf-8")

    def test_upload_chunk_has_progress_based_stall_detection(self) -> None:
        self.assertIn("const UPLOAD_STALL_MS = 30 * 1000", self.source)
        self.assertIn("const armStallTimer = () =>", self.source)
        self.assertIn("xhr.upload.onprogress", self.source)
        self.assertIn("armStallTimer();", self.source)
        self.assertIn("err.name = 'UploadStallError'", self.source)
        self.assertIn("upload made no progress", self.source)

    def test_pause_and_cancel_release_retry_wait(self) -> None:
        self.assertIn("function _waitForRetry(t, wait)", self.source)
        self.assertIn("t._retryResolve = resolve", self.source)
        self.assertIn("function _cancelRetryWait(t)", self.source)
        self.assertIn("if (t._retryResolve) t._retryResolve()", self.source)

        pause = self.source.split("function _pauseTransfer(t)", 1)[1].split(
            "function _resumeTransfer(t)", 1
        )[0]
        cancel = self.source.split("function _cancelTransfer(t)", 1)[1].split(
            "function _renderTransfers()", 1
        )[0]
        self.assertIn("_cancelRetryWait(t)", pause)
        self.assertIn("_cancelRetryWait(t)", cancel)

    def test_resume_during_pump_shutdown_cannot_strand_queue(self) -> None:
        pump = self.source.split("async function _pumpUploads()", 1)[1].split(
            "async function _runUpload(t)", 1
        )[0]
        self.assertIn("_uploadRunning = false", pump)
        self.assertIn("t.state === 'queued'", pump)
        self.assertIn("queueMicrotask(_pumpUploads)", pump)

    def test_retry_message_exposes_failure_and_retained_offset(self) -> None:
        self.assertIn("err && err.message", self.source)
        self.assertIn("Connection problem${detail}", self.source)
        self.assertIn("kept ${_fmtBytes(t.offset)}", self.source)


if __name__ == "__main__":
    unittest.main()
