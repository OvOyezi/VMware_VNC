"""Optional loopback interoperability checks through real websockify."""
from __future__ import annotations

import argparse
import asyncio
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


TOKEN = "fixture"
INITIAL_COLORS = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]
REFRESH_COLORS = [(9, 81, 173), (28, 126, 224), (63, 158, 211), (197, 221, 246)]
CHECK_NAMES = (
    "rfb_connection_websockify_handshake_pixels_inputs_disconnect_runtime_release",
    "mcp_stdio_websockify_screenshot_status_redaction_and_cleanup",
    "cli_websockify_url_screenshot_pixels_and_disconnect",
)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _proxy_version(python: Path, source: Path) -> str:
    result = subprocess.run(
        [str(python), "-c", "import importlib.metadata; print(importlib.metadata.version('websockify'))"],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("the selected interpreter cannot import the websockify distribution")
    version = result.stdout.strip()
    if not version:
        raise RuntimeError("websockify returned an empty version")
    return version


def _redact(text: str) -> str:
    return text.replace("token=fixture", "token=<redacted>").replace(TOKEN, "<redacted>")[:1200]


class WebsockifyProxy:
    """One foreground proxy bound to loopback and one fixture TCP endpoint."""

    def __init__(self, python: Path, source: Path, target_port: int):
        self.python = python
        self.source = source
        self.target_port = target_port
        self.port = _free_loopback_port()
        self.process: subprocess.Popen | None = None
        self._log = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/rfb?token={TOKEN}"

    def __enter__(self) -> WebsockifyProxy:
        self._log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        command = [
            str(self.python), "-m", "websockify",
            f"127.0.0.1:{self.port}", f"127.0.0.1:{self.target_port}",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.source,
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                close_fds=True,
            )
            self._wait_ready()
            return self
        except BaseException:
            self.close()
            raise

    def _wait_ready(self) -> None:
        assert self.process is not None
        deadline = time.monotonic() + 8
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"websockify exited during startup (code {self.process.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise TimeoutError(f"websockify did not open its loopback port: {last_error}")

    def close(self) -> None:
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class RefreshSender:
    """Send a fresh full frame after capture asks for its second update."""

    def __init__(self, server: Any, colors: list[tuple[int, int, int]]):
        self.server = server
        self.colors = colors
        self.errors: list[BaseException] = []
        self.cancelled = threading.Event()
        self.sent = False
        self.thread = threading.Thread(target=self._send, name="websockify-rfb-refresh", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _send(self) -> None:
        try:
            deadline = time.monotonic() + 8
            predicate = lambda events: sum(event.get("type") == 3 for event in events) >= 2
            while not self.cancelled.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("capture did not send its second framebuffer request")
                try:
                    self.server.wait_for(predicate, timeout=min(0.1, remaining))
                except TimeoutError:
                    continue
                self.server.send_update([(0, 0, 2, 2, self.colors)])
                self.sent = True
                return
        except BaseException as exc:
            self.errors.append(exc)

    def join(self) -> None:
        self.thread.join(timeout=9)
        if self.thread.is_alive():
            raise TimeoutError("framebuffer refresh helper did not stop")
        if self.errors:
            raise RuntimeError(f"framebuffer refresh helper failed: {type(self.errors[0]).__name__}") from self.errors[0]
        if not self.sent:
            raise TimeoutError("framebuffer refresh helper stopped before sending an update")

    def cancel(self) -> None:
        self.cancelled.set()
        self.thread.join(timeout=1)


def _assert_pixels(image: Any, colors: list[tuple[int, int, int]], label: str) -> None:
    if image.size != (2, 2):
        raise AssertionError(f"{label} image dimensions differ: {image.size}")
    actual = [image.getpixel((x, y)) for y in range(2) for x in range(2)]
    if actual != colors:
        raise AssertionError(f"{label} pixels differ: {actual!r}")


def _assert_fixture_disconnected(server: Any, label: str) -> None:
    if not server.disconnected.wait(4):
        raise TimeoutError(f"{label}: RFB fixture did not observe the proxy connection closing")
    if server.errors:
        raise AssertionError(f"{label}: fixture reported {server.errors!r}")


def _check_rfb(source: Path, proxy_python: Path) -> None:
    from vmware_vnc.rfb import RFBConnection, runtime_info
    from vnc_native_rfb import NativeRfbFixture

    before = runtime_info()
    if before["io_threads"] or before["lease_count"]:
        raise AssertionError(f"RFB runtime was already active before the check: {before!r}")
    with NativeRfbFixture() as server:
        with WebsockifyProxy(proxy_python, source, server.port) as proxy:
            client = RFBConnection("", 0, None, 3, "raw", url=proxy.url)
            try:
                if client.endpoint.public()["transport"] != "ws":
                    raise AssertionError("RFBConnection did not select WebSocket transport")
                if (
                    server.client_banner != b"RFB 003.008\n"
                    or server.security_selected != 1
                    or server.shared_flag != 1
                    or client.frame_sequence != 1
                ):
                    raise AssertionError("RFB handshake or initial framebuffer exchange was incomplete")
                _assert_pixels(client.screen, INITIAL_COLORS, "RFBConnection initial frame")
                client.key_press("A")
                client.mouse_move(1, 1)
                client.mouse_down(1)
                client.mouse_up(1)
                events = server.wait_for(
                    lambda items: items if (
                        sum(event.get("type") == 4 for event in items) >= 4
                        and sum(event.get("type") == 5 for event in items) >= 3
                    ) else None,
                    timeout=4,
                )
                key_events = [event for event in events if event.get("type") == 4]
                key_wire = [(event["down"], event["keysym"]) for event in key_events]
                if key_wire != [(1, 0xFFE1), (1, ord("A")), (0, ord("A")), (0, 0xFFE1)]:
                    raise AssertionError(f"unexpected key events through WebSocket: {key_wire!r}")
                pointer_events = [event for event in events if event.get("type") == 5]
                if [(event["buttons"], event["x"], event["y"]) for event in pointer_events[-3:]] != [
                    (0, 1, 1), (1, 1, 1), (0, 1, 1),
                ]:
                    raise AssertionError("pointer move/down/up did not survive WebSocket proxying")
            finally:
                client.close()
            _assert_fixture_disconnected(server, "RFBConnection")
            after = runtime_info()
            if after["io_threads"] != 0 or after["lease_count"] != 0 or after["connection_count"] != 0:
                raise AssertionError(f"RFB runtime resources were not released: {after!r}")


def _clean_child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("VNC_"):
            environment.pop(name, None)
    environment.update(
        VNC_TIMEOUT="3",
        VNC_ACTION_TIMEOUT="6",
        VNC_SCREENSHOT_MODE="refresh",
        VNC_IDLE_TIMEOUT="0",
        PYTHONUNBUFFERED="1",
    )
    return environment


def _mcp_tool_json(result: Any, tool: str) -> dict[str, Any]:
    if result.isError:
        messages = [getattr(item, "text", "") for item in result.content]
        raise AssertionError(f"MCP {tool} failed: {messages!r}")
    text = next((item.text for item in result.content if getattr(item, "type", None) == "text"), None)
    if text is None:
        raise AssertionError(f"MCP {tool} did not return JSON text")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AssertionError(f"MCP {tool} did not return an object")
    return value


async def _mcp_transaction(source: Path, url: str, server: Any, stderr_log: Any) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from PIL import Image

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(source / "main.py"), "mcp"],
        cwd=str(source),
        env=_clean_child_environment(),
    )
    async with stdio_client(parameters, errlog=stderr_log) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            connect = _mcp_tool_json(
                await session.call_tool("vnc_connect", {"url": url}), "vnc_connect",
            )
            connection_id = connect.get("connection_id")
            if not connect.get("connected") or not connection_id or connect.get("transport") != "ws":
                raise AssertionError(f"MCP WebSocket connect returned unexpected status: {connect!r}")
            if "token=fixture" in json.dumps(connect, ensure_ascii=False) or url in json.dumps(connect, ensure_ascii=False):
                raise AssertionError("MCP public connect status exposed the WebSocket URI token")

            try:
                shot = await session.call_tool(
                    "vnc_screenshot", {"image_format": "png", "connection_id": connection_id},
                )
                if shot.isError:
                    raise AssertionError(f"MCP screenshot failed: {[getattr(item, 'text', '') for item in shot.content]!r}")
                images = [item for item in shot.content if getattr(item, "type", None) == "image"]
                if len(images) != 1 or images[0].mimeType != "image/png":
                    raise AssertionError("MCP screenshot did not return exactly one PNG")
                pixels = Image.open(BytesIO(base64.b64decode(images[0].data))).convert("RGB")
                _assert_pixels(pixels, REFRESH_COLORS, "MCP screenshot")

                status = _mcp_tool_json(await session.call_tool("vnc_status"), "vnc_status")
                if not status.get("connected") or status.get("transport") != "ws":
                    raise AssertionError(f"MCP status returned unexpected state: {status!r}")
                public_status = json.dumps(status, ensure_ascii=False)
                if "token=fixture" in public_status or url in public_status:
                    raise AssertionError("MCP public status exposed the WebSocket URI token")
            finally:
                disconnected = _mcp_tool_json(
                    await session.call_tool("vnc_disconnect", {"connection_id": connection_id}),
                    "vnc_disconnect",
                )
                if disconnected.get("connected"):
                    raise AssertionError("MCP disconnect left the session connected")
                inventory = _mcp_tool_json(await session.call_tool("vnc_sessions"), "vnc_sessions")
                if inventory.get("session_count") != 0:
                    raise AssertionError(f"MCP disconnect left a session allocated: {inventory!r}")
                diagnostics = _mcp_tool_json(await session.call_tool("vnc_diagnostics"), "vnc_diagnostics")
                transport = diagnostics.get("transport", {})
                if transport.get("io_threads") != 0 or transport.get("lease_count") != 0:
                    raise AssertionError(f"MCP disconnect left RFB runtime resources: {transport!r}")


