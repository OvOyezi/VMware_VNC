"""MCP stdio server exposing screenshot-based VNC controls."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from pydantic import BaseModel, Field

from .client import VNCSession
from .sessions import DEFAULT_SESSION, SessionRegistry
from .guest import load_profiles
from .diagnostics import get_diagnostics


def _new_session() -> VNCSession:
    return VNCSession(
        timeout=float(os.environ.get("VNC_TIMEOUT", "15")),
        screenshot_mode=os.environ.get("VNC_SCREENSHOT_MODE", "refresh"),
        action_timeout=float(os.environ.get("VNC_ACTION_TIMEOUT", "20")),
        network_encoding=os.environ.get("VNC_ENCODING", "auto"),
    )


REGISTRY = SessionRegistry(
    _new_session,
    max_sessions=int(os.environ.get("VNC_MAX_SESSIONS", "4")),
    idle_timeout=float(os.environ.get("VNC_IDLE_TIMEOUT", "300")),
)
DEFAULT_HOST = os.environ.get("VNC_HOST")
DEFAULT_PORT = int(os.environ["VNC_PORT"]) if os.environ.get("VNC_PORT") else 0
PASSWORD_ENV = os.environ.get("VNC_PASSWORD_ENV", "VNC_PASSWORD")
PROFILES = load_profiles()


@asynccontextmanager
async def _lifespan(app: FastMCP):
    REGISTRY.start()
    try:
        yield {}
    finally:
        await REGISTRY.close()


async def _run(method: str, *args, session_id: str, connection_id: str | None = None, pin: bool = True) -> dict:
    result = await REGISTRY.run(method, *args, session_id=session_id, connection_id=connection_id, pin=pin)
    return result.as_dict()


class ScreenshotRegion(BaseModel):
    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


mcp = FastMCP(
    "VNC Desktop",
    lifespan=_lifespan,
    log_level="WARNING",
    instructions=(
        "Provide VNC screen observation and desktop input for VM tasks that need a visible interface. "
        "Always call vnc_screenshot after connecting "
        "to inspect the screen. Coordinates always refer to the native desktop; cropped images "
        "include their origin, which must be added to image-local coordinates. "
        "Connect multiple desktops using distinct session_id values. Always pass BOTH session_id "
        "and the returned connection_id for named-session actions and screenshots. Omitting session_id "
        "uses the separate legacy default session; it never selects the most recently named session. "
        "Idle sessions expire after 300 seconds by default; reconnect explicitly and inspect again. "
        "Use vnc_sessions for nonblocking inventory and vnc_status for current state. "
        "Use vnc_text or vnc_type for literal Unicode text: auto selects negotiated UTF-8 clipboard "
        "or an explicitly selected VMware guest profile. Guest text requires focus_x/focus_y from "
        "the inspected screenshot; the focus click clears any existing text selection. "
        "A sent paste is not proof that the application accepted text. Never replay failed input automatically. "
        "Use vnc_screenshot(remember=true) before an action when you need a pixel reference, then vnc_wait_for "
        "changed/matches, or stable without a reference. These are sampled visual conditions, not application readiness. "
        "Call vnc_screenshot again after actions to verify the result."
    ),
)


@mcp.tool()
async def vnc_connect(host: str = "", port: int = 0, password: str = "", session_id: str = DEFAULT_SESSION, profile: str = "") -> dict:
    """Connect to an explicit VNC host AND port, or a configured profile. Use distinct session_id names for independent desktops. Reconnecting the same name replaces only that session and returns a new connection_id. Names: 1-64 ASCII letters/digits/._-, starting with a letter or digit."""
    selected = None
    if profile:
        if profile not in PROFILES:
            raise ValueError("unknown guest profile; use vnc_profiles to list configured targets")
        selected = PROFILES[profile]
    target_host = host.strip() or (selected.host if selected else DEFAULT_HOST)
    target_port = port or (selected.port if selected else DEFAULT_PORT)
    if not target_host or not target_port:
        raise ValueError("Provide both host and port, or set VNC_HOST and VNC_PORT before starting the server")
    target_password = password or os.environ.get(PASSWORD_ENV) or None
    if selected is not None:
        return await _run("connect", target_host, target_port, target_password, selected, session_id=session_id, pin=False)
    return await _run("connect", target_host, target_port, target_password, session_id=session_id, pin=False)


@mcp.tool()
async def vnc_profiles() -> dict:
    """List explicit VMware guest profiles without reading or returning any credentials. Selecting a profile is required before guest text operations."""
    return {"profiles": [PROFILES[name].public() for name in sorted(PROFILES)]}


@mcp.tool()
async def vnc_diagnostics() -> dict:
    """Read-only VNC runtime and optional guest-clipboard backend discovery. Does not connect, execute guest commands or read credentials."""
    return {**get_diagnostics(), "configured_profiles": len(PROFILES)}


@mcp.tool()
async def vnc_text(text: str, method: Literal["auto", "keys", "clipboard", "guest"] = "auto",
                   paste_key: Literal["ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"] = "ctrl-v",
                   connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                   focus_x: int | None = None, focus_y: int | None = None) -> dict:
    """Submit literal Unicode text. Auto selects negotiated UTF-8 clipboard, then a bound VMware guest profile, then Latin-1 keys. Guest text REQUIRES focus_x/focus_y from the inspected screenshot: CLI helpers can steal focus. Linux terminals may need ctrl-shift-v. Never retries or presses Enter implicitly; verify the application afterward."""
    return await _run("send_text", text, method, paste_key, focus_x, focus_y, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_disconnect(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Disconnect only the selected session and release its slot. Named sessions require connection_id."""
    return await _run("disconnect", session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_status(session_id: str = DEFAULT_SESSION) -> dict:
    """Return current state of the selected session, waiting for its earlier work to finish. Unknown named sessions require an explicit connect."""
    return await _run("status", session_id=session_id, pin=False)


@mcp.tool()
async def vnc_sessions() -> dict:
    """List session IDs, cached target states, queue usage and idle ages without waiting for busy desktops. Passwords are never included. Use vnc_status(session_id=...) for live state."""
    return REGISTRY.inventory()


@mcp.tool()
async def vnc_screenshot(image_format: Literal["jpeg", "png"] = "jpeg", quality: int = 80, region: ScreenshotRegion | None = None, connection_id: str | None = None, session_id: str = DEFAULT_SESSION, remember: bool = False) -> list:
    """Capture the native desktop or a region {x,y,width,height}. JPEG quality: 1-95; PNG is lossless. Add region origin to image-local coordinates. remember=true returns a reference_id for vnc_wait_for; only the latest reference per connection is retained, until replacement, resize or disconnect. No scaling."""
    box = (region.x, region.y, region.width, region.height) if region is not None else None
    result = await REGISTRY.run("capture", image_format, quality, box, remember, session_id=session_id, connection_id=connection_id)
    image = result.value
    return [
        json.dumps({**image.metadata(), "session_id": result.session_id, "connection_id": result.connection_id}),
        Image(data=image.data, format=image_format),
    ]


@mcp.tool()
async def vnc_click(x: int, y: int, button: str = "left", count: int = 1, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Click at screen coordinates. button: left, middle, right; count: 1-3."""
    return await _run("click", x, y, button, count, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_move(x: int, y: int, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Move the pointer to screen coordinates."""
    return await _run("move", x, y, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left", connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Drag from one screen coordinate to another with a mouse button held."""
    return await _run("drag", x1, y1, x2, y2, button, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_scroll(x: int, y: int, direction: str, amount: int = 1, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Scroll at a screen coordinate. direction: up, down, left, right; amount: 1-20."""
    return await _run("scroll", x, y, direction, amount, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_keypress(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Press a key or chord, e.g. enter, ctrl-c, alt-tab, f5, or a."""
    return await _run("keypress", key, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_key_down(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Hold a key down until vnc_key_up releases it."""
    return await _run("key_down", key, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_key_up(key: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Release a key held with vnc_key_down."""
    return await _run("key_up", key, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_mouse_down(x: int, y: int, button: str = "left", connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Press and hold a mouse button at screen coordinates."""
    return await _run("mouse_down", x, y, button, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_mouse_up(x: int, y: int, button: str = "left", connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Release a mouse button at screen coordinates."""
    return await _run("mouse_up", x, y, button, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_type(text: str, delay_ms: int = 0, connection_id: str | None = None, session_id: str = DEFAULT_SESSION,
                   method: Literal["auto", "keys", "clipboard", "guest"] = "auto",
                   paste_key: Literal["ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"] = "ctrl-v",
                   focus_x: int | None = None, focus_y: int | None = None) -> dict:
    """Submit text using Unicode clipboard or an explicit guest profile. Explicit method=keys sends keysyms; delay_ms spaces key events and is valid only with keys/auto. Auto never guesses Unicode-key support. Verify the target application afterward."""
    return await _run("send_text", text, method, paste_key, focus_x, focus_y, delay_ms,
                      session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_clipboard_set(text: str, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Send text to the remote VNC clipboard (RFB client cut text)."""
    return await _run("clipboard_set", text, session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_clipboard_get(connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Return the latest clipboard text announced by the remote VNC server."""
    return await _run("clipboard_get", session_id=session_id, connection_id=connection_id)


@mcp.tool()
async def vnc_wait(seconds: float = 1.0, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Wait up to 60 seconds for an application to respond before taking another screenshot."""
    return (await REGISTRY.wait(seconds, session_id, connection_id)).as_dict()


@mcp.tool()
async def vnc_wait_for(condition: Literal["changed", "matches", "stable"] = "stable",
                       reference_id: str | None = None, region: ScreenshotRegion | None = None,
                       timeout: float = 10.0, interval: float = 0.25, stable_for: float = 0.5,
                       pixel_threshold: int = 8, max_changed_ratio: float = 0.001,
                       image_format: Literal["jpeg", "png"] = "jpeg", quality: int = 80,
                       connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> list:
    """Read-only sampled visual condition, returning the final screenshot and matched/timed_out. changed/matches need a reference_id from screenshot(remember=true); stable uses its own first frame and needs no reference. A pixel changes when any RGB channel differs by more than pixel_threshold; matches allows at most max_changed_ratio of pixels to change. Stable compares to one anchor for stable_for seconds. Region must be inside the reference. Timeout 0 samples once (changed/matches); other timeouts are 0-60 seconds. Polling releases the session queue between samples, never sends input, and never reconnects. A stable or matching picture does not prove application readiness. Always pass both IDs for named sessions."""
    box = (region.x, region.y, region.width, region.height) if region is not None else None
    result = await REGISTRY.wait_for_visual(condition, reference_id, box, timeout, interval, stable_for,
                                            pixel_threshold, max_changed_ratio, image_format, quality,
                                            session_id, connection_id)
    image = result.value.screenshot
    return [json.dumps({**image.metadata(), **result.value.details,
                        "session_id": result.session_id, "connection_id": result.connection_id}),
            Image(data=image.data, format=image_format)]


def run() -> None:
    mcp.run(transport="stdio")
