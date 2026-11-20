"""Synthetic guest clipboard selection, credential safety, and profile checks."""
from __future__ import annotations

import argparse
import hashlib
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
    def test_credential_backends_are_explicit_exclusive_and_validated_without_reading(self):
        invalid = (
            "",
            'username_env = "VNC_TEST_GUEST_USER"\n',
            'password_env = "VNC_TEST_GUEST_PASSWORD"\n',
            'username_env = []\npassword_env = "VNC_TEST_GUEST_PASSWORD"\n',
            'username_env = "VNC_TEST_GUEST_USER"\npassword_env = "invalid-name"\n',
            'windows_credential_target = ""\n',
            'windows_credential_target = []\n',
            'windows_credential_target = "private\\nvalue"\n',
            'windows_credential_target = "private\\u0000value"\n',
            'windows_credential_target = "synthetic"\nusername_env = "VNC_TEST_GUEST_USER"\n',
            'windows_credential_target = "synthetic"\npassword_env = "VNC_TEST_GUEST_PASSWORD"\n',
            'windows_credential_target = "synthetic"\nusername_env = "VNC_TEST_GUEST_USER"\n'
            'password_env = "VNC_TEST_GUEST_PASSWORD"\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for settings in invalid:
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    _load_profile(root, credential_settings=settings)
            for guest_os in ("windows", "linux"):
                profile = _load_profile(
                    root, credential_settings='windows_credential_target = "synthetic"\n', guest_os=guest_os
                )
                self.assertEqual(profile.windows_credential_target, "synthetic")
                self.assertIsNone(profile.username_env)
                self.assertIsNone(profile.password_env)
                self.assertTrue(profile.matches("127.0.0.1", 5901))
                self.assertFalse(profile.matches("127.0.0.1", 5902))

    def test_environment_credentials_and_legacy_positional_profile_remain_compatible(self):
        profile = _guest.GuestProfile(
            "synthetic", "127.0.0.1", 5901, "synthetic.vmx", "linux", "/tmp",
            "VNC_TEST_GUEST_USER", "VNC_TEST_GUEST_PASSWORD", "VNC_TEST_VM_PASSWORD",
            "synthetic-vmrun", ":9", "/tmp/authority", "x11", None,
        )
        self.assertIsNone(profile.windows_credential_target)
        self.assertEqual(profile.vm_password_env, "VNC_TEST_VM_PASSWORD")
        self.assertEqual(profile.vmrun, "synthetic-vmrun")
        self.assertEqual(profile.clipboard_backend, "x11")
        credentials = {"VNC_TEST_GUEST_USER": "合成用户", "VNC_TEST_GUEST_PASSWORD": "合成密码🙂"}
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name == "win32cred":
                raise AssertionError("the environment backend imported Windows credential support")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import), \
             mock.patch.dict(_guest.os.environ, credentials, clear=True):
            self.assertEqual(profile.guest_credentials(), ("合成用户", "合成密码🙂"))
            self.assertEqual(profile.public()["credentials"], "environment_variables")
        for missing in credentials:
            environment = {key: value for key, value in credentials.items() if key != missing}
            with self.subTest(missing=missing), mock.patch.dict(_guest.os.environ, environment, clear=True), \
                 self.assertRaises(ValueError):
                profile.guest_credentials()

    def test_windows_credentials_are_lazy_in_profile_listing_and_clipboard_construction(self):
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name == "win32cred":
                raise AssertionError("profile inspection read Windows credentials")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vmrun.exe").write_bytes(b"synthetic")
            with mock.patch("builtins.__import__", side_effect=guarded_import), \
                 mock.patch.dict(_guest.os.environ, {}, clear=True), \
                 mock.patch.object(_guest, "get_diagnostics") as diagnostics:
                profile = _load_profile(root, credential_settings='windows_credential_target = "private-target"\n')
                _guest.VmrunClipboard(profile)
                public = profile.public()
            diagnostics.assert_not_called()
        self.assertEqual(public["credentials"], "windows_credential_manager")
        self.assertNotIn("private-target", json.dumps(public))

    def test_windows_credentials_read_only_the_named_generic_target_and_decode_unicode(self):
        username, password = "域\\合成用户", "合成密码🙂🧪"
        credential_store = SimpleNamespace(
            CRED_TYPE_GENERIC=1,
            CredRead=mock.Mock(return_value={"UserName": username, "CredentialBlob": password.encode("utf-16-le")}),
            CredEnumerate=mock.Mock(side_effect=AssertionError("credential enumeration is forbidden")),
        )
        with tempfile.TemporaryDirectory() as directory:
            profile = _load_profile(
                Path(directory), credential_settings='windows_credential_target = "synthetic-target"\n'
            )
            with mock.patch.object(_guest.sys, "platform", "win32"), \
                 mock.patch.dict(sys.modules, {"win32cred": credential_store}):
                self.assertEqual(profile.guest_credentials(), (username, password))
                credential_store.CredRead.return_value["CredentialBlob"] = b""
                self.assertEqual(profile.guest_credentials(), (username, ""))
        self.assertEqual(credential_store.CredRead.call_args_list, [mock.call("synthetic-target", 1, 0)] * 2)
        credential_store.CredEnumerate.assert_not_called()

    def test_windows_credential_failures_and_malformed_records_hide_all_values(self):
        secret = "PRIVATE-CREDENTIAL-VALUE"
        invalid = (
            None,
            {},
            {"UserName": secret},
            {"CredentialBlob": secret.encode("utf-16-le")},
            {"UserName": "", "CredentialBlob": b""},
            {"UserName": [secret], "CredentialBlob": b""},
            {"UserName": secret + "\0", "CredentialBlob": b""},
            {"UserName": secret, "CredentialBlob": secret},
            {"UserName": secret, "CredentialBlob": b"\xff"},
            {"UserName": secret, "CredentialBlob": b"\x00\xd8"},
            {"UserName": secret, "CredentialBlob": (secret + "\0").encode("utf-16-le")},
        )
        with tempfile.TemporaryDirectory() as directory:
            profile = _load_profile(
                Path(directory), credential_settings=f'windows_credential_target = "{secret}"\n'
            )
            for record in invalid:
                credential_store = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=mock.Mock(return_value=record))
                with self.subTest(record=record), mock.patch.object(_guest.sys, "platform", "win32"), \
                     mock.patch.dict(sys.modules, {"win32cred": credential_store}), \
                     self.assertRaises(RuntimeError) as caught:
                    profile.guest_credentials()
                self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))
            for failure in (FileNotFoundError(secret), PermissionError(secret), OSError(secret)):
                credential_store = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=mock.Mock(side_effect=failure))
                with self.subTest(failure=type(failure).__name__), \
                     mock.patch.object(_guest.sys, "platform", "win32"), \
                     mock.patch.dict(sys.modules, {"win32cred": credential_store}), \
                     self.assertRaises(RuntimeError) as caught:
                    profile.guest_credentials()
                self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))

    def test_windows_credential_platform_and_import_failures_are_safe(self):
        original_import = __import__
        imports = []

        def unavailable_import(name, *args, **kwargs):
            if name == "win32cred":
                imports.append(name)
                raise ImportError("PRIVATE-IMPORT-VALUE")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            profile = _load_profile(Path(directory), credential_settings='windows_credential_target = "synthetic"\n')
            with mock.patch("builtins.__import__", side_effect=unavailable_import):
                with mock.patch.object(_guest.sys, "platform", "linux"), self.assertRaises(RuntimeError):
                    profile.guest_credentials()
                self.assertEqual(imports, [])
                with mock.patch.object(_guest.sys, "platform", "win32"), self.assertRaises(RuntimeError) as caught:
                    profile.guest_credentials()
            self.assertEqual(imports, ["win32cred"])
            self.assertNotIn("PRIVATE-IMPORT-VALUE", "".join(traceback.format_exception(caught.exception)))

    def test_vm_encryption_environment_is_independent_of_windows_guest_credentials(self):
        credential_store = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=mock.Mock())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vmrun.exe").write_bytes(b"synthetic")
            profile = _load_profile(
                root, 'vm_password_env = "VNC_TEST_VM_PASSWORD"\n',
                credential_settings='windows_credential_target = "synthetic"\n',
            )
            with mock.patch.object(_guest.sys, "platform", "win32"), \
                 mock.patch.dict(sys.modules, {"win32cred": credential_store}):
                with mock.patch.dict(_guest.os.environ, {}, clear=True), self.assertRaises(ValueError):
                    _guest.VmrunClipboard(profile)
                with mock.patch.dict(_guest.os.environ, {"VNC_TEST_VM_PASSWORD": "VM-ENCRYPTION-SECRET"}, clear=True):
                    helper = _guest.VmrunClipboard(profile)
            credential_store.CredRead.assert_not_called()
            self.assertEqual(helper.vm_password, "VM-ENCRYPTION-SECRET")

    def test_invalid_or_cancelled_clipboard_request_does_not_read_windows_credentials(self):
        credential_store = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=mock.Mock())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vmrun.exe").write_bytes(b"synthetic")
            profile = _load_profile(root, credential_settings='windows_credential_target = "synthetic"\n')
            with mock.patch.object(_guest.sys, "platform", "win32"), \
                 mock.patch.dict(sys.modules, {"win32cred": credential_store}):
                helper = _guest.VmrunClipboard(profile)
                with self.assertRaises(ValueError):
                    helper.prepare_clipboard("invalid\0text", _operations.ActionControl(time.monotonic() + 5))
                control = _operations.ActionControl(time.monotonic() + 5)
                control.cancelled.set()
                with self.assertRaises(_operations.ActionCancelled):
                    helper.prepare_clipboard("synthetic", control)
            credential_store.CredRead.assert_not_called()

    def test_windows_credential_read_failure_never_invokes_vmrun_fallback_or_paste(self):
        client_module = importlib.import_module("vmware_vnc.client")
        credential_store = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=mock.Mock(side_effect=OSError("PRIVATE-SECRET")))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vmrun.exe").write_bytes(b"synthetic")
            profile = _load_profile(root, credential_settings='windows_credential_target = "synthetic"\n')
            session = client_module.VNCSession()
            session.host, session.port, session.guest_profile = profile.host, profile.port, profile
            with mock.patch.object(_guest.sys, "platform", "win32"), \
                 mock.patch.dict(sys.modules, {"win32cred": credential_store}), \
                 mock.patch.object(session, "_require_client", return_value=SimpleNamespace()), \
                 mock.patch.object(session, "_require_display"), \
                 mock.patch.object(session, "_literal_text_transport", return_value="guest"), \
                 mock.patch.object(session, "_check_point"), \
                 mock.patch.object(session, "clipboard_set") as fallback, \
                 mock.patch.object(session, "type_text") as keys, \
                 mock.patch.object(session, "click") as click, \
                 mock.patch.object(session, "keypress") as paste, \
                 mock.patch.object(_guest.subprocess, "Popen", side_effect=AssertionError("vmrun was invoked")) as popen, \
                 self.assertRaises(RuntimeError) as caught:
                session.send_text("synthetic", focus_x=5, focus_y=5)
            credential_store.CredRead.assert_called_once_with("synthetic", 1, 0)
            for action in (fallback, keys, click, paste, popen):
                action.assert_not_called()
            self.assertNotIn("PRIVATE-SECRET", "".join(traceback.format_exception(caught.exception)))

    def test_windows_credential_values_are_redacted_from_vmrun_failure(self):
        username, password, vm_password = "PRIVATE-USERNAME", "合成密码🙂", "PRIVATE-VM-PASSWORD"
        credential_store = SimpleNamespace(
            CRED_TYPE_GENERIC=1,
            CredRead=mock.Mock(return_value={"UserName": username, "CredentialBlob": password.encode("utf-16-le")}),
        )
        child = SimpleNamespace(
            returncode=1,
            communicate=mock.Mock(return_value=(f"{username} {password} {vm_password}".encode("utf-8"), b"")),
            poll=mock.Mock(return_value=1), kill=mock.Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vmrun.exe").write_bytes(b"synthetic")
            profile = _load_profile(
                root, 'vm_password_env = "VNC_TEST_VM_PASSWORD"\n',
                credential_settings='windows_credential_target = "synthetic"\n',
            )
            with mock.patch.object(_guest.sys, "platform", "win32"), \
                 mock.patch.dict(sys.modules, {"win32cred": credential_store}), \
                 mock.patch.dict(_guest.os.environ, {"VNC_TEST_VM_PASSWORD": vm_password}, clear=True), \
                 mock.patch.object(_guest.subprocess, "Popen", return_value=child), \
                 self.assertRaises(RuntimeError) as caught:
                _guest.VmrunClipboard(profile).prepare_clipboard("synthetic", _operations.ActionControl(time.monotonic() + 5))
            credential_store.CredRead.assert_called_once()
            rendered = "".join(traceback.format_exception(caught.exception))
            for secret in (username, password, vm_password):
                self.assertNotIn(secret, rendered)

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


def _profile_document(root: Path, extra: str, guest_os: str = "linux", credential_settings: str | None = None) -> str:
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
        + (credential_settings if credential_settings is not None else
           'username_env = "VNC_TEST_GUEST_USER"\npassword_env = "VNC_TEST_GUEST_PASSWORD"\n')
        + vmrun_setting
        + extra
    )


def _load_profile(root: Path, extra: str = "", credential_settings: str | None = None, guest_os: str = "linux"):
    path = root / "profiles.toml"
    path.write_text(_profile_document(root, extra, guest_os, credential_settings), encoding="utf-8")
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


def _run_checks() -> dict[str, object]:
    if __package__:
        from ._unittest_report import run_cases
    else:
        from _unittest_report import run_cases

    checks, result = run_cases(GuestClipboardChecks, stream=io.StringIO())
    return {"success": result.wasSuccessful(), "checks": checks,
            "skipped": result.skipped_details, "failures": result.failure_details}


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
