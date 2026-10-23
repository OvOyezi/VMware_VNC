# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Instance-owned MCP stdio adapter for the shared VNC application."""
from __future__ import annotations

from contextlib import asynccontextmanager
import base64
import json
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
from pydantic import BaseModel, Field, StrictInt

from .application import VNCApplication
from .config import VNCConfig
from .sessions import DEFAULT_SESSION


class ScreenshotRegion(BaseModel):
    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


class PixelPoint(BaseModel):
    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)


def _image_content(image) -> ImageContent:
    return ImageContent(type="image", mimeType=f"image/{image.image_format}",
                        data=base64.b64encode(image.data).decode("ascii"),
                        _meta={"codex/imageDetail": "original"})


INSTRUCTIONS = (
    'Help AI assistants reliably observe and control VMware virtual machines through VNC over MCP. Basic '
    'screen observation and pointer/keyboard input use the VNC endpoint directly, without guest OS '
    'credentials, VMware Tools or external control CLIs. Guest clipboard profiles are optional text '
    'enhancements. Always call vnc_screenshot after connecting to inspect the screen. Coordinates always '
    'refer to the native desktop; cropped images include their origin, which must be added to image-local '
    'coordinates. Connect multiple desktops using distinct session_id values. Always pass BOTH session_id and '
    'the returned connection_id for named-session actions and screenshots. Omitting session_id uses the '
    'separate legacy default session; it never selects the most recently named session. Idle sessions expire '
    'after 300 seconds by default; reconnect explicitly and inspect again. Use vnc_sessions for nonblocking '
    'inventory and vnc_status for current state. Use vnc_text or vnc_type for literal Unicode text: auto '
    'selects negotiated UTF-8 clipboard or an explicitly selected VMware guest profile. '
    'Auto never falls back to keyboard events: inspect the target input mode and choose method=keys explicitly. '
    'If the connected display is unavailable, vnc_wake sends pointer motion only; capture again afterward. '
    'Guest text requires focus_x/focus_y from the inspected screenshot; the focus click clears any existing text selection. A '
    'sent paste is not proof that the application accepted text. Never replay failed input automatically. Use '
    'vnc_screenshot(remember=true) before an action when you need a pixel reference, then vnc_wait_for '
    'changed/matches, or stable without a reference. These are sampled visual conditions, not application '
    'readiness. Screenshots default to lossless native-size PNG. For exact colors or subtle differences use '
    'vnc_inspect; pass a screenshot frame_id to inspect those exact cached pixels without a new capture. '
    'Amplified difference images alter colors; current and detail_current retain original colors. Call '
    'vnc_screenshot again after actions to verify the result.'
)


