from __future__ import annotations

import json
import math
import queue
import re
import threading
from typing import Any, Callable

ProgressCallback = Callable[[dict[str, Any]], None]

MAX_PROGRESS_MARKER = 4096
MAX_PROGRESS_TEXT = 256
MAX_PROGRESS_METRICS = 16
PROGRESS_EVENT_QUEUE_CAPACITY = 64
PROGRESS_FLUSH_TIMEOUT = 65.0


def parse_progress_marker(line: str) -> dict[str, Any] | None:
    prefix = "[AGENT_PROGRESS] "
    if not line.startswith(prefix) or len(line) > MAX_PROGRESS_MARKER:
        return None
    try:
        payload = json.loads(line[len(prefix):].strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    result: dict[str, Any] = {}
    for field in ("stage_name", "message"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()[:MAX_PROGRESS_TEXT]
    if "stage_name" not in result and "message" not in result:
        return None

    for field in ("current", "total"):
        value = payload.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(float(value)) and abs(float(value)) <= 1_000_000_000:
                result[field] = value

    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        safe_metrics: dict[str, str | int | float | bool] = {}
        for key, value in list(metrics.items())[:MAX_PROGRESS_METRICS]:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
                continue
            if isinstance(value, (str, int, float, bool)):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                safe_metrics[key] = value
        if safe_metrics:
            result["metrics"] = safe_metrics
    return result


class ProgressDispatcher:
    """Keep progress publication outside command watchdog loops with bounded handoff."""

    def __init__(self, callback: ProgressCallback, log: Callable[[str], None]) -> None:
        self._callback = callback
        self._log = log
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=PROGRESS_EVENT_QUEUE_CAPACITY
        )
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="local-agent-progress",
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                try:
                    self._callback(event)
                except Exception as exc:
                    self._log(
                        f"progress callback failed: {type(exc).__name__}: {exc}"
                    )
            finally:
                self._queue.task_done()

    def submit(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            dropped = self._queue.get_nowait()
            self._queue.task_done()
            if dropped is None:
                self._closed = True
                return
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._log("progress event dropped because the bounded queue is full")

    def close(self, timeout: float = PROGRESS_FLUSH_TIMEOUT) -> None:
        if self._closed:
            return
        self._closed = True
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    continue
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            self._log("progress publication did not drain within its bounded deadline")
