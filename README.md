# SnatchIMG

SnatchIMG is a small Python web app and CLI for finding and downloading images
from online gallery pages. It can scan a starting page, optionally follow
same-site links, skip duplicate image content, and package saved images as a ZIP
from the web UI.

## Features

- Local web UI for scanning galleries and downloading a ZIP.
- Standalone CLI for direct image downloads.
- Same-site crawling and deep gallery/image-page discovery.
- Browser-like request headers and referer support for image downloads.
- Duplicate image-content detection using hashes.
- Clear saved/skipped counters and progress logs.
- Friendly public errors for common blocked-image cases.
- Docker development workflow with polling-based hot reload.

## Requirements

- Python 3.12 or newer recommended.
- Node.js for the JavaScript syntax check.
- Docker Desktop, optional, for the containerized development workflow.

The app uses only the Python standard library at runtime unless you use Docker
hot reload, which installs `watchdog` inside the container.

## Run The Web UI

Start the local server:

```powershell
python web_app.py
```

Open:

```text
http://127.0.0.1:8080
```

Optional host and port:

```powershell
python web_app.py --host 127.0.0.1 --port 8080
```

You can also set:

```powershell
$env:SNATCHIMG_HOST="127.0.0.1"
$env:SNATCHIMG_PORT="8080"
python web_app.py
```

Generated run files and ZIPs are stored under `.snatchimg_runs`.

## Web UI Options

- `Max pages`: maximum same-site pages to scan. `0` means starting page only.
- `Deep`: follows image-detail links and continue/open-image actions to find
  full-size images.
- `Links only`: with deep scans, starts from linked gallery/image pages instead
  of direct images on the starting page. When `Max pages` is `0`, direct images
  on the starting page still count.

## Run With Docker

Build and run the development container:

```powershell
docker compose up --build
```

Then open:

```text
http://127.0.0.1:8080
```

The compose setup:

- binds the app to `0.0.0.0:8080` inside the container,
- mounts the source tree into `/app`,
- persists `.snatchimg_runs` on the host,
- reloads when `.py`, `.html`, `.js`, or `.css` files change.

## CLI Usage

Download images from a page:

```powershell
python snatchimg.py https://example.com/gallery
```

Write files to a specific directory:

```powershell
python snatchimg.py https://example.com/gallery --output downloaded_images
```

Deep scan linked gallery/image pages:

```powershell
python snatchimg.py https://example.com/gallery --deep --links-only --max-pages 25
```

Same-site crawl:

```powershell
python snatchimg.py https://example.com/gallery --crawl --max-pages 50
```

Useful CLI options:

- `--max-pages 0`: scan only the starting page.
- `--delay 0.2`: wait between requests.
- `--timeout 20`: request timeout in seconds.
- `--user-agent "..."`: override the default browser-like user agent.
- `--verbose`: print parsed links while scanning.

## Testing

Run the lightweight validation checks:

```powershell
python -m py_compile web_app.py snatchimg.py
node --check static\app.js
python -m unittest discover -s tests -v
```

In VS Code, use the included test task:

```text
Ctrl+Shift+P -> Tasks: Run Test Task -> Run SnatchIMG Tests
```

The test suite is offline-friendly. It uses fakes and monkeypatching instead of
live network calls.

## Behavior Notes

- A run with zero discovered images is treated as an error.
- A run where every discovered image is skipped does not create a ZIP.
- Byte-identical image downloads are skipped as duplicate content.
- `saved` counts files written to disk.
- `skipped` counts discovered images that were not saved.
- `total` remains the number of discovered images.
- Static files are served only from `static`.
- Download files are served only from `.snatchimg_runs`.

## Project Layout

```text
web_app.py          Local HTTP server, job lifecycle, API routes, static files, ZIP creation
snatchimg.py        Scraper/parser/downloader CLI and shared discovery logic
dev_reload.py       Docker development hot-reload helper
static/index.html   Web UI markup
static/app.js       Client state, polling, progress, job controls
static/styles.css   Visual design and light/dark theme styles
tests/              Unit and lightweight integration tests
```
