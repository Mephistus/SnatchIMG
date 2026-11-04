import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import snatchimg
import web_app


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "images"
        sleep = patch.object(snatchimg.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def save(self, **kwargs):
        return snatchimg.save_image(
            "https://cdn.test/expired.jpg", self.output, 5, "test-agent", 1, 1,
            **kwargs,
        )

    def test_html_response_refreshes_link_and_referer_then_saves(self):
        refresh = Mock(return_value=("https://other.test/fresh.png", "https://e-hentai.org/s/abcdef/123-1"))
        messages = []
        with patch.object(snatchimg, "fetch", side_effect=[
            (b"<html>unavailable</html>", "text/html"), (b"image bytes", "image/png"),
        ]) as fetch:
            saved = self.save(refresh_url=refresh, on_progress=messages.append)
        self.assertEqual(saved.name, "01.png")
        self.assertEqual(saved.read_bytes(), b"image bytes")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0], "https://other.test/fresh.png")
        self.assertEqual(fetch.call_args.kwargs["referer"], refresh.return_value[1])
        refresh.assert_called_once()
        self.assertTrue(any("Retrying" in message for message in messages))

    def test_timeout_and_connection_failure_retry_successfully(self):
        for error in (TimeoutError("secret host"), URLError("private network info")):
            with self.subTest(error=type(error).__name__):
                with patch.object(snatchimg, "fetch", side_effect=[error, (b"image", "image/jpeg")]) as fetch:
                    self.assertIsNotNone(self.save())
                    self.assertEqual(fetch.call_count, 2)

    def test_failed_retries_are_bounded_and_keep_public_messages_safe(self):
        messages = []
        refresh = Mock(side_effect=URLError("secret local path"))
        with patch.object(snatchimg, "fetch", side_effect=TimeoutError("private host")) as fetch:
            self.assertIsNone(self.save(refresh_url=refresh, on_progress=messages.append))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(refresh.call_count, 2)
        self.assertFalse(self.output.exists())
        self.assertTrue(any("failed" in message for message in messages))
        self.assertFalse(any("secret" in message or "private" in message for message in messages))

    def test_duplicates_do_not_retry_or_refresh(self):
        refresh = Mock()
        hashes = set()
        with patch.object(snatchimg, "fetch", return_value=(b"same content", "image/jpeg")) as fetch:
            self.assertIsNotNone(self.save(seen_hashes=hashes, refresh_url=refresh))
            self.assertIsNone(self.save(seen_hashes=hashes, refresh_url=refresh))
        self.assertEqual(fetch.call_count, 2)
        refresh.assert_not_called()

    def test_cancellation_stops_before_refresh_or_next_request(self):
        cancelled = False

        def fail(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            raise TimeoutError()

        refresh = Mock()
        with patch.object(snatchimg, "fetch", side_effect=fail) as fetch:
            self.assertIsNone(self.save(refresh_url=refresh, should_cancel=lambda: cancelled))
        fetch.assert_called_once()
        refresh.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_site_reload_action_selects_new_main_image(self):
        viewer = "https://e-hentai.org/s/abcdef/123-1"
        with patch.object(snatchimg, "fetch_text", side_effect=[
            '<img id="img" src="https://old.test/1.jpg">'
            '<a id="loadfail" onclick="return nl(\'51866-498280\')">reload</a>',
            '<img src="/navigation.jpg"><img id="img" src="https://new.test/1.jpg">',
        ]) as fetch:
            result = snatchimg.refresh_gallery_image(viewer, 5, "test-agent")
        self.assertEqual(result, ("https://new.test/1.jpg", viewer + "?nl=51866-498280"))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0], viewer + "?nl=51866-498280")

    def test_final_forbidden_failure_records_one_skip_reason(self):
        reasons = []
        with patch.object(snatchimg, "fetch", side_effect=HTTPError("url", 403, "Forbidden", None, None)) as fetch:
            self.assertIsNone(self.save(refresh_url=Mock(return_value=None), skip_reasons=reasons))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(reasons, [snatchimg.FORBIDDEN_IMAGE_SKIP])

    def test_web_job_refreshes_failed_image_and_counts_it_as_saved(self):
        viewer = "https://e-hentai.org/s/abcdef/123-1"
        job = web_app.Job("retry-job", "https://e-hentai.org/g/123/abcdef/")

        with patch.object(web_app, "RUNS_DIR", self.output.parent / "runs"), \
             patch.object(web_app, "jobs", {job.id: job}), \
             patch.object(snatchimg, "fetch_text", side_effect=[
                 f'<a href="{viewer}">image</a>',
                 '<img id="img" src="https://cdn.test/expired.jpg">',
                 '<img id="img" src="https://new.test/fresh.jpg">',
             ]), \
             patch.object(snatchimg, "fetch", side_effect=[(b"html", "text/html"), (b"image", "image/jpeg")]):
            web_app.run_job(job.id, {"delay": 0, "followGalleryPages": True})
        self.assertEqual((job.status, job.saved, job.skipped, job.total), ("complete", 1, 0, 1))
        self.assertTrue(job.zip_path.exists())
        self.assertTrue(any("Retrying image download" in message for message in job.logs))
