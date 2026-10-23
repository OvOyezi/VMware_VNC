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

from vnc_regression import Fixture, payload


MCP_OBSERVER_DELAY_SECONDS = 0.1


def keys(fixture, code):
    return sum(event[0] == "key" and event[1] and event[2] == code for event in fixture.events)


def pointer_events(events, start):
    return [event for event in events[start:] if event[0] == "pointer"]


def wheel_pulses(events, start):
    masks = [event[1] for event in pointer_events(events, start)]
    first_down = next((index for index, mask in enumerate(masks) if mask & 16), None)
    return masks[first_down:] if first_down is not None else []


class EarlyWakeEvent:
    """Event substitute that sleeps, then spuriously reports an early wake."""

    def __init__(self):
        self.wait_calls = 0

    def is_set(self):
        return False

    def wait(self, timeout=None):
        self.wait_calls += 1
        if timeout is not None and timeout > 0:
            time.sleep(timeout / 2)
        return False


async def wait_for_events(fixture, predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = tuple(fixture.events)
        if predicate(events):
            # Exercise a late MCP consumer instead of requiring it to observe
            # intermediate states during an 80 ms input pulse.
            await asyncio.sleep(MCP_OBSERVER_DELAY_SECONDS)
            return tuple(fixture.events)
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for synthetic RFB input events")


def wait_for_events_sync(fixture, predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = tuple(fixture.events)
        if predicate(events):
            return events
        time.sleep(0.005)
    raise AssertionError("timed out waiting for synthetic RFB input events")


def record_input_timing(session, operation, *args, **kwargs):
    """Measure at the sender; the MCP observer may resume after a pulse ends."""
    timeline = []
    original_call = session._call

    def recorded_call(name, *values):
        timeline.append((name, values, time.monotonic()))
        return original_call(name, *values)

    with patch.object(session, "_call", side_effect=recorded_call):
        result = getattr(session, operation)(*args, **kwargs)
    return result, timeline


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
    metrics["mcp_observer_delay_seconds"] = MCP_OBSERVER_DELAY_SECONDS
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

                initial_connection = await connect(a)

                click_start = len(a.events)
                click_task = asyncio.create_task(session.call_tool(
                    "vnc_click", {"x": 22, "y": 18, "button": "left", "count": 2}
                ))
                click_result = payload(await click_task)
                click_final_events = await wait_for_events(
                    a, lambda events: len(pointer_events(events, click_start)) >= 5
                )
                click_sequence = pointer_events(click_final_events, click_start)
                assert click_sequence == [("pointer", mask, 22, 18) for mask in (0, 1, 0, 1, 0)]
                assert click_result == {
                    "clicked": "left", "count": 2, "x": 22, "y": 18,
                    "session_id": initial_connection["session_id"],
                    "connection_id": initial_connection["connection_id"],
                }, click_result
                checks["click_settles_pointer_and_doubleclicks_with_held_pulses"] = True

                mouse_down_start = len(a.events)
                mouse_down_task = asyncio.create_task(session.call_tool(
                    "vnc_mouse_down", {"x": 11, "y": 14, "button": "right"}
                ))
                payload(await mouse_down_task)
                mouse_down_events = await wait_for_events(a, lambda events: any(
                    event[1] & 4 for event in pointer_events(events, mouse_down_start)
                ))
                assert pointer_events(mouse_down_events, mouse_down_start) == [
                    ("pointer", 0, 11, 14), ("pointer", 4, 11, 14)
                ]
                before_duplicate_click = len(a.events)
                assert (await session.call_tool("vnc_click", {
                    "x": 11, "y": 14, "button": "right"
                })).isError
                assert (await call("vnc_status"))["connected"]
                assert len(a.events) == before_duplicate_click
                await call("vnc_mouse_up", x=11, y=14, button="right")

                def right_button_released(events):
                    pointers = pointer_events(events, mouse_down_start)
                    down_index = next((index for index, event in enumerate(pointers) if event[1] & 4), None)
                    return down_index is not None and any(
                        not (event[1] & 4) for event in pointers[down_index + 1:]
                    )

                mouse_up_events = await wait_for_events(a, right_button_released)
                assert mouse_up_events and (await call("vnc_status"))["connected"]
                checks["explicit_mouse_down_settles_and_keeps_held_button"] = True

                f10_start = len(a.events)
                f10_press = asyncio.create_task(session.call_tool("vnc_keypress", {"key": "f10"}))
                payload(await f10_press)
                f10_up_events = await wait_for_events(
                    a, lambda events: any(
                        event[0] == "key" and not event[1] and event[2] == 0xFFC7
                        for event in events[f10_start:]
                    )
                )
                f10_sequence = [event for event in f10_up_events[f10_start:]
                                if event[0] == "key" and event[2] == 0xFFC7]
                assert [event[1] for event in f10_sequence] == [True, False]
                checks["keypress_uses_held_down_up_pulse"] = True

                wheel_start = len(a.events)
                wheel_scroll = asyncio.create_task(session.call_tool(
                    "vnc_scroll", {"x": 20, "y": 20, "direction": "down", "amount": 2}
                ))
                payload(await wheel_scroll)
                wheel_final_events = await wait_for_events(
                    a, lambda events: len(wheel_pulses(events, wheel_start)) >= 4
                )
                assert pointer_events(wheel_final_events, wheel_start)[0] == ("pointer", 0, 20, 20)
                assert wheel_pulses(wheel_final_events, wheel_start) == [16, 0, 16, 0]
                checks["scroll_repeats_ordered_held_wheel_pulses"] = True

                paste_start = len(a.events)
                paste_task = asyncio.create_task(session.call_tool(
                    "vnc_type", {"text": "paste-pulse", "method": "clipboard", "paste_key": "ctrl-v"}
                ))

                def paste_key_counts(events, down):
                    return {
                        code: sum(event[0] == "key" and event[1] is down and event[2] == code
                                  for event in events[paste_start:])
                        for code in (0xFFE3, ord("v"))
                    }

                paste_result = payload(await paste_task)
                paste_up_events = await wait_for_events(
                    a, lambda events: all(count == 1 for count in paste_key_counts(events, False).values())
                )
                paste_sequence = [event for event in paste_up_events[paste_start:]
                                  if event[0] == "key" and event[2] in {0xFFE3, ord("v")}]
                assert sum(event[1] for event in paste_sequence) == 2
                assert sum(not event[1] for event in paste_sequence) == 2
                assert paste_result["paste_sent"] is True
                checks["send_text_paste_shortcut_uses_held_key_pulse"] = True

                held_start = len(a.events)
                await call("vnc_key_down", key="shift")
                await wait_for_events(a, lambda events: any(
                    event[0] == "key" and event[1] and event[2] == 0xFFE1
                    for event in events[held_start:]
                ))
                assert (await session.call_tool("vnc_keypress", {"key": "shift"})).isError
                assert (await call("vnc_status"))["connected"]
                assert not any(event[0] == "key" and not event[1] and event[2] == 0xFFE1
                               for event in a.events[held_start:])
                await call("vnc_key_up", key="shift")
                await wait_for_events(a, lambda events: any(
                    event[0] == "key" and not event[1] and event[2] == 0xFFE1
                    for event in events[held_start:]
                ))
                checks["keypress_preserves_overlapping_tracked_key"] = True

                wheel_cancel_start = len(a.events)
                wheel_request_id = session._request_id
                cancelled_wheel = asyncio.create_task(session.call_tool(
                    "vnc_scroll", {"x": 20, "y": 20, "direction": "down", "amount": 20}
                ))
                await wait_for_events(
                    a, lambda events: bool(wheel_pulses(events, wheel_cancel_start))
                )
                await cancel(session, wheel_request_id, cancelled_wheel)
                assert not (await call("vnc_status"))["connected"]
                cancelled_wheel_events = await wait_for_events(
                    a, lambda events: len(wheel_pulses(events, wheel_cancel_start)) >= 2
                    and wheel_pulses(events, wheel_cancel_start)[-1] == 0
                )
                cancelled_pulses = wheel_pulses(cancelled_wheel_events, wheel_cancel_start)
                assert 0 < len(cancelled_pulses) < 40
                assert cancelled_pulses == [16, 0] * (len(cancelled_pulses) // 2)
                current_connection = await connect(a)
                checks["cancelled_wheel_pulse_releases_and_clears_connection"] = True

                successful_drag_start = len(a.events)
                successful_drag = await call(
                    "vnc_drag", x1=8, y1=9, x2=48, y2=36, button="left"
                )
                successful_drag_events = await wait_for_events(a, lambda events: any(
                    event == ("pointer", 0, 48, 36)
                    for event in pointer_events(events, successful_drag_start)
                ))
                successful_drag_pointers = pointer_events(
                    successful_drag_events, successful_drag_start
                )
                assert successful_drag == {
                    "dragged": "left", "from": [8, 9], "to": [48, 36],
                    "session_id": current_connection["session_id"],
                    "connection_id": current_connection["connection_id"],
                }
                assert len(successful_drag_pointers) == 33
                assert successful_drag_pointers[0] == ("pointer", 0, 8, 9)
                assert successful_drag_pointers[1] == ("pointer", 1, 8, 9)
                assert all(event[1] & 1 for event in successful_drag_pointers[2:-1])
                assert successful_drag_pointers[-2] == ("pointer", 1, 48, 36)
                assert successful_drag_pointers[-1] == ("pointer", 0, 48, 36)
                checks["drag_interpolates_up_to_30_steps_and_releases_at_endpoint"] = True

                drag_cancel_start = len(a.events)
                drag_request_id = session._request_id
                cancelled_drag = asyncio.create_task(session.call_tool("vnc_drag", {
                    "x1": 8, "y1": 9, "x2": 48, "y2": 36, "button": "left"
                }))
                await wait_for_events(a, lambda events: any(
                    event[1] & 1 and event[2:] == (8, 9)
                    for event in pointer_events(events, drag_cancel_start)
                ))
                await cancel(session, drag_request_id, cancelled_drag)
                assert not (await call("vnc_status"))["connected"]

                def drag_released(events):
                    pointers = pointer_events(events, drag_cancel_start)
                    down_index = next((index for index, event in enumerate(pointers) if event[1] & 1), None)
                    return down_index is not None and any(
                        not (event[1] & 1) for event in pointers[down_index + 1:]
                    )

                released_drag_events = await wait_for_events(a, drag_released)
                drag_pointers = pointer_events(released_drag_events, drag_cancel_start)
                drag_down_index = next(index for index, event in enumerate(drag_pointers) if event[1] & 1)
                assert drag_pointers[drag_down_index][2:] == (8, 9)
                assert drag_released(released_drag_events)
                assert 1 <= sum(bool(event[1] & 1) for event in drag_pointers) <= 31
                assert drag_pointers[-1][1] == 0
                await connect(a)
                checks["cancelled_drag_releases_button_and_clears_connection"] = True

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
                typing = asyncio.create_task(session.call_tool("vnc_type", {"text": "a" * 20, "method": "keys", "delay_ms": 80}))
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
                assert (await session.call_tool("vnc_type", {"text": "x" * 30, "method": "keys", "delay_ms": 1000})).isError
                assert len(a.events) == before
                assert (await call("vnc_status"))["connected"]
                checks["impossible_delayed_action_rejected_before_input"] = True

                # A cancelled call waiting for serialization must never execute later.
                blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "b" * 6, "method": "keys", "delay_ms": 80}))
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
                blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "b" * 6, "method": "keys", "delay_ms": 80}))
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

                blocker = asyncio.create_task(session.call_tool("vnc_type", {"text": "b" * 8, "method": "keys", "delay_ms": 80}))
                await asyncio.sleep(0.04)
                replies = await asyncio.gather(*(session.call_tool("vnc_status") for _ in range(24)))
                payload(await blocker)
                rejected = sum(reply.isError for reply in replies)
                assert rejected > 0 and rejected < len(replies)
                assert (await call("vnc_status"))["connected"]
                metrics["queue_overload_rejections"] = rejected
                checks["queue_overload_bounded_and_recovers"] = True
                await call("vnc_disconnect")


