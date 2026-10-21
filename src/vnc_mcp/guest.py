"""Endpoint-bound profiles and optional VMware clipboard preparation for VNC text."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import subprocess
import tempfile
import time
import tomllib
from uuid import uuid4

from .clipboard import validate_text
from .diagnostics import get_diagnostics
from .operations import ActionControl


@dataclass(frozen=True)
class GuestProfile:
    name: str
    host: str
    port: int
    vmx: str
    guest_os: str
    guest_temp_dir: str
    username_env: str
    password_env: str
    vm_password_env: str | None = None
    vmrun: str | None = None
    display: str | None = None
    xauthority: str | None = None

    def matches(self, host: str, port: int) -> bool:
        return self.host.casefold() == host.casefold() and self.port == port

    def public(self) -> dict:
        return {"profile": self.name, "host": self.host, "port": self.port, "guest_os": self.guest_os,
                "text_transport": "official_vmrun", "credentials": "environment_variables"}


def load_profiles(path: str | None = None) -> dict[str, GuestProfile]:
    source = path if path is not None else os.environ.get("VNC_PROFILES_FILE")
    if not source:
        return {}
    profile_path = Path(source).expanduser().resolve()
    with profile_path.open("rb") as handle:
        config = tomllib.load(handle)
    if type(config.get("version")) is not int or config["version"] != 1 or set(config) - {"version", "profiles"}:
        raise ValueError("guest profile file requires version = 1 and a profiles table")
    if not isinstance(config.get("profiles", {}), dict):
        raise ValueError("profiles must be a table")
    profiles = {}
    for name, fields in config.get("profiles", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("invalid guest profile name")
        if not isinstance(fields, dict):
            raise ValueError("guest profile must be a table")
        allowed = set(GuestProfile.__dataclass_fields__) - {"name"}
        if set(fields) - allowed:
            raise ValueError(f"unknown settings in guest profile {name!r}; inline credentials are not supported")
        try:
            profile = GuestProfile(name=name, **fields)
        except TypeError as exc:
            raise ValueError(f"missing or invalid settings in guest profile {name!r}") from exc
        required_strings = (profile.host, profile.vmx, profile.guest_temp_dir, profile.username_env, profile.password_env)
        if any(not isinstance(value, str) or not value or any(c in value for c in "\0\r\n") for value in required_strings):
            raise ValueError("profile paths, host and credential variable names must be nonempty single-line strings")
        if isinstance(profile.port, bool) or not isinstance(profile.port, int) or not 1 <= profile.port <= 65535:
            raise ValueError("profile port must be an integer between 1 and 65535")
        if not isinstance(profile.guest_os, str) or profile.guest_os not in {"windows", "linux"}:
            raise ValueError("guest_os must be windows or linux")
        guest_path = ntpath if profile.guest_os == "windows" else posixpath
        if (not guest_path.isabs(profile.guest_temp_dir)
                or guest_path.dirname(profile.guest_temp_dir) == profile.guest_temp_dir
                or (profile.guest_os == "windows" and not ntpath.splitdrive(profile.guest_temp_dir)[0])):
            raise ValueError("guest_temp_dir must be an absolute, non-root guest directory")
        if any(value is not None and (not isinstance(value, str) or any(c in value for c in "\0\r\n"))
               for value in (profile.vm_password_env, profile.vmrun, profile.display, profile.xauthority)):
            raise ValueError("optional profile settings must be single-line strings")
        for variable in (profile.username_env, profile.password_env, profile.vm_password_env):
            if variable is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                raise ValueError("credential environment variable name is invalid")
        vmx = Path(profile.vmx).expanduser()
        if not vmx.is_absolute():
            vmx = profile_path.parent / vmx
        if vmx.suffix.lower() != ".vmx" or not vmx.is_file():
            raise ValueError(f"profile {name!r} must refer to an existing .vmx file")
        profiles[name] = GuestProfile(**{**profile.__dict__, "vmx": str(vmx.resolve()),
                                          "guest_temp_dir": guest_path.normpath(profile.guest_temp_dir)})
    return profiles


class VmrunClipboard:
    """Prepare and verify clipboard text in the profile's bound guest."""

    def __init__(self, profile: GuestProfile) -> None:
        self.profile = profile
        discovered = profile.vmrun or get_diagnostics()["vmrun"]["path"]
        if not discovered or not Path(discovered).is_file():
            raise ValueError("official vmrun was not found; set VNC_VMRUN_PATH or the profile's vmrun path")
        self.executable = str(discovered)
        for key in (profile.username_env, profile.password_env, profile.vm_password_env):
            if key is not None and key not in os.environ:
                raise ValueError(f"required credential environment variable {key!r} is not set")
        self.username = os.environ[profile.username_env]
        if not self.username:
            raise ValueError("guest username must not be empty")
        self.password = os.environ[profile.password_env]
        self.vm_password = os.environ.get(profile.vm_password_env, "") if profile.vm_password_env else ""

    def _invoke(self, command: str, *arguments: str, control: ActionControl) -> str:
        control.check()
        argv = [self.executable, "-T", "ws"]
        if self.vm_password:
            argv += ["-vp", self.vm_password]
        argv += ["-gu", self.username, "-gp", self.password, command, self.profile.vmx, *arguments]
        try:
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise RuntimeError(f"vmrun {command} could not start ({type(exc).__name__})") from None
        try:
            while True:
                control.check()
                try:
                    out, err = child.communicate(timeout=min(0.1, max(0.001, control.deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            control.check()
            if child.returncode:
                detail = (out + err).decode("utf-8", "replace").strip()
                for secret in sorted({self.password, self.vm_password}, key=len, reverse=True):
                    if secret:
                        detail = detail.replace(secret, "<redacted>")
                raise RuntimeError(f"vmrun {command} failed (exit {child.returncode}): {detail[:2000]}")
            return out.decode("utf-8", "replace").strip()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def prepare_clipboard(self, text: str, control: ActionControl) -> dict:
        validate_text(text)
        token = uuid4().hex
        join = ntpath.join if self.profile.guest_os == "windows" else posixpath.join
        stem = join(self.profile.guest_temp_dir, f"vnc-mcp-{token}")
        script_name = "clipboard_windows.ps1" if self.profile.guest_os == "windows" else "clipboard_linux.py"
        remote_script = stem + (".ps1" if self.profile.guest_os == "windows" else ".py")
        remote_payload, remote_result = stem + ".json", stem + ".result.json"
        canonical = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.profile.guest_os == "windows":
            canonical = canonical.replace("\n", "\r\n")
        payload = {"text": canonical, "token": token, "display": self.profile.display, "xauthority": self.profile.xauthority}
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cleanup_errors = []
        attempted = []
        try:
            with tempfile.TemporaryDirectory(prefix="vnc-mcp-text-") as temporary:
                local = Path(temporary)
                script = local / script_name
                script.write_bytes(files("vnc_mcp").joinpath("resources", script_name).read_bytes())
                data = local / "payload.json"
                data.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                # Track attempted destinations even if the CLI reports an uncertain copy.
                attempted.append(remote_script)
                self._invoke("copyFileFromHostToGuest", str(script), remote_script, control=control)
                attempted.append(remote_payload)
                self._invoke("copyFileFromHostToGuest", str(data), remote_payload, control=control)
                attempted.append(remote_result)
                if self.profile.guest_os == "windows":
                    quote = lambda value: "'" + value.replace("'", "''") + "'"
                    wrapper = f"& {quote(remote_script)} -PayloadPath {quote(remote_payload)} -ResultPath {quote(remote_result)}"
                    encoded = base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii")
                    self._invoke("runProgramInGuest", "-interactive",
                                 r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                                 "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden",
                                 "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded, control=control)
                else:
                    # Linux clipboard tools use the explicit DISPLAY/authority
                    # or owned Wayland socket. VIX's interactive flag requires
                    # a login-manager desktop and rejects valid Xvnc sessions.
                    self._invoke("runProgramInGuest", "/usr/bin/python3", remote_script,
                                 remote_payload, remote_result, control=control)
                result = local / "result.json"
                self._invoke("copyFileFromGuestToHost", remote_result, str(result), control=control)
                if result.stat().st_size > 4096:
                    raise RuntimeError("guest clipboard acknowledgement exceeds the size limit")
                ack = json.loads(result.read_text(encoding="utf-8-sig"))
                if not ack.get("success") or ack.get("token") != token or ack.get("sha256") != expected_hash:
                    # Only fixed diagnostic labels may cross the guest boundary.
                    # Exception messages, paths and command lines can contain secrets.
                    labels = []
                    for field, allowed in (
                        ("backend", {"wayland", "xclip", "xsel"}),
                        ("phase", {"environment", "write", "verify"}),
                        ("error", {"RuntimeError", "TimeoutExpired", "CalledProcessError", "FileNotFoundError", "PermissionError", "OSError"}),
                    ):
                        value = ack.get(field)
                        if isinstance(value, str) and value in allowed:
                            labels.append(value)
                    detail = " (" + "/".join(labels) + ")" if labels else ""
                    raise RuntimeError("guest clipboard could not be verified" + detail + "; no paste was sent")
        finally:
            # A cancelled action may have prepared the clipboard, but never pastes.
            cleanup = ActionControl(time.monotonic() + 3.0)
            for remote in attempted:
                try:
                    self._invoke("deleteFileInGuest", remote, control=cleanup)
                except Exception:
                    cleanup_errors.append(remote)
        if cleanup_errors:
            raise RuntimeError("guest clipboard prepared but temporary file cleanup failed; no paste was sent")
        result = {"clipboard_verified": True, "clipboard_sha256": expected_hash, "guest_profile": self.profile.name,
                  "guest_os": self.profile.guest_os, "temporary_files_removed": True}
        backend = ack.get("backend")
        if isinstance(backend, str) and backend in {"wayland", "xclip", "xsel"}:
            result["clipboard_backend"] = backend
        return result
