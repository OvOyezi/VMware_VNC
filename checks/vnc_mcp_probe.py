"""Read-only VNC-MCP connection/screenshot probe for an explicitly selected VM."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tomllib
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(args, report):
    env = dict(os.environ)
    if not args.codex_config:
        env.update(VNC_HOST=args.host, VNC_PORT=str(args.port), VNC_TIMEOUT="2")
    params = StdioServerParameters(command=sys.executable, args=[str(args.source / "main.py"), "mcp"], cwd=str(args.source), env=env)
    if args.codex_config:
        with args.codex_config.open("rb") as config_file:
            entry = tomllib.load(config_file)["mcp_servers"][args.server_name]
        assert entry.get("enabled", True), "Registered server is disabled"
        registered_env = entry.get("env", {})
        env.update(registered_env)
        params = StdioServerParameters(command=entry["command"], args=entry.get("args", []), cwd=entry.get("cwd"), env=env)
        report["registered_server"] = args.server_name
        report["registered_config"] = str(args.codex_config)
    with args.output.with_suffix(".stderr.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(params, errlog=error_log) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                report["mcp_initialized"] = True
                report["server_info"] = initialized.serverInfo.model_dump()
                report["tool_count"] = len((await session.list_tools()).tools)
                if args.check_unbound:
                    unspecified = await session.call_tool("vnc_connect")
                    report["unspecified_target_rejected"] = bool(unspecified.isError)
                    if not unspecified.isError:
                        await session.call_tool("vnc_disconnect")
                        report["connection_error"] = "Server still connects without an explicitly selected target"
                        return
                result = await session.call_tool("vnc_connect", {"host": args.host, "port": args.port})
                if result.isError:
                    report["connection_error"] = " ".join(getattr(item, "text", "") for item in result.content)
                    return
                report["connection"] = result.structuredContent
                if report["connection"] is None:
                    report["connection"] = json.loads(next(item.text for item in result.content if item.type == "text"))
                assert report["connection"]["connected"]
                assert report["connection"]["host"] == args.host and report["connection"]["port"] == args.port
                screenshot = await session.call_tool("vnc_screenshot")
                if screenshot.isError:
                    report["screenshot_error"] = " ".join(getattr(item, "text", "") for item in screenshot.content)
                else:
                    images = [item for item in screenshot.content if item.type == "image"]
                    if len(images) == 1:
                        image_path = args.output.with_suffix(".jpg" if images[0].mimeType == "image/jpeg" else ".png")
                        image_bytes = base64.b64decode(images[0].data)
                        image_path.write_bytes(image_bytes)
                        report["screenshot"] = str(image_path)
                        report["mime_type"] = images[0].mimeType
                        report["image_bytes"] = len(image_bytes)
                        report["success"] = True
                await session.call_tool("vnc_disconnect")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex-config", type=Path)
    parser.add_argument("--server-name", default="vmware_vnc")
    parser.add_argument("--check-unbound", action="store_true")
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "host": args.host, "port": args.port, "source": str(args.source), "input_sent": False, "success": False}
    try:
        asyncio.run(asyncio.wait_for(probe(args, report), timeout=15))
    except BaseException:
        report["error"] = traceback.format_exc()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
