import unittest
from urllib.error import URLError

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


if __name__ == "__main__":
    unittest.main()
