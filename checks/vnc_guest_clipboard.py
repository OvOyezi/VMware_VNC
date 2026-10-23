"""Synthetic Linux guest clipboard selection, safety, and profile checks."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import traceback
import unittest
from types import SimpleNamespace
from unittest import mock


_guest = None
_operations = None
_linux = None


class _Entry:
    def __init__(self, name: str, mode: int, uid: int):
        self.name = name
        self._info = SimpleNamespace(st_mode=mode, st_uid=uid)

    def lstat(self):
        return self._info


class _Runtime:
    def __init__(self, entries: list[_Entry] | None = None, explicit: dict[str, _Entry] | None = None):
        self._entries = entries or []
        self._explicit = explicit or {entry.name: entry for entry in self._entries}

    def iterdir(self):
        return iter(self._entries)

    def __truediv__(self, name: str):
        return self._explicit.get(name, _Entry(name, stat.S_IFREG | 0o600, 0))

    def __str__(self):
        return "/synthetic/runtime"


class GuestClipboardChecks(unittest.TestCase):
    def test_profile_backend_and_wayland_settings_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_load_profile(root).clipboard_backend, "auto")
            self.assertEqual(
                _load_profile(root, 'clipboard_backend = "wayland"\nwayland_display = "wayland-0"\n').wayland_display,
                "wayland-0",
            )
            invalid = (
                'clipboard_backend = "xwayland"\n',
                "clipboard_backend = []\n",
                "wayland_display = []\n",
                'display = ":0"\nclipboard_backend = "wayland"\n',
                'clipboard_backend = "x11"\nwayland_display = "wayland-0"\n',
                'guest_os = "windows"\nclipboard_backend = "wayland"\n',
            )
            for extra in invalid:
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    _load_profile(root, extra)

    def test_wayland_socket_discovery_rejects_ambiguity_even_with_inherited_name(self):
        uid = 1200
        runtime = _Runtime([
            _Entry("wayland-0", stat.S_IFSOCK | 0o600, uid),
            _Entry("wayland-1", stat.S_IFSOCK | 0o600, uid),
        ])

        def use_runtime(env, actual_uid, required):
            env["XDG_RUNTIME_DIR"] = str(runtime)
            return runtime

        with mock.patch.dict(_linux.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True), \
             mock.patch.object(_linux.os, "getuid", return_value=uid, create=True), \
             mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name), \
             mock.patch.object(_linux, "_runtime_directory", side_effect=use_runtime):
            result = _linux.prepare_clipboard({"token": "t", "text": "private", "clipboard_backend": "wayland"})
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous_wayland_socket")
        self.assertNotIn("wayland-0", json.dumps(result))

        selected_runtime = _Runtime([_Entry("wayland-2", stat.S_IFSOCK | 0o600, uid)])

        def select_runtime(env, actual_uid, required):
            env["XDG_RUNTIME_DIR"] = str(selected_runtime)
            return selected_runtime

        with mock.patch.dict(_linux.os.environ, {"WAYLAND_DISPLAY": "wayland-untrusted", "DISPLAY": ":99"}, clear=True), \
             mock.patch.object(_linux.os, "getuid", return_value=uid, create=True), \
             mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name), \
             mock.patch.object(_linux, "_runtime_directory", side_effect=select_runtime):
            env, commands = _linux._select_session({"clipboard_backend": "wayland"})
        self.assertEqual(commands[2], "wayland")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-2")
        self.assertNotIn("DISPLAY", env)

    def test_explicit_wayland_name_rejects_traversal_symlink_and_foreign_socket(self):
        uid = 1200
        runtime = _Runtime(explicit={
            "wayland-link": _Entry("wayland-link", stat.S_IFLNK | 0o777, uid),
            "wayland-foreign": _Entry("wayland-foreign", stat.S_IFSOCK | 0o600, uid + 1),
            "wayland-owned": _Entry("wayland-owned", stat.S_IFSOCK | 0o600, uid),
        })
        for name in ("../wayland-owned", "/tmp/wayland-owned", "wayland-link", "wayland-foreign"):
            with self.subTest(name=name), self.assertRaises(_linux.ClipboardSetupError) as caught:
                _linux._wayland_socket(runtime, uid, name)
            self.assertEqual(caught.exception.reason, "invalid_wayland_socket")
        self.assertEqual(_linux._wayland_socket(runtime, uid, "wayland-owned"), "wayland-owned")

    def test_runtime_lstat_rejects_symlink_and_foreign_directory_and_preserves_valid_xdg(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_path = Path(directory)
            uid = getattr(os, "getuid", lambda: runtime_path.lstat().st_uid)()
            env = {"XDG_RUNTIME_DIR": str(runtime_path)}
            self.assertEqual(_linux._runtime_directory(env, uid, True), runtime_path)
            self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime_path))
            for mode, owner in ((stat.S_IFLNK | 0o777, uid), (stat.S_IFDIR | 0o700, uid + 1)):
                with self.subTest(mode=mode, owner=owner), mock.patch.object(
                    _linux, "_lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=owner)
                ), self.assertRaises(_linux.ClipboardSetupError) as caught:
                    _linux._runtime_directory({"XDG_RUNTIME_DIR": str(runtime_path)}, uid, True)
                self.assertEqual(caught.exception.reason, "invalid_runtime_directory")

    def test_auto_prefers_x11_when_both_display_variables_are_present(self):
        with mock.patch.dict(_linux.os.environ, {"DISPLAY": ":9", "WAYLAND_DISPLAY": "wayland-3"}, clear=True), \
             mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name), \
             mock.patch.object(_linux, "_runtime_directory") as runtime:
            env, commands = _linux._select_session({})
        self.assertEqual(commands[2], "xclip")
        self.assertEqual(env["DISPLAY"], ":9")
        self.assertNotIn("WAYLAND_DISPLAY", env)
        runtime.assert_not_called()

        with mock.patch.dict(_linux.os.environ, {"DISPLAY": ":9", "WAYLAND_DISPLAY": "wayland-3"}, clear=True), \
             mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name):
            explicit_x11, explicit_commands = _linux._select_session({"clipboard_backend": "x11"})
        self.assertEqual(explicit_commands[2], "xclip")
        self.assertEqual(explicit_x11["DISPLAY"], ":9")
        self.assertNotIn("WAYLAND_DISPLAY", explicit_x11)

    def test_wayland_mime_is_identical_for_copy_and_paste(self):
        with mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name):
            write, read, backend = _linux.clipboard_commands({}, "wayland")
        self.assertEqual(backend, "wayland")
        self.assertEqual(write[write.index("--type") + 1], _linux.WAYLAND_MIME_TYPE)
        self.assertEqual(read[read.index("--type") + 1], _linux.WAYLAND_MIME_TYPE)

    def test_wayland_write_timeout_kills_only_its_process_group_and_hides_input(self):
        uid = 1200
        runtime = _Runtime(explicit={"wayland-0": _Entry("wayland-0", stat.S_IFSOCK | 0o600, uid)})

        def use_runtime(env, actual_uid, required):
            env["XDG_RUNTIME_DIR"] = str(runtime)
            return runtime

        child = mock.Mock(pid=4312, returncode=None)
        child.communicate.side_effect = _linux.subprocess.TimeoutExpired(
            ["wl-copy", "SENSITIVE-ARGUMENT", "/private/path"], 5
        )
        child.wait.return_value = -9
        with mock.patch.dict(_linux.os.environ, {}, clear=True), \
             mock.patch.object(_linux.os, "getuid", return_value=uid, create=True), \
             mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name), \
             mock.patch.object(_linux, "_runtime_directory", side_effect=use_runtime), \
             mock.patch.object(_linux.subprocess, "Popen", return_value=child) as popen, \
             mock.patch.object(_linux.os, "killpg", create=True) as killpg:
            result = _linux.prepare_clipboard({
                "token": "synthetic", "text": "SENSITIVE-CLIPBOARD-TEXT", "clipboard_backend": "wayland",
                "wayland_display": "wayland-0",
            })
        self.assertFalse(result["success"])
        self.assertEqual(result.get("reason"), "wayland_focus_timeout", result)
        self.assertEqual(result["phase"], "write")
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(4312, getattr(_linux.signal, "SIGKILL", 9))
        rendered = json.dumps(result)
        self.assertNotIn("SENSITIVE", rendered)
        self.assertNotIn("/private/path", rendered)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "killpg"), "real process groups require POSIX")
    def test_real_timeout_reaps_a_spawned_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "child.pid"
            child_code = "import time; time.sleep(60)"
            parent_code = (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "open(sys.argv[1],'w').write(str(child.pid)); time.sleep(60)"
            )
            command = [sys.executable, "-c", parent_code, str(pid_path), child_code]
            with self.assertRaises(_linux.subprocess.TimeoutExpired):
                _linux._run_process(command, dict(os.environ), timeout=1)
            self.assertTrue(pid_path.exists(), "the child did not reach its process-group test point")
            child_pid = int(pid_path.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            try:
                while time.monotonic() < deadline and _process_is_running(child_pid):
                    time.sleep(0.05)
                self.assertFalse(_process_is_running(child_pid), "descendant survived process-group cleanup")
            finally:
                if _process_is_running(child_pid):
                    os.kill(child_pid, _linux.signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "killpg"), "real process groups require POSIX")
    def test_real_verify_failure_reaps_successful_write_owner_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "owner.pid"
            writer_code = (
                "import subprocess,sys; "
                "owner=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "open(sys.argv[1],'w').write(str(owner.pid)); sys.stdin.buffer.read()"
            )
            reader_code = "import sys; sys.stdout.buffer.write(b'verification-mismatch')"
            commands = (
                [sys.executable, "-c", writer_code, str(pid_path)],
                [sys.executable, "-c", reader_code],
                "wayland",
            )
            with mock.patch.object(_linux, "_select_session", return_value=(dict(os.environ), commands)):
                result = _linux.prepare_clipboard({"token": "synthetic", "text": "safe-input"})
            self.assertFalse(result["success"])
            self.assertEqual(result["reason"], "clipboard_mismatch")
            self.assertTrue(pid_path.exists(), "the clipboard owner did not reach its process-group test point")
            owner_pid = int(pid_path.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            try:
                while time.monotonic() < deadline and _process_is_running(owner_pid):
                    time.sleep(0.05)
                self.assertFalse(_process_is_running(owner_pid), "successful write owner survived verify failure")
            finally:
                if _process_is_running(owner_pid):
                    os.kill(owner_pid, _linux.signal.SIGKILL)

    def test_successful_clipboard_owner_is_not_killed_and_host_filters_ack_reason(self):
        uid = 1200
        runtime = _Runtime(explicit={"wayland-0": _Entry("wayland-0", stat.S_IFSOCK | 0o600, uid)})

        def use_runtime(env, actual_uid, required):
            env["XDG_RUNTIME_DIR"] = str(runtime)
            return runtime

        text = "clipboard 🙂"
        children = [
            mock.Mock(pid=4313, returncode=0),
            mock.Mock(pid=4314, returncode=0),
        ]
        children[0].communicate.return_value = (None, None)
        children[1].communicate.return_value = (text.encode("utf-8"), None)
        with mock.patch.dict(_linux.os.environ, {}, clear=True), \
             mock.patch.object(_linux.os, "getuid", return_value=uid, create=True), \
             mock.patch.object(_linux.shutil, "which", side_effect=lambda name: name), \
             mock.patch.object(_linux, "_runtime_directory", side_effect=use_runtime), \
             mock.patch.object(_linux.subprocess, "Popen", side_effect=children) as popen, \
             mock.patch.object(_linux.os, "killpg", create=True) as killpg:
            result = _linux.prepare_clipboard({
                "token": "synthetic", "text": text, "clipboard_backend": "wayland", "wayland_display": "wayland-0"
            })
        self.assertTrue(result["success"])
        self.assertEqual(result["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(popen.call_count, 2)
        self.assertTrue(all(call.kwargs["start_new_session"] for call in popen.call_args_list))
        killpg.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vmrun_path = root / "vmrun.exe"
            vmrun_path.write_bytes(b"synthetic")
            _load_profile(root, 'clipboard_backend = "wayland"\nwayland_display = "wayland-0"\n')
            profile_path = root / "profiles.toml"
            profile = _guest.load_profiles(str(profile_path))["synthetic"]
            credentials = {"VNC_TEST_GUEST_USER": "synthetic-user", "VNC_TEST_GUEST_PASSWORD": "SENSITIVE-PASSWORD"}

            for reason, expected_hint in (
                ("wayland_focus_timeout", "Wayland clipboard ownership timed out; the compositor may require the target desktop to be focused. Focus it and retry"),
                ("/private/path/SENSITIVE-SECRET", "guest clipboard could not be verified"),
                (["untrusted", "reason"], "guest clipboard could not be verified"),
            ):
                captured = {}

                def invoke(command, *arguments, control):
                    if command == "copyFileFromHostToGuest" and arguments[0].endswith("payload.json"):
                        captured.update(json.loads(Path(arguments[0]).read_text(encoding="utf-8")))
                    elif command == "copyFileFromGuestToHost":
                        ack = {"success": False, "token": captured["token"], "backend": "wayland", "phase": "write",
                               "error": "TimeoutExpired", "reason": reason}
                        Path(arguments[1]).write_text(json.dumps(ack), encoding="utf-8")
                    return ""

                with mock.patch.dict(_guest.os.environ, credentials, clear=True):
                    helper = _guest.VmrunClipboard(profile)
                    with mock.patch.object(helper, "_invoke", side_effect=invoke), self.assertRaises(RuntimeError) as caught:
                        helper.prepare_clipboard("SENSITIVE-CLIPBOARD-TEXT", _operations.ActionControl(time.monotonic() + 5))
                if reason == "wayland_focus_timeout":
                    self.assertIn(expected_hint, str(caught.exception))
                elif isinstance(reason, str):
                    self.assertNotIn(reason, str(caught.exception))
                self.assertNotIn("SENSITIVE", str(caught.exception))
                self.assertEqual(captured["clipboard_backend"], "wayland")
                self.assertEqual(captured["wayland_display"], "wayland-0")


def _profile_document(root: Path, extra: str, guest_os: str = "linux") -> str:
    vmx = root / "synthetic.vmx"
    vmx.write_text("synthetic", encoding="utf-8")
    guest_temp_dir = "/tmp" if guest_os == "linux" else "C:/Temp"
    vmrun_path = root / "vmrun.exe"
    vmrun_setting = f"vmrun = {json.dumps(vmrun_path.as_posix())}\n" if vmrun_path.is_file() else ""
    return (
        "version = 1\n\n[profiles.synthetic]\n"
        'host = "127.0.0.1"\nport = 5901\n'
        f"vmx = {json.dumps(vmx.as_posix())}\n"
        f'guest_os = "{guest_os}"\n'
        f'guest_temp_dir = "{guest_temp_dir}"\n'
        'username_env = "VNC_TEST_GUEST_USER"\npassword_env = "VNC_TEST_GUEST_PASSWORD"\n'
        + vmrun_setting
        + extra
    )


def _load_profile(root: Path, extra: str = ""):
    path = root / "profiles.toml"
    path.write_text(_profile_document(root, extra), encoding="utf-8")
    return _guest.load_profiles(str(path))["synthetic"]


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    proc_status = Path(f"/proc/{pid}/stat")
    if proc_status.is_file():
        try:
            return proc_status.read_text(encoding="ascii").split()[2] != "Z"
        except (OSError, IndexError):
            pass
    return True


def _load_modules(source_root: Path) -> None:
    source = str(source_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    global _guest, _operations, _linux
    _guest = importlib.import_module("vmware_vnc.guest")
    _operations = importlib.import_module("vmware_vnc.operations")
    helper_spec = importlib.util.spec_from_file_location(
        "linux_guest_clipboard", source_root / "src/vmware_vnc/resources/clipboard_linux.py"
    )
    _linux = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(_linux)


class _EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_details = []

    def _record(self, test, err):
        exception_type, exception, _ = err
        self.failure_details.append({
            "test": test.id(),
            "exception_type": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception),
            "traceback": self._exc_info_to_string(err, test),
        })

    def addFailure(self, test, err):
        self._record(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record(test, err)
        super().addError(test, err)


def _run_checks() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GuestClipboardChecks)
    tests = list(suite)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=2, resultclass=_EvidenceResult).run(suite)
    failed_ids = {detail["test"] for detail in result.failure_details}
    skipped_ids = {test.id() for test, _ in result.skipped}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed_ids
        for test in tests if test.id() not in skipped_ids
    }
    skipped = [{"test": test.id(), "reason": reason} for test, reason in result.skipped]
    return {"success": result.wasSuccessful(), "checks": checks, "skipped": skipped, "failures": result.failure_details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    try:
        _load_modules(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "failures": [{
                "test": "modules_load",
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
