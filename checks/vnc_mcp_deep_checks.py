"""Cancellation, queuing, rendering and auth checks against synthetic desktops."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import traceback
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from PIL import Image

from vnc_mcp_regression import Fixture, payload


def keys(fixture, code):
    return sum(event[0] == "key" and event[1] and event[2] == code for event in fixture.events)


async def cancel(session, request_id, task):
    await session.send_notification(types.ClientNotification(types.CancelledNotification(
        params=types.CancelledNotificationParams(requestId=request_id, reason="synthetic cancellation check"),
    )))
    await asyncio.sleep(0.05)
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def mcp_checks(args, report, a, b, auth):
    env = dict(os.environ)
    for name in ("VNC_HOST", "VNC_PORT", "VNC_PASSWORD"):
        env.pop(name, None)
    env.update(VNC_TIMEOUT="0.5", VNC_SCREENSHOT_MODE="fresh")
    params = StdioServerParameters(command=sys.executable, args=[str(args.source / "main.py"), "mcp"], cwd=str(args.source), env=env)
    checks, metrics = report["checks"], report["metrics"]
    with args.output.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()

                async def call(name, **arguments):
                    return payload(await session.call_tool(name, arguments))

                async def connect(fixture, password=None):
                    arguments = {"host": "127.0.0.1", "port": fixture.port}
                    if password is not None:
                        arguments["password"] = password
                    state = await call("vnc_connect", **arguments)
                    assert state["name"] == fixture.name and state["connection_id"]
                    assert not (await session.call_tool("vnc_screenshot")).isError
                    return state

                await connect(a)
                waiter = asyncio.create_task(session.call_tool("vnc_wait", {"seconds": 1.0}))
                await asyncio.sleep(0.05)
                started = time.monotonic()
                await call("vnc_status")
                metrics["status_latency_during_wait_seconds"] = round(time.monotonic() - started, 4)
                assert metrics["status_latency_during_wait_seconds"] < 0.25
                await waiter
                checks["wait_does_not_hold_connection_or_worker"] = True

                request_id = session._request_id  # pinned MCP SDK request sequence
                before = keys(a, 97)
                typing = asyncio.create_task(session.call_tool("vnc_type", {"text": "a" * 20, "delay_ms": 80}))
                await asyncio.sleep(0.18)
                at_cancel = keys(a, 97) - before
                started = time.monotonic()
                await cancel(session, request_id, typing)
                await call("vnc_status")  # drain the actual worker, not only the client task
                metrics["cancel_and_drain_seconds"] = round(time.monotonic() - started, 4)
                await asyncio.sleep(0.35)
                final = keys(a, 97) - before
                metrics["cancelled_typing"] = {"before_cancel": at_cancel, "final": final, "requested": 20}
                assert final <= at_cancel + 1 and final < 20
                assert not (await call("vnc_status"))["connected"]
                checks["mcp_cancel_stops_real_worker_and_clears_connection"] = True

                await connect(a)
                before = len(a.events)
                assert (await session.call_tool("vnc_type", {"text": "x" * 30, "delay_ms": 1000})).isError
                assert len(a.events) == before
                assert (await call("vnc_status"))["connected"]
                checks["impossible_delayed_action_rejected_before_input"] = True

                # A cancelled call waiting for serialization must never execute later.
                blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "b" * 6, "delay_ms": 80}))
                await asyncio.sleep(0.04)
                queued_id = session._request_id
                enter_before = keys(a, 0xFF0D)
                queued = asyncio.create_task(session.call_tool("vnc_keypress", {"key": "enter"}))
                await asyncio.sleep(0.04)
                await cancel(session, queued_id, queued)
                payload(await blocker)
                await call("vnc_status")
                assert keys(a, 0xFF0D) == enter_before
                checks["cancelled_queued_input_never_runs"] = True

                old_id = (await call("vnc_status"))["connection_id"]
                blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "b" * 6, "delay_ms": 80}))
                await asyncio.sleep(0.04)
                switch = asyncio.create_task(session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": b.port}))
                await asyncio.sleep(0.04)
                stale = asyncio.create_task(session.call_tool("vnc_keypress", {"key": "c"}))
                payload(await blocker)
                new_state = payload(await switch)
                assert new_state["connection_id"] != old_id
                assert (await stale).isError and keys(b, 99) == 0
                assert not (await session.call_tool("vnc_screenshot")).isError
                assert (await session.call_tool("vnc_keypress", {"key": "c", "connection_id": old_id})).isError
                assert keys(b, 99) == 0
                checks["queued_and_explicit_stale_connection_ids_rejected"] = True

                region = {"x": 13, "y": 17, "width": 19, "height": 12}
                result = await session.call_tool("vnc_screenshot", {"image_format": "png", "region": region})
                assert not result.isError
                metadata = json.loads(next(item.text for item in result.content if item.type == "text"))
                content = next(item for item in result.content if item.type == "image")
                image = Image.open(BytesIO(base64.b64decode(content.data)))
                assert image.size == (19, 12) and image.getpixel((0, 0)) == b.rgb(13, 17)
                assert metadata["desktop_width"] == 64 and metadata["origin_x"] == 13 and metadata["origin_y"] == 17
                await call("vnc_click", x=20, y=18, connection_id=new_state["connection_id"])
                await asyncio.sleep(0.03)
                assert ("pointer", 1, 20, 18) in b.events
                checks["region_exact_pixels_and_native_coordinate_origin"] = True
                invalid = {"x": 63, "y": 0, "width": 2, "height": 1}
                assert (await session.call_tool("vnc_screenshot", {"region": invalid})).isError
                assert (await call("vnc_status"))["connected"]
                checks["invalid_region_does_not_poison_control"] = True

                waiter = asyncio.create_task(session.call_tool("vnc_wait", {"seconds": 0.3}))
                await asyncio.sleep(0.04)
                started = time.monotonic()
                await call("vnc_disconnect")
                metrics["disconnect_during_wait_seconds"] = round(time.monotonic() - started, 4)
                assert metrics["disconnect_during_wait_seconds"] < 0.2 and (await waiter).isError
                checks["wait_detects_target_disconnection"] = True

                assert (await session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": auth.port, "password": "wrong"})).isError
                assert not (await call("vnc_status"))["connected"]
                await connect(auth, "test1234")
                assert not (await session.call_tool("vnc_screenshot")).isError
                checks["vnc_auth_rejection_and_fresh_snapshot_password_reuse"] = True

                blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "b" * 8, "delay_ms": 80}))
                await asyncio.sleep(0.04)
                replies = await asyncio.gather(*(session.call_tool("vnc_status") for _ in range(24)))
                payload(await blocker)
                rejected = sum(reply.isError for reply in replies)
                assert rejected > 0 and rejected < len(replies)
                assert (await call("vnc_status"))["connected"]
                metrics["queue_overload_rejections"] = rejected
                checks["queue_overload_bounded_and_recovers"] = True
                await call("vnc_disconnect")


def backend_checks(args, report, a, b):
    sys.path.insert(0, str(args.source / "src"))
    from vnc_mcp.client import VNCSession
    from vnc_mcp.operations import ActionControl, ActionTimedOut
    from vnc_mcp.cli import build_parser, _password
    checks, metrics = report["checks"], report["metrics"]
    for value in (-1, 0, float("inf"), float("nan")):
        try:
            VNCSession(timeout=value)
            raise AssertionError("Invalid timeout accepted")
        except ValueError:
            pass
    checks["invalid_timeouts_rejected"] = True
    parser = build_parser()
    with redirect_stderr(StringIO()):
        try:
            parser.parse_args(["screenshot", "--host", "127.0.0.1"])
            raise AssertionError("CLI accepted an implicit target port")
        except SystemExit as exc:
            assert exc.code == 2
    parsed = parser.parse_args(["screenshot", "--host", "127.0.0.1", "--port", "5901", "--screenshot-mode", "fresh", "--region", "1", "2", "3", "4"])
    assert parsed.screenshot_mode == "fresh" and parsed.region == [1, 2, 3, 4]
    checks["cli_explicit_target_and_fresh_region_options"] = True
    with patch.dict(os.environ, {}, clear=True), patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", side_effect=AssertionError("unexpected password prompt")):
        assert _password("VNC_PASSWORD") is None
    assert "vnc_mcp.server" not in sys.modules
    checks["cli_noninteractive_password_and_lazy_server_import"] = True
    one, two = VNCSession(.5, "fresh"), VNCSession(.5, "fresh")
    one.connect("127.0.0.1", a.port)
    two.connect("127.0.0.1", b.port)
    save = Image.Image.save
    threads = []
    started, release = threading.Event(), threading.Event()
    errors = []

    def delayed_save(image, *pos, **kw):
        threads.append(threading.current_thread().name)
        started.set()
        assert release.wait(2)
        return save(image, *pos, **kw)

    def capture():
        try:
            one.capture("jpeg")
        except BaseException:
            errors.append(traceback.format_exc())

    with patch.object(Image.Image, "save", delayed_save):
        worker = threading.Thread(target=capture, name="encoding-worker")
        worker.start()
        assert started.wait(2)
        tick = time.monotonic()
        two.keypress("a")
        metrics["independent_keypress_during_slow_encode_seconds"] = round(time.monotonic() - tick, 4)
        release.set()
        worker.join(2)
    assert not errors and threads == ["encoding-worker"]
    assert metrics["independent_keypress_during_slow_encode_seconds"] < 0.15
    metrics["image_encoding_threads"] = threads
    checks["encoding_outside_reactor_keeps_other_connections_responsive"] = True
    before = keys(a, 120)
    control = ActionControl(time.monotonic() + 0.075)
    try:
        one.execute("type_text", ("x" * 20, 20), control, one.connection_id, True)
        raise AssertionError("Action exceeded deadline without failing")
    except ActionTimedOut:
        pass
    time.sleep(0.04)
    assert 0 < keys(a, 120) - before < 20 and not one.status()["connected"]
    checks["actual_action_deadline_stops_partial_input"] = True
    two.disconnect()

    candidate = VNCSession(.5, "fresh")
    candidate.connect("127.0.0.1", a.port)
    durations = []
    for _ in range(5):
        count = keys(a, 100)
        tick = time.monotonic()
        candidate.type_text("d" * 2000)
        deadline = time.monotonic() + 2
        while keys(a, 100) < count + 2000:
            assert time.monotonic() < deadline
            time.sleep(.001)
        received = keys(a, 100) - count
        assert received == 2000
        durations.append(time.monotonic() - tick)
    candidate.disconnect()
    metrics["typing_2000_wire_complete_median_seconds"] = round(statistics.median(durations), 4)
    checks["current_client_delivers_five_complete_2000_character_transfers"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "scope": "Synthetic desktops only; no real desktop input", "checks": {}, "metrics": {}, "success": False}
    fixtures = [Fixture("deep A", 73), Fixture("deep B", 193), Fixture("deep auth", 109, "test1234")]
    try:
        asyncio.run(asyncio.wait_for(mcp_checks(args, report, *fixtures), timeout=25))
        backend_checks(args, report, *fixtures[:2])
        assert not any(fixture.errors for fixture in fixtures)
        report["success"] = True
    except BaseException:
        report["failure"] = traceback.format_exc()
    finally:
        for fixture in fixtures:
            fixture.close()
        report["fixture_errors"] = [error for fixture in fixtures for error in fixture.errors]
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
