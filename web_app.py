#!/usr/bin/env python3
"""Small web UI for SnatchIMG."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import snatchimg


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
RUNS_DIR = ROOT / ".snatchimg_runs"
PUBLIC_SCAN_ERROR = "The scan failed. Please check the URL/options and try again."
PUBLIC_NO_IMAGES_ERROR = "No images were found on that page. Try changing the options or using a direct gallery page."


@dataclass
class Job:
    id: str
    url: str
    status: str = "queued"
    phase: str = "Queued"
    total: int = 0
    saved: int = 0
    progress: int = 0
    logs: list[str] = field(default_factory=list)
    zip_path: Path | None = None
    error: str | None = None
    cancel_requested: bool = False

    def add_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.logs.append(f"{timestamp}  {message}")
        self.logs = self.logs[-300:]


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def cleanup_job_files(job_id: str) -> None:
    run_dir = RUNS_DIR / job_id
    if is_relative_to(run_dir, RUNS_DIR) and run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)


def run_job(job_id: str, options: dict[str, Any]) -> None:
    def is_cancelled() -> bool:
        with jobs_lock:
            return jobs[job_id].cancel_requested

    def finish_cancelled() -> None:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "cancelled"
            job.phase = "Stopped"
            job.add_log("Stopped by user.")

    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.phase = "Scanning gallery (Started scan - This could take a few minutes.)"
        job.progress = 3
        job.add_log("Started scan - This could take a few minutes.")

    output_dir = RUNS_DIR / job_id / "images"
    zip_base = RUNS_DIR / job_id / "snatchimg_images"
    seen_image_hashes: set[str] = set()

    try:
        images = snatchimg.discover_images(
            job.url,
            crawl=bool(options.get("crawl", False)),
            deep=bool(options.get("deep", True)),
            links_only=bool(options.get("linksOnly", True)),
            verbose=False,
            max_pages=int(options.get("maxPages", 200)),
            delay=float(options.get("delay", 0.2)),
            timeout=int(options.get("timeout", 20)),
            user_agent=snatchimg.DEFAULT_USER_AGENT,
            should_cancel=is_cancelled,
        )
        images = list(dict.fromkeys(images))

        if is_cancelled():
            finish_cancelled()
            return

        with jobs_lock:
            job.total = len(images)
            job.phase = "Downloading images"
            job.progress = 10 if images else 100
            job.add_log(f"Found {len(images)} image(s).")

        if not images:
            cleanup_job_files(job_id)
            with jobs_lock:
                job.status = "error"
                job.phase = "No images found"
                job.error = PUBLIC_NO_IMAGES_ERROR
                job.zip_path = None
                job.add_log(PUBLIC_NO_IMAGES_ERROR)
            return

        for url in images:
            if is_cancelled():
                finish_cancelled()
                return

            with jobs_lock:
                next_index = job.saved + 1
                job.add_log(f"Downloading image {next_index}/{job.total}.")

            saved_path = snatchimg.save_image(
                url,
                output_dir,
                timeout=int(options.get("timeout", 20)),
                user_agent=snatchimg.DEFAULT_USER_AGENT,
                index=next_index,
                total=job.total,
                seen_hashes=seen_image_hashes,
            )

            with jobs_lock:
                if saved_path:
                    job.saved += 1
                    job.add_log(f"Saved {saved_path.name}.")
                else:
                    job.add_log(f"Skipped image {next_index}/{job.total}.")
                if job.total:
                    job.progress = 10 + int((job.saved / job.total) * 80)

            if options.get("delay", 0.2):
                time.sleep(float(options.get("delay", 0.2)))

        if job.saved == 0:
            cleanup_job_files(job_id)
            with jobs_lock:
                job.status = "error"
                job.phase = "No images saved"
                job.progress = 100
                job.error = PUBLIC_NO_IMAGES_ERROR
                job.zip_path = None
                job.add_log(PUBLIC_NO_IMAGES_ERROR)
            return

        if is_cancelled():
            finish_cancelled()
            return

        with jobs_lock:
            job.phase = "Creating ZIP"
            job.progress = 95
            job.add_log("Creating ZIP archive.")

        zip_path = Path(shutil.make_archive(str(zip_base), "zip", output_dir))

        with jobs_lock:
            job.status = "complete"
            job.phase = "Ready"
            job.progress = 100
            job.zip_path = zip_path
            job.add_log(f"ZIP ready with {job.saved} saved image(s).")

    except snatchimg.UserFacingError as exc:
        print(f"Job {job_id} failed:")
        print(traceback.format_exc())
        with jobs_lock:
            job.status = "error"
            job.phase = "Failed"
            job.progress = 100
            job.error = str(exc)
            job.add_log(str(exc))

    except Exception:  # Keep the server alive and show a safe error in the UI.
        print(f"Job {job_id} failed:")
        print(traceback.format_exc())
        with jobs_lock:
            job.status = "error"
            job.phase = "Failed"
            job.progress = 100
            job.error = PUBLIC_SCAN_ERROR
            job.add_log(PUBLIC_SCAN_ERROR)


class SnatchHandler(BaseHTTPRequestHandler):
    server_version = "SnatchIMG/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/":
            self.send_static(STATIC_DIR / "index.html")
            return

        if path.startswith("/static/"):
            self.send_static(STATIC_DIR / path.removeprefix("/static/"))
            return

        if path.startswith("/api/jobs/") and path.endswith("/download"):
            self.send_job_download(path)
            return

        if path.startswith("/api/jobs/"):
            self.send_job_status(path)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
            self.cancel_job(parsed.path)
            return

        if parsed.path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            body = self.read_json()
            url = normalize_url(str(body.get("url", "")))
            if url in {"http://", "https://"}:
                raise ValueError("A website URL is required.")
            max_pages = int(body.get("maxPages", 200))
            if max_pages < 0:
                raise ValueError("Max pages must be at least 0.")
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        job_id = uuid.uuid4().hex
        job = Job(job_id, url)
        with jobs_lock:
            jobs[job_id] = job

        thread = threading.Thread(target=run_job, args=(job_id, body), daemon=True)
        thread.start()
        self.send_json({"id": job_id}, HTTPStatus.CREATED)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_job_status(self, path: str) -> None:
        job_id = path.removeprefix("/api/jobs/").strip("/")
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = {
                "id": job.id,
                "url": job.url,
                "status": job.status,
                "phase": job.phase,
                "total": job.total,
                "saved": job.saved,
                "progress": job.progress,
                "logs": job.logs,
                "error": job.error,
                "cancelRequested": job.cancel_requested,
                "downloadUrl": f"/api/jobs/{job.id}/download"
                if job.status == "complete" and job.zip_path
                else None,
            }
        self.send_json(data)

    def cancel_job(self, path: str) -> None:
        job_id = path.removeprefix("/api/jobs/").removesuffix("/cancel").strip("/")
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if job.status in {"complete", "cancelled", "error"}:
                self.send_json({"status": job.status})
                return
            job.cancel_requested = True
            job.status = "stopping"
            job.phase = "Stopping"
            job.add_log("Stop requested.")
        self.send_json({"status": "stopping"})

    def send_job_download(self, path: str) -> None:
        job_id = path.removeprefix("/api/jobs/").removesuffix("/download").strip("/")
        with jobs_lock:
            job = jobs.get(job_id)
            zip_path = job.zip_path if job else None

        if not zip_path or not is_relative_to(zip_path, RUNS_DIR) or not zip_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        data = zip_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            'attachment; filename="snatchimg_images.zip"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, path: Path) -> None:
        if not is_relative_to(path, STATIC_DIR) or not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        return


class SnatchServer(ThreadingHTTPServer):
    allow_reuse_address = os.environ.get("SNATCHIMG_REUSE_ADDRESS") == "1"

    def server_bind(self) -> None:
        if not self.allow_reuse_address and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SnatchIMG web UI.")
    parser.add_argument(
        "--host",
        default=os.environ.get("SNATCHIMG_HOST", "127.0.0.1"),
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        default=int(os.environ.get("SNATCHIMG_PORT", "8080")),
        type=int,
        help="Port to bind (default: 8080)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    RUNS_DIR.mkdir(exist_ok=True)
    server = SnatchServer((args.host, args.port), SnatchHandler)
    print(f"SnatchIMG web app running at http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
