import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import snatchimg
import web_app


class ImagefapTests(unittest.TestCase):
    ROOT = "https://www.imagefap.com/pictures/123/example"
    SECOND = ROOT + "?gid=123&page=1&view=0"

    def pages(self):
        return {
            self.ROOT: '''
                <img src="/ad.jpg">
                <a href="/photo/99/?gid=123&page=0"><img src="/thumb99.jpg"></a>
                <a href="/photo/10/?gid=123&page=0">second image</a>
                <a href="?gid=123&amp;page=1&amp;view=0">next</a>
                <a href="/gallery.php?gid=123&amp;page=0&amp;view=0">first</a>
                <a href="/pictures/999/other">unrelated gallery</a>
                <a href="/photo/88/?gid=999">unrelated photo</a>
                <a href="https://evil.test/photo/99/?gid=123">external</a>
                <a href="/gallery.php?tagid=123">category</a>
            ''',
            self.SECOND: '''
                <a href="/photo/3/?gid=123&page=1">third image</a>
                <a href="/photo/99/?gid=123&page=1">duplicate viewer context</a>
                <a href="/gallery/123?page=0&amp;view=0">first alias</a>
            ''',
            **{
                f"https://www.imagefap.com/photo/{photo}/?gid=123&page={page}":
                f'''<img src="/ad.jpg"><img id="mainPhoto" data-src="https://cdnc.imagefap.com/images/frame-thumb/{photo}.jpg">
                    <a imageid="777" original="https://cdnc.imagefap.com/images/full/777.jpg" href="#">neighbor</a>
                    <a imageid="{photo}" original="https://cdnc.imagefap.com/images/full/{photo}.jpg?secure=old" href="#">original</a>'''
                for photo, page in ((99, 0), (10, 0), (3, 1))
            },
        }

    def scan(self, *, limit=2, follow=True, links_only=True, cancel=None, pages=None):
        pages = pages or self.pages()
        sources = {}
        messages = []
        with patch.object(snatchimg, "fetch_text", side_effect=lambda url, *args: pages[url]) as fetch:
            with redirect_stdout(io.StringIO()):
                images = snatchimg.discover_images(
                    self.ROOT, crawl=True, deep=True, links_only=links_only, verbose=False,
                    max_pages=limit, delay=0, timeout=5, user_agent="test",
                    follow_gallery_pages=follow, on_progress=messages.append,
                    image_sources=sources, should_cancel=cancel,
                )
        return images, sources, messages, fetch

    def test_follows_listing_pages_collects_only_current_originals_in_gallery_order(self):
        images, sources, messages, fetch = self.scan()
        self.assertEqual(images, [f"https://cdnc.imagefap.com/images/full/{i}.jpg?secure=old" for i in (99, 10, 3)])
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(len(sources), 3)
        self.assertTrue(sources[images[0]].endswith("/photo/99/?gid=123&page=0"))
        self.assertFalse(any("incomplete" in message for message in messages))

    def test_limit_counts_listing_pages_and_still_reads_their_viewers(self):
        images, sources, messages, fetch = self.scan(limit=1)
        self.assertEqual(len(images), 2)
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(any("Max pages was reached" in message for message in messages))

    def test_follow_disabled_reads_only_first_gallery_page(self):
        images, sources, messages, fetch = self.scan(follow=False)
        self.assertEqual(len(images), 2)
        self.assertEqual(fetch.call_count, 3)

    def test_unchecked_links_only_does_not_collect_ads_or_thumbnails(self):
        images, sources, messages, fetch = self.scan(links_only=False)
        self.assertEqual(len(images), 3)
        self.assertTrue(all("/images/full/" in url for url in images))

    def test_max_pages_zero_preserves_direct_start_page_images(self):
        images, sources, messages, fetch = self.scan(limit=0)
        self.assertIn("https://www.imagefap.com/ad.jpg", images)
        self.assertEqual(fetch.call_count, 1)

    def test_missing_original_warns_without_substituting_thumbnail(self):
        pages = self.pages()
        pages["https://www.imagefap.com/photo/99/?gid=123&page=0"] = '<img id="mainPhoto" data-src="/images/frame-thumb/99.jpg">'
        images, sources, messages, fetch = self.scan(pages=pages)
        self.assertEqual(len(images), 2)
        self.assertTrue(any("Scan incomplete: 1" in message for message in messages))

    def test_cancelled_scan_makes_no_requests(self):
        images, sources, messages, fetch = self.scan(cancel=lambda: True)
        self.assertEqual(images, [])
        fetch.assert_not_called()

    def test_gallery_identity_and_listing_query_boundaries(self):
        for url in (self.ROOT, "https://imagefap.com/gallery/123", "https://www.imagefap.com/gallery.php?gid=123"):
            self.assertEqual(snatchimg.imagefap_gallery_id(url), "123")
        for url in (
            "https://www.imagefap.com/gallery.php?gid=999&page=1",
            "https://imagefap.com.evil.test/gallery/123",
            self.ROOT + "?gid=999&page=1",
            self.ROOT + "?gid=123&page=1&action=delete",
            self.ROOT + "?gid=123&page=1&page=2",
            self.ROOT + "?gid=123&page=-1",
            "https://www.imagefap.com/gallery.php?tagid=123&page=1",
        ):
            with self.subTest(url=url):
                self.assertIsNone(snatchimg.imagefap_listing_page(url, "123"))

    def test_refresh_resolves_signed_original_for_current_photo_only(self):
        viewer = "https://www.imagefap.com/photo/99/?gid=123&page=0"
        html = self.pages()[viewer].replace("secure=old", "secure=new")
        with patch.object(snatchimg, "fetch_text", return_value=html):
            refreshed = snatchimg.refresh_gallery_image(viewer, 5, "test")
        self.assertEqual(refreshed, ("https://cdnc.imagefap.com/images/full/99.jpg?secure=new", viewer))

    def test_photo_links_reject_ambiguous_gallery_ids_and_actions(self):
        for query in ("gid=999&gid=123", "gid=123&action=delete", "gid=123&page=-1"):
            with self.subTest(query=query):
                self.assertIsNone(snatchimg.imagefap_photo_id("https://www.imagefap.com/photo/99/?" + query, "123"))

    def test_web_job_retries_with_refreshed_imagefap_original(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            job = web_app.Job("imagefap-test", self.ROOT)
            viewer = "https://www.imagefap.com/photo/99/?gid=123&page=0"
            html = self.pages()[viewer]
            with patch.object(web_app, "RUNS_DIR", Path(directory)), \
                 patch.object(web_app, "jobs", {job.id: job}), \
                 patch.object(snatchimg.time, "sleep"), \
                 patch.object(snatchimg, "fetch_text", side_effect=[f'<a href="{viewer}">photo</a>', html, html.replace("secure=old", "secure=new")]), \
                 patch.object(snatchimg, "fetch", side_effect=[(b"expired", "text/html"), (b"image", "image/jpeg")]) as fetch:
                web_app.run_job(job.id, {"followGalleryPages": True, "delay": 0})
            self.assertEqual((job.status, job.total, job.saved, job.skipped), ("complete", 1, 1, 0))
            self.assertTrue(fetch.call_args.args[0].endswith("99.jpg?secure=new"))
            self.assertEqual(fetch.call_args.kwargs["referer"], viewer)
            self.assertTrue(job.zip_path.exists())
