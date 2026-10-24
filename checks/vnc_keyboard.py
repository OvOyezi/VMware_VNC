"""Synthetic RFB keyboard-input regressions; never connects to a real VM."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import random
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest import mock


CHECK_NAMES = (
    "literal_newlines_emit_enter_and_preserve_input_length",
    "modifier_case_and_explicit_shift_wire_sequences",
    "auto_text_refuses_before_focus_and_explicit_keys_remain_available",
    "normalized_newline_budget_refuses_before_focus_or_input",
    "invalid_text_parameters_and_focus_send_no_prefix",
    "parallel_named_sessions_keep_complete_wire_streams_isolated",
    "stale_connection_id_is_rejected_without_input",
)


def _load(source: Path):
    src = source / "src"
    checks = source / "checks"
    if not (src / "vmware_vnc" / "application.py").is_file():
        raise FileNotFoundError("src/vmware_vnc/application.py was not found under --source")
    if not (checks / "vnc_regression.py").is_file():
        raise FileNotFoundError("checks/vnc_regression.py was not found under --source")
    for location in (str(src), str(checks)):
        if location in sys.path:
            sys.path.remove(location)
        sys.path.insert(0, location)
    application = importlib.import_module("vmware_vnc.application")
    config = importlib.import_module("vmware_vnc.config")
    client = importlib.import_module("vmware_vnc.client")
    regression = importlib.import_module("vnc_regression")
    return application, config, client, regression.Fixture


def _app(application_module: Any, config_module: Any, action_timeout: float = 8.0,
         profiles_file: str | None = None):
    config = config_module.VNCConfig(
        timeout=1.5,
        action_timeout=action_timeout,
        screenshot_mode="refresh",
        idle_timeout=0,
        profiles_file=profiles_file,
    )
    return application_module.VNCApplication(config, environment={})


async def _expect_error(expected: type[BaseException], operation: Callable[[], Awaitable[Any]],
                        label: str) -> BaseException:
    try:
        await operation()
    except expected as exc:
        return exc
    except Exception as exc:
        raise AssertionError(f"{label} raised {type(exc).__name__}, expected {expected.__name__}: {exc}") from exc
    raise AssertionError(f"{label} did not raise {expected.__name__}")


def _snapshot_events(fixture: Any) -> list[tuple]:
    with fixture.lock:
        return list(fixture.events)


async def _barrier(app: Any, fixture: Any, session_id: str, connection_id: str) -> None:
    """Finish a framebuffer round trip after input on the same RFB stream."""
    before = len(fixture.framebuffer_requests)
    await app.run("capture", "png", 80, session_id=session_id, connection_id=connection_id)
    if len(fixture.framebuffer_requests) <= before:
        raise AssertionError("capture returned without the fixture observing a new framebuffer request")


def _wire_for_text(text: str) -> list[tuple[str, bool, int]]:
    """Independent expected RFB sequence for the supported synthetic ASCII set."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    shifted = frozenset('~!@#$%^&*()_+{}|:"<>?')
    events: list[tuple[str, bool, int]] = []
    for character in normalized:
        if character == "\n":
            codes = (0xFF0D,)
        elif character == "\t":
            codes = (0xFF09,)
        elif "A" <= character <= "Z" or character in shifted:
            codes = (0xFFE1, ord(character))
        else:
            codes = (ord(character),)
        events.extend(("key", True, code) for code in codes)
        events.extend(("key", False, code) for code in reversed(codes))
    return events


def _wire_for_chord(codes: tuple[int, ...]) -> list[tuple[str, bool, int]]:
    return [*(('key', True, code) for code in codes), *(('key', False, code) for code in reversed(codes))]


