#!/usr/bin/env python3
"""Download images from a web page, with optional same-site crawling."""

from __future__ import annotations

import argparse
import hashlib
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
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
FORBIDDEN_IMAGE_SKIP = "forbidden"

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

NON_IMAGE_EXTENSIONS = {
    ".css",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".pdf",
    ".php",
    ".txt",
    ".xml",
}


class UserFacingError(Exception):
    """An expected scan problem safe to show in the UI."""


class PageAccessError(UserFacingError):
    """The starting page could not be fetched."""


def browser_headers(
    user_agent: str,
    *,
    accept: str,
    referer: str | None = None,
) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
    }
    if referer:
        headers["Referer"] = referer
    return headers


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
        self.main_image_urls: set[str] = set()
        self.reload_url: str | None = None
        self.linked_image_urls: set[str] = set()
        self.page_urls: set[PageLink] = set()
        self.page_url_order: dict[str, int] = {}
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
            if tag == "img" and attr.get("id") == "img" and attr.get("src"):
                add_image_url(self.main_image_urls, urljoin(self.base_url, attr["src"]))
            if tag == "img" and attr.get("id") == "mainPhoto":
                for key in ("data-original", "data-src", "src"):
                    if attr.get(key):
                        candidate = urljoin(self.base_url, attr[key])
                        if is_imagefap_original(candidate):
                            add_image_url(self.main_image_urls, candidate)
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
            if attr.get("id") == "loadfail":
                match = re.search(r"\bnl\(['\"]([a-zA-Z0-9_-]+)['\"]\)", attr["onclick"])
                if match:
                    parsed = urlparse(self.base_url)
                    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "nl"]
                    self.reload_url = parsed._replace(query=urlencode(query + [("nl", match.group(1))])).geturl()
            for candidate in urls_from_text(attr["onclick"], self.base_url):
                if is_continue_image_link(candidate, label):
                    self.continue_urls.add(candidate)

        if tag == "a" and "href" in attr:
            photo = re.fullmatch(r"/photo/(\d+)(?:/.*)?", urlparse(self.base_url).path)
            if photo and attr.get("imageid") == photo.group(1):
                original = urljoin(self.base_url, attr.get("original") or attr["href"])
                if is_imagefap_original(original):
                    add_image_url(self.main_image_urls, original)
            href = clean_url(urljoin(self.base_url, attr["href"]))
            if href.startswith(("http://", "https://")):
                if looks_like_image_url(href):
                    add_image_url(self.linked_image_urls, href)
                else:
                    self.page_urls.add(PageLink(href, label.strip()))
                    self.page_url_order.setdefault(href, len(self.page_url_order))
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


def looks_like_non_image_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in NON_IMAGE_EXTENSIONS


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


def is_gallery_page(url: str, start_url: str) -> bool:
    """Match the same gallery with only its numeric pagination query changed."""
    if not same_site(url, start_url):
        return False
    candidate = urlparse(clean_url(url))
    start = urlparse(clean_url(start_url))
    if candidate.path.rstrip("/") != start.path.rstrip("/"):
        return False
    page_keys = {"p", "page", "pg", "offset", "start"}

    def split_query(query: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        pages = []
        other = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key.lower() in page_keys and value.isascii() and value.isdigit():
                pages.append((key, value))
            else:
                other.append((key, value))
        return sorted(pages), sorted(other)

    candidate_pages, candidate_other = split_query(candidate.query)
    start_pages, start_other = split_query(start.query)
    return candidate_other == start_other and (
        candidate_pages != start_pages or clean_url(url) == clean_url(start_url)
    )


def eh_gallery_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"e-hentai.org", "exhentai.org"}:
        return None
    match = re.fullmatch(r"/g/(\d+)/[a-fA-F0-9]+/?", parsed.path)
    return match.group(1) if match else None


def is_imagefap_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"imagefap.com", "www.imagefap.com"}


