"""Bounded, cancellable operations for the synchronous VNC backend."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Protocol

import anyio


class ActionCancelled(ConnectionError):
    pass


class ActionTimedOut(TimeoutError):
    pass


@dataclass
class ActionControl:
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)

    def check(self) -> None:
        if self.cancelled.is_set():
            raise ActionCancelled("VNC action cancelled; input may be partial and must not be replayed automatically")
        if time.monotonic() >= self.deadline:
            raise ActionTimedOut("VNC action time budget exceeded; input may be partial")

    def wait(self, seconds: float) -> None:
        self.check()
        self.cancelled.wait(min(seconds, max(0, self.deadline - time.monotonic())))
        self.check()


class SessionBackend(Protocol):
    connection_id: str | None
    action_timeout: float

    def execute(self, method: str, args: tuple, control: ActionControl, expected: str | None, pin: bool) -> Any: ...
    def disconnect(self) -> dict: ...


class OperationDispatcher:
    """Serialize before creating workers, and pin queued work to its connection."""

    def __init__(self, session: SessionBackend, max_pending: int = 16) -> None:
        self.session = session
        self.max_pending = max_pending
        self._pending = 0
        self._lock = asyncio.Lock()
        self._active: ActionControl | None = None
        self._closing = False

    @property
    def pending(self) -> int:
        return self._pending

    async def run(self, method: str, *args: Any, connection_id: str | None = None, pin: bool = True) -> Any:
        expected = connection_id if connection_id is not None else self.session.connection_id
        if self._closing:
            raise ConnectionError("VNC server is shutting down")
        if self._pending >= self.max_pending:
            raise RuntimeError("VNC operation queue is full; wait for outstanding calls to finish")
        self._pending += 1
        try:
            async with self._lock:
                if self._closing:
                    raise ConnectionError("VNC server is shutting down")
                control = ActionControl(time.monotonic() + self.session.action_timeout)
                self._active = control
                worker = asyncio.create_task(asyncio.to_thread(
                    self.session.execute, method, args, control, expected, pin,
                ))
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    control.cancelled.set()
                    # MCP uses AnyIO cancellation scopes. Drain the actual worker
                    # before releasing serialization or starting a target switch.
                    with anyio.CancelScope(shield=True):
                        try:
                            await asyncio.shield(worker)
                        except Exception:
                            pass
                    raise
                finally:
                    self._active = None
        finally:
            self._pending -= 1

    async def close(self) -> None:
        self._closing = True
        if self._active is not None:
            self._active.cancelled.set()
        with anyio.CancelScope(shield=True):
            async with self._lock:
                await asyncio.to_thread(self.session.disconnect)