async def _check_newlines(app: Any, fixture: Any, session_id: str, connection_id: str) -> dict[str, int]:
    cases = ("a\rb", "a\nb", "a\r\nb", "a\rb\r\nc\nd")
    before = len(_snapshot_events(fixture))
    results: dict[str, int] = {}
    for index, text in enumerate(cases):
        if index == 0:
            # Exercise the literal-text operation directly once; the remaining
            # cases also cover the submitted-character result from send_text.
            result = await app.run("type_text", text, 0,
                                   session_id=session_id, connection_id=connection_id)
        else:
            result = await app.run("send_text", text, "keys", "ctrl-v", None, None, 0,
                                   session_id=session_id, connection_id=connection_id)
        value = result.value
        if value.get("typed_characters") != len(text):
            raise AssertionError(f"typed_characters did not preserve original length for {text!r}: {value}")
        if index and value.get("submitted_characters") != len(text):
            raise AssertionError(f"submitted_characters did not preserve original length for {text!r}: {value}")
        results[repr(text)] = value["typed_characters"]
    # Also exercise the transport batch API directly: the application normally
    # normalizes first, which would otherwise hide a CR regression below it.
    from vmware_vnc.operations import ActionControl

    direct_text = "x\ry\r\nz\n"
    backend = app.registry._entries[session_id].backend
    await asyncio.to_thread(backend.client.type_batch, direct_text,
                            ActionControl(time.monotonic() + 5))
    results["direct_batch_input_length"] = len(direct_text)
    await _barrier(app, fixture, session_id, connection_id)
    expected = [event for text in (*cases, direct_text) for event in _wire_for_text(text)]
    actual = _snapshot_events(fixture)[before:]
    if actual != expected:
        raise AssertionError(f"literal newline wire events differ: expected {expected!r}, got {actual!r}")
    if _wire_for_text(cases[0]) != _wire_for_text(cases[1]) or _wire_for_text(cases[1]) != _wire_for_text(cases[2]):
        raise AssertionError("test oracle does not equate CR, LF and CRLF")
    if sum(event[2] == 0xFF0D and event[1] for event in _wire_for_text(cases[-1])) != 3:
        raise AssertionError("test oracle does not expect three Enter presses for mixed newlines")
    return results


async def _check_modifiers(app: Any, fixture: Any, session_id: str, connection_id: str) -> None:
    before = len(_snapshot_events(fixture))
    await app.run("keypress", "Ctrl+A", session_id=session_id, connection_id=connection_id)
    await app.run("keypress", "ctrl+a", session_id=session_id, connection_id=connection_id)
    await app.run("keypress", "ctrl+shift+A", session_id=session_id, connection_id=connection_id)
    await app.run("keypress", "A", session_id=session_id, connection_id=connection_id)
    await _barrier(app, fixture, session_id, connection_id)

    ctrl_a = _wire_for_chord((0xFFE3, ord("a")))
    ctrl_shift_a = _wire_for_chord((0xFFE3, 0xFFE1, ord("a")))
    standalone_upper = _wire_for_chord((0xFFE1, ord("A")))
    expected = [*ctrl_a, *ctrl_a, *ctrl_shift_a, *standalone_upper]
    actual = _snapshot_events(fixture)[before:]
    if actual != expected:
        raise AssertionError(f"modifier wire sequences differ: expected {expected!r}, got {actual!r}")


async def _check_auto_text(app: Any, fixture: Any, client_module: Any,
                           application_module: Any, session_id: str,
                           connection_id: str, profile_path: Path) -> None:
    before = len(_snapshot_events(fixture))
    backend = app.registry._entries[session_id].backend
    if backend.client is None or backend.client.ext_clipboard.supported:
        raise AssertionError("fixture unexpectedly negotiated UTF-8 Extended Clipboard")
    if profile_path.exists():
        raise AssertionError("synthetic missing-profile sentinel unexpectedly exists")

    with mock.patch.object(
        application_module,
        "load_profiles",
        side_effect=AssertionError("automatic text attempted to load guest profiles"),
    ) as load_profiles, mock.patch.object(
        client_module,
        "VmrunClipboard",
        side_effect=AssertionError("automatic text attempted VmrunClipboard"),
    ) as vmrun_clipboard:
        await _expect_error(
            ValueError,
            lambda: app.run("send_text", "safe-prefix", "auto", "ctrl-v", 4, 5, 0,
                            session_id=session_id, connection_id=connection_id),
            "auto text without negotiated clipboard or profile",
        )
        if load_profiles.called or vmrun_clipboard.called:
            raise AssertionError("automatic text touched a guest profile or VmrunClipboard")

    if app._profiles is not None:
        raise AssertionError("automatic text loaded guest profiles despite no profile selection")
    await _barrier(app, fixture, session_id, connection_id)
    if _snapshot_events(fixture)[before:] != []:
        raise AssertionError("rejected automatic text emitted a pointer or keyboard event")

    typed = "A!z"
    explicit = await app.run("send_text", typed, "keys", "ctrl-v", None, None, 0,
                             session_id=session_id, connection_id=connection_id)
    if explicit.value.get("input_method_sensitive") is not True:
        raise AssertionError(f"explicit key text did not report input_method_sensitive=true: {explicit.value}")
    if explicit.value.get("application_verified") is not False:
        raise AssertionError(f"explicit key text claimed application verification: {explicit.value}")
    if explicit.value.get("submitted_characters") != len(typed):
        raise AssertionError(f"explicit key text lost its original character count: {explicit.value}")
    await _barrier(app, fixture, session_id, connection_id)
    expected = _wire_for_text(typed)
    if _snapshot_events(fixture)[before:] != expected:
        raise AssertionError("explicit keys either failed or did not preserve the checked key-event sequence")


