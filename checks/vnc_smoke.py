"""Exercise VMware VNC over real MCP stdio and a loopback RFB fixture.

The fixture is an artificial framebuffer. No host or guest desktop receives input.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


class RfbFixture:
    width, height = 64, 48

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.socket.settimeout(0.5)
        self.port = self.socket.getsockname()[1]
        self.events = []
        self.errors = []
        self.stop = threading.Event()
        self.connection = None
        self.clicked = False
        self.clipboard_sent = False
        self.pixel_format = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def receive(self, count):
        data = bytearray()
        while len(data) < count and not self.stop.is_set():
            try:
                chunk = self.connection.recv(count - len(data))
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError
            data.extend(chunk)
        if len(data) != count:
            raise EOFError
        return bytes(data)

    def rgb(self, x, y):
        if self.clicked and (x, y) == (12, 15):
            return 255, 0, 255
        return (x * 3) % 256, (y * 5) % 256, ((x + y) * 7) % 256

    def frame(self):
        bpp, depth, endian, true_color, rm, gm, bm, rs, gs, bs = self.pixel_format
        if not true_color or bpp not in (8, 16, 32):
            raise ValueError(f"Fixture cannot encode requested format: {self.pixel_format}")
        pixels = bytearray()
        for y in range(self.height):
            for x in range(self.width):
                r, g, b = self.rgb(x, y)
                value = ((r * rm // 255) << rs) | ((g * gm // 255) << gs) | ((b * bm // 255) << bs)
                pixels.extend(value.to_bytes(bpp // 8, "big" if endian else "little"))
        header = b"\x00\x00\x00\x01" + struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
        self.connection.sendall(header + pixels)
        if not self.clipboard_sent:
            text = b"fixture clipboard"
            self.connection.sendall(b"\x03\x00\x00\x00" + struct.pack(">I", len(text)) + text)
            self.clipboard_sent = True

    def serve(self):
        try:
            while not self.stop.is_set():
                try:
                    self.connection, _ = self.socket.accept()
                    break
                except socket.timeout:
                    continue
            if self.connection is None:
                return
            self.connection.settimeout(0.5)
            self.connection.sendall(b"RFB 003.008\n")
            version = self.receive(12)
            if version != b"RFB 003.008\n":
                raise ValueError(f"Unexpected RFB version: {version!r}")
            self.connection.sendall(b"\x01\x01")
            if self.receive(1) != b"\x01":
                raise ValueError("Client did not choose fixture's no-auth security type")
            self.connection.sendall(b"\x00\x00\x00\x00")
            self.receive(1)
            name = b"VMware VNC isolated protocol fixture"
            init = struct.pack(">HH", self.width, self.height)
            init += struct.pack(">BBBBHHHBBB3x", *self.pixel_format)
            init += struct.pack(">I", len(name)) + name
            self.connection.sendall(init)
            while not self.stop.is_set():
                message = self.receive(1)[0]
                if message == 0:
                    self.receive(3)
                    self.pixel_format = struct.unpack(">BBBBHHHBBB3x", self.receive(16))
                elif message == 2:
                    _, count = struct.unpack(">BH", self.receive(3))
                    self.receive(count * 4)
                elif message == 3:
                    self.receive(9)
                    self.frame()
                elif message == 4:
                    down, key = struct.unpack(">B2xI", self.receive(7))
                    self.events.append({"kind": "key", "down": bool(down), "key": key})
                elif message == 5:
                    mask, x, y = struct.unpack(">BHH", self.receive(5))
                    self.events.append({"kind": "pointer", "mask": mask, "x": x, "y": y})
                    if mask & 1 and (x, y) == (12, 15):
                        self.clicked = True
                elif message == 6:
                    length = struct.unpack(">3xI", self.receive(7))[0]
                    self.events.append({"kind": "clipboard", "text": self.receive(length).decode("latin-1")})
                else:
                    raise ValueError(f"Unsupported RFB client message {message}")
        except (EOFError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            if not self.stop.is_set():
                self.errors.append(traceback.format_exc())
        finally:
            if self.connection is not None:
                self.connection.close()

    def close(self):
        self.stop.set()
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.socket.close()
        self.thread.join(2)


def payload(result):
    if result.isError:
        raise AssertionError("MCP tool failed: " + " ".join(getattr(x, "text", "") for x in result.content))
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(next(x.text for x in result.content if x.type == "text"))


async def run(args, report, fixture):
    environment = dict(os.environ)
    environment.update(VNC_HOST="127.0.0.1", VNC_PORT=str(fixture.port), VNC_TIMEOUT="5")
    environment.pop("VNC_PASSWORD", None)
    params = StdioServerParameters(command=sys.executable, args=[str(args.source / "main.py"), "mcp"], cwd=str(args.source), env=environment)
    stderr_path = args.output.with_suffix(".stderr.log")
    with stderr_path.open("w", encoding="utf-8") as error_log:
        async with stdio_client(params, errlog=error_log) as streams:
            async with ClientSession(*streams) as session:
                init = await session.initialize()
                report["server_info"] = init.serverInfo.model_dump()
                tools = await session.list_tools()
                report["tools"] = [tool.name for tool in tools.tools]
                required = {"vnc_connect", "vnc_screenshot", "vnc_click", "vnc_type", "vnc_disconnect"}
                assert required.issubset(report["tools"])
                report["checks"]["mcp_initialize_and_tools"] = True
                initial = payload(await session.call_tool("vnc_status"))
                assert initial["connected"] is False
                connected = payload(await session.call_tool("vnc_connect"))
                assert connected["connected"] and connected["width"] == 64 and connected["height"] == 48
                report["checks"]["rfb_connect"] = True

                async def capture(label):
                    result = await session.call_tool("vnc_screenshot", {"image_format": "png"})
                    assert not result.isError, [getattr(x, "text", "") for x in result.content]
                    images = [x for x in result.content if x.type == "image"]
                    assert len(images) == 1, f"Expected MCP ImageContent, got {[x.type for x in result.content]}"
                    assert images[0].mimeType == "image/png"
                    png = base64.b64decode(images[0].data)
                    image = Image.open(BytesIO(png)).convert("RGB")
                    assert image.size == (64, 48)
                    expected = bytes(channel for y in range(48) for x in range(64) for channel in fixture.rgb(x, y))
                    assert image.tobytes() == expected, "Screenshot pixels do not match the RFB framebuffer"
                    path = args.output.with_name(label + ".png")
                    path.write_bytes(png)
                    return image

                before = await capture("vnc-fixture-before")
                report["checks"]["native_mcp_png_and_exact_pixels"] = True
                payload(await session.call_tool("vnc_click", {"x": 12, "y": 15}))
                after = await capture("vnc-fixture-after")
                assert before.getpixel((12, 15)) != after.getpixel((12, 15)) == (255, 0, 255)
                assert any(e.get("mask") == 1 and e.get("x") == 12 and e.get("y") == 15 for e in fixture.events)
                report["checks"]["pointer_and_changed_framebuffer"] = True
                jpeg = await session.call_tool("vnc_screenshot", {"image_format": "jpeg"})
                assert not jpeg.isError
                compressed = next(x for x in jpeg.content if x.type == "image")
                assert compressed.mimeType == "image/jpeg"
                with Image.open(BytesIO(base64.b64decode(compressed.data))) as encoded:
                    assert encoded.size == (64, 48) and encoded.format == "JPEG"
                report["checks"]["explicit_jpeg_preserves_native_coordinates"] = True
                payload(await session.call_tool("vnc_keypress", {"key": "ctrl+l"}))
                payload(await session.call_tool("vnc_type", {"text": "Hi-中\n", "method": "keys"}))
                await asyncio.sleep(0.1)
                keys = [e["key"] for e in fixture.events if e["kind"] == "key" and e["down"]]
                assert 0x01004E2D in keys and 45 in keys and 0xFF0D in keys
                report["checks"]["keyboard_ascii_unicode_protocol_events"] = True
                payload(await session.call_tool("vnc_scroll", {"x": 20, "y": 20, "direction": "down", "amount": 2}))
                payload(await session.call_tool("vnc_drag", {"x1": 3, "y1": 4, "x2": 8, "y2": 9}))
                report["checks"]["scroll_and_drag"] = True
                payload(await session.call_tool("vnc_clipboard_set", {"text": "roundtrip fixture"}))
                clipboard = payload(await session.call_tool("vnc_clipboard_get"))
                assert clipboard["text"] == "fixture clipboard"
                await asyncio.sleep(0.1)
                assert any(e.get("text") == "roundtrip fixture" for e in fixture.events)
                report["checks"]["rfb_clipboard_both_directions"] = True
                invalid = await session.call_tool("vnc_click", {"x": -1, "y": 0})
                assert invalid.isError
                report["checks"]["out_of_bounds_input_rejected"] = True
                chinese_clipboard = await session.call_tool("vnc_clipboard_set", {"text": "中文"})
                assert chinese_clipboard.isError
                report["limitations"].append("This fixture has only the legacy Latin-1 clipboard. UTF-8 clipboard and guest-text channels are covered separately; keyboard events here reached only a synthetic server.")
                payload(await session.call_tool("vnc_key_down", {"key": "shift"}))
                payload(await session.call_tool("vnc_mouse_down", {"x": 5, "y": 6, "button": "right"}))
                disconnected = payload(await session.call_tool("vnc_disconnect"))
                assert not disconnected["connected"]
                await asyncio.sleep(0.1)
                assert any(e["kind"] == "key" and e["key"] == 0xFFE1 and not e["down"] for e in fixture.events)
                assert fixture.events[-1]["kind"] == "pointer" and fixture.events[-1]["mask"] == 0
                report["checks"]["disconnect_releases_held_input"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "source": str(args.source), "python": sys.version, "scope": "Actual MCP stdio plus loopback synthetic RFB server; no real desktop input", "checks": {}, "limitations": [], "success": False}
    fixture = RfbFixture()
    fixture.thread.start()
    try:
        asyncio.run(asyncio.wait_for(run(args, report, fixture), timeout=40))
        assert not fixture.errors, fixture.errors
        report["success"] = True
    except BaseException:
        report["failure"] = traceback.format_exc()
    finally:
        fixture.close()
        report["fixture_errors"] = fixture.errors
        report["observed_rfb_events"] = fixture.events
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in ("observed_rfb_events", "failure")}, ensure_ascii=False, indent=2))
        if not report["success"]:
            print(report["failure"], file=sys.stderr)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
