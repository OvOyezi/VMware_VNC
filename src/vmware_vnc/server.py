# SPDX-License-Identifier: Apache-2.0
# This file has been modified by the VMware VNC maintainers.
"""Instance-owned MCP stdio adapter for the shared VNC application."""
from __future__ import annotations

from contextlib import asynccontextmanager
import base64
import json
from typing import Annotated, Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.types import ImageContent
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, create_model

from .application import VNCApplication
from .config import VNCConfig
from .inspection import KeyboardField
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
    'inventory and vnc_status for current state. Send commands, paths and mixed-language text with '
    'vnc_text or vnc_type. Check capabilities.literal_text_transport for the negotiated UTF-8 clipboard '
    'or the bound guest profile. For pure keyboard input use vnc_type_keys on the focused control. '
    'Direct mode simulates typing, including pinyin composition. A known RichEdit or GTK control '
    'can accept a whole printable string via entry_mode=richedit-hex or gtk-hex in direct ASCII input mode. '
    'Check the result after the batch. Use vnc_inspect(keyboard_fields=[...]) for specific keyboard state. '
    'It uses the latest server lock-key report and returns one original PNG for fields still unknown when '
    'a frame is available. Use that image only for unambiguous visible indicators. '
    'vnc_status.keyboard_state.leds reads the latest lock-key report without a screenshot. '
    'Ctrl/Alt/Super shortcut letters are case-insensitive; request Shift explicitly when needed. '
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


def _strict_input_tool(function: Callable) -> Tool:
    # FastMCP normally discards unknown arguments. A discarded focus or method
    # could send input through a different route or to a different control.
    tool = Tool.from_function(function)
    arguments = tool.fn_metadata.arg_model
    strict = create_model(arguments.__name__, __base__=arguments, __config__=ConfigDict(extra="forbid"))
    tool.fn_metadata.arg_model = strict
    tool.parameters = strict.model_json_schema(by_alias=True)
    return tool