def is_imagefap_original(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme in {"http", "https"} and (
        host == "imagefap.com" or host.endswith(".imagefap.com")
    ) and parsed.path.startswith("/images/full/")


def imagefap_gallery_id(url: str) -> str | None:
    if not is_imagefap_url(url):
        return None
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    match = re.fullmatch(r"/(?:pictures|gallery)/(\d+)(?:/.*)?", parsed.path)
    if match:
        gallery_id = match.group(1)
        return gallery_id if query.get("gid", gallery_id) == gallery_id else None
    if parsed.path == "/gallery.php" and re.fullmatch(r"[0-9]+", query.get("gid", "")):
        return query["gid"]
    return None


def imagefap_listing_page(url: str, gallery_id: str) -> int | None:
    if imagefap_gallery_id(url) != gallery_id:
        return None
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(pairs)
    if len(pairs) != len(query) or set(query) - {"gid", "page", "view"}:
        return None
    # view=2 is an alternate all-images view, not another numbered page.
    if query.get("view", "0") != "0":
        return None
    page = query.get("page", "0")
    return int(page) if re.fullmatch(r"[0-9]+", page) else None


def imagefap_photo_id(url: str, gallery_id: str) -> str | None:
    if not is_imagefap_url(url):
        return None
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(pairs)
    if len(pairs) != len(query) or set(query) - {"gid", "page", "pgid"}:
        return None
    if not re.fullmatch(r"[0-9]+", query.get("page", "0")):
        return None
    match = re.fullmatch(r"/photo/(\d+)(?:/.*)?", parsed.path)
    return match.group(1) if match and query.get("gid") == gallery_id else None


def eh_image_number(url: str, start_url: str, gallery_id: str) -> int | None:
    if not same_site(url, start_url):
        return None
    parsed = urlparse(url)
    match = re.fullmatch(r"/s/[a-fA-F0-9]+/" + re.escape(gallery_id) + r"-([1-9]\d*)", parsed.path)
    # Ignore viewer controls that reload the same image with different queries.
    return int(match.group(1)) if match and not parsed.query else None


def add_image_url(urls: set[str], url: str) -> None:
    clean = clean_url(url)
    if (
        clean.startswith(("http://", "https://"))
        and not looks_like_non_image_url(clean)
        and not is_site_asset(clean)
    ):
        urls.add(clean)


def is_continue_image_link(url: str, label: str) -> bool:
    words = f"{label} {urlparse(url).path}".lower()
    image_words = ("image", "img", "photo", "picture", "original", "full")
    action_words = ("continue", "view", "open", "show", "download", "get", "proceed")
    if "continue" in words:
        return True
    return any(word in words for word in image_words) and any(word in words for word in action_words)


def fetch(
    url: str,
    timeout: int,
    user_agent: str,
    *,
    accept: str = HTML_ACCEPT,
    referer: str | None = None,
) -> tuple[bytes, str]:
    request = Request(
        url,
        headers=browser_headers(user_agent, accept=accept, referer=referer),
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        return response.read(), content_type


def fetch_text(
    url: str,
    timeout: int,
    user_agent: str,
    *,
    referer: str | None = None,
) -> str:
    body, content_type = fetch(
        url,
        timeout,
        user_agent,
        accept=HTML_ACCEPT,
        referer=referer,
    )
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
    *,
    referer: str | None = None,
) -> str:
    data = urlencode(list(fields)).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            **browser_headers(user_agent, accept=HTML_ACCEPT, referer=referer),
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
    seen_hashes: set[str] | None = None,
    referer: str | None = None,
    skip_reasons: list[str] | None = None,
    refresh_url: Callable[[], tuple[str, str] | None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    max_attempts: int = 3,
) -> Path | None:
    is_cancelled = should_cancel or (lambda: False)
    report = on_progress or (lambda _message: None)
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        if is_cancelled():
            return None
        forbidden = False
        retryable = True
        try:
            body, content_type = fetch(
                url, timeout, user_agent, accept=IMAGE_ACCEPT, referer=referer,
            )
            content_type = (content_type or "").lower().strip()
            if content_type.startswith("image/"):
                break
            reason = "the host returned a page instead of an image"
            print(f"image attempt failed: {url} (not an image: {content_type or 'unknown'})")
        except HTTPError as exc:
            forbidden = exc.code == 403
            retryable = exc.code in {408, 429, 500, 502, 503, 504} or bool(refresh_url)
            reason = "the image host refused the request"
            print(f"image attempt failed: {url} ({exc})")
        except (URLError, TimeoutError, OSError) as exc:
            reason = "the image host could not be reached"
            print(f"image attempt failed: {url} ({exc})")

        if attempt + 1 == attempts or not retryable:
            if forbidden and skip_reasons is not None:
                skip_reasons.append(FORBIDDEN_IMAGE_SKIP)
            report(f"Image download failed: {reason}.")
            return None
        report(f"Retrying image download ({attempt + 2}/{attempts}): {reason}.")
        # Short waits allow Stop to interrupt retries promptly.
        for _ in range(10 * (attempt + 1)):
            if is_cancelled():
                return None
            time.sleep(0.1)
        if refresh_url and not is_cancelled():
            try:
                refreshed = refresh_url()
                if refreshed:
                    url, referer = refreshed
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                print(f"image link refresh failed: {exc}")

    digest = hashlib.sha256(body).hexdigest()
    if seen_hashes is not None:
        if digest in seen_hashes:
            print(f"skip: {url} (duplicate image content)")
            return None
        seen_hashes.add(digest)

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ordered_filename(index, total, url, content_type)
    path.write_bytes(body)
    print(f"saved: {path}")
    return path


def refresh_gallery_image(viewer_url: str, timeout: int, user_agent: str,
                          should_cancel: Callable[[], bool] | None = None) -> tuple[str, str] | None:
    """Refresh a supported gallery viewer and its load-failure action if available."""
    is_cancelled = should_cancel or (lambda: False)
    if is_cancelled():
        return None
    parser = ImagePageParser(viewer_url)
    parser.feed(fetch_text(viewer_url, timeout, user_agent))
    if parser.reload_url and not is_cancelled():
        reloaded = ImagePageParser(parser.reload_url)
        reloaded.feed(fetch_text(parser.reload_url, timeout, user_agent))
        if reloaded.main_image_urls:
            return sorted(reloaded.main_image_urls)[0], parser.reload_url
    if parser.main_image_urls:
        return sorted(parser.main_image_urls)[0], viewer_url
    return None


def add_found_images(found_images: list[str], seen_images: set[str], urls: Iterable[str]) -> None:
    for url in urls:
        if not looks_like_non_image_url(url) and url not in seen_images:
            seen_images.add(url)
            found_images.append(url)


def discover_imagefap_images(
    start_url: str, gallery_id: str, *, deep: bool, follow_gallery_pages: bool,
    max_pages: int, delay: float, timeout: int, user_agent: str,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[str], None] | None,
    image_sources: dict[str, str] | None,
) -> list[str]:
    if not deep:
        raise UserFacingError("Enable Deep to download full-size images from this gallery.")
    is_cancelled = should_cancel or (lambda: False)
    report = on_progress or (lambda _message: None)
    pending = deque([start_url])
    queued = {imagefap_listing_page(start_url, gallery_id)}
    viewers: dict[str, str] = {}
    scanned = 0
    missing = 0
    limit = max(1, max_pages) if follow_gallery_pages else 1
    while pending and scanned < limit and not is_cancelled():
        url = pending.popleft()
        scanned += 1
        report(f"Scanning gallery page {scanned}.")
        try:
            html = fetch_text(url, timeout, user_agent)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"skip gallery page: {url} ({exc})")
            if scanned == 1:
                raise PageAccessError("The page could not be accessed. The site may be blocking automated requests or requiring a browser session.") from exc
            missing += 1
            continue
        parser = ImagePageParser(url)
        parser.feed(html)
        # HTML order preserves the gallery's order; numeric photo IDs do not.
        for link in sorted(parser.page_urls, key=lambda item: parser.page_url_order[item.url]):
            photo_id = imagefap_photo_id(link.url, gallery_id)
            if photo_id:
                viewers.setdefault(photo_id, link.url)
            if follow_gallery_pages:
                page = imagefap_listing_page(link.url, gallery_id)
                if page is not None and page not in queued:
                    queued.add(page)
                    pending.append(link.url)
        if delay and not is_cancelled():
            time.sleep(delay)
    images: list[str] = []
    seen: set[str] = set()
    for index, viewer in enumerate(viewers.values(), 1):
        if is_cancelled():
            return images
        try:
            parser = ImagePageParser(viewer)
            parser.feed(fetch_text(viewer, timeout, user_agent))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"skip image page: {viewer} ({exc})")
            missing += 1
            continue
        if parser.main_image_urls:
            image = sorted(parser.main_image_urls)[0]
            add_found_images(images, seen, (image,))
            if image_sources is not None:
                image_sources[image] = viewer
        else:
            missing += 1
        report(f"Checked {index}/{len(viewers)} gallery image pages; found {len(images)} images.")
        if delay and not is_cancelled():
            time.sleep(delay)
    if not is_cancelled():
        if pending:
            report("Scan incomplete: Max pages was reached. Increase Max pages and try again.")
        if missing:
            report(f"Scan incomplete: {missing} gallery pages could not be read or had no main image.")
        report(f"Found {len(images)} gallery images.")
    return images


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
    follow_gallery_pages: bool = False,
    on_progress: Callable[[str], None] | None = None,
    image_sources: dict[str, str] | None = None,
) -> list[str]:
    imagefap_id = imagefap_gallery_id(start_url)
    if imagefap_id and max_pages != 0:
        return discover_imagefap_images(
            start_url, imagefap_id, deep=deep, follow_gallery_pages=follow_gallery_pages,
            max_pages=max_pages, delay=delay, timeout=timeout, user_agent=user_agent,
            should_cancel=should_cancel, on_progress=on_progress, image_sources=image_sources,
        )
    found_images: list[str] = []
    seen_images: set[str] = set()
    seen_pages: set[str] = set()
    pending: deque[tuple[str, int]] = deque([(clean_url(start_url), 0)])
    is_cancelled = should_cancel or (lambda: False)
    start_page_only = max_pages == 0
    page_limit = max(1, max_pages)
    gallery_id = eh_gallery_id(start_url)
    gallery_limit = bool(gallery_id and follow_gallery_pages and not start_page_only)
    listing_count = 0
    missing_images = 0
    limit_reached = False
    image_pages: set[int] = set()
    gallery_images: dict[int, str] = {}
    report = on_progress or (lambda _message: None)

    while pending and (gallery_limit or len(seen_pages) < page_limit) and not is_cancelled():
        page_url, depth = pending.popleft()
        if gallery_id and is_gallery_page(page_url, start_url):
            parsed_page = urlparse(page_url)
            query = [(key, value) for key, value in parse_qsl(parsed_page.query)
                     if not (key == "p" and value.isdigit() and int(value) == 0)]
            page_url = parsed_page._replace(
                path=parsed_page.path.rstrip("/") + "/", query=urlencode(sorted(query))
            ).geturl()
        if page_url in seen_pages:
            continue
        if not deep and not same_site(page_url, start_url):
            continue

        listing_page = depth == 0 or is_gallery_page(page_url, start_url)
        if gallery_limit and listing_page:
            if listing_count >= page_limit:
                limit_reached = True
                continue
            listing_count += 1
            report(f"Scanning gallery page {listing_count}.")

        seen_pages.add(page_url)
        print(f"scan: {page_url}")

        try:
            html = fetch_text(page_url, timeout, user_agent)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"skip page: {page_url} ({exc})")
            if depth == 0:
                raise PageAccessError(
                    "The page could not be accessed. The site may be blocking automated requests or requiring a browser session."
                ) from exc
            if gallery_id:
                missing_images += 1
            continue

        parser = ImagePageParser(page_url)
        parser.feed(html)
        gallery_page = depth == 0 or (
            follow_gallery_pages and is_gallery_page(page_url, start_url)
        )
        if gallery_id and not start_page_only:
            if listing_page:
                # Only this gallery's viewers and pagination belong in the scan.
                parser.page_urls = {
                    link for link in parser.page_urls
                    if (follow_gallery_pages and is_gallery_page(link.url, start_url))
                    or eh_image_number(link.url, start_url, gallery_id) is not None
                }
                parser.image_urls.clear()
            else:
                number = eh_image_number(page_url, start_url, gallery_id)
                if number in image_pages:
                    continue
                if number is not None:
                    image_pages.add(number)
                parser.page_urls.clear()
                parser.image_urls = parser.main_image_urls
                if number is not None and parser.main_image_urls:
                    gallery_images[number] = sorted(parser.main_image_urls)[0]
                    if image_sources is not None:
                        image_sources[gallery_images[number]] = page_url
                if not parser.image_urls:
                    missing_images += 1
                if len(image_pages) % 20 == 0:
                    report(f"Checked {len(image_pages)} gallery image pages; found {len(found_images) + len(parser.image_urls)} images.")
            # fullimg links can return HTML, and viewer navigation has UI assets.
            parser.linked_image_urls.clear()
            parser.continue_urls.clear()
            parser.continue_posts.clear()
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
        if start_page_only or not (links_only and gallery_page):
            add_found_images(found_images, seen_images, parser.image_urls)
        add_found_images(found_images, seen_images, parser.linked_image_urls)
        for posted_parser in posted_parsers:
            add_found_images(found_images, seen_images, posted_parser.image_urls)
            add_found_images(found_images, seen_images, posted_parser.linked_image_urls)
        if deep:
            for url in sorted(parser.continue_urls):
                if looks_like_image_url(url):
                    add_found_images(found_images, seen_images, (url,))
                elif url not in seen_pages:
                    pending.append((url, depth + 1))

        if follow_gallery_pages and gallery_page:
            for link in sorted(parser.page_urls, key=lambda item: item.url):
                if link.url not in seen_pages and is_gallery_page(link.url, start_url):
                    pending.append((link.url, depth + 1))

        if crawl:
            for link in sorted(parser.page_urls, key=lambda item: item.url):
                if link.url not in seen_pages and same_site(link.url, start_url):
                    pending.append((link.url, depth + 1))
        elif deep and gallery_page:
            links = sorted(
                parser.page_urls,
                key=lambda item: (
                    (0, eh_image_number(item.url, start_url, gallery_id) or 0, item.url)
                    if gallery_id else link_priority(item, start_url)
                ),
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

    if gallery_id and not start_page_only and not is_cancelled():
        if limit_reached or (pending and not gallery_limit):
            report("Scan incomplete: Max pages was reached. Increase Max pages and try again.")
        if missing_images:
            report(f"Scan incomplete: {missing_images} gallery pages could not be read or had no main image.")
        report(f"Found {len(found_images)} gallery images.")
        # Pagination links can be listed out of order; keep the archive in page order.
        if gallery_images:
            return list(dict.fromkeys(gallery_images[number] for number in sorted(gallery_images)))
    return found_images


def download_images(
    urls: Iterable[str],
    output_dir: Path,
    timeout: int,
    user_agent: str,
    delay: float,
    referer: str | None = None,
) -> int:
    count = 0
    url_list = list(urls)
    total = len(url_list)
    seen_hashes: set[str] = set()
    for url in url_list:
        if save_image(
            url,
            output_dir,
            timeout,
            user_agent,
            count + 1,
            total,
            seen_hashes,
            referer=referer,
        ):
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
        help="Maximum pages to scan when using --crawl or --deep; use 0 for the starting page only (default: 25)",
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

    if args.max_pages < 0:
        print("--max-pages must be at least 0", file=sys.stderr)
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
        referer=start_url,
    )
    print(f"Done. Saved {saved} of {len(images)} discovered image(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