def _check_mcp(source: Path, proxy_python: Path) -> None:
    from vnc_native_rfb import NativeRfbFixture

    with NativeRfbFixture() as server:
        with WebsockifyProxy(proxy_python, source, server.port) as proxy:
            refresher = RefreshSender(server, REFRESH_COLORS)
            refresher.start()
            try:
                with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_log:
                    asyncio.run(asyncio.wait_for(
                        _mcp_transaction(source, proxy.url, server, stderr_log), timeout=30,
                    ))
                refresher.join()
                _assert_fixture_disconnected(server, "MCP stdio")
            finally:
                refresher.cancel()


def _check_cli(source: Path, proxy_python: Path, temp_dir: Path) -> None:
    from PIL import Image
    from vnc_native_rfb import NativeRfbFixture

    with NativeRfbFixture() as server:
        with WebsockifyProxy(proxy_python, source, server.port) as proxy:
            refresher = RefreshSender(server, REFRESH_COLORS)
            refresher.start()
            try:
                output = temp_dir / "websockify-cli.png"
                command = [
                    sys.executable, str(source / "main.py"), "screenshot",
                    "--url", proxy.url,
                    "--screenshot-mode", "refresh",
                    "--timeout", "3",
                    "--action-timeout", "6",
                    "--output", str(output),
                ]
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                result = subprocess.run(
                    command,
                    cwd=source,
                    env=_clean_child_environment(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=25,
                    creationflags=creationflags,
                    check=False,
                )
                if result.returncode != 0:
                    detail = _redact((result.stderr or result.stdout).strip())
                    raise RuntimeError(f"CLI screenshot exited {result.returncode}: {detail}")
                payload = json.loads(result.stdout.strip())
                if "token=fixture" in json.dumps(payload, ensure_ascii=False) or proxy.url in result.stdout:
                    raise AssertionError("CLI screenshot output exposed the WebSocket URI token")
                _assert_pixels(Image.open(output).convert("RGB"), REFRESH_COLORS, "CLI screenshot")
                refresher.join()
                _assert_fixture_disconnected(server, "CLI screenshot")
            finally:
                refresher.cancel()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    parser.add_argument("--websockify-python", required=True, type=Path,
                        help="explicit Python interpreter containing websockify")
    args = parser.parse_args(argv)
    source = args.source.resolve()
    proxy_python = args.websockify_python.resolve()
    report: dict[str, Any] = {
        "success": False,
        "websockify_version": None,
        "checks": {name: False for name in CHECK_NAMES},
    }
    try:
        if not (source / "main.py").is_file() or not (source / "src" / "vmware_vnc").is_dir():
            raise ValueError("--source must name the vmware-vnc repository root")
        if not proxy_python.is_file():
            raise ValueError("--websockify-python must name an existing interpreter")
        sys.path.insert(0, str(source / "src"))
        sys.path.insert(0, str(source / "checks"))

        report["websockify_version"] = _proxy_version(proxy_python, source)
        with tempfile.TemporaryDirectory(prefix="vnc-websockify-check-") as directory:
            temp_dir = Path(directory)
            _check_rfb(source, proxy_python)
            report["checks"][CHECK_NAMES[0]] = True
            _check_mcp(source, proxy_python)
            report["checks"][CHECK_NAMES[1]] = True
            _check_cli(source, proxy_python, temp_dir)
            report["checks"][CHECK_NAMES[2]] = True
        report["success"] = all(report["checks"].values())
    except Exception as exc:
        message = _redact(str(exc))
        report["failure"] = {"type": type(exc).__name__, "message": message}
    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
