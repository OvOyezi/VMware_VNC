# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""One-shot guest helper using an existing X11 or Wayland clipboard utility."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys


WAYLAND_MIME_TYPE = "text/plain;charset=utf-8"
_BACKENDS = {"auto", "x11", "wayland"}


class ClipboardSetupError(RuntimeError):
    """A fixed, safe reason code for a configuration or session failure."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _lstat(path):
    return path.lstat()


def _validate_settings(payload):
    backend = payload.get("clipboard_backend", "auto")
    if not isinstance(backend, str) or backend not in _BACKENDS:
        raise ClipboardSetupError("invalid_clipboard_settings")
    display = payload.get("display")
    wayland_display = payload.get("wayland_display")
    if display is not None and (not isinstance(display, str) or any(c in display for c in "\0\r\n")):
        raise ClipboardSetupError("invalid_clipboard_settings")
    if wayland_display is not None and (
        not isinstance(wayland_display, str)
        or not wayland_display
        or any(c in wayland_display for c in "\0\r\n")
    ):
        raise ClipboardSetupError("invalid_clipboard_settings")
    authority = payload.get("xauthority")
    if authority is not None and (not isinstance(authority, str) or any(c in authority for c in "\0\r\n")):
        raise ClipboardSetupError("invalid_clipboard_settings")
    if display and backend == "wayland":
        raise ClipboardSetupError("invalid_clipboard_settings")
    if wayland_display is not None and backend == "x11":
        raise ClipboardSetupError("invalid_clipboard_settings")
    return backend, display, wayland_display


def _runtime_directory(env, uid, required):
    supplied = env.get("XDG_RUNTIME_DIR")
    if supplied == "":
        raise ClipboardSetupError("invalid_runtime_directory")
    runtime = Path(supplied) if supplied is not None else Path(f"/run/user/{uid}")
    if not runtime.is_absolute():
        raise ClipboardSetupError("invalid_runtime_directory")
    try:
        info = _lstat(runtime)
    except FileNotFoundError:
        if required:
            raise ClipboardSetupError("wayland_runtime_unavailable") from None
        return None
    except OSError:
        raise ClipboardSetupError("invalid_runtime_directory") from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != uid:
        raise ClipboardSetupError("invalid_runtime_directory")
    if supplied is None:
        env["XDG_RUNTIME_DIR"] = str(runtime)
    return runtime


def owned_xauthority(runtime, uid):
    authorities = []
    for candidate in runtime.glob(".mutter-Xwaylandauth.*"):
        try:
            info = _lstat(candidate)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == uid:
            authorities.append(candidate)
    if len(authorities) > 1:
        raise ClipboardSetupError("ambiguous_xauthority")
    return str(authorities[0]) if authorities else None


def _base_environment(payload):
    backend, display, _ = _validate_settings(payload)
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    if payload.get("xauthority"):
        authority = payload["xauthority"]
        if not isinstance(authority, str) or any(c in authority for c in "\0\r\n"):
            raise ClipboardSetupError("invalid_clipboard_settings")
        env["XAUTHORITY"] = authority
    if display and backend != "wayland":
        # Explicit X11 display takes precedence in auto mode.
        env.pop("WAYLAND_DISPLAY", None)
    if env.get("DISPLAY") and not env.get("XAUTHORITY") and display:
        runtime = _runtime_directory(env, os.getuid(), required=False)
        if runtime is not None:
            authority = owned_xauthority(runtime, os.getuid())
            if authority:
                env["XAUTHORITY"] = authority
    return env


def _x11_commands(env):
    if not env.get("DISPLAY"):
        return None
    xclip = shutil.which("xclip")
    if xclip:
        return [xclip, "-selection", "clipboard", "-in"], [xclip, "-selection", "clipboard", "-out"], "xclip"
    xsel = shutil.which("xsel")
    if xsel:
        return [xsel, "--clipboard", "--input"], [xsel, "--clipboard", "--output"], "xsel"
    return None


def _wayland_commands():
    copy = shutil.which("wl-copy")
    paste = shutil.which("wl-paste")
    if not copy or not paste:
        return None
    return (
        [copy, "--type", WAYLAND_MIME_TYPE],
        [paste, "--type", WAYLAND_MIME_TYPE, "--no-newline"],
        "wayland",
    )


def _wayland_socket(runtime, uid, explicit_name):
    if explicit_name is not None:
        # The profile value is a single socket name relative to the verified runtime.
        if (not explicit_name or explicit_name in {".", ".."} or "/" in explicit_name
                or "\\" in explicit_name or Path(explicit_name).is_absolute()):
            raise ClipboardSetupError("invalid_wayland_socket")
        candidate = runtime / explicit_name
        try:
            info = _lstat(candidate)
        except OSError:
            raise ClipboardSetupError("invalid_wayland_socket") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
            raise ClipboardSetupError("invalid_wayland_socket")
        return explicit_name

    owned = []
    try:
        entries = runtime.iterdir()
        for candidate in entries:
            if not candidate.name.startswith("wayland-"):
                continue
            try:
                info = _lstat(candidate)
            except FileNotFoundError:
                continue
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == uid:
                owned.append(candidate.name)
    except OSError:
        raise ClipboardSetupError("wayland_socket_unavailable") from None
    if len(owned) > 1:
        raise ClipboardSetupError("ambiguous_wayland_socket")
    if not owned:
        raise ClipboardSetupError("wayland_socket_unavailable")
    return owned[0]


def _configure_wayland(env, uid, explicit_name):
    runtime = _runtime_directory(env, uid, required=True)
    name = _wayland_socket(runtime, uid, explicit_name)
    # Never let inherited Wayland or X11 variables bypass socket selection.
    env["WAYLAND_DISPLAY"] = name
    env.pop("DISPLAY", None)
    return env


def _select_session(payload):
    backend, display, wayland_display = _validate_settings(payload)
    env = _base_environment(payload)
    x11 = _x11_commands(env)
    wayland = _wayland_commands()

    if backend == "x11":
        env.pop("WAYLAND_DISPLAY", None)
        if not env.get("DISPLAY"):
            raise ClipboardSetupError("x11_display_unavailable")
        if x11 is None:
            raise ClipboardSetupError("clipboard_utility_unavailable")
        return env, x11

    if backend == "wayland":
        if wayland is None:
            raise ClipboardSetupError("clipboard_utility_unavailable")
        return _configure_wayland(env, os.getuid(), wayland_display), wayland

    # Auto mode honors a profile's explicit display first. Otherwise, a usable
    # inherited X11 session wins when both desktops are present.
    if display:
        env.pop("WAYLAND_DISPLAY", None)
        if x11 is None:
            raise ClipboardSetupError("clipboard_utility_unavailable")
        return env, x11
    if env.get("DISPLAY") and x11 is not None:
        env.pop("WAYLAND_DISPLAY", None)
        return env, x11
    if wayland is not None:
        return _configure_wayland(env, os.getuid(), wayland_display), wayland
    if env.get("DISPLAY"):
        raise ClipboardSetupError("clipboard_utility_unavailable")
    raise ClipboardSetupError("clipboard_utility_unavailable")


def session_environment(payload):
    """Compatibility helper for inspecting explicit environment adjustments."""
    return _base_environment(payload)


def clipboard_commands(env, backend=None):
    """Build commands for one already-selected backend; never fall back after failure."""
    if backend == "wayland":
        commands = _wayland_commands()
    elif backend in {"x11", "xclip", "xsel"}:
        commands = _x11_commands(env)
        if commands is not None and backend in {"xclip", "xsel"} and commands[2] != backend:
            # Preserve the requested implementation when both X utilities exist.
            executable = shutil.which(backend)
            if backend == "xclip" and executable:
                commands = [executable, "-selection", "clipboard", "-in"], [executable, "-selection", "clipboard", "-out"], backend
            elif backend == "xsel" and executable:
                commands = [executable, "--clipboard", "--input"], [executable, "--clipboard", "--output"], backend
            else:
                commands = None
    elif backend is None:
        commands = _x11_commands(env) or (_wayland_commands() if env.get("WAYLAND_DISPLAY") else None)
    else:
        commands = None
    if commands is None:
        raise ClipboardSetupError("clipboard_utility_unavailable")
    return commands


def _kill_process_group(child):
    try:
        if hasattr(os, "killpg"):
            os.killpg(child.pid, getattr(signal, "SIGKILL", 9))
        else:
            child.kill()
    except OSError:
        pass
    try:
        child.communicate(timeout=1)
    except Exception:
        try:
            child.wait(timeout=1)
        except Exception:
            pass


def _run_process(command, env, *, input_bytes=None, capture_stdout=False, timeout=5, owned_processes=None):
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    if owned_processes is not None:
        owned_processes.append(child)
    try:
        output, _ = child.communicate(input=input_bytes, timeout=timeout)
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command)
        return output or b""
    except BaseException:
        # start_new_session gives this invocation an isolated process group.
        # On success, a clipboard owner may remain alive to serve later pastes.
        _kill_process_group(child)
        if owned_processes is not None:
            try:
                owned_processes.remove(child)
            except ValueError:
                pass
        raise


def prepare_clipboard(payload):
    result = {"success": False, "token": payload.get("token"), "phase": "environment"}
    backend = None
    owned_processes = []
    try:
        env, commands = _select_session(payload)
        write, read, backend = commands
        result["backend"] = backend
        text = payload["text"].encode("utf-8")
        result["phase"] = "write"
        _run_process(write, env, input_bytes=text, timeout=5, owned_processes=owned_processes)
        result["phase"] = "verify"
        observed = _run_process(read, env, capture_stdout=True, timeout=5, owned_processes=owned_processes)
        if observed != text:
            result["reason"] = "clipboard_mismatch"
            raise RuntimeError("clipboard_mismatch")
        result.update(success=True, sha256=hashlib.sha256(observed).hexdigest(), phase="complete")
    except ClipboardSetupError as exc:
        result["error"] = type(exc).__name__
        result["reason"] = exc.reason
    except Exception as exc:
        result["error"] = type(exc).__name__
        if isinstance(exc, subprocess.TimeoutExpired):
            if result["phase"] == "write" and backend == "wayland":
                result["reason"] = "wayland_focus_timeout"
            elif result["phase"] == "write":
                result["reason"] = "clipboard_write_timeout"
            else:
                result["reason"] = "clipboard_verify_timeout"
        elif isinstance(exc, subprocess.CalledProcessError):
            result["reason"] = "clipboard_command_failed"
        elif isinstance(exc, FileNotFoundError):
            result["reason"] = "clipboard_utility_unavailable"
        elif isinstance(exc, OSError):
            result["reason"] = "clipboard_command_failed"
    finally:
        if not result["success"]:
            for child in owned_processes:
                _kill_process_group(child)
    return result


if __name__ == "__main__":
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    Path(sys.argv[2]).write_text(json.dumps(prepare_clipboard(payload)), encoding="utf-8")
