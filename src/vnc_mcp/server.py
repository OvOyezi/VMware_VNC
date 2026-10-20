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


def _new_session() -> VNCSession:
    return VNCSession(
        timeout=float(os.environ.get("VNC_TIMEOUT", "15")),
        screenshot_mode=os.environ.get("VNC_SCREENSHOT_MODE", "refresh"),
        action_timeout=float(os.environ.get("VNC_ACTION_TIMEOUT", "20")),
    )


REGISTRY = SessionRegistry(
    _new_session,
    max_sessions=int(os.environ.get("VNC_MAX_SESSIONS", "4")),
    idle_timeout=float(os.environ.get("VNC_IDLE_TIMEOUT", "300")),
)
DEFAULT_HOST = os.environ.get("VNC_HOST")
DEFAULT_PORT = int(os.environ["VNC_PORT"]) if os.environ.get("VNC_PORT") else 0
PASSWORD_ENV = os.environ.get("VNC_PASSWORD_ENV", "VNC_PASSWORD")


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
        "Control a remote desktop through VNC. Always call vnc_screenshot after connecting "
        "to inspect the screen. Coordinates always refer to the native desktop; cropped images "
        "include their origin, which must be added to image-local coordinates. "
        "Connect multiple desktops using distinct session_id values. Always pass BOTH session_id "
        "and the returned connection_id for named-session actions and screenshots. Omitting session_id "
        "uses the separate legacy default session; it never selects the most recently named session. "
        "Idle sessions expire after 300 seconds by default; reconnect explicitly and inspect again. "
        "Use vnc_sessions for nonblocking inventory and vnc_status for current state. "
        "Call vnc_screenshot again after actions to verify the result."
    ),
)


@mcp.tool()
async def vnc_connect(host: str = "", port: int = 0, password: str = "", session_id: str = DEFAULT_SESSION) -> dict:
    """Connect host AND port (VMware ports start at 5901). Use distinct session_id names for independent desktops. Reconnecting the same name replaces only that session and returns a new connection_id. Names: 1-64 ASCII letters/digits/._-, starting with a letter or digit."""
    target_host = host.strip() or DEFAULT_HOST
    target_port = port or DEFAULT_PORT
    if not target_host or not target_port:
        raise ValueError("Provide both host and port, or set VNC_HOST and VNC_PORT before starting the server")
    target_password = password or os.environ.get(PASSWORD_ENV) or None
    return await _run("connect", target_host, target_port, target_password, session_id=session_id, pin=False)


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
async def vnc_screenshot(image_format: Literal["jpeg", "png"] = "jpeg", quality: int = 80, region: ScreenshotRegion | None = None, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> list:
    """Capture the native desktop or a region {x,y,width,height}. JPEG quality: 1-95; PNG is lossless. A region's origin must be added to image-local click coordinates. No scaling."""
    box = (region.x, region.y, region.width, region.height) if region is not None else None
    result = await REGISTRY.run("capture", image_format, quality, box, session_id=session_id, connection_id=connection_id)
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
async def vnc_type(text: str, delay_ms: int = 0, connection_id: str | None = None, session_id: str = DEFAULT_SESSION) -> dict:
    """Type text into the focused desktop control; delay_ms optionally spaces key events."""
    return await _run("type_text", text, delay_ms, session_id=session_id, connection_id=connection_id)


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


def run() -> None:
    mcp.run(transport="stdio")
