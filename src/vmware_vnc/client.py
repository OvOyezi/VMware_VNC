# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Serialized VNC desktop session over the project-owned asyncio RFB core."""
from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import threading
import time
from typing import Any
from uuid import uuid4

from .rfb import FramebufferUnavailable, RFBConnection
from .keys import parse_key
from .transport import Endpoint
from .imaging import Screenshot, validate_encoding, validate_region
from .operations import ActionCancelled, ActionControl, ActionTimedOut
from .clipboard import normalize_newlines, validate_text
from .guest import GuestProfile, VmrunClipboard
from .visual import VisualFrame, VisualReference, reference_from_file

MIN_KEY_EVENT_GAP_MS = 20


class VNCSession:
    def __init__(self, timeout: float = 15.0, screenshot_mode: str = "auto", action_timeout: float = 20.0,
                 network_encoding: str = "auto", tls_ca_file: str | None = None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout must be finite and between 0 and 60 seconds")
        if isinstance(action_timeout, bool) or not isinstance(action_timeout, (int, float)) or not math.isfinite(action_timeout) or not 0 < action_timeout <= 60:
            raise ValueError("action_timeout must be finite and between 0 and 60 seconds")
        if screenshot_mode not in {"auto", "refresh", "fresh"}:
            raise ValueError("screenshot_mode must be auto, refresh or fresh")
        if network_encoding not in {"auto", "raw", "hextile", "zrle", "tight"}:
            raise ValueError("network_encoding must be auto, raw, hextile, zrle or tight")
        self.network_encoding = network_encoding
        self.tls_ca_file = tls_ca_file
        self._target_url: str | None = None
        self._endpoint: Endpoint | None = None
        self._refresh_strategy = "probe" if screenshot_mode == "auto" else screenshot_mode
        self.guest_profile: GuestProfile | None = None
        self._visual_reference: VisualReference | None = None
        self._last_visual_frame: VisualFrame | None = None
        self.timeout = timeout
        self.screenshot_mode = screenshot_mode
        self.action_timeout = action_timeout
        self.connection_id: str | None = None
        self._control: ActionControl | None = None
        self.client: RFBConnection | None = None
        self.host: str | None = None
        self.port: int | None = None
        self.name: str | None = None
        self.width: int | None = None
        self.height: int | None = None
        self._control_geometry_at_capture: tuple[int, int] | None = None
        self._display_unavailable = False
        self._lock = threading.RLock()
        self._held_keys: set[str] = set()
        self._held_buttons: set[str | int] = set()

    def connect(self, host: str = "", port: int = 0, password: str | None = None,
                profile: GuestProfile | None = None, url: str | None = None) -> dict[str, Any]:
        endpoint = Endpoint.from_target(host, port, url)
        if password is not None and not isinstance(password, str):
            raise ValueError("VNC password must be a string")
        if profile is not None and (url or not profile.matches(endpoint.host, endpoint.port)):
            raise ValueError("guest profile requires its bound native VNC endpoint")
        with self._lock:
            self.disconnect()
            self._target_url, self._endpoint = url, endpoint
            try:
                self.client = self._connect_client(host, port, password)
            except BaseException:
                self._target_url = self._endpoint = None
                raise
            self._refresh_strategy = "probe" if self.screenshot_mode == "auto" else self.screenshot_mode
            self.host, self.port = endpoint.host, endpoint.port
            self.guest_profile = profile
            self.name = self.client.name
            self.width, self.height = self.client.width, self.client.height
            self._control_geometry_at_capture = (self.client.width, self.client.height)
            self._display_unavailable = not self.client.framebuffer_available
            self.connection_id = uuid4().hex
            return self.status()


    def _connect_client(self, host: str, port: int, password: str | None,
                        initial_region: tuple[int, int, int, int] | None = None) -> RFBConnection:
        return RFBConnection("" if self._target_url else host, 0 if self._target_url else port, password,
                             self.timeout, self.network_encoding, initial_region,
                             url=self._target_url, tls_ca_file=self.tls_ca_file)

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
                if method not in {"capture", "screenshot", "status", "clipboard_get", "visual_sample", "visual_reference", "inspect"}:
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
            self._control_geometry_at_capture = None
            self._display_unavailable = False
            self._target_url = self._endpoint = None
            self.guest_profile = None
            self._visual_reference = None
            self._last_visual_frame = None
            if client is not None:
                self._close_client(client, keys, buttons)
            return self.status()

    def _close_client(self, client: RFBConnection, keys: tuple = (), buttons: tuple = ()) -> None:
        client.close(keys, buttons)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self.client is not None and self.client.closed.is_set():
                return self.disconnect()
            available = self.client is not None and self._display_available(self.client)
            if self.client is not None and not available:
                self._invalidate_display()
            return {
                "connected": self.client is not None,
                "connection_id": self.connection_id,
                "screenshot_mode": self.screenshot_mode,
                "host": self.host,
                "port": self.port,
                "transport": self._endpoint.transport if self._endpoint else None,
                "name": self.name,
                "width": self.width,
                "height": self.height,
                "display_state": "ready" if available else "unavailable" if self.client else "disconnected",
                "guest_profile": self.guest_profile.name if self.guest_profile else None,
                "capabilities": {
                    "utf8_clipboard": bool(self.client and self.client.ext_clipboard.supported),
                    "guest_text": self.guest_profile is not None,
                    "preferred_encoding": self.network_encoding,
                    "wire_encoding": self.client.wire_encoding if self.client else None,
                    "engine": "native-asyncio-rfb",
                    "refresh_strategy": self._refresh_strategy,
                    "fence": bool(self.client and self.client.fence_supported),
                    "continuous_updates": bool(self.client and self.client.continuous_supported),
                },
            }

    def _display_available(self, client: RFBConnection) -> bool:
        return not self._display_unavailable and (getattr(client, "framebuffer_available", True) is not False
                or (self._last_visual_frame is not None
                    and (client.width, client.height) == self._control_geometry_at_capture))

    def _invalidate_display(self) -> None:
        self._display_unavailable = True
        self._last_visual_frame = self._visual_reference = None
        self._control_geometry_at_capture = None
        if self.client is not None:
            self.width, self.height = self.client.width, self.client.height

    def _require_display(self, client: RFBConnection) -> None:
        if not self._display_available(client):
            self._invalidate_display()
            raise FramebufferUnavailable("VNC display is unavailable; call vnc_wake explicitly, then capture and inspect a new screenshot before input")

    def _require_client(self) -> RFBConnection:
        if self.client is not None and self.client.closed.is_set():
            reason = self.client.protocol_error
            self.disconnect()
            if reason:
                raise ConnectionError(f"RFB protocol rejected ({reason}); reconnect explicitly after inspecting the target")
        if self.client is None:
            raise ConnectionError("Not connected; call vnc_connect first")
        return self.client

    def _call(self, method: str, *args: Any, require_display: bool = True, **kwargs: Any) -> Any:
        if self._control is not None:
            self._control.check()
        client = self._require_client()
        if require_display and method in {"mouse_move", "mouse_down", "key_down", "type_batch"}:
            self._require_display(client)
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

    @contextmanager
    def _input_action(self) -> Iterator[ActionControl]:
        if self._control is None:
            control = ActionControl(time.monotonic() + self.action_timeout)
            self._control = control
            owns_control = True
        else:
            control = self._control
            owns_control = False
        try:
            yield control
        except BaseException:
            try:
                self.disconnect()
            except BaseException:
                pass
            raise
        finally:
            if owns_control and self._control is control:
                self._control = None

    def _position_pointer(self, x: int, y: int, control: ActionControl) -> None:
        self._call("mouse_move", x, y)
        control.wait(0.15)

    def screenshot(self, image_format: str = "png", quality: int = 80) -> tuple[bytes, int, int]:
        image = self.capture(image_format, quality)
        return image.data, image.width, image.height

    def capture(self, image_format: str = "png", quality: int = 80, region: tuple[int, int, int, int] | None = None,
                remember: bool = False) -> Screenshot:
        image_format = validate_encoding(image_format, quality)
        if not isinstance(remember, bool):
            raise ValueError("remember must be a boolean")
        with self._lock:
            frame = self._capture_frame(region).crop(region)
            if remember:
                frame.require_bounded()
            image = frame.encode(image_format, quality)
            if remember:
                reference_id = uuid4().hex
                self._visual_reference = VisualReference(reference_id, frame)
                image = replace(image, reference_id=reference_id)
            return image

    def visual_sample(self, region: tuple[int, int, int, int] | None = None) -> VisualFrame:
        with self._lock:
            return self._capture_frame(region).crop(region).require_bounded()

    def remember_visual(self, region: tuple[int, int, int, int] | None = None) -> str:
        """Remember a visual reference without encoding an unused image."""
        with self._lock:
            frame = self.visual_sample(region)
            reference_id = uuid4().hex
            self._visual_reference = VisualReference(reference_id, frame)
            return reference_id

    def visual_reference(self, reference_id: str, region: tuple[int, int, int, int] | None = None) -> VisualFrame:
        with self._lock:
            self._require_client()
            reference = self._visual_reference
            if reference is None or reference.reference_id != reference_id:
                raise ValueError("unknown, replaced or expired visual reference; take a screenshot with remember=true")
            return reference.frame.crop(region)

    def load_visual_reference(self, filename: str, region: tuple[int, int, int, int] | None = None) -> str:
        """Load an explicit CLI reference; MCP references come from inspected screenshots."""
        with self._lock:
            frame = reference_from_file(filename, self.visual_sample(region))
            reference_id = uuid4().hex
            self._visual_reference = VisualReference(reference_id, frame)
            return reference_id

    def inspect(self, region: tuple[int, int, int, int] | None = None,
                reference_id: str | None = None, points: list[tuple[int, int]] | None = None,
                detail_region: tuple[int, int, int, int] | None = None,
                zoom: int = 4, pixel_threshold: int = 0, frame_id: str | None = None):
        from .inspection import inspect_frame
        with self._lock:
            self._require_display(self._require_client())
            if frame_id is not None:
                frame = self._last_visual_frame
                if frame is None or frame.frame_id != frame_id:
                    raise ValueError("unknown or expired frame_id; capture and inspect the intended frame again")
            else:
                frame = self._capture_frame(region)
            selected = frame.crop(region).require_bounded()
            reference = self.visual_reference(reference_id, selected.region) if reference_id else None
            result = inspect_frame(selected, reference, points, detail_region, zoom, pixel_threshold)
            result.details.update(frame_id=frame.frame_id, frame_received_monotonic=frame.received_at,
                                  connection_id=self.connection_id, capture_source=frame.capture_source,
                                  reference_frame_id=reference.frame_id if reference else None,
                                  cached_frame=frame_id is not None,
                                  frame_age_seconds=round(max(0, time.monotonic() - frame.received_at), 6) if frame.received_at else None)
            images = []
            for kind, image in result.images:
                mapping = result.details["image_metadata"][kind]
                source = reference if kind == "detail_reference" else frame
                images.append((kind, replace(image, frame_id=source.frame_id, received_at=source.received_at,
                                            capture_source=source.capture_source, display_scale=mapping["scale"],
                                            color_semantics=mapping["color_semantics"])))
            return replace(result, images=images)

    def _capture_frame(self, region: tuple[int, int, int, int] | None = None) -> VisualFrame:
        try:
            return self._capture_available_frame(region)
        except FramebufferUnavailable:
            with self._lock:
                self._invalidate_display()
            raise
        except TimeoutError as exc:
            with self._lock:
                self._invalidate_display()
            if isinstance(exc, ActionTimedOut):
                raise
            raise FramebufferUnavailable("VNC screenshot timed out without a complete fresh frame; the display may be asleep or unresponsive. No cached image was returned. If still connected, call vnc_wake explicitly, then capture again; otherwise reconnect explicitly") from exc

    def _capture_available_frame(self, region: tuple[int, int, int, int] | None = None) -> VisualFrame:
        with self._lock:
            started = time.monotonic()
            client = self._require_client()
            control_geometry = (client.width, client.height)
            if region is not None and (self.width or client.width) and (self.height or client.height):
                validate_region(region, self.width or client.width, self.height or client.height)
            before_bytes = client.received_bytes
            snapshot = None
            snapshot_client = None
            if self._refresh_strategy != "fresh":
                try:
                    # Probe once per connection. A missed read is safe to abandon;
                    # no input is retried and cached pixels are never called fresh.
                    budget = min(self.timeout, 0.5) if self._refresh_strategy == "probe" else self.timeout
                    snapshot = client.capture(region, timeout=budget)
                    self._refresh_strategy = "refresh"
                except TimeoutError:
                    if self.screenshot_mode != "auto":
                        self.disconnect()
                        raise
                    self._refresh_strategy = "fresh"
                except FramebufferUnavailable:
                    if self.screenshot_mode != "auto":
                        raise
                    self._refresh_strategy = "fresh"
                except Exception:
                    self.disconnect()
                    raise
            if self._refresh_strategy == "fresh":
                snapshot_client = self._connect_client(self.host, self.port, client.password, region)
                try:
                    snapshot = snapshot_client.initial_snapshot()
                    transport_bytes = snapshot_client.received_bytes
                    wire_encoding = snapshot_client.wire_encoding
                finally:
                    self._close_client(snapshot_client)
                source = "fresh-connection"
            else:
                snapshot = client.last_snapshot
                transport_bytes = client.received_bytes - before_bytes
                wire_encoding = client.wire_encoding
                source = "framebuffer-update"
            if snapshot is None:
                raise RuntimeError("VNC server did not return a complete framebuffer")
            frame = snapshot.image.copy()
            self.width, self.height = frame.size
            reference = self._visual_reference
            if reference is not None and (reference.frame.desktop_width, reference.frame.desktop_height) != frame.size:
                self._visual_reference = None
            if self._control is not None:
                self._control.check()
            result = VisualFrame(frame, *frame.size, capture_seconds=time.monotonic()-started,
                               transport_bytes=transport_bytes, wire_encoding=wire_encoding,
                               frame_id=snapshot.frame_id, received_at=snapshot.received_at,
                               updated_regions=snapshot.updated_regions, capture_source=source)
            if snapshot.valid_region is not None:
                result = result.crop(snapshot.valid_region)
            self._last_visual_frame = result
            self._display_unavailable = False
            # A fresh screenshot may learn new dimensions without a resize
            # message on the long-lived control socket. Its complete image is
            # the coordinate authority; remember the control state only to
            # detect notifications arriving after this observation.
            current_control_geometry = (client.width, client.height)
            self._control_geometry_at_capture = (
                frame.size if source == "framebuffer-update" or current_control_geometry == frame.size
                else control_geometry
            )
            return result

    def wake_display(self) -> dict[str, Any]:
        """Explicit pointer motion only; never click, type, or unlock a desktop."""
        with self._lock:
            self._require_client()
            if self._held_keys or self._held_buttons:
                raise ValueError("release held keys and mouse buttons before waking the display")
            with self._input_action() as control:
                self._call("mouse_move", 0, 0, require_display=False)
                control.wait(0.05)
                self._call("mouse_move", 1, 0, require_display=False)
            return {"wake_sent": True, "input": "pointer-motion-only", "application_verified": False,
                    "next_action": "capture and inspect a new screenshot; wake does not unlock or guarantee a display"}

    def _check_point(self, x: int, y: int) -> None:
        if type(x) is not int or type(y) is not int:
            raise ValueError("coordinates must be integers")
        client = self._require_client()
        if self._display_unavailable or not client.width or not client.height:
            self._require_display(client)
        if (client.width, client.height) != self._control_geometry_at_capture:
            raise ValueError("desktop size changed; capture and inspect a new screenshot before input")
        self._require_display(client)
        if self.width is None or self.height is None or not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"coordinates must be within the observed {self.width}x{self.height} desktop")


    @staticmethod
    def _button_number(button: str | int) -> int:
        if isinstance(button, int):
            return button
        return {"left": 1, "middle": 2, "right": 3}[button]

    def _mouse_button_is_held(self, button: str | int) -> bool:
        number = self._button_number(button)
        return any(self._button_number(held) == number for held in self._held_buttons)

    def _discard_mouse_button(self, button: str | int) -> None:
        number = self._button_number(button)
        matches = [held for held in self._held_buttons if self._button_number(held) == number]
        self._held_buttons.difference_update(matches)

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        if type(count) is not int or not 1 <= count <= 3:
            raise ValueError("count must be between 1 and 3")
        with self._lock:
            self._check_point(x, y)
            if self._mouse_button_is_held(button):
                raise ValueError("cannot click a mouse button that is already held; release it first")
            with self._input_action() as control:
                self._position_pointer(x, y, control)
                for index in range(count):
                    self._pulse("mouse_down", buttons[button], "mouse_up", self._held_buttons, control)
                    if index + 1 < count:
                        control.wait(0.04)
            return {"clicked": button, "count": count, "x": x, "y": y}

    def move(self, x: int, y: int) -> dict[str, Any]:
        with self._lock:
            self._check_point(x, y)
            self._call("mouse_move", x, y)
            return {"moved": True, "x": x, "y": y}

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        with self._lock:
            self._check_point(x1, y1)
            self._check_point(x2, y2)
            if self._mouse_button_is_held(button):
                raise ValueError("cannot drag with a mouse button that is already held; release it first")
            distance = max(abs(x2 - x1), abs(y2 - y1))
            steps = min(30, distance)
            with self._input_action() as control:
                self._position_pointer(x1, y1, control)
                self._held_buttons.add(button)
                self._call("mouse_down", buttons[button])
                control.wait(0.08)
                for index in range(1, steps + 1):
                    control.check()
                    if index == steps:
                        x, y = x2, y2
                    else:
                        fraction = index / steps
                        x = round(x1 + (x2 - x1) * fraction)
                        y = round(y1 + (y2 - y1) * fraction)
                    self._call("mouse_move", x, y)
                    control.wait(0.02)
                control.wait(0.08)
                self._call("mouse_up", buttons[button])
                self._discard_mouse_button(button)
            return {"dragged": button, "from": [x1, y1], "to": [x2, y2]}

    def scroll(self, x: int, y: int, direction: str, amount: int = 1) -> dict[str, Any]:
        buttons = {"up": 4, "down": 5, "left": 6, "right": 7}
        if direction not in buttons:
            raise ValueError("direction must be up, down, left, or right")
        if type(amount) is not int or not 1 <= amount <= 20:
            raise ValueError("amount must be between 1 and 20")
        with self._lock:
            self._check_point(x, y)
            with self._input_action() as control:
                self._position_pointer(x, y, control)
                for index in range(amount):
                    self._pulse("mouse_down", buttons[direction], "mouse_up", self._held_buttons, control)
                    if index + 1 < amount:
                        control.wait(0.04)
            return {"scrolled": direction, "amount": amount, "x": x, "y": y}

    def keypress(self, key: str) -> dict[str, Any]:
        chord = parse_key(key)
        with self._lock:
            self._require_display(self._require_client())
            self._reject_key_overlap(chord.components)
            with self._input_action() as control:
                self._pulse("key_down", chord.name, "key_up", self._held_keys, control)
            return {"pressed": key}

    def _reject_key_overlap(self, components: frozenset[int]) -> None:
        if any(components & parse_key(held).components for held in self._held_keys):
            raise ValueError("key or chord overlaps an already held key; release the complete held chord first")


    def _pulse(self, down_method: str, value: Any, up_method: str, held: set,
               control: ActionControl) -> None:
        held.add(value)
        self._call(down_method, value)
        control.wait(0.08)
        self._call(up_method, value)
        held.discard(value)

    def key_down(self, key: str) -> dict[str, Any]:
        chord = parse_key(key)
        with self._lock:
            self._require_display(self._require_client())
            self._reject_key_overlap(chord.components)
            with self._input_action():
                self._held_keys.add(chord.name)
                self._call("key_down", chord.name)
            return {"key_down": key}


    def key_up(self, key: str) -> dict[str, Any]:
        chord = parse_key(key)
        with self._lock:
            if chord.name not in self._held_keys:
                self._reject_key_overlap(chord.components)
                raise ValueError("key or chord is not held by this session")
            with self._input_action():
                self._call("key_up", chord.name)
                self._held_keys.remove(chord.name)
            return {"key_up": key}




    def mouse_down(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        with self._lock:
            self._check_point(x, y)
            if self._mouse_button_is_held(button):
                raise ValueError("mouse button is already held; release it first")
            with self._input_action() as control:
                self._position_pointer(x, y, control)
                self._held_buttons.add(button)
                self._call("mouse_down", buttons[button])
            return {"mouse_down": button, "x": x, "y": y}

    def mouse_up(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        buttons = {"left": 1, "middle": 2, "right": 3}
        if button not in buttons:
            raise ValueError("button must be left, middle, or right")
        with self._lock:
            self._check_point(x, y)
            with self._input_action() as control:
                self._position_pointer(x, y, control)
                self._call("mouse_up", buttons[button])
                self._discard_mouse_button(button)
            return {"mouse_up": button, "x": x, "y": y}

    def _effective_key_delay(self, text: str, delay_ms: int) -> int:
        effective_delay_ms = max(delay_ms, MIN_KEY_EVENT_GAP_MS)
        normalized_text = normalize_newlines(text)
        wire_length = len(normalized_text)
        if max(0, wire_length - 1) * effective_delay_ms / 1000 >= self.action_timeout:
            raise ValueError("effective typing gaps exceed the action time budget; send smaller chunks")
        # Validate the entire key path before a focus click or any text prefix
        # is sent. Newline and tab use their explicit RFB special-key mappings.
        for character in normalized_text:
            if character not in "\n\t":
                parse_key(character)
        return effective_delay_ms

    def type_text(self, text: str, delay_ms: int = 0) -> dict[str, Any]:
        validate_text(text)
        if type(delay_ms) is not int or not 0 <= delay_ms <= 1000:
            raise ValueError("delay_ms must be between 0 and 1000")
        wire_text = normalize_newlines(text)
        effective_delay_ms = self._effective_key_delay(text, delay_ms)
        with self._lock:
            self._require_display(self._require_client())
            if self._held_keys or self._held_buttons:
                raise ValueError("release held keys and mouse buttons before typing literal text")
            control = self._control or ActionControl(time.monotonic() + self.action_timeout)
            try:
                for index, char in enumerate(wire_text):
                    control.check()
                    self._call("type_batch", char, control)
                    if index + 1 < len(wire_text):
                        control.wait(effective_delay_ms / 1000.0)
                control.check()
            except (ActionCancelled, ActionTimedOut):
                self.disconnect()
                raise
            return {"typed_characters": len(text), "effective_key_delay_ms": effective_delay_ms}

    def clipboard_set(self, text: str) -> dict[str, Any]:
        validate_text(text)
        with self._lock:
            client = self._require_client()
            if client.ext_clipboard.supported:
                self._call("set_clipboard_utf8", text)
                # Notify transfers ownership; lazy X11 servers request bytes
                # only when an application pastes. Waiting here would deadlock
                # the very Ctrl+V that causes Request -> Provide.
                return {"clipboard_characters": len(text), "encoding": "utf-8", "clipboard_offered": True,
                        "clipboard_data_sent": client.clipboard_sent.is_set(), "application_verified": False}
            try:
                text.encode("iso-8859-1")
            except UnicodeEncodeError as exc:
                raise ValueError("server has no negotiated Unicode clipboard; bind a guest profile and use vnc_text") from exc
            self._call("set_clipboard", text)
            return {"clipboard_characters": len(text), "encoding": "iso-8859-1", "application_verified": False}

    def clipboard_get(self) -> dict[str, Any]:
        with self._lock:
            client = self._require_client()
            return {"text": client.latest_clipboard, "encoding": client.clipboard_encoding,
                    "cached": True}

    def send_text(self, text: str, method: str = "auto", paste_key: str = "ctrl-v",
                  focus_x: int | None = None, focus_y: int | None = None, delay_ms: int = 0) -> dict[str, Any]:
        validate_text(text)
        if not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or not 0 <= delay_ms <= 1000:
            raise ValueError("delay_ms must be an integer between 0 and 1000")
        if delay_ms and method not in {"auto", "keys"}:
            raise ValueError("delay_ms is available only for key events")
        if delay_ms and method == "auto":
            raise ValueError("delayed key events require explicit method=keys after checking the target input mode")
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
                selected = "clipboard" if client.ext_clipboard.supported else "guest" if self.guest_profile else "unavailable"
                if selected == "unavailable" and text:
                    raise ValueError("automatic text needs a negotiated UTF-8 clipboard or an explicit guest profile; inspect the target input mode and select method=keys explicitly for keyboard events; no input was sent")
            result = {"submitted_characters": len(text), "typed_characters": len(text), "transport": selected, "paste_sent": False,
                      "application_verified": False}
            if not text:
                return result
            if selected == "keys":
                effective_delay_ms = self._effective_key_delay(text, delay_ms)
                result["effective_key_delay_ms"] = effective_delay_ms
                result["input_method_sensitive"] = True
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
                result.update(VmrunClipboard(profile).prepare_clipboard(text, control))
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
            if selected == "clipboard" and client.ext_clipboard.supported:
                until = min(control.deadline, time.monotonic() + 0.5)
                while not client.clipboard_sent.is_set() and time.monotonic() < until:
                    control.wait(min(0.02, max(0, until - time.monotonic())))
                    self._require_client()
                result["clipboard_data_sent"] = client.clipboard_sent.is_set()
            return result

    def wait(self, seconds: float) -> dict[str, Any]:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 60:
            raise ValueError("seconds must be between 0 and 60")
        with self._lock:
            client = self._require_client()
        # CLI/direct callers should not prevent a concurrent disconnect either.
        if client.closed.wait(seconds):
            raise ConnectionError("VNC connection closed during wait")
        with self._lock:
            if self._require_client() is not client:
                raise ConnectionError("VNC target changed during wait")
        return {"waited_seconds": seconds}