async def _check_budget(application_module: Any, config_module: Any, session_type: Any,
                        fixture: Any, session_id: str, profile_path: Path) -> int:
    # Leave room for the real fixture handshake; test rejection with a larger
    # normalized input rather than relying on a sub-frame setup deadline.
    app = _app(application_module, config_module, action_timeout=1.0, profiles_file=str(profile_path))
    try:
        connected = await app.connect("127.0.0.1", fixture.port, session_id=session_id)
        connection_id = connected.connection_id
        if not connection_id:
            raise AssertionError("budget fixture connect returned no connection_id")
        before = len(_snapshot_events(fixture))
        await _expect_error(
            ValueError,
            lambda: app.run("send_text", "\r" * 60, "keys", "ctrl-v", 4, 5, 20,
                            session_id=session_id, connection_id=connection_id),
            "newline-normalized key input over the 1s action budget",
        )

        backend = app.registry._entries[session_id].backend
        if not isinstance(backend, session_type):
            raise AssertionError(f"application backend is not a VNCSession: {type(backend).__name__}")
        before_requests = len(fixture.framebuffer_requests)
        # Use the application-owned VNCSession for a same-stream protocol
        # barrier, without changing the 1s budget that the rejection tested.
        await asyncio.to_thread(backend.capture, "png", 80)
        if len(fixture.framebuffer_requests) <= before_requests:
            raise AssertionError("VNCSession capture returned without the fixture observing a framebuffer request")
        if _snapshot_events(fixture)[before:] != []:
            raise AssertionError("over-budget text focused or emitted a keyboard prefix")
        return len(fixture.framebuffer_requests)
    finally:
        await app.close()


async def _check_invalid(app: Any, fixture: Any, session_id: str, connection_id: str) -> int:
    before = len(_snapshot_events(fixture))
    invalid_calls: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
        ("NUL in send_text", lambda: app.run("send_text", "prefix\0tail", "keys", "ctrl-v", 4, 5, 0,
                                              session_id=session_id, connection_id=connection_id)),
        ("surrogate in send_text", lambda: app.run("send_text", "prefix\ud800", "keys", "ctrl-v", 4, 5, 0,
                                                    session_id=session_id, connection_id=connection_id)),
        ("control character in send_text", lambda: app.run("send_text", "prefix\x01", "keys", "ctrl-v", 4, 5, 0,
                                                            session_id=session_id, connection_id=connection_id)),
        ("NUL in type_text", lambda: app.run("type_text", "prefix\0tail", 0,
                                              session_id=session_id, connection_id=connection_id)),
        ("control character in type_text", lambda: app.run("type_text", "prefix\x01", 0,
                                                            session_id=session_id, connection_id=connection_id)),
        ("non-string text", lambda: app.run("send_text", None, "keys", "ctrl-v", 4, 5, 0,
                                             session_id=session_id, connection_id=connection_id)),
        ("invalid method", lambda: app.run("send_text", "prefix", "unknown", "ctrl-v", 4, 5, 0,
                                            session_id=session_id, connection_id=connection_id)),
        ("invalid delay type", lambda: app.run("send_text", "prefix", "keys", "ctrl-v", 4, 5, True,
                                                session_id=session_id, connection_id=connection_id)),
        ("invalid paste key", lambda: app.run("send_text", "prefix", "keys", "invalid", 4, 5, 0,
                                               session_id=session_id, connection_id=connection_id)),
        ("out-of-bounds focus", lambda: app.run("send_text", "prefix", "keys", "ctrl-v", -1, 5, 0,
                                                 session_id=session_id, connection_id=connection_id)),
        ("boolean focus coordinate", lambda: app.run("send_text", "prefix", "keys", "ctrl-v", True, 5, 0,
                                                      session_id=session_id, connection_id=connection_id)),
        ("incomplete focus coordinate pair", lambda: app.run("send_text", "prefix", "keys", "ctrl-v", 4, None, 0,
                                                               session_id=session_id, connection_id=connection_id)),
    )
    for label, operation in invalid_calls:
        await _expect_error(ValueError, operation, label)

    await _barrier(app, fixture, session_id, connection_id)
    events = _snapshot_events(fixture)[before:]
    if events:
        raise AssertionError(f"invalid text or focus arguments emitted input before rejection: {events!r}")
    status = await app.run("status", session_id=session_id, connection_id=connection_id)
    if not status.value.get("connected"):
        raise AssertionError("invalid input validation disconnected the usable fixture session")
    return len(invalid_calls)


