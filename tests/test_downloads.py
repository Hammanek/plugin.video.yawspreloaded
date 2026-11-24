import os
import tempfile
import unittest

from requests import HTTPError

from tests.support.http_stubs import FakeResponse
from yawsp_app.downloads import DownloadError, DownloadReceipt, stream_download


class LocalFileOps:
    """Local implementation of the injected VFS operations with call records."""

    def __init__(self):
        self.write_paths = []
        self.replacements = []

    def open_write(self, path):
        self.write_paths.append(path)
        return open(path, "wb")

    def open_read(self, path):
        return open(path, "rb")

    def replace(self, source, destination):
        self.replacements.append((source, destination))
        return os.replace(source, destination)

    def remove(self, path):
        return os.remove(path)

    def exists(self, path):
        return os.path.exists(path)


class ProgressRecorder:
    def __init__(self, interval=10):
        self.interval = interval
        self.calls = []

    def __call__(self, percent, display_name):
        self.calls.append((percent, display_name))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.destination = os.path.join(self.tempdir.name, "movie.mkv")
        self.file_ops = LocalFileOps()

    def response(self, **kwargs):
        kwargs.setdefault("forbid_content", True)
        return FakeResponse(**kwargs)

    def test_http_error_creates_no_destination_or_temporary_file(self):
        response = self.response(status_code=403, chunks=[b"forbidden"])

        with self.assertRaises(HTTPError):
            stream_download(
                response, self.destination, "Movie", self.file_ops, ProgressRecorder()
            )

        self.assertEqual(response.raise_for_status_calls, 1)
        self.assertTrue(response.closed)
        self.assertFalse(os.path.exists(self.destination))
        self.assertEqual(self.file_ops.write_paths, [])
        self.assertEqual(os.listdir(self.tempdir.name), [])

    def test_missing_length_and_disabled_progress_still_stream_every_chunk(self):
        response = self.response(chunks=[b"first", b"", b"-second"])

        receipt = stream_download(response, self.destination, "Movie", self.file_ops)

        self.assertEqual(
            receipt, DownloadReceipt(destination=self.destination, bytes_written=12)
        )
        with open(self.destination, "rb") as downloaded:
            self.assertEqual(downloaded.read(), b"first-second")
        self.assertTrue(response.closed)

    def test_interrupted_stream_closes_response_and_removes_partial_file(self):
        response = self.response(
            chunks=[b"partial"], stream_error=RuntimeError("connection interrupted")
        )
        progress = ProgressRecorder(interval=1)

        with self.assertRaisesRegex(RuntimeError, "connection interrupted"):
            stream_download(
                response, self.destination, "Movie", self.file_ops, progress
            )

        self.assertTrue(response.closed)
        self.assertFalse(os.path.exists(self.destination))
        self.assertEqual(os.listdir(self.tempdir.name), [])
        self.assertNotIn((100, "Movie"), progress.calls)

    def test_zero_length_and_zero_progress_interval_do_not_divide_by_zero(self):
        response = self.response(headers={"Content-Length": "0"}, chunks=[])
        progress = ProgressRecorder(interval=0)

        receipt = stream_download(
            response, self.destination, "Empty", self.file_ops, progress
        )

        self.assertEqual(receipt.bytes_written, 0)
        self.assertEqual(os.path.getsize(self.destination), 0)
        self.assertEqual(progress.calls, [])
        self.assertTrue(response.closed)

    def test_success_atomically_replaces_target_and_reports_exact_bytes(self):
        with open(self.destination, "wb") as existing:
            existing.write(b"old")
        response = self.response(
            headers={"content-length": "6"}, chunks=[b"abc", b"def"]
        )
        progress = ProgressRecorder(interval=1)

        receipt = stream_download(
            response, self.destination, "Replacement", self.file_ops, progress
        )

        self.assertEqual(receipt.destination, self.destination)
        self.assertEqual(receipt.bytes_written, 6)
        with open(self.destination, "rb") as downloaded:
            self.assertEqual(downloaded.read(), b"abcdef")
        self.assertEqual(len(self.file_ops.replacements), 1)
        source, target = self.file_ops.replacements[0]
        self.assertEqual(target, self.destination)
        self.assertTrue(source.startswith(self.destination + ".part-"))
        self.assertFalse(os.path.exists(source))
        self.assertEqual(progress.calls[-1], (100, "Replacement"))
        percentages = [percent for percent, _ in progress.calls]
        self.assertEqual(len(percentages), len(set(percentages)))

    def test_short_response_for_declared_length_is_not_promoted(self):
        response = self.response(
            headers={"Content-Length": "10"}, chunks=[b"short"]
        )

        with self.assertRaisesRegex(DownloadError, "incomplete"):
            stream_download(response, self.destination, "Movie", self.file_ops)

        self.assertTrue(response.closed)
        self.assertFalse(os.path.exists(self.destination))
        self.assertEqual(os.listdir(self.tempdir.name), [])

    def test_invalid_progress_interval_falls_back_without_losing_data(self):
        response = self.response(headers={"Content-Length": "3"}, chunks=[b"abc"])
        progress = ProgressRecorder(interval="not-a-number")

        receipt = stream_download(
            response, self.destination, "Movie", self.file_ops, progress
        )

        self.assertEqual(receipt.bytes_written, 3)
        with open(self.destination, "rb") as downloaded:
            self.assertEqual(downloaded.read(), b"abc")


if __name__ == "__main__":
    unittest.main()
