"""Run explicit MCP tool calls against a selected VMware VNC endpoint."""

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ALLOWED = {"vnc_screenshot", "vnc_click", "vnc_keypress", "vnc_key_down", "vnc_key_up", "vnc_type", "vnc_wait"}


async def run(args, report):
    actions = json.loads(args.actions.read_text(encoding="utf-8-sig"))
    if not isinstance(actions, list) or any(action["tool"] not in ALLOWED for action in actions):
        raise ValueError("Actions must be a list of permitted VNC MCP tools")
    env = dict(os.environ)
    env.update(VNC_HOST=args.host, VNC_PORT=str(args.port), VNC_TIMEOUT="5", VNC_SCREENSHOT_MODE="fresh")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(args.source / "main.py"), "mcp"],
        cwd=str(args.source),
        env=env,
    )
    with args.output.with_suffix(".stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(parameters, errlog=errors) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                report["server_info"] = initialized.serverInfo.model_dump()

                async def call(tool, arguments):
                    result = await session.call_tool(tool, arguments)
                    if result.isError:
                        raise RuntimeError(" ".join(item.text for item in result.content if item.type == "text"))
                    return result

                connected = await call("vnc_connect", {})
                report["connection"] = [item.text for item in connected.content if item.type == "text"]
                try:
                    for index, action in enumerate(actions):
                        tool = action["tool"]
                        arguments = action.get("arguments", {})
                        if tool in {"vnc_click", "vnc_keypress", "vnc_key_down", "vnc_key_up", "vnc_type"}:
                            report["input_sent"] = True
                        result = await call(tool, arguments)
                        outcome = {"tool": tool, "result": []}
                        for item in result.content:
                            if item.type == "image":
                                suffix = "jpg" if item.mimeType == "image/jpeg" else "png"
                                path = args.output.with_name(f"{args.output.stem}-{index:02}.{suffix}")
                                path.write_bytes(base64.b64decode(item.data))
                                outcome["image"] = str(path)
                            elif item.type == "text":
                                outcome["result"].append(item.text)
                        report["actions"].append(outcome)
                finally:
                    await session.call_tool("vnc_disconnect", {})
                report["success"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "host": args.host, "port": args.port, "source": str(args.source), "input_sent": False, "success": False, "actions": []}
    try:
        asyncio.run(asyncio.wait_for(run(args, report), timeout=45))
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
