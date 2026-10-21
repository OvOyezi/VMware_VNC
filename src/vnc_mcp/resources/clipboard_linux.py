"""One-shot guest helper using an existing X11 or Wayland clipboard utility."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


def owned_xauthority(runtime, uid):
    authorities = []
    for candidate in runtime.glob(".mutter-Xwaylandauth.*"):
        info = candidate.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == uid:
            authorities.append(candidate)
    if len(authorities) > 1:
        raise RuntimeError("ambiguous_xauthority")
    return str(authorities[0]) if authorities else None


def session_environment(payload):
    env = dict(os.environ)
    if payload.get("display"):
        env["DISPLAY"] = payload["display"]
        # An explicit X11 target must not be diverted into another desktop.
        env.pop("WAYLAND_DISPLAY", None)
    if payload.get("xauthority"):
        env["XAUTHORITY"] = payload["xauthority"]
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir() and runtime.stat().st_uid == os.getuid():
        env.setdefault("XDG_RUNTIME_DIR", str(runtime))
        if payload.get("display") and not env.get("XAUTHORITY"):
            # GNOME's Xwayland authority filename changes at each login. Only
            # use one regular file owned by this account in its owned runtime.
            authority = owned_xauthority(runtime, os.getuid())
            if authority:
                env["XAUTHORITY"] = authority
        wayland = sorted(runtime.glob("wayland-*"))
        sockets = [entry.name for entry in wayland if entry.is_socket()]
        if len(sockets) == 1 and not payload.get("display"):
            env.setdefault("WAYLAND_DISPLAY", sockets[0])
    return env


def prepare_clipboard(payload):
    result = {"success": False, "token": payload["token"], "phase": "environment"}
    try:
        env = session_environment(payload)
        text = payload["text"].encode("utf-8")
        write, read, backend = clipboard_commands(env)
        result["backend"] = backend
        result["phase"] = "write"
        # Clipboard owners can fork and keep descriptors open. Do not capture
        # inherited stderr pipes, which can make communicate wait indefinitely.
        subprocess.run(write, input=text, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=5, check=True)
        result["phase"] = "verify"
        observed = subprocess.run(read, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=5, check=True).stdout
        if observed != text:
            raise RuntimeError("clipboard_mismatch")
        result.update(success=True, sha256=hashlib.sha256(observed).hexdigest(), phase="complete")
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def clipboard_commands(env):
    if env.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
        return ["wl-copy", "--type", "text/plain;charset=utf-8"], ["wl-paste", "--no-newline"], "wayland"
    elif env.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"], "xclip"
    elif env.get("DISPLAY") and shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"], "xsel"
    else:
        raise RuntimeError("No configured graphical session and clipboard utility")


if __name__ == "__main__":
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    Path(sys.argv[2]).write_text(json.dumps(prepare_clipboard(payload)), encoding="utf-8")
