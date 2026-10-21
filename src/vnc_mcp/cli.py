"""CLI entry point for the packaged VNC MCP server and direct controls."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys

from . import __version__

def _connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="VNC host or IP address")
    parser.add_argument("--port", type=int, required=True, help="explicit VNC port; VMware ports normally start at 5901")
    parser.add_argument("--password-env", default="VNC_PASSWORD", help="environment variable holding VNC password")
    parser.add_argument("--ask-password", action="store_true", help="explicitly prompt for a password; automation never prompts by default")
    parser.add_argument("--timeout", type=float, default=15.0, help="operation timeout in seconds")
    parser.add_argument("--screenshot-mode", choices=("refresh", "fresh"), default=os.environ.get("VNC_SCREENSHOT_MODE", "refresh"), help="use fresh for VMware servers with refresh timeouts")
    parser.add_argument("--encoding", choices=("auto", "raw", "hextile"), default=os.environ.get("VNC_ENCODING", "auto"), help="lossless RFB transport encoding preference, with raw fallback")
    parser.add_argument("--profile", help="explicit VMware guest profile to bind to this endpoint")
    parser.add_argument("--profiles-file", help="local TOML profile file (or VNC_PROFILES_FILE)")


def _password(env_name: str, ask: bool = False) -> str | None:
    value = os.environ.get(env_name)
    if value:
        return value
    # Windows NUL can report isatty=True even with no interactive console.
    # Password prompts must therefore be explicitly requested, not inferred.
    if ask:
        entered = getpass.getpass("VNC password (leave blank for none): ")
        return entered or None
    return None


async def _wait_for_visual(session, args):
    from .sessions import SessionRegistry

    registry = SessionRegistry(lambda: session, idle_timeout=0)
    region = tuple(args.region) if args.region else None
    try:
        reference_id = None
        if args.reference_file:
            reference_id = await asyncio.to_thread(session.load_visual_reference, args.reference_file, region)
        elif args.condition == "changed":
            reference_id = await asyncio.to_thread(session.remember_visual, region)
        return await registry.wait_for_visual(
            args.condition, reference_id, region, args.wait_timeout, args.interval, args.stable_for,
            args.pixel_threshold, args.max_changed_ratio, args.image_format, args.quality,
            connection_id=session.connection_id,
        )
    finally:
        await registry.close()


def _run_action(args: argparse.Namespace) -> int:
    from .client import VNCSession
    from .guest import load_profiles

    session = None
    status_code = 0
    try:
        if args.command == "wait-for":
            from .visual import VisualCondition
            VisualCondition(args.condition, args.wait_timeout, args.interval, args.stable_for,
                            args.pixel_threshold, args.max_changed_ratio)
            if args.condition == "matches" and not args.reference_file:
                raise ValueError("matches requires --reference-file; use a PNG for exact comparisons")
            if args.condition == "stable" and args.reference_file:
                raise ValueError("stable samples its own initial frame; omit --reference-file")
        profile = None
        if args.profile:
            profiles = load_profiles(args.profiles_file)
            if args.profile not in profiles:
                raise ValueError("unknown guest profile")
            profile = profiles[args.profile]
            if not profile.matches(args.host, args.port):
                raise ValueError("guest profile endpoint does not match host and port")
        session = VNCSession(args.timeout, screenshot_mode=args.screenshot_mode, network_encoding=args.encoding)
        status = session.connect(args.host, args.port, _password(args.password_env, args.ask_password), profile)
        if args.command == "connect":
            result: object = status
        elif args.command == "screenshot":
            image = session.capture(args.image_format, args.quality, tuple(args.region) if args.region else None)
            target = Path(args.output or ("screenshot.jpg" if args.image_format == "jpeg" else "screenshot.png"))
            target.write_bytes(image.data)
            result = {"saved": str(target.resolve()), **image.metadata()}
        elif args.command == "click":
            result = session.click(args.x, args.y, args.button, args.count)
        elif args.command == "move":
            result = session.move(args.x, args.y)
        elif args.command == "drag":
            result = session.drag(args.x1, args.y1, args.x2, args.y2, args.button)
        elif args.command == "scroll":
            result = session.scroll(args.x, args.y, args.direction, args.amount)
        elif args.command == "keypress":
            result = session.keypress(args.key)
        elif args.command == "type":
            result = session.send_text(args.text, args.method, args.paste_key,
                                       *(args.focus or (None, None)), delay_ms=args.delay_ms)
        elif args.command == "text":
            if (args.text is None) == (args.text_file is None):
                raise ValueError("provide exactly one of text or --text-file")
            if args.text_file:
                path = Path(args.text_file)
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("text file exceeds the byte limit")
                text = path.read_text(encoding="utf-8-sig")
            else:
                text = args.text
            result = session.send_text(text, args.method, args.paste_key, *(args.focus or (None, None)))
        elif args.command == "clipboard-get":
            result = session.clipboard_get()
        elif args.command == "clipboard-set":
            result = session.clipboard_set(args.text)
        elif args.command == "wait":
            result = session.wait(args.seconds)
        elif args.command == "wait-for":
            waited = asyncio.run(_wait_for_visual(session, args))
            image = waited.value.screenshot
            target = Path(args.output or ("wait-result.jpg" if args.image_format == "jpeg" else "wait-result.png"))
            target.write_bytes(image.data)
            result = {"saved": str(target.resolve()), **image.metadata(), **waited.value.details,
                      "connection_id": waited.connection_id}
            status_code = 0 if waited.value.details["matched"] else 3
        elif args.command == "status":
            result = status
        else:
            raise ValueError(f"Unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False))
        return status_code
    except Exception as exc:
        print(f"vnc-mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        # vncdotool's reactor runs in a daemon thread; disconnect only after
        # pending VNC actions have drained, without hanging on an unopened client.
        if session is not None and session.client is not None:
            try:
                session.disconnect()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnc-mcp", description="VNC desktop controls and MCP stdio server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="run the MCP stdio server")
    sub.add_parser("doctor", help="read-only VNC runtime and optional clipboard-backend discovery")
    profiles = sub.add_parser("profiles", help="list configured endpoints without reading credentials")
    profiles.add_argument("--profiles-file")
    for name in ("connect", "status"):
        command = sub.add_parser(name, help=f"connect and show VNC {name} information")
        _connection_options(command)
    screenshot = sub.add_parser("screenshot", help="capture the desktop to a PNG")
    _connection_options(screenshot)
    screenshot.add_argument("--output")
    screenshot.add_argument("--format", dest="image_format", choices=("png", "jpeg"), default="png")
    screenshot.add_argument("--quality", type=int, default=80)
    screenshot.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    click = sub.add_parser("click", help="click at screen coordinates")
    _connection_options(click)
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument("--button", choices=("left", "middle", "right"), default="left")
    click.add_argument("--count", type=int, default=1)
    move = sub.add_parser("move", help="move pointer to screen coordinates")
    _connection_options(move)
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    drag = sub.add_parser("drag", help="drag between screen coordinates")
    _connection_options(drag)
    drag.add_argument("x1", type=int)
    drag.add_argument("y1", type=int)
    drag.add_argument("x2", type=int)
    drag.add_argument("y2", type=int)
    drag.add_argument("--button", choices=("left", "middle", "right"), default="left")
    scroll = sub.add_parser("scroll", help="scroll at screen coordinates")
    _connection_options(scroll)
    scroll.add_argument("x", type=int)
    scroll.add_argument("y", type=int)
    scroll.add_argument("direction", choices=("up", "down", "left", "right"))
    scroll.add_argument("--amount", type=int, default=1)
    keypress = sub.add_parser("keypress", help="press a key or chord such as ctrl-c or alt-tab")
    _connection_options(keypress)
    keypress.add_argument("key")
    type_command = sub.add_parser("type", help="type text in the focused desktop control")
    _connection_options(type_command)
    type_command.add_argument("text")
    type_command.add_argument("--delay-ms", type=int, default=0)
    type_command.add_argument("--method", choices=("auto", "keys", "clipboard", "guest"), default="auto")
    type_command.add_argument("--paste-key", choices=("ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"), default="ctrl-v")
    type_command.add_argument("--focus", nargs=2, type=int, metavar=("X", "Y"), help="observed input control coordinates; required for guest text")
    literal = sub.add_parser("text", help="submit Unicode text using negotiated clipboard or an explicit guest profile")
    _connection_options(literal)
    literal.add_argument("text", nargs="?")
    literal.add_argument("--text-file", help="UTF-8 input file; text is never a shell command")
    literal.add_argument("--method", choices=("auto", "keys", "clipboard", "guest"), default="auto")
    literal.add_argument("--paste-key", choices=("ctrl-v", "ctrl-shift-v", "shift-insert", "meta-v"), default="ctrl-v")
    literal.add_argument("--focus", nargs=2, type=int, metavar=("X", "Y"), help="observed input control coordinates; required for guest text")
    subcommands = (("clipboard-get", None), ("clipboard-set", "text"))
    for name, value in subcommands:
        command = sub.add_parser(name, help=f"remote clipboard {name.removeprefix('clipboard-')}")
        _connection_options(command)
        if value:
            command.add_argument(value)
    wait = sub.add_parser("wait", help="wait for the desktop to respond")
    _connection_options(wait)
    wait.add_argument("seconds", type=float, default=1.0, nargs="?")
    visual = sub.add_parser("wait-for", help="wait for sampled visual change, a reference image, or a stable region")
    _connection_options(visual)
    visual.add_argument("--condition", choices=("changed", "matches", "stable"), default="stable")
    visual.add_argument("--reference-file", help="explicit PNG/JPEG matching the selected region; required for matches")
    visual.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    visual.add_argument("--wait-timeout", type=float, default=10.0, help="visual condition budget in seconds; separate from network --timeout")
    visual.add_argument("--interval", type=float, default=0.25)
    visual.add_argument("--stable-for", type=float, default=0.5)
    visual.add_argument("--pixel-threshold", type=int, default=8)
    visual.add_argument("--max-changed-ratio", type=float, default=0.001)
    visual.add_argument("--format", dest="image_format", choices=("png", "jpeg"), default="png")
    visual.add_argument("--quality", type=int, default=80)
    visual.add_argument("--output", help="final screenshot, saved on both match and timeout")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "doctor":
        from .diagnostics import get_diagnostics
        print(json.dumps(get_diagnostics(), ensure_ascii=False))
        return 0
    if args.command == "profiles":
        from .guest import load_profiles
        try:
            print(json.dumps({"profiles": [profile.public() for profile in load_profiles(args.profiles_file).values()]}, ensure_ascii=False))
            return 0
        except (OSError, ValueError) as exc:
            print(f"vnc-mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if args.command == "mcp":
        from .server import run as run_mcp
        run_mcp()
        return 0
    return _run_action(args)


if __name__ == "__main__":
    raise SystemExit(main())
