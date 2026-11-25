"""Async SSE EventBus with per-run raw_logs ring buffer and HITL state cache."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class EventBus:
    """Broadcasts SSE events to concurrent subscribers and caches transient run state."""

    def __init__(
        self,
        max_raw_logs_per_run: int = 500,
        subscriber_queue_size: int = 256,
        max_tracked_runs: int = 100,
    ) -> None:
        self._subscribers: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._subscriber_queue_size = subscriber_queue_size
        self._max_raw_logs = max_raw_logs_per_run
        self._max_tracked_runs = max(1, max_tracked_runs)
        self._raw_logs: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        self._hitl_requests: dict[str, dict[str, Any]] = {}
        self._current_kiki_status: dict[str, Any] = {"status": "idle"}

    @property
    def current_kiki_status(self) -> dict[str, Any]:
        return dict(self._current_kiki_status)

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "kiki_status":
            self._current_kiki_status = dict(payload)

        dead: list[asyncio.Queue[tuple[str, dict[str, Any]]]] = []
        for queue in list(self._subscribers):
            if queue.full():
                # Never let high-volume raw_log bursts evict critical state events
                if event_type == "raw_log":
                    continue
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait((event_type, payload))
            except Exception:
                dead.append(queue)
        for q in dead:
            self._subscribers.discard(q)

    def publish_kiki_status(
        self,
        status: Literal["idle", "thinking", "running", "waiting_input"],
        reason: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {"status": status}
        if reason:
            payload["reason"] = reason
        self.publish("kiki_status", payload)

    def append_raw_log(
        self,
        run_id: str,
        line: str,
        stream: Literal["stdout", "stderr", "ndjson"] = "stdout",
    ) -> dict[str, Any]:
        entry = {
            "run_id": run_id,
            "line": line,
            "timestamp": _utc_now(),
            "stream": stream,
        }
        buf = self._raw_logs.get(run_id)
        if buf is None:
            buf = deque(maxlen=self._max_raw_logs)
            self._raw_logs[run_id] = buf
            while len(self._raw_logs) > self._max_tracked_runs:
                self._raw_logs.popitem(last=False)
        else:
            self._raw_logs.move_to_end(run_id)
        buf.append(entry)
        self.publish("raw_log", entry)
        return entry

    def get_raw_logs(self, run_id: str) -> list[dict[str, Any]]:
        buf = self._raw_logs.get(run_id)
        return list(buf) if buf else []

    def set_hitl_request(self, run_id: str, hitl_request: dict[str, Any]) -> None:
        self._hitl_requests[run_id] = dict(hitl_request)

    def clear_hitl_request(self, run_id: str) -> None:
        self._hitl_requests.pop(run_id, None)

    def get_hitl_request(self, run_id: str) -> Optional[dict[str, Any]]:
        req = self._hitl_requests.get(run_id)
        return dict(req) if req else None

    def enrich_run(self, run_dict: dict[str, Any]) -> dict[str, Any]:
        """Attach cached raw_logs and hitl_request to a DB-hydrated SessionRun dict."""
        run_id = run_dict["id"]
        enriched = dict(run_dict)
        enriched["raw_logs"] = self.get_raw_logs(run_id)
        enriched["hitl_request"] = self.get_hitl_request(run_id)
        return enriched

    async def subscribe(self, heartbeat_interval: float = 15.0) -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        self._subscribers.add(queue)
        try:
            # Send initial kiki_status immediately upon connection
            init_data = json.dumps(self._current_kiki_status, ensure_ascii=False)
            yield f"event: kiki_status\ndata: {init_data}\n\n"

            while True:
                try:
                    event_type, payload = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval
                    )
                    data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subscribers.discard(queue)
