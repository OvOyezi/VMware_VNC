"""Real MCP-stdio fidelity checks against synthetic loopback RFB desktops."""
from __future__ import annotations

import argparse
import asyncio
import base64
from hashlib import sha256
from io import BytesIO
import importlib
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import traceback
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


_regression = None
_source_root: Path | None = None
_checks: dict[str, bool] = {}
_server_stderr: str = ""

_CHECK_NAMES = (
    "default_screenshot_is_lossless_png_with_source_hash_and_image_hint",
    "named_sessions_require_ids_and_remain_isolated",
    "cached_frame_inspection_sends_no_refresh_and_matches_displayed_image",
    "one_pixel_channel_step_has_exact_point_and_comparison_values",
    "detail_images_preserve_original_colors_and_label_amplified_difference",
    "default_wait_for_returns_png_with_original_image_hint",
    "superseded_and_reconnected_frame_ids_are_rejected",
    "desktop_resize_invalidates_remembered_reference",
)

TARGET = (17, 9)


def _payload(result) -> dict:
    if result.structuredContent:
        return result.structuredContent
    for item in result.content:
        if item.type == "text":
            return json.loads(item.text)
    raise AssertionError("MCP tool result contained no JSON text payload")


def _result_text(result) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "type", None) == "text"
    )


def _image_block(result):
    images = [item for item in result.content if item.type == "image"]
    if len(images) != 1:
        raise AssertionError(f"expected one image content block, found {len(images)}: {_result_text(result)}")
    return images[0]


def _decode_png(image_block) -> Image.Image:
    if image_block.mimeType != "image/png":
        raise AssertionError(f"expected image/png, got {image_block.mimeType!r}")
    with Image.open(BytesIO(base64.b64decode(image_block.data, validate=True))) as decoded:
        if decoded.format != "PNG":
            raise AssertionError(f"expected a PNG stream, got {decoded.format!r}")
        decoded.load()
        return decoded.convert("RGB").copy()


def _assert_original_hint(test: unittest.TestCase, image_block) -> None:
    # MCP's Python SDK names the aliased wire field ``meta``; serialization
    # emits it as ``_meta`` per the MCP schema.
    metadata = getattr(image_block, "meta", None)
    test.assertIsInstance(metadata, dict)
    test.assertEqual(metadata.get("codex/imageDetail"), "original")


def _expected_rgb(x: int, y: int, color: int, clicked: bool = False,
                  special_pixel: bool = True) -> tuple[int, int, int]:
    if special_pixel and (x, y) == TARGET:
        return (254, 0, 255) if clicked else (255, 0, 255)
    return (x * 3) % 256, (y * 5) % 256, color


def _expected_bytes(width: int, height: int, color: int, clicked: bool = False,
                    special_pixel: bool = True) -> bytes:
    return bytes(channel for y in range(height) for x in range(width)
                 for channel in _expected_rgb(x, y, color, clicked, special_pixel))


def _expected_source_hash(width: int, height: int, color: int, clicked: bool = False,
                          special_pixel: bool = True) -> str:
    return sha256(struct.pack("!II", width, height)
                  + _expected_bytes(width, height, color, clicked, special_pixel)).hexdigest()


def _mcp_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "VNC_HOST", "VNC_PORT", "VNC_PASSWORD", "VNC_PASSWORD_ENV", "VNC_PROFILE",
        "VNC_PROFILES", "VNC_PROFILES_FILE",
    ):
        env.pop(name, None)
    env.update(
        VNC_TIMEOUT="2",
        VNC_ACTION_TIMEOUT="5",
        VNC_SCREENSHOT_MODE="refresh",
        VNC_IDLE_TIMEOUT="0",
        VNC_MAX_SESSIONS="4",
    )
    return env


def _stdio_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(_source_root / "main.py"), "mcp"],
        cwd=str(_source_root),
        env=_mcp_environment(),
    )


