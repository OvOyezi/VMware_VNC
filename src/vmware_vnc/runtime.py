# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Shared, reference-counted asyncio runtime for synchronous RFB callers."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeout
import math
import threading
from typing import Any, Awaitable


_CANCEL_DRAIN_TIMEOUT = 5.0
_SHUTDOWN_TIMEOUT = 15.0


class _RuntimeCore:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="vnc-rfb-io", daemon=True)
        self.ready = threading.Event()
        self.closing = False
        self.lease_count = 0
        self.tasks: set[asyncio.Task[Any]] = set()
        self._start_error: BaseException | None = None
        self.thread.start()
        if not self.ready.wait(5):
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            raise TimeoutError("native RFB event loop did not start")
        if self._start_error is not None:
            raise RuntimeError("native RFB event loop failed to start") from None

    def _run(self) -> None:
        try:
            asyncio.set_event_loop(self.loop)
        except BaseException as exc:
            self._start_error = exc
            self.ready.set()
            return
        self.ready.set()
        self.loop.run_forever()
        try:
            self.loop.close()
        finally:
            asyncio.set_event_loop(None)

    def submit(self, awaitable: Awaitable[Any], timeout: float) -> Any:
        if threading.current_thread() is self.thread:
            _close_awaitable(awaitable)
            raise RuntimeError("runtime.call cannot run on the RFB I/O thread")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            _close_awaitable(awaitable)
            raise ValueError("runtime timeout must be finite and greater than zero")
        if self.closing or not self.thread.is_alive() or self.loop.is_closed():
            _close_awaitable(awaitable)
            raise RuntimeError("RFB runtime is closed")

        completion: Future[Any] = Future()
        slot: dict[str, Any] = {"task": None, "abandoned": False}
        slot_lock = threading.Lock()

        def start() -> None:
            with slot_lock:
                if slot["abandoned"]:
                    _close_awaitable(awaitable)
                    return
                if self.closing:
                    _close_awaitable(awaitable)
                    if not completion.done():
                        completion.set_exception(RuntimeError("RFB runtime is closed"))
                    return
                try:
                    async def invoke() -> Any:
                        return await awaitable

                    task = self.loop.create_task(invoke())
                except BaseException as exc:
                    _close_awaitable(awaitable)
                    if not completion.done():
                        completion.set_exception(exc)
                    return
                slot["task"] = task
                self.tasks.add(task)

            def finished(done: asyncio.Task[Any]) -> None:
                self.tasks.discard(done)
                # A task can be cancelled before invoke() first runs. In that
                # case Python never awaits or closes the supplied coroutine.
                _close_awaitable(awaitable)
                if completion.done():
                    return
                try:
                    completion.set_result(done.result())
                except BaseException as exc:
                    completion.set_exception(exc)

            task.add_done_callback(finished)

        try:
            self.loop.call_soon_threadsafe(start)
        except RuntimeError:
            _close_awaitable(awaitable)
            raise RuntimeError("RFB runtime is closed") from None

        try:
            return completion.result(timeout=timeout)
        except FutureTimeout:
            if completion.done():
                return completion.result()
            with slot_lock:
                task = slot["task"]
                if task is None:
                    slot["abandoned"] = True
            if task is not None:
                try:
                    async def cancel_and_drain() -> None:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                    drained = asyncio.run_coroutine_threadsafe(cancel_and_drain(), self.loop)
                    drained.result(timeout=_CANCEL_DRAIN_TIMEOUT)
                except FutureTimeout:
                    raise TimeoutError("timed-out RFB operation did not drain cancellation") from None
                except RuntimeError:
                    pass
            else:
                _close_awaitable(awaitable)
            raise TimeoutError("RFB operation timed out") from None

    def shutdown(self) -> None:
        if not self.thread.is_alive():
            return

        async def close_loop() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.loop.shutdown_asyncgens()
            await self.loop.shutdown_default_executor()

        try:
            asyncio.run_coroutine_threadsafe(close_loop(), self.loop).result(timeout=_SHUTDOWN_TIMEOUT)
        except FutureTimeout:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            raise TimeoutError("RFB runtime shutdown timed out") from None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if self.thread.is_alive():
            raise TimeoutError("RFB I/O thread did not stop")


def _close_awaitable(awaitable: Awaitable[Any]) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


class RfbRuntime:
    """One lease on the process-wide asyncio loop used by RFB connections."""

    _condition = threading.Condition()
    _core: _RuntimeCore | None = None

    def __init__(self, core: _RuntimeCore) -> None:
        self._core = core
        self._released = False
        self._release_lock = threading.Lock()

    @classmethod
    def acquire(cls) -> "RfbRuntime":
        with cls._condition:
            while cls._core is not None and cls._core.closing:
                cls._condition.wait()
            core = cls._core
            if core is None:
                core = _RuntimeCore()
                cls._core = core
            core.lease_count += 1
            return cls(core)

    def call(self, coroutine: Awaitable[Any], timeout: float) -> Any:
        with self._release_lock:
            if self._released:
                _close_awaitable(coroutine)
                raise RuntimeError("RFB runtime lease is released")
        return self._core.submit(coroutine, timeout)

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        core = self._core
        shutdown = False
        cls = type(self)
        with cls._condition:
            if core.lease_count <= 0:
                return
            core.lease_count -= 1
            if core.lease_count == 0:
                core.closing = True
                shutdown = True
        if not shutdown:
            return
        try:
            core.shutdown()
        finally:
            with cls._condition:
                if cls._core is core:
                    cls._core = None
                cls._condition.notify_all()

    close = release

    @classmethod
    def info(cls) -> dict[str, int | str]:
        with cls._condition:
            core = cls._core
            live = int(core is not None and core.thread.is_alive())
            leases = int(core.lease_count) if core is not None else 0
        return {
            "engine": "native-asyncio-rfb",
            "io_threads": live,
            "lease_count": leases,
            "connection_count": leases,
        }

    def __enter__(self) -> "RfbRuntime":
        if self._released:
            raise RuntimeError("RFB runtime lease is released")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