def _random_texts(round_index: int, rng: random.Random, size: int = 76) -> tuple[str, str]:
    alphabet = "abcxyzABCXYZ019-=[]\\;',./!@#$%^&*()_+{}|:\"<>? \t\r\n"
    first = "".join(rng.choice(alphabet) for _ in range(size))
    second = "".join(rng.choice(alphabet) for _ in range(size))
    if first == second:
        second = second[:-1] + ("Q" if second[-1] != "Q" else "R")
    if len(first) != size or len(second) != size:
        raise AssertionError(f"round {round_index} generated an unexpected text length")
    return first, second


async def _check_parallel(app: Any, fixture_a: Any, fixture_b: Any,
                          session_a: str, connection_a: str,
                          session_b: str, connection_b: str) -> dict[str, int]:
    rng = random.Random(0x5EED_2026)
    rounds = 3
    characters_per_session = 0
    event_counts = {session_a: 0, session_b: 0}
    for round_index in range(rounds):
        text_a, text_b = _random_texts(round_index, rng)
        if text_a == text_b:
            raise AssertionError("parallel test inputs must be distinct")
        start_a = len(_snapshot_events(fixture_a))
        start_b = len(_snapshot_events(fixture_b))
        result_a, result_b = await asyncio.gather(
            app.run("type_text", text_a, 0, session_id=session_a, connection_id=connection_a),
            app.run("type_text", text_b, 0, session_id=session_b, connection_id=connection_b),
        )
        if result_a.value.get("typed_characters") != len(text_a):
            raise AssertionError(f"session {session_a} returned a wrong original text count")
        if result_b.value.get("typed_characters") != len(text_b):
            raise AssertionError(f"session {session_b} returned a wrong original text count")
        await asyncio.gather(
            _barrier(app, fixture_a, session_a, connection_a),
            _barrier(app, fixture_b, session_b, connection_b),
        )
        actual_a = _snapshot_events(fixture_a)[start_a:]
        actual_b = _snapshot_events(fixture_b)[start_b:]
        expected_a = _wire_for_text(text_a)
        expected_b = _wire_for_text(text_b)
        if actual_a != expected_a:
            raise AssertionError(f"round {round_index + 1} session {session_a} wire stream mismatch")
        if actual_b != expected_b:
            raise AssertionError(f"round {round_index + 1} session {session_b} wire stream mismatch")
        # Each fixture has its own listener and the full stream must match only
        # that session's separately generated text. Exact equality also catches
        # missing, duplicated, interleaved and cross-routed input.
        characters_per_session += len(text_a)
        event_counts[session_a] += len(actual_a)
        event_counts[session_b] += len(actual_b)
    return {"rounds": rounds, "characters_per_session": characters_per_session,
            "events_session_a": event_counts[session_a], "events_session_b": event_counts[session_b]}


async def _check_stale(app: Any, fixture: Any, session_id: str,
                       old_connection_id: str) -> str:
    # Reconnect the existing named session in place so its registry lease stays
    # live while the old generation is tested.
    reconnected = await app.connect("127.0.0.1", fixture.port, session_id=session_id)
    new_connection_id = reconnected.connection_id
    if not new_connection_id or new_connection_id == old_connection_id:
        raise AssertionError("reconnect did not produce a fresh connection_id")

    before = len(_snapshot_events(fixture))
    await _expect_error(
        ConnectionError,
        lambda: app.run("type_text", "stale", 0, session_id=session_id,
                        connection_id=old_connection_id),
        "input addressed with an old connection_id",
    )
    await app.run("key_down", "z", session_id=session_id, connection_id=new_connection_id)
    await app.run("key_up", "z", session_id=session_id, connection_id=new_connection_id)
    await _barrier(app, fixture, session_id, new_connection_id)
    expected = [("key", True, ord("z")), ("key", False, ord("z"))]
    actual = _snapshot_events(fixture)[before:]
    if actual != expected:
        raise AssertionError(f"stale connection_id emitted input or interrupted the valid barrier sequence: {actual!r}")
    return new_connection_id


