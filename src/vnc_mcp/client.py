"""Synchronous VNC session wrapper around vncdotool."""

from __future__ import annotations

import math
from dataclasses import replace
from functools import partial
import struct
import threading
import time
from typing import Any
from uuid import uuid4

from vncdotool import api, rfb
from vncdotool.client import VNCDoToolClient, VNCDoToolFactory
from twisted.internet import reactor

from .imaging import Screenshot, encode_frame, validate_encoding
from .operations import ActionCancelled, ActionControl, ActionTimedOut
from .clipboard import ExtendedClipboard, MAX_CLIPBOARD_BYTES, validate_text
from .guest import GuestProfile, VmrunGuest


_REACTOR_START_LOCK = threading.Lock()


class _ClipboardClient(VNCDoToolClient):
    def dataReceived(self, data: bytes) -> None:
        self.factory.received_bytes += len(data)
        super().dataReceived(data)

    def setEncodings(self, encodings) -> None:
        preference = self.factory.network_encoding
        # vncdotool 1.4.2's ZRLE packed-palette reader ignores per-row
        # padding. Do not advertise it: real narrow TigerVNC rectangles fail.
        choices = {"auto": [rfb.Encoding.HEXTILE, rfb.Encoding.RAW],
                   "hextile": [rfb.Encoding.HEXTILE, rfb.Encoding.RAW], "raw": [rfb.Encoding.RAW]}
        pseudos = [value for value in encodings if value < 0]
        super().setEncodings([*choices[preference], *pseudos, rfb.Encoding.PSEUDO_EXTENDED_CLIPBOARD])

    def _handleRectangle(self, block: bytes) -> None:
        encoding = struct.unpack("!HHHHi", block)[4]
        if encoding >= 0:
            self.factory.wire_encoding = {0: "raw", 5: "hextile", 16: "zrle"}.get(encoding, str(encoding))
        super()._handleRectangle(block)

    def _handleServerCutText(self, block: bytes) -> None:
        length, = struct.unpack("!3xi", block)
        if abs(length) > MAX_CLIPBOARD_BYTES or (length < 0 and length > -4):
            self.factory.protocol_error = "clipboard_size_limit"
            self.transport.abortConnection()
            return
        self.expect(self._receive_clipboard, abs(length), length < 0)

    def _receive_clipboard(self, block: bytes, extended: bool) -> None:
        try:
            if extended:
                messages, text, sent = self.factory.ext_clipboard.receive(block)
                for message in messages:
                    self.transport.write(message)
                if text is not None:
                    self.copy_text(text)
                    self.factory.clipboard_encoding = "utf-8"
                if sent:
                    self.factory.clipboard_sent.set()
            else:
                self.copy_text(block.decode("iso-8859-1"))
                self.factory.clipboard_encoding = "iso-8859-1"
        except (ValueError, struct.error) as exc:
            self.factory.protocol_error = "invalid_extended_clipboard: " + str(exc)[:200]
            self.transport.abortConnection()
            return
        self.expect(self._handleConnection, 1)

    def set_clipboard_utf8(self, text: str) -> _ClipboardClient:
        message, sent = self.factory.ext_clipboard.set_text(text)
        self.factory.clipboard_sent.clear()
        self.transport.write(message)
        if sent:
            self.factory.clipboard_sent.set()
        return self

    def connectionMade(self) -> None:
        self.factory.active_protocol = self
        if self.factory.close_requested:
            self.transport.abortConnection()
            return
        super().connectionMade()

    def connectionLost(self, reason: Any) -> None:
        try:
            super().connectionLost(reason)
        finally:
            # The upstream proxy/factory/Deferred form reference cycles. Fresh
            # snapshots must not keep multi-megabyte framebuffers alive until
            # a cyclic GC pass; release large and private state on closure.
            self.screen = None
            self._packet.clear()
            self.factory.active_protocol = None
            self.factory.latest_clipboard = ""
            self.factory.ext_clipboard.local_text = None
            self.factory.closed.set()

    def copy_text(self, text: str) -> None:
        self.factory.latest_clipboard = text

    def capture_frame(self, frames: list) -> Any:
        # Keep the proxy's Deferred result as the protocol, just as captureScreen
        # does. Only copy a completed framebuffer here; CPU-heavy encoding must
        # run outside the reactor so unrelated connections can keep processing.
        def capture(result: Any) -> _ClipboardClient:
            if self.screen is None:
                raise RuntimeError("VNC server did not provide a framebuffer")
            frames.append(self.screen.copy())
            return self

        deferred = self.refreshScreen(incremental=False)
        deferred.addCallback(capture)
        return deferred

    def type_batch(self, text: str, control: ActionControl) -> _ClipboardClient:
        for char in text:
            control.check()
            if char == "\r":
                continue
            if char == "\n":
                self.keyPress("enter")
            elif char == "\t":
                self.keyPress("tab")
            elif ord(char) <= 0xFF:
                self.keyPress("minus" if char == "-" else char)
            else:
                self.unicode_key(ord(char))
        return self

    def unicode_key(self, codepoint: int) -> _ClipboardClient:
        # vncdotool's synchronous proxy chains method return values through its
        # connection Deferred. Raw keyEvent returns None and breaks that chain.
        # Emit both events on the reactor thread, then preserve the protocol.
        keysym = 0x01000000 | codepoint
        self.keyEvent(keysym, down=True)
        self.keyEvent(keysym, down=False)
        return self


