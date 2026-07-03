#!/usr/bin/env python3
"""Download images from a web page, with optional same-site crawling."""

from __future__ import annotations

import argparse
import mimetypes
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}


@dataclass(frozen=True)
class PageLink:
    url: str
    label: str = ""


@dataclass(frozen=True)
class FormPost:
    url: str
    data: tuple[tuple[str, str], ...]


class ImagePageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.image_urls: set[str] = set()
        self.linked_image_urls: set[str] = set()
        self.page_urls: set[PageLink] = set()
        self.continue_urls: set[str] = set()
        self.continue_posts: set[FormPost] = set()
        self._active_anchor_href: str | None = None
        self._active_anchor_words: list[str] = []
        self._active_form_action: str | None = None
        self._active_form_method = "get"
        self._active_form_fields: list[tuple[str, str]] = []
        self._active_form_words: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value for name, value in attrs if value is not None}
        label = " ".join(
            attr.get(key, "")
            for key in ("alt", "aria-label", "title", "value", "class", "id")
        )

        if tag in {"img", "source"}:
            for key in ("src", "data-src", "data-original", "data-lazy-src"):
                if key in attr:
                    add_image_url(self.image_urls, urljoin(self.base_url, attr[key]))

            for key in ("srcset", "data-srcset"):
                if key in attr:
                    for url in parse_srcset(attr[key], self.base_url):
                        add_image_url(self.image_urls, url)

        if tag == "form":
            action = clean_url(urljoin(self.base_url, attr.get("action", "")))
            if action.startswith(("http://", "https://")):
                self._active_form_action = action
                self._active_form_method = attr.get("method", "get").lower()
                self._active_form_fields = []
                self._active_form_words = [label]

        if self._active_form_action:
            self._active_form_words.append(label)
            if tag == "input" and "name" in attr:
                self._active_form_fields.append((attr["name"], attr.get("value", "")))

        for key in ("data-href", "data-url"):
            if key in attr:
                candidate = clean_url(urljoin(self.base_url, attr[key]))
                if candidate.startswith(("http://", "https://")) and is_continue_image_link(
                    candidate, label
                ):
                    self.continue_urls.add(candidate)

        if "onclick" in attr:
            for candidate in urls_from_text(attr["onclick"], self.base_url):
                if is_continue_image_link(candidate, label):
                    self.continue_urls.add(candidate)

        if tag == "a" and "href" in attr:
            href = clean_url(urljoin(self.base_url, attr["href"]))
            if href.startswith(("http://", "https://")):
                if looks_like_image_url(href):
                    add_image_url(self.linked_image_urls, href)
                else:
                    self.page_urls.add(PageLink(href, label.strip()))
                    self._active_anchor_href = href
                    self._active_anchor_words = [label]

    def handle_data(self, data: str) -> None:
        if self._active_anchor_href:
            self._active_anchor_words.append(data)
        if self._active_form_action:
            self._active_form_words.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._active_form_action:
            label = " ".join(self._active_form_words)
            if is_continue_image_link(self._active_form_action, label):
                if self._active_form_method == "post":
                    self.continue_posts.add(
                        FormPost(
                            self._active_form_action,
                            tuple(self._active_form_fields),
                        )
                    )
                else:
                    self.continue_urls.add(self._active_form_action)

            self._active_form_action = None
            self._active_form_method = "get"
            self._active_form_fields = []
            self._active_form_words = []

        if tag != "a" or not self._active_anchor_href:
            return

        label = " ".join(self._active_anchor_words)
        if is_continue_image_link(self._active_anchor_href, label):
            self.continue_urls.add(self._active_anchor_href)

        self._active_anchor_href = None
        self._active_anchor_words = []


def clean_url(url: str) -> str:
    return urldefrag(url.strip())[0]


def parse_srcset(srcset: str, base_url: str) -> set[str]:
    urls: set[str] = set()
    for candidate in srcset.split(","):
        url = candidate.strip().split(" ", 1)[0]
        if url:
            urls.add(clean_url(urljoin(base_url, url)))
    return urls


def urls_from_text(text: str, base_url: str) -> set[str]:
    urls: set[str] = set()
    for match in re.finditer(r"""(?P<quote>['"])(?P<url>https?://[^'"]+|/[^'"]+)(?P=quote)""", text):
        urls.add(clean_url(urljoin(base_url, match.group("url"))))
    return urls


