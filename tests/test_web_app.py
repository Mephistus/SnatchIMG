import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

import web_app


class RunJobTests(unittest.TestCase):
    def setUp(self):
        self.original_runs_dir = web_app.RUNS_DIR
        self.original_fetch = web_app.snatchimg.fetch
        self.original_discover_images = web_app.snatchimg.discover_images
        self.original_save_image = web_app.snatchimg.save_image
        self.original_make_archive = web_app.shutil.make_archive
        self.original_sleep = web_app.time.sleep
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        web_app.RUNS_DIR = Path(self.temp_dir.name) / ".snatchimg_runs"
        web_app.RUNS_DIR.mkdir()
        web_app.jobs.clear()
        web_app.time.sleep = lambda _seconds: None

    def tearDown(self):
        web_app.time.sleep = self.original_sleep
        web_app.shutil.make_archive = self.original_make_archive
        web_app.snatchimg.save_image = self.original_save_image
        web_app.snatchimg.discover_images = self.original_discover_images
        web_app.snatchimg.fetch = self.original_fetch
        web_app.jobs.clear()
        web_app.RUNS_DIR = self.original_runs_dir
        self.temp_dir.cleanup()

    def test_zero_discovered_images_marks_error_and_removes_run_folder(self):
        job_id = "job-zero-images"
        run_dir = web_app.RUNS_DIR / job_id
        run_dir.mkdir(parents=True)
        (run_dir / "leftover.txt").write_text("old run data", encoding="utf-8")
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")
        web_app.snatchimg.discover_images = lambda *args, **kwargs: []

        web_app.run_job(job_id, {"maxPages": 0, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, web_app.PUBLIC_NO_IMAGES_ERROR)
        self.assertIsNone(job.zip_path)
        self.assertFalse(run_dir.exists())

    def test_zero_saved_images_marks_error_cleans_up_and_does_not_zip(self):
        job_id = "job-zero-saved"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")
        web_app.snatchimg.discover_images = lambda *args, **kwargs: [
            "https://example.test/image.jpg"
        ]
        web_app.snatchimg.save_image = lambda *args, **kwargs: None

        def fail_make_archive(*args, **kwargs):
            raise AssertionError("ZIP should not be created without saved images")

        web_app.shutil.make_archive = fail_make_archive

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "error")
        self.assertEqual(job.phase, "No images saved")
        self.assertEqual(job.error, web_app.PUBLIC_NO_IMAGES_ERROR)
        self.assertIsNone(job.zip_path)
        self.assertFalse((web_app.RUNS_DIR / job_id).exists())

    def test_successful_job_saves_images_and_creates_zip(self):
        job_id = "job-success"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")
        web_app.snatchimg.discover_images = lambda *args, **kwargs: [
            "https://example.test/first.jpg",
            "https://example.test/second.jpg",
        ]

        def fake_save_image(
            url, output_dir, timeout, user_agent, index, total, seen_hashes=None, **kwargs
        ):
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:02d}.jpg"
            path.write_bytes(url.encode("utf-8"))
            return path

        def fake_make_archive(base_name, format, root_dir):
            self.assertEqual(format, "zip")
            self.assertEqual(Path(root_dir), web_app.RUNS_DIR / job_id / "images")
            zip_path = Path(f"{base_name}.zip")
            zip_path.write_bytes(b"zip")
            return str(zip_path)

        web_app.snatchimg.save_image = fake_save_image
        web_app.shutil.make_archive = fake_make_archive

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "complete")
        self.assertEqual(job.phase, "Ready")
        self.assertEqual(job.total, 2)
        self.assertEqual(job.saved, 2)
        self.assertEqual(job.skipped, 0)
        self.assertEqual(job.progress, 100)
        self.assertTrue(job.zip_path.exists())

    def test_duplicate_discovered_images_are_saved_once(self):
        job_id = "job-duplicates"
        saved_urls = []
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")
        web_app.snatchimg.discover_images = lambda *args, **kwargs: [
            "https://example.test/same.jpg",
            "https://example.test/same.jpg",
            "https://example.test/other.jpg",
            "https://example.test/same.jpg",
        ]

        def fake_save_image(
            url, output_dir, timeout, user_agent, index, total, seen_hashes=None, **kwargs
        ):
            saved_urls.append((url, index, total))
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:02d}.jpg"
            path.write_bytes(url.encode("utf-8"))
            return path

        def fake_make_archive(base_name, format, root_dir):
            zip_path = Path(f"{base_name}.zip")
            zip_path.write_bytes(b"zip")
            return str(zip_path)

        web_app.snatchimg.save_image = fake_save_image
        web_app.shutil.make_archive = fake_make_archive

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "complete")
        self.assertEqual(job.total, 2)
        self.assertEqual(job.saved, 2)
        self.assertEqual(job.skipped, 0)
        self.assertEqual(
            saved_urls,
            [
                ("https://example.test/same.jpg", 1, 2),
                ("https://example.test/other.jpg", 2, 2),
            ],
        )

    def test_different_urls_with_same_image_content_are_saved_once(self):
        job_id = "job-same-content"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")
        web_app.snatchimg.discover_images = lambda *args, **kwargs: [
            "https://example.test/first.jpg",
            "https://cdn.example.test/copy.jpg",
        ]
        web_app.snatchimg.fetch = lambda *args, **kwargs: (
            b"same image bytes",
            "image/jpeg",
        )

        def fake_make_archive(base_name, format, root_dir):
            zip_path = Path(f"{base_name}.zip")
            zip_path.write_bytes(b"zip")
            return str(zip_path)

        web_app.shutil.make_archive = fake_make_archive

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        image_dir = web_app.RUNS_DIR / job_id / "images"
        self.assertEqual(job.status, "complete")
        self.assertEqual(job.total, 2)
        self.assertEqual(job.saved, 1)
        self.assertEqual(job.skipped, 1)
        self.assertEqual(job.progress, 100)
        self.assertEqual([path.name for path in image_dir.iterdir()], ["01.jpg"])

    def test_forbidden_image_skips_use_friendly_public_error(self):
        job_id = "job-forbidden-images"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test/gallery")
        web_app.snatchimg.discover_images = lambda *args, **kwargs: [
            "https://cdn.example.test/photo.jpg",
        ]

        def fake_save_image(*args, **kwargs):
            kwargs["skip_reasons"].append(web_app.snatchimg.FORBIDDEN_IMAGE_SKIP)
            return None

        def fail_make_archive(*args, **kwargs):
            raise AssertionError("ZIP should not be created without saved images")

        web_app.snatchimg.save_image = fake_save_image
        web_app.shutil.make_archive = fail_make_archive

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, web_app.PUBLIC_FORBIDDEN_IMAGES_ERROR)
        self.assertEqual(job.skipped, 1)
        self.assertIn("site refused access", job.logs[-1])

    def test_user_facing_error_message_is_preserved(self):
        job_id = "job-user-error"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")
        message = "The page could not be accessed."

        def fail_discovery(*args, **kwargs):
            raise web_app.snatchimg.UserFacingError(message)

        web_app.snatchimg.discover_images = fail_discovery

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, message)
        self.assertIn(message, job.logs[-1])

    def test_unexpected_error_uses_public_error_message(self):
        job_id = "job-private-error"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test")

        def fail_discovery(*args, **kwargs):
            raise RuntimeError(r"C:\secret\internal-path exploded")

        web_app.snatchimg.discover_images = fail_discovery

        web_app.run_job(job_id, {"maxPages": 1, "delay": 0, "timeout": 5})

        job = web_app.jobs[job_id]
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, web_app.PUBLIC_SCAN_ERROR)
        self.assertIn(web_app.PUBLIC_SCAN_ERROR, job.logs[-1])
        self.assertNotIn("secret", job.error)
        self.assertNotIn("internal-path", job.logs[-1])


