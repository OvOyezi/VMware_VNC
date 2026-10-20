"""Read-only acceptance: repeated images and VM switching in one registered MCP process."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import time
import tomllib
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


def payload(result):
    assert not result.isError, [getattr(item, "text", "") for item in result.content]
    return result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))


async def run(args, report):
    entry = tomllib.loads(args.config.read_text(encoding="utf-8-sig"))["mcp_servers"]["vmware_vnc"]
    assert entry.get("enabled", True)
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    params = StdioServerParameters(command=entry["command"], args=entry.get("args", []), cwd=entry.get("cwd"), env=env)
    with args.output.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                report["tool_count"] = len(tools.tools)
                screenshot_tool = next(tool for tool in tools.tools if tool.name == "vnc_screenshot")
                assert screenshot_tool.inputSchema["properties"]["image_format"]["default"] == "jpeg"
                assert (await session.call_tool("vnc_connect")).isError
                assert (await session.call_tool("vnc_connect", {"host": args.host})).isError
                report["explicit_target_required"] = True
                targets = [target.rsplit("=", 1) for target in args.targets]
                for label, port_text in targets + targets[:1]:
                    target = {"label": label, "host": args.host, "port": int(port_text), "images": []}
                    report["targets"].append(target)
                    target["connection"] = payload(await session.call_tool("vnc_connect", {"host": args.host, "port": int(port_text)}))
                    native_size = target["connection"]["width"], target["connection"]["height"]
                    index = len(report["targets"])
                    formats = ["jpeg", "jpeg", "jpeg", "png"] if index <= len(targets) else ["jpeg"]
                    for sample, image_format in enumerate(formats):
                        started = time.monotonic()
                        arguments = {} if image_format == "jpeg" else {"image_format": "png"}
                        result = await session.call_tool("vnc_screenshot", arguments)
                        assert not result.isError, [getattr(item, "text", "") for item in result.content]
                        content = next(item for item in result.content if item.type == "image")
                        assert content.mimeType == "image/" + image_format
                        data = base64.b64decode(content.data)
                        image = Image.open(BytesIO(data))
                        assert image.size == native_size
                        suffix = "jpg" if image_format == "jpeg" else "png"
                        path = args.output.with_name(f"{args.output.stem}-{index}-{label}-{sample}.{suffix}")
                        path.write_bytes(data)
                        target["images"].append({"format": image_format, "bytes": len(data), "width": image.width, "height": image.height, "seconds": round(time.monotonic() - started, 3), "path": str(path)})
                        if image_format == "png":
                            encoded = BytesIO()
                            image.save(encoded, format="JPEG", quality=80, subsampling=0, optimize=True)
                            target["same_frame_comparison"] = {"png_bytes": len(data), "jpeg_bytes": len(encoded.getvalue()), "reduction_percent": round(100 * (1 - len(encoded.getvalue()) / len(data)), 2)}
                    if args.region and index <= len(targets):
                        region = dict(zip(("x", "y", "width", "height"), args.region))
                        result = await session.call_tool("vnc_screenshot", {"region": region, "connection_id": target["connection"]["connection_id"]})
                        assert not result.isError
                        metadata = json.loads(next(item.text for item in result.content if item.type == "text"))
                        content = next(item for item in result.content if item.type == "image")
                        data = base64.b64decode(content.data)
                        image = Image.open(BytesIO(data))
                        assert image.size == tuple(args.region[2:])
                        assert (metadata["origin_x"], metadata["origin_y"]) == tuple(args.region[:2])
                        assert (metadata["desktop_width"], metadata["desktop_height"]) == native_size
                        path = args.output.with_name(f"{args.output.stem}-{index}-{label}-region.jpg")
                        path.write_bytes(data)
                        target["region_image"] = {"path": str(path), **metadata}
                    assert payload(await session.call_tool("vnc_status"))["connected"]
                final = payload(await session.call_tool("vnc_disconnect"))
                assert not final["connected"] and final["host"] is None
                report["disconnected_state_cleared"] = True
                report["success"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--targets", nargs="+", required=True, help="LABEL=PORT entries")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    args = parser.parse_args()
    args.output = args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "input_sent": False, "registered_server": "vmware_vnc", "targets": [], "success": False}
    try:
        asyncio.run(asyncio.wait_for(run(args, report), timeout=50))
    except BaseException:
        report["failure"] = traceback.format_exc()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
