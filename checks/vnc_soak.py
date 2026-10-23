"""Manually run a read-only VNC screenshot soak through the real MCP stdio server.

Live endpoints are refused unless ``--allow-live`` is present. ``--self-test``
uses synthetic loopback RFB fixtures and never contacts a real desktop.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any


_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_TOOL_TIMEOUT_SECONDS = 30.0


class SoakError(RuntimeError):
    """A bounded test operation failed; no automatic retry is performed."""


@dataclass
class NumericAggregate:
    count: int = 0
    minimum: float | int | None = None
    maximum: float | int | None = None
    total: float | int = 0

    def add(self, value: float | int) -> None:
        self.count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.total += value

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "min": self.minimum,
            "average": self.total / self.count if self.count else None,
            "max": self.maximum,
            "total": self.total,
        }


@dataclass
class Target:
    name: str
    host: str
    port: int
    connection_id: str | None = None
    screenshot_count: int = 0
    reconnect_count: int = 0
    stale_id_rejections: int = 0
    client_seconds: NumericAggregate = field(default_factory=NumericAggregate)
    capture_seconds: NumericAggregate = field(default_factory=NumericAggregate)
    transport_bytes: NumericAggregate = field(default_factory=NumericAggregate)

    def record_screenshot(self, client_seconds: float, capture_seconds: float, transport_bytes: int) -> None:
        self.screenshot_count += 1
        self.client_seconds.add(client_seconds)
        self.capture_seconds.add(capture_seconds)
        self.transport_bytes.add(transport_bytes)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "screenshot_count": self.screenshot_count,
            "reconnect_count": self.reconnect_count,
            "stale_id_rejections": self.stale_id_rejections,
            "screenshot_client_seconds": self.client_seconds.summary(),
            "screenshot_capture_seconds": self.capture_seconds.summary(),
            "screenshot_transport_bytes": self.transport_bytes.summary(),
        }


def _valid_host(host: str) -> bool:
    if not host or host != host.strip() or any(char in host for char in " /\\\t\r\n") or ":" in host:
        return False
    try:
        ipaddress.IPv4Address(host)
        return True
    except ipaddress.AddressValueError:
        hostname = host[:-1] if host.endswith(".") else host
        return bool(hostname) and len(hostname) <= 253 and all(
            _HOST_LABEL.fullmatch(label) for label in hostname.split(".")
        )


def parse_targets(specs: list[str]) -> list[Target]:
    if not 1 <= len(specs) <= 4:
        raise ValueError("provide between one and four --targets entries")
    targets, names = [], set()
    for spec in specs:
        if spec.count("=") != 1:
            raise ValueError(f"invalid target {spec!r}; expected NAME=HOST:PORT")
        name, endpoint = spec.split("=", 1)
        if not _SESSION_NAME.fullmatch(name) or name == "default":
            raise ValueError(f"invalid target name {name!r}; use a non-default MCP session name")
        if name in names:
            raise ValueError(f"duplicate target name {name!r}")
        names.add(name)
        if endpoint.count(":") != 1:
            raise ValueError(f"invalid endpoint {endpoint!r}; expected IPv4-or-hostname:PORT")
        host, port_text = endpoint.rsplit(":", 1)
        if not _valid_host(host):
            raise ValueError(f"invalid IPv4 address or hostname {host!r}")
        if not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        targets.append(Target(name, host, int(port_text)))
    return targets


def _write_checkpoint(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=path.name + ".",
            suffix=".tmp", dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({key: value for key, value in report.items() if not key.startswith("_")},
                      handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _make_report(
    output: Path, mode: str, duration: float, interval: float, screenshot_mode: str,
    reconnect_every: int, targets: list[Target],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "success": False,
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "duration_target_seconds": duration,
        "duration_actual_seconds": 0.0,
        "total_runtime_seconds": 0.0,
        "setup_seconds": 0.0,
        "interval_seconds": interval,
        "rounds": 0,
        "reconnect_every_rounds": reconnect_every,
        "screenshot_mode": screenshot_mode,
        "targets": [target.summary() for target in targets],
        "cleanup": {"disconnect_attempts": 0, "errors": [], "inventory_session_count": None},
        "failure": None,
        "checkpoint_file": str(output),
    }


def _refresh_targets(report: dict[str, Any], targets: list[Target]) -> None:
    report["targets"] = [target.summary() for target in targets]


def _record_failure(report: dict[str, Any], exc: BaseException) -> None:
    if report.get("failure") is None:
        report["failure"] = {
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "reason": str(exc)[:1200] or type(exc).__name__,
        }
    report["success"] = False


def _environment(screenshot_mode: str, allow_credentials: bool) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("VNC_HOST", "VNC_PORT", "VNC_PROFILES_FILE"):
        env.pop(key, None)
    if not allow_credentials:
        env.pop("VNC_PASSWORD", None)
        env.pop("VNC_PASSWORD_ENV", None)
    env.update(
        VNC_TIMEOUT="15", VNC_ACTION_TIMEOUT="20", VNC_IDLE_TIMEOUT="0",
        VNC_MAX_SESSIONS="4", VNC_SCREENSHOT_MODE=screenshot_mode, VNC_ENCODING="auto",
    )
    return env


async def _tool_call(session: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    import asyncio

    try:
        return await asyncio.wait_for(
            session.call_tool(name, arguments or {}), timeout=_TOOL_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        raise SoakError(f"MCP tool {name} exceeded {_TOOL_TIMEOUT_SECONDS:g} seconds") from exc


def _payload(result: Any, name: str) -> dict[str, Any]:
    if getattr(result, "isError", False):
        details = " ".join(
            str(item.text) for item in getattr(result, "content", [])
            if getattr(item, "type", None) == "text"
        ).strip()
        raise SoakError(f"MCP tool {name} returned an error" + (f": {details[:800]}" if details else ""))
    value = getattr(result, "structuredContent", None)
    if isinstance(value, dict):
        return value
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) == "text":
            try:
                value = json.loads(item.text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
    raise SoakError(f"MCP tool {name} returned no JSON object")


async def _connect(session: Any, target: Target) -> str:
    data = _payload(await _tool_call(session, "vnc_connect", {
        "host": target.host, "port": target.port, "session_id": target.name,
    }), "vnc_connect")
    connection_id = data.get("connection_id")
    if data.get("connected") is not True or not isinstance(connection_id, str) or not connection_id:
        raise SoakError(f"vnc_connect did not return a connected session for target {target.name!r}")
    target.connection_id = connection_id
    return connection_id


async def _capture(session: Any, target: Target) -> None:
    if target.connection_id is None:
        raise SoakError(f"target {target.name!r} has no active connection_id")
    started = time.monotonic()
    result = await _tool_call(session, "vnc_screenshot", {
        "image_format": "jpeg", "quality": 80, "session_id": target.name,
        "connection_id": target.connection_id,
    })
    elapsed = time.monotonic() - started
    metadata = _payload(result, "vnc_screenshot")
    images = [item for item in getattr(result, "content", []) if getattr(item, "type", None) == "image"]
    if len(images) != 1:
        raise SoakError(f"vnc_screenshot returned no metadata/image for target {target.name!r}")
    if metadata.get("session_id") != target.name or metadata.get("connection_id") != target.connection_id:
        raise SoakError(f"screenshot identity did not match target {target.name!r}")
    if getattr(images[0], "mimeType", None) not in {"image/jpeg", "image/png"}:
        raise SoakError(f"screenshot has an unsupported image type for target {target.name!r}")
    if not getattr(images[0], "data", "") or isinstance(metadata.get("bytes"), bool) or not isinstance(metadata.get("bytes"), int) or metadata["bytes"] <= 0:
        raise SoakError(f"screenshot was empty for target {target.name!r}")
    capture = metadata.get("capture_seconds")
    size = metadata.get("transport_bytes")
    if isinstance(capture, bool) or not isinstance(capture, (float, int)) or not math.isfinite(capture) or capture < 0:
        raise SoakError(f"screenshot capture timing was invalid for target {target.name!r}")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise SoakError(f"screenshot transport byte count was invalid for target {target.name!r}")
    target.record_screenshot(elapsed, float(capture), size)
    del result, images


async def _disconnect(session: Any, target: Target) -> None:
    if target.connection_id is None:
        return
    data = _payload(await _tool_call(session, "vnc_disconnect", {
        "session_id": target.name, "connection_id": target.connection_id,
    }), "vnc_disconnect")
    if data.get("connected") is not False:
        raise SoakError(f"vnc_disconnect did not clear target {target.name!r}")
    target.connection_id = None


async def _reconnect(session: Any, target: Target) -> None:
    old_id = target.connection_id
    if old_id is None:
        raise SoakError(f"cannot explicitly reconnect inactive target {target.name!r}")
    await _disconnect(session, target)
    if await _connect(session, target) == old_id:
        raise SoakError(f"reconnected target {target.name!r} reused its old connection_id")
    target.reconnect_count += 1
    await _capture(session, target)
    result = await _tool_call(session, "vnc_screenshot", {
        "image_format": "jpeg", "quality": 80, "session_id": target.name,
        "connection_id": old_id,
    })
    if not getattr(result, "isError", False):
        raise SoakError(f"old connection_id was accepted after reconnect for target {target.name!r}")
    target.stale_id_rejections += 1


def _progress(report: dict[str, Any]) -> None:
    targets = ", ".join(f"{item['name']}={item['screenshot_count']}" for item in report["targets"])
    print(
        f"soak progress: {report['duration_actual_seconds']:.0f}/{report['duration_target_seconds']:.0f}s, "
        f"rounds={report['rounds']}, screenshots[{targets}]", flush=True,
    )


async def _parallel_screenshots(
    session: Any, targets: list[Target], report: dict[str, Any], progress_due: float,
    live_progress: bool,
) -> float:
    import asyncio

    pending = {asyncio.create_task(_capture(session, target)) for target in targets}
    first_error: BaseException | None = None
    while pending:
        timeout = max(0.0, progress_due - time.monotonic()) if live_progress else None
        done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.ALL_COMPLETED)
        for task in done:
            try:
                task.result()
            except BaseException as exc:
                first_error = first_error or exc
        if pending and live_progress and time.monotonic() >= progress_due:
            _progress(report)
            progress_due = time.monotonic() + 30.0
    if first_error is not None:
        raise first_error
    return progress_due


async def _wait_interval(until: float, report: dict[str, Any], progress_due: float, live_progress: bool) -> float:
    import asyncio

    while (remaining := until - time.monotonic()) > 0:
        wait_for = min(remaining, max(0.0, progress_due - time.monotonic())) if live_progress else remaining
        await asyncio.sleep(wait_for)
        if live_progress and time.monotonic() >= progress_due:
            _progress(report)
            progress_due = time.monotonic() + 30.0
    return progress_due


async def _run_soak_body(
    session: Any, targets: list[Target], report: dict[str, Any], output: Path,
    duration: float, interval: float, reconnect_every: int, live_progress: bool,
) -> None:
    setup_started = time.monotonic()
    for target in targets:
        await _connect(session, target)
        await _capture(session, target)
        _refresh_targets(report, targets)
        _write_checkpoint(output, report)
    soak_started = time.monotonic()
    report["_soak_started"] = soak_started
    report["setup_seconds"] = soak_started - setup_started
    report["soak_started_at"] = datetime.now(timezone.utc).isoformat()
    _refresh_targets(report, targets)
    _write_checkpoint(output, report)

    progress_due, next_round = soak_started + 30.0, soak_started
    while time.monotonic() - soak_started < duration:
        if time.monotonic() < next_round:
            progress_due = await _wait_interval(next_round, report, progress_due, live_progress)
        round_number = report["rounds"] + 1
        if round_number % reconnect_every == 0:
            index = (round_number // reconnect_every - 1) % len(targets)
            await _reconnect(session, targets[index])
        progress_due = await _parallel_screenshots(session, targets, report, progress_due, live_progress)
        report["rounds"] = round_number
        report["duration_actual_seconds"] = time.monotonic() - soak_started
        report["total_runtime_seconds"] = time.monotonic() - report["_runtime_started"]
        _refresh_targets(report, targets)
        _write_checkpoint(output, report)
        next_round += interval
    report["duration_actual_seconds"] = time.monotonic() - soak_started
    report["total_runtime_seconds"] = time.monotonic() - report["_runtime_started"]


async def _cleanup(session: Any, targets: list[Target], report: dict[str, Any]) -> None:
    cleanup = report["cleanup"]
    for target in reversed(targets):
        if target.connection_id is None:
            continue
        cleanup["disconnect_attempts"] += 1
        try:
            await _disconnect(session, target)
        except BaseException as exc:
            message = f"disconnect {target.name!r}: {type(exc).__name__}: {str(exc)[:600]}"
            cleanup["errors"].append(message)
            _record_failure(report, SoakError(message))
    try:
        data = _payload(await _tool_call(session, "vnc_sessions"), "vnc_sessions")
        count = data.get("session_count")
        if isinstance(count, bool) or not isinstance(count, int):
            raise SoakError("vnc_sessions returned an invalid session_count")
        cleanup["inventory_session_count"] = count
        if count:
            names = [item.get("session_id") for item in data.get("sessions", []) if isinstance(item, dict)]
            message = f"VNC registry still has {count} session(s) after cleanup: {names}"
            cleanup["errors"].append(message)
            _record_failure(report, SoakError(message))
    except BaseException as exc:
        message = f"final inventory: {type(exc).__name__}: {str(exc)[:600]}"
        cleanup["errors"].append(message)
        _record_failure(report, SoakError(message))


async def _run_mcp(
    source: Path, output: Path, targets: list[Target], mode: str, duration: float,
    interval: float, reconnect_every: int, screenshot_mode: str, live_progress: bool,
    fixtures: list[Any] | None = None,
) -> dict[str, Any]:
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    report = _make_report(output, mode, duration, interval, screenshot_mode, reconnect_every, targets)
    report["_runtime_started"] = time.monotonic()
    if fixtures is not None:
        report["self_test_note"] = "Two local synthetic RFB fixtures; no real desktop endpoint or input events."
    initialized = False
    try:
        if fixtures is not None:
            _check_aggregate_summary()
        _write_checkpoint(output, report)
        params = StdioServerParameters(
            command=sys.executable, args=[str(source / "main.py"), "mcp"], cwd=str(source),
            env=_environment(screenshot_mode, mode == "live_read_only"),
        )
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_log:
            async with stdio_client(params, errlog=error_log) as streams:
                async with ClientSession(*streams) as session:
                    try:
                        await asyncio.wait_for(session.initialize(), timeout=_TOOL_TIMEOUT_SECONDS)
                        initialized = True
                        report["mcp_initialized"] = True
                        await _run_soak_body(
                            session, targets, report, output, duration, interval,
                            reconnect_every, live_progress,
                        )
                    except Exception as exc:
                        _record_failure(report, exc)
                    finally:
                        if "_soak_started" in report:
                            report["duration_actual_seconds"] = time.monotonic() - report["_soak_started"]
                        if initialized:
                            try:
                                await asyncio.shield(_cleanup(session, targets, report))
                            except BaseException as exc:
                                _record_failure(report, exc)
    except BaseException as exc:
        _record_failure(report, exc)
    finally:
        if fixtures is not None:
            fixture_reports = []
            for index, fixture in enumerate(fixtures, start=1):
                try:
                    fixture.close()
                except BaseException as exc:
                    fixture.errors.append(f"{type(exc).__name__}: {exc}")
                errors, event_count = list(fixture.errors), len(fixture.events)
                fixture_reports.append({
                    "name": f"synthetic-{index}", "port": fixture.port,
                    "errors": errors, "input_event_count": event_count,
                })
                if errors:
                    _record_failure(report, SoakError(f"synthetic RFB fixture error: {errors[0]}"))
                if event_count:
                    _record_failure(report, SoakError("synthetic fixture observed input; soak must be read-only"))
            report["synthetic_fixtures"] = fixture_reports
        report.pop("_soak_started", None)
        report["total_runtime_seconds"] = time.monotonic() - report.pop("_runtime_started")
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["success"] = report.get("failure") is None and not report["cleanup"]["errors"]
        _refresh_targets(report, targets)
        _write_checkpoint(output, report)
    return report


def _synthetic_targets() -> tuple[list[Target], list[Any]]:
    checks_dir = str(Path(__file__).resolve().parent)
    if checks_dir not in sys.path:
        sys.path.insert(0, checks_dir)
    from vnc_regression import Fixture

    fixtures = []
    try:
        for index, color in enumerate((170, 220), start=1):
            fixtures.append(Fixture(f"Synthetic read-only soak {index}", color))
    except BaseException:
        for fixture in fixtures:
            fixture.close()
        raise
    targets = [Target(f"synthetic-{index}", "127.0.0.1", fixture.port)
               for index, fixture in enumerate(fixtures, start=1)]
    return targets, fixtures


async def _run_requested(args: argparse.Namespace, targets: list[Target]) -> dict[str, Any]:
    if args.self_test:
        targets, fixtures = _synthetic_targets()
        return await _run_mcp(
            args.source, args.output, targets, "synthetic_self_test", 0.6, 0.1, 1,
            args.screenshot_mode, False, fixtures,
        )
    return await _run_mcp(
        args.source, args.output, targets, "live_read_only", args.duration,
        args.interval, 50, args.screenshot_mode, True,
    )


def _check_aggregate_summary() -> None:
    probe = Target("probe", "127.0.0.1", 5900)
    probe.record_screenshot(0.25, 0.75, 6)
    probe.record_screenshot(0.5, 0.25, 3)
    expected = {
        "screenshot_count": 2,
        "screenshot_client_seconds": {"count": 2, "min": 0.25, "average": 0.375, "max": 0.5, "total": 0.75},
        "screenshot_capture_seconds": {"count": 2, "min": 0.25, "average": 0.5, "max": 0.75, "total": 1.0},
        "screenshot_transport_bytes": {"count": 2, "min": 3, "average": 4.5, "max": 6, "total": 9},
    }
    actual = probe.summary()
    if any(actual[key] != value for key, value in expected.items()):
        raise SoakError("streaming screenshot aggregate summary did not match sample values")


def _positive_finite(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="repository root containing main.py")
    parser.add_argument("--targets", nargs="+", default=[], metavar="NAME=HOST:PORT",
                        help="one to four named VNC endpoints; live targets require --allow-live")
    parser.add_argument("--duration", type=_positive_finite, default=1800.0,
                        help="soak duration in seconds (default: 1800)")
    parser.add_argument("--interval", type=_positive_finite, default=2.0,
                        help="seconds between concurrent screenshot rounds (default: 2)")
    parser.add_argument("--output", type=Path, required=True, help="checkpoint and final JSON report path")
    parser.add_argument("--screenshot-mode", choices=("auto", "fresh", "refresh"), default="auto")
    parser.add_argument("--allow-live", action="store_true", help="authorize connections to the supplied endpoints")
    parser.add_argument("--self-test", action="store_true", help="use synthetic loopback RFB fixtures only")
    return parser


def _validate_args(args: argparse.Namespace) -> list[Target]:
    args.source = args.source.expanduser().resolve()
    if not (args.source / "main.py").is_file():
        raise ValueError(f"source root must contain main.py: {args.source}")
    args.output = args.output.expanduser().resolve()
    if not 0.1 <= args.interval <= 3600:
        raise ValueError("interval must be between 0.1 and 3600 seconds")
    if args.duration > 7 * 24 * 60 * 60:
        raise ValueError("duration must not exceed seven days")
    if args.self_test:
        if args.allow_live or args.targets:
            raise ValueError("--self-test cannot be combined with --allow-live or --targets")
        return []
    if not args.allow_live:
        raise ValueError("refusing live connections without explicit --allow-live")
    return parse_targets(args.targets)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        targets = _validate_args(args)
    except Exception as exc:
        report = _make_report(
            output, "synthetic_self_test" if args.self_test else "live_read_only",
            args.duration, args.interval, args.screenshot_mode, 1 if args.self_test else 50, [],
        )
        _record_failure(report, exc)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _write_checkpoint(output, report)
        except OSError as write_error:
            print(f"Could not write report: {write_error}", file=sys.stderr)
        print(f"Refused to start: {exc}", file=sys.stderr)
        return 2

    try:
        report = __import__("asyncio").run(_run_requested(args, targets))
    except BaseException as exc:
        report = _make_report(
            args.output, "synthetic_self_test" if args.self_test else "live_read_only",
            0.6 if args.self_test else args.duration, 0.1 if args.self_test else args.interval,
            args.screenshot_mode, 1 if args.self_test else 50, targets,
        )
        _record_failure(report, exc)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _refresh_targets(report, targets)
        _write_checkpoint(args.output, report)

    print(json.dumps({
        "success": report["success"], "mode": report["mode"],
        "duration_actual_seconds": report["duration_actual_seconds"],
        "duration_target_seconds": report["duration_target_seconds"],
        "rounds": report["rounds"], "failure": report["failure"],
        "report": str(args.output),
    }, ensure_ascii=False), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