def _image_payloads(result) -> tuple[dict, dict[str, tuple[dict, object]]]:
    if not result.content or result.content[0].type != "text":
        raise AssertionError("vnc_inspect must return JSON metadata before image content")
    metadata = json.loads(result.content[0].text)
    images: dict[str, tuple[dict, object]] = {}
    index = 1
    while index < len(result.content):
        text_block = result.content[index]
        if text_block.type != "text":
            raise AssertionError("each inspect image must be preceded by its JSON mapping")
        image_metadata = json.loads(text_block.text)
        kind = image_metadata.get("image_kind")
        if not isinstance(kind, str) or index + 1 >= len(result.content):
            raise AssertionError("inspect image mapping is incomplete")
        image_block = result.content[index + 1]
        if image_block.type != "image":
            raise AssertionError("inspect image mapping was not followed by an image content block")
        images[kind] = image_metadata, image_block
        index += 2
    return metadata, images


class ResizablePeer(socketserver.BaseRequestHandler):
    """Minimal loopback RFB peer that can publish one DesktopSize update."""

    def read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            try:
                data = self.request.recv(size - len(result))
            except socket.timeout:
                if self.server.owner.closed:
                    raise EOFError
                continue
            if not data:
                raise EOFError
            result.extend(data)
        return bytes(result)

    def handle(self) -> None:
        owner = self.server.owner
        with owner.lock:
            owner.connections.add(self.request)
        self.request.settimeout(0.1)
        pixel_format = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        try:
            self.request.sendall(b"RFB 003.008\n")
            assert self.read(12) == b"RFB 003.008\n"
            self.request.sendall(b"\x01\x01")
            assert self.read(1) == b"\x01"
            self.request.sendall(b"\x00\x00\x00\x00")
            assert self.read(1) == b"\x01"
            name = b"synthetic resize fixture"
            self.request.sendall(
                struct.pack(">HH", owner.width, owner.height)
                + struct.pack(">BBBBHHHBBB3x", *pixel_format)
                + struct.pack(">I", len(name)) + name
            )
            while True:
                message = self.read(1)[0]
                if message == 0:
                    self.read(3)
                    pixel_format = struct.unpack(">BBBBHHHBBB3x", self.read(16))
                elif message == 2:
                    _padding, count = struct.unpack(">BH", self.read(3))
                    self.read(count * 4)
                elif message == 3:
                    incremental, x0, y0, requested_width, requested_height = struct.unpack("!BHHHH", self.read(9))
                    with owner.lock:
                        owner.framebuffer_requests.append((incremental, x0, y0, requested_width, requested_height))
                        new_size = owner.resize_to
                        if new_size is not None:
                            owner.resize_to = None
                            owner.width, owner.height = new_size
                    if new_size is not None:
                        width, height = new_size
                        self.request.sendall(
                            b"\x00\x00\x00\x02"
                            + struct.pack(">HHHHi", 0, 0, width, height, -223)
                            + struct.pack(">HHHHi", 0, 0, width, height, 0)
                            + self._pixels(pixel_format, width, height)
                        )
                        continue
                    width, height = owner.width, owner.height
                    if x0 + requested_width > width or y0 + requested_height > height:
                        x0, y0, requested_width, requested_height = 0, 0, width, height
                    self.request.sendall(
                        b"\x00\x00\x00\x01"
                        + struct.pack(">HHHHi", x0, y0, requested_width, requested_height, 0)
                        + self._pixels(pixel_format, requested_width, requested_height, x0, y0)
                    )
                elif message == 4:
                    down, key = struct.unpack(">B2xI", self.read(7))
                    with owner.lock:
                        owner.events.append(("key", bool(down), key))
                elif message == 5:
                    mask, x, y = struct.unpack(">BHH", self.read(5))
                    with owner.lock:
                        owner.events.append(("pointer", mask, x, y))
                elif message == 6:
                    self.read(struct.unpack(">3xI", self.read(7))[0])
                else:
                    raise AssertionError(f"unexpected RFB client message {message}")
        except (EOFError, OSError):
            pass
        except Exception:
            with owner.lock:
                owner.errors.append(traceback.format_exc())
        finally:
            with owner.lock:
                owner.connections.discard(self.request)

    @staticmethod
    def _pixels(pixel_format, width: int, height: int, x0: int = 0, y0: int = 0) -> bytes:
        bpp, _depth, endian, _true_color, rm, gm, bm, rs, gs, bs = pixel_format
        data = bytearray()
        for y in range(y0, y0 + height):
            for x in range(x0, x0 + width):
                r, g, b = (x * 7 + 9) % 256, (y * 11 + 13) % 256, (x + y * 2) % 256
                value = ((r * rm // 255) << rs) | ((g * gm // 255) << gs) | ((b * bm // 255) << bs)
                data.extend(value.to_bytes(bpp // 8, "big" if endian else "little"))
        return bytes(data)


class ResizableFixture:
    def __init__(self, width: int = 64, height: int = 48):
        self.width, self.height = width, height
        self.resize_to: tuple[int, int] | None = None
        self.closed = False
        self.framebuffer_requests: list[tuple[int, int, int, int, int]] = []
        self.events: list[tuple] = []
        self.errors: list[str] = []
        self.connections = set()
        self.lock = threading.Lock()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ResizablePeer)
        self.server.daemon_threads = True
        self.server.owner = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def request_resize(self, width: int, height: int) -> None:
        with self.lock:
            self.resize_to = (width, height)

    def close(self) -> None:
        self.closed = True
        with self.lock:
            peers = tuple(self.connections)
        for peer in peers:
            try:
                peer.shutdown(socket.SHUT_RDWR)
                peer.close()
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)


class VisualFidelityMcpChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_png_frames_sessions_and_exact_inspection(self):
        primary = _regression.Fixture("Fidelity primary", 73, honor_regions=True)
        secondary = _regression.Fixture("Fidelity secondary", 137, honor_regions=True)

        def primary_rgb(x: int, y: int) -> tuple[int, int, int]:
            clicked = any(
                event[0] == "pointer" and event[1] & 1 and event[2:] == TARGET
                for event in tuple(primary.events)
            )
            return _expected_rgb(x, y, 73, clicked)

        primary.rgb = primary_rgb
        connection_ids: dict[str, str] = {}
        frame_ids: dict[str, str] = {}
        reference_id = None
        try:
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as server_stderr:
                async with stdio_client(_stdio_params(), errlog=server_stderr) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()

                        async def call(name: str, arguments: dict):
                            return await session.call_tool(name, arguments)

                        async def connect(target, session_id: str) -> dict:
                            result = await call("vnc_connect", {
                                "host": "127.0.0.1", "port": target.port, "session_id": session_id,
                            })
                            self.assertFalse(result.isError, _result_text(result))
                            value = _payload(result)
                            self.assertTrue(value["connected"])
                            self.assertTrue(value["connection_id"])
                            return value

                        primary_connection = await connect(primary, "fidelity-a")
                        connection_ids["fidelity-a"] = primary_connection["connection_id"]
                        missing_guard = await call("vnc_screenshot", {"session_id": "fidelity-a"})
                        self.assertTrue(missing_guard.isError, "named screenshot was accepted without connection_id")
                        missing_inspect_guard = await call("vnc_inspect", {"session_id": "fidelity-a"})
                        self.assertTrue(missing_inspect_guard.isError, "named inspect was accepted without connection_id")

                        primary_requests = len(primary.framebuffer_requests)
                        baseline_result = await call("vnc_screenshot", {
                            "session_id": "fidelity-a",
                            "connection_id": connection_ids["fidelity-a"],
                            "remember": True,
                        })
                        self.assertFalse(baseline_result.isError, _result_text(baseline_result))
                        baseline_metadata = _payload(baseline_result)
                        baseline_image_block = _image_block(baseline_result)
                        _assert_original_hint(self, baseline_image_block)
                        baseline_image = _decode_png(baseline_image_block)
                        self.assertEqual(baseline_metadata["format"], "png")
                        self.assertTrue(baseline_metadata["lossless"])
                        self.assertEqual(baseline_image.size, (64, 48))
                        baseline_bytes = _expected_bytes(64, 48, 73)
                        self.assertEqual(baseline_image.tobytes(), baseline_bytes)
                        self.assertEqual(baseline_metadata["source_rgb_sha256"], _expected_source_hash(64, 48, 73))
                        self.assertTrue(baseline_metadata["frame_id"])
                        self.assertIsInstance(baseline_metadata["frame_received_monotonic"], (int, float))
                        self.assertIsInstance(baseline_metadata["updated_regions"], list)
                        self.assertIsInstance(baseline_metadata["capture_source"], str)
                        self.assertEqual((baseline_metadata["origin_x"], baseline_metadata["origin_y"]), (0, 0))
                        self.assertEqual((baseline_metadata["width"], baseline_metadata["height"]), (64, 48))
                        frame_ids["baseline"] = baseline_metadata["frame_id"]
                        reference_id = baseline_metadata["reference_id"]
                        self.assertTrue(reference_id)
                        self.assertGreater(len(primary.framebuffer_requests), primary_requests)
                        _checks["default_screenshot_is_lossless_png_with_source_hash_and_image_hint"] = True

                        secondary_connection = await connect(secondary, "fidelity-b")
                        connection_ids["fidelity-b"] = secondary_connection["connection_id"]
                        secondary_screenshot = await call("vnc_screenshot", {
                            "session_id": "fidelity-b", "connection_id": connection_ids["fidelity-b"],
                        })
                        self.assertFalse(secondary_screenshot.isError, _result_text(secondary_screenshot))
                        secondary_metadata = _payload(secondary_screenshot)
                        secondary_image_block = _image_block(secondary_screenshot)
                        _assert_original_hint(self, secondary_image_block)
                        secondary_image = _decode_png(secondary_image_block)
                        self.assertEqual(secondary_image.tobytes(), _expected_bytes(64, 48, 137, special_pixel=False))
                        self.assertEqual(secondary_metadata["source_rgb_sha256"],
                                         _expected_source_hash(64, 48, 137, special_pixel=False))
                        self.assertNotEqual(secondary_metadata["frame_id"], frame_ids["baseline"])
                        wrong_session_frame = await call("vnc_inspect", {
                            "frame_id": frame_ids["baseline"],
                            "session_id": "fidelity-b",
                            "connection_id": connection_ids["fidelity-b"],
                        })
                        self.assertTrue(wrong_session_frame.isError, "frame_id crossed session boundaries")
                        wrong_session_reference = await call("vnc_inspect", {
                            "frame_id": secondary_metadata["frame_id"],
                            "reference_id": reference_id,
                            "session_id": "fidelity-b",
                            "connection_id": connection_ids["fidelity-b"],
                        })
                        self.assertTrue(wrong_session_reference.isError, "reference_id crossed session boundaries")
                        _checks["named_sessions_require_ids_and_remain_isolated"] = True

                        requests_before_cached = len(primary.framebuffer_requests)
                        cached_result = await call("vnc_inspect", {
                            "frame_id": frame_ids["baseline"],
                            "session_id": "fidelity-a",
                            "connection_id": connection_ids["fidelity-a"],
                        })
                        self.assertFalse(cached_result.isError, _result_text(cached_result))
                        cached_metadata, cached_images = _image_payloads(cached_result)
                        self.assertEqual(cached_metadata["frame_id"], frame_ids["baseline"])
                        self.assertTrue(cached_metadata["cached_frame"])
                        self.assertEqual(cached_metadata["image_order"], ["current"])
                        current_meta, current_block = cached_images["current"]
                        _assert_original_hint(self, current_block)
                        self.assertEqual(current_meta["mapping"]["scale"], 1)
                        self.assertEqual(_decode_png(current_block).tobytes(), baseline_bytes)
                        self.assertEqual(len(primary.framebuffer_requests), requests_before_cached,
                                         "cached frame inspection requested a new RFB update")
                        _checks["cached_frame_inspection_sends_no_refresh_and_matches_displayed_image"] = True

                        click_result = await call("vnc_click", {
                            "x": TARGET[0], "y": TARGET[1],
                            "session_id": "fidelity-a", "connection_id": connection_ids["fidelity-a"],
                        })
                        self.assertFalse(click_result.isError, _result_text(click_result))
                        self.assertTrue(any(event[0] == "pointer" and event[1] & 1 and event[2:] == TARGET
                                            for event in primary.events), "synthetic fixture did not receive the click")

                        requests_before_wait = len(primary.framebuffer_requests)
                        wait_result = await call("vnc_wait_for", {
                            "condition": "changed", "reference_id": reference_id,
                            "timeout": 0, "interval": 0.1, "pixel_threshold": 0,
                            "max_changed_ratio": 0,
                            "session_id": "fidelity-a", "connection_id": connection_ids["fidelity-a"],
                        })
                        self.assertFalse(wait_result.isError, _result_text(wait_result))
                        wait_metadata = _payload(wait_result)
                        wait_image_block = _image_block(wait_result)
                        _assert_original_hint(self, wait_image_block)
                        wait_image = _decode_png(wait_image_block)
                        self.assertEqual(wait_metadata["format"], "png")
                        self.assertTrue(wait_metadata["lossless"])
                        self.assertTrue(wait_metadata["matched"])
                        self.assertEqual(wait_image.size, (64, 48))
                        after_bytes = _expected_bytes(64, 48, 73, clicked=True)
                        self.assertEqual(wait_image.tobytes(), after_bytes)
                        self.assertEqual(wait_metadata["source_rgb_sha256"], _expected_source_hash(64, 48, 73, clicked=True))
                        self.assertGreater(len(primary.framebuffer_requests), requests_before_wait)
                        changed = [
                            (x, y, baseline_image.getpixel((x, y)), wait_image.getpixel((x, y)))
                            for y in range(48) for x in range(64)
                            if baseline_image.getpixel((x, y)) != wait_image.getpixel((x, y))
                        ]
                        self.assertEqual(changed, [(TARGET[0], TARGET[1], (255, 0, 255), (254, 0, 255))])
                        frame_ids["after"] = wait_metadata["frame_id"]
                        self.assertNotEqual(frame_ids["after"], frame_ids["baseline"])
                        _checks["default_wait_for_returns_png_with_original_image_hint"] = True

                        requests_before_inspect = len(primary.framebuffer_requests)
                        inspect_result = await call("vnc_inspect", {
                            "frame_id": frame_ids["after"],
                            "reference_id": reference_id,
                            "points": [{"x": TARGET[0], "y": TARGET[1]}],
                            "detail_region": {"x": TARGET[0], "y": TARGET[1], "width": 1, "height": 1},
                            "zoom": 3,
                            "pixel_threshold": 0,
                            "session_id": "fidelity-a",
                            "connection_id": connection_ids["fidelity-a"],
                        })
                        self.assertFalse(inspect_result.isError, _result_text(inspect_result))
                        inspection, image_items = _image_payloads(inspect_result)
                        self.assertEqual(len(primary.framebuffer_requests), requests_before_inspect,
                                         "inspection of a cached frame unexpectedly refreshed the framebuffer")
                        self.assertEqual(inspection["frame_id"], frame_ids["after"])
                        self.assertTrue(inspection["cached_frame"])
                        self.assertEqual(inspection["reference_frame_id"], frame_ids["baseline"])
                        self.assertEqual(inspection["comparison"]["changed_pixels"], 1)
                        self.assertEqual(inspection["comparison"]["total_pixels"], 64 * 48)
                        self.assertEqual(inspection["comparison"]["changed_ratio"], 1 / (64 * 48))
                        self.assertEqual(inspection["comparison"]["max_channel_difference"], 1)
                        self.assertEqual(inspection["comparison"]["changed_bbox"], [17, 9, 18, 10])
                        self.assertEqual(inspection["points"], [{
                            "x": 17, "y": 9, "rgb": [254, 0, 255],
                            "before_rgb": [255, 0, 255], "delta_rgb": [-1, 0, 0],
                        }])
                        self.assertEqual(inspection["detail_region"], [17, 9, 1, 1])
                        self.assertEqual(inspection["image_order"], [
                            "current", "detail_current", "detail_reference", "difference_amplified",
                        ])
                        for kind, (image_metadata, block) in image_items.items():
                            self.assertEqual(block.mimeType, "image/png", kind)
                            _assert_original_hint(self, block)
                            expected_image_frame_id = (
                                frame_ids["baseline"] if kind == "detail_reference" else frame_ids["after"]
                            )
                            self.assertEqual(image_metadata["frame_id"], expected_image_frame_id, kind)
                            if kind == "detail_reference":
                                self.assertEqual(
                                    image_metadata["frame_received_monotonic"],
                                    baseline_metadata["frame_received_monotonic"],
                                )
                            mapping = image_metadata["mapping"]
                            self.assertEqual(inspection["image_metadata"][kind], mapping)
                        _checks["one_pixel_channel_step_has_exact_point_and_comparison_values"] = True

                        current_image = _decode_png(image_items["current"][1])
                        self.assertEqual(current_image.tobytes(), after_bytes)
                        for kind, color in (("detail_current", (254, 0, 255)),
                                            ("detail_reference", (255, 0, 255)),
                                            ("difference_amplified", (16, 0, 0))):
                            metadata, image_block = image_items[kind]
                            detail = _decode_png(image_block)
                            self.assertEqual(detail.size, (3, 3))
                            self.assertEqual(detail.tobytes(), bytes(color) * 9)
                            mapping = metadata["mapping"]
                            self.assertEqual((mapping["origin_x"], mapping["origin_y"]), TARGET)
                            self.assertEqual((mapping["source_width"], mapping["source_height"]), (1, 1))
                            self.assertEqual((mapping["output_width"], mapping["output_height"], mapping["scale"]),
                                             (3, 3, 3))
                        self.assertIn("altered", image_items["difference_amplified"][0]["mapping"]["color_semantics"])
                        _checks["detail_images_preserve_original_colors_and_label_amplified_difference"] = True

                        superseded = await call("vnc_inspect", {
                            "frame_id": frame_ids["baseline"],
                            "session_id": "fidelity-a",
                            "connection_id": connection_ids["fidelity-a"],
                        })
                        self.assertTrue(superseded.isError, "superseded frame_id was accepted")
                        self.assertIn("frame_id", _result_text(superseded))

                        replacement = await connect(primary, "fidelity-a")
                        self.assertNotEqual(replacement["connection_id"], connection_ids["fidelity-a"])
                        connection_ids["fidelity-a"] = replacement["connection_id"]
                        reconnected = await call("vnc_inspect", {
                            "frame_id": frame_ids["after"],
                            "reference_id": reference_id,
                            "session_id": "fidelity-a",
                            "connection_id": connection_ids["fidelity-a"],
                        })
                        self.assertTrue(reconnected.isError, "reconnected session accepted an old frame or reference")
                        self.assertTrue(any(word in _result_text(reconnected).lower()
                                            for word in ("frame_id", "reference")))
                        _checks["superseded_and_reconnected_frame_ids_are_rejected"] = True
        finally:
            primary.close()
            secondary.close()
        self.assertEqual(primary.errors, [])
        self.assertEqual(secondary.errors, [])

    async def test_resize_expires_reference_through_real_stdio_mcp(self):
        fixture = ResizableFixture()
        connection_id = None
        try:
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as server_stderr:
                async with stdio_client(_stdio_params(), errlog=server_stderr) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        connected_result = await session.call_tool("vnc_connect", {
                            "host": "127.0.0.1", "port": fixture.port, "session_id": "fidelity-resize",
                        })
                        self.assertFalse(connected_result.isError, _result_text(connected_result))
                        connected = _payload(connected_result)
                        connection_id = connected["connection_id"]
                        baseline_result = await session.call_tool("vnc_screenshot", {
                            "session_id": "fidelity-resize", "connection_id": connection_id, "remember": True,
                        })
                        self.assertFalse(baseline_result.isError, _result_text(baseline_result))
                        baseline = _payload(baseline_result)
                        self.assertEqual((baseline["desktop_width"], baseline["desktop_height"]), (64, 48))
                        reference_id = baseline["reference_id"]
                        self.assertTrue(reference_id)

                        fixture.request_resize(80, 48)
                        resized_result = await session.call_tool("vnc_screenshot", {
                            "session_id": "fidelity-resize", "connection_id": connection_id,
                        })
                        self.assertFalse(resized_result.isError, _result_text(resized_result))
                        resized = _payload(resized_result)
                        resized_image = _decode_png(_image_block(resized_result))
                        self.assertEqual((resized["desktop_width"], resized["desktop_height"]), (80, 48))
                        self.assertEqual(resized_image.size, (80, 48))
                        expected = bytes(channel for y in range(48) for x in range(80)
                                         for channel in ((x * 7 + 9) % 256, (y * 11 + 13) % 256, (x + y * 2) % 256))
                        self.assertEqual(resized_image.tobytes(), expected)
                        self.assertNotEqual(resized["frame_id"], baseline["frame_id"])

                        expired_reference = await session.call_tool("vnc_inspect", {
                            "reference_id": reference_id,
                            "session_id": "fidelity-resize",
                            "connection_id": connection_id,
                        })
                        self.assertTrue(expired_reference.isError, "resize did not invalidate the remembered reference")
                        self.assertIn("visual reference", _result_text(expired_reference).lower())
                        _checks["desktop_resize_invalidates_remembered_reference"] = True

                        disconnected = await session.call_tool("vnc_disconnect", {
                            "session_id": "fidelity-resize", "connection_id": connection_id,
                        })
                        self.assertFalse(disconnected.isError, _result_text(disconnected))
        finally:
            fixture.close()
        self.assertEqual(fixture.errors, [])


def _load(source_root: Path) -> None:
    global _regression, _source_root
    _source_root = source_root.resolve()
    source_dir = str(_source_root / "src")
    checks_dir = str(_source_root / "checks")
    for entry in (source_dir, checks_dir):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    _regression = importlib.import_module("vnc_regression")
    expected = (_source_root / "checks" / "vnc_regression.py").resolve()
    if Path(_regression.__file__).resolve() != expected:
        raise ImportError(f"RFB fixture loaded from a different source: {_regression.__file__}")


class _EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures_with_tracebacks = []

    def _remember_failure(self, test, err):
        exception_type, exception, _ = err
        self.failures_with_tracebacks.append({
            "test": test.id(),
            "exception_type": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception),
            "traceback": self._exc_info_to_string(err, test),
        })

    def addFailure(self, test, err):
        self._remember_failure(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._remember_failure(test, err)
        super().addError(test, err)


def _run_checks() -> dict[str, object]:
    global _checks, _server_stderr
    _checks = {name: False for name in _CHECK_NAMES}
    _server_stderr = ""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(VisualFidelityMcpChecks)
    tests = list(suite)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2, resultclass=_EvidenceResult).run(suite)
    failures = result.failures_with_tracebacks
    failed_ids = {detail["test"] for detail in failures}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed_ids
        for test in tests
    }
    checks.update(_checks)
    report: dict[str, object] = {
        "success": result.wasSuccessful() and all(_checks.values()),
        "checks": checks,
        "failures": failures,
        "scope": "real MCP stdio with synthetic loopback RFB desktops only; no VM access",
    }
    if output.getvalue():
        report["test_output"] = output.getvalue()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    try:
        _load(args.source)
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"module_loads": False},
            "failures": [{
                "test": "module_loads",
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }],
            "scope": "checks did not start",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = sum(bool(value) for value in report["checks"].values())
    total = len(report["checks"])
    print(f"Fidelity checks: {passed}/{total} passed; report: {args.output}")
    if not report["success"]:
        for failure in report.get("failures", []):
            print(f"{failure['test']}: {failure['exception_type']}: {failure['message']}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