def create_server(config: VNCConfig | None = None, *, application: VNCApplication | None = None) -> FastMCP:
    """Construct an isolated adapter without opening sockets or loading guest profiles."""
    if config is not None and application is not None:
        raise ValueError("provide either config or application")
    application = application or VNCApplication(config)

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        async with application:
            yield {"application": application}

    async def _run(method: str, *args, session_id: str, connection_id: str | None = None, pin: bool = True) -> dict:
        return (await application.run(method, *args, session_id=session_id, connection_id=connection_id, pin=pin)).as_dict()

    async def vnc_text(text: str, method: Literal["auto", "clipboard", "guest"] = "auto",
                       paste_key: Literal["ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"] = "ctrl-v",
                       connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                       focus_x: StrictInt | None = None, focus_y: StrictInt | None = None) -> dict:
        """Paste literal text, including commands and mixed scripts. Auto uses negotiated UTF-8 clipboard or the bound guest profile. Guest paste requires focus_x/focus_y from the inspected screenshot. If neither channel is available the call fails before input. Verify the application afterward."""
        return await _run("send_text", text, method, paste_key, focus_x, focus_y, session_id=session_id, connection_id=connection_id)

    async def vnc_type(text: str, delay_ms: Annotated[StrictInt, Field(ge=0, le=0)] = 0, connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                       method: Literal["auto", "clipboard", "guest"] = "auto",
                       paste_key: Literal["ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"] = "ctrl-v",
                       focus_x: StrictInt | None = None, focus_y: StrictInt | None = None) -> dict:
        """Paste literal text, like vnc_text. Guest paste requires inspected focus_x/focus_y. The legacy delay_ms argument accepts only zero. Inspect the application after pasting."""
        return await _run("send_text", text, method, paste_key, focus_x, focus_y, delay_ms,
                          session_id=session_id, connection_id=connection_id)

    async def vnc_type_keys(text: str, delay_ms: StrictInt = 0, connection_id: str | None = None,
                            session_id: str = DEFAULT_SESSION,
                            entry_mode: Literal["direct", "richedit-hex", "gtk-hex"] = "direct",
                            expect_caps_lock: StrictBool | None = None) -> dict:
        """Send RFB keys to the focused control. Direct mode simulates typing; CRLF, CR or LF presses Enter. Choose a hex mode only for a known RichEdit or GTK control with direct ASCII entry active. GTK commits with Enter. Optional expect_caps_lock checks the last reported LED without a screenshot; a missing or different report stops input. Check the result after the batch."""
        return await _run("type_text", text, delay_ms, entry_mode, expect_caps_lock,
                          session_id=session_id, connection_id=connection_id)

    server = FastMCP(
        "VMware VNC", lifespan=lifespan, log_level="WARNING", instructions=INSTRUCTIONS,
        tools=[_strict_input_tool(fn) for fn in (vnc_text, vnc_type, vnc_type_keys)],
    )

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
    async def vnc_wake(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Explicitly attempt to wake a connected display using pointer motion only. Never clicks, types or unlocks; refuses held keys/buttons. Works with a 0x0 framebuffer. Capture and inspect a NEW screenshot before further input; success only means wake motion was sent."""
        return await _run("wake_display", session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_disconnect(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Disconnect only the selected session and release its slot. Named sessions require connection_id."""
        return await _run("disconnect", session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_status(session_id: str = DEFAULT_SESSION) -> dict:
        """Read session state after earlier queued work. keyboard_state.leds is the latest server lock-key report or null. ime_mode is unknown (null). This call does not capture the screen."""
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
                          connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                          keyboard_fields: Annotated[list[KeyboardField], Field(min_length=1)] | None = None) -> list:
        """Inspect exact RGB pixels or specific keyboard state. With keyboard_fields, lock keys use the last report on this connection. When reports cover the request, the tool returns values without a screenshot. Unresolved fields stay null. If the display is available, the tool provides one original PNG for those fields, cropped to region if supplied. Interpret only unambiguous visible indicators. Omit reference_id, points, detail_region and non-default zoom or pixel_threshold in keyboard mode.

        Pixel inspection returns original-color PNG. Native-coordinate points return RGB and reference deltas. A reference_id from screenshot(remember=true) enables exact comparison (threshold 0 default), original-color before/after crops and a labeled x16 amplified difference. Zoom 1-8 uses nearest-neighbor. Add origin plus image coordinate/zoom for native coordinates. Maximum detail region is 512x512, with an enlarged image limit of 4,194,304 pixels.

        Optional frame_id pins the latest captured pixels without a new capture. The tool checks it even when visual evidence is unnecessary. Replaced or reconnected frame IDs fail. Pass session_id and connection_id for named sessions. No input is sent."""
        box = (region.x, region.y, region.width, region.height) if region else None
        detail = (detail_region.x, detail_region.y, detail_region.width, detail_region.height) if detail_region else None
        coordinates = [(point.x, point.y) for point in points] if points is not None else None
        arguments = (box, reference_id, coordinates, detail, zoom, pixel_threshold, frame_id)
        if keyboard_fields is not None:
            arguments += (keyboard_fields,)
        result = await application.run("inspect", *arguments,
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
        """Press a key or chord, e.g. enter, ctrl-c, alt-tab, f5, or a. Ctrl/Alt/Super ASCII shortcut letters ignore case: Ctrl+A equals ctrl+a; use ctrl+shift+a for Shift. Standalone A retains its implicit Shift."""
        return await _run("keypress", key, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_key_down(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Hold a key or complete chord until vnc_key_up releases it. Shortcut notation follows vnc_keypress; overlapping held keysyms are rejected."""
        return await _run("key_down", key, session_id=session_id, connection_id=connection_id)

    @server.tool()
    async def vnc_key_up(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
        """Release the complete key or chord held with vnc_key_down; do not release one modifier from a held chord."""
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
