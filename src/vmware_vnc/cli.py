# SPDX-License-Identifier: Apache-2.0
# Modified and maintained for VMware VNC; historical attribution is in NOTICE.
"""CLI entry point for the packaged VNC application and MCP stdio server."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys

from . import __version__


class _CLIArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if hasattr(parsed, "host"):
            if (parsed.host is None) != (parsed.port is None):
                self.error("--host and --port must be supplied together")
            if parsed.url is not None and (
                parsed.host is not None or parsed.port is not None or parsed.profile is not None
            ):
                self.error("--url cannot be combined with --host, --port or --profile")
        return parsed


def _connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="VNC host or IP address; supply together with --port")
    parser.add_argument("--port", type=int, help="explicit VNC port; VMware ports normally start at 5901")
    parser.add_argument("--url", help="VNC endpoint URL: vnc://, vnc+tls://, ws:// or wss://")
    parser.add_argument("--password-env", default=None, help="environment variable holding VNC password")
    parser.add_argument("--ask-password", action="store_true", help="explicitly prompt for a password; automation never prompts by default")
    parser.add_argument("--timeout", type=float, default=None, help="connection timeout in seconds")
    parser.add_argument("--action-timeout", type=float, default=None, help="per-action time budget in seconds")
    parser.add_argument("--screenshot-mode", choices=("auto", "refresh", "fresh"), default=None, help="probe refresh once, with a fresh shared-connection fallback")
    parser.add_argument("--encoding", choices=("auto", "raw", "hextile", "zrle", "tight"), default=None, help="lossless RFB transport encoding preference, with raw fallback")
    parser.add_argument("--profile", help="explicit VMware guest profile to bind to this endpoint")
    parser.add_argument("--profiles-file", default=None, help="local TOML profile file (or VNC_PROFILES_FILE)")
    parser.add_argument("--tls-ca-file", default=None, help="CA certificate file for TLS VNC/WebSocket endpoints")


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


def _config_from_args(args: argparse.Namespace):
    from dataclasses import replace

    from .config import VNCConfig

    config = VNCConfig.from_env()
    overrides = {}
    for option, field in (
        ("timeout", "timeout"),
        ("action_timeout", "action_timeout"),
        ("screenshot_mode", "screenshot_mode"),
        ("encoding", "encoding"),
        ("password_env", "password_env"),
        ("profiles_file", "profiles_file"),
        ("tls_ca_file", "tls_ca_file"),
    ):
        if hasattr(args, option):
            value = getattr(args, option)
            if value is not None:
                overrides[field] = value
    return replace(config, **overrides) if overrides else config


def _validate_target(args: argparse.Namespace, config) -> None:
    host, port, url, profile = args.host, args.port, args.url, args.profile
    if url is not None:
        if not url.strip():
            raise ValueError("--url must not be empty")
        if host is not None or port is not None or profile is not None:
            raise ValueError("--url cannot be combined with --host, --port or --profile")
        return
    if profile is not None and not profile.strip():
        raise ValueError("--profile must not be empty")
    if (host is None) != (port is None):
        raise ValueError("--host and --port must be supplied together")
    if host is not None:
        if not host.strip():
            raise ValueError("--host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        return
    if profile is not None:
        return
    if config.default_url:
        return
    if config.default_host.strip() and 1 <= config.default_port <= 65535:
        return
    raise ValueError(
        "select --host and --port together, --profile or --url, or configure VNC_HOST and VNC_PORT / VNC_URL"
    )


def _region(args: argparse.Namespace) -> tuple[int, int, int, int] | None:
    return tuple(args.region) if args.region else None


async def _run_action_async(args: argparse.Namespace, config) -> int:
    from .application import VNCApplication

    try:
        if args.command == "wait-for":
            from .visual import VisualCondition

            VisualCondition(args.condition, args.wait_timeout, args.interval, args.stable_for,
                            args.pixel_threshold, args.max_changed_ratio)
            if args.condition == "matches" and not args.reference_file:
                raise ValueError("matches requires --reference-file; use a PNG for exact comparisons")
            if args.condition == "stable" and args.reference_file:
                raise ValueError("stable samples its own initial frame; omit --reference-file")

        async with VNCApplication(config) as application:
            connection = await application.connect(
                args.host or "", args.port or 0,
                _password(config.password_env, args.ask_password) or "",
                profile=args.profile or "", url=args.url or "",
            )
            status_code = 0
            if args.command in {"connect", "status"}:
                result = connection.value
            elif args.command == "screenshot":
                captured = await application.run("capture", args.image_format, args.quality, _region(args))
                image = captured.value
                target = Path(args.output or ("screenshot.jpg" if args.image_format == "jpeg" else "screenshot.png"))
                target.write_bytes(image.data)
                result = {"saved": str(target.resolve()), **image.metadata()}
            elif args.command == "inspect":
                region = _region(args)
                reference_id = None
                if args.reference_file:
                    reference_id = (await application.run(
                        "load_visual_reference", args.reference_file, region,
                    )).value
                points = [tuple(point) for point in args.point] if args.point else None
                inspected_result = await application.run(
                    "inspect", region, reference_id, points,
                    tuple(args.detail_region) if args.detail_region else None,
                    args.zoom, args.pixel_threshold,
                )
                inspected = inspected_result.value
                target = Path(args.output_dir)
                target.mkdir(parents=True, exist_ok=True)
                saved = {}
                for kind, image in inspected.images:
                    path = target / f"{kind}.png"
                    path.write_bytes(image.data)
                    saved[kind] = str(path.resolve())
                result = {**inspected.details, "saved": saved}
            elif args.command == "click":
                result = (await application.run("click", args.x, args.y, args.button, args.count)).value
            elif args.command == "move":
                result = (await application.run("move", args.x, args.y)).value
            elif args.command == "wake":
                result = (await application.run("wake_display")).value
            elif args.command == "drag":
                result = (await application.run("drag", args.x1, args.y1, args.x2, args.y2, args.button)).value
            elif args.command == "scroll":
                result = (await application.run("scroll", args.x, args.y, args.direction, args.amount)).value
            elif args.command == "keypress":
                result = (await application.run("keypress", args.key)).value
            elif args.command == "type":
                focus_x, focus_y = args.focus or (None, None)
                result = (await application.run(
                    "send_text", args.text, args.method, args.paste_key,
                    focus_x, focus_y, args.delay_ms,
                )).value
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
                focus_x, focus_y = args.focus or (None, None)
                result = (await application.run(
                    "send_text", text, args.method, args.paste_key, focus_x, focus_y,
                )).value
            elif args.command == "clipboard-get":
                result = (await application.run("clipboard_get")).value
            elif args.command == "clipboard-set":
                result = (await application.run("clipboard_set", args.text)).value
            elif args.command == "wait":
                result = (await application.wait(args.seconds)).value
            elif args.command == "wait-for":
                region = _region(args)
                reference_id = None
                if args.reference_file:
                    reference_id = (await application.run(
                        "load_visual_reference", args.reference_file, region,
                    )).value
                elif args.condition == "changed":
                    reference_id = (await application.run("remember_visual", region)).value
                waited = await application.wait_for_visual(
                    args.condition, reference_id, region, args.wait_timeout, args.interval,
                    args.stable_for, args.pixel_threshold, args.max_changed_ratio,
                    args.image_format, args.quality,
                )
                image = waited.value.screenshot
                target = Path(args.output or ("wait-result.jpg" if args.image_format == "jpeg" else "wait-result.png"))
                target.write_bytes(image.data)
                result = {
                    "saved": str(target.resolve()), **image.metadata(),
                    **waited.value.details, "connection_id": waited.connection_id,
                }
                status_code = 0 if waited.value.details["matched"] else 3
            else:
                raise ValueError(f"Unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False))
        return status_code
    except Exception as exc:
        print(f"vmware-vnc: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


async def _run_readonly_async(args: argparse.Namespace, config) -> int:
    from .application import VNCApplication

    try:
        async with VNCApplication(config) as application:
            if args.command == "doctor":
                diagnostics = await application.diagnostics()
                # Keep the existing doctor JSON schema; the shared application
                # adapter adds configuration inventory for MCP consumers.
                fields = {
                    "schema_version", "application", "transport", "python", "host",
                    "runtime_versions", "vmrun",
                }
                result = {key: value for key, value in diagnostics.items() if key in fields}
            elif args.command == "profiles":
                result = await application.profiles()
            else:
                raise ValueError(f"Unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"vmware-vnc: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = _CLIArgumentParser(prog="vmware-vnc", description="VNC desktop controls and MCP stdio server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="run the MCP stdio server")
    sub.add_parser("doctor", help="read-only VNC runtime and optional clipboard-backend discovery")
    profiles = sub.add_parser("profiles", help="list configured endpoints without reading credentials")
    profiles.add_argument("--profiles-file", default=None)
    for name in ("connect", "status"):
        command = sub.add_parser(name, help=f"connect and show VNC {name} information")
        _connection_options(command)
    screenshot = sub.add_parser("screenshot", help="capture the desktop to a PNG")
    _connection_options(screenshot)
    screenshot.add_argument("--output")
    screenshot.add_argument("--format", dest="image_format", choices=("png", "jpeg"), default="png")
    screenshot.add_argument("--quality", type=int, default=80)
    screenshot.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    inspect = sub.add_parser("inspect", help="inspect lossless pixels and optional reference differences")
    _connection_options(inspect)
    inspect.add_argument("--reference-file", help="explicit PNG/JPEG reference with matching selected-region dimensions")
    inspect.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    inspect.add_argument("--point", nargs=2, type=int, action="append", metavar=("X", "Y"))
    inspect.add_argument("--detail-region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    inspect.add_argument("--zoom", type=int, default=4)
    inspect.add_argument("--pixel-threshold", type=int, default=0)
    inspect.add_argument("--output-dir", required=True, help="directory for current and clearly labeled detail PNGs")
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
    wake = sub.add_parser("wake", help="explicit pointer-only display wake; capture a new screenshot afterward")
    _connection_options(wake)
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .server import run as run_mcp

        run_mcp()
        return 0

    try:
        config = _config_from_args(args)
        if hasattr(args, "host"):
            _validate_target(args, config)
    except ValueError as exc:
        parser.error(str(exc))

    if args.command in {
        "connect", "status", "screenshot", "inspect", "click", "move", "wake", "drag", "scroll",
        "keypress", "type", "text", "clipboard-get", "clipboard-set", "wait", "wait-for",
    }:
        return asyncio.run(_run_action_async(args, config))
    return asyncio.run(_run_readonly_async(args, config))


if __name__ == "__main__":
    raise SystemExit(main())
