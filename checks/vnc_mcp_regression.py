"""MCP/RFB lifecycle regressions. Input reaches synthetic desktops only."""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import hmac
import os
from pathlib import Path
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES


class Peer(socketserver.BaseRequestHandler):
    def read(self, size):
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

    def handle(self):
        owner = self.server.owner
        with owner.lock:
            owner.connections.add(self.request)
            owner.accepted += 1
        self.request.settimeout(0.1)
        pixel_format = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        clipboard_sent = False
        try:
            self.request.sendall(b"RFB 003.008\n")
            assert self.read(12) == b"RFB 003.008\n"
            if owner.password is None:
                self.request.sendall(b"\x01\x01")
                assert self.read(1) == b"\x01"
                self.request.sendall(b"\x00\x00\x00\x00")
            else:
                self.request.sendall(b"\x01\x02")
                assert self.read(1) == b"\x02"
                challenge = bytes(range(16))
                self.request.sendall(challenge)
                password = owner.password.encode("ascii")[:8].ljust(8, b"\x00")
                key = bytes(int(f"{value:08b}"[::-1], 2) for value in password)
                encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
                expected = encryptor.update(challenge) + encryptor.finalize()
                if not hmac.compare_digest(self.read(16), expected):
                    reason = b"fixture authentication failed"
                    self.request.sendall(b"\x00\x00\x00\x01" + struct.pack(">I", len(reason)) + reason)
                    return
                self.request.sendall(b"\x00\x00\x00\x00")
            assert self.read(1) == b"\x01", "All VNC connections must use shared mode"
            name = owner.name.encode()
            self.request.sendall(struct.pack(">HH", 64, 48) + struct.pack(">BBBBHHHBBB3x", *pixel_format) + struct.pack(">I", len(name)) + name)
            while True:
                message = self.read(1)[0]
                if message == 0:
                    self.read(3)
                    pixel_format = struct.unpack(">BBBBHHHBBB3x", self.read(16))
                elif message == 2:
                    _, count = struct.unpack(">BH", self.read(3))
                    self.read(count * 4)
                elif message == 3:
                    incremental, x0, y0, requested_width, requested_height = struct.unpack("!BHHHH", self.read(9))
                    owner.framebuffer_requests.append((incremental, x0, y0, requested_width, requested_height))
                    if owner.silent:
                        continue
                    bpp, _, endian, _, rm, gm, bm, rs, gs, bs = pixel_format
                    frame = bytearray()
                    x0, y0, width, height = (x0, y0, requested_width, requested_height) if owner.honor_regions else (0, 0, 64, 48)
                    for y in range(y0, y0 + height):
                        for x in range(x0, x0 + width):
                            r, g, b = owner.rgb(x, y)
                            value = ((r * rm // 255) << rs) | ((g * gm // 255) << gs) | ((b * bm // 255) << bs)
                            frame.extend(value.to_bytes(bpp // 8, "big" if endian else "little"))
                    self.request.sendall(b"\x00\x00\x00\x01" + struct.pack(">HHHHi", x0, y0, width, height, 0) + frame)
                    if not clipboard_sent:
                        text = (owner.name + " clipboard").encode()
                        self.request.sendall(b"\x03\x00\x00\x00" + struct.pack(">I", len(text)) + text)
                        clipboard_sent = True
                elif message == 4:
                    down, key = struct.unpack(">B2xI", self.read(7))
                    owner.events.append(("key", bool(down), key))
                    owner.key_event_times.append((bool(down), key, time.monotonic()))
                elif message == 5:
                    mask, x, y = struct.unpack(">BHH", self.read(5))
                    owner.events.append(("pointer", mask, x, y))
                elif message == 6:
                    self.read(struct.unpack(">3xI", self.read(7))[0])
                else:
                    raise AssertionError(f"Unexpected RFB message {message}")
        except (EOFError, OSError):
            pass
        except Exception:
            owner.errors.append(traceback.format_exc())
        finally:
            with owner.lock:
                owner.connections.discard(self.request)


class Fixture:
    def __init__(self, name, color, password=None, honor_regions=False):
        self.name, self.color = name, color
        self.password = password
        self.silent = self.closed = False
        self.events, self.key_event_times, self.errors = [], [], []
        self.honor_regions = honor_regions
        self.framebuffer_requests = []
        self.connections = set()
        self.accepted = 0
        self.lock = threading.Lock()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Peer)
        self.server.daemon_threads = True
        self.server.owner = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def rgb(self, x, y):
        return (x * 3) % 256, (y * 5) % 256, self.color

    def drop(self):
        with self.lock:
            peers = tuple(self.connections)
        for peer in peers:
            try:
                peer.shutdown(socket.SHUT_RDWR)
                peer.close()
            except OSError:
                pass

    def close(self):
        self.closed = True
        self.drop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)


def payload(result):
    assert not result.isError, [getattr(item, "text", "") for item in result.content]
    return result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))


