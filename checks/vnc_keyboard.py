"""Synthetic RFB keyboard-input regressions; never connects to a real VM."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
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
    "normalized_newline_budget_refuses_before_key_input",
    "invalid_text_parameters_and_focus_send_no_prefix",
    "parallel_named_sessions_keep_complete_wire_streams_isolated",
    "stale_connection_id_is_rejected_without_input",
    "public_mcp_text_paths_reject_legacy_keys_delay_and_unnegotiated_paste",
    "public_cli_text_paths_reject_legacy_keys_delay_and_unnegotiated_paste",
    "public_cli_type_keys_preserves_exact_wire_and_result_contract",
    "public_mcp_entry_modes_preserve_exact_wire_and_result_contract",
    "public_cli_hex_entry_modes_preserve_exact_wire_and_result_contract",
    "hex_entry_modes_reject_controls_and_invalid_modes_without_input",
    "hex_expanded_key_budget_refuses_before_input",
    "parallel_named_hex_modes_keep_wire_streams_isolated",
    "public_status_keeps_unobserved_ime_and_leds_unknown",
    "public_caps_lock_guard_checks_reported_state_and_stops_without_replay",
)

HEX_SAMPLE = ("af\u4e2d\U0001f600\U0010ffff", ("0061", "0066", "4e2d", "1f600", "10ffff"))


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


def _clipboard_input_messages(fixture: Any) -> list[tuple[int, bytes]]:
    return [(length, data) for length, data in fixture.clipboard_messages
            if length >= 0 or int.from_bytes(data[:4], "big") & 0x01000000 == 0]


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


def _wire_for_hex(digits: tuple[str, ...], entry_mode: str) -> tuple[list[tuple[str, bool, int]], int]:
    strokes: list[tuple[int, ...]] = []
    for character in digits:
        if entry_mode == "gtk-hex":
            strokes.append((0xFFE3, 0xFFE1, ord("u")))
        strokes.extend((ord(digit),) for digit in character)
        if entry_mode == "richedit-hex":
            strokes.extend((0xFFE1, 0xFF51) for _ in character)
            strokes.append((0xFFE9, ord("x")))
        elif entry_mode == "gtk-hex":
            strokes.append((0xFF0D,))
        else:
            raise AssertionError(f"unsupported hex oracle mode: {entry_mode}")
    return [event for stroke in strokes for event in _wire_for_chord(stroke)], len(strokes)


def _assert_key_result(value: dict[str, Any], text: str, delay_ms: int,
                       entry_mode: str = "direct", key_strokes: int | None = None) -> None:
    expected = {
        "submitted_characters": len(text), "typed_characters": len(text),
        "effective_key_delay_ms": delay_ms, "transport": "keys", "input_method_sensitive": True,
        "application_verified": False, "paste_sent": False,
    }
    if entry_mode != "direct":
        expected.update(entry_mode=entry_mode, key_strokes=key_strokes)
    elif "entry_mode" in value or "key_strokes" in value:
        raise AssertionError(f"direct typing changed its result contract: {value}")
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise AssertionError(f"{entry_mode} typing result contract differs: {value}")


async def _mcp_value(server: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(tool, arguments)
    if isinstance(result, tuple):
        content, structured = result
        if isinstance(structured, dict):
            return structured
    else:
        content = result
    return json.loads(next(item.text for item in content if item.type == "text"))


async def _settle_cli_connection(fixture: Any, existing: set[Any]) -> None:
    deadline = time.monotonic() + 1
    while True:
        with fixture.lock:
            extra = fixture.connections - existing
        if not extra:
            return
        if time.monotonic() >= deadline:
            raise AssertionError("CLI left its synthetic RFB connection open")
        await asyncio.sleep(0.01)


async def _check_newlines(app: Any, fixture: Any, session_id: str, connection_id: str) -> dict[str, int]:
    cases = ("a\rb", "a\nb", "a\r\nb", "a\rb\r\nc\nd")
    before = len(_snapshot_events(fixture))
    results: dict[str, int] = {}
    for text in cases:
        result = await app.run("type_text", text, 0,
                               session_id=session_id, connection_id=connection_id)
        value = result.value
        if value.get("typed_characters") != len(text):
            raise AssertionError(f"typed_characters did not preserve original length for {text!r}: {value}")
        if value.get("submitted_characters") != len(text):
            raise AssertionError(f"submitted_characters did not preserve original length for {text!r}: {value}")
        if (value.get("transport"), value.get("input_method_sensitive"),
                value.get("application_verified"), value.get("paste_sent")) != ("keys", True, False, False):
            raise AssertionError(f"physical typing result contract differs: {value}")
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
    explicit = await app.run("type_text", typed, 0,
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
            lambda: app.run("type_text", "\r" * 60, 20,
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
            raise AssertionError("over-budget physical text emitted an input prefix")
        return len(fixture.framebuffer_requests)
    finally:
        await app.close()


async def _check_invalid(app: Any, fixture: Any, session_id: str, connection_id: str) -> int:
    before = len(_snapshot_events(fixture))
    clipboard_before = _clipboard_input_messages(fixture)
    invalid_calls: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
        ("NUL in send_text", lambda: app.run("send_text", "prefix\0tail", "auto", "ctrl-v", 4, 5, 0,
                                              session_id=session_id, connection_id=connection_id)),
        ("surrogate in send_text", lambda: app.run("send_text", "prefix\ud800", "auto", "ctrl-v", 4, 5, 0,
                                                    session_id=session_id, connection_id=connection_id)),
        ("legacy keys in send_text", lambda: app.run("send_text", "prefix", "keys", "ctrl-v", 4, 5, 0,
                                                       session_id=session_id, connection_id=connection_id)),
        ("NUL in type_text", lambda: app.run("type_text", "prefix\0tail", 0,
                                              session_id=session_id, connection_id=connection_id)),
        ("control character in type_text", lambda: app.run("type_text", "prefix\x01", 0,
                                                            session_id=session_id, connection_id=connection_id)),
        ("surrogate in type_text", lambda: app.run("type_text", "prefix\ud800", 0,
                                                    session_id=session_id, connection_id=connection_id)),
        ("non-string physical text", lambda: app.run("type_text", None, 0,
                                                       session_id=session_id, connection_id=connection_id)),
        ("boolean physical delay", lambda: app.run("type_text", "prefix", True,
                                                      session_id=session_id, connection_id=connection_id)),
        ("fractional physical delay", lambda: app.run("type_text", "prefix", 1.5,
                                                         session_id=session_id, connection_id=connection_id)),
        ("invalid physical entry mode", lambda: app.run("type_text", "prefix", 0, "unknown",
                                                          session_id=session_id, connection_id=connection_id)),
        ("non-boolean Caps Lock guard", lambda: app.run("type_text", "prefix", 0, "direct", 1,
                                                          session_id=session_id, connection_id=connection_id)),
        ("newline in RichEdit entry", lambda: app.run("type_text", "af\ntail", 0, "richedit-hex",
                                                        session_id=session_id, connection_id=connection_id)),
        ("tab in GTK entry", lambda: app.run("type_text", "af\ttail", 0, "gtk-hex",
                                               session_id=session_id, connection_id=connection_id)),
        ("non-string text", lambda: app.run("send_text", None, "auto", "ctrl-v", 4, 5, 0,
                                             session_id=session_id, connection_id=connection_id)),
        ("invalid method", lambda: app.run("send_text", "prefix", "unknown", "ctrl-v", 4, 5, 0,
                                            session_id=session_id, connection_id=connection_id)),
        ("invalid delay type", lambda: app.run("send_text", "prefix", "auto", "ctrl-v", 4, 5, True,
                                                session_id=session_id, connection_id=connection_id)),
        ("invalid paste key", lambda: app.run("send_text", "prefix", "auto", "invalid", 4, 5, 0,
                                               session_id=session_id, connection_id=connection_id)),
        ("out-of-bounds focus", lambda: app.run("send_text", "prefix", "auto", "ctrl-v", -1, 5, 0,
                                                 session_id=session_id, connection_id=connection_id)),
        ("boolean focus coordinate", lambda: app.run("send_text", "prefix", "auto", "ctrl-v", True, 5, 0,
                                                      session_id=session_id, connection_id=connection_id)),
        ("incomplete focus coordinate pair", lambda: app.run("send_text", "prefix", "auto", "ctrl-v", 4, None, 0,
                                                               session_id=session_id, connection_id=connection_id)),
    )
    for label, operation in invalid_calls:
        await _expect_error(ValueError, operation, label)

    await _barrier(app, fixture, session_id, connection_id)
    events = _snapshot_events(fixture)[before:]
    if events:
        raise AssertionError(f"invalid text or focus arguments emitted input before rejection: {events!r}")
    if _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("invalid text or focus arguments modified the clipboard before rejection")
    status = await app.run("status", session_id=session_id, connection_id=connection_id)
    if not status.value.get("connected"):
        raise AssertionError("invalid input validation disconnected the usable fixture session")
    return len(invalid_calls)


async def _check_public_mcp(app: Any, fixture: Any, session_id: str, connection_id: str,
                            negotiated: bool = False) -> int:
    server = importlib.import_module("vmware_vnc.server").create_server(application=app)
    tools = {tool.name: tool for tool in await server.list_tools()}
    if "vnc_type_keys" not in tools:
        raise AssertionError("the public physical key tool is missing")
    properties = tools["vnc_type_keys"].inputSchema["properties"]
    if set(properties) != {"text", "delay_ms", "connection_id", "session_id", "entry_mode", "expect_caps_lock"}:
        raise AssertionError(f"physical key tool exposes unexpected arguments: {properties}")
    if (properties["entry_mode"].get("enum"), properties["entry_mode"].get("default")) != (
            ["direct", "richedit-hex", "gtk-hex"], "direct"):
        raise AssertionError(f"physical key tool exposes an unexpected entry-mode schema: {properties['entry_mode']}")
    before = _snapshot_events(fixture)
    clipboard_before = _clipboard_input_messages(fixture)
    count = 0
    for tool in ("vnc_text", "vnc_type"):
        cases = [
            {"method": "keys"},
            {"method": "auto", "delay_ms": 25},
            {"method": "clipboard", "delay_ms": 25},
            {"method": "guest", "delay_ms": 25},
        ]
        if not negotiated:
            cases.append({"method": "clipboard"})
        for options in cases:
            await _expect_error(
                Exception,
                lambda tool=tool, options=options: server.call_tool(tool, {
                    "text": "ASCII-paste", "focus_x": 4, "focus_y": 5,
                    "session_id": session_id, "connection_id": connection_id, **options,
                }),
                f"{tool} legacy or unnegotiated text route {options}",
            )
            count += 1
    await _expect_error(
        Exception,
        lambda: server.call_tool("vnc_type_keys", {
            "text": "must-not-type", "focus_x": 4, "focus_y": 5,
            "session_id": session_id, "connection_id": connection_id,
        }),
        "vnc_type_keys with unsupported focus coordinates",
    )
    count += 1
    await _barrier(app, fixture, session_id, connection_id)
    if _snapshot_events(fixture) != before or _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("a rejected public MCP text route sent pointer, key or clipboard data")
    return count


async def _cli(source: Path, *arguments: str) -> tuple[int, str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("VNC_")}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(source / "main.py"), *arguments,
        cwd=str(source), env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    return process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")


async def _check_public_cli(source: Path, fixture: Any, profile_path: Path, negotiated: bool = False) -> int:
    before = _snapshot_events(fixture)
    clipboard_before = _clipboard_input_messages(fixture)
    count = 0
    for command in ("text", "type"):
        cases = [
            ("--method", "keys"),
            ("--method", "auto", "--delay-ms", "25"),
            ("--method", "clipboard", "--delay-ms", "25"),
            ("--method", "guest", "--delay-ms", "25"),
        ]
        if not negotiated:
            cases.append(("--method", "clipboard"))
        for options in cases:
            code, stdout, stderr = await _cli(
                source, command, "ASCII-paste", "--host", "127.0.0.1", "--port", str(fixture.port),
                "--profiles-file", str(profile_path), "--focus", "4", "5", *options,
            )
            if code == 0 or stdout.strip():
                raise AssertionError(f"CLI {command} accepted {options}: code={code}, stdout={stdout!r}, stderr={stderr!r}")
            if not stderr.strip():
                raise AssertionError(f"CLI {command} rejection did not explain the invalid route")
            count += 1
    if _snapshot_events(fixture) != before or _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("a rejected public CLI text route sent pointer, key or clipboard data")
    return count


async def _check_cli_type_keys(source: Path, fixture: Any, profile_path: Path) -> None:
    text = "A!z\r\n\tq"
    clipboard_before = list(fixture.clipboard_messages)
    for options in ((), ("--entry-mode", "direct")):
        before = len(_snapshot_events(fixture))
        with fixture.lock:
            connections_before = set(fixture.connections)
        code, stdout, stderr = await _cli(
            source, "type-keys", text, "--delay-ms", "25", "--host", "127.0.0.1", "--port", str(fixture.port),
            "--profiles-file", str(profile_path), *options,
        )
        await _settle_cli_connection(fixture, connections_before)
        if code != 0:
            raise AssertionError(f"CLI type-keys failed: {stderr}")
        _assert_key_result(json.loads(stdout), text, 25)
        if _snapshot_events(fixture)[before:] != _wire_for_text(text):
            raise AssertionError("CLI type-keys emitted a different physical wire sequence")
    if fixture.clipboard_messages != clipboard_before:
        raise AssertionError("CLI type-keys touched the clipboard")
    rejected_before = _snapshot_events(fixture)
    code, _, _ = await _cli(
        source, "type-keys", "must-not-type", "--host", "127.0.0.1", "--port", str(fixture.port),
        "--focus", "4", "5",
    )
    if code == 0 or _snapshot_events(fixture) != rejected_before:
        raise AssertionError("CLI type-keys accepted focus coordinates or sent input before rejection")


async def _check_mcp_entry_modes(app: Any, fixture: Any, session_id: str,
                                 connection_id: str) -> dict[str, int]:
    server = importlib.import_module("vmware_vnc.server").create_server(application=app)
    clipboard_before = _clipboard_input_messages(fixture)
    text, digits = HEX_SAMPLE
    cases = [
        ("A!z\r\n\tq", {}, _wire_for_text("A!z\r\n\tq"), None),
        ("A!z\r\n\tq", {"entry_mode": "direct"}, _wire_for_text("A!z\r\n\tq"), None),
    ]
    for entry_mode in ("richedit-hex", "gtk-hex"):
        wire, strokes = _wire_for_hex(digits, entry_mode)
        cases.append((text, {"entry_mode": entry_mode}, wire, strokes))
    for text, options, expected, strokes in cases:
        before = len(_snapshot_events(fixture))
        requests_before = len(fixture.framebuffer_requests)
        result = await _mcp_value(server, "vnc_type_keys", {
            "text": text, "session_id": session_id, "connection_id": connection_id, **options,
        })
        _assert_key_result(result, text, 20, options.get("entry_mode", "direct"), strokes)
        if len(fixture.framebuffer_requests) != requests_before:
            raise AssertionError("physical key operation requested a framebuffer during typing")
        await _barrier(app, fixture, session_id, connection_id)
        actual = _snapshot_events(fixture)[before:]
        if actual != expected:
            raise AssertionError(f"MCP {options} wire differs: expected {expected!r}, got {actual!r}")
    if _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("MCP physical key modes sent clipboard data despite a negotiated clipboard")
    return {"cases": len(cases), "hex_key_strokes": sum(case[3] or 0 for case in cases)}


async def _check_cli_hex(source: Path, fixture: Any, profile_path: Path) -> int:
    text, digits = HEX_SAMPLE
    clipboard_before = _clipboard_input_messages(fixture)
    for entry_mode in ("richedit-hex", "gtk-hex"):
        expected, strokes = _wire_for_hex(digits, entry_mode)
        before = len(_snapshot_events(fixture))
        requests_before = len(fixture.framebuffer_requests)
        with fixture.lock:
            connections_before = set(fixture.connections)
        code, stdout, stderr = await _cli(
            source, "type-keys", text, "--entry-mode", entry_mode, "--screenshot-mode", "refresh",
            "--host", "127.0.0.1", "--port", str(fixture.port), "--profiles-file", str(profile_path),
        )
        await _settle_cli_connection(fixture, connections_before)
        if code != 0:
            raise AssertionError(f"CLI {entry_mode} failed: {stderr}")
        _assert_key_result(json.loads(stdout), text, 20, entry_mode, strokes)
        if _snapshot_events(fixture)[before:] != expected:
            raise AssertionError(f"CLI {entry_mode} emitted an unexpected physical wire sequence")
        if len(fixture.framebuffer_requests) - requests_before != 1:
            raise AssertionError(f"CLI {entry_mode} performed a framebuffer read after initial connection")
    if _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("CLI hex entry sent clipboard data despite a negotiated clipboard")
    return 2


async def _check_hex_invalid(app: Any, fixture: Any, source: Path, profile_path: Path,
                             session_id: str, connection_id: str) -> dict[str, int]:
    server = importlib.import_module("vmware_vnc.server").create_server(application=app)
    before = _snapshot_events(fixture)
    clipboard_before = _clipboard_input_messages(fixture)
    controls = ("\n", "\r", "\t", "\x01", "\x7f", "\x85")
    mcp_count = 0
    for entry_mode in ("richedit-hex", "gtk-hex"):
        for control in controls:
            await _expect_error(Exception, lambda entry_mode=entry_mode, control=control: server.call_tool(
                "vnc_type_keys", {"text": "af" + control + "tail", "entry_mode": entry_mode,
                                  "session_id": session_id, "connection_id": connection_id},
            ), f"MCP {entry_mode} with control {control!r}")
            mcp_count += 1
    for mode in ("unknown", "DIRECT", None, 1):
        await _expect_error(Exception, lambda mode=mode: server.call_tool(
            "vnc_type_keys", {"text": "af", "entry_mode": mode,
                              "session_id": session_id, "connection_id": connection_id},
        ), f"MCP invalid entry_mode {mode!r}")
        mcp_count += 1
    cli_cases = [("af", ("--entry-mode", "unknown"))]
    cli_cases.extend(("af" + control + "tail", ("--entry-mode", entry_mode))
                     for entry_mode in ("richedit-hex", "gtk-hex") for control in ("\n", "\t"))
    for text, options in cli_cases:
        with fixture.lock:
            connections_before = set(fixture.connections)
        code, stdout, stderr = await _cli(
            source, "type-keys", text, "--host", "127.0.0.1", "--port", str(fixture.port),
            "--profiles-file", str(profile_path), *options,
        )
        await _settle_cli_connection(fixture, connections_before)
        if code == 0 or stdout.strip() or not stderr.strip():
            raise AssertionError(f"CLI accepted invalid hex text/mode: code={code}, stdout={stdout!r}, stderr={stderr!r}")
    await _barrier(app, fixture, session_id, connection_id)
    if _snapshot_events(fixture) != before or _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("invalid hex text/mode emitted an input prefix or clipboard data")
    return {"mcp": mcp_count, "cli": len(cli_cases)}


async def _check_hex_budget(application_module: Any, config_module: Any, fixture: Any,
                            source: Path, profile_path: Path) -> int:
    app = _app(application_module, config_module, action_timeout=1.0, profiles_file=str(profile_path))
    try:
        connected = await app.connect("127.0.0.1", fixture.port, session_id="keyboard-hex-budget")
        connection_id = connected.connection_id
        server = importlib.import_module("vmware_vnc.server").create_server(application=app)
        before = _snapshot_events(fixture)
        clipboard_before = _clipboard_input_messages(fixture)
        text = "\u4e2d" * 12
        for entry_mode in ("richedit-hex", "gtk-hex"):
            error = await _expect_error(Exception, lambda entry_mode=entry_mode: server.call_tool(
                "vnc_type_keys", {"text": text, "delay_ms": 20, "entry_mode": entry_mode,
                                  "session_id": "keyboard-hex-budget", "connection_id": connection_id},
            ), f"MCP {entry_mode} expanded key budget")
            if "budget" not in str(error).lower():
                raise AssertionError(f"MCP expanded key budget failed for another reason: {error}")
            with fixture.lock:
                connections_before = set(fixture.connections)
            code, stdout, stderr = await _cli(
                source, "type-keys", text, "--entry-mode", entry_mode, "--action-timeout", "1",
                "--host", "127.0.0.1", "--port", str(fixture.port), "--profiles-file", str(profile_path),
            )
            await _settle_cli_connection(fixture, connections_before)
            if code == 0 or stdout.strip() or "budget" not in stderr.lower():
                raise AssertionError(f"CLI expanded key budget failed incorrectly: code={code}, stdout={stdout!r}, stderr={stderr!r}")
        await _barrier(app, fixture, "keyboard-hex-budget", connection_id)
        if _snapshot_events(fixture) != before or _clipboard_input_messages(fixture) != clipboard_before:
            raise AssertionError("over-budget expanded key input emitted a prefix or clipboard data")
        return 4
    finally:
        await app.close()


async def _check_hex_parallel(app: Any, fixture_a: Any, fixture_b: Any,
                              session_a: str, connection_a: str,
                              session_b: str, connection_b: str) -> int:
    server = importlib.import_module("vmware_vnc.server").create_server(application=app)
    cases = (
        (fixture_a, session_a, connection_a, "richedit-hex", "af\u4e2d", ("0061", "0066", "4e2d")),
        (fixture_b, session_b, connection_b, "gtk-hex", "f0\U0001f600", ("0066", "0030", "1f600")),
    )
    starts = [len(_snapshot_events(fixture)) for fixture, *_ in cases]
    results = await asyncio.gather(*(_mcp_value(server, "vnc_type_keys", {
        "text": text, "entry_mode": entry_mode, "session_id": session_id, "connection_id": connection_id,
    }) for _, session_id, connection_id, entry_mode, text, _ in cases))
    await asyncio.gather(*(_barrier(app, fixture, session_id, connection_id)
                           for fixture, session_id, connection_id, *_ in cases))
    for (fixture, _, _, entry_mode, text, digits), start, result in zip(cases, starts, results):
        expected, strokes = _wire_for_hex(digits, entry_mode)
        _assert_key_result(result, text, 20, entry_mode, strokes)
        if _snapshot_events(fixture)[start:] != expected:
            raise AssertionError(f"parallel {entry_mode} input was missing, duplicated or routed across sessions")
    return len(cases)


async def _check_keyboard_status(app: Any, fixture: Any, source: Path, profile_path: Path,
                                 session_id: str) -> None:
    server = importlib.import_module("vmware_vnc.server").create_server(application=app)
    requests_before = len(fixture.framebuffer_requests)
    status = await _mcp_value(server, "vnc_status", {"session_id": session_id})
    if len(fixture.framebuffer_requests) != requests_before:
        raise AssertionError("MCP status captured the screen to infer keyboard state")
    code, stdout, stderr = await _cli(
        source, "status", "--host", "127.0.0.1", "--port", str(fixture.port),
        "--profiles-file", str(profile_path),
    )
    if code != 0:
        raise AssertionError(f"CLI keyboard status failed: {stderr}")
    for value in (status, json.loads(stdout)):
        state = value.get("keyboard_state", {})
        if not {"ime_mode", "leds"}.issubset(state) or state["ime_mode"] is not None or state["leds"] is not None:
            raise AssertionError(f"unobserved keyboard state was inferred: {value}")


async def _check_caps_lock_guard(app: Any, fixture: Any, source: Path, profile_path: Path,
                                 session_id: str, connection_id: str) -> dict[str, int]:
    server = importlib.import_module("vmware_vnc.server").create_server(application=app)
    backend = app.registry._entries[session_id].backend
    client_type = type(backend.client)
    arguments = {"text": "af", "session_id": session_id, "connection_id": connection_id}
    before = _snapshot_events(fixture)
    clipboard_before = _clipboard_input_messages(fixture)
    rejected = 0
    for mode in ("direct", "richedit-hex", "gtk-hex"):
        for expected in (False, True):
            await _expect_error(Exception, lambda mode=mode, expected=expected: server.call_tool(
                "vnc_type_keys", {**arguments, "entry_mode": mode, "expect_caps_lock": expected},
            ), f"MCP {mode} Caps Lock guard without a report")
            rejected += 1
    for invalid in (0, 1, "false"):
        await _expect_error(Exception, lambda invalid=invalid: server.call_tool(
            "vnc_type_keys", {**arguments, "expect_caps_lock": invalid},
        ), f"MCP non-boolean Caps Lock expectation {invalid!r}")
        rejected += 1
    for reported in (False, True):
        leds = {"source": "vmware", "caps_lock": reported, "num_lock": False, "scroll_lock": False}
        with mock.patch.object(client_type, "keyboard_leds", new_callable=mock.PropertyMock, return_value=leds):
            await _expect_error(Exception, lambda reported=reported: server.call_tool(
                "vnc_type_keys", {**arguments, "expect_caps_lock": not reported},
            ), f"MCP Caps Lock guard opposite a reported {reported}")
            rejected += 1
    for expected in ("off", "on", "false"):
        with fixture.lock:
            connections_before = set(fixture.connections)
        code, stdout, stderr = await _cli(
            source, "type-keys", "af", "--expect-caps-lock", expected,
            "--host", "127.0.0.1", "--port", str(fixture.port), "--profiles-file", str(profile_path),
        )
        await _settle_cli_connection(fixture, connections_before)
        if code == 0 or stdout.strip() or not stderr.strip():
            raise AssertionError(f"CLI accepted unknown/invalid Caps Lock state {expected!r}: {code}, {stdout!r}, {stderr!r}")
        rejected += 1
    await _barrier(app, fixture, session_id, connection_id)
    if _snapshot_events(fixture) != before:
        raise AssertionError("a rejected Caps Lock guard emitted an input prefix")

    matching_cases = ((False, "direct", "a"), (True, "direct", "b"),
                      (False, "richedit-hex", "c"), (False, "gtk-hex", "d"))
    for reported, mode, text in matching_cases:
        leds = {"source": "vmware", "caps_lock": reported, "num_lock": False, "scroll_lock": False}
        start = len(_snapshot_events(fixture))
        with mock.patch.object(client_type, "keyboard_leds", new_callable=mock.PropertyMock, return_value=leds):
            result = await _mcp_value(server, "vnc_type_keys", {
                **arguments, "text": text, "entry_mode": mode, "expect_caps_lock": reported,
            })
        expected, strokes = (_wire_for_text(text), None) if mode == "direct" else _wire_for_hex(
            ("0063",) if text == "c" else ("0064",), mode,
        )
        _assert_key_result(result, text, 20, mode, strokes)
        await _barrier(app, fixture, session_id, connection_id)
        if _snapshot_events(fixture)[start:] != expected:
            raise AssertionError(f"matching Caps Lock guard changed {mode} input")

    for mode in ("direct", "richedit-hex", "gtk-hex"):
        start = len(_snapshot_events(fixture))
        leds = {"source": "vmware", "caps_lock": False, "num_lock": False, "scroll_lock": False}
        send = backend._call

        def send_and_report(method: str, *args: Any) -> Any:
            result = send(method, *args)
            if method in ("type_batch", "key_up"):
                leds["caps_lock"] = True
            return result

        with mock.patch.object(client_type, "keyboard_leds", new_callable=mock.PropertyMock,
                               side_effect=lambda: dict(leds)), mock.patch.object(
            backend, "_call", side_effect=send_and_report,
        ):
            error = await _expect_error(Exception, lambda mode=mode: server.call_tool(
                "vnc_type_keys", {**arguments, "text": "abcd", "entry_mode": mode, "expect_caps_lock": False},
            ), f"MCP {mode} Caps Lock changed after the first key group")
        if "partial" not in str(error).lower() or "replay" not in str(error).lower():
            raise AssertionError(f"mid-batch Caps Lock failure hid partial input: {error}")
        expected = (_wire_for_text("a") if mode == "direct" else _wire_for_text("0")
                    if mode == "richedit-hex" else _wire_for_chord((0xFFE3, 0xFFE1, ord("u"))))
        reconnected = await app.connect("127.0.0.1", fixture.port, session_id=session_id)
        connection_id = reconnected.connection_id
        arguments["connection_id"] = connection_id
        backend = app.registry._entries[session_id].backend
        await _barrier(app, fixture, session_id, connection_id)
        if _snapshot_events(fixture)[start:] != expected:
            raise AssertionError(f"Caps Lock changed but {mode} sent more than the completed first key group")
        await _mcp_value(server, "vnc_type_keys", {**arguments, "text": "z"})
        await _barrier(app, fixture, session_id, connection_id)
        if _snapshot_events(fixture)[start:] != expected + _wire_for_text("z"):
            raise AssertionError(f"{mode} replayed partial input before a later explicit action")
    if _clipboard_input_messages(fixture) != clipboard_before:
        raise AssertionError("Caps Lock guard tests unexpectedly sent clipboard data")
    return {"rejected": rejected, "matching": len(matching_cases), "partial": 3}


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
    for entry_mode in ("richedit-hex", "gtk-hex"):
        await _expect_error(
            ConnectionError,
            lambda entry_mode=entry_mode: app.run("type_text", "af", 0, entry_mode,
                                                session_id=session_id, connection_id=old_connection_id),
            f"{entry_mode} input addressed with an old connection_id",
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
    fixture_literal = fixture_type("keyboard-synthetic-literal", 139, extended_clipboard=True)
    fixtures = (fixture_a, fixture_b, fixture_budget, fixture_literal)
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
                checks[name] = not any(failure["check"] == name for failure in failures)
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
        literal_connection = await app.connect("127.0.0.1", fixture_literal.port, session_id="keyboard-literal")
        backend = app.registry._entries["keyboard-literal"].backend
        if not backend.client.ext_clipboard.supported:
            raise AssertionError("invalid-parameter test did not negotiate a usable literal text transport")
        invalid_count = await run_check(
            CHECK_NAMES[4], lambda: _check_invalid(app, fixture_literal, "keyboard-literal", literal_connection.connection_id),
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
        metrics["public_mcp_rejected_cases"] = await run_check(
            CHECK_NAMES[7], lambda: _check_public_mcp(app, fixture_a, "keyboard-a", connection_a),
        )
        negotiated_mcp = await run_check(
            CHECK_NAMES[7], lambda: _check_public_mcp(
                app, fixture_literal, "keyboard-literal", literal_connection.connection_id, negotiated=True,
            ),
        )
        if negotiated_mcp is not None and metrics["public_mcp_rejected_cases"] is not None:
            metrics["public_mcp_rejected_cases"] += negotiated_mcp
        metrics["public_cli_rejected_cases"] = await run_check(
            CHECK_NAMES[8], lambda: _check_public_cli(source, fixture_b, profile_path),
        )
        negotiated_cli = await run_check(
            CHECK_NAMES[8], lambda: _check_public_cli(source, fixture_literal, profile_path, negotiated=True),
        )
        if negotiated_cli is not None and metrics["public_cli_rejected_cases"] is not None:
            metrics["public_cli_rejected_cases"] += negotiated_cli
        await run_check(CHECK_NAMES[9], lambda: _check_cli_type_keys(source, fixture_b, profile_path))
        metrics["public_mcp_entry_modes"] = await run_check(
            CHECK_NAMES[10], lambda: _check_mcp_entry_modes(
                app, fixture_literal, "keyboard-literal", literal_connection.connection_id,
            ),
        )
        metrics["public_cli_hex_modes"] = await run_check(
            CHECK_NAMES[11], lambda: _check_cli_hex(source, fixture_literal, profile_path),
        )
        metrics["public_hex_invalid_cases"] = await run_check(
            CHECK_NAMES[12], lambda: _check_hex_invalid(
                app, fixture_literal, source, profile_path, "keyboard-literal", literal_connection.connection_id,
            ),
        )
        metrics["public_hex_budget_cases"] = await run_check(
            CHECK_NAMES[13], lambda: _check_hex_budget(
                application_module, config_module, fixture_budget, source, profile_path,
            ),
        )
        metrics["parallel_hex_sessions"] = await run_check(
            CHECK_NAMES[14], lambda: _check_hex_parallel(
                app, fixture_a, fixture_b, "keyboard-a", connection_a, "keyboard-b", connection_b,
            ),
        )
        await run_check(
            CHECK_NAMES[15], lambda: _check_keyboard_status(app, fixture_a, source, profile_path, "keyboard-a"),
        )
        metrics["public_caps_lock_guard_cases"] = await run_check(
            CHECK_NAMES[16], lambda: _check_caps_lock_guard(
                app, fixture_literal, source, profile_path, "keyboard-literal", literal_connection.connection_id,
            ),
        )
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
