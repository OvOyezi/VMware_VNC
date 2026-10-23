"""Standard-library checks for clipboard and guest-text extensions."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import unittest
from types import SimpleNamespace
from unittest import mock
import zlib


_clipboard = None
_guest = None
_client = None
_operations = None
_linux_helper = None


def _payload_of_wire_message(wire_message: bytes) -> bytes:
    if len(wire_message) < 8 or wire_message[0] != 6:
        raise AssertionError("fixture did not produce an RFB ServerCutText message")
    length, = struct.unpack_from("!i", wire_message, 4)
    if length >= 0 or len(wire_message) != 8 + abs(length):
        raise AssertionError("fixture did not produce a complete extended clipboard message")
    return wire_message[8:]


def _capabilities_payload(text_limit: int, actions: int | None = None) -> bytes:
    if actions is None:
        actions = _clipboard.REQUEST | _clipboard.NOTIFY | _clipboard.PROVIDE
    flags = _clipboard.CAPS | _clipboard.TEXT | actions
    return struct.pack("!I", flags) + struct.pack("!I", text_limit)


def _provide_payload(value: bytes, trailing_compressed: bytes = b"") -> bytes:
    raw = struct.pack("!I", len(value)) + value
    return struct.pack("!I", _clipboard.PROVIDE | _clipboard.TEXT) + zlib.compress(raw) + trailing_compressed


def _provide_syncflush_payload(value: bytes, extra_decoded: bytes = b"") -> bytes:
    raw = struct.pack("!I", len(value)) + value + extra_decoded
    compressor = zlib.compressobj()
    compressed = compressor.compress(raw) + compressor.flush(zlib.Z_SYNC_FLUSH)
    return struct.pack("!I", _clipboard.PROVIDE | _clipboard.TEXT) + compressed


class LoopbackRfbClipboardFixture:
    """Small loopback-only RFB server that exercises the real VNC client protocol."""

    width = 8
    height = 8
    pixel_format = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)

    def __init__(self, remote_text: str, attack: bool = False,
                 request_on_paste: bool = False, text_limit: int | None = None,
                 width: int = 8, height: int = 8):
        self.remote_text = remote_text
        self.attack = attack
        self.request_on_paste = request_on_paste
        self.text_limit = text_limit if text_limit is not None else _clipboard.MAX_CLIPBOARD_BYTES
        self.width = width
        self.height = height
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.connection = None
        self.stop = threading.Event()
        self.attack_requested = threading.Event()
        self.attack_armed = threading.Event()
        self.attack_sent = threading.Event()
        self.client_caps_seen = threading.Event()
        self.client_notify_seen = threading.Event()
        self.request_seen = threading.Event()
        self.paste_key_seen = threading.Event()
        self.provide_sent = threading.Event()
        self.client_provide_seen = threading.Event()
        self.provided_wire_text = None
        self.events = []
        self.errors = []
        self._expected_disconnect = False
        self.thread = threading.Thread(target=self._serve, name="synthetic-rfb", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _receive(self, count: int, deadline: float) -> bytes:
        if count < 0:
            raise ValueError("fixture receive size must not be negative")
        data = bytearray()
        while len(data) < count:
            if self.stop.is_set():
                raise EOFError
            if time.monotonic() >= deadline:
                raise TimeoutError("loopback RFB fixture timed out")
            try:
                chunk = self.connection.recv(count - len(data))
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)

    def _send_cut_text(self, payload: bytes) -> None:
        self.connection.sendall(b"\x03\x00\x00\x00" + struct.pack(">i", -len(payload)) + payload)

    def _send_caps(self) -> None:
        flags = _clipboard.CAPS | _clipboard.TEXT | _clipboard.REQUEST | _clipboard.NOTIFY | _clipboard.PROVIDE
        self.events.append({"kind": "server_caps", "flags": flags})
        self._send_cut_text(struct.pack("!II", flags, self.text_limit))

    def _send_notify(self) -> None:
        flags = _clipboard.NOTIFY | _clipboard.TEXT
        self.events.append({"kind": "server_notify", "flags": flags})
        self._send_cut_text(struct.pack("!I", flags))

    def _send_provide(self) -> None:
        value = _clipboard.wire_text(self.remote_text)
        raw = struct.pack("!I", len(value)) + value
        payload = struct.pack("!I", _clipboard.PROVIDE | _clipboard.TEXT) + zlib.compress(raw)
        self.events.append({"kind": "server_provide", "characters": len(self.remote_text)})
        self._send_cut_text(payload)
        self.provide_sent.set()

    def _send_request(self) -> None:
        flags = _clipboard.REQUEST | _clipboard.TEXT
        self.events.append({"kind": "server_request", "flags": flags})
        self._send_cut_text(struct.pack("!I", flags))
        self.request_seen.set()

    def _send_oversized_header(self) -> None:
        self._expected_disconnect = True
        length = _clipboard.MAX_CLIPBOARD_BYTES + 1
        self.events.append({"kind": "server_oversized_clipboard", "bytes": length})
        self.connection.sendall(b"\x03\x00\x00\x00" + struct.pack(">i", -length))
        self.attack_sent.set()

    def _send_frame(self) -> None:
        update = b"\x00\x00\x00\x01"
        update += struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
        update += b"\x00" * (self.width * self.height * 4)
        self.connection.sendall(update)

    def _receive_client_cut_text(self, deadline: float) -> None:
        length, = struct.unpack(">3xi", self._receive(7, deadline))
        if abs(length) > _clipboard.MAX_CLIPBOARD_BYTES:
            raise ValueError("client clipboard message exceeded the fixture limit")
        data = self._receive(abs(length), deadline)
        if length >= 0:
            self.events.append({"kind": "clipboard", "bytes": len(data)})
            return
        flags, = struct.unpack_from("!I", data)
        actions = flags & 0xFF000000
        self.events.append({"kind": "extended_clipboard", "flags": flags})
        if actions & _clipboard.CAPS:
            self.client_caps_seen.set()
        if actions == _clipboard.REQUEST:
            self.request_seen.set()
            self._send_provide()
        elif actions == _clipboard.NOTIFY:
            self.client_notify_seen.set()
            self.events.append({"kind": "client_notify", "flags": flags})
        elif actions == _clipboard.PROVIDE:
            try:
                raw = zlib.decompress(data[4:])
                size, = struct.unpack_from("!I", raw)
                self.provided_wire_text = raw[4:4 + size]
                if len(self.provided_wire_text) != size:
                    raise ValueError("client clipboard Provide payload was truncated")
            except (zlib.error, struct.error) as exc:
                raise ValueError("client clipboard Provide payload could not be decoded") from exc
            self.client_provide_seen.set()

    def _serve(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    self.connection, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if self.connection is None:
                return
            self.connection.settimeout(0.2)
            deadline = time.monotonic() + 12
            self.connection.sendall(b"RFB 003.008\n")
            if self._receive(12, deadline) != b"RFB 003.008\n":
                raise ValueError("unexpected RFB version from client")
            self.connection.sendall(b"\x01\x01")
            if self._receive(1, deadline) != b"\x01":
                raise ValueError("client did not select the fixture's no-auth mode")
            self.connection.sendall(b"\x00\x00\x00\x00")
            self._receive(1, deadline)  # ClientInit shared flag.
            name = b"Synthetic extended clipboard fixture"
            server_init = struct.pack(">HH", self.width, self.height)
            server_init += struct.pack(">BBBBHHHBBB3x", *self.pixel_format)
            server_init += struct.pack(">I", len(name)) + name
            self.connection.sendall(server_init)

            caps_sent = False
            while not self.stop.is_set():
                message = self._receive(1, deadline)[0]
                if message == 0:  # SetPixelFormat
                    self._receive(19, deadline)
                elif message == 2:  # SetEncodings
                    _, count = struct.unpack(">BH", self._receive(3, deadline))
                    self._receive(count * 4, deadline)
                elif message == 3:  # FramebufferUpdateRequest
                    self._receive(9, deadline)
                    self._send_frame()
                    if not caps_sent:
                        self._send_caps()
                        caps_sent = True
                        if self.attack:
                            self.attack_armed.set()
                            while not self.stop.is_set() and not self.attack_requested.wait(0.05):
                                if time.monotonic() >= deadline:
                                    raise TimeoutError("test did not release the synthetic oversized-message attack")
                            if self.stop.is_set():
                                return
                            self._send_oversized_header()
                        elif self.request_on_paste:
                            # Some servers defer Request until an application paste.
                            pass
                        else:
                            self._send_notify()
                elif message == 4:  # KeyEvent
                    down, key = struct.unpack(">B2xI", self._receive(7, deadline))
                    self.events.append({"kind": "key", "down": bool(down), "key": key})
                    if (self.request_on_paste and down and key in {ord("v"), ord("V")}
                            and self.client_notify_seen.is_set() and not self.request_seen.is_set()):
                        self.paste_key_seen.set()
                        self._send_request()
                elif message == 5:  # PointerEvent
                    mask, x, y = struct.unpack(">BHH", self._receive(5, deadline))
                    self.events.append({"kind": "pointer", "mask": mask, "x": x, "y": y})
                elif message == 6:  # ClientCutText, including negative extended lengths.
                    self._receive_client_cut_text(deadline)
                else:
                    raise ValueError(f"unsupported synthetic RFB client message {message}")
        except (EOFError, ConnectionResetError, BrokenPipeError, OSError):
            if not self.stop.is_set() and not self._expected_disconnect:
                self.errors.append("loopback RFB connection ended unexpectedly")
        except Exception as exc:
            if not self.stop.is_set() and not self._expected_disconnect:
                self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if self.connection is not None:
                try:
                    self.connection.close()
                except OSError:
                    pass

    def trigger_attack(self) -> None:
        self.attack_requested.set()

    def close(self) -> None:
        self.stop.set()
        self.attack_requested.set()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.listener.close()
        except OSError:
            pass
        if self.thread.ident is not None:
            self.thread.join(3)
            if self.thread.is_alive():
                self.errors.append("loopback RFB server thread did not stop within 3 seconds")


def _wait_until(predicate, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _close_loopback(fixtures, sessions) -> list[str]:
    errors = []
    for fixture in fixtures:
        fixture.stop.set()
    for session in sessions:
        try:
            session.disconnect()
        except Exception as exc:
            errors.append(f"session cleanup: {type(exc).__name__}: {exc}")
    for fixture in fixtures:
        try:
            fixture.close()
        except Exception as exc:
            errors.append(f"fixture cleanup: {type(exc).__name__}: {exc}")
    return errors


class ExtensionChecks(unittest.TestCase):
    def test_linux_explicit_display_stays_on_x11_and_discovers_owned_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            authority = runtime / ".mutter-Xwaylandauth.synthetic"
            authority.write_bytes(b"synthetic authority")
            uid = runtime.stat().st_uid
            with mock.patch.dict(_linux_helper.os.environ, {"WAYLAND_DISPLAY": "other-session"}, clear=True), mock.patch.object(
                _linux_helper.os, "getuid", return_value=uid, create=True
            ), mock.patch.object(_linux_helper, "Path", return_value=runtime):
                env = _linux_helper.session_environment({"display": ":0"})
            self.assertEqual(env["DISPLAY"], ":0")
            self.assertNotIn("WAYLAND_DISPLAY", env)
            self.assertEqual(env["XAUTHORITY"], str(authority))
            with mock.patch.object(_linux_helper.shutil, "which", side_effect=lambda name: name):
                self.assertEqual(_linux_helper.clipboard_commands(env)[2], "xclip")

    def test_linux_ambiguous_authorities_fail_before_clipboard_access(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            for name in ("one", "two"):
                (runtime / (".mutter-Xwaylandauth." + name)).write_bytes(b"synthetic authority")
            with mock.patch.dict(_linux_helper.os.environ, {}, clear=True), mock.patch.object(
                _linux_helper.os, "getuid", return_value=runtime.stat().st_uid, create=True
            ), mock.patch.object(_linux_helper, "Path", return_value=runtime), mock.patch.object(
                _linux_helper.subprocess, "run"
            ) as run:
                result = _linux_helper.prepare_clipboard({"token": "synthetic", "text": "test", "display": ":0"})
            self.assertFalse(result["success"])
            self.assertEqual(result["phase"], "environment")
            run.assert_not_called()

    def test_linux_explicit_authority_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            for name in ("one", "two"):
                (runtime / (".mutter-Xwaylandauth." + name)).write_bytes(b"synthetic authority")
            with mock.patch.dict(_linux_helper.os.environ, {}, clear=True), mock.patch.object(
                _linux_helper.os, "getuid", return_value=runtime.stat().st_uid, create=True
            ), mock.patch.object(_linux_helper, "Path", return_value=runtime):
                env = _linux_helper.session_environment({"display": ":0", "xauthority": "/explicit/authority"})
            self.assertEqual(env["XAUTHORITY"], "/explicit/authority")

    def test_linux_authority_rejects_foreign_owners_and_nonregular_files(self):
        regular = SimpleNamespace(lstat=lambda: SimpleNamespace(st_uid=10, st_mode=0o100600))
        symlink = SimpleNamespace(lstat=lambda: SimpleNamespace(st_uid=11, st_mode=0o120777))
        runtime = SimpleNamespace(glob=lambda pattern: [regular, symlink])
        self.assertIsNone(_linux_helper.owned_xauthority(runtime, 11))

    def test_linux_clipboard_failure_reports_phase_without_exception_message(self):
        with mock.patch.object(_linux_helper, "session_environment", return_value={}), mock.patch.object(
            _linux_helper, "clipboard_commands", return_value=(["synthetic-write"], ["synthetic-read"], "wayland")
        ), mock.patch.object(_linux_helper.subprocess, "run", side_effect=[
            SimpleNamespace(returncode=0),
            _linux_helper.subprocess.TimeoutExpired(["synthetic-read", "SENSITIVE-ARGUMENT"], 5),
        ]):
            result = _linux_helper.prepare_clipboard({"token": "synthetic", "text": "test"})
        self.assertFalse(result["success"])
        self.assertEqual((result["backend"], result["phase"], result["error"]), ("wayland", "verify", "TimeoutExpired"))
        self.assertNotIn("SENSITIVE-ARGUMENT", json.dumps(result))

    def test_guest_failure_exposes_only_fixed_diagnostic_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vmrun = root / "vmrun.exe"
            vmrun.write_bytes(b"synthetic executable")
            _, profile_path = _write_profile_files(root, vmrun=str(vmrun))
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
            credentials = {"VNC_TEST_GUEST_USER": "synthetic-user", "VNC_TEST_GUEST_PASSWORD": "SENSITIVE-PASSWORD"}
            for backend, error, expected in (
                ("wayland", "TimeoutExpired", "wayland/verify/TimeoutExpired"),
                (["SENSITIVE-PASSWORD"], "SENSITIVE-PASSWORD", "verify"),
            ):
                payload = {}
                def invoke(command, *arguments, control):
                    if command == "copyFileFromHostToGuest" and arguments[0].endswith("payload.json"):
                        payload.update(json.loads(Path(arguments[0]).read_text(encoding="utf-8")))
                    elif command == "copyFileFromGuestToHost":
                        Path(arguments[1]).write_text(json.dumps({"success": False, "token": payload["token"],
                            "backend": backend, "phase": "verify", "error": error}), encoding="utf-8")
                    return ""
                with self.subTest(error=expected), mock.patch.dict(_guest.os.environ, credentials, clear=True):
                    helper = _guest.VmrunClipboard(profile)
                    with mock.patch.object(helper, "_invoke", side_effect=invoke), self.assertRaises(RuntimeError) as caught:
                        helper.prepare_clipboard("test", _operations.ActionControl(time.monotonic() + 5))
                    self.assertIn(expected, str(caught.exception))
                    self.assertNotIn("SENSITIVE-PASSWORD", str(caught.exception))

    def test_unicode_emoji_and_crlf_roundtrip(self):
        text = "first\r\nsecond\rthird\nemoji 🙂"
        codec = _clipboard.ExtendedClipboard()
        codec.receive(_capabilities_payload(4096))
        wire_message, sent = codec.set_text(text)
        self.assertTrue(sent)

        remote = _clipboard.ExtendedClipboard()
        messages, received, remote_sent = remote.receive(_payload_of_wire_message(wire_message))
        self.assertEqual(messages, [])
        self.assertEqual(received, "first\nsecond\nthird\nemoji 🙂")
        self.assertFalse(remote_sent)

    def test_capabilities_size_validation(self):
        flags = _clipboard.CAPS | _clipboard.TEXT
        malformed = (
            struct.pack("!I", flags),
            struct.pack("!I", flags) + struct.pack("!II", 128, 256),
        )
        for payload in malformed:
            with self.subTest(payload_size=len(payload)), self.assertRaises(ValueError):
                _clipboard.ExtendedClipboard().receive(payload)

    def test_notify_request_provide_exchange(self):
        local = _clipboard.ExtendedClipboard()
        local.receive(_capabilities_payload(text_limit=1))

        notify_message, sent = local.set_text("local text")
        self.assertFalse(sent)
        notify_flags, = struct.unpack_from("!I", _payload_of_wire_message(notify_message))
        self.assertEqual(notify_flags, _clipboard.NOTIFY | _clipboard.TEXT)

        request_messages, _, request_sent = local.receive(
            struct.pack("!I", _clipboard.NOTIFY | _clipboard.TEXT)
        )
        self.assertFalse(request_sent)
        self.assertEqual(len(request_messages), 1)
        request_flags, = struct.unpack_from("!I", _payload_of_wire_message(request_messages[0]))
        self.assertEqual(request_flags, _clipboard.REQUEST | _clipboard.TEXT)

        provide_messages, _, provide_sent = local.receive(
            struct.pack("!I", _clipboard.REQUEST | _clipboard.TEXT)
        )
        self.assertTrue(provide_sent)
        self.assertEqual(len(provide_messages), 1)
        remote = _clipboard.ExtendedClipboard()
        _, received, _ = remote.receive(_payload_of_wire_message(provide_messages[0]))
        self.assertEqual(received, "local text")

    def test_remote_clipboard_ownership_clears_local_text(self):
        codec = _clipboard.ExtendedClipboard()
        codec.receive(_capabilities_payload(4096))
        codec.set_text("former local text")

        messages, received, sent = codec.receive(_provide_payload(b"new remote text\0"))
        self.assertEqual(messages, [])
        self.assertEqual(received, "new remote text")
        self.assertFalse(sent)
        self.assertIsNone(codec.local_text)

        messages, _, sent = codec.receive(struct.pack("!I", _clipboard.REQUEST | _clipboard.TEXT))
        self.assertEqual(messages, [])
        self.assertFalse(sent)

    def test_invalid_utf8_nul_surrogate_bomb_and_trailing_compressed_data(self):
        invalid_payloads = (
            _provide_payload(b"\xff\0"),
            _provide_payload(b"contains\0embedded\0"),
            _provide_payload(b"valid\0", trailing_compressed=b"trailing"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload[-12:]), self.assertRaises(ValueError):
                _clipboard.ExtendedClipboard().receive(payload)

        expanded = struct.pack("!I", _clipboard.MAX_CLIPBOARD_BYTES + 1) + (
            b"A" * (_clipboard.MAX_CLIPBOARD_BYTES + 1)
        )
        bomb = struct.pack("!I", _clipboard.PROVIDE | _clipboard.TEXT) + zlib.compress(expanded)
        with self.assertRaises(ValueError):
            _clipboard.ExtendedClipboard().receive(bomb)
        with self.assertRaises(ValueError):
            _clipboard.validate_text("unpaired surrogate: \ud800")

    def test_tigervnc_syncflush_provide_decodes_multilingual_text(self):
        wire_text = _clipboard.wire_text("TigerVNC sync-flush: 中文 🙂\r\n第二行")
        payload = _provide_syncflush_payload(wire_text)
        self.assertTrue(payload.endswith(b"\x00\x00\xff\xff"))

        messages, received, sent = _clipboard.ExtendedClipboard().receive(payload)
        self.assertEqual(messages, [])
        self.assertEqual(received, "TigerVNC sync-flush: 中文 🙂\n第二行")
        self.assertFalse(sent)

    def test_tigervnc_syncflush_provide_rejects_truncated_flush_and_extra_data(self):
        valid_text = _clipboard.wire_text("有效文本🙂")
        truncated_flush = _provide_syncflush_payload(valid_text)[:-1]
        extra_decoded = _provide_syncflush_payload(valid_text, extra_decoded=b"unexpected")

        for name, payload in (
            ("truncated sync-flush trailer", truncated_flush),
            ("extra decompressed bytes", extra_decoded),
        ):
            with self.subTest(case=name), self.assertRaises(ValueError):
                _clipboard.ExtendedClipboard().receive(payload)

    def test_codec_instances_keep_independent_clipboard_state(self):
        first = _clipboard.ExtendedClipboard()
        first.receive(_capabilities_payload(4096))
        first.set_text("only in first connection")

        second = _clipboard.ExtendedClipboard()
        messages, _, sent = second.receive(struct.pack("!I", _clipboard.REQUEST | _clipboard.TEXT))
        self.assertEqual(messages, [])
        self.assertFalse(sent)
        self.assertIsNone(second.local_text)
        self.assertEqual(first.local_text, "only in first connection")

    def test_loopback_rfb_caps_notify_provide_unicode_and_key_chain(self):
        remote_text = "第一行\r\nemoji 🙂\r\n끝"
        expected_text = "第一行\nemoji 🙂\n끝"
        fixture = LoopbackRfbClipboardFixture(remote_text)
        session = _client.VNCSession(timeout=3, action_timeout=3)
        try:
            fixture.start()
            connected = session.connect("127.0.0.1", fixture.port)
            self.assertTrue(connected["connected"])
            self.assertTrue(fixture.client_caps_seen.wait(4))
            self.assertTrue(fixture.request_seen.wait(4))
            self.assertTrue(fixture.provide_sent.wait(4))
            self.assertTrue(
                _wait_until(
                    lambda: session.client is not None
                    and session.client.latest_clipboard == expected_text
                )
            )
            client = session.client
            self.assertTrue(client.ext_clipboard.supported)
            self.assertEqual(client.clipboard_encoding, "utf-8")
            self.assertEqual(session.clipboard_get()["text"], expected_text)
            self.assertTrue(session.status()["capabilities"]["utf8_clipboard"])

            # Exercise the same connection's key event chain after clipboard negotiation.
            session.keypress("ctrl+l")
            session.keypress("x")
            self.assertTrue(
                _wait_until(
                    lambda: any(
                        event["kind"] == "key" and event["down"] and event["key"] == ord("x")
                        for event in fixture.events
                    )
                )
            )
            key_events = [event for event in fixture.events if event["kind"] == "key"]
            l_down = next(i for i, event in enumerate(key_events) if event["down"] and event["key"] == ord("l"))
            x_down = next(i for i, event in enumerate(key_events) if event["down"] and event["key"] == ord("x"))
            self.assertLess(l_down, x_down)
            self.assertTrue(any(event["key"] == 0xFFE3 and event["down"] for event in key_events))
        finally:
            cleanup_errors = _close_loopback([fixture], [session])
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(fixture.errors, [])

    def test_loopback_rfb_connections_keep_clipboard_state_isolated(self):
        remote_texts = ["connection one 🧪", "第二个连接\r\n🙂"]
        expected_texts = ["connection one 🧪", "第二个连接\n🙂"]
        fixtures = [LoopbackRfbClipboardFixture(value) for value in remote_texts]
        sessions = [_client.VNCSession(timeout=3, action_timeout=3) for _ in fixtures]
        try:
            for fixture in fixtures:
                fixture.start()
            for session, fixture in zip(sessions, fixtures):
                self.assertTrue(session.connect("127.0.0.1", fixture.port)["connected"])
            for session, fixture, expected in zip(sessions, fixtures, expected_texts):
                self.assertTrue(fixture.client_caps_seen.wait(4))
                self.assertTrue(fixture.request_seen.wait(4))
                self.assertTrue(fixture.provide_sent.wait(4))
                self.assertTrue(
                    _wait_until(
                        lambda s=session, value=expected: s.client is not None
                        and s.client.latest_clipboard == value
                    )
                )
                self.assertTrue(session.client.ext_clipboard.supported)

            first_local_text = "only connection one owns this clipboard 🔒"
            sessions[0].clipboard_set(first_local_text)
            self.assertTrue(fixtures[0].client_provide_seen.wait(4))
            self.assertEqual(sessions[0].client.ext_clipboard.local_text, first_local_text)
            self.assertIsNone(sessions[1].client.ext_clipboard.local_text)
            self.assertEqual(sessions[1].clipboard_get()["text"], expected_texts[1])
        finally:
            cleanup_errors = _close_loopback(fixtures, sessions)
        self.assertEqual(cleanup_errors, [])
        self.assertEqual([fixture.errors for fixture in fixtures], [[], []])

    def test_loopback_rfb_oversized_extended_clipboard_disconnects(self):
        fixture = LoopbackRfbClipboardFixture("unused", attack=True)
        session = _client.VNCSession(timeout=3, action_timeout=3)
        try:
            fixture.start()
            self.assertTrue(session.connect("127.0.0.1", fixture.port)["connected"])
            self.assertTrue(fixture.attack_armed.wait(4))
            client = session.client
            fixture.trigger_attack()
            self.assertTrue(client.closed.wait(4))
            self.assertTrue(fixture.attack_sent.is_set())
            self.assertEqual(client.protocol_error, "clipboard_size_limit")
        finally:
            cleanup_errors = _close_loopback([fixture], [session])
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(fixture.errors, [])

    def test_loopback_disconnect_releases_framebuffer_and_clipboard_state(self):
        remote_text = "远端数据🙂" * 20_000
        expected_remote = remote_text.replace("\r\n", "\n")
        local_text = "本地文本🙂" * 20_000
        fixture = LoopbackRfbClipboardFixture(
            remote_text, width=1024, height=768
        )
        session = _client.VNCSession(timeout=8, action_timeout=8)
        try:
            fixture.start()
            self.assertTrue(session.connect("127.0.0.1", fixture.port)["connected"])
            client = session.client

            self.assertEqual((client.width, client.height), (1024, 768))
            self.assertTrue(fixture.client_caps_seen.wait(4))
            self.assertTrue(
                _wait_until(lambda: client.latest_clipboard == expected_remote, timeout=6)
            )
            self.assertEqual(client.latest_clipboard, expected_remote)

            session.clipboard_set(local_text)
            self.assertTrue(fixture.client_provide_seen.wait(4))
            self.assertEqual(client.ext_clipboard.local_text, local_text)

            fixture._expected_disconnect = True
            session.disconnect()
            self.assertTrue(client.closed.wait(4))
            self.assertIsNone(client.screen)
            self.assertEqual(client.latest_clipboard, "")
            self.assertIsNone(client.ext_clipboard.local_text)
        finally:
            cleanup_errors = _close_loopback([fixture], [session])
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(fixture.errors, [])

    def test_lazy_server_requests_exact_utf8_clipboard_after_paste_key(self):
        fixture = LoopbackRfbClipboardFixture(
            "unused", request_on_paste=True, text_limit=1
        )
        session = _client.VNCSession(timeout=3, action_timeout=3)
        text = "延迟请求 🙂\r\n第二行\rthird\nend"
        try:
            fixture.start()
            self.assertTrue(session.connect("127.0.0.1", fixture.port)["connected"])
            session.screenshot()
            self.assertTrue(fixture.client_caps_seen.wait(4))
            self.assertTrue(session.client.ext_clipboard.supported)

            started = time.monotonic()
            result = session.send_text(text, method="clipboard")
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2.0, "lazy clipboard Request deadlocked paste submission")
            self.assertTrue(result["paste_sent"])
            self.assertTrue(result["clipboard_offered"])
            self.assertTrue(result["clipboard_data_sent"])
            self.assertTrue(fixture.client_notify_seen.wait(4))
            self.assertTrue(fixture.paste_key_seen.wait(4))
            self.assertTrue(fixture.request_seen.wait(4))
            self.assertTrue(fixture.client_provide_seen.wait(4))
            self.assertEqual(fixture.provided_wire_text, _clipboard.wire_text(text))

            events = fixture.events
            notify_index = next(i for i, event in enumerate(events) if event["kind"] == "client_notify")
            paste_index = next(
                i for i, event in enumerate(events)
                if event["kind"] == "key" and event["down"] and event["key"] in {ord("v"), ord("V")}
            )
            request_index = next(i for i, event in enumerate(events) if event["kind"] == "server_request")
            provide_index = next(
                i for i, event in enumerate(events)
                if event["kind"] == "extended_clipboard"
                and event["flags"] & 0xFF000000 == _clipboard.PROVIDE
            )
            self.assertLess(notify_index, paste_index)
            self.assertLess(paste_index, request_index)
            self.assertLess(request_index, provide_index)
        finally:
            cleanup_errors = _close_loopback([fixture], [session])
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(fixture.errors, [])

    def test_clipboard_set_offers_without_sending_keyboard_or_mouse_events(self):
        fixture = LoopbackRfbClipboardFixture(
            "unused", request_on_paste=True, text_limit=1
        )
        session = _client.VNCSession(timeout=3, action_timeout=3)
        try:
            fixture.start()
            self.assertTrue(session.connect("127.0.0.1", fixture.port)["connected"])
            session.screenshot()
            self.assertTrue(fixture.client_caps_seen.wait(4))

            result = session.clipboard_set("只提供文字 🙂\r\n下一行")

            self.assertTrue(result["clipboard_offered"])
            self.assertFalse(result["clipboard_data_sent"])
            self.assertTrue(fixture.client_notify_seen.wait(4))
            self.assertFalse(fixture.request_seen.is_set())
            self.assertFalse(fixture.client_provide_seen.is_set())
            self.assertFalse(any(event["kind"] in {"key", "pointer"} for event in fixture.events))
        finally:
            cleanup_errors = _close_loopback([fixture], [session])
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(fixture.errors, [])

    def test_unknown_inline_secret_profile_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, profile_path = _write_profile_files(Path(temp_dir), extra='password = "synthetic-secret"\n')
            with self.assertRaises(ValueError):
                _guest.load_profiles(str(profile_path))

    def test_profiles_top_level_and_version_types_are_strict(self):
        invalid_documents = (
            ("boolean version", "version = true\nprofiles = {}\n"),
            ("non-table profiles", 'version = 1\nprofiles = "not-a-table"\n'),
        )
        for label, document in invalid_documents:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp_dir:
                profile_path = Path(temp_dir) / "profiles.toml"
                profile_path.write_text(document, encoding="utf-8")
                with self.assertRaises(ValueError):
                    _guest.load_profiles(str(profile_path))

    def test_profile_fields_with_invalid_types_raise_value_error(self):
        invalid_fields = (
            ("guest_os", [], "linux", ""),
            ("guest_temp_dir", "linux", [], ""),
            ("optional vmrun", "linux", "/tmp/vnc-mcp", 'vmrun = []\n'),
        )
        for label, guest_os, guest_temp_dir, extra in invalid_fields:
            with self.subTest(field=label), tempfile.TemporaryDirectory() as temp_dir:
                _, profile_path = _write_profile_files(
                    Path(temp_dir), guest_os=guest_os, guest_temp_dir=guest_temp_dir, extra=extra
                )
                with self.assertRaises(ValueError):
                    _guest.load_profiles(str(profile_path))

    def test_windows_guest_temp_dir_is_normalized(self):
        supplied = "C:/Users/guest/AppData/Local/Temp"
        with tempfile.TemporaryDirectory() as temp_dir:
            _, profile_path = _write_profile_files(
                Path(temp_dir), guest_os="windows", guest_temp_dir=supplied
            )
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
        self.assertEqual(profile.guest_temp_dir, r"C:\Users\guest\AppData\Local\Temp")

    def test_windows_guest_temp_dir_requires_absolute_drive_or_unc(self):
        invalid_paths = ("/Users/guest/Temp", r"\Temp", "C:Temp")
        for guest_temp_dir in invalid_paths:
            with self.subTest(guest_temp_dir=guest_temp_dir), tempfile.TemporaryDirectory() as temp_dir:
                _, profile_path = _write_profile_files(
                    Path(temp_dir), guest_os="windows", guest_temp_dir=guest_temp_dir
                )
                with self.assertRaises(ValueError):
                    _guest.load_profiles(str(profile_path))

    def test_endpoint_mismatch_is_rejected_before_connect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, profile_path = _write_profile_files(Path(temp_dir))
            profile = _guest.load_profiles(str(profile_path))["synthetic"]

        session = _client.VNCSession()
        with mock.patch.object(session, "_connect_client") as connect_client:
            with self.assertRaises(ValueError):
                session.connect("192.0.2.11", profile.port, profile=profile)
        connect_client.assert_not_called()

    def test_missing_credentials_are_detected_without_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vmrun_path = root / "vmrun.exe"
            vmrun_path.write_text("synthetic executable placeholder", encoding="utf-8")
            _, profile_path = _write_profile_files(root, vmrun=str(vmrun_path))
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
            with mock.patch.dict(_guest.os.environ, {}, clear=True), mock.patch.object(
                _guest.subprocess, "Popen"
            ) as popen:
                with self.assertRaises(ValueError):
                    _guest.VmrunClipboard(profile)
            popen.assert_not_called()

    def test_explicit_invalid_vmrun_does_not_fallback_or_invoke(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_vmrun = root / "missing-vmrun.exe"
            _, profile_path = _write_profile_files(root, vmrun=str(invalid_vmrun))
            profile = _guest.load_profiles(str(profile_path))["synthetic"]

        credentials = {
            "VNC_TEST_GUEST_USER": "synthetic-user",
            "VNC_TEST_GUEST_PASSWORD": "synthetic-password",
        }
        with mock.patch.dict(_guest.os.environ, credentials, clear=True), mock.patch.object(
            _guest, "get_diagnostics"
        ) as diagnostics, mock.patch.object(_guest.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                _guest.VmrunClipboard(profile)
        diagnostics.assert_not_called()
        popen.assert_not_called()

    def test_vmrun_failure_redacts_guest_and_encryption_passwords(self):
        guest_password = "synthetic"
        encryption_password = "synthetic-encryption-password"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vmrun_path = root / "vmrun.exe"
            vmrun_path.write_text("synthetic executable placeholder", encoding="utf-8")
            _, profile_path = _write_profile_files(
                root,
                extra='vm_password_env = "VNC_TEST_VM_PASSWORD"\n',
                vmrun=str(vmrun_path),
            )
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
            credentials = {
                "VNC_TEST_GUEST_USER": "synthetic-user",
                "VNC_TEST_GUEST_PASSWORD": guest_password,
                "VNC_TEST_VM_PASSWORD": encryption_password,
            }
            fake_process = SimpleNamespace(
                returncode=1,
                communicate=mock.Mock(
                    return_value=(
                        f"guest output {guest_password}".encode("utf-8"),
                        f"vm output {encryption_password}".encode("utf-8"),
                    )
                ),
                poll=mock.Mock(return_value=1),
                kill=mock.Mock(),
            )
            with mock.patch.dict(_guest.os.environ, credentials, clear=True), mock.patch.object(
                _guest.subprocess, "Popen", return_value=fake_process
            ) as popen:
                vmrun = _guest.VmrunClipboard(profile)
                with self.assertRaises(RuntimeError) as caught:
                    vmrun._invoke(
                        "runProgramInGuest",
                        "synthetic-argument",
                        control=_operations.ActionControl(time.monotonic() + 5),
                    )

            popen.assert_called_once()
            fake_process.communicate.assert_called_once()
            self.assertNotIn(guest_password, str(caught.exception))
            self.assertNotIn(encryption_password, str(caught.exception))

    def test_vmrun_password_crossing_error_truncation_boundary_is_redacted(self):
        guest_password = "PASSWORD-CROSSES-2000-BOUNDARY"
        prefix = b"x" * 1995
        stderr = prefix + guest_password.encode("utf-8") + b" tail"
        self.assertEqual(len(prefix), 1995)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vmrun_path = root / "vmrun.exe"
            vmrun_path.write_text("synthetic executable placeholder", encoding="utf-8")
            _, profile_path = _write_profile_files(root, vmrun=str(vmrun_path))
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
            credentials = {
                "VNC_TEST_GUEST_USER": "synthetic-user",
                "VNC_TEST_GUEST_PASSWORD": guest_password,
            }
            fake_process = SimpleNamespace(
                returncode=1,
                communicate=mock.Mock(return_value=(b"", stderr)),
                poll=mock.Mock(return_value=1),
                kill=mock.Mock(),
            )
            with mock.patch.dict(_guest.os.environ, credentials, clear=True), mock.patch.object(
                _guest.subprocess, "Popen", return_value=fake_process
            ):
                vmrun = _guest.VmrunClipboard(profile)
                with self.assertRaises(RuntimeError) as caught:
                    vmrun._invoke(
                        "runProgramInGuest",
                        "synthetic-argument",
                        control=_operations.ActionControl(time.monotonic() + 5),
                    )

        message = str(caught.exception)
        self.assertNotIn(guest_password, message)
        self.assertNotIn(guest_password[:5], message)

    def test_vmrun_cancellation_kills_and_drains_child(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vmrun_path = root / "vmrun.exe"
            vmrun_path.write_text("synthetic executable placeholder", encoding="utf-8")
            _, profile_path = _write_profile_files(root, vmrun=str(vmrun_path))
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
            credentials = {
                "VNC_TEST_GUEST_USER": "synthetic-user",
                "VNC_TEST_GUEST_PASSWORD": "synthetic-password",
            }
            fake_process = SimpleNamespace(
                returncode=None,
                communicate=mock.Mock(return_value=(b"", b"")),
                poll=mock.Mock(return_value=None),
                kill=mock.Mock(),
            )
            control = SimpleNamespace(
                deadline=time.monotonic() + 5,
                check=mock.Mock(side_effect=[None, _operations.ActionCancelled("synthetic cancellation")]),
            )
            with mock.patch.dict(_guest.os.environ, credentials, clear=True), mock.patch.object(
                _guest.subprocess, "Popen", return_value=fake_process
            ) as popen:
                vmrun = _guest.VmrunClipboard(profile)
                with self.assertRaises(_operations.ActionCancelled):
                    vmrun._invoke("runProgramInGuest", "synthetic-argument", control=control)

            popen.assert_called_once()
            fake_process.kill.assert_called_once()
            fake_process.communicate.assert_called_once()

    def test_auto_unicode_without_capability_or_profile_fails_before_input(self):
        session = _fake_connected_session()
        with mock.patch.object(session, "type_text") as type_text, mock.patch.object(
            session, "keypress"
        ) as keypress:
            with self.assertRaises(ValueError):
                session.send_text("Unicode 🙂", method="auto")
        type_text.assert_not_called()
        keypress.assert_not_called()

    def test_held_key_blocks_literal_text_before_input(self):
        session = _fake_connected_session()
        session._held_keys.add("ctrl")
        with mock.patch.object(session, "type_text") as type_text, mock.patch.object(
            session, "keypress"
        ) as keypress:
            with self.assertRaises(ValueError):
                session.send_text("plain text", method="auto")
        type_text.assert_not_called()
        keypress.assert_not_called()

    def test_held_mouse_button_blocks_literal_text_before_focus_or_type(self):
        session = _fake_connected_session()
        session._held_buttons.add("left")
        with mock.patch.object(session, "_check_point") as check_point, mock.patch.object(
            session, "click"
        ) as click, mock.patch.object(session, "type_text") as type_text:
            with self.assertRaises(ValueError):
                session.send_text("plain text", method="keys", focus_x=5, focus_y=5)

        check_point.assert_called_once_with(5, 5)
        click.assert_not_called()
        type_text.assert_not_called()

    def test_keys_focus_click_precedes_type_and_cancelled_wait_prevents_type(self):
        session = _fake_connected_session()
        control = _operations.ActionControl(time.monotonic() + 5)
        session._control = control
        calls = []
        try:
            with mock.patch.object(session, "_check_point"), mock.patch.object(
                session, "click", side_effect=lambda *args: calls.append(("click", *args))
            ), mock.patch.object(
                control, "wait", side_effect=lambda seconds: calls.append(("wait", seconds))
            ), mock.patch.object(
                session, "type_text", side_effect=lambda text: calls.append(("type", text))
            ) as type_text:
                result = session.send_text("typed text", method="keys", focus_x=5, focus_y=5)
            self.assertEqual(result["effective_key_delay_ms"], 20)
            self.assertEqual(
                calls,
                [("click", 5, 5), ("wait", 0.15), ("type", "typed text")],
            )
            type_text.assert_called_once_with("typed text")
        finally:
            session._control = None

        cancelled_session = _fake_connected_session()
        cancelled_control = _operations.ActionControl(time.monotonic() + 5)
        cancelled_session._control = cancelled_control
        cancelled_calls = []

        def cancel_focus_wait(seconds):
            cancelled_calls.append(("wait", seconds))
            cancelled_control.cancelled.set()
            cancelled_control.check()

        try:
            with mock.patch.object(cancelled_session, "_check_point"), mock.patch.object(
                cancelled_session, "click",
                side_effect=lambda *args: cancelled_calls.append(("click", *args)),
            ), mock.patch.object(
                cancelled_control, "wait", side_effect=cancel_focus_wait
            ) as wait, mock.patch.object(cancelled_session, "type_text") as type_text:
                with self.assertRaises(_operations.ActionCancelled):
                    cancelled_session.send_text(
                        "must not type", method="keys", focus_x=5, focus_y=5
                    )
            self.assertEqual(cancelled_calls, [("click", 5, 5), ("wait", 0.15)])
            wait.assert_called_once_with(0.15)
            type_text.assert_not_called()
        finally:
            cancelled_session._control = None

    def test_send_text_delay_routes_guard_unicode_and_key_only_modes(self):
        session = _fake_connected_session()
        cases = (
            ("auto Unicode delayed", "Unicode 🙂", "auto", True),
            ("auto ASCII delayed", "ASCII text", "auto", False),
            ("explicit Unicode keys delayed", "Unicode 🙂", "keys", False),
            ("guest delayed", "ASCII text", "guest", True),
            ("clipboard delayed", "ASCII text", "clipboard", True),
        )
        for label, text, method, rejected in cases:
            with self.subTest(case=label):
                with mock.patch.object(session, "click") as click, mock.patch.object(
                    session, "type_text"
                ) as type_text, mock.patch.object(
                    session, "clipboard_set"
                ) as clipboard_set, mock.patch.object(session, "keypress") as keypress:
                    if rejected:
                        with self.assertRaises(ValueError):
                            session.send_text(text, method=method, delay_ms=25)
                        click.assert_not_called()
                        type_text.assert_not_called()
                        clipboard_set.assert_not_called()
                        keypress.assert_not_called()
                    else:
                        result = session.send_text(text, method=method, delay_ms=25)
                        self.assertEqual(result["transport"], "keys")
                        self.assertEqual(result["effective_key_delay_ms"], 25)
                        type_text.assert_called_once_with(text, 25)
                        click.assert_not_called()
                        clipboard_set.assert_not_called()
                        keypress.assert_not_called()

    def test_guest_preparation_failure_does_not_send_paste_key(self):
        session = _fake_connected_session()
        session.host = "192.0.2.10"
        session.port = 5901
        session.guest_profile = _guest.GuestProfile(
            name="synthetic",
            host=session.host,
            port=session.port,
            vmx="synthetic.vmx",
            guest_os="linux",
            guest_temp_dir="/tmp/vnc-mcp",
            username_env="VNC_TEST_GUEST_USER",
            password_env="VNC_TEST_GUEST_PASSWORD",
        )
        fake_guest = mock.Mock()
        fake_guest.prepare_clipboard.side_effect = RuntimeError("synthetic guest failure")
        with mock.patch.object(_client, "VmrunClipboard", return_value=fake_guest), mock.patch.object(
            session, "_check_point"
        ) as check_point, mock.patch.object(session, "click") as click, mock.patch.object(
            session, "keypress"
        ) as keypress:
            with self.assertRaises(RuntimeError):
                session.send_text("text", method="guest", focus_x=5, focus_y=5)
        fake_guest.prepare_clipboard.assert_called_once()
        check_point.assert_called_once_with(5, 5)
        click.assert_not_called()
        keypress.assert_not_called()

    def test_guest_transport_requires_focus_before_guest_construction(self):
        session = _fake_connected_session()
        session.host = "192.0.2.10"
        session.port = 5901
        session.guest_profile = _guest.GuestProfile(
            name="synthetic",
            host=session.host,
            port=session.port,
            vmx="synthetic.vmx",
            guest_os="linux",
            guest_temp_dir="/tmp/vnc-mcp",
            username_env="VNC_TEST_GUEST_USER",
            password_env="VNC_TEST_GUEST_PASSWORD",
        )
        with mock.patch.object(_client, "VmrunClipboard") as vmrun_guest, mock.patch.object(
            session, "click"
        ) as click, mock.patch.object(session, "keypress") as keypress:
            with self.assertRaises(ValueError):
                session.send_text("text", method="guest")
        vmrun_guest.assert_not_called()
        click.assert_not_called()
        keypress.assert_not_called()

    def test_cancelled_focus_wait_does_not_send_or_replay_paste(self):
        session = _fake_connected_session()
        session.host = "192.0.2.10"
        session.port = 5901
        session.guest_profile = _guest.GuestProfile(
            name="synthetic",
            host=session.host,
            port=session.port,
            vmx="synthetic.vmx",
            guest_os="linux",
            guest_temp_dir="/tmp/vnc-mcp",
            username_env="VNC_TEST_GUEST_USER",
            password_env="VNC_TEST_GUEST_PASSWORD",
        )
        control = _operations.ActionControl(time.monotonic() + 5)
        session._control = control
        calls = []
        fake_guest = mock.Mock()
        fake_guest.prepare_clipboard.side_effect = lambda *args: (
            calls.append("prepare") or {"clipboard_verified": True}
        )

        def cancel_during_wait(seconds):
            calls.append(("wait", seconds))
            control.cancelled.set()
            control.check()

        try:
            with mock.patch.object(_client, "VmrunClipboard", return_value=fake_guest), mock.patch.object(
                session, "_check_point"
            ), mock.patch.object(session, "click", side_effect=lambda *args: calls.append(("click", *args))), mock.patch.object(
                session, "keypress"
            ) as keypress, mock.patch.object(control, "wait", side_effect=cancel_during_wait) as wait:
                with self.assertRaises(_operations.ActionCancelled):
                    session.send_text("text", method="guest", focus_x=5, focus_y=5)
        finally:
            session._control = None

        self.assertEqual(calls, ["prepare", ("click", 5, 5), ("wait", 0.15)])
        fake_guest.prepare_clipboard.assert_called_once()
        wait.assert_called_once_with(0.15)
        keypress.assert_not_called()


def _write_profile_files(
    root: Path,
    extra: str = "",
    vmrun: str | None = None,
    guest_os: object = "linux",
    guest_temp_dir: object = "/tmp/vnc-mcp",
) -> tuple[Path, Path]:
    vmx_path = root / "synthetic.vmx"
    vmx_path.write_text("fixture = true\n", encoding="utf-8")
    fields = [
        'host = "192.0.2.10"',
        "port = 5901",
        'vmx = "synthetic.vmx"',
        f"guest_os = {json.dumps(guest_os)}",
        f"guest_temp_dir = {json.dumps(guest_temp_dir)}",
        'username_env = "VNC_TEST_GUEST_USER"',
        'password_env = "VNC_TEST_GUEST_PASSWORD"',
    ]
    if vmrun is not None:
        fields.append(f"vmrun = {json.dumps(vmrun)}")
    profile_path = root / "profiles.toml"
    profile_path.write_text(
        "version = 1\n\n[profiles.synthetic]\n"
        + "\n".join(fields)
        + "\n"
        + extra,
        encoding="utf-8",
    )
    return vmx_path, profile_path


def _fake_connected_session():
    session = _client.VNCSession()
    session.client = SimpleNamespace(
        closed=threading.Event(),
        ext_clipboard=_clipboard.ExtendedClipboard(),
        clipboard_sent=threading.Event(),
        latest_clipboard="",
        clipboard_encoding="iso-8859-1",
        protocol_error=None,
    )
    return session


def _load_modules(source_root: Path) -> None:
    source = str(source_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    global _clipboard, _guest, _client, _operations, _linux_helper
    _clipboard = importlib.import_module("vnc_mcp.clipboard")
    _guest = importlib.import_module("vnc_mcp.guest")
    _client = importlib.import_module("vnc_mcp.client")
    _operations = importlib.import_module("vnc_mcp.operations")
    helper_spec = importlib.util.spec_from_file_location("linux_clipboard_helper", source_root / "src/vnc_mcp/resources/clipboard_linux.py")
    _linux_helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(_linux_helper)


class EvidenceTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_details = []

    def _record_failure(self, test, err, subtest=None):
        exception_type, exception, _ = err
        detail = {
            "test": test.id(),
            "exception_type": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception),
            "traceback": self._exc_info_to_string(err, test),
        }
        if subtest is not None:
            detail["subtest"] = str(subtest)
        self.failure_details.append(detail)

    def addFailure(self, test, err):
        self._record_failure(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record_failure(test, err)
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._record_failure(test, err, subtest)
        super().addSubTest(test, subtest, err)


def _run_checks() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExtensionChecks)
    tests = list(suite)
    result = unittest.TextTestRunner(
        stream=io.StringIO(), verbosity=2, resultclass=EvidenceTextTestResult
    ).run(suite)
    failed_ids = {detail["test"] for detail in result.failure_details}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed_ids
        for test in tests
    }
    return {
        "success": result.wasSuccessful(),
        "checks": checks,
        "failures": result.failure_details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    try:
        _load_modules(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "failures": [
                {
                    "test": "modules_load",
                    "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            ],
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
