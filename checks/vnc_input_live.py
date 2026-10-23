"""Explicit live acceptance for the Windows/Linux input fixtures.

This driver is intentionally absent from the default checks. It starts this
repository's stdio MCP server, sends real desktop input, and independently
reads each fixture's JSON result through the profile-bound official vmrun.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

LIVE_STEPS = (
    "connect_initial_png",
    "fixture_identity_and_controls",
    "move_event",
    "crop_png_metadata_and_remember",
    "wait_matches",
    "button_single_click_and_canvas_change",
    "wait_changed",
    "button_double_click_count",
    "wait_stable",
    "wait_timeout",
    "canvas_drag_events_and_coordinates",
    "wheel_event_and_scroll",
    "text_clear_before_keys",
    "ascii_keys_readback",
    "text_clear_before_ascii",
    "ascii_auto_readback",
    "ascii_backspace_readback",
    "ascii_four_readback",
    "text_clear_before_unicode",
    "unicode_auto_readback",
    "text_clear_before_shift_rejection",
    "held_shift_refusal",
    "held_shift_no_replay",
)

MULTI_STEPS = (
    "cross_session_connection_id_rejected",
    "five_read_only_screenshot_batches",
    "fifth_session_capacity_rejected",
    "disconnect_reconnect_rejects_old_id",
    "other_session_ids_preserved",
    "all_sessions_disconnected",
)

REQUIRED_TOOLS = {
    "vnc_connect", "vnc_screenshot", "vnc_click", "vnc_move", "vnc_drag",
    "vnc_scroll", "vnc_keypress", "vnc_key_down", "vnc_key_up", "vnc_type",
    "vnc_wait_for", "vnc_disconnect", "vnc_sessions",
}


class AcceptanceError(RuntimeError):
    """A live check could not establish its required evidence."""


@dataclass(frozen=True)
class TargetSpec:
    name: str
    host: str
    port: int
    profile_name: str
    result_path: str
    token: str


@dataclass
class TargetRun:
    spec: TargetSpec
    profile: GuestProfile
    result: dict[str, Any]
    reader: "VmrunResultReader"
    connection_id: str | None = None
    initial_connection_id: str | None = None
    desktop_width: int | None = None
    desktop_height: int | None = None
    fixture: dict[str, Any] | None = None
    ready: bool = False
    held_keys: set[str] | None = None
    temporary_directory: Any = None

    def __post_init__(self) -> None:
        if self.held_keys is None:
            self.held_keys = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_record() -> dict[str, Any]:
    return {"status": "not_executed"}


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "target"


def _strict_int(value: Any) -> bool:
    return type(value) is int


def _read_manifest(path: Path, profiles: dict[str, GuestProfile]) -> list[TargetSpec]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"targets"} or not isinstance(data["targets"], list):
        raise ValueError("manifest must contain only a targets array")
    if len(data["targets"]) != 4:
        raise ValueError("this acceptance requires exactly four targets for the four-session limit check")

    required = {"name", "host", "port", "profile", "result_path", "token"}
    specs: list[TargetSpec] = []
    names: set[str] = set()
    tokens: set[str] = set()
    profile_names: set[str] = set()
    endpoints: set[tuple[str, int]] = set()
    vmx_paths: set[str] = set()
    for index, item in enumerate(data["targets"]):
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("each target must contain name, host, port, profile, result_path and token")
        name, host, profile_name, result_path, token = (
            item["name"], item["host"], item["profile"], item["result_path"], item["token"]
        )
        port = item["port"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("target names must be valid 1-64 character MCP session IDs")
        if not isinstance(host, str) or not host or any(ch in host for ch in "\0\r\n"):
            raise ValueError("target host must be a nonempty single-line string")
        if not _strict_int(port) or not 1 <= port <= 65535:
            raise ValueError("target port must be an integer from 1 through 65535")
        if not isinstance(profile_name, str) or profile_name not in profiles:
            raise ValueError("each target profile must name a profile from --profiles-file")
        if not isinstance(result_path, str) or not result_path or any(ch in result_path for ch in "\0\r\n"):
            raise ValueError("result_path must be a nonempty single-line guest path")
        if not isinstance(token, str) or not 1 <= len(token) <= 256 or any(ch in token for ch in "\0\r\n"):
            raise ValueError("token must be a 1-256 character single-line string")
        if name in names or token in tokens:
            raise ValueError("target names and tokens must be unique")

        profile = profiles[profile_name]
        if not profile.matches(host, port):
            raise ValueError("target host/port must match its named guest profile")
        endpoint = (host.casefold(), port)
        vmx_path = os.path.normcase(os.path.abspath(profile.vmx))
        if profile_name in profile_names or endpoint in endpoints or vmx_path in vmx_paths:
            raise ValueError("the four targets must refer to four distinct profiles, endpoints and VMX files")
        path_module = ntpath if profile.guest_os == "windows" else posixpath
        if not path_module.isabs(result_path):
            raise ValueError("result_path must be absolute in the guest")

        specs.append(TargetSpec(name, host, port, profile_name, result_path, token))
        names.add(name)
        tokens.add(token)
        profile_names.add(profile_name)
        endpoints.add(endpoint)
        vmx_paths.add(vmx_path)
    return specs


class VmrunResultReader:
    """Read one explicitly named guest result using the matching profile."""

    def __init__(self, profile: GuestProfile, result_path: str, destination: Path, secrets: list[str]) -> None:
        from vmware_vnc.diagnostics import get_diagnostics

        self.profile = profile
        self.result_path = (ntpath if profile.guest_os == "windows" else posixpath).normpath(result_path)
        self.destination = destination
        self.secrets = secrets
        for variable in (profile.username_env, profile.password_env, profile.vm_password_env):
            if variable is not None and variable not in os.environ:
                raise ValueError("required credential environment variable is not set: " + variable)
        self.username = os.environ[profile.username_env]
        self.password = os.environ[profile.password_env]
        self.vm_password = os.environ.get(profile.vm_password_env, "") if profile.vm_password_env else ""
        executable = profile.vmrun or get_diagnostics()["vmrun"].get("path")
        if not executable or not Path(executable).is_file():
            raise ValueError("official vmrun executable is unavailable for profile " + profile.name)
        self.executable = str(executable)

    def copy_json(self) -> dict[str, Any]:
        if self.destination.exists():
            self.destination.unlink()
        command = "copyFileFromGuestToHost"
        argv = [self.executable, "-T", "ws"]
        if self.vm_password:
            argv += ["-vp", self.vm_password]
        argv += ["-gu", self.username, "-gp", self.password, command,
                 self.profile.vmx, self.result_path, str(self.destination)]
        try:
            child = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3.0,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise AcceptanceError("vmrun copyFileFromGuestToHost timed out") from None
        except OSError as exc:
            raise AcceptanceError(
                "vmrun copyFileFromGuestToHost could not start (" + type(exc).__name__ + ")"
            ) from None
        if child.returncode != 0:
            raise AcceptanceError(
                "vmrun copyFileFromGuestToHost failed with exit code {}".format(child.returncode)
            )
        if not self.destination.is_file():
            raise AcceptanceError("vmrun copyFileFromGuestToHost produced no local result file")
        if self.destination.stat().st_size > 1024 * 1024:
            raise AcceptanceError("fixture result exceeds the 1 MiB read limit")
        try:
            value = json.loads(self.destination.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError("fixture result is not complete UTF-8 JSON: " + str(exc)) from None
        if not isinstance(value, dict):
            raise AcceptanceError("fixture result must be a JSON object")
        return value

class LiveInputDriver:
    def __init__(self, args: argparse.Namespace, specs: list[TargetSpec], profiles: dict[str, GuestProfile]) -> None:
        self.args = args
        self.output = args.output.resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "test": "vmware-vnc-input-live",
            "started_at": _now(),
            "allow_live": True,
            "output": str(self.output),
            "status": "incomplete",
            "targets": [],
            "multi_session": {"steps": {name: _step_record() for name in MULTI_STEPS}},
            "passed": [],
            "failed": [],
            "not_executed": [],
            "cleanup_errors": [],
        }
        self.output_temporary = self.output.with_name(self.output.name + ".tmp")
        self.secrets = self._find_secrets(specs, profiles)
        self.states: list[TargetRun] = []
        self.extra_sessions: list[tuple[str, str, TargetRun]] = []
        self.session: Any = None
        self.stderr_capture: Any = None

        try:
            for spec in specs:
                profile = profiles[spec.profile_name]
                result = {
                    "name": spec.name,
                    "profile": spec.profile_name,
                    "guest_os": profile.guest_os,
                    "status": "not_executed",
                    "steps": {name: _step_record() for name in LIVE_STEPS},
                    "screenshots": [],
                }
                temp_dir = tempfile.TemporaryDirectory(prefix="vmware-vnc-input-", dir=self.output.parent)
                destination = Path(temp_dir.name) / "fixture-result.json"
                try:
                    reader = VmrunResultReader(profile, spec.result_path, destination, self.secrets)
                except Exception:
                    temp_dir.cleanup()
                    raise
                self.states.append(TargetRun(spec, profile, result, reader, temporary_directory=temp_dir))
                self.report["targets"].append(result)
        except BaseException:
            for state in self.states:
                if state.temporary_directory is not None:
                    state.temporary_directory.cleanup()
            raise

    @staticmethod
    def _find_secrets(specs: list[TargetSpec], profiles: dict[str, GuestProfile]) -> list[str]:
        names: set[str] = {"VNC_PASSWORD"}
        password_env = os.environ.get("VNC_PASSWORD_ENV")
        if password_env:
            names.add(password_env)
        for spec in specs:
            profile = profiles[spec.profile_name]
            names.update(value for value in (profile.username_env, profile.password_env, profile.vm_password_env) if value)
        return sorted({os.environ[name] for name in names if os.environ.get(name)}, key=len, reverse=True)

    def _redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "<redacted>")
        return value

    def _exception_text(self, exc: BaseException) -> str:
        return self._redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))).rstrip()

    def _close_stderr_capture(self) -> None:
        capture = self.stderr_capture
        self.stderr_capture = None
        if capture is None:
            return
        contents = ""
        try:
            capture.flush()
            capture.seek(0)
            contents = capture.read()
        except Exception as exc:
            self.report["cleanup_errors"].append({
                "operation": "read MCP stderr capture",
                "error": self._exception_text(exc),
            })
        finally:
            try:
                capture.close()
            except Exception as exc:
                self.report["cleanup_errors"].append({
                    "operation": "close MCP stderr capture",
                    "error": self._exception_text(exc),
                })

        if not self.report.get("driver_error") or not contents:
            return
        markers = re.compile(
            r"traceback|\b(error|exception|failed|failure|fatal)\b|(?:Error|Exception):",
            re.IGNORECASE,
        )
        error_lines = [self._redact(line).strip() for line in contents.splitlines() if markers.search(line)]
        error_lines = [line[-800:] for line in error_lines[-20:] if line]
        if error_lines:
            self.report["mcp_stderr_errors"] = error_lines

    def write_report(self) -> None:
        data = json.dumps(self.report, ensure_ascii=False, indent=2)
        self.output_temporary.write_text(data, encoding="utf-8", newline="\n")
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(self.output_temporary, self.output)
                return
            except PermissionError as exc:
                # Windows readers may briefly deny delete sharing. Retry only
                # this report rename; never repeat the desktop operation.
                if (os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33)
                        or time.monotonic() >= deadline):
                    raise
                time.sleep(0.02)

    def _step(self, scope: dict[str, Any], name: str) -> dict[str, Any]:
        return scope["steps"][name]

    async def target_step(self, state: TargetRun, name: str, operation: Callable[[], Any]) -> Any:
        step = self._step(state.result, name)
        step.clear()
        step["status"] = "running"
        self.write_report()
        try:
            evidence = await operation()
        except BaseException as exc:
            step.clear()
            step.update(status="failed", error=self._exception_text(exc))
            state.result["status"] = "failed"
            self.write_report()
            raise
        step.clear()
        step.update(status="passed", evidence=evidence or {})
        self.write_report()
        return evidence

    async def multi_step(self, name: str, operation: Callable[[], Any]) -> Any:
        scope = self.report["multi_session"]
        step = self._step(scope, name)
        step.clear()
        step["status"] = "running"
        self.write_report()
        try:
            evidence = await operation()
        except BaseException as exc:
            step.clear()
            step.update(status="failed", error=self._exception_text(exc))
            self.write_report()
            raise
        step.clear()
        step.update(status="passed", evidence=evidence or {})
        self.write_report()
        return evidence

    @staticmethod
    def _tool_error(result: Any) -> str:
        return " ".join(
            str(item.text) for item in getattr(result, "content", [])
            if getattr(item, "type", None) == "text"
        ).strip() or "MCP tool returned an error without text"

    @classmethod
    def _payload(cls, result: Any, name: str) -> dict[str, Any]:
        if getattr(result, "isError", False):
            raise AcceptanceError("MCP tool {} failed: {}".format(name, cls._tool_error(result)))
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        for item in getattr(result, "content", []):
            if getattr(item, "type", None) == "text":
                try:
                    value = json.loads(item.text)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    return value
        raise AcceptanceError("MCP tool {} returned no JSON object".format(name))

    async def _raw_call(self, name: str, arguments: dict[str, Any], state: TargetRun | None = None) -> Any:
        if self.session is None:
            raise AcceptanceError("stdio MCP session is not initialized")
        args = dict(arguments)
        if state is not None:
            if not state.connection_id:
                raise AcceptanceError("named action has no active connection_id")
            args["session_id"] = state.spec.name
            args["connection_id"] = state.connection_id
        try:
            return await asyncio.wait_for(self.session.call_tool(name, args), timeout=65.0)
        except asyncio.TimeoutError:
            raise AcceptanceError("MCP tool {} exceeded the action timeout".format(name)) from None
        except Exception as exc:
            raise AcceptanceError(
                "MCP tool {} call failed ({}): {}".format(name, type(exc).__name__, str(exc))
            ) from None

    async def _call(self, name: str, arguments: dict[str, Any], state: TargetRun | None = None) -> dict[str, Any]:
        return self._payload(await self._raw_call(name, arguments, state), name)

    def _child_environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("VNC_HOST", None)
        env.pop("VNC_PORT", None)
        env.update(
            VNC_PROFILES_FILE=str(self.args.profiles_file.resolve()),
            VNC_SCREENSHOT_MODE="fresh",
            VNC_TIMEOUT="10",
            VNC_ACTION_TIMEOUT="60",
            VNC_IDLE_TIMEOUT="0",
            VNC_MAX_SESSIONS="4",
        )
        return env

    async def connect_and_capture(self, state: TargetRun) -> dict[str, Any]:
        data = await self._call("vnc_connect", {
            "host": state.spec.host,
            "port": state.spec.port,
            "profile": state.spec.profile_name,
            "session_id": state.spec.name,
        })
        connection_id = data.get("connection_id")
        if isinstance(connection_id, str) and connection_id:
            state.connection_id = connection_id
            state.initial_connection_id = connection_id
        if data.get("connected") is not True or not state.connection_id:
            raise AcceptanceError("vnc_connect did not return an active named connection")

        # The first operation after connect is always a full PNG screenshot.
        metadata, image_size = await self.capture_image(state, label="initial-full", save=True)
        state.desktop_width = metadata["desktop_width"]
        state.desktop_height = metadata["desktop_height"]
        return {
            "session_id": state.spec.name,
            "connected": True,
            "desktop_size": [state.desktop_width, state.desktop_height],
            "png_size": list(image_size),
            "screenshot": self._safe_metadata(metadata),
        }

    @staticmethod
    def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        allowed = (
            "session_id", "connection_id", "desktop_width", "desktop_height",
            "origin_x", "origin_y", "width", "height", "format", "bytes",
            "capture_seconds", "transport_bytes", "wire_encoding", "reference_id",
        )
        return {key: metadata[key] for key in allowed if key in metadata}

    async def capture_image(
        self,
        state: TargetRun,
        label: str,
        region: dict[str, int] | None = None,
        remember: bool = False,
        save: bool = False,
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        arguments: dict[str, Any] = {"image_format": "png", "remember": remember}
        if region is not None:
            arguments["region"] = region
        result = await self._raw_call("vnc_screenshot", arguments, state)
        metadata = self._payload(result, "vnc_screenshot")
        images = [item for item in getattr(result, "content", []) if getattr(item, "type", None) == "image"]
        if len(images) != 1 or getattr(images[0], "mimeType", None) != "image/png":
            raise AcceptanceError("vnc_screenshot did not return exactly one PNG image")
        if metadata.get("session_id") != state.spec.name or metadata.get("connection_id") != state.connection_id:
            raise AcceptanceError("screenshot metadata did not match both named session IDs")
        encoded = getattr(images[0], "data", "")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise AcceptanceError("screenshot image data was not valid base64: " + str(exc)) from None

        try:
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                image_size = image.size
                image_format = image.format
        except Exception as exc:
            raise AcceptanceError("screenshot PNG could not be decoded: {}: {}".format(type(exc).__name__, exc)) from None
        if image_format != "PNG":
            raise AcceptanceError("screenshot bytes were not encoded as PNG")

        for key in ("desktop_width", "desktop_height", "origin_x", "origin_y", "width", "height", "bytes"):
            if not _strict_int(metadata.get(key)):
                raise AcceptanceError("screenshot metadata has an invalid {} field".format(key))
        if image_size != (metadata["width"], metadata["height"]):
            raise AcceptanceError("decoded PNG size did not match screenshot metadata")
        if metadata["bytes"] != len(image_bytes):
            raise AcceptanceError("screenshot byte count did not match PNG data")
        if region is None:
            expected = (metadata["desktop_width"], metadata["desktop_height"])
            if (metadata["origin_x"], metadata["origin_y"], metadata["width"], metadata["height"]) != (
                0, 0, expected[0], expected[1]
            ):
                raise AcceptanceError("full screenshot metadata did not describe the complete native desktop")
        elif (metadata["origin_x"], metadata["origin_y"], metadata["width"], metadata["height"]) != (
            region["x"], region["y"], region["width"], region["height"]
        ):
            raise AcceptanceError("crop metadata did not match the requested native rectangle")

        file_path = None
        if save:
            file_path_obj = self.output.with_name(
                "{}-{}-{}.png".format(self.output.stem, _safe_filename(state.spec.name), _safe_filename(label))
            )
            file_path_obj.write_bytes(image_bytes)
            file_path = str(file_path_obj)
        if save or remember:
            item = {"label": label, **self._safe_metadata(metadata)}
            if file_path is not None:
                item["path"] = file_path
            state.result["screenshots"].append(item)
            self.write_report()
        if remember and not isinstance(metadata.get("reference_id"), str):
            raise AcceptanceError("remember=true did not return a visual reference_id")
        return metadata, image_size

    def _validate_fixture(self, state: TargetRun, report: dict[str, Any]) -> dict[str, Any]:
        if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            raise AcceptanceError("fixture result schema_version was not 1")
        if report.get("token") != state.spec.token:
            raise AcceptanceError("fixture result token did not match the manifest")
        if not _strict_int(report.get("process_id")) or report["process_id"] <= 0:
            raise AcceptanceError("fixture result process_id was invalid")
        if report.get("text") != "" or report.get("button_clicks") != 0 or report.get("color") != "green":
            raise AcceptanceError("fixture did not start with empty text, green canvas and zero button clicks")
        counts = report.get("counts")
        required_counts = {"mouse_down", "mouse_up", "mouse_move", "wheel", "key_down", "key_up"}
        if not isinstance(counts, dict) or set(counts) != required_counts or any(
            not _strict_int(counts.get(key)) or counts[key] < 0 for key in required_counts
        ):
            raise AcceptanceError("fixture input event counters were invalid")
        if not _strict_int(report.get("button_clicks")) or report["button_clicks"] < 0:
            raise AcceptanceError("fixture button_clicks was invalid")
        if report.get("color") not in {"green", "red"}:
            raise AcceptanceError("fixture color must be green or red")
        if not _strict_int(report.get("wheel_delta")):
            raise AcceptanceError("fixture wheel_delta was invalid")
        drag = report.get("drag")
        if drag is not None:
            if not isinstance(drag, dict) or set(drag) != {"start", "end"}:
                raise AcceptanceError("fixture drag must be null or contain start and end")
            for point_name in ("start", "end"):
                point = drag[point_name]
                if point is not None and (
                    not isinstance(point, dict) or set(point) != {"x", "y"}
                    or not _strict_int(point.get("x")) or not _strict_int(point.get("y"))
                ):
                    raise AcceptanceError(
                        "fixture drag {} must be native integer coordinates or null".format(point_name)
                    )
        controls = report.get("controls")
        if not isinstance(controls, dict) or set(controls) != {"text", "button", "canvas", "wheel"}:
            raise AcceptanceError("fixture controls object did not have the four required controls")
        if state.desktop_width is None or state.desktop_height is None:
            raise AcceptanceError("desktop dimensions are unavailable for control validation")
        clean_controls: dict[str, dict[str, int]] = {}
        for name, rect in controls.items():
            if not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}:
                raise AcceptanceError("fixture {} bounds were invalid".format(name))
            if any(not _strict_int(rect[key]) for key in ("x", "y", "width", "height")):
                raise AcceptanceError("fixture {} bounds must use native integer coordinates".format(name))
            if (rect["x"] < 0 or rect["y"] < 0 or rect["width"] <= 0 or rect["height"] <= 0
                    or rect["x"] + rect["width"] > state.desktop_width
                    or rect["y"] + rect["height"] > state.desktop_height):
                raise AcceptanceError("fixture {} bounds were outside the native desktop".format(name))
            clean_controls[name] = dict(rect)
        if clean_controls["canvas"]["width"] < 80 or clean_controls["canvas"]["height"] < 60:
            raise AcceptanceError("canvas bounds are too small for visual and drag acceptance")
        if clean_controls["wheel"]["height"] < 100:
            raise AcceptanceError("wheel area is too small for scroll acceptance")
        return clean_controls

    @staticmethod
    def _center(rect: dict[str, int]) -> tuple[int, int]:
        return rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2

    async def read_fixture(self, state: TargetRun) -> dict[str, Any]:
        value = await asyncio.to_thread(state.reader.copy_json)
        if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
            raise AcceptanceError("fixture result schema_version was not 1")
        if value.get("token") != state.spec.token:
            raise AcceptanceError("fixture result token did not match the manifest")
        return value

    async def wait_fixture(
        self,
        state: TargetRun,
        predicate: Callable[[dict[str, Any]], bool],
        description: str,
        timeout: float = 2.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        last_state: dict[str, Any] | None = None
        while True:
            try:
                snapshot = await asyncio.to_thread(state.reader.copy_json)
                if type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1:
                    last_error = "schema_version is not 1"
                elif snapshot.get("token") != state.spec.token:
                    last_error = "token does not match manifest"
                else:
                    last_state = snapshot
                    if predicate(snapshot):
                        state.fixture = snapshot
                        return snapshot
            except Exception as exc:
                last_error = "{}: {}".format(type(exc).__name__, exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                summary = None
                if last_state is not None:
                    summary = {
                        "text_matches": last_state.get("text") is not None,
                        "color": last_state.get("color"),
                        "button_clicks": last_state.get("button_clicks"),
                        "counts": last_state.get("counts"),
                    }
                raise AcceptanceError(
                    "timed out after {:.1f}s waiting for {}; last_state={}; last_read_error={}".format(
                        timeout, description, json.dumps(summary, ensure_ascii=False), last_error
                    )
                )
            await asyncio.sleep(min(0.2, remaining))

    async def _wait_visual(
        self,
        state: TargetRun,
        condition: str,
        region: dict[str, int],
        reference_id: str | None,
        timeout: float,
        stable_for: float = 0.3,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "condition": condition,
            "region": region,
            "timeout": timeout,
            "interval": 0.15,
            "stable_for": stable_for,
            "pixel_threshold": 8,
            "max_changed_ratio": 0.001,
            "image_format": "png",
        }
        if reference_id is not None:
            args["reference_id"] = reference_id
        result = await self._raw_call("vnc_wait_for", args, state)
        metadata = self._payload(result, "vnc_wait_for")
        if metadata.get("session_id") != state.spec.name or metadata.get("connection_id") != state.connection_id:
            raise AcceptanceError("visual wait metadata did not match both named session IDs")
        if (metadata.get("width"), metadata.get("height"), metadata.get("origin_x"), metadata.get("origin_y")) != (
            region["width"], region["height"], region["x"], region["y"]
        ):
            raise AcceptanceError("visual wait did not sample the exact canvas interior")
        if len([item for item in result.content if getattr(item, "type", None) == "image"]) != 1:
            raise AcceptanceError("visual wait returned no final image")
        return metadata

    async def connect_initial_targets(self) -> None:
        for state in self.states:
            try:
                await self.target_step(
                    state, "connect_initial_png", lambda state=state: self.connect_and_capture(state)
                )
                async def validate_initial(state: TargetRun = state) -> dict[str, Any]:
                    report = await self.wait_fixture(
                        state,
                        lambda item: item.get("schema_version") == 1 and item.get("token") == state.spec.token,
                        "initial fixture result",
                    )
                    controls = self._validate_fixture(state, report)
                    state.result["controls_initial"] = controls
                    state.fixture = report
                    state.ready = True
                    return {
                        "schema_version": report["schema_version"],
                        "token_matches": True,
                        "text_initially_empty": report["text"] == "",
                        "color_initial": report["color"],
                        "controls": controls,
                    }

                await self.target_step(state, "fixture_identity_and_controls", validate_initial)
                state.result["status"] = "ready"
                self.write_report()
            except BaseException:
                state.ready = False

    async def _move_pointer_to_text(self, state: TargetRun, controls: dict[str, dict[str, int]]) -> tuple[int, int]:
        rect = controls["text"]
        point = (rect["x"] + min(90, rect["width"] - 5), rect["y"] + min(90, rect["height"] - 5))
        await self._call("vnc_move", {"x": point[0], "y": point[1]}, state)
        return point

    async def _clear_text(self, state: TargetRun, controls: dict[str, dict[str, int]]) -> dict[str, Any]:
        before = await self.read_fixture(state)
        x, y = self._center(controls["text"])
        await self._call("vnc_click", {"x": x, "y": y, "button": "left", "count": 1}, state)
        await self._call("vnc_keypress", {"key": "ctrl-a"}, state)
        await self._call("vnc_keypress", {"key": "backspace"}, state)
        after = await self.wait_fixture(
            state,
            lambda value: value.get("text") == ""
            and value["counts"]["key_down"] > before["counts"]["key_down"]
            and value["counts"]["key_up"] > before["counts"]["key_up"],
            "main text box cleared by observed key-down/up events",
        )
        return after

    async def test_move(self, state: TargetRun, controls: dict[str, dict[str, int]]) -> dict[str, Any]:
        before = await self.read_fixture(state)
        previous = before["counts"]["mouse_move"]
        point = await self._move_pointer_to_text(state, controls)
        # The cursor can already occupy the first point after a fixture restart.
        # A planned second point verifies movement without replaying failed input.
        second = (point[0] + 25, point[1] + 15)
        await self._call("vnc_move", {"x": second[0], "y": second[1]}, state)
        after = await self.wait_fixture(
            state, lambda value: value["counts"]["mouse_move"] > previous, "mouse move event"
        )
        return {"points": [list(point), list(second)], "mouse_move_before": previous, "mouse_move_after": after["counts"]["mouse_move"]}

    async def test_crop_and_remember(
        self, state: TargetRun, controls: dict[str, dict[str, int]]
    ) -> dict[str, Any]:
        canvas = controls["canvas"]
        region = {
            "x": canvas["x"] + 4,
            "y": canvas["y"] + 4,
            "width": canvas["width"] - 8,
            "height": canvas["height"] - 8,
        }
        metadata, size = await self.capture_image(
            state, label="canvas-interior-green", region=region, remember=True, save=True
        )
        if size != (region["width"], region["height"]):
            raise AcceptanceError("PNG crop dimensions did not match the requested canvas interior")
        return {"region": region, "png_size": list(size), "screenshot": self._safe_metadata(metadata)}

    async def test_single_click(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        button = before["controls"]["button"]
        x, y = self._center(button)
        color_before = before["color"]
        clicks_before = before["button_clicks"]
        await self._call("vnc_click", {"x": x, "y": y, "button": "left", "count": 1}, state)
        after = await self.wait_fixture(
            state,
            lambda value: value.get("button_clicks") == clicks_before + 1
            and value.get("color") != color_before,
            "one button click and canvas color toggle",
        )
        state.fixture = after
        return {"button_clicks_before": clicks_before, "button_clicks_after": after["button_clicks"],
                "color_before": color_before, "color_after": after["color"]}

    async def test_double_click(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        x, y = self._center(before["controls"]["button"])
        count_before = before["button_clicks"]
        color_before = before["color"]
        await self._call("vnc_click", {"x": x, "y": y, "button": "left", "count": 2}, state)
        after = await self.wait_fixture(
            state,
            lambda value: value.get("button_clicks") == count_before + 2
            and value.get("color") == color_before,
            "two button click events from a double click",
        )
        state.fixture = after
        return {"button_clicks_before": count_before, "button_clicks_after": after["button_clicks"],
                "color_after": after["color"]}

    async def test_drag(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        rect = before["controls"]["canvas"]
        start = (rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2)
        end = (min(state.desktop_width - 2, start[0] + 24), min(state.desktop_height - 2, start[1] + 18))
        if end == start:
            raise AcceptanceError("desktop leaves no room to drag the canvas")
        counts_before = dict(before["counts"])
        await self._call("vnc_drag", {
            "x1": start[0], "y1": start[1], "x2": end[0], "y2": end[1], "button": "left",
        }, state)

        def completed(value: dict[str, Any]) -> bool:
            drag = value.get("drag")
            if not isinstance(drag, dict) or not isinstance(drag.get("start"), dict) or not isinstance(drag.get("end"), dict):
                return False
            moved = value.get("controls", {}).get("canvas", {})
            return (
                value.get("counts", {}).get("mouse_down", 0) > counts_before["mouse_down"]
                and value.get("counts", {}).get("mouse_up", 0) > counts_before["mouse_up"]
                and value.get("counts", {}).get("mouse_move", 0) > counts_before["mouse_move"]
                and (moved.get("x") != rect["x"] or moved.get("y") != rect["y"])
            )

        after = await self.wait_fixture(state, completed, "canvas drag start/end, input counters and moved bounds")
        drag = after["drag"]
        if drag["start"] != {"x": start[0], "y": start[1]}:
            raise AcceptanceError("fixture drag start did not match the native start coordinate")
        if abs(drag["end"].get("x", 0) - end[0]) > 12 or abs(drag["end"].get("y", 0) - end[1]) > 12:
            raise AcceptanceError("fixture drag end was not near the native release coordinate")
        state.fixture = after
        return {"requested_start": list(start), "requested_end": list(end), "drag": drag,
                "counts_before": counts_before, "counts_after": after["counts"],
                "canvas_after": after["controls"]["canvas"]}

    async def test_wheel(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        x, y = self._center(before["controls"]["wheel"])
        count_before = before["counts"]["wheel"]
        delta_before = before["wheel_delta"]
        await self._call("vnc_scroll", {"x": x, "y": y, "direction": "down", "amount": 2}, state)
        after = await self.wait_fixture(
            state,
            lambda value: value.get("counts", {}).get("wheel", 0) > count_before
            and value.get("wheel_delta") != delta_before,
            "wheel event and nonzero wheel delta",
        )
        state.fixture = after
        return {"wheel_count_before": count_before, "wheel_count_after": after["counts"]["wheel"],
                "wheel_delta_before": delta_before, "wheel_delta_after": after["wheel_delta"],
                "point": [x, y]}

    async def test_ascii_keys(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        focus = self._center(before["controls"]["text"])
        expected = "123-456-7890"
        response = await self._call("vnc_type", {
            "text": expected, "method": "keys", "focus_x": focus[0], "focus_y": focus[1],
        }, state)
        if response.get("transport") != "keys" or response.get("paste_sent") is not False:
            raise AcceptanceError("explicit keyboard text used a clipboard transport")
        after = await self.wait_fixture(
            state,
            lambda value: value.get("text") == expected
            and value["counts"]["key_down"] > before["counts"]["key_down"]
            and value["counts"]["key_up"] > before["counts"]["key_up"],
            "explicit ASCII keyboard text and key-down/up events",
        )
        state.fixture = after
        return {"expected_text": expected, "observed_text": after["text"],
                "fixture_readback_verified": True, "transport": response["transport"],
                "paste_sent": response["paste_sent"], "counts": after["counts"]}

    async def test_ascii(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        text_rect = before["controls"]["text"]
        focus = self._center(text_rect)
        expected = "VNC-{}-123".format(state.spec.name)
        response = await self._call("vnc_type", {
            "text": expected, "method": "auto", "focus_x": focus[0], "focus_y": focus[1],
        }, state)
        if response.get("application_verified") is not False:
            raise AcceptanceError("vnc_type did not keep application_verified=false")
        if not _strict_int(response.get("typed_characters")) or response["typed_characters"] != len(expected):
            raise AcceptanceError("vnc_type did not report the submitted ASCII character count")
        after = await self.wait_fixture(state, lambda value: value.get("text") == expected, "exact ASCII application readback")
        state.fixture = after
        return {
            "expected_text": expected,
            "observed_text": after["text"],
            "fixture_readback_verified": True,
            "application_verified": response["application_verified"],
            "transport": response.get("transport"),
            "paste_sent": response.get("paste_sent"),
        }

    async def test_ascii_backspace(self, state: TargetRun) -> dict[str, Any]:
        expected = "VNC-{}-123".format(state.spec.name)
        before = await self.read_fixture(state)
        if before.get("text") != expected:
            raise AcceptanceError("ASCII text changed before the Backspace acceptance step")
        shortened = expected[:-1]
        await self._call("vnc_keypress", {"key": "backspace"}, state)
        after_backspace = await self.wait_fixture(
            state,
            lambda value: value.get("text") == shortened
            and value["counts"]["key_down"] > before["counts"]["key_down"]
            and value["counts"]["key_up"] > before["counts"]["key_up"],
            "ASCII Backspace readback and key-down/up events",
        )
        state.fixture = after_backspace
        return {"expected_text": shortened, "observed_text": after_backspace["text"],
                "fixture_readback_verified": True, "counts": after_backspace["counts"]}

    async def test_ascii_four(self, state: TargetRun) -> dict[str, Any]:
        shortened = "VNC-{}-12".format(state.spec.name)
        before = await self.read_fixture(state)
        if before.get("text") != shortened:
            raise AcceptanceError("ASCII text changed before the replacement digit acceptance step")
        await self._call("vnc_keypress", {"key": "4"}, state)
        final = shortened + "4"
        after_four = await self.wait_fixture(
            state,
            lambda value: value.get("text") == final
            and value["counts"]["key_down"] > before["counts"]["key_down"]
            and value["counts"]["key_up"] > before["counts"]["key_up"],
            "ASCII replacement digit readback and key-down/up events",
        )
        state.fixture = after_four
        return {"expected_text": final, "observed_text": after_four["text"],
                "fixture_readback_verified": True, "counts": after_four["counts"]}

    async def test_unicode(self, state: TargetRun) -> dict[str, Any]:
        before = await self.read_fixture(state)
        focus = self._center(before["controls"]["text"])
        expected = "中文输入 {} 😀\n第二行 café".format(state.spec.name)
        response = await self._call("vnc_type", {
            "text": expected, "method": "auto", "focus_x": focus[0], "focus_y": focus[1],
        }, state)
        if response.get("clipboard_verified") is not True:
            raise AcceptanceError("Unicode auto transport did not report clipboard_verified=true")
        if response.get("paste_sent") is not True:
            raise AcceptanceError("Unicode auto transport did not report paste_sent=true")
        if response.get("application_verified") is not False:
            raise AcceptanceError("Unicode vnc_type response must report application_verified=false")
        after = await self.wait_fixture(
            state,
            lambda value: isinstance(value.get("text"), str)
            and value["text"].replace("\r\n", "\n") == expected,
            "exact Unicode and multiline fixture readback",
        )
        observed = after["text"].replace("\r\n", "\n")
        state.fixture = after
        await self.capture_image(state, label="unicode-readback")
        return {
            "expected_text": expected,
            "observed_text": observed,
            "fixture_readback_verified": observed == expected,
            "clipboard_verified": response["clipboard_verified"],
            "paste_sent": response["paste_sent"],
            "application_verified": response["application_verified"],
            "transport": response.get("transport"),
        }

    async def test_shift_refusal(self, state: TargetRun) -> dict[str, Any]:
        intended = "SHOULD_NOT_APPEAR-" + state.spec.name
        before = await self.read_fixture(state)
        state.held_keys.add("shift")
        key_down_ok = False
        refusal: dict[str, Any] | None = None
        text_unchanged = False
        try:
            await self._call("vnc_key_down", {"key": "shift"}, state)
            key_down_ok = True
            await self.wait_fixture(
                state,
                lambda value: value["counts"]["key_down"] > before["counts"]["key_down"],
                "held Shift key-down event",
            )
            result = await self._raw_call("vnc_type", {"text": intended, "method": "auto"}, state)
            if getattr(result, "isError", False):
                refusal = {"refused": True, "error": self._redact(self._tool_error(result))}
            else:
                payload = self._payload(result, "vnc_type")
                refusal = {"refused": False, "response_application_verified": payload.get("application_verified")}
            after = await self.wait_fixture(
                state, lambda value: value.get("text") == "", "held-Shift refusal left main text empty"
            )
            text_unchanged = after["text"] == ""
            if refusal["refused"] is not True:
                raise AcceptanceError("auto text was accepted while Shift was held; no replay was attempted")
            if not text_unchanged:
                raise AcceptanceError("fixture text changed after the held-Shift refusal")
        finally:
            if "shift" in state.held_keys:
                try:
                    await self._call("vnc_key_up", {"key": "shift"}, state)
                    state.held_keys.discard("shift")
                except Exception as exc:
                    state.result.setdefault("cleanup_errors", []).append(self._exception_text(exc))
                    self.write_report()
                    raise AcceptanceError("failed to release held Shift: " + self._exception_text(exc)) from None
        after_release = await self.wait_fixture(
            state,
            lambda value: value.get("text") == ""
            and value["counts"]["key_down"] > before["counts"]["key_down"]
            and value["counts"]["key_up"] > before["counts"]["key_up"],
            "held Shift key-up event and no text replay",
        )
        return {"key_down_succeeded": key_down_ok, **(refusal or {}),
                "text_still_empty": after_release["text"] == "", "automatic_replay": False,
                "counts": after_release["counts"]}

    async def run_target_suite(self, state: TargetRun) -> None:
        controls = state.result["controls_initial"]
        operations: list[tuple[str, Callable[[], Any]]] = [
            ("move_event", lambda: self.test_move(state, controls)),
            ("crop_png_metadata_and_remember", lambda: self.test_crop_and_remember(state, controls)),
        ]
        # The first crop step creates a remembered native canvas-interior reference.
        for name, operation in operations:
            await self.target_step(state, name, operation)

        crop = state.result["steps"]["crop_png_metadata_and_remember"]["evidence"]
        region = crop["region"]
        reference_before = crop["screenshot"].get("reference_id")
        if not reference_before:
            raise AcceptanceError("canvas crop did not retain a reference_id")

        await self.target_step(state, "wait_matches", lambda: self._visual_evidence(
            state, "matches", region, reference_before, timeout=1.5, expected_match=True
        ))
        await self.target_step(state, "button_single_click_and_canvas_change", lambda: self.test_single_click(state))
        await self.target_step(state, "wait_changed", lambda: self._visual_evidence(
            state, "changed", region, reference_before, timeout=2.0, expected_match=True
        ))
        await self.target_step(state, "button_double_click_count", lambda: self.test_double_click(state))

        state.fixture = await self.read_fixture(state)
        async def capture_and_wait_stable() -> dict[str, Any]:
            metadata, _size = await self.capture_image(
                state, label="canvas-interior-after-doubleclick", region=region, remember=True, save=False
            )
            after_reference = metadata.get("reference_id")
            if not after_reference:
                raise AcceptanceError("post-double-click crop did not return a reference_id")
            evidence = await self._visual_evidence(
                state, "stable", region, None, timeout=2.0, expected_match=True, stable_for=0.3
            )
            evidence["reference_id"] = after_reference
            return evidence

        stable_evidence = await self.target_step(state, "wait_stable", capture_and_wait_stable)
        after_reference = stable_evidence.get("reference_id")
        if not after_reference:
            raise AcceptanceError("stable canvas step did not retain a native reference_id")
        await self.target_step(state, "wait_timeout", lambda: self._visual_evidence(
            state, "changed", region, after_reference, timeout=0.5, expected_match=False
        ))
        await self.target_step(state, "canvas_drag_events_and_coordinates", lambda: self.test_drag(state))
        await self.target_step(state, "wheel_event_and_scroll", lambda: self.test_wheel(state))

        await self.target_step(state, "text_clear_before_keys", lambda: self._clear_and_evidence(state))
        await self.target_step(state, "ascii_keys_readback", lambda: self.test_ascii_keys(state))
        await self.target_step(state, "text_clear_before_ascii", lambda: self._clear_and_evidence(state))
        await self.target_step(state, "ascii_auto_readback", lambda: self.test_ascii(state))
        await self.target_step(state, "ascii_backspace_readback", lambda: self.test_ascii_backspace(state))
        await self.target_step(state, "ascii_four_readback", lambda: self.test_ascii_four(state))
        await self.target_step(state, "text_clear_before_unicode", lambda: self._clear_and_evidence(state))
        await self.target_step(state, "unicode_auto_readback", lambda: self.test_unicode(state))
        await self.target_step(state, "text_clear_before_shift_rejection", lambda: self._clear_and_evidence(state))
        await self.target_step(state, "held_shift_refusal", lambda: self.test_shift_refusal(state))
        await self.target_step(state, "held_shift_no_replay", lambda: self.test_shift_no_replay(state))
        state.result["status"] = "passed"
        self.write_report()

    async def _clear_and_evidence(self, state: TargetRun) -> dict[str, Any]:
        after = await self._clear_text(state, state.fixture["controls"] if state.fixture else state.result["controls_initial"])
        state.fixture = after
        return {"text_empty": after["text"] == "", "counts": after["counts"]}

    async def test_shift_no_replay(self, state: TargetRun) -> dict[str, Any]:
        after = await self.wait_fixture(state, lambda value: value.get("text") == "", "no replay after Shift release")
        if after.get("text") != "":
            raise AcceptanceError("held-Shift refused text appeared in the fixture; no replay was attempted")
        return {"text_empty_after_shift_release": True, "automatic_replay": False}

    async def _visual_evidence(
        self,
        state: TargetRun,
        condition: str,
        region: dict[str, int],
        reference_id: str | None,
        timeout: float,
        expected_match: bool,
        stable_for: float = 0.3,
    ) -> dict[str, Any]:
        result = await self._wait_visual(state, condition, region, reference_id, timeout, stable_for)
        if result.get("matched") is not expected_match:
            raise AcceptanceError("vnc_wait_for {} returned matched={}".format(condition, result.get("matched")))
        if condition == "changed" and expected_match is False and result.get("timed_out") is not True:
            raise AcceptanceError("unchanged canvas did not produce the expected visual timeout")
        if expected_match and result.get("timed_out") is True:
            raise AcceptanceError("visual condition {} timed out".format(condition))
        return {"condition": condition, "matched": result.get("matched"),
                "timed_out": result.get("timed_out"), "reference_id": result.get("reference_id"),
                "region": region, "width": result.get("width"), "height": result.get("height")}

    async def _connect(self, state: TargetRun) -> None:
        # Reconnect has the same immediate screenshot requirement as initial connect.
        data = await self._call("vnc_connect", {
            "host": state.spec.host,
            "port": state.spec.port,
            "profile": state.spec.profile_name,
            "session_id": state.spec.name,
        })
        connection_id = data.get("connection_id")
        if isinstance(connection_id, str) and connection_id:
            state.connection_id = connection_id
        if data.get("connected") is not True or not state.connection_id:
            raise AcceptanceError("vnc_connect did not return an active named connection")
        metadata, size = await self.capture_image(state, label="reconnected-full", save=True)
        if (metadata["desktop_width"], metadata["desktop_height"]) != (state.desktop_width, state.desktop_height):
            raise AcceptanceError("desktop dimensions changed on reconnect")
        if size != (state.desktop_width, state.desktop_height):
            raise AcceptanceError("reconnected full PNG dimensions changed")

    async def test_multi_session(self) -> None:
        connected = [state for state in self.states if state.connection_id]
        if len(connected) != 4:
            reason = "requires four active named sessions; {} are active".format(len(connected))
            for name in MULTI_STEPS:
                step = self.report["multi_session"]["steps"][name]
                step["reason"] = reason
            return

        first, second = connected[0], connected[1]

        async def cross_id() -> dict[str, Any]:
            result = await self._raw_call("vnc_screenshot", {
                "image_format": "png",
                "session_id": first.spec.name,
                "connection_id": second.connection_id,
            })
            if not getattr(result, "isError", False):
                raise AcceptanceError("screenshot accepted another session's connection_id")
            return {"rejected": True, "error": self._redact(self._tool_error(result))}

        try:
            await self.multi_step("cross_session_connection_id_rejected", cross_id)
        except Exception:
            pass

        async def screenshot_batches() -> dict[str, Any]:
            batches = []
            for index in range(1, 6):
                tasks = [self.capture_image(state, label="read-only-batch-{}".format(index)) for state in connected]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                errors = [item for item in results if isinstance(item, BaseException)]
                if errors:
                    raise AcceptanceError("screenshot batch {} failed: {}".format(
                        index, " | ".join(self._exception_text(error) for error in errors)
                    ))
                batches.append({"batch": index, "screenshots": len(results)})
                self.report["multi_session"]["completed_batches"] = list(batches)
                self.write_report()
            return {"batches": batches, "screenshots_total": len(batches) * len(connected), "input_sent": False}

        try:
            await self.multi_step("five_read_only_screenshot_batches", screenshot_batches)
        except Exception:
            pass

        async def capacity() -> dict[str, Any]:
            overflow_name = "capacity-overflow"
            result = await self._raw_call("vnc_connect", {
                "host": first.spec.host,
                "port": first.spec.port,
                "profile": first.spec.profile_name,
                "session_id": overflow_name,
            })
            if not getattr(result, "isError", False):
                data = self._payload(result, "vnc_connect")
                overflow_id = data.get("connection_id")
                if isinstance(overflow_id, str) and overflow_id:
                    self.extra_sessions.append((overflow_name, overflow_id, first))
                    disconnected = await self._raw_call("vnc_disconnect", {
                        "session_id": overflow_name, "connection_id": overflow_id,
                    })
                    disconnect_payload = self._payload(disconnected, "vnc_disconnect")
                    if disconnect_payload.get("connected") is not False:
                        raise AcceptanceError("unexpected fifth session could not be disconnected")
                    self.extra_sessions = [
                        item for item in self.extra_sessions if not (item[0] == overflow_name and item[1] == overflow_id)
                    ]
                raise AcceptanceError("fifth named session was accepted despite the four-session limit")
            inventory = await self._call("vnc_sessions", {})
            if inventory.get("session_count") != 4:
                raise AcceptanceError("capacity refusal did not preserve exactly four active sessions")
            return {"rejected": True, "active_sessions": inventory.get("session_count"),
                    "error": self._redact(self._tool_error(result))}

        try:
            await self.multi_step("fifth_session_capacity_rejected", capacity)
        except Exception:
            pass

        old_id = first.connection_id

        async def reconnect() -> dict[str, Any]:
            if old_id is None:
                raise AcceptanceError("reconnect source had no old connection_id")
            disconnected = await self._call("vnc_disconnect", {}, first)
            if disconnected.get("connected") is not False:
                raise AcceptanceError("vnc_disconnect did not close the selected named session")
            first.connection_id = None
            await self._connect(first)
            if first.connection_id == old_id:
                raise AcceptanceError("reconnect reused the previous connection_id")
            stale = await self._raw_call("vnc_screenshot", {
                "image_format": "png", "session_id": first.spec.name, "connection_id": old_id,
            })
            if not getattr(stale, "isError", False):
                raise AcceptanceError("screenshot accepted the old connection_id after reconnect")
            return {"old_id_rejected": True, "new_connection_id": first.connection_id,
                    "error": self._redact(self._tool_error(stale))}

        try:
            await self.multi_step("disconnect_reconnect_rejects_old_id", reconnect)
        except Exception:
            pass

        async def preserve_other_ids() -> dict[str, Any]:
            observations = []
            for state in connected[1:]:
                if not state.connection_id:
                    raise AcceptanceError("another named session lost its connection_id")
                metadata, _size = await self.capture_image(state, label="other-session-id-preserved")
                if metadata.get("connection_id") != state.initial_connection_id:
                    raise AcceptanceError("another named session changed its connection_id")
                observations.append({"name": state.spec.name, "connection_id_unchanged": True})
            return {"sessions": observations}

        try:
            await self.multi_step("other_session_ids_preserved", preserve_other_ids)
        except Exception:
            pass

    async def cleanup(self) -> None:
        for state in self.states:
            for key in list(state.held_keys or set()):
                try:
                    await self._call("vnc_key_up", {"key": key}, state)
                    state.held_keys.discard(key)
                except Exception as exc:
                    self.report["cleanup_errors"].append({
                        "target": state.spec.name,
                        "operation": "release held key",
                        "error": self._exception_text(exc),
                    })
        for state in self.states:
            if not state.connection_id:
                continue
            current_id = state.connection_id
            try:
                result = await self._call("vnc_disconnect", {}, state)
                if result.get("connected") is not False:
                    raise AcceptanceError("vnc_disconnect did not return connected=false")
                state.connection_id = None
            except Exception as exc:
                self.report["cleanup_errors"].append({
                    "target": state.spec.name,
                    "connection_id": current_id,
                    "operation": "disconnect",
                    "error": self._exception_text(exc),
                })
        for session_name, connection_id, state in list(self.extra_sessions):
            try:
                result = await self._raw_call("vnc_disconnect", {
                    "session_id": session_name, "connection_id": connection_id,
                })
                payload = self._payload(result, "vnc_disconnect")
                if payload.get("connected") is not False:
                    raise AcceptanceError("vnc_disconnect did not close the extra named session")
                self.extra_sessions.remove((session_name, connection_id, state))
            except Exception as exc:
                self.report["cleanup_errors"].append({
                    "target": state.spec.name,
                    "session_id": session_name,
                    "connection_id": connection_id,
                    "operation": "disconnect unexpected extra session",
                    "error": self._exception_text(exc),
                })
        if self.session is not None:
            try:
                inventory = await self._call("vnc_sessions", {})
                self.report["final_inventory"] = inventory
                step = self.report["multi_session"]["steps"]["all_sessions_disconnected"]
                if inventory.get("session_count") == 0:
                    step.update(status="passed", evidence={"session_count": 0})
                else:
                    step.update(status="failed", error="final session inventory was not empty")
            except Exception as exc:
                step = self.report["multi_session"]["steps"]["all_sessions_disconnected"]
                step.update(status="failed", error=self._exception_text(exc))
        else:
            self.report["multi_session"]["steps"]["all_sessions_disconnected"]["reason"] = "MCP session did not initialize"

    def finalize(self) -> None:
        passed: list[str] = []
        failed: list[str] = []
        not_executed: list[str] = []
        scopes = [("multi_session", self.report["multi_session"]["steps"])]
        scopes.extend(("targets/{}".format(state.spec.name), state.result["steps"]) for state in self.states)
        for prefix, steps in scopes:
            for name, step in steps.items():
                entry = prefix + "/" + name
                if step.get("status") == "passed":
                    passed.append(entry)
                elif step.get("status") == "failed":
                    failed.append(entry)
                else:
                    step.setdefault("status", "not_executed")
                    not_executed.append(entry)
        for error in self.report.get("cleanup_errors", []):
            failed.append("cleanup/{}/{}".format(
                error.get("target", "driver"), error.get("operation", "unknown_operation")
            ))
        for state in self.states:
            if state.result.get("suite_error"):
                failed.append("targets/{}/suite_error".format(state.spec.name))
        if self.report.get("driver_error"):
            failed.append("driver_error")
        self.report["passed"] = passed
        self.report["failed"] = failed
        self.report["not_executed"] = not_executed
        self.report["finished_at"] = _now()
        self.report["summary"] = {
            "passed": len(passed), "failed": len(failed), "not_executed": len(not_executed),
        }
        if failed:
            self.report["status"] = "failed"
        elif not_executed:
            self.report["status"] = "incomplete"
        else:
            self.report["status"] = "passed"
        for state in self.states:
            statuses = [step.get("status") for step in state.result["steps"].values()]
            if "failed" in statuses or state.result.get("suite_error") or any(
                error.get("target") == state.spec.name for error in self.report.get("cleanup_errors", [])
            ):
                state.result["status"] = "failed"
            elif all(item == "passed" for item in statuses):
                state.result["status"] = "passed"
            elif state.result["status"] != "failed":
                state.result["status"] = "incomplete"

    async def run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(REPO_ROOT / "main.py"), "mcp"],
            cwd=str(REPO_ROOT),
            env=self._child_environment(),
        )
        try:
            self.stderr_capture = tempfile.TemporaryFile(
                mode="w+",
                encoding="utf-8",
                errors="replace",
                prefix=".vmware-vnc-stderr-",
                suffix=".tmp",
                dir=self.output.parent,
            )
            async with stdio_client(parameters, errlog=self.stderr_capture) as streams:
                async with ClientSession(*streams) as session:
                    self.session = session
                    try:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        missing = sorted(REQUIRED_TOOLS - names)
                        if missing:
                            raise AcceptanceError("stdio server is missing required tools: " + ", ".join(missing))
                        self.report["mcp_tool_count"] = len(names)
                        self.report["mcp_server"] = {
                            "command": sys.executable,
                            "args": ["main.py", "mcp"],
                            "screenshot_mode": "fresh",
                            "max_sessions": 4,
                        }
                        self.write_report()

                        await self.connect_initial_targets()
                        for state in self.states:
                            if not state.ready:
                                continue
                            try:
                                await self.run_target_suite(state)
                            except BaseException as exc:
                                # The failing target's remaining steps stay not_executed.
                                if not any(
                                    step.get("status") == "failed"
                                    for step in state.result["steps"].values()
                                ):
                                    state.result["suite_error"] = self._exception_text(exc)
                                    state.result["status"] = "failed"
                                    self.write_report()
                                continue
                        await self.test_multi_session()
                    except BaseException as exc:
                        self.report["driver_error"] = self._exception_text(exc)
                    finally:
                        await self.cleanup()
        except BaseException as exc:
            self.report["driver_error"] = self._exception_text(exc)
        finally:
            self._close_stderr_capture()
            for state in self.states:
                temp_dir = state.temporary_directory
                if temp_dir is not None:
                    try:
                        temp_dir.cleanup()
                    except Exception as exc:
                        self.report["cleanup_errors"].append({
                            "target": state.spec.name,
                            "operation": "remove local vmrun temporary directory",
                            "error": self._exception_text(exc),
                        })
            self.finalize()
            try:
                self.write_report()
            except Exception as exc:
                print("Could not write live acceptance report: {}".format(type(exc).__name__), file=sys.stderr)


def _new_report(output: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "test": "vmware-vnc-input-live",
        "started_at": _now(),
        "allow_live": True,
        "output": str(output),
        "status": "incomplete",
        "targets": [],
        "passed": [],
        "failed": [],
        "not_executed": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit four-VM live input acceptance for the VNC MCP fixtures."
    )
    parser.add_argument("--manifest", type=Path, required=True,
                        help="JSON target manifest kept in .local")
    parser.add_argument("--profiles-file", type=Path, required=True,
                        help="TOML guest profiles; credentials are named environment variables")
    parser.add_argument("--output", type=Path, required=True,
                        help="JSON report path; PNG screenshots are written beside it")
    parser.add_argument("--allow-live", action="store_true", required=True,
                        help="required acknowledgement that real guest desktops will receive input")
    args = parser.parse_args(argv)
    args.manifest = args.manifest.resolve()
    args.profiles_file = args.profiles_file.resolve()
    args.output = args.output.resolve()
    if args.output in (args.manifest, args.profiles_file):
        parser.error("--output must not replace --manifest or --profiles-file")

    report = _new_report(args.output)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        from vmware_vnc.guest import load_profiles

        profiles = load_profiles(str(args.profiles_file))
        specs = _read_manifest(args.manifest, profiles)
        driver = LiveInputDriver(args, specs, profiles)
        asyncio.run(driver.run())
        print(json.dumps({
            "status": driver.report["status"],
            "summary": driver.report.get("summary"),
            "output": str(args.output),
        }, ensure_ascii=False))
        return 0 if driver.report["status"] == "passed" else 1
    except BaseException as exc:
        report["status"] = "failed"
        report["failed"].append("input_validation")
        report["failure"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        report["finished_at"] = _now()
        try:
            temporary = args.output.with_name(args.output.name + ".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            os.replace(temporary, args.output)
        except Exception:
            pass
        print("Live input acceptance could not start: {}".format(type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
