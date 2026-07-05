import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"
APP_JS = STATIC_DIR / "app.js"


class StaticIndexParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []
        self.info_tips = []
        self._span_stack = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "link" and attrs_dict.get("rel") == "stylesheet":
            self.assets.append(attrs_dict.get("href", ""))
        if tag == "script" and attrs_dict.get("src"):
            self.assets.append(attrs_dict["src"])

        if tag == "span":
            classes = set(attrs_dict.get("class", "").split())
            entry = {"classes": classes, "text": ""}
            if "info-tip" in classes:
                self.info_tips.append(attrs_dict)
            self._span_stack.append(entry)

    def handle_data(self, data):
        for entry in self._span_stack:
            entry["text"] += data

    def handle_endtag(self, tag):
        if tag == "span" and self._span_stack:
            child = self._span_stack.pop()
            if self._span_stack:
                self._span_stack[-1]["text"] += child["text"]


class StaticUiStructureTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX_HTML.read_text(encoding="utf-8")
        self.css = STYLES_CSS.read_text(encoding="utf-8")
        self.app_js = APP_JS.read_text(encoding="utf-8")
        self.parser = StaticIndexParser()
        self.parser.feed(self.html)

    def test_option_info_icons_have_accessible_tooltips(self):
        tips_by_label = {
            tip.get("aria-label"): tip.get("data-tooltip", "")
            for tip in self.parser.info_tips
        }

        self.assertEqual(
            set(tips_by_label),
            {"Max pages info", "Deep option info", "Links only option info"},
        )
        self.assertIn("same-site pages", tips_by_label["Max pages info"])
        self.assertIn("Use 0", tips_by_label["Max pages info"])
        self.assertIn("full-size images", tips_by_label["Deep option info"])
        self.assertIn("starting page", tips_by_label["Links only option info"])

        for tip in self.parser.info_tips:
            self.assertEqual(tip.get("tabindex"), "0")
            self.assertEqual(tip.get("role"), "button")

    def test_option_info_tooltip_css_supports_hover_and_keyboard_focus(self):
        self.assertIn(".info-tip:hover::after", self.css)
        self.assertIn(".info-tip:focus::after", self.css)
        self.assertIn(".info-tip:hover::before", self.css)
        self.assertIn(".info-tip:focus::before", self.css)
        self.assertIn("content: attr(data-tooltip)", self.css)

    def test_skipped_count_renders_below_file_count(self):
        self.assertIn('<span class="count-stack">', self.html)
        self.assertIn('<span class="count-stack">', self.app_js)
        self.assertIn('<span class="skip-count">${skippedCount} skipped</span>', self.app_js)
        self.assertLess(
            self.app_js.index('<span class="file-count">${savedCount} / ${totalCount} Files</span>'),
            self.app_js.index('${skippedLabel}'),
        )
        self.assertIn("flex-direction: column", self.css)
        self.assertIn(".meter-count .count-stack", self.css)

    def test_cache_busted_static_assets_resolve_to_files(self):
        self.assertGreaterEqual(len(self.parser.assets), 2)

        for asset in self.parser.assets:
            parsed = urlparse(asset)
            self.assertEqual(parsed.path.startswith("/static/"), True, asset)
            asset_path = STATIC_DIR / parsed.path.removeprefix("/static/")
            self.assertTrue(asset_path.is_file(), asset)
            self.assertTrue(parsed.query.startswith("v="), asset)


if __name__ == "__main__":
    unittest.main()
