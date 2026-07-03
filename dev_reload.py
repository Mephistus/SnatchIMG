#!/usr/bin/env python3
"""Run SnatchIMG with polling-based reloads for Docker Desktop bind mounts."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver


ROOT = Path(__file__).resolve().parent
WATCH_EXTENSIONS = {".py", ".html", ".js", ".css"}
IGNORED_DIRS = {".git", ".snatchimg_runs", "__pycache__"}
DEBOUNCE_SECONDS = 0.5


def should_reload(path: str) -> bool:
    file_path = Path(path)
    if any(part in IGNORED_DIRS for part in file_path.parts):
        return False
    return file_path.suffix.lower() in WATCH_EXTENSIONS


class ReloadHandler(FileSystemEventHandler):
    def __init__(self, changed: threading.Event) -> None:
        self.changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if should_reload(event.src_path):
            self.changed.set()


def start_app() -> subprocess.Popen[bytes]:
    print("Starting SnatchIMG development server", flush=True)
    return subprocess.Popen([sys.executable, "web_app.py"], cwd=ROOT)


def stop_app(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> int:
    changed = threading.Event()
    observer = PollingObserver(timeout=1)
    observer.schedule(ReloadHandler(changed), str(ROOT), recursive=True)
    observer.start()
    process = start_app()

    try:
        while True:
            if process.poll() is not None:
                print(
                    "SnatchIMG stopped. Waiting for a file change before restarting.",
                    flush=True,
                )
                changed.wait()
                time.sleep(DEBOUNCE_SECONDS)
                changed.clear()
                process = start_app()
                continue

            if changed.wait(timeout=0.2):
                time.sleep(DEBOUNCE_SECONDS)
                changed.clear()
                print("File change detected. Restarting SnatchIMG.", flush=True)
                stop_app(process)
                process = start_app()
    except KeyboardInterrupt:
        return 0
    finally:
        observer.stop()
        observer.join()
        stop_app(process)


if __name__ == "__main__":
    raise SystemExit(main())
