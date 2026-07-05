import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

import snatchimg


class DiscoverImagesTests(unittest.TestCase):
    def setUp(self):
        self.original_fetch_text = snatchimg.fetch_text
        self.original_post_text = snatchimg.post_text
        self.original_sleep = snatchimg.time.sleep
        snatchimg.time.sleep = lambda _seconds: None

    def tearDown(self):
        snatchimg.fetch_text = self.original_fetch_text
        snatchimg.post_text = self.original_post_text
        snatchimg.time.sleep = self.original_sleep

    def test_max_pages_zero_links_only_keeps_starting_page_images(self):
        calls = []

        def fake_fetch_text(url, timeout, user_agent):
            calls.append(url)
            return """
                <img src="/direct.jpg">
                <a href="/linked.png">image</a>
                <a href="/next">next page</a>
            """

        snatchimg.fetch_text = fake_fetch_text

        images = snatchimg.discover_images(
            "https://example.test/gallery",
            crawl=False,
            deep=True,
            links_only=True,
            verbose=False,
            max_pages=0,
            delay=0,
            timeout=5,
            user_agent="test-agent",
        )

        self.assertEqual(calls, ["https://example.test/gallery"])
        self.assertEqual(
            images,
            [
                "https://example.test/direct.jpg",
                "https://example.test/linked.png",
            ],
        )

    def test_starting_page_fetch_error_is_user_facing(self):
        def fake_fetch_text(url, timeout, user_agent):
            raise URLError("blocked")

        snatchimg.fetch_text = fake_fetch_text

        with self.assertRaises(snatchimg.PageAccessError) as raised:
            snatchimg.discover_images(
                "https://example.test/gallery",
                crawl=False,
                deep=True,
                links_only=False,
                verbose=False,
                max_pages=1,
                delay=0,
                timeout=5,
                user_agent="test-agent",
            )

        self.assertIn("could not be accessed", str(raised.exception))

    def test_links_only_skips_start_page_direct_images_when_pages_can_continue(self):
        pages = {
            "https://example.test/gallery": """
                <img src="/direct.jpg">
                <a href="/image/123">view image</a>
            """,
            "https://example.test/image/123": """
                <img src="/detail.jpg">
            """,
        }

        def fake_fetch_text(url, timeout, user_agent):
            return pages[url]

        snatchimg.fetch_text = fake_fetch_text

        images = snatchimg.discover_images(
            "https://example.test/gallery",
            crawl=False,
            deep=True,
            links_only=True,
            verbose=False,
            max_pages=2,
            delay=0,
            timeout=5,
            user_agent="test-agent",
        )

        self.assertEqual(images, ["https://example.test/detail.jpg"])

    def test_cancel_before_scan_does_not_fetch_pages(self):
        def fake_fetch_text(url, timeout, user_agent):
            raise AssertionError("cancelled runs should not fetch pages")

        snatchimg.fetch_text = fake_fetch_text

        images = snatchimg.discover_images(
            "https://example.test/gallery",
            crawl=False,
            deep=True,
            links_only=False,
            verbose=False,
            max_pages=1,
            delay=0,
            timeout=5,
            user_agent="test-agent",
            should_cancel=lambda: True,
        )

        self.assertEqual(images, [])


class ParserTests(unittest.TestCase):
    def test_parser_collects_lazy_and_srcset_images(self):
        parser = snatchimg.ImagePageParser("https://example.test/gallery/page.html")
        parser.feed(
            """
            <img data-src="../lazy.webp">
            <source srcset="/small.jpg 480w, /large.jpg 960w">
            """
        )

        self.assertEqual(
            parser.image_urls,
            {
                "https://example.test/lazy.webp",
                "https://example.test/small.jpg",
                "https://example.test/large.jpg",
            },
        )


class RequestHeaderTests(unittest.TestCase):
    def test_browser_headers_include_accept_language_and_optional_referer(self):
        headers = snatchimg.browser_headers(
            "test-agent",
            accept=snatchimg.IMAGE_ACCEPT,
            referer="https://example.test/gallery",
        )

        self.assertEqual(headers["User-Agent"], "test-agent")
        self.assertEqual(headers["Accept"], snatchimg.IMAGE_ACCEPT)
        self.assertEqual(headers["Accept-Language"], snatchimg.DEFAULT_ACCEPT_LANGUAGE)
        self.assertEqual(headers["Referer"], "https://example.test/gallery")


class SaveImageTests(unittest.TestCase):
    def setUp(self):
        self.original_fetch = snatchimg.fetch
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())

    def tearDown(self):
        snatchimg.fetch = self.original_fetch
        self.temp_dir.cleanup()

    def test_different_urls_with_same_image_content_are_saved_once(self):
        def fake_fetch(url, timeout, user_agent, **kwargs):
            return b"same image bytes", "image/jpeg"

        snatchimg.fetch = fake_fetch
        output_dir = Path(self.temp_dir.name) / "images"
        seen_hashes = set()

        first = snatchimg.save_image(
            "https://example.test/first.jpg",
            output_dir,
            timeout=5,
            user_agent="test-agent",
            index=1,
            total=2,
            seen_hashes=seen_hashes,
        )
        second = snatchimg.save_image(
            "https://cdn.example.test/copy.jpg",
            output_dir,
            timeout=5,
            user_agent="test-agent",
            index=2,
            total=2,
            seen_hashes=seen_hashes,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual([path.name for path in output_dir.iterdir()], ["01.jpg"])

    def test_save_image_uses_image_accept_and_referer(self):
        calls = []

        def fake_fetch(url, timeout, user_agent, **kwargs):
            calls.append((url, kwargs))
            return b"image bytes", "image/jpeg"

        snatchimg.fetch = fake_fetch
        output_dir = Path(self.temp_dir.name) / "images"

        saved = snatchimg.save_image(
            "https://cdn.example.test/photo.jpg",
            output_dir,
            timeout=5,
            user_agent="test-agent",
            index=1,
            total=1,
            referer="https://example.test/gallery",
        )

        self.assertIsNotNone(saved)
        self.assertEqual(calls[0][1]["accept"], snatchimg.IMAGE_ACCEPT)
        self.assertEqual(calls[0][1]["referer"], "https://example.test/gallery")

    def test_forbidden_image_records_safe_skip_reason(self):
        def fake_fetch(url, timeout, user_agent, **kwargs):
            raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

        snatchimg.fetch = fake_fetch
        output_dir = Path(self.temp_dir.name) / "images"
        skip_reasons = []

        saved = snatchimg.save_image(
            "https://cdn.example.test/photo.jpg",
            output_dir,
            timeout=5,
            user_agent="test-agent",
            index=1,
            total=1,
            skip_reasons=skip_reasons,
        )

        self.assertIsNone(saved)
        self.assertEqual(skip_reasons, [snatchimg.FORBIDDEN_IMAGE_SKIP])


if __name__ == "__main__":
    unittest.main()
