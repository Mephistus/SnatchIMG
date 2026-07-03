#!/usr/bin/env python3
"""Small web UI for SnatchIMG."""

from __future__ import annotations

import json
import mimetypes
import shutil
import threading
import time
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


def run_job(job_id: str, options: dict[str, Any]) -> None:
    def is_cancelled() -> bool:
        with jobs_lock:
            return jobs[job_id].cancel_requested

    def finish_cancelled() -> None:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "cancelled"
            job.phase = "Stopped"
            job.add_log("Download stopped by user.")

    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.phase = "Scanning gallery"
        job.progress = 3
        job.add_log("Started scan.")

    output_dir = RUNS_DIR / job_id / "images"
    zip_base = RUNS_DIR / job_id / "snatchimg_images"

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

        if is_cancelled():
            finish_cancelled()
            return

        with jobs_lock:
            job.total = len(images)
            job.phase = "Downloading images"
            job.progress = 10 if images else 100
            job.add_log(f"Found {len(images)} image(s).")

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

        if is_cancelled():
            finish_cancelled()
            return

        with jobs_lock:
            job.phase = "Creating ZIP"
            job.progress = 95
            job.add_log("Creating ZIP archive.")

        if output_dir.exists():
            zip_path = Path(shutil.make_archive(str(zip_base), "zip", output_dir))
        else:
            zip_path = Path(shutil.make_archive(str(zip_base), "zip", RUNS_DIR / job_id))

        with jobs_lock:
            job.status = "complete"
            job.phase = "Ready"
            job.progress = 100
            job.zip_path = zip_path
            job.add_log(f"ZIP ready with {job.saved} saved image(s).")

    except Exception as exc:  # Keep the server alive and show the error in the UI.
        with jobs_lock:
            job.status = "error"
            job.phase = "Failed"
            job.error = str(exc)
            job.add_log(f"Error: {exc}")


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
            if max_pages < 1:
                raise ValueError("Max pages must be at least 1.")
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

        if not zip_path or not zip_path.exists():
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
        if not path.exists() or not path.is_file():
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


def main() -> int:
    RUNS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8080), SnatchHandler)
    print("SnatchIMG web app running at http://127.0.0.1:8080")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