async def _run(application_module: Any, config_module: Any, client_module: Any,
               fixture_type: Any, source: Path) -> dict[str, Any]:
    started = time.monotonic()
    checks = {name: False for name in CHECK_NAMES}
    failures: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"parallel_rounds": 0, "parallel_characters_per_session": 0,
                               "parallel_events_session_a": 0, "parallel_events_session_b": 0,
                               "invalid_cases": 0, "fixture_count": 0}
    fixture_a = fixture_type("keyboard-synthetic-a", 41)
    fixture_b = fixture_type("keyboard-synthetic-b", 193)
    fixture_budget = fixture_type("keyboard-synthetic-budget", 127)
    fixtures = (fixture_a, fixture_b, fixture_budget)
    app = None
    profile_path = Path(tempfile.gettempdir()) / f"vnc-keyboard-no-profile-{uuid.uuid4().hex}.toml"
    try:
        metrics["fixture_count"] = len(fixtures)
        if profile_path.exists():
            raise AssertionError("generated no-profile sentinel unexpectedly exists")
        app = _app(application_module, config_module, profiles_file=str(profile_path))
        connected_a, connected_b = await asyncio.gather(
            app.connect("127.0.0.1", fixture_a.port, session_id="keyboard-a"),
            app.connect("127.0.0.1", fixture_b.port, session_id="keyboard-b"),
        )
        connection_a, connection_b = connected_a.connection_id, connected_b.connection_id
        if not connection_a or not connection_b or connection_a == connection_b:
            raise AssertionError("the two named fixture sessions did not receive distinct connection_id values")
        metrics["named_sessions_connected"] = 2
        old_connection_a = connection_a

        async def run_check(name: str, operation: Callable[[], Awaitable[Any]]) -> Any:
            try:
                result = await operation()
                checks[name] = True
                return result
            except Exception as exc:
                failures.append({
                    "check": name,
                    "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                })
                return None

        newline_metrics = await run_check(
            CHECK_NAMES[0], lambda: _check_newlines(app, fixture_a, "keyboard-a", connection_a),
        )
        if newline_metrics is not None:
            metrics["newline_original_lengths"] = newline_metrics
        await run_check(CHECK_NAMES[1], lambda: _check_modifiers(app, fixture_a, "keyboard-a", connection_a))
        await run_check(
            CHECK_NAMES[2],
            lambda: _check_auto_text(app, fixture_a, client_module, application_module,
                                     "keyboard-a", connection_a, profile_path),
        )
        invalid_count = await run_check(
            CHECK_NAMES[4], lambda: _check_invalid(app, fixture_a, "keyboard-a", connection_a),
        )
        if invalid_count is not None:
            metrics["invalid_cases"] = invalid_count
        budget_count = await run_check(
            CHECK_NAMES[3],
            lambda: _check_budget(application_module, config_module, client_module.VNCSession, fixture_budget,
                                  "keyboard-budget", profile_path),
        )
        if budget_count is not None:
            metrics["budget_barrier_requests"] = budget_count
        parallel_metrics = await run_check(
            CHECK_NAMES[5],
            lambda: _check_parallel(app, fixture_a, fixture_b,
                                    "keyboard-a", connection_a, "keyboard-b", connection_b),
        )
        if parallel_metrics is not None:
            metrics.update({
                "parallel_rounds": parallel_metrics["rounds"],
                "parallel_characters_per_session": parallel_metrics["characters_per_session"],
                "parallel_events_session_a": parallel_metrics["events_session_a"],
                "parallel_events_session_b": parallel_metrics["events_session_b"],
            })
        fresh_connection_a = await run_check(
            CHECK_NAMES[6], lambda: _check_stale(app, fixture_a, "keyboard-a", old_connection_a),
        )
        if fresh_connection_a:
            metrics["reconnected_connection_id"] = True
    except Exception as exc:
        failures.append({
            "check": "setup_or_cleanup",
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })
    finally:
        if app is not None:
            try:
                await app.close()
            except Exception as exc:
                failures.append({
                    "check": "application_cleanup",
                    "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                })
        for fixture in fixtures:
            try:
                fixture.close()
            except Exception as exc:
                failures.append({
                    "check": "fixture_cleanup",
                    "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                })

    metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
    metrics["checks_passed"] = sum(checks.values())
    metrics["checks_total"] = len(checks)
    report: dict[str, Any] = {"success": not failures and all(checks.values()), "checks": checks, "metrics": metrics}
    if failures:
        report["failure"] = failures
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        application_module, config_module, client_module, fixture_type = _load(args.source.resolve())
        report = asyncio.run(_run(application_module, config_module, client_module,
                                  fixture_type, args.source.resolve()))
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "metrics": {"elapsed_seconds": round(time.monotonic() - started, 3),
                        "checks_passed": 0, "checks_total": 1},
            "failure": [{
                "check": "modules_load",
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
