"""Independent loopback protocol-contract checks for the native RFB client."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
import unittest


_rfb = None
_client_module = None
DEFAULT_PIXEL_FORMAT = bytes.fromhex("20 18 00 01 00 ff 00 ff 00 ff 00 08 10 00 00 00")


def _wire_pixel(rgb: tuple[int, int, int], pixel_format: bytes = DEFAULT_PIXEL_FORMAT) -> bytes:
    """Encode one server-side RFB pixel using the independently supplied wire format."""
    bpp, _depth, bigendian, truecolor = pixel_format[:4]
    if not truecolor or bpp not in (8, 16, 32):
        raise ValueError("fixture only encodes supported truecolor pixel formats")
    redmax, greenmax, bluemax = struct.unpack_from(">HHH", pixel_format, 4)
    redshift, greenshift, blueshift = pixel_format[10:13]
    value = (
        round(rgb[0] * redmax / 255) << redshift
        | round(rgb[1] * greenmax / 255) << greenshift
        | round(rgb[2] * bluemax / 255) << blueshift
    )
    return value.to_bytes(bpp // 8, "big" if bigendian else "little")


def _rect(x: int, y: int, width: int, height: int, pixels: bytes | None,
          encoding: int = 0) -> bytes:
    header = struct.pack(">HHHHi", x, y, width, height, encoding)
    return header + (pixels or b"")


def _framebuffer_update(rectangles: list[bytes]) -> bytes:
    return b"\x00\x00" + struct.pack(">H", len(rectangles)) + b"".join(rectangles)


def _wait_until(predicate, timeout: float = 1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError("RFB state did not reach the expected value")


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class NativeRfbFixture:
    """Tiny independently authored RFB server for one loopback connection."""

    width = 2
    height = 2
    pixel_format = DEFAULT_PIXEL_FORMAT
    initial_colors = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]

    def __init__(self, *, version: bytes = b"RFB 003.008\n",
                 security_types: tuple[int, ...] = (1,), security_33: int = 1,
                 width: int = 2, height: int = 2, name: bytes = b"loopback",
                 name_length: int | None = None, send_name: bool = True,
                 pixel_format: bytes | None = None,
                 initial_rectangles: list[tuple[int, int, int, int, list[tuple[int, int, int]] | None]] | None = None,
                 auth_challenge: bytes = bytes.fromhex("0123456789abcdef0123456789abcdef"),
                 expected_auth_response: bytes | None = None, auth_success: bool = True,
                 send_fence_offer: bool = False, send_continuous_offer: bool = False,
                 automatic_initial_update: bool = True, read_fragment: int = 2,
                 write_fragment: int = 3):
        self.version = version
        self.security_types = security_types
        self.security_33 = security_33
        self.width, self.height = width, height
        self.pixel_format = pixel_format if pixel_format is not None else self.pixel_format
        self.name = name
        self.name_length = len(name) if name_length is None else name_length
        self.send_name = send_name
        self.initial_rectangles = initial_rectangles
        self.auth_challenge = auth_challenge
        self.expected_auth_response = expected_auth_response
        self.auth_success = auth_success
        self.send_fence_offer = send_fence_offer
        self.send_continuous_offer = send_continuous_offer
        self.automatic_initial_update = automatic_initial_update
        self.read_fragment = read_fragment
        self.write_fragment = write_fragment
        self.listener = _ThreadingServer(("127.0.0.1", 0), _RfbHandler)
        self.listener.owner = self
        self.port = self.listener.server_address[1]
        self.stop = threading.Event()
        self.changed = threading.Event()
        self.disconnected = threading.Event()
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.connection: socket.socket | None = None
        self.events: list[dict] = []
        self.errors: list[str] = []
        self.client_banner: bytes | None = None
        self.security_selected: int | None = None
        self.shared_flag: int | None = None
        self.auth_response: bytes | None = None
        self.client_read_chunks = 0
        self.server_write_chunks = 0
        self.initial_sent = False
        self.server_thread = threading.Thread(
            target=self.listener.serve_forever, kwargs={"poll_interval": 0.02},
            name="native-rfb-loopback", daemon=True,
        )

    def __enter__(self):
        self.server_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def record(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)
            self.changed.set()

    def wait_for(self, predicate, timeout: float = 1.5):
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                value = predicate(list(self.events))
                if value:
                    return value
                self.changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.changed.wait(remaining):
                raise TimeoutError("loopback RFB fixture did not observe the expected message")

    def send_wire(self, data: bytes, pause: float = 0.0005) -> None:
        with self.send_lock:
            connection = self.connection
            if connection is None:
                raise ConnectionError("loopback client has not connected")
            for offset in range(0, len(data), self.write_fragment):
                chunk = data[offset:offset + self.write_fragment]
                connection.sendall(chunk)
                self.server_write_chunks += 1
                if pause and offset + self.write_fragment < len(data):
                    time.sleep(pause)

    def send_update(self, rectangles: list[tuple[int, int, int, int, list[tuple[int, int, int]]]]):
        payloads = []
        for x, y, width, height, colors in rectangles:
            if len(colors) != width * height:
                raise ValueError("fixture pixel count does not match rectangle dimensions")
            pixels = b"".join(_wire_pixel(color, self.pixel_format) for color in colors)
            payloads.append(_rect(x, y, width, height, pixels))
        self.send_wire(_framebuffer_update(payloads))

    def send_server_cut_text_length(self, length: int) -> None:
        self.send_wire(b"\x03\x00\x00\x00" + struct.pack("!i", length))

    def drop(self) -> None:
        with self.lock:
            connection = self.connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        self.stop.set()
        self.drop()
        self.listener.shutdown()
        self.listener.server_close()
        if self.server_thread.is_alive():
            self.server_thread.join(timeout=1)

    def _initial_update_wire(self) -> bytes:
        if self.initial_rectangles is None:
            colors = self.initial_colors[:self.width * self.height]
            if self.width * self.height != len(colors):
                colors = [(0, 0, 0)] * (self.width * self.height)
            return _framebuffer_update([_rect(
                0, 0, self.width, self.height,
                b"".join(_wire_pixel(color, self.pixel_format) for color in colors),
            )])
        rectangles = []
        for x, y, width, height, colors in self.initial_rectangles:
            pixels = None if colors is None else b"".join(
                _wire_pixel(color, self.pixel_format) for color in colors
            )
            rectangles.append(_rect(x, y, width, height, pixels))
        return _framebuffer_update(rectangles)


class _RfbHandler(socketserver.BaseRequestHandler):
    def setup(self):
        self.owner: NativeRfbFixture = self.server.owner
        self.request.settimeout(0.1)
        with self.owner.lock:
            self.owner.connection = self.request

    def _read_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            if self.owner.stop.is_set():
                raise EOFError
            try:
                part = self.request.recv(min(count - len(data), self.owner.read_fragment))
            except socket.timeout:
                continue
            if not part:
                raise EOFError
            self.owner.client_read_chunks += 1
            data.extend(part)
        return bytes(data)

    def _send(self, data: bytes) -> None:
        self.owner.send_wire(data)

    def _handshake(self) -> bool:
        self._send(self.owner.version)
        self.owner.client_banner = self._read_exact(12)
        try:
            minor = int(self.owner.client_banner[8:11])
        except ValueError as exc:
            raise AssertionError("client sent malformed protocol banner") from exc
        if minor <= 3:
            self._send(struct.pack("!I", self.owner.security_33))
            selected = self.owner.security_33
        else:
            offers = bytes(self.owner.security_types)
            self._send(bytes((len(offers),)) + offers)
            selected = self._read_exact(1)[0]
        self.owner.security_selected = selected
        if selected == 2:
            self._send(self.owner.auth_challenge)
            try:
                self.owner.auth_response = self._read_exact(16)
            except EOFError:
                return False
            auth_ok = self.owner.auth_success and (
                self.owner.expected_auth_response is None
                or self.owner.auth_response == self.owner.expected_auth_response
            )
            if not auth_ok:
                failure = struct.pack("!I", 1)
                if minor >= 8:
                    reason = b"fixture rejected VNC Authentication"
                    failure += struct.pack("!I", len(reason)) + reason
                self._send(failure)
                return False
            self._send(struct.pack("!I", 0))
        elif selected == 1 and minor >= 8:
            self._send(struct.pack("!I", 0))
        elif selected != 1:
            return False
        self.owner.shared_flag = self._read_exact(1)[0]
        return True

    def _server_init(self) -> bool:
        self._send(struct.pack("!HH", self.owner.width, self.owner.height)
                   + self.owner.pixel_format
                   + struct.pack("!I", self.owner.name_length)
                   + (self.owner.name if self.owner.send_name else b""))
        if self.owner.name_length > 65_536:
            return False
        if not self.owner.send_name:
            return False
        while True:
            kind = self._read_exact(1)[0]
            if kind == 0:
                body = self._read_exact(19)
                self.owner.record({"type": 0, "pixel_format": body[3:]})
            elif kind == 2:
                header = self._read_exact(3)
                count, = struct.unpack_from("!H", header, 1)
                values = self._read_exact(count * 4)
                encodings = list(struct.unpack("!" + "i" * count, values)) if count else []
                self.owner.record({"type": 2, "encodings": encodings})
                if self.owner.send_fence_offer:
                    self._send(struct.pack("!B3xIB", 248, 0x80000001, 5) + b"offer")
                if self.owner.send_continuous_offer:
                    self._send(b"\x96")
                return True
            else:
                raise AssertionError(f"expected SetPixelFormat/SetEncodings, got client message {kind}")

    def _client_message(self) -> dict:
        kind = self._read_exact(1)[0]
        if kind == 3:
            incremental, x, y, width, height = struct.unpack("!BHHHH", self._read_exact(9))
            return {"type": kind, "incremental": incremental, "region": (x, y, width, height)}
        if kind == 4:
            down, keysym = struct.unpack("!B2xI", self._read_exact(7))
            return {"type": kind, "down": down, "keysym": keysym}
        if kind == 5:
            buttons, x, y = struct.unpack("!BHH", self._read_exact(5))
            return {"type": kind, "buttons": buttons, "x": x, "y": y}
        if kind == 6:
            length, = struct.unpack("!3xI", self._read_exact(7))
            payload = self._read_exact(length)
            return {"type": kind, "length": length, "payload": payload}
        if kind == 150:
            enabled, x, y, width, height = struct.unpack("!BHHHH", self._read_exact(9))
            return {"type": kind, "enabled": enabled, "region": (x, y, width, height)}
        if kind == 248:
            flags, length = struct.unpack("!3xIB", self._read_exact(8))
            payload = self._read_exact(length)
            return {"type": kind, "flags": flags, "payload": payload}
        raise AssertionError(f"unexpected RFB client message type {kind}")

    def handle(self):
        try:
            if not self._handshake() or not self._server_init():
                return
            while not self.owner.stop.is_set():
                event = self._client_message()
                self.owner.record(event)
                if event["type"] == 3 and not self.owner.initial_sent:
                    self.owner.initial_sent = True
                    if self.owner.automatic_initial_update:
                        self._send(self.owner._initial_update_wire())
                elif event["type"] == 248 and event["flags"] & 0x80000000:
                    self._send(struct.pack("!B3xIB", 248, event["flags"] & 3,
                                           len(event["payload"])) + event["payload"])
                elif event["type"] == 150 and not event["enabled"]:
                    # EndOfContinuousUpdates is the server's disable acknowledgement.
                    self._send(b"\x96")
        except (EOFError, OSError, ConnectionError):
            pass
        except Exception as exc:
            with self.owner.lock:
                self.owner.errors.append(f"{type(exc).__name__}: {str(exc)[:240]}")
        finally:
            self.owner.disconnected.set()


def _pixel_events(events: list[dict], message_type: int) -> list[dict]:
    return [event for event in events if event.get("type") == message_type]


class NativeRfbChecks(unittest.TestCase):
    def connect(self, server: NativeRfbFixture, *, password: str | None = None,
                encoding: str = "auto", timeout: float = 1.5,
                initial_region: tuple[int, int, int, int] | None = None):
        return _rfb.RFBConnection("127.0.0.1", server.port, password, timeout,
                                  encoding, initial_region)

    def test_fragmented_handshakes_and_complete_initial_snapshot(self):
        for minor in (3, 7, 8):
            with self.subTest(rfb_minor=minor):
                banner = f"RFB 003.{minor:03}\n".encode("ascii")
                with NativeRfbFixture(version=banner, security_types=(1,)) as server:
                    client = self.connect(server)
                    try:
                        self.assertEqual(server.client_banner, banner)
                        self.assertEqual(server.security_selected, 1)
                        self.assertEqual(server.shared_flag, 1)
                        self.assertEqual(client.name, "loopback")
                        self.assertEqual(client.frame_sequence, 1)
                        self.assertEqual(client.initial_snapshot().sequence, 1)
                        self.assertEqual(client.screen.size, (2, 2))
                        self.assertEqual(client.screen.getpixel((1, 1)), (100, 110, 120))
                        self.assertGreater(server.client_read_chunks, 8)
                        self.assertGreater(server.server_write_chunks, 8)
                    finally:
                        client.close()

    def test_vnc_authentication_known_des_vector_rejection_and_missing_password(self):
        # Classic DES vector: key 133457799BBCDFF1, plaintext 0123456789ABCDEF,
        # ciphertext 85E813540F0AB405. VNC reverses key bits in each Latin-1 byte.
        password = bytes.fromhex("c8 2c ea 9e d9 3d fb 8f").decode("latin-1")
        expected = bytes.fromhex("85 e8 13 54 0f 0a b4 05") * 2
        challenge = bytes.fromhex("01 23 45 67 89 ab cd ef") * 2
        with NativeRfbFixture(security_types=(2,), auth_challenge=challenge,
                              expected_auth_response=expected) as server:
            client = self.connect(server, password=password)
            try:
                self.assertEqual(server.auth_response, expected)
                self.assertEqual(client.frame_sequence, 1)
            finally:
                client.close()

        with NativeRfbFixture(security_types=(2,), auth_challenge=challenge,
                              expected_auth_response=expected, auth_success=False) as server:
            with self.assertRaisesRegex(ConnectionError, "authentication failed"):
                self.connect(server, password=password)
            self.assertEqual(server.auth_response, expected)

        with NativeRfbFixture(security_types=(2,), auth_challenge=challenge) as server:
            with self.assertRaisesRegex(ValueError, "explicit password"):
                self.connect(server, password=None)
            self.assertEqual(server.security_selected, 2)
            self.assertIsNone(server.auth_response)

    def test_lossless_encoding_advertisement_and_raw_fallback(self):
        expected = {
            "auto": [16, 7, 5, 1, 0],
            "zrle": [16, 0],
            "tight": [7, 0],
            "hextile": [5, 0],
            "raw": [0],
        }
        for preference, encodings in expected.items():
            with self.subTest(encoding=preference):
                with NativeRfbFixture() as server:
                    client = self.connect(server, encoding=preference)
                    try:
                        message = server.wait_for(lambda events: next(
                            (event for event in events if event.get("type") == 2), None
                        ))
                        self.assertEqual(message["encodings"][:len(encodings)], encodings)
                        self.assertEqual(message["encodings"][len(encodings):],
                                         [-223, -224, -239, -232, -1063131698, -312, -313])
                        self.assertEqual(client.wire_encoding, "raw")
                    finally:
                        client.close()

    def test_server_advertised_bgrx_format_is_preserved_and_decoded(self):
        # A VMware server can advertise BGRX and ignore later SetPixelFormat
        # requests. These are server-originated BGRX raw bytes for unequal RGB
        # colors, encoded independently from the client's pixel decoder.
        bgrx = bytes.fromhex("20 18 00 01 00 ff 00 ff 00 ff 10 08 00 00 00 00")
        colors = [(0, 80, 180), (23, 77, 201), (41, 143, 17), (222, 31, 91)]
        with NativeRfbFixture(pixel_format=bgrx, initial_rectangles=[
            (0, 0, 2, 2, colors),
        ]) as server:
            client = self.connect(server)
            try:
                set_format = server.wait_for(lambda events: next((
                    event for event in events if event.get("type") == 0
                ), None))
                self.assertEqual(set_format["pixel_format"], bgrx,
                                 "client should retain the server's advertised supported format")
                self.assertEqual(list(client.screen.get_flattened_data()), colors)
                self.assertEqual(client.screen.getpixel((0, 0)), (0, 80, 180))
            finally:
                client.close()

    def test_rgb565_cursor_shape_uses_negotiated_pixel_width_and_preserves_alignment(self):
        rgb565 = bytes.fromhex("10 10 00 01 00 1f 00 3f 00 1f 0b 05 00 00 00 00")
        expected = (99, 89, 58)  # RGB565 channel codes (12, 22, 7), exactly representable.
        raw_pixel = _wire_pixel(expected, rgb565)
        self.assertEqual(raw_pixel, bytes.fromhex("c7 62"))
        with NativeRfbFixture(pixel_format=rgb565) as server:
            client = self.connect(server)
            try:
                cursor_shape = _wire_pixel((255, 0, 0), rgb565) + b"\x80"
                # The cursor pseudo-rectangle carries one 16-bit pixel and one
                # mask byte; the immediately following RAW rect proves the
                # reader consumed exactly that negotiated cursor payload.
                server.send_wire(_framebuffer_update([
                    _rect(0, 0, 1, 1, cursor_shape, encoding=-239),
                    _rect(1, 1, 1, 1, raw_pixel),
                ]))
                _wait_until(lambda: client.frame_sequence == 2)
                self.assertEqual(client.screen.getpixel((1, 1)), expected)
                self.assertEqual(client.updated_regions, ((1, 1, 1, 1),))
                self.assertFalse(client.closed.is_set())
            finally:
                client.close()

    def test_initial_publication_requires_full_coverage_and_valid_desktop_bounds(self):
        halves = [
            (0, 0, 1, 2, [(1, 2, 3), (4, 5, 6)]),
            (1, 0, 1, 2, [(7, 8, 9), (10, 11, 12)]),
        ]
        with NativeRfbFixture(initial_rectangles=halves) as server:
            client = self.connect(server)
            try:
                self.assertEqual(client.frame_sequence, 1)
                self.assertEqual(list(client.screen.get_flattened_data()),
                                 [(1, 2, 3), (7, 8, 9), (4, 5, 6), (10, 11, 12)])
            finally:
                client.close()

        invalid_cases = (
            {"width": 0, "height": 2},
            {"width": 4096, "height": 4097},
            {"name_length": 65_537, "send_name": False},
            {"initial_rectangles": [(1, 0, 2, 1, None)]},
            {"initial_rectangles": [(0, 0, 1, 1, [(1, 2, 3)])]},
        )
        labels = ("empty framebuffer", "pixel limit", "desktop name limit",
                  "rectangle outside desktop", "partial initial framebuffer")
        for label, options in zip(labels, invalid_cases):
            with self.subTest(malformed=label):
                with NativeRfbFixture(**options) as server:
                    with self.assertRaises((ValueError, ConnectionError)):
                        self.connect(server)

    def test_incremental_request_and_atomic_completed_update_publication(self):
        old = NativeRfbFixture.initial_colors
        with NativeRfbFixture() as server:
            client = self.connect(server)
            frames = []
            outcome: list[BaseException] = []

            def capture():
                try:
                    client.capture_frame(frames, timeout=1, incremental=True)
                except BaseException as exc:
                    outcome.append(exc)

            worker = threading.Thread(target=capture, name="capture-contract")
            try:
                worker.start()
                requests = server.wait_for(lambda events: [
                    event for event in _pixel_events(events, 3)
                ] if len(_pixel_events(events, 3)) >= 2 else None)
                self.assertEqual(requests[-1]["incremental"], 1)
                self.assertEqual(requests[-1]["region"], (0, 0, 2, 2))

                first = _rect(0, 0, 1, 2,
                              _wire_pixel((201, 1, 2)) + _wire_pixel((202, 3, 4)))
                second = _rect(1, 0, 1, 2,
                               _wire_pixel((203, 5, 6)) + _wire_pixel((204, 7, 8)))
                server.send_wire(b"\x00\x00\x00\x02" + first)
                time.sleep(0.08)
                self.assertEqual(client.frame_sequence, 1)
                self.assertEqual(list(client.screen.get_flattened_data()), old)
                self.assertEqual(frames, [])
                server.send_wire(second)
                worker.join(timeout=1.5)
                self.assertFalse(worker.is_alive(), "capture did not finish after complete update")
                self.assertEqual(outcome, [])
                self.assertEqual(client.frame_sequence, 2)
                self.assertEqual(list(frames[0].get_flattened_data()),
                                 [(201, 1, 2), (203, 5, 6), (202, 3, 4), (204, 7, 8)])
            finally:
                client.close()

    def test_unsolicited_update_advances_cached_frame_sequence(self):
        with NativeRfbFixture() as server:
            client = self.connect(server)
            try:
                initial = client.initial_snapshot()
                server.send_update([(1, 0, 1, 1, [(230, 40, 50)])])
                _wait_until(lambda: client.frame_sequence >= 2)
                self.assertEqual(client.frame_sequence, 2)
                self.assertNotEqual(client.frame_id, initial.frame_id)
                self.assertEqual(client.screen.getpixel((1, 0)), (230, 40, 50))
                self.assertEqual(client.updated_regions, ((1, 0, 1, 1),))
                self.assertEqual(client.initial_snapshot().sequence, 1)
            finally:
                client.close()

    def test_fence_echo_roundtrip_and_continuous_update_enable_disable(self):
        with NativeRfbFixture(send_fence_offer=True, send_continuous_offer=True) as server:
            client = self.connect(server)
            try:
                self.assertTrue(client.fence_supported)
                self.assertTrue(client.continuous_supported)
                server.wait_for(lambda events: any(
                    event.get("type") == 248 and event.get("payload") == b"offer"
                    and event.get("flags") == 1 for event in events
                ))

                client.fence()
                request = server.wait_for(lambda events: next((
                    event for event in events if event.get("type") == 248
                    and event.get("flags", 0) & 0x80000000
                ), None))
                self.assertEqual(request["flags"], 0x80000001)
                self.assertEqual(len(request["payload"]), 16)

                client.continuous(True)
                client.continuous(False)
                updates = server.wait_for(lambda events: [
                    event for event in events if event.get("type") == 150
                ] if len([event for event in events if event.get("type") == 150]) >= 2 else None)
                self.assertEqual([event["enabled"] for event in updates[-2:]], [1, 0])
                self.assertTrue(all(event["region"] == (0, 0, 2, 2) for event in updates[-2:]))
                self.assertFalse(client.continuous_enabled)
            finally:
                client.close()

    def test_invalid_clipboard_length_closes_and_clears_frame(self):
        with NativeRfbFixture() as server:
            client = self.connect(server)
            server.send_server_cut_text_length(1_048_577)
            try:
                self.assertTrue(client.closed.wait(1.0))
                _wait_until(lambda: client.screen is None)
                self.assertIsNone(client.screen)
                self.assertEqual(client.frame_sequence, 0)
                self.assertIn("clipboard_size_limit", client.protocol_error)
            finally:
                client.close()

    def test_abrupt_disconnect_during_capture_clears_screen_promptly(self):
        with NativeRfbFixture(automatic_initial_update=True) as server:
            client = self.connect(server, timeout=1.5)
            outcome: list[BaseException] = []

            def capture():
                try:
                    client.capture_frame([], timeout=1.2)
                except BaseException as exc:
                    outcome.append(exc)

            worker = threading.Thread(target=capture, name="disconnect-capture")
            try:
                worker.start()
                server.wait_for(lambda events: len(_pixel_events(events, 3)) >= 2)
                started = time.monotonic()
                server.drop()
                worker.join(timeout=1.0)
                self.assertFalse(worker.is_alive(), "capture waited for timeout after peer disconnect")
                self.assertLess(time.monotonic() - started, 0.9)
                self.assertTrue(outcome)
                self.assertIsInstance(outcome[0], ConnectionError)
                self.assertTrue(client.closed.is_set())
                self.assertIsNone(client.screen)
            finally:
                client.close()

    def test_read_timeout_has_no_input_and_reconnect_does_not_replay_input(self):
        with NativeRfbFixture(automatic_initial_update=True) as server:
            client = self.connect(server, timeout=1.0)
            try:
                with self.assertRaises(TimeoutError):
                    client.capture_frame([], timeout=0.15)
                events = list(server.events)
                self.assertEqual(_pixel_events(events, 4), [])
                self.assertEqual(_pixel_events(events, 5), [])
            finally:
                client.close()

        with NativeRfbFixture() as first:
            old_client = self.connect(first)
            old_client.keyPress("a")
            old_client.mouseDown(1)
            first.wait_for(lambda events: len(_pixel_events(events, 4)) >= 2
                           and len(_pixel_events(events, 5)) >= 1)
            old_client.close(keys=("a",), buttons=(1,))

        with NativeRfbFixture() as second:
            new_client = self.connect(second)
            try:
                self.assertEqual(_pixel_events(list(second.events), 4), [])
                self.assertEqual(_pixel_events(list(second.events), 5), [])
            finally:
                new_client.close()

    def test_printable_keysyms_include_physical_shift_and_release_in_reverse_order(self):
        with NativeRfbFixture() as server:
            client = self.connect(server)
            try:
                client.keyDown("A")
                client.keyUp("A")
                client.keyPress("?")
                client.keyPress("a")
                client.keyDown("ctrl-l")
                client.keyUp("ctrl-l")

                events = server.wait_for(lambda items: [
                    event for event in _pixel_events(items, 4)
                ] if len(_pixel_events(items, 4)) >= 14 else None)
                wire = [(event["down"], event["keysym"]) for event in events]
                self.assertEqual(wire, [
                    # Standalone uppercase: down Shift, down A, up A, up Shift.
                    (1, 0xFFE1), (1, ord("A")), (0, ord("A")), (0, 0xFFE1),
                    # Question mark uses the same physical Shift pairing.
                    (1, 0xFFE1), (1, ord("?")), (0, ord("?")), (0, 0xFFE1),
                    # Lowercase remains unshifted.
                    (1, ord("a")), (0, ord("a")),
                    # Chord release walks the explicit sequence in reverse.
                    (1, 0xFFE3), (1, ord("l")), (0, ord("l")), (0, 0xFFE3),
                ])
                self.assertEqual(sum(keysym == 0xFFE1 for _down, keysym in wire), 4)
            finally:
                client.close()

    def test_session_held_shift_overlap_and_literal_plus_wire_semantics(self):
        with NativeRfbFixture() as server:
            session = _client_module.VNCSession(
                timeout=1.5, action_timeout=3, network_encoding="raw",
            )
            try:
                status = session.connect("127.0.0.1", server.port)
                self.assertTrue(status["connected"])
                session.key_down("shift")
                initial = server.wait_for(lambda events: [
                    event for event in _pixel_events(events, 4)
                ] if len(_pixel_events(events, 4)) >= 1 else None)
                self.assertEqual(
                    [(event["down"], event["keysym"]) for event in initial],
                    [(1, 0xFFE1)],
                )

                for shifted in ("A", "?"):
                    with self.subTest(overlap=shifted):
                        with self.assertRaisesRegex(ValueError, "overlaps an already held key"):
                            session.keypress(shifted)
                        self.assertIsNotNone(session.client)
                        self.assertFalse(session.client.closed.is_set())
                        self.assertIn("shift", session._held_keys)
                        self.assertEqual(len(_pixel_events(list(server.events), 4)), 1,
                                         "rejected pulse must not emit a key event or release Shift")

                session.keypress("a")
                events = server.wait_for(lambda items: [
                    event for event in _pixel_events(items, 4)
                ] if len(_pixel_events(items, 4)) >= 3 else None)
                self.assertEqual(
                    [(event["down"], event["keysym"]) for event in events],
                    [(1, 0xFFE1), (1, ord("a")), (0, ord("a"))],
                )
                self.assertIn("shift", session._held_keys,
                              "lowercase pulse must leave the explicitly held Shift intact")

                session.key_up("shift")
                released = server.wait_for(lambda items: [
                    event for event in _pixel_events(items, 4)
                ] if len(_pixel_events(items, 4)) >= 4 else None)
                self.assertEqual(
                    [(event["down"], event["keysym"]) for event in released],
                    [(1, 0xFFE1), (1, ord("a")), (0, ord("a")), (0, 0xFFE1)],
                )
                self.assertNotIn("shift", session._held_keys)

                session.keypress("+")
                plus_events = server.wait_for(lambda items: [
                    event for event in _pixel_events(items, 4)
                ] if len(_pixel_events(items, 4)) >= 8 else None)
                self.assertEqual(
                    [(event["down"], event["keysym"]) for event in plus_events[-4:]],
                    [(1, 0xFFE1), (1, ord("+")), (0, ord("+")), (0, 0xFFE1)],
                    "literal plus must remain plus and release its implicit Shift last",
                )
            finally:
                session.disconnect()

    def test_initial_roi_is_requested_and_limits_later_capture_regions(self):
        roi = (1, 1, 1, 1)
        with NativeRfbFixture(initial_rectangles=[
            (1, 1, 1, 1, [(215, 125, 35)]),
        ]) as server:
            client = self.connect(server, initial_region=roi)
            try:
                request = server.wait_for(lambda events: next((
                    event for event in _pixel_events(events, 3)
                ), None))
                self.assertEqual(request["incremental"], 0)
                self.assertEqual(request["region"], roi)
                snapshot = client.initial_snapshot()
                self.assertEqual(snapshot.valid_region, roi)
                self.assertEqual(snapshot.image.size, (2, 2))
                self.assertEqual(snapshot.image.getpixel((1, 1)), (215, 125, 35))
                self.assertEqual(client.screen.getpixel((1, 1)), (215, 125, 35))

                with self.assertRaisesRegex(ValueError, "uninitialized pixels"):
                    client.capture_frame([], region=(0, 0, 1, 1), timeout=0.2)
                self.assertEqual(len(_pixel_events(list(server.events), 3)), 1,
                                 "rejected out-of-ROI capture must not reach the wire")
            finally:
                client.close()

    def test_desktop_size_only_then_full_update_publishes_new_safe_geometry(self):
        with NativeRfbFixture() as server:
            client = self.connect(server)
            original = client.initial_snapshot()
            original_pixels = list(original.image.get_flattened_data())
            try:
                # DesktopSize has no pixel payload. The reader must request the
                # resized surface and keep the old complete snapshot published.
                server.send_wire(_framebuffer_update([
                    _rect(0, 0, 3, 2, None, encoding=-223),
                ]))
                requests = server.wait_for(lambda events: [
                    event for event in _pixel_events(events, 3)
                ] if len(_pixel_events(events, 3)) >= 2 else None)
                self.assertEqual(requests[-1]["region"], (0, 0, 3, 2))
                self.assertEqual(client.frame_sequence, 1)
                self.assertEqual(client.screen.size, (2, 2))
                self.assertEqual(list(original.image.get_flattened_data()), original_pixels)

                colors = [
                    (9, 19, 29), (39, 49, 59), (69, 79, 89),
                    (99, 109, 119), (129, 139, 149), (159, 169, 179),
                ]
                server.send_update([(0, 0, 3, 2, colors)])
                _wait_until(lambda: client.frame_sequence == 2)
                self.assertEqual(client.screen.size, (3, 2))
                self.assertEqual(list(client.screen.get_flattened_data()), colors)
                self.assertEqual(client.updated_regions, ((0, 0, 3, 2),))
                self.assertEqual(original.image.size, (2, 2),
                                 "published snapshot reference must retain its original geometry")
            finally:
                client.close()

    def test_copyrect_cannot_initialize_desktopsized_surface(self):
        # Keep this malformed compound update in one socket write: the client
        # should close as soon as it sees CopyRect, potentially before a
        # deliberately fragmented fixture finishes sending the packet.
        with NativeRfbFixture(write_fragment=4096) as server:
            client = self.connect(server)
            original = client.initial_snapshot()
            original_pixels = list(original.image.get_flattened_data())
            try:
                self.assertFalse(server.disconnected.is_set(),
                                 f"loopback peer ended early: {server.errors}")
                server.send_wire(_framebuffer_update([
                    _rect(0, 0, 3, 2, None, encoding=-223),
                    _rect(0, 0, 1, 1, struct.pack(">HH", 0, 0), encoding=1),
                ]))
                self.assertTrue(client.closed.wait(1.0),
                                "CopyRect must be rejected before incomplete resized pixels publish")
                _wait_until(lambda: client.screen is None)
                self.assertIn("CopyRect", client.protocol_error)
                self.assertIsNone(client.screen)
                self.assertEqual(client.frame_sequence, 0)
                self.assertEqual(original.image.size, (2, 2))
                self.assertEqual(list(original.image.get_flattened_data()), original_pixels)
            finally:
                client.close()


class EvidenceTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_details = []

    def _record(self, test, err):
        exc_type, exc, tb = err
        self.failure_details.append({
            "test": test.id(),
            "exception_type": f"{exc_type.__module__}.{exc_type.__qualname__}",
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
        })

    def addFailure(self, test, err):
        self._record(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record(test, err)
        super().addError(test, err)


def _load_module(source: Path):
    src = source / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return importlib.import_module("vnc_mcp.rfb")


def _run_checks() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeRfbChecks)
    tests = list(suite)
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=EvidenceTextTestResult,
    ).run(suite)
    failed = {detail["test"] for detail in result.failure_details}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed
        for test in tests
    }
    return {"success": result.wasSuccessful(), "checks": checks,
            "failures": result.failure_details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    try:
        global _rfb
        _rfb = _load_module(args.source.resolve())
        global _client_module
        _client_module = importlib.import_module("vnc_mcp.client")
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "failures": [{
                "test": "modules_load",
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