class PathContainmentTests(unittest.TestCase):
    def test_static_and_run_containment_rejects_parent_paths(self):
        self.assertTrue(web_app.is_relative_to(web_app.STATIC_DIR / "app.js", web_app.STATIC_DIR))
        self.assertFalse(web_app.is_relative_to(web_app.STATIC_DIR / ".." / "web_app.py", web_app.STATIC_DIR))
        self.assertTrue(web_app.is_relative_to(web_app.RUNS_DIR / "job" / "images", web_app.RUNS_DIR))
        self.assertFalse(web_app.is_relative_to(web_app.RUNS_DIR / ".." / "web_app.py", web_app.RUNS_DIR))


class HandlerSecurityTests(unittest.TestCase):
    def setUp(self):
        web_app.jobs.clear()

    def tearDown(self):
        web_app.jobs.clear()

    def make_handler(self, path="/api/jobs"):
        handler = object.__new__(web_app.SnatchHandler)
        handler.path = path
        handler.sent_json = None
        handler.sent_error = None
        handler.static_path = None

        def send_json(data, status=HTTPStatus.OK):
            handler.sent_json = (data, status)

        def send_error(status):
            handler.sent_error = status

        def send_static(path):
            handler.static_path = path

        handler.send_json = send_json
        handler.send_error = send_error
        handler.send_static = send_static
        return handler

    def test_tampered_post_cannot_submit_empty_url(self):
        handler = self.make_handler()
        handler.read_json = lambda: {"url": ""}

        handler.do_POST()

        data, status = handler.sent_json
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(data["error"], "A website URL is required.")
        self.assertEqual(web_app.jobs, {})

    def test_tampered_post_cannot_submit_negative_max_pages(self):
        handler = self.make_handler()
        handler.read_json = lambda: {
            "url": "https://example.test",
            "maxPages": -1,
        }

        handler.do_POST()

        data, status = handler.sent_json
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(data["error"], "Max pages must be at least 0.")
        self.assertEqual(web_app.jobs, {})

    def test_tampered_post_cannot_submit_non_numeric_max_pages(self):
        handler = self.make_handler()
        handler.read_json = lambda: {
            "url": "https://example.test",
            "maxPages": "unlimited",
        }

        handler.do_POST()

        data, status = handler.sent_json
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("invalid literal", data["error"])
        self.assertEqual(web_app.jobs, {})

    def test_download_url_is_hidden_until_job_is_complete(self):
        job_id = "job-not-ready"
        web_app.jobs[job_id] = web_app.Job(job_id, "https://example.test", status="running")
        handler = self.make_handler(f"/api/jobs/{job_id}")

        handler.send_job_status(handler.path)

        data, status = handler.sent_json
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIsNone(data["downloadUrl"])

    def test_tampered_download_path_cannot_read_files_outside_runs_dir(self):
        job_id = "job-outside-zip"
        web_app.jobs[job_id] = web_app.Job(
            job_id,
            "https://example.test",
            status="complete",
            zip_path=web_app.ROOT / "web_app.py",
        )
        handler = self.make_handler(f"/api/jobs/{job_id}/download")

        handler.send_job_download(handler.path)

        self.assertEqual(handler.sent_error, HTTPStatus.NOT_FOUND)

    def test_encoded_static_traversal_is_sent_to_containment_check(self):
        handler = self.make_handler("/static/%2e%2e/web_app.py")

        handler.do_GET()

        self.assertEqual(handler.static_path, web_app.STATIC_DIR / ".." / "web_app.py")
        self.assertFalse(web_app.is_relative_to(handler.static_path, web_app.STATIC_DIR))


if __name__ == "__main__":
    unittest.main()
