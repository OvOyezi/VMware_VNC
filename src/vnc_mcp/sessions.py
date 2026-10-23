"""Explicit desktop routing and bounded lifetime for independent VNC sessions."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import math
import re
import time
from typing import Any, Callable

import anyio

from .operations import OperationDispatcher, SessionBackend
from .operations import ActionTimedOut
from .imaging import validate_encoding
from .visual import VisualCondition, VisualWaitResult, VisualWatch


log = logging.getLogger(__name__)
DEFAULT_SESSION = "default"
_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True)
class SessionResult:
    value: Any
    session_id: str
    connection_id: str | None

    def as_dict(self) -> dict:
        return {**self.value, "session_id": self.session_id, "connection_id": self.connection_id}


@dataclass
class _Entry:
    backend: SessionBackend
    dispatcher: OperationDispatcher
    last_used: float = field(default_factory=time.monotonic)
    users: int = 0
    retiring: bool = False
    last_status: dict | None = None
    status_checked_at: float | None = None
    visual_waiting: bool = False


class SessionRegistry:
    """Own sessions on one asyncio loop; never implicitly change the selected VM."""

    def __init__(self, factory: Callable[[], SessionBackend], max_sessions: int = 4,
                 idle_timeout: float = 300.0, max_pending: int = 16) -> None:
        if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or not 1 <= max_sessions <= 32:
            raise ValueError("VNC_MAX_SESSIONS must be an integer between 1 and 32")
        if not math.isfinite(idle_timeout) or not 0 <= idle_timeout <= 86400:
            raise ValueError("VNC_IDLE_TIMEOUT must be finite and between 0 and 86400 seconds (0 disables expiry)")
        if not 1 <= max_pending <= 256:
            raise ValueError("max_pending must be between 1 and 256")
        self.factory = factory
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.max_pending = max_pending
        self._entries: dict[str, _Entry] = {}
        self._closing = False
        self._reaper: asyncio.Task | None = None

    @staticmethod
    def _validate_name(session_id: str) -> None:
        if not isinstance(session_id, str) or not _SESSION_NAME.fullmatch(session_id):
            raise ValueError("session_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit")

    @staticmethod
    def _require_guard(session_id: str, connection_id: str | None) -> None:
        if session_id != DEFAULT_SESSION and not connection_id:
            raise ValueError("Named sessions require the connection_id returned by vnc_connect; pass both session_id and connection_id")

    @asynccontextmanager
    async def _lease(self, session_id: str, create: bool = False, status_query: bool = False):
        self._validate_name(session_id)
        if self._closing:
            raise ConnectionError("VNC server is shutting down")
        entry = self._entries.get(session_id)
        if entry is None:
            if not create and session_id != DEFAULT_SESSION:
                raise ConnectionError(f"Unknown or expired VNC session {session_id!r}; call vnc_connect explicitly")
            transient_status = status_query and not create and session_id == DEFAULT_SESSION
            if not transient_status and len(self._entries) >= self.max_sessions:
                raise RuntimeError("VNC session limit reached; disconnect an existing session or wait for idle expiry")
            backend = self.factory()
            entry = _Entry(backend, OperationDispatcher(backend, self.max_pending))
            if not transient_status:
                self._entries[session_id] = entry
        if entry.retiring:
            raise ConnectionError("VNC session is closing; reconnect explicitly after cleanup finishes")
        if entry.users >= self.max_pending:
            raise RuntimeError("VNC session queue is full; wait for outstanding calls to finish")
        entry.users += 1
        try:
            yield entry
        finally:
            entry.users -= 1
            entry.last_used = time.monotonic()
            # Failed connects and explicit disconnects release capacity immediately.
            # A queued call or async wait retains its original entry until it exits.
            # Identity protects a persistent default entry created while a
            # transient status lease for that same ID is still unwinding.
            if not entry.users and entry.backend.connection_id is None and not entry.retiring:
                if self._entries.get(session_id) is entry:
                    del self._entries[session_id]

    @staticmethod
    def _remember(entry: _Entry, result: Any) -> None:
        if isinstance(result, dict) and "connected" in result:
            entry.last_status = dict(result)
            entry.status_checked_at = time.monotonic()

    async def run(self, method: str, *args: Any, session_id: str = DEFAULT_SESSION,
                  connection_id: str | None = None, pin: bool = True) -> SessionResult:
        if pin:
            self._require_guard(session_id, connection_id)
        async with self._lease(session_id, create=method == "connect",
                               status_query=method == "status") as entry:
            value = await entry.dispatcher.run(method, *args, connection_id=connection_id, pin=pin)
            self._remember(entry, value)
            return SessionResult(value, session_id, entry.backend.connection_id)

    async def wait(self, seconds: float, session_id: str = DEFAULT_SESSION,
                   connection_id: str | None = None) -> SessionResult:
        if not math.isfinite(seconds) or not 0 <= seconds <= 60:
            raise ValueError("seconds must be finite and between 0 and 60")
        self._require_guard(session_id, connection_id)
        async with self._lease(session_id) as entry:
            before = await entry.dispatcher.run("status", pin=False)
            self._remember(entry, before)
            if not before["connected"]:
                raise ConnectionError("Not connected; call vnc_connect first")
            if connection_id is not None and connection_id != before["connection_id"]:
                raise ConnectionError("VNC target changed; inspect the current desktop before retrying")
            # The lease prevents idle expiry, without holding a worker or session lock.
            await asyncio.sleep(seconds)
            after = await entry.dispatcher.run("status", pin=False)
            self._remember(entry, after)
            if not after["connected"] or before["connection_id"] != after["connection_id"]:
                raise ConnectionError("VNC target changed or disconnected during wait")
            return SessionResult({"waited_seconds": seconds}, session_id, after["connection_id"])

    async def wait_for_visual(self, condition: str = "stable", reference_id: str | None = None,
                              region: tuple[int, int, int, int] | None = None, timeout: float = 10.0,
                              interval: float = 0.25, stable_for: float = 0.5,
                              pixel_threshold: int = 8, max_changed_ratio: float = 0.001,
                              image_format: str = "png", quality: int = 80,
                              session_id: str = DEFAULT_SESSION, connection_id: str | None = None) -> SessionResult:
        settings = VisualCondition(condition, timeout, interval, stable_for, pixel_threshold, max_changed_ratio)
        image_format = validate_encoding(image_format, quality)
        if condition == "stable" and reference_id is not None:
            raise ValueError("stable samples its own initial frame; omit reference_id")
        if condition != "stable" and not reference_id:
            raise ValueError("changed and matches require a remembered screenshot reference_id")
        self._require_guard(session_id, connection_id)
        started = time.monotonic()
        deadline = started + timeout if timeout else None
        async with self._lease(session_id) as entry:
            if entry.visual_waiting:
                raise RuntimeError("a visual wait is already active for this session")
            entry.visual_waiting = True
            try:
                before = await entry.dispatcher.run("status", pin=False)
                self._remember(entry, before)
                if not before["connected"]:
                    raise ConnectionError("Not connected; call vnc_connect first")
                expected = before["connection_id"]
                if connection_id is not None and connection_id != expected:
                    raise ConnectionError("VNC target changed; inspect the current desktop before retrying")
                reference = None
                if reference_id is not None:
                    reference = await entry.dispatcher.run("visual_reference", reference_id, region,
                                                           connection_id=expected)
                    region = reference.region
                watch = VisualWatch(settings, reference)
                last_frame, matched, last_observed = None, False, started
                total_bytes, total_capture_seconds = 0, 0.0
                while True:
                    try:
                        frame = await entry.dispatcher.run("visual_sample", region, connection_id=expected, deadline=deadline)
                    except ActionTimedOut:
                        if last_frame is None or deadline is None or time.monotonic() < deadline:
                            raise
                        break
                    last_observed = time.monotonic()
                    matched = await asyncio.to_thread(watch.observe, frame, last_observed)
                    last_frame = frame
                    total_bytes += frame.transport_bytes
                    total_capture_seconds += frame.capture_seconds
                    if matched or deadline is None or time.monotonic() >= deadline:
                        break
                    # No backend/dispatcher lock, worker or image encoder is held
                    # between samples. Other actions and sessions remain usable.
                    await asyncio.sleep(min(interval, max(0, deadline-time.monotonic())))
                image = await asyncio.to_thread(last_frame.encode, image_format, quality)
                after = await entry.dispatcher.run("status", connection_id=expected)
                self._remember(entry, after)
                if not after["connected"] or after["connection_id"] != expected:
                    raise ConnectionError("VNC target changed or disconnected during visual wait")
                details = watch.metadata(matched, time.monotonic()-started, last_observed)
                details.update(reference_id=reference_id, sampled_transport_bytes=total_bytes,
                               sampled_capture_seconds=round(total_capture_seconds, 6),
                               last_sample_age_seconds=round(time.monotonic()-last_observed, 6))
                return SessionResult(VisualWaitResult(image, details), session_id, expected)
            finally:
                entry.visual_waiting = False

    def inventory(self) -> dict:
        now = time.monotonic()
        return {
            "session_count": len(self._entries),
            "max_sessions": self.max_sessions,
            "max_pending_per_session": self.max_pending,
            "idle_timeout_seconds": self.idle_timeout,
            "sessions": [
                {
                    "session_id": name,
                    "outstanding_calls": entry.users,
                    "pending_work_operations": entry.dispatcher.pending,
                    "closing": entry.retiring,
                    "visual_wait_active": entry.visual_waiting,
                    "idle_seconds": round(max(0, now - entry.last_used), 3) if not entry.users else 0,
                    "last_known_status": dict(entry.last_status) if entry.last_status is not None else None,
                    "status_age_seconds": round(now - entry.status_checked_at, 3) if entry.status_checked_at is not None else None,
                }
                for name, entry in sorted(self._entries.items())
            ],
            "status_note": "Inventory uses cached status and never waits for busy desktops; call vnc_status(session_id=...) for current state",
        }

    def start(self) -> None:
        if self.idle_timeout and self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="vnc-idle-reaper")

    async def _retire(self, name: str, entry: _Entry) -> bool:
        try:
            await entry.dispatcher.close()
        except Exception:
            # Quarantine the slot if cleanup fails, rather than leaking unlimited
            # transports by making capacity available prematurely.
            log.exception("Failed to close VNC session %s", name)
            return False
        if self._entries.get(name) is entry:
            del self._entries[name]
        return True

    async def reap_idle(self) -> int:
        if self._closing or not self.idle_timeout:
            return 0
        now = time.monotonic()
        expired = [(name, entry) for name, entry in self._entries.items()
                   if not entry.users and not entry.retiring and now - entry.last_used >= self.idle_timeout]
        for _, entry in expired:
            entry.retiring = True
        with anyio.CancelScope(shield=True):
            results = await asyncio.gather(*(self._retire(name, entry) for name, entry in expired))
        return sum(results)

    async def _reap_loop(self) -> None:
        interval = max(0.05, min(30.0, self.idle_timeout / 2))
        while True:
            await asyncio.sleep(interval)
            await self.reap_idle()

    async def close(self) -> None:
        self._closing = True
        with anyio.CancelScope(shield=True):
            if self._reaper is not None:
                self._reaper.cancel()
                await asyncio.gather(self._reaper, return_exceptions=True)
                self._reaper = None
            entries = tuple(self._entries.items())
            for _, entry in entries:
                entry.retiring = True
            await asyncio.gather(*(self._retire(name, entry) for name, entry in entries))
