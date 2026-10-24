"""Behavioral checks for application ownership, lazy profiles and MCP factories."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import tomllib
import unittest
import uuid
from unittest import mock


_application = None
_config = None
_server = None


class FakeBackend:
    """In-memory backend used in place of a VNC connection."""

    action_timeout = 2.0

    def __init__(self, label: str, created: list[FakeBackend]) -> None:
        self.label = label
        self.connection_id: str | None = None
        self.target: tuple[str, int, str | None, str | None] | None = None
        self.calls: list[tuple[str, tuple, str | None, bool]] = []
        created.append(self)

    def execute(self, method: str, args: tuple, control, expected: str | None, pin: bool):
        control.check()
        self.calls.append((method, args, expected, pin))
        if method == "connect":
            host, port, _password, profile, url = args
            self.connection_id = f"{self.label}:{uuid.uuid4().hex}"
            self.target = (host, port, getattr(profile, "name", None), url)
            return {
                "connected": True,
                "connection_id": self.connection_id,
                "host": host,
                "port": port,
                "guest_profile": getattr(profile, "name", None),
                "url": url,
            }
        if method == "status":
            return {
                "connected": self.connection_id is not None,
                "connection_id": self.connection_id,
                "host": self.target[0] if self.target else "",
                "port": self.target[1] if self.target else 0,
            }
        raise AssertionError(f"unexpected fake backend operation: {method}")

    def disconnect(self) -> dict:
        self.connection_id = None
        return {"connected": False, "connection_id": None}


def _factory(label: str, created: list[FakeBackend]):
    return lambda: FakeBackend(label, created)


def _tool_payload(result):
    """Normalize FastMCP 1.30 tool results across structured/text encodings."""
    if isinstance(result, dict):
        if "structuredContent" in result:
            return result["structuredContent"]
        if "content" in result:
            result = result["content"]
        else:
            return result
    for item in result:
        text = getattr(item, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"MCP tool result did not contain JSON data: {result!r}")


class ApplicationChecks(unittest.IsolatedAsyncioTestCase):
    async def test_instances_keep_config_registry_and_targets_isolated(self):
        made_a: list[FakeBackend] = []
        made_b: list[FakeBackend] = []
        env_a = {"VNC_TEST_SECRET": "synthetic-a"}
        env_b = {"VNC_TEST_SECRET": "synthetic-b"}
        config_a = _config.VNCConfig(
            default_host="display-a.example", default_port=5901,
            password_env="VNC_TEST_SECRET", max_sessions=2, idle_timeout=0,
        )
        config_b = _config.VNCConfig(
            default_host="display-b.example", default_port=5902,
            password_env="VNC_TEST_SECRET", max_sessions=3, idle_timeout=0,
        )
        app_a = _application.VNCApplication(config_a, _factory("app-a", made_a), env_a)
        app_b = _application.VNCApplication(config_b, _factory("app-b", made_b), env_b)
        try:
            self.assertIsNot(app_a.registry, app_b.registry)
            self.assertEqual(app_a.config.default_host, "display-a.example")
            self.assertEqual(app_b.config.default_host, "display-b.example")
            self.assertEqual(app_a.registry.max_sessions, 2)
            self.assertEqual(app_b.registry.max_sessions, 3)

            result_a = await app_a.connect(session_id="screen-a")
            result_b = await app_b.connect(session_id="screen-b")
            self.assertNotEqual(result_a.connection_id, result_b.connection_id)
            self.assertEqual(made_a[0].target[:2], ("display-a.example", 5901))
            self.assertEqual(made_b[0].target[:2], ("display-b.example", 5902))
            self.assertEqual(made_a[0].calls[0][1][2], "synthetic-a")
            self.assertEqual(made_b[0].calls[0][1][2], "synthetic-b")
            self.assertEqual([item["session_id"] for item in app_a.inventory()["sessions"]], ["screen-a"])
            self.assertEqual([item["session_id"] for item in app_b.inventory()["sessions"]], ["screen-b"])

            status_a = await app_a.run("status", session_id="screen-a",
                                       connection_id=result_a.connection_id)
            status_b = await app_b.run("status", session_id="screen-b",
                                       connection_id=result_b.connection_id)
            self.assertEqual(status_a.value["host"], "display-a.example")
            self.assertEqual(status_b.value["host"], "display-b.example")
            with self.assertRaises(ConnectionError):
                await app_a.run("status", session_id="screen-b", pin=False)
        finally:
            await app_a.close()
            await app_b.close()

    async def test_malformed_optional_profile_does_not_block_basic_connect_or_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="vnc-app-profiles-") as temp:
            cases = []
            missing_path = Path(temp) / "missing.toml"
            malformed_path = Path(temp) / "malformed.toml"
            malformed_path.write_text("[profiles\nnot = valid = toml\n", encoding="utf-8")
            cases.extend((
                ("missing", missing_path, FileNotFoundError),
                ("malformed", malformed_path, tomllib.TOMLDecodeError),
            ))
            for label, profile_path, profile_error in cases:
                with self.subTest(profile_file=label):
                    backends: list[FakeBackend] = []
                    config = _config.VNCConfig(profiles_file=str(profile_path), idle_timeout=0)
                    app = _application.VNCApplication(config, _factory(label, backends), {})
                    try:
                        connected = await app.connect("basic.example", 5900, session_id="basic")
                        self.assertTrue(connected.value["connected"])
                        self.assertEqual(backends[0].target, ("basic.example", 5900, None, None))

                        with mock.patch(
                            "vmware_vnc.diagnostics.get_diagnostics",
                            return_value={"local_diagnostic": True},
                        ):
                            diagnostic = await app.diagnostics()
                        self.assertTrue(diagnostic["local_diagnostic"])
                        self.assertTrue(diagnostic["profiles_configured"])
                        self.assertFalse(diagnostic["profiles_loaded"])
                        self.assertIsNone(diagnostic["configured_profiles"])

                        with self.assertRaises(profile_error):
                            await app.profiles()
                        self.assertEqual(len(backends), 1)
                    finally:
                        await app.close()

    async def test_explicit_profile_is_bound_to_its_configured_endpoint(self):
        backends: list[FakeBackend] = []
        with tempfile.TemporaryDirectory(prefix="vnc-app-bound-profile-") as temp:
            root = Path(temp)
            vmx = root / "placeholder.vmx"
            vmx.write_text("synthetic fixture; never opened as a VM", encoding="utf-8")
            profile_path = root / "profiles.toml"
            profile_path.write_text(
                "version = 1\n"
                "[profiles.clipboard-test]\n"
                'host = "bound.example"\n'
                "port = 5907\n"
                'vmx = "placeholder.vmx"\n'
                'guest_os = "linux"\n'
                'guest_temp_dir = "/tmp/vnc-test"\n'
                'username_env = "VNC_TEST_USERNAME"\n'
                'password_env = "VNC_TEST_PASSWORD"\n',
                encoding="utf-8",
            )
            app = _application.VNCApplication(
                _config.VNCConfig(profiles_file=str(profile_path), idle_timeout=0),
                _factory("profile", backends),
                {},
            )
            try:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    await app.connect("other.example", 5908, session_id="wrong", profile="clipboard-test")
                self.assertEqual(backends, [])

                matched = await app.connect(session_id="bound", profile="clipboard-test")
                self.assertEqual(backends[0].target,
                                 ("bound.example", 5907, "clipboard-test", None))
                self.assertEqual(matched.value["guest_profile"], "clipboard-test")
            finally:
                await app.close()

    async def test_url_conflicts_with_host_port_or_profile(self):
        backends: list[FakeBackend] = []
        app = _application.VNCApplication(
            _config.VNCConfig(idle_timeout=0), _factory("url", backends), {},
        )
        try:
            for values in (
                {"host": "screen.example", "url": "vnc://screen.example:5900"},
                {"port": 5900, "url": "vnc://screen.example:5900"},
                {"profile": "clipboard-test", "url": "vnc://screen.example:5900"},
            ):
                with self.subTest(values=values):
                    with self.assertRaisesRegex(ValueError, "url alone"):
                        await app.connect(**values)
            self.assertEqual(backends, [])
        finally:
            await app.close()

    async def test_closed_application_rejects_async_operations(self):
        app = _application.VNCApplication(
            _config.VNCConfig(idle_timeout=0), _factory("closed", []), {},
        )
        await app.close()
        with mock.patch(
            "vmware_vnc.diagnostics.get_diagnostics",
            return_value={"local_diagnostic": True},
        ):
            operations = {
                "connect": lambda: app.connect("closed.example", 5900),
                "run": lambda: app.run("status"),
                "wait": lambda: app.wait(0),
                "wait_for_visual": lambda: app.wait_for_visual(timeout=0),
                "profiles": app.profiles,
                "diagnostics": app.diagnostics,
            }
            for name, operation in operations.items():
                with self.subTest(operation=name):
                    with self.assertRaises(ConnectionError):
                        await operation()

    async def test_cleanup_failure_is_reported_and_retry_releases_quarantine(self):
        made = []
        app = _application.VNCApplication(_config.VNCConfig(idle_timeout=0), _factory("retry", made))
        await app.connect("127.0.0.1", 5901, session_id="retry")
        backend = made[0]
        disconnect = backend.disconnect
        attempts = 0

        def flaky_disconnect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic cleanup failure")
            return disconnect()

        backend.disconnect = flaky_disconnect
        with self.assertLogs("vmware_vnc.sessions", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed: retry"):
                await app.close()
        self.assertEqual(app.registry.inventory()["session_count"], 1)
        with self.assertRaises(ConnectionError):
            await app.run("status")
        await app.close()
        self.assertEqual(attempts, 2)
        self.assertEqual(app.registry.inventory()["session_count"], 0)
        self.assertIsNone(backend.connection_id)

    async def test_mcp_rejects_boolean_and_fractional_integer_arguments_before_dispatch(self):
        made = []
        app = _application.VNCApplication(_config.VNCConfig(idle_timeout=0), _factory("strict", made))
        server = _server.create_server(application=app)
        try:
            await server.call_tool("vnc_connect", {"host": "127.0.0.1", "port": 5901})
            before = len(made[0].calls)
            invalid = [
                ("vnc_connect", {"host": "127.0.0.1", "port": True}),
                ("vnc_move", {"x": True, "y": 0}),
                ("vnc_click", {"x": 0, "y": 0, "count": True}),
                ("vnc_drag", {"x1": 0.5, "y1": 0, "x2": 1, "y2": 1}),
                ("vnc_scroll", {"x": 0, "y": 0, "direction": "up", "amount": True}),
                ("vnc_screenshot", {"quality": True}),
                ("vnc_type", {"text": "x", "delay_ms": True}),
            ]
            for name, arguments in invalid:
                try:
                    await server.call_tool(name, arguments)
                except Exception:
                    pass
                self.assertEqual(len(made[0].calls), before, name)
        finally:
            await app.close()

    async def test_two_mcp_factories_have_same_tools_and_distinct_backing_apps(self):
        made_a: list[FakeBackend] = []
        made_b: list[FakeBackend] = []
        app_a = _application.VNCApplication(
            _config.VNCConfig(default_host="mcp-a.example", default_port=5911, idle_timeout=0),
            _factory("mcp-a", made_a), {},
        )
        app_b = _application.VNCApplication(
            _config.VNCConfig(default_host="mcp-b.example", default_port=5912, idle_timeout=0),
            _factory("mcp-b", made_b), {},
        )
        server_a = _server.create_server(application=app_a)
        server_b = _server.create_server(application=app_b)
        try:
            tools_a = await server_a.list_tools()
            tools_b = await server_b.list_tools()
            names_a = {tool.name for tool in tools_a}
            names_b = {tool.name for tool in tools_b}
            self.assertEqual(names_a, names_b)
            self.assertIn("vnc_connect", names_a)
            self.assertIn("vnc_sessions", names_a)

            connected_a = _tool_payload(await server_a.call_tool(
                "vnc_connect", {"session_id": "only-a"},
            ))
            connected_b = _tool_payload(await server_b.call_tool(
                "vnc_connect", {"session_id": "only-b"},
            ))
            self.assertNotEqual(connected_a["connection_id"], connected_b["connection_id"])
            self.assertEqual(made_a[0].target[:2], ("mcp-a.example", 5911))
            self.assertEqual(made_b[0].target[:2], ("mcp-b.example", 5912))
            self.assertEqual(
                [item["session_id"] for item in _tool_payload(await server_a.call_tool("vnc_sessions", {}))["sessions"]],
                ["only-a"],
            )
            self.assertEqual(
                [item["session_id"] for item in _tool_payload(await server_b.call_tool("vnc_sessions", {}))["sessions"]],
                ["only-b"],
            )
        finally:
            await app_a.close()
            await app_b.close()


class ConfigurationChecks(unittest.TestCase):
    def test_boolean_nonfinite_range_and_enum_values_are_rejected(self):
        invalid_values = [
            {"timeout": True},
            {"action_timeout": False},
            {"timeout": math.nan},
            {"action_timeout": math.inf},
            {"idle_timeout": -math.inf},
            {"timeout": 0},
            {"timeout": 60.001},
            {"action_timeout": 0},
            {"idle_timeout": 86400.1},
            {"max_sessions": True},
            {"max_sessions": 1.5},
            {"max_sessions": 0},
            {"max_sessions": 33},
            {"max_pending": False},
            {"max_pending": 257},
            {"default_port": True},
            {"default_port": 65536},
            {"screenshot_mode": "stale"},
            {"encoding": "lossy"},
        ]
        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    _config.VNCConfig(**values)

    def test_default_url_conflicts_with_host_or_port(self):
        url = "vnc://display.example:5900"
        for values in (
            {"default_url": url, "default_host": "other.example"},
            {"default_url": url, "default_port": 5900},
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "VNC_URL or VNC_HOST/VNC_PORT"):
                    _config.VNCConfig(**values)


class ImportLifecycleChecks(unittest.TestCase):
    def test_import_and_create_server_do_not_start_socket_thread_or_read_profiles(self):
        with tempfile.TemporaryDirectory(prefix="vnc-app-import-") as temp:
            profile_path = Path(temp) / "deliberately-invalid.toml"
            profile_path.write_text("[not valid TOML", encoding="utf-8")
            code = r'''
import os, socket, sys, threading
from pathlib import Path
root = Path(sys.argv[1])
profile_path = Path(sys.argv[2])
sys.path.insert(0, str(root / "src"))

def forbidden(*args, **kwargs):
    raise AssertionError("socket or thread started during import/create_server")

socket.socket.__init__ = forbidden
threading.Thread.start = forbidden

def audit(event, args):
    if event == "open" and args:
        try:
            name = os.path.normcase(os.path.abspath(os.fspath(args[0])))
        except (TypeError, ValueError):
            return
        if name == os.path.normcase(os.path.abspath(profile_path)):
            raise AssertionError("guest profiles were read during import/create_server")

sys.addaudithook(audit)
from vmware_vnc.config import VNCConfig
from vmware_vnc.server import create_server
server = create_server(VNCConfig(profiles_file=str(profile_path), idle_timeout=0))
assert "vnc_connect" in {tool.name for tool in server._tool_manager.list_tools()}
print("import/create_server are resource-lazy")
'''
            child_env = dict(os.environ)
            for key in list(child_env):
                if key.startswith("VNC_"):
                    child_env.pop(key, None)
            result = subprocess.run(
                [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(profile_path)],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=child_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("resource-lazy", result.stdout)


class EvidenceTextResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_details = []

    def _record(self, test, err):
        exc_type, exc, tb = err
        self.failure_details.append({
            "test": test.id(),
            "exception_type": f"{exc_type.__module__}.{exc_type.__qualname__}",
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
        })

    def addFailure(self, test, err):
        self._record(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record(test, err)
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._record(test, err)
        super().addSubTest(test, subtest, err)


def _load_modules(source: Path):
    src = source / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return (
        importlib.import_module("vmware_vnc.application"),
        importlib.import_module("vmware_vnc.config"),
        importlib.import_module("vmware_vnc.server"),
    )


def _run_checks() -> dict[str, object]:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(ConfigurationChecks),
        loader.loadTestsFromTestCase(ImportLifecycleChecks),
        loader.loadTestsFromTestCase(ApplicationChecks),
    ])
    tests = []
    for item in suite:
        tests.extend(list(item) if isinstance(item, unittest.TestSuite) else [item])
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceTextResult).run(suite)
    failed = {detail["test"] for detail in result.failure_details}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed
        for test in tests
    }
    report: dict[str, object] = {"success": result.wasSuccessful(), "checks": checks}
    if result.failure_details:
        report["failure"] = result.failure_details
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    try:
        global _application, _config, _server
        _application, _config, _server = _load_modules(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "failure": {
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
