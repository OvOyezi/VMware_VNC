"""Read-only multi-VM acceptance through the actual registered stdio entry."""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
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
    config_before = args.config.read_bytes()
    entry = tomllib.loads(config_before.decode("utf-8-sig"))["mcp_servers"]["vmware_vnc"]
    assert entry.get("enabled", True)
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    params = StdioServerParameters(command=entry["command"], args=entry.get("args", []), cwd=entry.get("cwd"), env=env)
    targets = {name: int(port) for name, port in (item.rsplit("=", 1) for item in args.targets)}
    states = {}
    with args.output.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                report["tool_count"] = len(tools.tools)
                assert {"vnc_connect", "vnc_screenshot", "vnc_text", "vnc_profiles", "vnc_diagnostics"}.issubset({tool.name for tool in tools.tools})

                async def call(name, **arguments):
                    return payload(await session.call_tool(name, arguments))

                async def screenshot(label, index, region=None):
                    state = states[label]
                    arguments = {"session_id": label, "connection_id": state["connection_id"]}
                    if region:
                        arguments["region"] = region
                    started = time.monotonic()
                    result = await session.call_tool("vnc_screenshot", arguments)
                    assert not result.isError, result
                    metadata = json.loads(next(item.text for item in result.content if item.type == "text"))
                    content = next(item for item in result.content if item.type == "image")
                    assert content.mimeType == "image/jpeg"
                    data = base64.b64decode(content.data)
                    image = Image.open(BytesIO(data))
                    expected_size = (region["width"], region["height"]) if region else (state["width"], state["height"])
                    assert image.size == expected_size
                    assert (metadata["desktop_width"], metadata["desktop_height"]) == (state["width"], state["height"])
                    assert metadata["session_id"] == label and metadata["connection_id"] == state["connection_id"]
                    path = args.output.with_name(f"{args.output.stem}-{label}-{index}.jpg")
                    path.write_bytes(data)
                    sample = {"label": label, "sample": index, "path": str(path), "seconds": round(time.monotonic() - started, 4), **metadata}
                    report["images"].append(sample)
                    return sample

                async def connect(label, sample="initial"):
                    state = await call("vnc_connect", host=args.host, port=targets[label], session_id=label)
                    states[label] = state
                    assert state["connected"] and state["session_id"] == label
                    await screenshot(label, sample)
                    return state

                try:
                    started = time.monotonic()
                    await asyncio.gather(*(connect(label) for label in targets))
                    report["parallel_connect_and_initial_capture_seconds"] = round(time.monotonic() - started, 4)
                    report["initial_states"] = dict(states)
                    initial_ids = {label: state["connection_id"] for label, state in states.items()}
                    report["inventory"] = await call("vnc_sessions")
                    assert report["inventory"]["session_count"] == len(targets)
                    assert not (await call("vnc_status"))["connected"]
                    assert (await call("vnc_sessions"))["session_count"] == len(targets)
                    report["checks"]["simultaneous_named_connections_and_independent_default"] = True

                    for index in range(3):
                        started = time.monotonic()
                        await asyncio.gather(*(screenshot(label, f"parallel-{index}") for label in targets))
                        report["parallel_capture_batch_seconds"].append(round(time.monotonic() - started, 4))
                    region = dict(zip(("x", "y", "width", "height"), args.region))
                    await asyncio.gather(*(screenshot(label, "region", region) for label in targets))
                    report["checks"]["repeated_parallel_full_and_region_images_keep_correct_ids_and_native_coordinates"] = True

                    first, second = list(targets)[:2]
                    disconnected = await call("vnc_disconnect", session_id=first, connection_id=states[first]["connection_id"])
                    assert not disconnected["connected"]
                    del states[first]
                    other = await call("vnc_status", session_id=second)
                    assert other["connection_id"] == initial_ids[second]
                    await screenshot(second, "other-disconnected")
                    report["checks"]["disconnect_one_vm_preserves_the_other_connection"] = True

                    await connect(first, "reconnected")
                    assert states[first]["connection_id"] != initial_ids[first]
                    stale = await session.call_tool("vnc_screenshot", {"session_id": first, "connection_id": initial_ids[first]})
                    assert stale.isError
                    report["checks"]["reconnect_changes_generation_and_rejects_previous_handle"] = True
                    for label, state in states.items():
                        assert (await call("vnc_status", session_id=label))["connection_id"] == state["connection_id"]
                finally:
                    for label, state in states.items():
                        await call("vnc_disconnect", session_id=label, connection_id=state["connection_id"])
                    report["final_inventory"] = await call("vnc_sessions")
                    assert report["final_inventory"]["session_count"] == 0
    report["config_sha256"] = hashlib.sha256(config_before).hexdigest()
    assert args.config.read_bytes() == config_before
    report["checks"]["registered_configuration_unchanged_and_all_test_sessions_closed"] = True
    report["success"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--targets", nargs="+", required=True)
    parser.add_argument("--region", nargs=4, type=int, default=[470, 300, 740, 420])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.config, args.output = args.config.resolve(), args.output.resolve()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "registered_server": "vmware_vnc",
              "input_sent": False, "images": [], "parallel_capture_batch_seconds": [], "checks": {}, "success": False}
    try:
        asyncio.run(asyncio.wait_for(run(args, report), timeout=55))
    except BaseException:
        report["failure"] = traceback.format_exc()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "images"}, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
