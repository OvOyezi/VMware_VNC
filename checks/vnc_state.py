"""Wire-level checks for framebuffer state, held keys, and runtime cleanup."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import struct
import sys
import threading
import time
import traceback
from typing import Any, Callable


CHECK_NAMES = (
    "initial_framebuffer_stays_private_until_split_update_is_complete",
    "capture_collects_split_resize_and_preserves_old_snapshot",
    "copyrect_accepts_initialized_and_rejects_uninitialized_sources",
    "held_chord_overlap_is_rejected_and_alias_release_is_exact",
    "invalid_coordinates_counts_and_delays_send_no_input_and_recover",
    "runtime_releases_after_close_failure_and_repeated_cycles",
    "fresh_resize_uses_observed_geometry_and_invalidates_later_notifications",
)


def _run_in_thread(call: Callable[[], Any], name: str) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def invoke() -> None:
        try:
            outcome["result"] = call()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            outcome["done"] = True

    thread = threading.Thread(target=invoke, name=name, daemon=True)
    thread.start()
    return thread, outcome


def _finish_thread(thread: threading.Thread, outcome: dict[str, Any], timeout: float = 4.0) -> Any:
    thread.join(timeout)
    assert not thread.is_alive(), f"{thread.name} did not finish"
    if "error" in outcome:
        raise outcome["error"]
    assert outcome.get("done") is True
    return outcome.get("result")


def _events(server: Any, message_type: int | None = None) -> list[dict[str, Any]]:
    with server.lock:
        result = list(server.events)
    return result if message_type is None else [event for event in result if event.get("type") == message_type]


def _wait_request_count(server: Any, count: int, timeout: float = 2.0) -> list[dict[str, Any]]:
    return server.wait_for(
        lambda events: requests if len(requests := [event for event in events if event.get("type") == 3]) >= count else None,
        timeout=timeout,
    )


def _wait_frame_sequence(client: Any, sequence: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.frame_sequence >= sequence:
            return
        time.sleep(0.01)
    raise TimeoutError("RFB frame sequence did not advance")


def _assert_runtime_stopped(runtime_type: Any) -> None:
    info = runtime_type.info()
    assert info["lease_count"] == 0, info
    assert info["io_threads"] == 0, info


def _check_initial_split(native: Any, session_type: Any, runtime_type: Any) -> None:
    colors = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]
    session = session_type(timeout=2.5, action_timeout=2.5)
    with native.NativeRfbFixture(width=2, height=2, automatic_initial_update=False) as server:
        thread, outcome = _run_in_thread(
            lambda: session.connect("127.0.0.1", server.port, password=None, url=None),
            "vnc-split-initial-connect",
        )
        try:
            _wait_request_count(server, 1)
            server.send_update([(0, 0, 2, 1, colors[:2])])
            _wait_request_count(server, 2)
            assert thread.is_alive(), "connect returned with only half of the initial framebuffer"
            assert session.client is None, "session exposed a client before the initial image was complete"

            server.send_update([(0, 1, 2, 1, colors[2:])])
            result = _finish_thread(thread, outcome)
            assert result["connected"] is True
            client = session.client
            assert client is not None
            snapshot = client.initial_snapshot()
            assert snapshot.sequence == 1
            assert snapshot.image.size == (2, 2)
            assert [snapshot.image.getpixel(point) for point in ((0, 0), (1, 0), (0, 1), (1, 1))] == colors
        finally:
            if thread.is_alive():
                server.drop()
                thread.join(3)
            if session.client is not None:
                session.disconnect()
    _assert_runtime_stopped(runtime_type)


def _check_capture_resize(native: Any, session_type: Any, runtime_type: Any) -> None:
    baseline = [(11, 21, 31), (41, 51, 61), (71, 81, 91), (101, 111, 121)]
    left_half = [(20, 30, 40), (50, 60, 70), (80, 90, 100), (110, 120, 130)]
    right_half = [(140, 150, 160), (170, 180, 190), (200, 210, 220), (230, 240, 250)]

    session = session_type(timeout=2.5, action_timeout=2.5)
    with native.NativeRfbFixture(width=2, height=2) as server:
        session.connect("127.0.0.1", server.port, password=None, url=None)
        client = session.client
        assert client is not None
        _wait_request_count(server, 1)

        capture_thread, capture_outcome = _run_in_thread(
            lambda: client.capture(region=None, timeout=2.0, incremental=False),
            "vnc-capture-baseline",
        )
        try:
            _wait_request_count(server, 2)
            server.send_update([(0, 0, 2, 2, baseline)])
            old_snapshot = _finish_thread(capture_thread, capture_outcome)
            old_image_bytes = old_snapshot.image.tobytes()
            assert old_snapshot.image.size == (2, 2)

            resize_thread, resize_outcome = _run_in_thread(
                lambda: client.capture(region=None, timeout=2.0, incremental=False),
                "vnc-capture-resize",
            )
            _wait_request_count(server, 3)
            desktop_size = native._framebuffer_update([
                native._rect(0, 0, 4, 2, None, encoding=-223),
            ])
            server.send_wire(desktop_size)
            _wait_request_count(server, 4)
            assert resize_thread.is_alive(), "capture returned during an uninitialized resize"
            assert client.screen is not None and client.screen.size == (2, 2)
            assert client.screen.tobytes() == old_image_bytes
            assert old_snapshot.image.tobytes() == old_image_bytes

            server.send_update([(0, 0, 2, 2, left_half)])
            _wait_request_count(server, 5)
            assert resize_thread.is_alive(), "capture returned after only the first resized half"
            assert client.screen is not None and client.screen.size == (2, 2)
            assert client.screen.tobytes() == old_image_bytes

            server.send_update([(2, 0, 2, 2, right_half)])
            resized_snapshot = _finish_thread(resize_thread, resize_outcome)
            assert resized_snapshot.sequence == old_snapshot.sequence + 1
            assert resized_snapshot.image.size == (4, 2)
            expected = {
                (0, 0): left_half[0], (1, 0): left_half[1],
                (0, 1): left_half[2], (1, 1): left_half[3],
                (2, 0): right_half[0], (3, 0): right_half[1],
                (2, 1): right_half[2], (3, 1): right_half[3],
            }
            assert {point: resized_snapshot.image.getpixel(point) for point in expected} == expected
            assert old_snapshot.image.size == (2, 2)
            assert old_snapshot.image.tobytes() == old_image_bytes, "published snapshot changed after resize"
        finally:
            if capture_thread.is_alive():
                server.drop()
                capture_thread.join(3)
            if "resize_thread" in locals() and resize_thread.is_alive():
                server.drop()
                resize_thread.join(3)
            if session.client is not None:
                session.disconnect()
    _assert_runtime_stopped(runtime_type)


def _check_copyrect(native: Any, connection_type: Any, runtime_type: Any) -> None:
    with native.NativeRfbFixture(width=2, height=2) as server:
        client = connection_type("127.0.0.1", server.port, None, 2.0)
        try:
            before = client.initial_snapshot()
            expected_source = before.image.getpixel((0, 0))
            copy_known = native._framebuffer_update([
                native._rect(1, 0, 1, 1, struct.pack("!HH", 0, 0), encoding=1),
            ])
            server.send_wire(copy_known)
            _wait_frame_sequence(client, before.sequence + 1)
            copied = client.screen
            assert copied is not None and copied.getpixel((1, 0)) == expected_source

            desktop_and_known_column = native._framebuffer_update([
                native._rect(0, 0, 3, 2, None, encoding=-223),
                native._rect(0, 0, 1, 2,
                             native._wire_pixel((25, 35, 45)) + native._wire_pixel((55, 65, 75))),
            ])
            server.send_wire(desktop_and_known_column)
            _wait_request_count(server, 2)
            before_invalid = client.frame_sequence
            copy_unknown = native._framebuffer_update([
                native._rect(2, 0, 1, 1, struct.pack("!HH", 1, 0), encoding=1),
            ])
            server.send_wire(copy_unknown)
            assert client.closed.wait(2), "CopyRect accepted a source pixel that was not initialized after resize"
            assert client.frame_sequence == 0 or client.frame_sequence == before_invalid
            assert client.protocol_error is not None and "uninitialized" in client.protocol_error.lower()
        finally:
            client.close()
    _assert_runtime_stopped(runtime_type)


def _check_keys_and_inputs(native: Any, session_type: Any, runtime_type: Any) -> None:
    session = session_type(timeout=2.0, action_timeout=3.0)
    with native.NativeRfbFixture(width=2, height=2) as server:
        try:
            session.connect("127.0.0.1", server.port, password=None, url=None)
            client = session.client
            assert client is not None
            initial_key_count = len(_events(server, 4))

            session.key_down("ctrl+a")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= initial_key_count + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(1, 0xFFE3), (1, 0x61)]

            held_count = len(keys)
            for operation in (lambda: session.key_down("ctrl"), lambda: session.key_up("ctrl")):
                try:
                    operation()
                except ValueError:
                    pass
                else:
                    raise AssertionError("partial held-chord operation was accepted")
            assert len(_events(server, 4)) == held_count, "overlap or partial release emitted a key event"
            session.key_up("ctrl+a")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= held_count + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(0, 0x61), (0, 0xFFE3)]

            held_count = len(keys)
            session.key_down("ctrl+backspace")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= held_count + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(1, 0xFFE3), (1, 0xFF08)]
            held_count = len(keys)
            session.key_up("ctrl+bsp")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= held_count + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(0, 0xFF08), (0, 0xFFE3)]

            held_count = len(keys)
            session.key_down("A")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= held_count + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(1, 0xFFE1), (1, 0x41)]
            held_count = len(keys)
            for operation in (lambda: session.key_down("shift"), lambda: session.key_up("shift")):
                try:
                    operation()
                except ValueError:
                    pass
                else:
                    raise AssertionError("implicit Shift overlap was accepted")
            assert len(_events(server, 4)) == held_count, "implicit Shift overlap emitted a key event"
            session.key_up("A")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= held_count + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(0, 0x41), (0, 0xFFE1)]

            keypress_start = len(keys)
            client.key_press("b")
            events = server.wait_for(
                lambda values: keys if len(keys := [event for event in values if event.get("type") == 4]) >= keypress_start + 2 else None
            )
            keys = [event for event in events if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in keys[-2:]] == [(1, 0x62), (0, 0x62)]

            client.mouse_move(1, 0)
            client.mouse_down(1)
            client.mouse_up(1)
            pointer_events = server.wait_for(
                lambda values: pointers if len(pointers := [event for event in values if event.get("type") == 5]) >= 3 else None
            )
            pointers = [event for event in pointer_events if event.get("type") == 5]
            assert [(event["buttons"], event["x"], event["y"]) for event in pointers[-3:]] == [
                (0, 1, 0), (1, 1, 0), (0, 1, 0),
            ]
        finally:
            if session.client is not None:
                session.disconnect()
    _assert_runtime_stopped(runtime_type)


def _check_invalid_inputs(native: Any, session_type: Any, runtime_type: Any) -> None:
    session = session_type(timeout=2.0, action_timeout=3.0)
    with native.NativeRfbFixture(width=2, height=2) as server:
        try:
            session.connect("127.0.0.1", server.port, password=None, url=None)
            before_keys = len(_events(server, 4))
            before_pointers = len(_events(server, 5))
            invalid_calls = (
                lambda: session.move(True, 0),
                lambda: session.move(0.5, 0),
                lambda: session.click(0, 0, count=True),
                lambda: session.click(0, 0, count=1.5),
                lambda: session.type_text("x", delay_ms=True),
                lambda: session.type_text("x", delay_ms=1.5),
                lambda: session.keypress("\t"),
                lambda: session.type_text("prefix\x01"),
                lambda: session.send_text("prefix\x01", method="keys", focus_x=0, focus_y=0),
                lambda: session.capture("png", True),
            )
            for call in invalid_calls:
                try:
                    call()
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid input argument was accepted")
            assert len(_events(server, 4)) == before_keys
            assert len(_events(server, 5)) == before_pointers
            assert session.status()["connected"] is True, "invalid input disconnected the usable session"

            session.move(1, 1)
            moved = server.wait_for(lambda values: next(
                (event for event in values if event.get("type") == 5 and event.get("x") == 1 and event.get("y") == 1), None
            ))
            assert moved["buttons"] == 0
        finally:
            if session.client is not None:
                session.disconnect()
    _assert_runtime_stopped(runtime_type)


def _check_runtime_cleanup(native: Any, connection_type: Any, runtime_type: Any) -> None:
    for cycle in range(3):
        with native.NativeRfbFixture(width=2, height=2) as server:
            client = connection_type("127.0.0.1", server.port, None, 2.0)
            assert client.frame_sequence == 1
            client.close()
        _assert_runtime_stopped(runtime_type)

    for cycle in range(3):
        with native.NativeRfbFixture(width=2, height=2, security_types=(3,)) as server:
            try:
                connection_type("127.0.0.1", server.port, None, 2.0)
            except ValueError:
                pass
            else:
                raise AssertionError("connection accepted an unsupported security type")
        _assert_runtime_stopped(runtime_type)


def _check_fresh_geometry(native: Any, session_type: Any, runtime_type: Any) -> None:
    session = session_type(timeout=2.0, screenshot_mode="fresh")
    with native.NativeRfbFixture(width=2, height=2) as server:
        try:
            session.connect("127.0.0.1", server.port)
            primary = session.client
            primary_socket = server.connection
            assert primary is not None and primary_socket is not None
            assert (primary.width, primary.height) == (2, 2)

            # VMware-style fallback: only the new screenshot handshake learns
            # the changed dimensions; the control socket has no resize message.
            server.width = 3
            server.initial_rectangles = [(0, 0, 3, 2, [(23, 67, 149)] * 6)]
            server.initial_sent = False
            image = session.capture()
            assert (image.desktop_width, image.desktop_height) == (3, 2)
            assert (primary.width, primary.height) == (2, 2)
            session.move(2, 1)
            server.wait_for(lambda events: next((event for event in events
                if event.get("type") == 5 and event.get("x") == 2 and event.get("y") == 1), None))
            before = len(_events(server, 5))
            try:
                session.move(3, 1)
            except ValueError:
                pass
            else:
                raise AssertionError("input outside the newly observed desktop was accepted")
            assert len(_events(server, 5)) == before

            # A resize notification after that capture invalidates the observed
            # geometry until a new full screenshot has been obtained.
            primary_socket.sendall(native._framebuffer_update([
                native._rect(0, 0, 4, 2, None, encoding=-223),
            ]))
            native._wait_until(lambda: primary.width == 4)
            try:
                session.move(1, 1)
            except ValueError as exc:
                assert "screenshot" in str(exc)
            else:
                raise AssertionError("input ignored a later geometry notification")
            assert len(_events(server, 5)) == before
            server.width = 4
            server.initial_rectangles = [(0, 0, 4, 2, [(91, 128, 203)] * 8)]
            server.initial_sent = False
            image = session.capture()
            assert (image.desktop_width, image.desktop_height) == (4, 2)
            session.move(3, 1)
            server.wait_for(lambda events: next((event for event in events
                if event.get("type") == 5 and event.get("x") == 3 and event.get("y") == 1), None))
        finally:
            session.disconnect()
    _assert_runtime_stopped(runtime_type)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    source = args.source.resolve()
    sys.path.insert(0, str(source / "src"))
    sys.path.insert(0, str(source / "checks"))
    report: dict[str, Any] = {"success": False, "checks": {name: False for name in CHECK_NAMES}}
    checks = report["checks"]

    try:
        native = importlib.import_module("vnc_native_rfb")
        rfb = importlib.import_module("vmware_vnc.rfb")
        client = importlib.import_module("vmware_vnc.client")
        runtime = importlib.import_module("vmware_vnc.runtime").RfbRuntime

        _check_initial_split(native, client.VNCSession, runtime)
        checks[CHECK_NAMES[0]] = True
        _check_capture_resize(native, client.VNCSession, runtime)
        checks[CHECK_NAMES[1]] = True
        _check_copyrect(native, rfb.RFBConnection, runtime)
        checks[CHECK_NAMES[2]] = True
        _check_keys_and_inputs(native, client.VNCSession, runtime)
        checks[CHECK_NAMES[3]] = True
        _check_invalid_inputs(native, client.VNCSession, runtime)
        checks[CHECK_NAMES[4]] = True
        _check_runtime_cleanup(native, rfb.RFBConnection, runtime)
        checks[CHECK_NAMES[5]] = True
        _check_fresh_geometry(native, client.VNCSession, runtime)
        checks[CHECK_NAMES[6]] = True
        report["success"] = all(checks.values())
    except BaseException as exc:
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        report["success"] = False

    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
