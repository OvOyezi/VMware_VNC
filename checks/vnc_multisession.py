"""Multi-desktop isolation/lifetime tests. All keyboard/mouse events stay in fixtures."""
from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, redirect_stderr
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from vnc_deep_checks import cancel, keys
from vnc_regression import Fixture, payload


def startup_child(args):
    sys.path.insert(0, str(args.source / "src"))
    from vmware_vnc.client import VNCSession
    from vmware_vnc.rfb import runtime_info

    barrier = threading.Barrier(2)
    errors = []
    states = [None, None]
    clients = [None, None]
    sessions = [VNCSession(timeout=2) for _ in range(2)]

    def connect(index):
        try:
            barrier.wait(timeout=5)
            states[index] = sessions[index].connect("127.0.0.1", args.startup_child[index])
        except Exception as exc:
            errors.append({"index": index, "type": type(exc).__name__, "message": str(exc)})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(connect, index) for index in range(2)]
            for future in futures:
                future.result(timeout=8)

        clients = [session.client for session in sessions]
        runtime = runtime_info()
        io_thread_names = [thread.name for thread in threading.enumerate() if thread.name == "vnc-rfb-io"]
        protocol_errors = [client.protocol_error for client in clients if client is not None]
        result = {
            "runtime": runtime,
            "io_thread_names": io_thread_names,
            "both_connected": all(state is not None and state["connected"] for state in states),
            "connect_errors": errors,
            "protocol_errors": protocol_errors,
        }
        assert result["both_connected"], result
        assert not errors and protocol_errors == [None, None], result
        assert runtime["engine"] == "native-asyncio-rfb" and runtime["io_threads"] == 1, result
        assert io_thread_names == ["vnc-rfb-io"], result
    finally:
        cleanup_errors = []
        for session in sessions:
            try:
                session.disconnect()
            except Exception as exc:
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
        closed = [client.closed.wait(2) if client is not None else True for client in clients]
        if cleanup_errors or not all(closed):
            raise AssertionError({"cleanup_errors": cleanup_errors, "clients_closed_after_cleanup": closed})
    result["clients_closed_after_cleanup"] = True
    print(json.dumps(result))