async def run_mode(args, mode, a, b, report):
    env = dict(os.environ)
    for name in ("VNC_HOST", "VNC_PORT", "VNC_PASSWORD"):
        env.pop(name, None)
    env.update(VNC_TIMEOUT="0.5", VNC_SCREENSHOT_MODE=mode)
    params = StdioServerParameters(command=sys.executable, args=[str(args.source / "main.py"), "mcp"], cwd=str(args.source), env=env)
    checks = report[mode] = {}
    with args.output.with_name(args.output.stem + "-" + mode + ".stderr.log").open("w", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()

                async def call(name, **kw):
                    return payload(await session.call_tool(name, kw))

                async def connect(target):
                    result = await call("vnc_connect", host="127.0.0.1", port=target.port)
                    assert result["connected"] and result["name"] == target.name
                    return result

                async def screenshot(image_format="jpeg"):
                    result = await session.call_tool("vnc_screenshot", {"image_format": image_format})
                    assert not result.isError, [getattr(item, "text", "") for item in result.content]
                    content = next(item for item in result.content if item.type == "image")
                    assert content.mimeType == "image/" + image_format
                    image = Image.open(BytesIO(base64.b64decode(content.data)))
                    assert image.size == (64, 48)
                    return image

                for arguments in ({}, {"host": "127.0.0.1"}, {"port": a.port}):
                    assert (await session.call_tool("vnc_connect", arguments)).isError
                checks["target_requires_host_and_port"] = True
                await connect(a)
                for _ in range(3):
                    await screenshot()
                lossless = await screenshot("png")
                assert lossless.getpixel((20, 17)) == a.rgb(20, 17)
                for bad in ({"image_format": "gif"}, {"quality": 96}):
                    assert (await session.call_tool("vnc_screenshot", bad)).isError
                assert (await call("vnc_status"))["connected"]
                checks["repeated_jpeg_lossless_png_and_validation"] = True
                if mode == "fresh":
                    assert a.accepted >= 5
                await connect(b)
                assert (await screenshot("png")).getpixel((20, 17)) == b.rgb(20, 17)
                await asyncio.sleep(0.05)
                assert (await call("vnc_clipboard_get"))["text"] == b.name + " clipboard"
                checks["switch_selects_new_desktop_and_clipboard"] = True
                await call("vnc_key_down", key="shift")
                await call("vnc_mouse_down", x=7, y=8, button="right")
                await connect(a)
                await asyncio.sleep(0.05)
                assert ("key", False, 0xFFE1) in b.events
                assert ("pointer", 0, 7, 8) in b.events
                checks["switch_releases_previous_desktop_input"] = True
                with socket.socket() as reserved:
                    reserved.bind(("127.0.0.1", 0))
                    unused_port = reserved.getsockname()[1]
                    assert (await session.call_tool("vnc_connect", {"host": "127.0.0.1", "port": unused_port})).isError
                disconnected = await call("vnc_status")
                assert not disconnected["connected"]
                assert all(disconnected[key] is None for key in ("host", "port", "name", "width", "height"))
                checks["failed_switch_clears_stale_target"] = True
                await connect(a)
                a.drop()
                await asyncio.sleep(0.15)
                assert not (await call("vnc_status"))["connected"]
                before = len(a.events)
                assert (await session.call_tool("vnc_keypress", {"key": "enter"})).isError
                assert len(a.events) == before
                checks["connection_loss_requires_explicit_reconnect_no_input_replay"] = True
                await connect(a)
                a.silent = True
                started = time.monotonic()
                assert (await session.call_tool("vnc_screenshot")).isError
                assert time.monotonic() - started < 2
                a.silent = False
                status = await call("vnc_status")
                if mode == "refresh":
                    assert not status["connected"]
                    await connect(a)
                else:
                    assert status["connected"], "Failed snapshot must not poison the control channel"
                await screenshot()
                checks["screenshot_timeout_cleanup_and_recovery"] = True
                assert (await session.call_tool("vnc_keypress", {"key": "nonexistentkey"})).isError
                assert not (await call("vnc_status"))["connected"]
                await connect(a)
                await screenshot()
                checks["failed_proxy_command_cleared_and_reconnects"] = True
                await call("vnc_key_down", key="ctrl")
                await call("vnc_mouse_down", x=9, y=10, button="left")
                # Leave input held: EOF must run the server lifespan cleanup.
        for _ in range(20):
            if ("key", False, 0xFFE3) in a.events and ("pointer", 0, 9, 10) in a.events:
                break
            await asyncio.sleep(0.05)
        assert ("key", False, 0xFFE3) in a.events
        assert ("pointer", 0, 9, 10) in a.events
        checks["stdio_shutdown_releases_held_input"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "scope": "Real MCP stdio and multi-client synthetic RFB; no real desktop input", "success": False}
    fixtures = []
    try:
        for mode in ("refresh", "fresh"):
            a, b = Fixture(mode + " A", 41), Fixture(mode + " B", 193)
            fixtures += [a, b]
            asyncio.run(asyncio.wait_for(run_mode(args, mode, a, b, report), timeout=30))
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