def same_site(url: str, root_url: str) -> bool:
    url_parts = urlparse(url)
    root_parts = urlparse(root_url)
    return url_parts.scheme in {"http", "https"} and url_parts.netloc == root_parts.netloc


def looks_like_image_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in IMAGE_EXTENSIONS


def is_site_asset(url: str) -> bool:
    path = urlparse(url).path.lower()
    asset_names = (
        "favicon",
        "apple-touch-icon",
        "logo",
        "left-chevron",
        "right-chevron",
        "up-chevron",
    )
    return "/css/img/" in path or any(name in path for name in asset_names)


def is_navigation_link(url: str, start_url: str) -> bool:
    parsed = urlparse(url)
    start = urlparse(start_url)
    path = parsed.path.rstrip("/")
    nav_paths = ("", "/login.php", "/user/register")
    return parsed.netloc == start.netloc and (
        path in nav_paths or path.startswith("/page/")
    )


def link_priority(link: PageLink, start_url: str) -> tuple[int, str]:
    path = urlparse(link.url).path
    if re.search(r"/(i|image|photo|picture)/", path, re.IGNORECASE):
        return (0, link.url)
    if is_navigation_link(link.url, start_url):
        return (2, link.url)
    return (1, link.url)


def add_image_url(urls: set[str], url: str) -> None:
    clean = clean_url(url)
    if clean.startswith(("http://", "https://")) and not is_site_asset(clean):
        urls.add(clean)


def is_continue_image_link(url: str, label: str) -> bool:
    words = f"{label} {urlparse(url).path}".lower()
    image_words = ("image", "img", "photo", "picture", "original", "full")
    action_words = ("continue", "view", "open", "show", "download", "get", "proceed")
    if "continue" in words:
        return True
    return any(word in words for word in image_words) and any(word in words for word in action_words)


def fetch(url: str, timeout: int, user_agent: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        return response.read(), content_type


def fetch_text(url: str, timeout: int, user_agent: str) -> str:
    body, content_type = fetch(url, timeout, user_agent)
    encoding = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    if match:
        encoding = match.group(1)
    return body.decode(encoding, errors="replace")


def post_text(
    url: str,
    fields: Iterable[tuple[str, str]],
    timeout: int,
    user_agent: str,
) -> str:
    data = urlencode(list(fields)).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "User-Agent": user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        body = response.read()

    encoding = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    if match:
        encoding = match.group(1)
    return body.decode(encoding, errors="replace")


def extension_for(url: str, content_type: str) -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in IMAGE_EXTENSIONS:
        return path_ext

    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed

    return ".img"


def ordered_filename(index: int, total: int, url: str, content_type: str) -> str:
    width = max(2, len(str(total)))
    return f"{index:0{width}d}{extension_for(url, content_type)}"


def save_image(
    url: str,
    output_dir: Path,
    timeout: int,
    user_agent: str,
    index: int,
    total: int,
) -> Path | None:
    try:
        body, content_type = fetch(url, timeout, user_agent)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"skip: {url} ({exc})")
        return None

    if "image/" not in content_type.lower() and extension_for(url, content_type) == ".img":
        print(f"skip: {url} (not an image: {content_type or 'unknown type'})")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ordered_filename(index, total, url, content_type)
    path.write_bytes(body)
    print(f"saved: {path}")
    return path


def add_found_images(found_images: list[str], seen_images: set[str], urls: Iterable[str]) -> None:
    for url in urls:
        if url not in seen_images:
            seen_images.add(url)
            found_images.append(url)


