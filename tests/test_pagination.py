import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch

import snatchimg


class EhGalleryTests(unittest.TestCase):
    ROOT = "https://e-hentai.org/g/349653/86d9589590/"

    def scan(self, limit=16, missing_main_image=False):
        pages = {}
        for listing in range(16):
            url = self.ROOT if listing == 0 else self.ROOT + f"?p={listing}"
            pages[url] = (
                '<img src="https://ehgt.org/rating.png">'
                '<a href="/tag/something">tag</a>'
                '<a href="/g/999/abcdef/">unrelated gallery</a>'
                '<a href="/s/abcdef/999-1">unrelated viewer</a>'
                '<a href="https://other.test/s/abcdef/349653-1">external</a>'
                '<a href="/g/349653/86d9589590/?p=0">first</a>'
            )
            if listing < 15:
                pages[url] += f'<a href="?p={listing + 1}">next</a>'
            for number in range(listing * 20 + 1, min((listing + 1) * 20, 305) + 1):
                pages[url] += f'<a href="/s/abcdef/349653-{number}"><img src="/thumb{number}.jpg"></a>'
                pages[f"https://e-hentai.org/s/abcdef/349653-{number}"] = (
                    '<img src="https://ehgt.org/next.png"><img src="/advert.jpg">'
                    f'<img id="img" src="https://cdn.test/{number}.jpg">'
                    f'<a href="/fullimg/349653/{number}/original.jpg">Download original image</a>'
                    '<a href="/s/abcdef/999-1">view image</a>'
                )
        if missing_main_image:
            pages["https://e-hentai.org/s/abcdef/349653-1"] = '<img src="/navigation.jpg">'
        messages = []
        with patch.object(snatchimg, "fetch_text", side_effect=lambda url, *args: pages[url]) as fetch:
            with redirect_stdout(io.StringIO()):
                images = snatchimg.discover_images(
                    self.ROOT, crawl=True, deep=True, links_only=True, verbose=False,
                    max_pages=limit, delay=0, timeout=5, user_agent="test-agent",
                    follow_gallery_pages=True, on_progress=messages.append,
                )
        return images, fetch, messages

    def test_16_listing_pages_include_all_305_main_images_in_order(self):
        images, fetch, messages = self.scan()
        self.assertEqual(images, [f"https://cdn.test/{i}.jpg" for i in range(1, 306)])
        self.assertEqual(fetch.call_count, 321)
        self.assertFalse(any("incomplete" in message for message in messages))

    def test_listing_limit_keeps_image_pages_and_reports_truncation(self):
        images, fetch, messages = self.scan(limit=1)
        self.assertEqual(images, [f"https://cdn.test/{i}.jpg" for i in range(1, 21)])
        self.assertEqual(fetch.call_count, 21)
        self.assertTrue(any("Max pages was reached" in message for message in messages))

    def test_host_and_gallery_id_validation(self):
        self.assertEqual(snatchimg.eh_gallery_id(self.ROOT), "349653")
        self.assertIsNone(snatchimg.eh_gallery_id("https://e-hentai.org.evil.test/g/349653/abcdef/"))
        self.assertIsNone(snatchimg.eh_image_number("https://e-hentai.org/s/abcdef/999-1", self.ROOT, "349653"))

    def test_missing_main_image_warns_instead_of_collecting_navigation(self):
        images, fetch, messages = self.scan(missing_main_image=True)
        self.assertEqual(len(images), 304)
        self.assertTrue(all(url.startswith("https://cdn.test/") for url in images))
        self.assertTrue(any("Scan incomplete: 1" in message for message in messages))


class GalleryPaginationTests(unittest.TestCase):
    def scan(self, *, follow=True, limit=20, deep=True):
        root = "https://example.test/g/123/token/"
        pages = {}
        for number in range(3):
            url = root if number == 0 else root + f"?p={number}"
            pages[url] = (
                f'<img src="/thumb{number}.jpg">'
                f'<a href="/s/{number}">image</a>'
                f'<a href="{root}">1</a>'
            )
            if number < 2:
                pages[url] += f'<a href="?p={number + 1}">next</a>'
            pages[f"https://example.test/s/{number}"] = f'<img src="/full{number}.jpg">'
        with patch.object(snatchimg, "fetch_text", side_effect=lambda url, *args: pages[url]) as fetch:
            images = snatchimg.discover_images(
                root, crawl=False, deep=deep, links_only=deep, verbose=False,
                max_pages=limit, delay=0, timeout=5, user_agent="test-agent",
                follow_gallery_pages=follow,
            )
        return images, [call.args[0] for call in fetch.call_args_list]

    def test_opens_image_links_on_every_gallery_page_without_revisits(self):
        images, calls = self.scan()
        self.assertEqual(set(images), {
            f"https://example.test/full{number}.jpg" for number in range(3)
        })
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(calls), len(set(calls)))

    def test_respects_limits_including_start_page_only(self):
        for limit in (0, 1, 3):
            with self.subTest(limit=limit):
                images, calls = self.scan(limit=limit)
                self.assertEqual(len(calls), max(1, limit))
                if limit == 0:
                    self.assertEqual(images, ["https://example.test/thumb0.jpg"])

    def test_disabled_option_preserves_existing_scan(self):
        images, calls = self.scan(follow=False)
        self.assertNotIn("https://example.test/full1.jpg", images)
        self.assertNotIn("https://example.test/g/123/token/?p=2", calls)

    def test_inline_images_without_deep(self):
        images, calls = self.scan(deep=False)
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(images), {
            f"https://example.test/thumb{number}.jpg" for number in range(3)
        })

    def test_matching_preserves_gallery_and_nonpagination_parameters(self):
        root = "https://example.test/g/123/token/?lang=en"
        for query in ("p=1", "page=2", "pg=3", "offset=40", "start=20"):
            self.assertTrue(snatchimg.is_gallery_page(root + "&" + query, root))
        for url in (
            "https://other.test/g/123/token/?lang=en&p=1",
            "https://example.test/g/456/token/?lang=en&p=1",
            "https://example.test/g/123/token/?lang=fr&p=1",
            "https://example.test/g/123/token/?lang=en&sort=new",
            "https://example.test/g/123/token/?lang=en&p=invalid",
        ):
            with self.subTest(url=url):
                self.assertFalse(snatchimg.is_gallery_page(url, root))