class _ClipboardFactory(VNCDoToolFactory):
    protocol = _ClipboardClient
    force_caps = True

    def __init__(self, network_encoding: str = "auto") -> None:
        super().__init__()
        self.network_encoding = network_encoding
        self.wire_encoding: str | None = None
        self.received_bytes = 0
        self.ext_clipboard = ExtendedClipboard()
        self.clipboard_sent = threading.Event()
        self.clipboard_encoding = "iso-8859-1"
        self.protocol_error: str | None = None
        self.latest_clipboard = ""
        self.closed = threading.Event()
        self.close_requested = False
        self.active_protocol: _ClipboardClient | None = None


class VNCSession:
    def __init__(self, timeout: float = 15.0, screenshot_mode: str = "refresh", action_timeout: float = 20.0,
                 network_encoding: str = "auto") -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout must be finite and between 0 and 60 seconds")
        if not math.isfinite(action_timeout) or not 0 < action_timeout <= 60:
            raise ValueError("action_timeout must be finite and between 0 and 60 seconds")
        if screenshot_mode not in {"refresh", "fresh"}:
            raise ValueError("screenshot_mode must be refresh or fresh")
        if network_encoding not in {"auto", "raw", "hextile"}:
            raise ValueError("network_encoding must be auto, raw or hextile")
        self.network_encoding = network_encoding
        self.guest_profile: GuestProfile | None = None
        self.timeout = timeout
        self.screenshot_mode = screenshot_mode
        self.action_timeout = action_timeout
        self.connection_id: str | None = None
        self._control: ActionControl | None = None
        self.client: Any | None = None
        self.host: str | None = None
        self.port: int | None = None
        self.name: str | None = None
        self.width: int | None = None
        self.height: int | None = None
        self._lock = threading.RLock()
        self._held_keys: set[str] = set()
        self._held_buttons: set[str] = set()

    def connect(self, host: str, port: int = 5900, password: str | None = None,
                profile: GuestProfile | None = None) -> dict[str, Any]:
        if not host.strip():
            raise ValueError("host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if profile is not None and not profile.matches(host, port):
            raise ValueError("guest profile endpoint does not match this VNC connection")
        with self._lock:
            self.disconnect()
            endpoint_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            endpoint = f"{endpoint_host}::{port}"
            self.client = self._connect_client(endpoint, password)
            try:
                # api.connect starts the background reactor; an initial refresh waits
                # for protocol negotiation and the server's first framebuffer update.
                self._call("refreshScreen", incremental=False)
            except Exception:
                self.disconnect()
                raise
            self.host, self.port = host, port
            self.guest_profile = profile
            raw_name = getattr(self.client.protocol, "name", "")
            self.name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else str(raw_name)
            screen = getattr(self.client.protocol, "screen", None)
            if screen is not None:
                self.width, self.height = screen.size
            self.connection_id = uuid4().hex
            return self.status()

    def _connect_client(self, endpoint: str, password: str | None) -> Any:
        # vncdotool 1.4.2 checks reactor.running before starting its global
        # thread without a lock. Concurrent first connects can start it twice.
        # Only serialize creation through the first reactor callback; desktop
        # negotiation and all steady-state operations remain independent.
        with _REACTOR_START_LOCK:
            client = api.connect(endpoint, password=password, timeout=self.timeout,
                                 factory_class=partial(_ClipboardFactory, self.network_encoding))
            ready = threading.Event()
            reactor.callFromThread(ready.set)
            if not ready.wait(self.timeout):
                self._close_client(client)
                raise TimeoutError("VNC reactor did not start in time")
            return client

    def execute(self, method: str, args: tuple, control: ActionControl, expected: str | None, pin: bool) -> Any:
        with self._lock:
            if pin and expected != self.connection_id:
                raise ConnectionError("VNC target changed while this call was queued; inspect the current desktop before retrying")
            self._control = control
            try:
                control.check()
                result = getattr(self, method)(*args)
                control.check()
                return result
            except (ActionCancelled, ActionTimedOut):
                if method not in {"capture", "screenshot", "status", "clipboard_get"}:
                    self.disconnect()
                raise
            finally:
                self._control = None

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            client, self.client = self.client, None
            keys, buttons = tuple(self._held_keys), tuple(self._held_buttons)
            self._held_keys.clear()
            self._held_buttons.clear()
            self.host = self.port = self.name = self.width = self.height = None
            self.connection_id = None
            self.guest_profile = None
            if client is not None:
                self._close_client(client, keys, buttons)
            return self.status()

    def _close_client(self, client: Any, keys: tuple = (), buttons: tuple = ()) -> None:
        # The upstream synchronous disconnect is chained behind pending commands.
        # A timeout or failed Deferred can therefore prevent it from closing at
        # all. Close the transport directly on its owning reactor thread instead.
        client.factory.close_requested = True
        finished = threading.Event()

        def close() -> None:
            try:
                protocol = client.factory.active_protocol
                transport = getattr(protocol, "transport", None)
                if transport is not None and transport.connected:
                    for method, value in [("keyUp", key) for key in keys] + [
                        ("mouseUp", {"left": 1, "middle": 2, "right": 3}[button]) for button in buttons
                    ]:
                        try:
                            getattr(protocol, method)(value)
                        except Exception:
                            pass
                    transport.loseConnection()
            finally:
                finished.set()

        reactor.callFromThread(close)
        finished.wait(timeout=self.timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self.client is not None and self.client.factory.closed.is_set():
                return self.disconnect()
            screen = getattr(getattr(self.client, "protocol", None), "screen", None)
            if screen is not None and (self.screenshot_mode == "refresh" or self.width is None or self.height is None):
                self.width, self.height = screen.size
            return {
                "connected": self.client is not None,
                "connection_id": self.connection_id,
                "screenshot_mode": self.screenshot_mode,
                "host": self.host,
                "port": self.port,
                "name": self.name,
                "width": self.width,
                "height": self.height,
                "guest_profile": self.guest_profile.name if self.guest_profile else None,
                "capabilities": {
                    "utf8_clipboard": bool(self.client and self.client.factory.ext_clipboard.supported),
                    "guest_text": self.guest_profile is not None,
                    "preferred_encoding": self.network_encoding,
                    "wire_encoding": self.client.factory.wire_encoding if self.client else None,
                },
            }

    def _require_client(self) -> Any:
        if self.client is not None and self.client.factory.closed.is_set():
            reason = self.client.factory.protocol_error
            self.disconnect()
            if reason:
                raise ConnectionError(f"RFB protocol rejected ({reason}); reconnect explicitly after inspecting the target")
        if self.client is None:
            raise ConnectionError("Not connected; call vnc_connect first")
        return self.client

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._control is not None:
            self._control.check()
        client = self._require_client()
        try:
            result = getattr(client, method)(*args, **kwargs)
        except (ActionCancelled, ActionTimedOut):
            raise
        except Exception as exc:
            # Never reuse a possibly failed proxy queue or replay input. The
            # caller must explicitly select and reconnect the intended desktop.
            self.disconnect()
            raise ConnectionError(f"{type(exc).__name__}: {exc}; connection cleared, call vnc_connect again") from exc
        if self._control is not None:
            self._control.check()
        return result

    def screenshot(self, image_format: str = "png", quality: int = 80) -> tuple[bytes, int, int]:
        image = self.capture(image_format, quality)
        return image.data, image.width, image.height

    def capture(self, image_format: str = "png", quality: int = 80, region: tuple[int, int, int, int] | None = None) -> Screenshot:
        image_format = validate_encoding(image_format, quality)
        with self._lock:
            started = time.monotonic()
            client = self._require_client()
            before_bytes = client.factory.received_bytes
            frames = []
            if self.screenshot_mode == "fresh":
                # Some VMware VNC servers do not answer repeated full-refresh
                # requests on an unchanged display. Obtain the initial full
                # frame on a separate shared connection, keeping held input
                # and the control channel intact.
                endpoint_host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
                snapshot = self._connect_client(f"{endpoint_host}::{self.port}", client.factory.password)
                try:
                    snapshot.capture_frame(frames)
                finally:
                    self._close_client(snapshot)
                transport_bytes = snapshot.factory.received_bytes
                wire_encoding = snapshot.factory.wire_encoding
            else:
                self._call("capture_frame", frames)
                transport_bytes = client.factory.received_bytes - before_bytes
                wire_encoding = client.factory.wire_encoding
            if not frames:
                raise RuntimeError("VNC client did not report screen dimensions")
            frame = frames[0]
            self.width, self.height = frame.size
            if self._control is not None:
                self._control.check()
            image = encode_frame(frame, image_format, quality, region)
            return replace(image, capture_seconds=round(time.monotonic() - started, 6),
                           transport_bytes=transport_bytes, wire_encoding=wire_encoding)

    def _check_point(self, x: int, y: int) -> None:
        client = self._require_client()
        screen = getattr(client.protocol, "screen", None)
        if screen is not None and (self.screenshot_mode == "refresh" or self.width is None or self.height is None):
            self.width, self.height = screen.size
        if self.width is not None and self.height is not None:
            if not (0 <= x < self.width and 0 <= y < self.height):
                raise ValueError(f"coordinates must be within {self.width}x{self.height}")

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        if not 1 <= count <= 3:
            raise ValueError("count must be between 1 and 3")
        with self._lock:
            self._check_point(x, y)
            self._call("mouseMove", x, y)
            for _ in range(count):
                self._call("mousePress", buttons[button])
            return {"clicked": button, "count": count, "x": x, "y": y}

    def move(self, x: int, y: int) -> dict[str, Any]:
        with self._lock:
            self._check_point(x, y)
            self._call("mouseMove", x, y)
            return {"moved": True, "x": x, "y": y}

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        with self._lock:
            self._check_point(x1, y1)
            self._check_point(x2, y2)
            self._call("mouseMove", x1, y1)
            self._call("mouseDown", buttons[button])
            distance = max(abs(x2 - x1), abs(y2 - y1))
            step = max(1, (distance + 29) // 30)
            try:
                self._call("mouseDrag", x2, y2, step=step)
            finally:
                if self.client is not None:
                    self._call("mouseUp", buttons[button])
            return {"dragged": button, "from": [x1, y1], "to": [x2, y2]}

    def scroll(self, x: int, y: int, direction: str, amount: int = 1) -> dict[str, Any]:
        buttons = {"up": 4, "down": 5, "left": 6, "right": 7}
        if direction not in buttons:
            raise ValueError("direction must be up, down, left, or right")
        if not 1 <= amount <= 20:
            raise ValueError("amount must be between 1 and 20")
        with self._lock:
            self._check_point(x, y)
            self._call("mouseMove", x, y)
            for _ in range(amount):
                self._call("mousePress", buttons[direction])
            return {"scrolled": direction, "amount": amount, "x": x, "y": y}

    def keypress(self, key: str) -> dict[str, Any]:
        if not key.strip():
            raise ValueError("key must not be empty")
        with self._lock:
            self._call("keyPress", self._normalize_key(key))
            return {"pressed": key}

    def key_down(self, key: str) -> dict[str, Any]:
        if not key.strip():
            raise ValueError("key must not be empty")
        with self._lock:
            normalized = self._normalize_key(key)
            self._call("keyDown", normalized)
            self._held_keys.add(normalized)
            return {"key_down": key}

    def key_up(self, key: str) -> dict[str, Any]:
        if not key.strip():
            raise ValueError("key must not be empty")
        with self._lock:
            normalized = self._normalize_key(key)
            self._call("keyUp", normalized)
            self._held_keys.discard(normalized)
            return {"key_up": key}

    @staticmethod
    def _normalize_key(key: str) -> str:
        parts = key.strip().replace("+", "-").split("-")
        aliases = {
            "backspace": "bsp", "escape": "esc", "return": "enter",
            "insert": "ins", "pageup": "pgup", "pagedown": "pgdn",
            "windows": "super", "win": "super",
        }
        parts = [aliases.get(part.lower(), part) for part in parts]
        if len(parts) > 1:
            return "-".join(part.lower() for part in parts)
        if len(parts[0]) == 1:
            return parts[0]
        return parts[0].lower()

    def mouse_down(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        with self._lock:
            self._check_point(x, y)
            self._call("mouseMove", x, y)
            self._call("mouseDown", buttons[button])
            self._held_buttons.add(button)
            return {"mouse_down": button, "x": x, "y": y}

    def mouse_up(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        with self._lock:
            self._check_point(x, y)
            self._call("mouseMove", x, y)
            self._call("mouseUp", buttons[button])
            self._held_buttons.discard(button)
            return {"mouse_up": button, "x": x, "y": y}

    def type_text(self, text: str, delay_ms: int = 0) -> dict[str, Any]:
        validate_text(text)
        if not 0 <= delay_ms <= 1000:
            raise ValueError("delay_ms must be between 0 and 1000")
        wire_text = text.replace("\r", "")
        if max(0, len(wire_text) - 1) * delay_ms / 1000 >= self.action_timeout:
            raise ValueError("requested typing delays exceed the action time budget; send smaller chunks")
        with self._lock:
            self._require_client()
            control = self._control or ActionControl(time.monotonic() + self.action_timeout)
            batch_size = 1 if delay_ms else 64
            try:
                for start in range(0, len(wire_text), batch_size):
                    control.check()
                    self._call("type_batch", wire_text[start:start + batch_size], control)
                    if delay_ms and start + batch_size < len(wire_text):
                        control.wait(delay_ms / 1000.0)
                control.check()
            except (ActionCancelled, ActionTimedOut):
                self.disconnect()
                raise
            return {"typed_characters": len(text)}

    def clipboard_set(self, text: str) -> dict[str, Any]:
        validate_text(text)
        with self._lock:
            client = self._require_client()
            if client.factory.ext_clipboard.supported:
                self._call("set_clipboard_utf8", text)
                # Notify transfers ownership; lazy X11 servers request bytes
                # only when an application pastes. Waiting here would deadlock
                # the very Ctrl+V that causes Request -> Provide.
                return {"clipboard_characters": len(text), "encoding": "utf-8", "clipboard_offered": True,
                        "clipboard_data_sent": client.factory.clipboard_sent.is_set(), "application_verified": False}
            try:
                text.encode("iso-8859-1")
            except UnicodeEncodeError as exc:
                raise ValueError("server has no negotiated Unicode clipboard; bind a guest profile and use vnc_text") from exc
            self._call("paste", text)
            return {"clipboard_characters": len(text), "encoding": "iso-8859-1", "application_verified": False}

    def clipboard_get(self) -> dict[str, Any]:
        with self._lock:
            client = self._require_client()
            return {"text": client.factory.latest_clipboard, "encoding": client.factory.clipboard_encoding,
                    "cached": True}

    def send_text(self, text: str, method: str = "auto", paste_key: str = "ctrl-v",
                  focus_x: int | None = None, focus_y: int | None = None, delay_ms: int = 0) -> dict[str, Any]:
        validate_text(text)
        if not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or not 0 <= delay_ms <= 1000:
            raise ValueError("delay_ms must be an integer between 0 and 1000")
        if delay_ms and method not in {"auto", "keys"}:
            raise ValueError("delay_ms is available only for key events")
        if delay_ms and method == "auto" and any(ord(char) > 255 for char in text):
            raise ValueError("Unicode key events require explicit method=keys; omit delay_ms to use automatic Unicode text transport")
        if method not in {"auto", "keys", "clipboard", "guest"}:
            raise ValueError("text method must be auto, keys, clipboard or guest")
        if paste_key not in {"ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"}:
            raise ValueError("paste_key must be ctrl-v, ctrl-shift-v, shift-insert or meta-v")
        if (focus_x is None) != (focus_y is None):
            raise ValueError("provide both focus_x and focus_y")
        with self._lock:
            client = self._require_client()
            if focus_x is not None:
                if any(isinstance(value, bool) or not isinstance(value, int) for value in (focus_x, focus_y)):
                    raise ValueError("focus coordinates must be integers")
                self._check_point(focus_x, focus_y)
            if self._held_keys or self._held_buttons:
                raise ValueError("release held keys and mouse buttons before submitting literal text")
            selected = "keys" if delay_ms else method
            if selected == "auto":
                selected = "clipboard" if client.factory.ext_clipboard.supported else "guest" if self.guest_profile else "keys"
                if selected == "keys" and any(ord(char) > 255 for char in text):
                    raise ValueError("Unicode text needs a negotiated UTF-8 clipboard or an explicit guest profile; use method=keys only for a server known to accept Unicode keysyms")
            result = {"submitted_characters": len(text), "typed_characters": len(text), "transport": selected, "paste_sent": False,
                      "application_verified": False}
            if not text:
                return result
            control = self._control or ActionControl(time.monotonic() + self.action_timeout)
            if selected == "keys":
                if focus_x is not None:
                    self.click(focus_x, focus_y)
                    control.wait(0.15)
                if delay_ms:
                    self.type_text(text, delay_ms)
                else:
                    self.type_text(text)
                return result
            if selected == "guest":
                profile = self.guest_profile
                if profile is None or not profile.matches(self.host or "", self.port or 0):
                    raise ValueError("guest text requires a profile bound to this exact VNC connection")
                # Windows CLI-launched console helpers can steal focus even when
                # hidden. Restore only a caller-selected point after preparation;
                # never guess a window or paste into an unknown foreground app.
                if focus_x is None:
                    raise ValueError("guest text requires focus_x and focus_y from the inspected screenshot to restore the intended input control")
                result.update(VmrunGuest(profile).prepare_clipboard(text, control))
            else:
                # Unicode failure is detected before sending any clipboard bytes.
                result.update(self.clipboard_set(text))
            control.check()
            if focus_x is not None:
                self.click(focus_x, focus_y)
                # RFB orders bytes, but guests process pointer and keyboard
                # through separate device queues. Wait for the focus click to
                # settle before the paste chord (VMware can otherwise deliver
                # Ctrl+V while the text control is still handling mouse-down).
                control.wait(0.15)
            self.keypress(paste_key)
            result["paste_sent"] = True
            if selected == "clipboard" and client.factory.ext_clipboard.supported:
                until = min(control.deadline, time.monotonic() + 0.5)
                while not client.factory.clipboard_sent.is_set() and time.monotonic() < until:
                    control.wait(min(0.02, max(0, until - time.monotonic())))
                    self._require_client()
                result["clipboard_data_sent"] = client.factory.clipboard_sent.is_set()
            return result

    def wait(self, seconds: float) -> dict[str, Any]:
        if not 0 <= seconds <= 60:
            raise ValueError("seconds must be between 0 and 60")
        with self._lock:
            client = self._require_client()
        # CLI/direct callers should not prevent a concurrent disconnect either.
        if client.factory.closed.wait(seconds):
            raise ConnectionError("VNC connection closed during wait")
        with self._lock:
            if self._require_client() is not client:
                raise ConnectionError("VNC target changed during wait")
        return {"waited_seconds": seconds}