def discover_images(
    start_url: str,
    *,
    crawl: bool,
    deep: bool,
    links_only: bool,
    verbose: bool,
    max_pages: int,
    delay: float,
    timeout: int,
    user_agent: str,
    should_cancel: Callable[[], bool] | None = None,
) -> list[str]:
    found_images: list[str] = []
    seen_images: set[str] = set()
    seen_pages: set[str] = set()
    pending: deque[tuple[str, int]] = deque([(clean_url(start_url), 0)])
    is_cancelled = should_cancel or (lambda: False)

    while pending and len(seen_pages) < max_pages and not is_cancelled():
        page_url, depth = pending.popleft()
        if page_url in seen_pages:
            continue
        if not deep and not same_site(page_url, start_url):
            continue

        seen_pages.add(page_url)
        print(f"scan: {page_url}")

        try:
            html = fetch_text(page_url, timeout, user_agent)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"skip page: {page_url} ({exc})")
            continue

        parser = ImagePageParser(page_url)
        parser.feed(html)
        posted_parsers: list[ImagePageParser] = []
        if deep:
            for post in sorted(parser.continue_posts, key=lambda item: item.url):
                if is_cancelled():
                    break
                try:
                    posted_html = post_text(
                        post.url,
                        post.data,
                        timeout,
                        user_agent,
                    )
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    print(f"skip form: {post.url} ({exc})")
                    continue

                posted_parser = ImagePageParser(post.url)
                posted_parser.feed(posted_html)
                posted_parsers.append(posted_parser)

        image_count = len(parser.image_urls) + len(parser.linked_image_urls)
        print(
            f"found: {image_count} image link(s), "
            f"{len(parser.page_urls)} page link(s), "
            f"{len(parser.continue_urls)} continue link(s), "
            f"{len(parser.continue_posts)} continue form(s)"
        )
        if verbose:
            for link in sorted(parser.page_urls, key=lambda item: item.url)[:20]:
                label = re.sub(r"\s+", " ", link.label).strip()
                print(f"  page link: {link.url} [{label}]")
            for url in sorted(parser.continue_urls):
                print(f"  continue link: {url}")
            for post in sorted(parser.continue_posts, key=lambda item: item.url):
                field_names = ", ".join(name for name, _ in post.data)
                print(f"  continue form: {post.url} [{field_names}]")
        if not (links_only and depth == 0):
            add_found_images(found_images, seen_images, parser.image_urls)
        add_found_images(found_images, seen_images, parser.linked_image_urls)
        for posted_parser in posted_parsers:
            add_found_images(found_images, seen_images, posted_parser.image_urls)
            add_found_images(found_images, seen_images, posted_parser.linked_image_urls)
        if deep:
            add_found_images(found_images, seen_images, parser.continue_urls)
            for url in sorted(parser.continue_urls):
                if not looks_like_image_url(url) and url not in seen_pages:
                    pending.append((url, depth + 1))

        if crawl:
            for link in sorted(parser.page_urls, key=lambda item: item.url):
                if link.url not in seen_pages and same_site(link.url, start_url):
                    pending.append((link.url, depth + 1))
        elif deep and depth == 0:
            links = sorted(
                parser.page_urls,
                key=lambda item: link_priority(item, start_url),
            )
            if links_only:
                links = [
                    link for link in links if not is_navigation_link(link.url, start_url)
                ]
            for link in links:
                if link.url not in seen_pages:
                    pending.append((link.url, depth + 1))

        if delay:
            time.sleep(delay)

    return found_images


def download_images(
    urls: Iterable[str],
    output_dir: Path,
    timeout: int,
    user_agent: str,
    delay: float,
) -> int:
    count = 0
    url_list = list(urls)
    total = len(url_list)
    for url in url_list:
        if save_image(url, output_dir, timeout, user_agent, count + 1, total):
            count += 1
        if delay:
            time.sleep(delay)
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download images from a web page. Use --crawl to scan internal pages too."
    )
    parser.add_argument("url", help="Page URL to scan, for example https://example.com")
    parser.add_argument(
        "-o",
        "--output",
        default="downloaded_images",
        help="Directory for downloaded images (default: downloaded_images)",
    )
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="Follow links on the same domain and download images from those pages too",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Open links from the starting page and follow continue/view image links",
    )
    parser.add_argument(
        "--links-only",
        action="store_true",
        help="With --deep, skip images on the starting page and only use its links",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print parsed links while scanning pages",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=25,
        help="Maximum pages to scan when using --crawl or --deep (default: 25)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay between requests in seconds (default: 0.2)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Request timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header to send with requests",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start_url = args.url
    if not start_url.startswith(("http://", "https://")):
        start_url = f"https://{start_url}"

    if args.max_pages < 1:
        print("--max-pages must be at least 1", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("--delay must be 0 or greater", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be greater than 0", file=sys.stderr)
        return 2
    if args.links_only and not args.deep:
        print("--links-only must be used with --deep", file=sys.stderr)
        return 2

    images = discover_images(
        start_url,
        crawl=args.crawl,
        deep=args.deep,
        links_only=args.links_only,
        verbose=args.verbose,
        max_pages=args.max_pages,
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
    )

    if not images:
        print("No images found.")
        return 0

    saved = download_images(
        images,
        Path(args.output),
        timeout=args.timeout,
        user_agent=args.user_agent,
        delay=args.delay,
    )
    print(f"Done. Saved {saved} of {len(images)} discovered image(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
