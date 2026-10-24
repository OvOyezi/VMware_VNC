"""Boundary regressions for unavailable VNC displays and explicit recovery."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable


CHECK_NAMES = (
    "automatic_text_refuses_without_channel_and_keys_are_marked_sensitive",
    "automatic_text_delay_refuses_before_any_input",
    "zero_size_server_init_is_connected_but_unavailable",
    "wake_sends_only_pointer_motion_and_rejects_held_input",
    "zero_size_connection_recovers_from_split_raw_resize",
    "zero_resize_invalidates_cached_inspection_frame",
    "capture_waiter_fails_fast_on_zero_resize_and_recovers",
    "positive_server_init_without_complete_frame_stays_connected_and_recovers",
    "capture_timeout_invalidates_old_visuals_and_recovers",
    "mcp_stdio_wake_requires_named_connection_id_and_is_pointer_only",
    "cli_wake_reports_attempt_and_releases_runtime",
)


_native: Any = None
_client_module: Any = None
_rfb_module: Any = None
_runtime_type: Any = None


def _events(server: Any, message_type: int | None = None) -> list[dict[str, Any]]:
    with server.lock:
        values = list(server.events)
    return values if message_type is None else [event for event in values if event.get("type") == message_type]


def _expect_raises(call: Callable[[], Any], error_type: type[BaseException], label: str) -> BaseException:
    try:
        call()
    except error_type as exc:
        return exc
    except BaseException as exc:
        raise AssertionError(f"{label}: expected {error_type.__name__}, got {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"{label}: expected {error_type.__name__}, call returned successfully")


def _run_thread(call: Callable[[], Any], name: str) -> tuple[threading.Thread, dict[str, Any]]:
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


def _finish_thread(thread: threading.Thread, outcome: dict[str, Any], timeout: float = 3.0) -> Any:
    thread.join(timeout)
    assert not thread.is_alive(), f"{thread.name} did not finish within {timeout:.2f}s"
    assert outcome.get("done") is True
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def _assert_runtime_stopped() -> dict[str, Any]:
    info = _runtime_type.info()
    assert info["lease_count"] == 0, info
    assert info["io_threads"] == 0, info
    return info


def _session(*, mode: str = "refresh"):
    return _client_module.VNCSession(timeout=2.0, action_timeout=3.0, screenshot_mode=mode)


def _check_automatic_text() -> dict[str, Any]:
    session = _session()
    with _native.NativeRfbFixture() as server:
        try:
            connected = session.connect("127.0.0.1", server.port, password=None, url=None)
            before_events = _events(server)
            before_keys = len(_events(server, 4))
            before_pointers = len(_events(server, 5))
            refusals = {}
            for label, text in (("ascii", "plain"), ("latin1", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"),
                                ("unicode", "\N{CJK UNIFIED IDEOGRAPH-4F60}\N{CJK UNIFIED IDEOGRAPH-597D}")):
                exc = _expect_raises(lambda value=text: session.send_text(value, method="auto"), ValueError,
                                     f"automatic {label} text without a channel")
                assert "no input was sent" in str(exc)
                refusals[label] = type(exc).__name__
            assert _events(server) == before_events, "automatic text refusal emitted an RFB message"
            assert len(_events(server, 4)) == before_keys
            assert len(_events(server, 5)) == before_pointers

            result = session.send_text("a", method="keys")
            observed = server.wait_for(lambda values: keys if len(keys := [e for e in values if e.get("type") == 4]) >= before_keys + 2 else None)
            key_events = [event for event in observed if event.get("type") == 4]
            assert [(event["down"], event["keysym"]) for event in key_events[-2:]] == [(1, 0x61), (0, 0x61)]
            assert result["transport"] == "keys"
            assert result["input_method_sensitive"] is True
            assert len(_events(server, 5)) == before_pointers, "explicit key text unexpectedly moved/clicked the pointer"
            assert connected["connected"] is True
            return {"refused_text_classes": sorted(refusals), "explicit_keys": result,
                    "key_events": [(event["down"], event["keysym"]) for event in key_events[-2:]],
                    "pointer_events": len(_events(server, 5))}
        finally:
            if session.client is not None:
                session.disconnect()
            _assert_runtime_stopped()


def _check_auto_delay() -> dict[str, Any]:
    session = _session()
    with _native.NativeRfbFixture() as server:
        try:
            session.connect("127.0.0.1", server.port, password=None, url=None)
            before = _events(server)
            exc = _expect_raises(lambda: session.send_text("slow", method="auto", delay_ms=50), ValueError,
                                 "automatic text with delay_ms")
            assert "explicit method=keys" in str(exc)
            assert _events(server) == before, "automatic delayed text emitted an RFB message"
            assert not _events(server, 4) and not _events(server, 5)
            assert session.status()["connected"] is True
            return {"error": str(exc), "key_events": 0, "pointer_events": 0}
        finally:
            if session.client is not None:
                session.disconnect()
            _assert_runtime_stopped()


def _check_zero_server_init() -> dict[str, Any]:
    session = _session()
    with _native.NativeRfbFixture(width=0, height=0) as server:
        try:
            connected = session.connect("127.0.0.1", server.port, password=None, url=None)
            connection_id = connected["connection_id"]
            assert connected["connected"] is True
            assert connected["display_state"] == "unavailable"
            assert connected["width"] == 0 and connected["height"] == 0
            exc = _expect_raises(session.capture, _rfb_module.FramebufferUnavailable,
                                 "capture on a connected 0x0 desktop")
            after = session.status()
            assert after["connected"] is True
            assert after["display_state"] == "unavailable"
            assert after["connection_id"] == connection_id
            assert session.client is not None and not session.client.closed.is_set()
            assert not _events(server, 4) and not _events(server, 5)
            return {"display_state": after["display_state"], "connection_id_preserved": True,
                    "capture_error": type(exc).__name__}
        finally:
            if session.client is not None:
                session.disconnect()
            _assert_runtime_stopped()


def _check_wake_and_held_input() -> dict[str, Any]:
    session = _session()
    zero_evidence: dict[str, Any]
    with _native.NativeRfbFixture(width=0, height=0) as server:
        try:
            session.connect("127.0.0.1", server.port, password=None, url=None)
            before = _events(server)
            result = session.wake_display()
            observed = server.wait_for(lambda values: pointers if len(pointers := [e for e in values if e.get("type") == 5]) >= 2 else None)
            pointers = [event for event in observed if event.get("type") == 5]
            assert [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]] == [(0, 0, 0), (0, 1, 0)]
            assert result["wake_sent"] is True
            assert result["input"] == "pointer-motion-only"
            assert result["application_verified"] is False
            assert not _events(server, 4)
            assert _events(server)[:len(before)] == before
            zero_evidence = {"pointer_events": [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]],
                             "result": result}
        finally:
            if session.client is not None:
                session.disconnect()

    held = _session()
    with _native.NativeRfbFixture() as server:
        try:
            held.connect("127.0.0.1", server.port, password=None, url=None)
            held.key_down("a")
            server.wait_for(lambda values: next((e for e in values if e.get("type") == 4 and e.get("down") == 1), None))
            before_key_wake = _events(server)
            _expect_raises(held.wake_display, ValueError, "wake with a held key")
            assert _events(server) == before_key_wake, "wake with a held key emitted an event"
            held.key_up("a")

            held.mouse_down(0, 0, "left")
            server.wait_for(lambda values: next((e for e in values if e.get("type") == 5 and e.get("buttons") == 1), None))
            before_button_wake = _events(server)
            _expect_raises(held.wake_display, ValueError, "wake with a held mouse button")
            assert _events(server) == before_button_wake, "wake with a held mouse button emitted an event"
            held.mouse_up(0, 0, "left")
            assert held.status()["connected"] is True
            return {**zero_evidence, "held_key_rejection_events": 0, "held_button_rejection_events": 0}
        finally:
            if held.client is not None:
                held.disconnect()
            _assert_runtime_stopped()


def _send_raw_row(server: Any, y: int, colors: list[tuple[int, int, int]]) -> None:
    pixels = b"".join(_native._wire_pixel(color) for color in colors)
    server.send_wire(_native._framebuffer_update([_native._rect(0, y, len(colors), 1, pixels)]))


def _check_initial_zero_split_recovery() -> dict[str, Any]:
    with _native.NativeRfbFixture(width=0, height=0, automatic_initial_update=False) as server:
        client = _rfb_module.RFBConnection("127.0.0.1", server.port, None, 2.0, "raw")
        try:
            assert client.width == client.height == 0
            assert client.framebuffer_available is False
            server.send_wire(_native._framebuffer_update([_native._rect(0, 0, 3, 2, None, encoding=-223)]))
            _native._wait_until(lambda: (client.width, client.height) == (3, 2))
            thread, outcome = _run_thread(lambda: client.capture(timeout=2.0), "vnc-zero-start-capture")
            server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 1 else None)
            first_row = [(10, 20, 30), (40, 50, 60), (70, 80, 90)]
            second_row = [(100, 110, 120), (130, 140, 150), (160, 170, 180)]
            _send_raw_row(server, 0, first_row)
            server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 2 else None)
            _send_raw_row(server, 1, second_row)
            snapshot = _finish_thread(thread, outcome)
            expected = first_row + second_row
            assert snapshot.image.size == (3, 2)
            assert list(snapshot.image.get_flattened_data()) == expected
            assert client.framebuffer_available is True
            return {"geometry": list(snapshot.image.size), "pixels": [list(pixel) for pixel in expected],
                    "frame_sequence": client.frame_sequence}
        finally:
            client.close()
            _assert_runtime_stopped()


def _check_cached_inspection_invalidated() -> dict[str, Any]:
    session = _session()
    with _native.NativeRfbFixture() as server:
        try:
            session.connect("127.0.0.1", server.port, password=None, url=None)
            client = session.client
            assert client is not None
            control_socket = server.connection
            assert control_socket is not None
            capture_thread, outcome = _run_thread(session.capture, "vnc-cache-seed-capture")
            server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 2 else None)
            server.send_update([(0, 0, 2, 2, [(25, 35, 45), (55, 65, 75), (85, 95, 105), (115, 125, 135)])])
            screenshot = _finish_thread(capture_thread, outcome)
            frame_id = screenshot.frame_id
            assert frame_id
            assert session.inspect(frame_id=frame_id).details["cached_frame"] is True

            control_socket.sendall(_native._framebuffer_update([_native._rect(0, 0, 0, 0, None, encoding=-223)]))
            _native._wait_until(lambda: client.width == 0 and client.height == 0)
            exc = _expect_raises(lambda: session.inspect(frame_id=frame_id), _rfb_module.FramebufferUnavailable,
                                 "inspect a cached frame after a zero-size resize")
            assert session._last_visual_frame is None, "zero resize retained an inspectable cached frame"
            assert session.status()["connected"] is True
            assert session.status()["display_state"] == "unavailable"
            return {"old_frame_id_rejected": True, "error": type(exc).__name__,
                    "cache_cleared": session._last_visual_frame is None}
        finally:
            if session.client is not None:
                session.disconnect()
            _assert_runtime_stopped()


def _check_waiter_zero_resize_and_recovery() -> dict[str, Any]:
    with _native.NativeRfbFixture() as server:
        client = _rfb_module.RFBConnection("127.0.0.1", server.port, None, 2.0, "raw")
        first: tuple[threading.Thread, dict[str, Any]] | None = None
        second: tuple[threading.Thread, dict[str, Any]] | None = None
        try:
            first = _run_thread(lambda: client.capture(timeout=3.0), "vnc-capture-waiting-for-zero-resize")
            server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 2 else None)
            start = time.monotonic()
            server.send_wire(_native._framebuffer_update([_native._rect(0, 0, 0, 0, None, encoding=-223)]))
            first[0].join(1.0)
            assert not first[0].is_alive(), "capture waiter was not woken promptly by 0x0 resize"
            assert "error" in first[1], "capture waiter returned a stale snapshot after 0x0 resize"
            assert isinstance(first[1]["error"], _rfb_module.FramebufferUnavailable), first[1]["error"]
            zero_elapsed = time.monotonic() - start
            assert zero_elapsed < 1.0, f"0x0 resize error took {zero_elapsed:.3f}s"

            server.send_wire(_native._framebuffer_update([_native._rect(0, 0, 2, 2, None, encoding=-223)]))
            _native._wait_until(lambda: (client.width, client.height) == (2, 2))
            server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 3 else None)
            second = _run_thread(lambda: client.capture(timeout=2.0), "vnc-capture-after-zero-recovery")
            server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 4 else None)
            colors = [(210, 20, 30), (40, 220, 60), (70, 80, 230), (240, 200, 10)]
            server.send_update([(0, 0, 2, 2, colors)])
            recovered = _finish_thread(*second)
            assert recovered.image.size == (2, 2)
            assert list(recovered.image.get_flattened_data()) == colors
            assert client.framebuffer_available is True
            return {"zero_resize_error": type(first[1]["error"]).__name__,
                    "zero_resize_seconds": round(zero_elapsed, 6), "recovered_geometry": list(recovered.image.size),
                    "recovered_pixels": [list(pixel) for pixel in colors]}
        finally:
            if first is not None and first[0].is_alive():
                server.drop()
                first[0].join(2)
            if second is not None and second[0].is_alive():
                server.drop()
                second[0].join(2)
            client.close()
            _assert_runtime_stopped()


def _check_incomplete_initial_frame() -> dict[str, Any]:
    cases: dict[str, Any] = {}
    for first_frame in ("none", "partial"):
        session = _client_module.VNCSession(timeout=0.45, action_timeout=2.0, screenshot_mode="refresh")
        with _native.NativeRfbFixture(
            width=2, height=2, automatic_initial_update=False,
            security_types=(2,), read_fragment=4096, write_fragment=4096,
        ) as server:
            connect_call: tuple[threading.Thread, dict[str, Any]] | None = None
            capture_call: tuple[threading.Thread, dict[str, Any]] | None = None
            try:
                started = time.monotonic()
                connect_call = _run_thread(
                    lambda: session.connect("127.0.0.1", server.port, password="synthetic", url=None),
                    f"vnc-initial-{first_frame}-frame-connect",
                )
                server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 1 else None)
                if first_frame == "partial":
                    _send_raw_row(server, 0, [(11, 21, 31), (41, 51, 61)])
                    server.wait_for(lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= 2 else None)
                connected = _finish_thread(*connect_call, timeout=2.0)
                connect_seconds = time.monotonic() - started
                assert connect_seconds <= 1.2, f"initial-frame timeout left connect blocked for {connect_seconds:.3f}s"
                control = session.client
                connection_id = connected["connection_id"]
                assert connected["connected"] is True and connected["display_state"] == "unavailable"
                assert connected["width"] == 2 and connected["height"] == 2
                assert control is not None and control.password == "synthetic"
                assert not control.closed.is_set()
                assert control.framebuffer_available is False
                assert control.last_snapshot is None and control.screen is None
                assert session._last_visual_frame is None and session._visual_reference is None

                before_input = _events(server)
                _expect_raises(lambda: session.key_down("a"), _rfb_module.FramebufferUnavailable,
                               f"key input before a complete {first_frame} initial frame")
                try:
                    session.click(0, 0)
                except (_rfb_module.FramebufferUnavailable, ValueError) as exc:
                    click_refusal = {"type": type(exc).__name__, "message": str(exc)}
                else:
                    raise AssertionError(f"click before a complete {first_frame} initial frame was accepted")
                assert _events(server) == before_input, "input before a complete initial frame reached the server"

                wake = session.wake_display()
                pointer_events = server.wait_for(
                    lambda values: pointers if len(pointers := [e for e in values if e.get("type") == 5]) >= 2 else None
                )
                pointers = [event for event in pointer_events if event.get("type") == 5]
                assert [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]] == [
                    (0, 0, 0), (0, 1, 0),
                ]
                assert wake["wake_sent"] is True and wake["application_verified"] is False
                assert not _events(server, 4)
                assert session.status()["display_state"] == "unavailable"
                assert session.connection_id == connection_id and session.client is control

                request_count = len(_events(server, 3))
                capture_call = _run_thread(session.capture, f"vnc-recover-{first_frame}-initial-frame")
                server.wait_for(
                    lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) > request_count else None
                )
                expected = [(15, 35, 55), (75, 95, 115), (135, 155, 175), (195, 215, 235)]
                server.send_update([(0, 0, 2, 2, expected)])
                screenshot = _finish_thread(*capture_call)
                assert screenshot.desktop_width == 2 and screenshot.desktop_height == 2
                assert session.connection_id == connection_id and session.client is control
                assert control.framebuffer_available is True
                assert list(session._last_visual_frame.image.get_flattened_data()) == expected
                ready = session.status()
                assert ready["connected"] is True and ready["display_state"] == "ready"
                assert not _events(server, 4)
                cases[first_frame] = {
                    "connect_seconds": round(connect_seconds, 6),
                    "authenticated_session_retained": True,
                    "connection_id_preserved": True,
                    "initial_cache_absent": True,
                    "key_input_refusal": "FramebufferUnavailable",
                    "click_input_refusal": click_refusal,
                    "wake_pointer_events": [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]],
                    "recovered_geometry": [screenshot.desktop_width, screenshot.desktop_height],
                    "recovered_pixels": [list(pixel) for pixel in expected],
                    "state_after_capture": ready["display_state"],
                }
            finally:
                if connect_call is not None and connect_call[0].is_alive():
                    server.drop()
                    connect_call[0].join(2)
                if capture_call is not None and capture_call[0].is_alive():
                    server.drop()
                    capture_call[0].join(2)
                if session.client is not None:
                    session.disconnect()
                _assert_runtime_stopped()
    return {"cases": cases}


def _check_capture_timeout_invalidates_and_recovers() -> dict[str, Any]:
    session = _client_module.VNCSession(timeout=0.3, action_timeout=3.0, screenshot_mode="auto")
    with _native.NativeRfbFixture(width=2, height=2, read_fragment=4096, write_fragment=4096) as server:
        first_capture: tuple[threading.Thread, dict[str, Any]] | None = None
        failed_capture: tuple[threading.Thread, dict[str, Any]] | None = None
        recovered_capture: tuple[threading.Thread, dict[str, Any]] | None = None
        try:
            connected = session.connect("127.0.0.1", server.port, password=None, url=None)
            control = session.client
            control_socket = server.connection
            connection_id = connected["connection_id"]
            assert control is not None and control_socket is not None
            assert connected["display_state"] == "ready"

            requests_before = len(_events(server, 3))
            first_capture = _run_thread(lambda: session.capture(remember=True), "vnc-recovery-cache-seed")
            server.wait_for(
                lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) > requests_before else None
            )
            seed_pixels = [(24, 34, 44), (54, 64, 74), (84, 94, 104), (114, 124, 134)]
            server.send_update([(0, 0, 2, 2, seed_pixels)])
            seed = _finish_thread(*first_capture)
            frame_id, reference_id = seed.frame_id, seed.reference_id
            assert frame_id and reference_id
            assert session.status()["display_state"] == "ready"
            assert session.inspect(frame_id=frame_id).details["cached_frame"] is True

            requests_before = len(_events(server, 3))
            failed_capture = _run_thread(session.capture, "vnc-recovery-timed-out-capture")
            server.wait_for(
                lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) > requests_before else None
            )
            # Auto mode falls back to a short-lived fresh connection after the
            # control connection's requested refresh times out. The fixture's
            # first response has already been used, so this new connection also
            # withholds pixels and exercises the complete no-fresh-frame path.
            _native._wait_until(lambda: server.connection is not control_socket, timeout=1.5)
            fallback_socket = server.connection
            server.wait_for(
                lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) >= requests_before + 2 else None
            )
            try:
                _finish_thread(*failed_capture, timeout=2.0)
            except _rfb_module.FramebufferUnavailable as exc:
                failure = exc
            else:
                raise AssertionError("capture timeout returned a cached or incomplete image")
            assert str(failure).strip(), "capture timeout returned an empty FramebufferUnavailable message"
            message = str(failure).lower()
            assert "fresh" in message or "no cached" in message, failure
            assert session.client is control and not control.closed.is_set()
            assert session.connection_id == connection_id
            assert session._last_visual_frame is None and session._visual_reference is None
            unavailable = session.status()
            assert unavailable["connected"] is True and unavailable["display_state"] == "unavailable"
            inspect_error = _expect_raises(lambda: session.inspect(frame_id=frame_id),
                                           _rfb_module.FramebufferUnavailable,
                                           "inspect the pre-timeout frame after capture timeout")
            reference_error = _expect_raises(lambda: session.visual_reference(reference_id), ValueError,
                                             "reuse the pre-timeout reference after capture timeout")
            assert "frame" in str(inspect_error).lower() or "display" in str(inspect_error).lower()
            assert "reference" in str(reference_error).lower()

            before_wake = _events(server)
            _expect_raises(lambda: session.key_down("a"), _rfb_module.FramebufferUnavailable,
                           "input after capture timeout invalidated the display")
            assert _events(server) == before_wake, "input after capture timeout reached the server"
            wake = session.wake_display()
            pointer_events = server.wait_for(
                lambda values: pointers if len(pointers := [e for e in values if e.get("type") == 5]) >= 2 else None
            )
            pointers = [event for event in pointer_events if event.get("type") == 5]
            assert [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]] == [
                (0, 0, 0), (0, 1, 0),
            ]
            assert wake["wake_sent"] is True and wake["application_verified"] is False
            assert session.status()["display_state"] == "unavailable", "wake incorrectly marked the display ready"
            assert not _events(server, 4)

            # Once the current wake cycle permits a full fresh frame, auto's
            # established fresh strategy must recover without changing the
            # long-lived connection ID. Reset the one-shot fixture response
            # before creating that next temporary screenshot connection.
            server.initial_sent = False
            fallback_socket = server.connection
            requests_before = len(_events(server, 3))
            recovered_capture = _run_thread(session.capture, "vnc-recovery-successful-fresh-capture")
            _native._wait_until(lambda: server.connection is not fallback_socket, timeout=1.5)
            server.wait_for(
                lambda values: requests if len(requests := [e for e in values if e.get("type") == 3]) > requests_before else None
            )
            recovered = _finish_thread(*recovered_capture, timeout=2.0)
            assert recovered.capture_source == "fresh-connection"
            assert recovered.desktop_width == 2 and recovered.desktop_height == 2
            expected = list(server.initial_colors)
            assert list(session._last_visual_frame.image.get_flattened_data()) == expected
            ready = session.status()
            assert ready["connected"] is True and ready["display_state"] == "ready"
            assert session.client is control and session.connection_id == connection_id
            assert session._visual_reference is None, "a timed-out reference was implicitly restored"
            return {
                "timeout_error": str(failure),
                "old_frame_rejected": type(inspect_error).__name__,
                "old_reference_rejected": type(reference_error).__name__,
                "connection_id_preserved": True,
                "wake_result": wake,
                "wake_state": unavailable["display_state"],
                "recovered_capture_source": recovered.capture_source,
                "state_after_recovery": ready["display_state"],
                "pixels_after_recovery": [list(pixel) for pixel in expected],
            }
        finally:
            for task in (first_capture, failed_capture, recovered_capture):
                if task is not None and task[0].is_alive():
                    server.drop()
                    task[0].join(2)
            if session.client is not None:
                session.disconnect()
            _assert_runtime_stopped()


class _McpStdio:
    def __init__(self, source: Path) -> None:
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("VNC_")}
        old_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(source / "src") + (os.pathsep + old_pythonpath if old_pythonpath else "")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "vmware_vnc", "mcp"], cwd=str(source), env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, name="vmware-vnc-stdio-reader", daemon=True)
        self.reader.start()
        self.request_id = 0

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _write(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP stdio request {method!r} timed out; process={self.process.poll()}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"MCP stdio request {method!r} timed out; process={self.process.poll()}") from exc
            if line is None:
                raise ConnectionError(f"MCP stdio closed while waiting for {method!r}; process={self.process.poll()}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    @staticmethod
    def content_text(response: dict[str, Any]) -> str:
        result = response.get("result", {})
        blocks = result.get("content", []) if isinstance(result, dict) else []
        return "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict))

    @classmethod
    def tool_value(cls, response: dict[str, Any]) -> tuple[Any, str, bool]:
        if "error" in response:
            error = response["error"]
            return None, json.dumps(error, ensure_ascii=False), True
        result = response.get("result", {})
        if not isinstance(result, dict):
            return result, "", False
        text = cls.content_text(response)
        if "structuredContent" in result:
            return result["structuredContent"], text, bool(result.get("isError"))
        for block in result.get("content", []):
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    return json.loads(block["text"]), text, bool(result.get("isError"))
                except json.JSONDecodeError:
                    pass
        return None, text, bool(result.get("isError"))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[Any, str, bool]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        return self.tool_value(response)

    def close(self) -> dict[str, Any]:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        return {"process_returncode": self.process.returncode, "reader_stopped": not self.reader.is_alive()}


def _check_mcp_wake(source: Path) -> dict[str, Any]:
    client = _McpStdio(source)
    connected = False
    session_id = "recovery_named"
    evidence: dict[str, Any] = {}
    try:
        initialized = client.request("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "vnc-recovery-check", "version": "1"},
        })
        assert "result" in initialized, initialized
        client.notify("notifications/initialized")
        tools = client.request("tools/list")
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        assert "vnc_wake" in tool_names

        with _native.NativeRfbFixture(width=0, height=0) as server:
            connection, connection_text, connection_error = client.call_tool("vnc_connect", {
                "host": "127.0.0.1", "port": server.port, "session_id": session_id,
            })
            assert not connection_error, connection_text
            assert isinstance(connection, dict), (connection, connection_text)
            connected = True
            connection_id = connection["connection_id"]
            assert connection["session_id"] == session_id
            assert connection["connected"] is True
            assert connection["display_state"] == "unavailable"

            status, status_text, status_error = client.call_tool("vnc_status", {"session_id": session_id})
            assert not status_error, status_text
            assert status["display_state"] == "unavailable" and status["connected"] is True
            assert status["connection_id"] == connection_id

            before = _events(server)
            _missing, missing_text, missing_error = client.call_tool("vnc_wake", {"session_id": session_id})
            assert missing_error, "named-session vnc_wake succeeded without connection_id"
            assert "connection_id" in missing_text, missing_text
            assert _events(server) == before, "missing-ID vnc_wake emitted RFB input"

            wake, wake_text, wake_error = client.call_tool("vnc_wake", {
                "session_id": session_id, "connection_id": connection_id,
            })
            assert not wake_error, wake_text
            assert wake["wake_sent"] is True
            assert wake["input"] == "pointer-motion-only"
            assert wake["application_verified"] is False
            assert wake["session_id"] == session_id and wake["connection_id"] == connection_id
            events = server.wait_for(lambda values: pointers if len(pointers := [e for e in values if e.get("type") == 5]) >= 2 else None)
            pointers = [event for event in events if event.get("type") == 5]
            assert [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]] == [(0, 0, 0), (0, 1, 0)]
            assert not _events(server, 4)
            after = client.call_tool("vnc_status", {"session_id": session_id})[0]
            assert after["connected"] is True and after["display_state"] == "unavailable"

            disconnected, disconnect_text, disconnect_error = client.call_tool("vnc_disconnect", {
                "session_id": session_id, "connection_id": connection_id,
            })
            assert not disconnect_error, disconnect_text
            connected = False
            assert disconnected["connected"] is False
            evidence = {"protocol_version": initialized["result"].get("protocolVersion"),
                        "named_connection_guard": missing_text,
                        "wake": wake, "status_after_wake": after["display_state"],
                        "pointer_events": [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]]}
    finally:
        if connected:
            try:
                client.call_tool("vnc_disconnect", {"session_id": session_id,
                                                       "connection_id": connection.get("connection_id")})
            except BaseException:
                pass
        process_info = client.close()
    assert process_info["process_returncode"] == 0, process_info
    assert process_info["reader_stopped"] is True, process_info
    _assert_runtime_stopped()
    return {**evidence, **process_info}


def _without_vnc_environment(call: Callable[[], Any]) -> Any:
    removed = {key: value for key, value in os.environ.items() if key.upper().startswith("VNC_")}
    try:
        for key in removed:
            os.environ.pop(key, None)
        return call()
    finally:
        os.environ.update(removed)


def _check_cli_wake() -> dict[str, Any]:
    from vmware_vnc import cli

    with _native.NativeRfbFixture(width=0, height=0) as server:
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = ["wake", "--host", "127.0.0.1", "--port", str(server.port),
                "--timeout", "2", "--action-timeout", "3", "--screenshot-mode", "refresh"]
        code = _without_vnc_environment(lambda: _invoke_cli(cli.main, args, stdout, stderr))
        assert code == 0, f"CLI returned {code}; stdout={stdout.getvalue()!r}; stderr={stderr.getvalue()!r}"
        result = json.loads(stdout.getvalue())
        assert result["wake_sent"] is True
        assert result["input"] == "pointer-motion-only"
        assert result["application_verified"] is False
        assert "does not unlock or guarantee a display" in result["next_action"]
        events = server.wait_for(lambda values: pointers if len(pointers := [e for e in values if e.get("type") == 5]) >= 2 else None)
        pointers = [event for event in events if event.get("type") == 5]
        assert [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]] == [(0, 0, 0), (0, 1, 0)]
        assert not _events(server, 4)
        _native._wait_until(server.disconnected.is_set, timeout=2.0)
        runtime = _assert_runtime_stopped()
        return {"exit_code": code, "json": result,
                "pointer_events": [(event["buttons"], event["x"], event["y"]) for event in pointers[-2:]],
                "runtime": runtime, "server_disconnected": server.disconnected.is_set()}


def _invoke_cli(main: Callable[[list[str]], int], args: list[str], stdout: io.StringIO,
                stderr: io.StringIO) -> int:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        return main(args)


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

    report: dict[str, Any] = {
        "success": False,
        "checks": {name: False for name in CHECK_NAMES},
        "evidence": {},
        "failures": [],
    }
    try:
        _native = importlib.import_module("vnc_native_rfb")
        _rfb_module = importlib.import_module("vmware_vnc.rfb")
        _client_module = importlib.import_module("vmware_vnc.client")
        _runtime_type = importlib.import_module("vmware_vnc.runtime").RfbRuntime
    except BaseException as exc:
        report["failures"].append({"check": "modules_load", "type": type(exc).__name__,
                                   "message": str(exc), "traceback": traceback.format_exc()})
    else:
        globals()["_native"] = _native
        globals()["_rfb_module"] = _rfb_module
        globals()["_client_module"] = _client_module
        globals()["_runtime_type"] = _runtime_type
        checks: tuple[Callable[[], dict[str, Any]], ...] = (
            _check_automatic_text,
            _check_auto_delay,
            _check_zero_server_init,
            _check_wake_and_held_input,
            _check_initial_zero_split_recovery,
            _check_cached_inspection_invalidated,
            _check_waiter_zero_resize_and_recovery,
            _check_incomplete_initial_frame,
            _check_capture_timeout_invalidates_and_recovers,
            lambda: _check_mcp_wake(source),
            _check_cli_wake,
        )
        for name, check in zip(CHECK_NAMES, checks):
            try:
                report["evidence"][name] = check()
                report["checks"][name] = True
            except BaseException as exc:
                report["failures"].append({"check": name, "type": type(exc).__name__,
                                           "message": str(exc), "traceback": traceback.format_exc()})
    report["success"] = all(report["checks"].values()) and not report["failures"]
    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