def input_pacing_checks(session, report):
    checks, metrics = report["checks"], report["metrics"]
    _, click = record_input_timing(session, "click", 22, 18, count=2)
    assert [(name, values) for name, values, _ in click] == [
        ("mouse_move", (22, 18)), ("mouse_down", (1,)), ("mouse_up", (1,)),
        ("mouse_down", (1,)), ("mouse_up", (1,)),
    ]
    click_times = [timestamp for _, _, timestamp in click]
    settle = click_times[1] - click_times[0]
    holds = [click_times[2] - click_times[1], click_times[4] - click_times[3]]
    gap = click_times[3] - click_times[2]
    assert settle >= 0.12 and min(holds) >= 0.06 and gap >= 0.025, (settle, holds, gap)
    metrics["click_position_settle_seconds"] = round(settle, 4)
    metrics["click_pulse_hold_seconds"] = [round(value, 4) for value in holds]
    metrics["double_click_gap_seconds"] = round(gap, 4)

    _, mouse = record_input_timing(session, "mouse_down", 11, 14, button="right")
    assert [(name, values) for name, values, _ in mouse] == [
        ("mouse_move", (11, 14)), ("mouse_down", (3,)),
    ]
    settle = mouse[1][2] - mouse[0][2]
    assert settle >= 0.12, settle
    metrics["mouse_down_position_settle_seconds"] = round(settle, 4)
    session.mouse_up(11, 14, button="right")

    _, key = record_input_timing(session, "keypress", "f10")
    assert [(name, values) for name, values, _ in key] == [
        ("key_down", ("f10",)), ("key_up", ("f10",)),
    ]
    hold = key[1][2] - key[0][2]
    assert hold >= 0.06, hold
    metrics["keypress_hold_seconds"] = round(hold, 4)

    _, wheel = record_input_timing(session, "scroll", 20, 20, "down", 2)
    assert [(name, values) for name, values, _ in wheel] == [
        ("mouse_move", (20, 20)), ("mouse_down", (5,)), ("mouse_up", (5,)),
        ("mouse_down", (5,)), ("mouse_up", (5,)),
    ]
    wheel_times = [timestamp for _, _, timestamp in wheel]
    settle = wheel_times[1] - wheel_times[0]
    holds = [wheel_times[2] - wheel_times[1], wheel_times[4] - wheel_times[3]]
    gap = wheel_times[3] - wheel_times[2]
    assert settle >= 0.12 and min(holds) >= 0.06 and gap >= 0.025, (settle, holds, gap)
    metrics["wheel_position_settle_seconds"] = round(settle, 4)
    metrics["wheel_pulse_hold_seconds"] = [round(value, 4) for value in holds]
    metrics["wheel_inter_pulse_gap_seconds"] = round(gap, 4)

    result, paste = record_input_timing(session, "send_text", "paste-pulse", method="clipboard")
    paste_keys = [entry for entry in paste if entry[0] in {"key_down", "key_up"}]
    assert [(name, values) for name, values, _ in paste_keys] == [
        ("key_down", ("ctrl-v",)), ("key_up", ("ctrl-v",)),
    ]
    hold = paste_keys[1][2] - paste_keys[0][2]
    assert result["paste_sent"] and hold >= 0.06, (result, hold)
    metrics["paste_shortcut_hold_seconds"] = round(hold, 4)
    metrics["input_pacing_clock"] = "sender call boundary"
    checks["input_pacing_measured_independently_of_mcp_observer"] = True