def create_server(config: VNCConfig | None = None, *, application: VNCApplication | None = None) -> FastMCP:
    """Construct an isolated adapter without opening sockets or loading guest profiles."""
    if config is not None and application is not None:
        raise ValueError("provide either config or application")
    application = application or VNCApplication(config)

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        async with application:
            yield {"application": application}

    server = FastMCP("VMware VNC", lifespan=lifespan, log_level="WARNING", instructions=INSTRUCTIONS)

    async def _run(method: str, *args, session_id: str, connection_id: str | None = None, pin: bool = True) -> dict:
        return (await application.run(method, *args, session_id=session_id, connection_id=connection_id, pin=pin)).as_dict()

    @server.tool()
    async def vnc_connect(host: str = "", port: StrictInt = 0, password: str = "", session_id: str = DEFAULT_SESSION, profile: str = "", url: str = "") -> dict:
        """Connect to host AND port, a configured profile, or a vnc/vnc+tls/ws/wss URL. Use distinct session_id names for independent desktops. Named actions require the returned connection_id. Reconnect explicitly after failure; input is never replayed."""
        return (await application.connect(host, port, password, session_id, profile, url)).as_dict()

    @server.tool()
    async def vnc_profiles() -> dict:
        """List optional VMware guest clipboard profiles without returning credentials."""
        return await application.profiles()

    @server.tool()
    async def vnc_diagnostics() -> dict:
        """Read-only runtime discovery; does not connect, execute guest commands or read credentials."""
        return await application.diagnostics()

    @server.tool()
    async def vnc_text(text: str, method: Literal["auto", "keys", "clipboard", "guest"] = "auto",
                       paste_key: Literal["ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"] = "ctrl-v",
                       connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                       focus_x: StrictInt | None = None, focus_y: StrictInt | None = None) -> dict:
        """Submit literal text. Auto selects negotiated UTF-8 clipboard or a bound VMware guest profile, otherwise fails before input. Explicit method=keys requires visual confirmation of the target input mode; key events can be transformed by an IME. Guest text REQUIRES focus_x/focus_y from the inspected screenshot. Verify the application afterward."""
        return await _run("send_text", text, method, paste_key, focus_x, focus_y, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_wake(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Explicitly attempt to wake a connected display using pointer motion only. Never clicks, types or unlocks; refuses held keys/buttons. Works with a 0x0 framebuffer. Capture and inspect a NEW screenshot before further input; success only means wake motion was sent."""
        return await _run("wake_display", session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_disconnect(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Disconnect only the selected session and release its slot. Named sessions require connection_id."""
        return await _run("disconnect", session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_status(session_id: str = DEFAULT_SESSION) -> dict:
        """Return current state of the selected session, waiting for its earlier work to finish. Unknown named sessions require an explicit connect."""
        return await _run("status", session_id=session_id, pin=False)

    @server.tool()
    async def vnc_sessions() -> dict:
        """List session IDs, cached target states, queue usage and idle ages without waiting for busy desktops. Passwords are never included. Use vnc_status(session_id=...) for live state."""
        return application.inventory()

    @server.tool()
    async def vnc_screenshot(image_format: Literal["jpeg", "png"] = "png", quality: StrictInt = 80, region: ScreenshotRegion | None = None, connection_id: str | None = None, session_id: str = DEFAULT_SESSION, remember: bool = False) -> list:
        """Capture the native desktop or a region {x,y,width,height}. JPEG quality: 1-95; PNG is lossless. Add region origin to image-local coordinates. remember=true returns a reference_id for vnc_wait_for; only the latest reference per connection is retained, until replacement, resize or disconnect. No scaling."""
        box = (region.x, region.y, region.width, region.height) if region is not None else None
        result = await application.run("capture", image_format, quality, box, remember, session_id=session_id, connection_id=connection_id)
        image = result.value
        return [
            json.dumps({**image.metadata(), "session_id": result.session_id, "connection_id": result.connection_id}),
            _image_content(image),
        ]

    @server.tool()
    async def vnc_inspect(region: ScreenshotRegion | None = None, reference_id: str | None = None,
                          points: list[PixelPoint] | None = None, detail_region: ScreenshotRegion | None = None,
                          zoom: StrictInt = 4, pixel_threshold: StrictInt = 0, frame_id: str | None = None,
                          connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> list:
        """Inspect exact RGB pixels and subtle differences. Always returns original-color PNG; optional native-coordinate points return RGB and reference deltas. A reference_id from screenshot(remember=true) enables exact comparison (threshold 0 default), original-color before/after crops and a separately labeled x16 amplified difference. zoom 1-8 is nearest-neighbor display enlargement; add origin plus image coordinate/zoom for native coordinates. Explicit detail region max 512x512 and enlarged image max 4M pixels. Optional frame_id reuses the latest captured frame, with age reported, instead of capturing; replaced/reconnected frame IDs fail. Pass both IDs for named sessions. No input or model inference."""
        box = (region.x, region.y, region.width, region.height) if region else None
        detail = (detail_region.x, detail_region.y, detail_region.width, detail_region.height) if detail_region else None
        coordinates = [(point.x, point.y) for point in points] if points is not None else None
        result = await application.run("inspect", box, reference_id, coordinates, detail, zoom, pixel_threshold, frame_id,
                                    session_id=session_id, connection_id=connection_id)
        inspection = result.value
        content = [json.dumps({**inspection.details, "session_id": result.session_id,
                               "connection_id": result.connection_id,
                               "image_order": [kind for kind, _ in inspection.images]})]
        for kind, image in inspection.images:
            content.append(json.dumps({"image_kind": kind, **image.metadata(),
                                       "mapping": inspection.details["image_metadata"][kind]}))
            content.append(_image_content(image))
        return content

    @server.tool()
    async def vnc_click(x: StrictInt, y: StrictInt, button: str = "left", count: StrictInt = 1, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Click at screen coordinates. button: left, middle, right; count: 1-3."""
        return await _run("click", x, y, button, count, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_move(x: StrictInt, y: StrictInt, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Move the pointer to screen coordinates."""
        return await _run("move", x, y, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_drag(x1: StrictInt, y1: StrictInt, x2: StrictInt, y2: StrictInt, button: str = "left", connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Drag from one screen coordinate to another with a mouse button held."""
        return await _run("drag", x1, y1, x2, y2, button, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_scroll(x: StrictInt, y: StrictInt, direction: str, amount: StrictInt = 1, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Scroll at a screen coordinate. direction: up, down, left, right; amount: 1-20."""
        return await _run("scroll", x, y, direction, amount, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_keypress(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Press a key or chord, e.g. enter, ctrl-c, alt-tab, f5, or a."""
        return await _run("keypress", key, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_key_down(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Hold a key down until vnc_key_up releases it."""
        return await _run("key_down", key, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_key_up(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Release a key held with vnc_key_down."""
        return await _run("key_up", key, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_mouse_down(x: StrictInt, y: StrictInt, button: str = "left", connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Press and hold a mouse button at screen coordinates."""
        return await _run("mouse_down", x, y, button, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_mouse_up(x: StrictInt, y: StrictInt, button: str = "left", connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Release a mouse button at screen coordinates."""
        return await _run("mouse_up", x, y, button, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_type(text: str, delay_ms: StrictInt = 0, connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                       method: Literal["auto", "keys", "clipboard", "guest"] = "auto",
                       paste_key: Literal["ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"] = "ctrl-v",
                       focus_x: StrictInt | None = None, focus_y: StrictInt | None = None) -> dict:
        """Submit text through Unicode clipboard or a bound guest profile. Auto fails before input when neither exists. Explicit method=keys requires inspecting the input mode; delay_ms is only for explicit keys and raises the minimum 20ms gap. Verify the target application afterward."""
        return await _run("send_text", text, method, paste_key, focus_x, focus_y, delay_ms,
                          session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_clipboard_set(text: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Send text to the remote VNC clipboard (RFB client cut text)."""
        return await _run("clipboard_set", text, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_clipboard_get(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Return the latest clipboard text announced by the remote VNC server."""
        return await _run("clipboard_get", session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_wait(seconds: float = 1.0, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Wait up to 60 seconds for an application to respond before taking another screenshot."""
        return (await application.wait(seconds, session_id, connection_id)).as_dict()

    @server.tool()
    async def vnc_wait_for(condition: Literal["changed", "matches", "stable"] = "stable",
                           reference_id: str | None = None, region: ScreenshotRegion | None = None,
                           timeout: float = 10.0, interval: float = 0.25, stable_for: float = 0.5,
                           pixel_threshold: StrictInt = 8, max_changed_ratio: float = 0.001,
                           image_format: Literal["jpeg", "png"] = "png", quality: StrictInt = 80,
                           connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> list:
        """Read-only sampled visual condition, returning the final screenshot and matched/timed_out. changed/matches need a reference_id from screenshot(remember=true); stable uses its own first frame and needs no reference. A pixel changes when any RGB channel differs by more than pixel_threshold; matches allows at most max_changed_ratio of pixels to change. Stable compares to one anchor for stable_for seconds. Region must be inside the reference. Timeout 0 samples once (changed/matches); other timeouts are 0-60 seconds. Polling releases the session queue between samples, never sends input, and never reconnects. A stable or matching picture does not prove application readiness. Always pass both IDs for named sessions."""
        box = (region.x, region.y, region.width, region.height) if region is not None else None
        result = await application.wait_for_visual(condition, reference_id, box, timeout, interval, stable_for,
                                                pixel_threshold, max_changed_ratio, image_format, quality,
                                                session_id, connection_id)
        image = result.value.screenshot
        return [json.dumps({**image.metadata(), **result.value.details,
                            "session_id": result.session_id, "connection_id": result.connection_id}),
                _image_content(image)]

    return server


def run() -> None:
    create_server().run(transport="stdio")