@asynccontextmanager
async def server(args, label, **overrides):
    env = dict(os.environ)
    for name in ("VNC_HOST", "VNC_PORT", "VNC_PASSWORD"):
        env.pop(name, None)
    env.update(VNC_TIMEOUT="0.5", VNC_SCREENSHOT_MODE="fresh", VNC_MAX_SESSIONS="2", VNC_IDLE_TIMEOUT="0")
    env.update(overrides)
    params = StdioServerParameters(command=sys.executable, args=[str(args.source / "main.py"), "mcp"],
                                  cwd=str(args.source), env=env)
    with args.output.with_name(args.output.stem + f"-{label}.stderr.log").open("w", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session


async def call(session, name, **arguments):
    return payload(await session.call_tool(name, arguments))


def handle(state):
    return {key: state[key] for key in ("session_id", "connection_id")}


async def screenshot(session, state, fixture):
    result = await session.call_tool("vnc_screenshot", {**handle(state), "image_format": "png"})
    assert not result.isError, result
    metadata = json.loads(next(item.text for item in result.content if item.type == "text"))
    image = Image.open(BytesIO(base64.b64decode(next(item.data for item in result.content if item.type == "image"))))
    assert image.size == (64, 48) and image.getpixel((20, 17)) == fixture.rgb(20, 17)
    assert metadata["session_id"] == state["session_id"] and metadata["connection_id"] == state["connection_id"]
    return image


async def connect(session, name, fixture, password=""):
    state = await call(session, "vnc_connect", host="127.0.0.1", port=fixture.port, session_id=name, password=password)
    assert state["connected"] and state["session_id"] == name and state["name"] == fixture.name
    await screenshot(session, state, fixture)
    return state


async def until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate()


async def isolation_checks(args, report, a, b, c, auth):
    checks, metrics = report["checks"], report["metrics"]
    async with server(args, "isolation") as session:
        tool_list = await session.list_tools()
        assert {"vnc_connect", "vnc_screenshot", "vnc_text", "vnc_profiles", "vnc_diagnostics"}.issubset({tool.name for tool in tool_list.tools})
        assert all("session_id" in tool.inputSchema["properties"] for tool in tool_list.tools
                   if tool.name not in {"vnc_sessions", "vnc_profiles", "vnc_diagnostics"})
        checks["all_control_tools_expose_explicit_session_routing"] = True

        alpha, beta = await asyncio.gather(connect(session, "alpha", a), connect(session, "beta", b))
        assert alpha["connection_id"] != beta["connection_id"]
        initial_inventory = await call(session, "vnc_sessions")
        assert initial_inventory["session_count"] == 2
        checks["two_desktops_connect_and_capture_concurrently"] = True

        event_counts = [len(item.events) for item in (a, b, c)]
        empty_default = await call(session, "vnc_status")
        assert empty_default["connected"] is False
        assert empty_default["session_id"] == "default" and empty_default["connection_id"] is None
        after_empty_status = await call(session, "vnc_sessions")
        initial_ids = {
            item["session_id"]: item["last_known_status"]["connection_id"]
            for item in initial_inventory["sessions"]
        }
        after_status_ids = {
            item["session_id"]: item["last_known_status"]["connection_id"]
            for item in after_empty_status["sessions"]
        }
        assert after_empty_status["session_count"] == 2 and after_status_ids == initial_ids == {
            "alpha": alpha["connection_id"], "beta": beta["connection_id"],
        }
        unknown_status = await session.call_tool("vnc_status", {"session_id": "missing"})
        unknown_status_error = " ".join(getattr(item, "text", "") for item in unknown_status.content)
        assert unknown_status.isError
        assert "Unknown or expired VNC session" in unknown_status_error
        assert "call vnc_connect explicitly" in unknown_status_error
        assert [len(item.events) for item in (a, b, c)] == event_counts
        checks["default_status_at_capacity_is_transient_and_named_status_stays_guarded"] = True

        invalid_calls = [
            session.call_tool("vnc_keypress", {"key": "x", "session_id": "alpha"}),
            session.call_tool("vnc_keypress", {"key": "x", "session_id": "missing", "connection_id": alpha["connection_id"]}),
            session.call_tool("vnc_keypress", {"key": "x", **handle(alpha), "connection_id": beta["connection_id"]}),
            session.call_tool("vnc_keypress", {"key": "x"}),
        ]
        assert all(result.isError for result in await asyncio.gather(*invalid_calls))
        assert [len(item.events) for item in (a, b, c)] == event_counts
        checks["missing_wrong_or_omitted_target_never_routes_to_named_desktop"] = True

        accepted_before = c.accepted
        assert (await session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": c.port, "session_id": "gamma"})).isError
        assert c.accepted == accepted_before and (await call(session, "vnc_sessions"))["session_count"] == 2
        assert [len(item.events) for item in (a, b, c)] == event_counts
        checks["session_limit_rejects_before_opening_transport"] = True

        for invalid in ("", "with space", "../other", "x" * 65, "虚拟机"):
            assert (await session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": c.port, "session_id": invalid})).isError
        assert c.accepted == accepted_before
        checks["invalid_session_names_do_not_allocate_resources"] = True

        a.silent = True
        stalled = asyncio.create_task(session.call_tool("vnc_screenshot", handle(alpha)))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await call(session, "vnc_keypress", key="b", **handle(beta))
        await screenshot(session, beta, b)
        metrics["other_desktop_key_and_capture_during_network_timeout_seconds"] = round(time.monotonic() - started, 4)
        assert metrics["other_desktop_key_and_capture_during_network_timeout_seconds"] < 0.3
        assert (await stalled).isError
        a.silent = False
        unavailable = await call(session, "vnc_status", session_id="alpha")
        assert unavailable["connected"] and unavailable["display_state"] == "unavailable"
        # A failed observation must be recovered before subsequent input. This
        # read is explicit and does not replay the action which timed out.
        await screenshot(session, alpha, a)
        assert (await call(session, "vnc_status", session_id="alpha"))["display_state"] == "ready"
        checks["network_timeout_on_one_desktop_does_not_block_another"] = True

        blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "a" * 9, "method": "keys", "delay_ms": 70, **handle(alpha)}))
        await asyncio.sleep(0.05)
        queued = [asyncio.create_task(session.call_tool("vnc_status", {"session_id": "alpha"})) for _ in range(24)]
        await asyncio.sleep(0.02)
        started = time.monotonic()
        inventory = await call(session, "vnc_sessions")
        await call(session, "vnc_keypress", key="c", **handle(beta))
        metrics["inventory_and_other_desktop_during_overload_seconds"] = round(time.monotonic() - started, 4)
        assert metrics["inventory_and_other_desktop_during_overload_seconds"] < 0.2
        active = next(item for item in inventory["sessions"] if item["session_id"] == "alpha")
        assert 1 <= active["outstanding_calls"] <= 16 and active["pending_work_operations"] <= 16
        rejected = sum(item.isError for item in await asyncio.gather(*queued))
        payload(await blocker)
        assert rejected > 0 and (await call(session, "vnc_status", session_id="beta"))["connected"]
        metrics["overloaded_session_rejections"] = rejected
        checks["queue_overload_and_inventory_are_isolated_per_session"] = True

        waiters = [asyncio.create_task(session.call_tool("vnc_wait", {"seconds": 0.3, **handle(alpha)})) for _ in range(24)]
        await asyncio.sleep(0.05)
        inventory = await call(session, "vnc_sessions")
        assert next(item for item in inventory["sessions"] if item["session_id"] == "alpha")["outstanding_calls"] <= 16
        await call(session, "vnc_keypress", key="c", **handle(beta))
        rejected_waits = sum(item.isError for item in await asyncio.gather(*waiters))
        assert rejected_waits == 8
        metrics["async_wait_overload_rejections"] = rejected_waits
        checks["async_wait_calls_are_bounded_without_blocking_other_sessions"] = True

        before = keys(a, 113)
        request_id = session._request_id
        typing = asyncio.create_task(session.call_tool("vnc_type", {"text": "q" * 20, "method": "keys", "delay_ms": 70, **handle(alpha)}))
        await asyncio.sleep(0.17)
        at_cancel = keys(a, 113) - before
        await cancel(session, request_id, typing)
        await until(lambda: keys(a, 113) - before <= at_cancel + 1)
        await asyncio.sleep(0.25)
        final = keys(a, 113) - before
        assert final < 20 and final <= at_cancel + 1
        inventory = await call(session, "vnc_sessions")
        assert [item["session_id"] for item in inventory["sessions"]] == ["beta"]
        await screenshot(session, beta, b)
        metrics["cancelled_named_typing"] = {"before_cancel": at_cancel, "final": final, "requested": 20}
        checks["cancellation_removes_only_affected_session"] = True

        # Omitted session_id stays on the independent legacy default desktop.
        legacy = await call(session, "vnc_connect", host="127.0.0.1", port=c.port)
        await screenshot(session, legacy, c)
        legacy_inventory = await call(session, "vnc_sessions")
        assert legacy_inventory["session_count"] == 2
        legacy_entries = {item["session_id"]: item for item in legacy_inventory["sessions"]}
        assert legacy_entries["default"]["last_known_status"]["connection_id"] == legacy["connection_id"]
        assert legacy_entries["beta"]["last_known_status"]["connection_id"] == beta["connection_id"]
        auth_accepted_before = auth.accepted
        assert (await session.call_tool("vnc_connect", {
            "host": "127.0.0.1", "port": auth.port, "session_id": "gamma",
        })).isError
        assert auth.accepted == auth_accepted_before and (await call(session, "vnc_sessions"))["session_count"] == 2

        typed_before = keys(c, ord("m"))
        default_input = asyncio.create_task(session.call_tool("vnc_type", {
            "text": "m" * 4, "method": "keys", "delay_ms": 70,
        }))
        await until(lambda: keys(c, ord("m")) > typed_before)
        queued_default_status = asyncio.create_task(session.call_tool("vnc_status", {}))
        await asyncio.sleep(0.03)
        assert not queued_default_status.done()
        payload(await default_input)
        existing_default_status = payload(await queued_default_status)
        assert existing_default_status["connected"] is True
        assert existing_default_status["session_id"] == "default"
        assert existing_default_status["connection_id"] == legacy["connection_id"]
        checks["existing_default_status_keeps_dispatcher_serialization_and_capacity"] = True

        z_before = [keys(item, 122) for item in (a, b, c)]
        await call(session, "vnc_keypress", key="z")
        await until(lambda: keys(c, 122) == z_before[2] + 1)
        assert [keys(item, 122) for item in (a, b)] == z_before[:2]
        await call(session, "vnc_disconnect")
        assert (await call(session, "vnc_status", session_id="beta"))["connection_id"] == beta["connection_id"]
        checks["legacy_default_is_independent_and_disconnect_releases_capacity"] = True

        assert (await session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": auth.port,
                                                         "password": "wrong", "session_id": "alpha"})).isError
        assert (await call(session, "vnc_sessions"))["session_count"] == 1
        guarded = await connect(session, "alpha", auth, "test1234")
        assert "test1234" not in json.dumps(await call(session, "vnc_sessions"))
        await call(session, "vnc_disconnect", **handle(guarded))
        checks["failed_auth_frees_slot_and_inventory_excludes_passwords"] = True

        alpha_new = await connect(session, "alpha", c)
        assert (await session.call_tool("vnc_keypress", {**handle(alpha_new), "connection_id": alpha["connection_id"], "key": "r"})).isError
        assert (await call(session, "vnc_status", session_id="beta"))["connection_id"] == beta["connection_id"]
        checks["reused_name_rejects_previous_generation_without_affecting_other_vm"] = True

        # Queue a switch and then old work behind a slow action on one named slot.
        blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "j" * 5, "method": "keys", "delay_ms": 60, **handle(alpha_new)}))
        await asyncio.sleep(0.03)
        switch = asyncio.create_task(session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": a.port, "session_id": "alpha"}))
        await asyncio.sleep(0.03)
        stale = asyncio.create_task(session.call_tool("vnc_keypress", {"key": "r", **handle(alpha_new)}))
        payload(await blocker)
        alpha_latest = payload(await switch)
        assert (await stale).isError
        await screenshot(session, alpha_latest, a)
        await screenshot(session, beta, b)
        checks["queued_named_input_cannot_cross_a_target_switch"] = True

        for state in (alpha_latest, beta):
            await call(session, "vnc_key_down", key="shift", **handle(state))
            await call(session, "vnc_mouse_down", x=5, y=6, button="right", **handle(state))
    await until(lambda: not a.connections and not b.connections and not c.connections and not auth.connections)
    for item in (a, b):
        assert ("key", False, 0xFFE1) in item.events and ("pointer", 0, 5, 6) in item.events
    checks["stdio_shutdown_closes_every_session_and_releases_held_input"] = True


async def idle_checks(args, report, a, b):
    checks = report["checks"]
    async with server(args, "idle", VNC_IDLE_TIMEOUT="0.3") as session:
        alpha = await connect(session, "alpha", a)
        await call(session, "vnc_key_down", key="ctrl", **handle(alpha))
        beta = await connect(session, "beta", b)
        waiter = asyncio.create_task(session.call_tool("vnc_wait", {"seconds": 0.8, **handle(beta)}))
        await asyncio.sleep(0.57)
        inventory = await call(session, "vnc_sessions")
        assert [item["session_id"] for item in inventory["sessions"]] == ["beta"]
        assert ("key", False, 0xFFE3) in a.events
        assert (await session.call_tool("vnc_keypress", {"key": "x", **handle(alpha)})).isError
        assert inventory["sessions"][0]["outstanding_calls"] == 1
        payload(await waiter)
        checks["idle_reaper_releases_held_keys_and_requires_explicit_reconnect"] = True
        checks["active_async_wait_is_exempt_from_idle_expiry"] = True
        await asyncio.sleep(0.55)
        assert not (await call(session, "vnc_sessions"))["sessions"]
        assert not a.connections and not b.connections
        alpha_new = await connect(session, "alpha", a)
        assert alpha_new["connection_id"] != alpha["connection_id"]
        assert (await session.call_tool("vnc_keypress", {"key": "x", "session_id": "alpha", "connection_id": alpha["connection_id"]})).isError
        await call(session, "vnc_disconnect", **handle(alpha_new))
        checks["expired_slots_are_reusable_but_old_ids_stay_invalid"] = True


async def churn_checks(args, report, a, b):
    latencies = []
    async with server(args, "churn") as session:
        previous = set()
        for _ in range(20):
            started = time.monotonic()
            states = await asyncio.gather(connect(session, "alpha", a), connect(session, "beta", b))
            current = {item["connection_id"] for item in states}
            assert not current.intersection(previous)
            previous.update(current)
            await asyncio.gather(*(call(session, "vnc_disconnect", **handle(item)) for item in states))
            await until(lambda: not a.connections and not b.connections)
            assert (await call(session, "vnc_sessions"))["session_count"] == 0
            latencies.append(time.monotonic() - started)
    report["metrics"]["churn"] = {"cycles": len(latencies), "connections": len(previous),
                                  "mean_cycle_seconds": round(sum(latencies) / len(latencies), 4),
                                  "remaining_fixture_connections": len(a.connections) + len(b.connections)}
    report["checks"]["forty_connect_capture_disconnect_lifecycles_leave_no_transports"] = True


async def main_checks(args, report, fixtures):
    a, b, c, auth = fixtures
    await isolation_checks(args, report, a, b, c, auth)
    await idle_checks(args, report, a, b)
    await churn_checks(args, report, a, b)
    await registry_lifecycle_checks(args, report)


async def registry_lifecycle_checks(args, report):
    sys.path.insert(0, str(args.source / "src"))
    from vmware_vnc.sessions import SessionRegistry

    class Backend:
        action_timeout = 2

        def __init__(self):
            self.connection_id = None
            self.closing, self.release = threading.Event(), threading.Event()
            self.fail_close = False

        def execute(self, method, args, control, expected, pin):
            assert method == "connect"
            self.connection_id = str(id(self))
            return {"connected": True, "connection_id": self.connection_id}

        def disconnect(self):
            self.closing.set()
            assert self.release.wait(2)
            if self.fail_close:
                raise OSError("synthetic close failure")
            self.connection_id = None
            return {"connected": False, "connection_id": None}

    backends = []

    def factory():
        backend = Backend()
        backends.append(backend)
        return backend

    registry = SessionRegistry(factory, max_sessions=1, idle_timeout=0.01)
    await registry.run("connect", session_id="alpha", pin=False)
    await asyncio.sleep(0.02)
    reaper = asyncio.create_task(registry.reap_idle())
    try:
        assert await asyncio.to_thread(backends[0].closing.wait, 1)
        for name, expected in (("alpha", ConnectionError), ("beta", RuntimeError)):
            try:
                await registry.run("connect", session_id=name, pin=False)
            except expected:
                pass
            else:
                raise AssertionError("Closing capacity was reused before transport cleanup")
        assert len(backends) == 1
        backends[0].release.set()
        assert await reaper == 1 and registry.inventory()["session_count"] == 0
        report["checks"]["retiring_slot_cannot_be_reused_until_backend_cleanup_finishes"] = True
    finally:
        for backend in backends:
            backend.release.set()
        await reaper
        await registry.close()

    registry = SessionRegistry(factory, max_sessions=1, idle_timeout=0.01)
    await registry.run("connect", session_id="alpha", pin=False)
    bad = backends[-1]
    bad.release.set()
    bad.fail_close = True
    await asyncio.sleep(0.02)
    try:
        with redirect_stderr(StringIO()) as error_log:
            assert await registry.reap_idle() == 0
        assert "synthetic close failure" in error_log.getvalue()
        assert registry.inventory()["sessions"][0]["closing"]
        try:
            await registry.run("connect", session_id="beta", pin=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Failed cleanup must retain its capacity")
        report["checks"]["cleanup_failure_quarantines_capacity_instead_of_leaking_sessions"] = True
    finally:
        bad.fail_close = False
        await registry.close()
    assert registry.inventory()["session_count"] == 0

    class TransientStatusBackend:
        action_timeout = 2

        def __init__(self):
            self.connection_id = None
            self.status_started = threading.Event()
            self.status_release = threading.Event()

        def execute(self, method, args, control, expected, pin):
            if method == "status":
                self.status_started.set()
                assert self.status_release.wait(2)
                return {"connected": False, "connection_id": None}
            if method == "connect":
                self.connection_id = "persistent-default-connection"
                return {"connected": True, "connection_id": self.connection_id}
            raise AssertionError(f"unexpected synthetic backend method: {method}")

        def disconnect(self):
            self.connection_id = None
            return {"connected": False, "connection_id": None}

    transient_backends = []

    def transient_factory():
        backend = TransientStatusBackend()
        transient_backends.append(backend)
        return backend

    registry = SessionRegistry(transient_factory, max_sessions=1, idle_timeout=0)
    status_task = asyncio.create_task(registry.run("status", session_id="default", pin=False))
    try:
        await asyncio.sleep(0)
        assert transient_backends
        assert await asyncio.to_thread(transient_backends[0].status_started.wait, 1)
        assert registry.inventory()["session_count"] == 0
        connected = await registry.run("connect", session_id="default", pin=False)
        assert connected.connection_id == "persistent-default-connection"
        transient_backends[0].status_release.set()
        empty_status = await status_task
        assert empty_status.value["connected"] is False
        assert empty_status.session_id == "default" and empty_status.connection_id is None
        inventory = registry.inventory()
        assert inventory["session_count"] == 1
        assert registry._entries["default"].backend.connection_id == connected.connection_id
        report["checks"]["transient_status_unwind_preserves_concurrent_default_entry"] = True
    finally:
        for backend in transient_backends:
            backend.status_release.set()
        await asyncio.gather(status_task, return_exceptions=True)
        await registry.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--startup-child", type=int, nargs=2)
    args = parser.parse_args()
    args.source = args.source.resolve()
    if args.startup_child:
        startup_child(args)
        return
    args.output = args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "source": str(args.source),
              "scope": "Actual MCP stdio and synthetic RFB desktops only; no real VM input",
              "checks": {}, "metrics": {}, "success": False}
    fixtures = [Fixture("alpha desktop", 33), Fixture("beta desktop", 177),
                Fixture("legacy desktop", 84), Fixture("auth desktop", 244, password="test1234")]
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--source", str(args.source),
                   "--startup-child", str(fixtures[0].port), str(fixtures[1].port)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=12)
        assert result.returncode == 0, (result.stdout, result.stderr)
        report["metrics"]["concurrent_native_startup"] = json.loads(result.stdout)
        report["checks"]["concurrent_first_connections_share_one_io_thread_without_errors"] = True
        asyncio.run(main_checks(args, report, fixtures))
        errors = [error for fixture in fixtures for error in fixture.errors]
        assert not errors, errors
        report["success"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        for fixture in fixtures:
            fixture.close()
        report["fixture_errors"] = [error for fixture in fixtures for error in fixture.errors]
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
