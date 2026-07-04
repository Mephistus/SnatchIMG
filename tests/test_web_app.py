import tempfile
import unittest
from pathlib import Path

import web_app


class RunJobTests(unittest.TestCase):
    def setUp(self):
        self.original_runs_dir = web_app.RUNS_DIR
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


class PathContainmentTests(unittest.TestCase):
    def test_static_and_run_containment_rejects_parent_paths(self):
        self.assertTrue(web_app.is_relative_to(web_app.STATIC_DIR / "app.js", web_app.STATIC_DIR))
        self.assertFalse(web_app.is_relative_to(web_app.STATIC_DIR / ".." / "web_app.py", web_app.STATIC_DIR))
        self.assertTrue(web_app.is_relative_to(web_app.RUNS_DIR / "job" / "images", web_app.RUNS_DIR))
        self.assertFalse(web_app.is_relative_to(web_app.RUNS_DIR / ".." / "web_app.py", web_app.RUNS_DIR))


if __name__ == "__main__":
    unittest.main()