def backend_checks(args, report, a, b):
    sys.path.insert(0, str(args.source / "src"))
    from vmware_vnc.client import VNCSession
    from vmware_vnc.operations import ActionCancelled, ActionControl, ActionTimedOut
    from vmware_vnc.cli import build_parser, _password
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
    assert "vmware_vnc.server" not in sys.modules
    checks["cli_noninteractive_password_and_lazy_server_import"] = True

    requested_wait = 0.08
    early_wake = EarlyWakeEvent()
    control = ActionControl(time.monotonic() + 1.0)
    control.cancelled = early_wake
    wait_started = time.monotonic()
    control.wait(requested_wait)
    wait_elapsed = time.monotonic() - wait_started
    assert wait_elapsed >= requested_wait - 0.005
    assert early_wake.wait_calls > 1
    metrics["early_wake_wait_elapsed_seconds"] = round(wait_elapsed, 4)
    checks["action_control_wait_survives_early_wakes"] = True

    early_wake = EarlyWakeEvent()
    control = ActionControl(time.monotonic() + 0.025)
    control.cancelled = early_wake
    try:
        control.wait(requested_wait)
        raise AssertionError("short action budget returned successfully after early wakes")
    except ActionTimedOut:
        pass
    assert early_wake.wait_calls > 1
    checks["action_control_early_wakes_still_enforce_deadline"] = True

    one, two = VNCSession(.5, "fresh"), VNCSession(.5, "fresh")
    one.connect("127.0.0.1", a.port)
    two.connect("127.0.0.1", b.port)

    input_pacing_checks(one, report)

    cancel_start = len(a.events)
    control = ActionControl(time.monotonic() + 5.0)
    original_call = one._call

    def cancel_after_pulse_down(name, *values):
        result = original_call(name, *values)
        if name == "key_down":
            control.cancelled.set()
        return result

    with patch.object(one, "_call", side_effect=cancel_after_pulse_down):
        try:
            one.execute("keypress", ("f9",), control, one.connection_id, True)
            raise AssertionError("cancelled key pulse was accepted")
        except ActionCancelled:
            pass
    cancelled_events = wait_for_events_sync(a, lambda events: any(
        event[0] == "key" and not event[1] and event[2] == 0xFFC6
        for event in events[cancel_start:]
    ))
    assert [event[1] for event in cancelled_events[cancel_start:]
            if event[0] == "key" and event[2] == 0xFFC6] == [True, False]
    assert not one.status()["connected"]
    checks["cancelled_key_pulse_releases_and_clears_connection"] = True
    one.connect("127.0.0.1", a.port)

    drag_start = len(a.events)
    control = ActionControl(time.monotonic() + 5.0)
    original_call = one._call

    def cancel_after_drag_down(name, *values):
        result = original_call(name, *values)
        if name == "mouse_down":
            control.cancelled.set()
        return result

    with patch.object(one, "_call", side_effect=cancel_after_drag_down):
        try:
            one.execute("drag", (8, 9, 48, 36), control, one.connection_id, True)
            raise AssertionError("cancelled drag was accepted")
        except ActionCancelled:
            pass
    drag_events = wait_for_events_sync(a, lambda events: any(
        event == ("pointer", 1, 8, 9) for event in events[drag_start:]
    ) and bool(pointer_events(events, drag_start))
        and pointer_events(events, drag_start)[-1][1] == 0)
    assert pointer_events(drag_events, drag_start) == [
        ("pointer", 0, 8, 9), ("pointer", 1, 8, 9), ("pointer", 0, 8, 9),
    ]
    assert not one.status()["connected"]
    checks["drag_cancelled_at_button_down_sends_no_interpolation"] = True
    one.connect("127.0.0.1", a.port)

    key_down_start = len(a.events)
    key_down_code = 0xFFC9  # F12
    control = ActionControl(time.monotonic() + 5.0)
    original_check = control.check
    check_calls = 0

    def cancel_after_wire_key_down():
        nonlocal check_calls
        check_calls += 1
        if check_calls == 3:
            wait_for_events_sync(a, lambda events: any(
                event[0] == "key" and event[1] and event[2] == key_down_code
                for event in events[key_down_start:]
            ))
            control.cancelled.set()
        original_check()

    control.check = cancel_after_wire_key_down
    key_down_errors = []

    def issue_held_key():
        try:
            one.execute("key_down", ("f12",), control, one.connection_id, True)
        except BaseException as exc:
            key_down_errors.append(exc)

    key_down_worker = threading.Thread(target=issue_held_key, name="cancelled-explicit-key-down")
    key_down_worker.start()
    try:
        key_down_events = wait_for_events_sync(a, lambda events: [
            event[1] for event in events[key_down_start:]
            if event[0] == "key" and event[2] == key_down_code
        ] == [True, False])
        key_down_worker.join(2)
        assert not key_down_worker.is_alive()
        key_down_sequence = [event[1] for event in key_down_events[key_down_start:]
                             if event[0] == "key" and event[2] == key_down_code]
        assert key_down_sequence == [True, False]
        assert len(key_down_errors) == 1 and isinstance(key_down_errors[0], ActionCancelled)
        assert not one.status()["connected"]
        checks["cancelled_explicit_key_down_releases_and_clears_connection"] = True
    finally:
        control.cancelled.set()
        key_down_worker.join(2)
        if one.status()["connected"]:
            one.disconnect()

    one.connect("127.0.0.1", a.port)
    key_start = len(a.events)
    try:
        one.execute("keypress", ("f10",), ActionControl(time.monotonic() + 0.045), one.connection_id, True)
        raise AssertionError("short key pulse budget was not enforced")
    except ActionTimedOut:
        pass
    wait_for_events_sync(
        a, lambda events: [event[1] for event in events[key_start:]
                           if event[0] == "key" and event[2] == 0xFFC7] == [True, False]
    )
    assert not one.status()["connected"]
    checks["short_key_pulse_budget_releases_and_disconnects"] = True

    one.connect("127.0.0.1", a.port)
    wheel_start = len(a.events)
    try:
        one.execute("scroll", (20, 20, "down", 2), ActionControl(time.monotonic() + 0.21),
                    one.connection_id, True)
        raise AssertionError("short wheel pulse budget was not enforced")
    except ActionTimedOut:
        pass
    wait_for_events_sync(
        a, lambda events: wheel_pulses(events, wheel_start) == [16, 0]
    )
    assert not one.status()["connected"]
    checks["short_wheel_pulse_budget_releases_and_disconnects"] = True

    one.connect("127.0.0.1", a.port)
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
    checks["encoding_outside_io_loop_keeps_other_connections_responsive"] = True
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
    timing_key_count = keys(a, 100)
    timing_event_count = len(a.key_event_times)
    typing_result = candidate.type_text("d" * 4)
    wait_for_events_sync(a, lambda events: keys(a, 100) >= timing_key_count + 4)
    down_times = [timestamp for down, key, timestamp in a.key_event_times[timing_event_count:]
                  if down and key == 100]
    key_gaps = [later - earlier for earlier, later in zip(down_times, down_times[1:])]
    assert typing_result["effective_key_delay_ms"] == 20
    assert len(down_times) == 4 and min(key_gaps) >= 0.019
    metrics["default_key_event_minimum_gap_seconds"] = round(min(key_gaps), 4)
    checks["default_key_typing_spaces_local_rfb_events"] = True
    candidate.disconnect()

    short_budget = VNCSession(.5, "fresh", action_timeout=0.03)
    short_budget.connect("127.0.0.1", a.port)
    before_preflight = len(a.events)
    try:
        short_budget.send_text("abc", method="keys", focus_x=5, focus_y=5)
        raise AssertionError("minimum typing gaps were not preflighted")
    except ValueError as exc:
        assert "action time budget" in str(exc)
    assert len(a.events) == before_preflight and short_budget.status()["connected"]
    short_budget.disconnect()
    checks["minimum_typing_gap_budget_rejects_before_focus_or_key_input"] = True

    candidate = VNCSession(.5, "fresh")
    candidate.connect("127.0.0.1", a.port)
    durations = []
    chunk_size = 100
    for _ in range(5):
        count = keys(a, 100)
        tick = time.monotonic()
        sent = 0
        for start in range(0, 2000, chunk_size):
            chunk_length = min(chunk_size, 2000 - start)
            result = candidate.type_text("d" * chunk_length)
            assert result == {
                "typed_characters": chunk_length,
                "effective_key_delay_ms": 20,
            }
            sent += chunk_length
            wait_for_events_sync(a, lambda events, expected=count + sent: keys(a, 100) >= expected)
            assert keys(a, 100) == count + sent
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
