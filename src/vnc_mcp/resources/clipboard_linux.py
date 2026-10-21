"""One-shot guest helper using an existing X11 or Wayland clipboard utility."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
result = {"success": False, "token": payload["token"]}
try:
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
        wayland = sorted(runtime.glob("wayland-*"))
        sockets = [entry.name for entry in wayland if entry.is_socket()]
        if len(sockets) == 1 and not payload.get("display"):
            env.setdefault("WAYLAND_DISPLAY", sockets[0])
    text = payload["text"].encode("utf-8")
    if env.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
        write, read = ["wl-copy", "--type", "text/plain;charset=utf-8"], ["wl-paste", "--no-newline"]
    elif env.get("DISPLAY") and shutil.which("xclip"):
        write, read = ["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"]
    elif env.get("DISPLAY") and shutil.which("xsel"):
        write, read = ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]
    else:
        raise RuntimeError("No configured graphical session and clipboard utility")
    # Clipboard owners can fork and keep descriptors open. Do not capture their
    # inherited stderr pipe, which would make communicate wait indefinitely.
    subprocess.run(write, input=text, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=5, check=True)
    observed = subprocess.run(read, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=5, check=True).stdout
    if observed != text:
        raise RuntimeError("Clipboard verification failed")
    result.update(success=True, sha256=hashlib.sha256(observed).hexdigest())
except Exception as exc:
    result["error"] = type(exc).__name__
Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")
