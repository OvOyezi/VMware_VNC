"""Project-owned asyncio RFB transport and a small synchronous boundary.

The protocol reader publishes only completed framebuffer updates. Input and
network state live on one asyncio thread; image decoding is offloaded. The
synchronous boundary serves the existing CLI and serialized session workers.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
import math
import struct
import threading
import time
from typing import Any, Coroutine
from uuid import uuid4

from PIL import Image, ImageDraw

from .clipboard import ExtendedClipboard, MAX_CLIPBOARD_BYTES
from .rfb_codec import FrameDecoder, PixelFormat, MAX_FRAME_PIXELS, MAX_RECT_BYTES


EXT_CLIPBOARD = -1063131698
FENCE = -312
CONTINUOUS = -313
REQUEST = 0x80000000
MAX_NAME_BYTES = 65_536
MAX_UPDATE_RECTS = 65_535

KEYS = {
    "bsp": 0xff08, "tab": 0xff09, "enter": 0xff0d, "esc": 0xff1b,
    "ins": 0xff63, "delete": 0xffff, "del": 0xffff, "home": 0xff50,
    "end": 0xff57, "left": 0xff51, "up": 0xff52, "right": 0xff53,
    "down": 0xff54, "pgup": 0xff55, "pgdn": 0xff56, "space": 0x20,
    "shift": 0xffe1, "shift_r": 0xffe2, "ctrl": 0xffe3, "ctrl_r": 0xffe4,
    "alt": 0xffe9, "alt_r": 0xffea, "meta": 0xffeb, "super": 0xffeb,
    "super_r": 0xffec, "capslock": 0xffe5, "numlock": 0xff7f,
    "scrolllock": 0xff14, "print": 0xff61, "sysrq": 0xff15,
    "pause": 0xff13, "break": 0xff6b, "menu": 0xff67,
    "minus": 0x2d, "equal": 0x3d, "slash": 0x2f, "backslash": 0x5c,
}
KEYS.update({f"f{index}": 0xffbd + index for index in range(1, 36)})


class RFBProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def keysyms(key: str) -> list[int]:
    # Hypervisor consoles translate keysyms back into physical keyboard events.
    # They need the Shift modifier as well as the printable keysym; sending
    # uppercase ASCII alone becomes lowercase on a console keyboard.
    shifted = len(key) == 1 and ("A" <= key <= "Z" or key in '~!@#$%^&*()_+{}|:"<>?')
    parts = key.split("-") if len(key) > 1 else [key]
    result = [KEYS["shift"]] if shifted else []
    for part in parts:
        if part.lower() in KEYS:
            result.append(KEYS[part.lower()])
        elif len(part) == 1:
            value = ord(part)
            result.append(value if value <= 255 else 0x01000000 | value)
        else:
            raise ValueError(f"unknown key name: {part[:40]}")
    return result


@dataclass(frozen=True)
class FrameSnapshot:
    image: Image.Image
    frame_id: str
    sequence: int
    received_at: float
    updated_regions: tuple[tuple[int, int, int, int], ...]
    valid_region: tuple[int, int, int, int] | None = None


class AsyncRFB:
    """One RFB connection; all methods and mutable protocol state share a loop."""

    def __init__(self, host: str, port: int, password: str | None, timeout: float,
                 encoding: str = "auto", initial_region: tuple[int, int, int, int] | None = None) -> None:
        self.host, self.port, self.password = host, port, password
        self.timeout, self.encoding = timeout, encoding
        self.initial_region = initial_region
        self.width = self.height = 0
        self.name = ""
        self.received_bytes = 0
        self.wire_encoding: str | None = None
        self.ext_clipboard = ExtendedClipboard()
        self.clipboard_sent = threading.Event()
        self.closed = threading.Event()
        self.latest_clipboard = ""
        self.clipboard_encoding = "iso-8859-1"
        self.protocol_error: str | None = None
        self.snapshot: FrameSnapshot | None = None
        self._identity = uuid4().hex
        self._sequence = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._frame_event = asyncio.Event()
        self._request_lock = asyncio.Lock()
        self._fences: dict[bytes, asyncio.Future] = {}
        self.fence_supported = False
        self.continuous_supported = False
        self.continuous_enabled = False
        self._pointer_x = self._pointer_y = self._buttons = 0
        self._decoder = FrameDecoder(PixelFormat())

    async def _read(self, count: int) -> bytes:
        if not 0 <= count <= MAX_RECT_BYTES:
            raise ValueError("RFB payload exceeds the byte limit")
        if self._reader is None:
            raise ConnectionError("RFB reader is unavailable")
        data = await self._reader.readexactly(count)
        self.received_bytes += len(data)
        return data

    async def _write(self, data: bytes) -> None:
        if self.closed.is_set() or self._writer is None:
            raise ConnectionError(self.protocol_error or "RFB connection is closed")
        self._writer.write(data)
        await self._writer.drain()

    async def connect(self) -> None:
        try:
            async with asyncio.timeout(self.timeout):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
                await self._handshake()
                self._reader_task = asyncio.create_task(self._receive(), name="rfb-reader")
                await self.capture(self.initial_region)
        except BaseException:
            await self.close()
            raise

    async def _reason(self) -> str:
        length, = struct.unpack("!I", await self._read(4))
        if length > MAX_NAME_BYTES:
            raise ValueError("RFB reason string exceeds the byte limit")
        return (await self._read(length)).decode("utf-8", "replace")[:300]

    async def _handshake(self) -> None:
        banner = await self._read(12)
        if not (banner.startswith(b"RFB ") and banner[7:8] == b"." and banner[-1:] == b"\n"):
            raise ValueError("invalid RFB protocol banner")
        try:
            major, minor = int(banner[4:7]), int(banner[8:11])
        except ValueError as exc:
            raise ValueError("invalid RFB protocol version") from exc
        if major != 3 or minor < 3:
            raise ValueError("unsupported RFB protocol version")
        version = 8 if minor >= 8 else 7 if minor >= 7 else 3
        await self._write(f"RFB 003.{version:03}\n".encode("ascii"))
        if version == 3:
            security, = struct.unpack("!I", await self._read(4))
            if security == 0:
                raise ConnectionError("RFB refused connection: " + await self._reason())
        else:
            count = (await self._read(1))[0]
            if not count:
                raise ConnectionError("RFB refused connection: " + await self._reason())
            offered = await self._read(count)
            security = 2 if self.password is not None and 2 in offered else 1 if 1 in offered else 2 if 2 in offered else 0
            if security == 0:
                raise ValueError("no supported RFB security type (None or VNC Authentication)")
            await self._write(bytes([security]))
        if security == 2:
            if self.password is None:
                raise ValueError("VNC Authentication requires an explicit password")
            from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
            from cryptography.hazmat.primitives.ciphers import Cipher, modes
            challenge = await self._read(16)
            raw = self.password.encode("latin-1", "strict")[:8].ljust(8, b"\0")
            key = bytes(int(f"{value:08b}"[::-1], 2) for value in raw)
            encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
            await self._write(encryptor.update(challenge) + encryptor.finalize())
        elif security != 1:
            raise ValueError("unsupported RFB security type")
        if security == 2 or version == 8:
            result, = struct.unpack("!I", await self._read(4))
            if result:
                reason = await self._reason() if version == 8 else "authentication failed"
                raise ConnectionError("RFB authentication failed: " + reason)
        await self._write(b"\x01")  # shared display: do not evict other clients
        init = await self._read(24)
        width, height = struct.unpack_from("!HH", init)
        self._validate_size(width, height)
        server_format = PixelFormat.from_bytes(init[4:20])
        name_length, = struct.unpack_from("!I", init, 20)
        if name_length > MAX_NAME_BYTES:
            raise ValueError("RFB desktop name exceeds the byte limit")
        self.name = (await self._read(name_length)).decode("utf-8", "replace")
        self.width, self.height = width, height
        # Keep the server's supported true-color layout. VMware consoles may
        # ignore a change to the channel shifts; requesting the advertised
        # format avoids interpreting BGRX bytes as RGBX. Decoders handle the
        # exact advertised masks and byte order rather than guessing by color.
        self._decoder = FrameDecoder(server_format)
        await self._write(b"\x00\0\0\0" + server_format.to_bytes())
        choices = {"auto": [16, 7, 5, 1, 0], "zrle": [16, 0],
                   "tight": [7, 0], "hextile": [5, 0], "raw": [0]}
        if self.encoding not in choices:
            raise ValueError("unknown RFB encoding preference")
        encodings = choices[self.encoding] + [-223, -224, -239, -232, EXT_CLIPBOARD, FENCE, CONTINUOUS]
        await self._write(struct.pack("!BBH", 2, 0, len(encodings)) + struct.pack("!" + "i" * len(encodings), *encodings))

    @staticmethod
    def _validate_size(width: int, height: int) -> None:
        if width <= 0 or height <= 0 or width * height > MAX_FRAME_PIXELS:
            raise ValueError("RFB desktop exceeds the pixel limit or has empty dimensions")

    async def _receive(self) -> None:
        try:
            while not self.closed.is_set():
                kind = (await self._read(1))[0]
                if kind == 0:
                    await self._update()
                elif kind == 1:
                    header = await self._read(5)
                    count, = struct.unpack_from("!H", header, 3)
                    await self._read(count * 6)
                elif kind == 2:
                    continue
                elif kind == 3:
                    await self._clipboard()
                elif kind == 248:
                    await self._fence_message()
                elif kind == 150:
                    self.continuous_supported = True
                else:
                    raise ValueError(f"unsupported RFB server message {kind}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.protocol_error = exc.code if isinstance(exc, RFBProtocolError) else f"{type(exc).__name__}: {str(exc)[:240]}"
        finally:
            await self.close()

    async def _update(self) -> None:
        _, count = struct.unpack("!BH", await self._read(3))
        if count > MAX_UPDATE_RECTS:
            raise ValueError("too many RFB rectangles")
        previous = self.snapshot
        same_geometry = previous is not None and previous.image.size == (self.width, self.height)
        canvas = await asyncio.to_thread(previous.image.copy) if same_geometry else Image.new("RGB", (self.width, self.height))
        coverage = None if same_geometry else Image.new("1", canvas.size)
        regions = []
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack("!HHHHi", await self._read(12))
            if encoding == -224:  # LastRect
                break
            if encoding == -223:
                self._validate_size(width, height)
                self.width, self.height = width, height
                resized = Image.new("RGB", (width, height))
                resized.paste(canvas.crop((0, 0, min(canvas.width, width), min(canvas.height, height))))
                canvas = resized
                # A resized surface needs a new complete server image.
                coverage = Image.new("1", canvas.size)
                continue
            if encoding == -239:  # separate cursor shape; never paint over pixels
                if width * height > MAX_FRAME_PIXELS:
                    raise ValueError("RFB cursor exceeds the pixel limit")
                await self._read(width * height * self._decoder.pixel_format.bytes_per_pixel
                                 + ((width + 7) // 8) * height)
                continue
            if encoding == -232:
                self._pointer_x, self._pointer_y = x, y
                continue
            if encoding < 0:
                raise ValueError(f"unsupported RFB pseudo-encoding {encoding}")
            if not width or not height or x + width > self.width or y + height > self.height:
                raise ValueError("RFB rectangle is outside the desktop")
            if coverage is not None and encoding == 1:
                raise ValueError("CopyRect requires a completed framebuffer at the current size")
            await self._decoder.decode(self._read, canvas, (x, y, width, height), encoding)
            self.wire_encoding = {0: "raw", 1: "copyrect", 5: "hextile", 7: "tight", 16: "zrle"}.get(encoding, str(encoding))
            regions.append((x, y, width, height))
            if coverage is not None:
                ImageDraw.Draw(coverage).rectangle((x, y, x + width - 1, y + height - 1), fill=1)
        required = self.initial_region if previous is None and self.initial_region else (0, 0, self.width, self.height)
        left, top, span_x, span_y = required
        if coverage is not None and coverage.crop((left, top, left + span_x, top + span_y)).getextrema()[0] == 0:
            # DesktopSize may be sent separately from its following pixel update.
            if previous is not None:
                await self._write(struct.pack("!BBHHHH", 3, 0, 0, 0, self.width, self.height))
                return
            raise ValueError("initial RFB update did not cover the full desktop")
        self._sequence += 1
        self.snapshot = FrameSnapshot(canvas, f"{self._identity}:{self._sequence}", self._sequence,
                                      time.monotonic(), tuple(regions), self.initial_region if previous is None else previous.valid_region)
        event, self._frame_event = self._frame_event, asyncio.Event()
        event.set()

    async def _clipboard(self) -> None:
        length, = struct.unpack("!3xi", await self._read(7))
        if abs(length) > MAX_CLIPBOARD_BYTES or -4 < length < 0:
            raise RFBProtocolError("clipboard_size_limit")
        payload = await self._read(abs(length))
        if length < 0:
            messages, text, sent = self.ext_clipboard.receive(payload)
            for message in messages:
                await self._write(message)
            if text is not None:
                self.latest_clipboard, self.clipboard_encoding = text, "utf-8"
            if sent:
                self.clipboard_sent.set()
        else:
            self.latest_clipboard, self.clipboard_encoding = payload.decode("latin-1"), "iso-8859-1"

    async def _fence_message(self) -> None:
        flags, length = struct.unpack("!3xIB", await self._read(8))
        if length > 64:
            raise ValueError("RFB Fence payload exceeds 64 bytes")
        payload = await self._read(length)
        self.fence_supported = True
        if flags & REQUEST:
            await self._write(struct.pack("!B3xIB", 248, flags & 3, length) + payload)
        else:
            future = self._fences.pop(payload, None)
            if future is not None and not future.done():
                future.set_result(None)

    async def fence(self) -> None:
        if not self.fence_supported:
            raise ValueError("server has not negotiated Fence")
        payload = uuid4().bytes
        future = asyncio.get_running_loop().create_future()
        self._fences[payload] = future
        try:
            await self._write(struct.pack("!B3xIB", 248, REQUEST | 1, len(payload)) + payload)
            await asyncio.wait_for(future, self.timeout)
        finally:
            self._fences.pop(payload, None)

    async def continuous(self, enabled: bool) -> None:
        if not self.continuous_supported:
            raise ValueError("server has not negotiated ContinuousUpdates")
        await self._write(struct.pack("!BBHHHH", 150, int(enabled), 0, 0, self.width, self.height))
        self.continuous_enabled = enabled

    async def capture(self, region: tuple[int, int, int, int] | None = None,
                      incremental: bool = False, timeout: float | None = None) -> FrameSnapshot:
        async with self._request_lock:
            if self.snapshot is not None and self.snapshot.valid_region is not None and region != self.snapshot.valid_region:
                raise ValueError("region-only initial connection cannot expose uninitialized pixels outside its region")
            before = self._sequence
            event = self._frame_event
            x, y, width, height = region or (0, 0, self.width, self.height)
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > self.width or y + height > self.height:
                raise ValueError("capture region is outside the desktop")
            await self._write(struct.pack("!BBHHHH", 3, int(incremental), x, y, width, height))
            async with asyncio.timeout(timeout or self.timeout):
                while self._sequence <= before:
                    await event.wait()
                    if self.closed.is_set():
                        raise ConnectionError(self.protocol_error or "RFB connection closed during capture")
                    event = self._frame_event
            assert self.snapshot is not None
            return self.snapshot

    async def key_down(self, key: str) -> None:
        for value in keysyms(key):
            await self._write(struct.pack("!BB2xI", 4, 1, value))

    async def key_up(self, key: str) -> None:
        for value in reversed(keysyms(key)):
            await self._write(struct.pack("!BB2xI", 4, 0, value))

    async def key_press(self, key: str) -> None:
        await self.key_down(key)
        await self.key_up(key)

    async def pointer(self, x: int | None = None, y: int | None = None,
                      button: int | None = None, down: bool = True) -> None:
        if x is not None:
            self._pointer_x, self._pointer_y = x, y
        if button is not None:
            if not 1 <= button <= 8:
                raise ValueError("RFB mouse button must be 1-8")
            mask = 1 << (button - 1)
            self._buttons = self._buttons | mask if down else self._buttons & ~mask
        await self._write(struct.pack("!BBHH", 5, self._buttons, self._pointer_x, self._pointer_y))

    async def set_clipboard(self, text: str, extended: bool) -> None:
        if extended:
            data, sent = self.ext_clipboard.set_text(text)
            self.clipboard_sent.clear()
            await self._write(data)
            if sent:
                self.clipboard_sent.set()
        else:
            data = text.encode("latin-1", "strict")
            if len(data) > MAX_CLIPBOARD_BYTES:
                raise ValueError("clipboard_size_limit")
            await self._write(struct.pack("!B3xI", 6, len(data)) + data)

    async def close(self, keys: tuple = (), buttons: tuple = ()) -> None:
        if not self.closed.is_set():
            try:
                for key in keys:
                    await self.key_up(key)
                for button in buttons:
                    number = button if type(button) is int else {"left": 1, "middle": 2, "right": 3}[button]
                    await self.pointer(button=number, down=False)
            except (OSError, ConnectionError, ValueError):
                pass
        self.closed.set()
        self._frame_event.set()
        for future in self._fences.values():
            if not future.done():
                future.cancel()
        self._fences.clear()
        task, self._reader_task = self._reader_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass
        self._reader = None
        self.snapshot = None
        self.latest_clipboard = ""
        self.ext_clipboard.local_text = None
        self.password = None


class _IOLoop:
    _lock = threading.Lock()
    _instance: _IOLoop | None = None

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        def run() -> None:
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()
        self.thread = threading.Thread(target=run, name="vnc-rfb-io", daemon=True)
        self.thread.start()
        if not ready.wait(5):
            raise TimeoutError("native RFB event loop did not start")

    @classmethod
    def get(cls) -> _IOLoop:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def call(self, coroutine: Coroutine, timeout: float) -> Any:
        if threading.current_thread() is self.thread:
            coroutine.close()
            raise RuntimeError("synchronous RFB boundary cannot run on its IO thread")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("RFB operation timed out") from exc


def runtime_info() -> dict:
    runtime = _IOLoop._instance
    return {"engine": "native-asyncio-rfb", "io_threads": int(bool(runtime and runtime.thread.is_alive()))}


class RFBConnection:
    """Explicit blocking facade used only at the CLI/session worker boundary."""

    def __init__(self, host: str, port: int, password: str | None, timeout: float,
                 encoding: str = "auto", initial_region: tuple[int, int, int, int] | None = None) -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("RFB timeout must be 0-60 seconds")
        self.timeout = timeout
        self._runtime = _IOLoop.get()
        self._state = AsyncRFB(host, port, password, timeout, encoding, initial_region)
        self.last_snapshot: FrameSnapshot | None = None
        self._runtime.call(self._state.connect(), timeout + 1)
        self.last_snapshot = self._state.snapshot

    def __getattr__(self, name: str) -> Any:
        # State reads only; methods below are explicit and have no deferred chain.
        if name in {"closed", "ext_clipboard", "clipboard_sent", "latest_clipboard",
                    "clipboard_encoding", "protocol_error", "password", "received_bytes",
                    "wire_encoding", "width", "height", "name", "fence_supported",
                    "continuous_supported", "continuous_enabled"}:
            return getattr(self._state, name)
        raise AttributeError(name)

    @property
    def screen(self) -> Image.Image | None:
        snapshot = self._state.snapshot
        return snapshot.image if snapshot else None

    @property
    def frame_sequence(self) -> int:
        return self._state.snapshot.sequence if self._state.snapshot else 0

    @property
    def frame_id(self) -> str | None:
        return self._state.snapshot.frame_id if self._state.snapshot else None

    @property
    def frame_time(self) -> float | None:
        return self._state.snapshot.received_at if self._state.snapshot else None

    @property
    def updated_regions(self) -> tuple:
        return self._state.snapshot.updated_regions if self._state.snapshot else ()

    def initial_snapshot(self) -> FrameSnapshot:
        if self.last_snapshot is None or self.closed.is_set():
            raise ConnectionError("RFB initial framebuffer is unavailable")
        return self.last_snapshot

    def capture_frame(self, frames: list, region: tuple[int, int, int, int] | None = None,
                      timeout: float | None = None, incremental: bool = False) -> None:
        snapshot = self._runtime.call(self._state.capture(region, incremental, timeout), (timeout or self.timeout) + 0.2)
        self.last_snapshot = snapshot
        frames.append(snapshot.image.copy())

    def refreshScreen(self, incremental: bool = False) -> None:
        self.capture_frame([], incremental=incremental)

    def keyDown(self, key: str) -> None:
        self._runtime.call(self._state.key_down(key), self.timeout)

    def keyUp(self, key: str) -> None:
        self._runtime.call(self._state.key_up(key), self.timeout)

    def keyPress(self, key: str) -> None:
        self._runtime.call(self._state.key_press(key), self.timeout)

    def mouseMove(self, x: int, y: int) -> None:
        self._runtime.call(self._state.pointer(x, y), self.timeout)

    def mouseDown(self, button: int) -> None:
        self._runtime.call(self._state.pointer(button=button), self.timeout)

    def mouseUp(self, button: int) -> None:
        self._runtime.call(self._state.pointer(button=button, down=False), self.timeout)

    def type_batch(self, text: str, control: Any) -> None:
        for char in text:
            control.check()
            if char != "\r":
                self.keyPress("enter" if char == "\n" else "tab" if char == "\t" else "minus" if char == "-" else char)

    def paste(self, text: str) -> None:
        self._runtime.call(self._state.set_clipboard(text, False), self.timeout)

    def set_clipboard_utf8(self, text: str) -> None:
        self._runtime.call(self._state.set_clipboard(text, True), self.timeout)

    def fence(self) -> None:
        self._runtime.call(self._state.fence(), self.timeout + 0.2)

    def continuous(self, enabled: bool) -> None:
        self._runtime.call(self._state.continuous(enabled), self.timeout)

    def close(self, keys: tuple = (), buttons: tuple = ()) -> None:
        self._runtime.call(self._state.close(keys, buttons), min(self.timeout, 2) + 1)
        self.last_snapshot = None
