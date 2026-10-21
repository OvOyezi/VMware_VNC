"""Read-only host and dependency diagnostics for VNC-MCP."""
from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Any

from . import __version__


_DISTRIBUTIONS = {
    "vncdotool": "vncdotool",
    "mcp": "mcp",
    "Pillow": "Pillow",
    "Twisted": "Twisted",
}


def _runtime_version(distribution: str) -> dict[str, Any]:
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return {"available": False, "version": None, "reason": "not_installed"}
    return {"available": True, "version": version}


def _path_exists(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _discover_vmrun() -> dict[str, Any]:
    """Return the configured or locally discoverable vmrun path without running it."""
    configured_path = os.environ.get("VNC_VMRUN_PATH")
    if configured_path is not None:
        return {
            "source": "VNC_VMRUN_PATH",
            "path": configured_path,
            "exists": _path_exists(configured_path),
        }

    path_path = shutil.which("vmrun")
    if path_path:
        return {
            "source": "PATH",
            "path": path_path,
            "exists": _path_exists(path_path),
        }

    if platform.system() == "Windows":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            standard_path = str(
                Path(program_files) / "VMware" / "VMware Workstation" / "vmrun.exe"
            )
            return {
                "source": "ProgramFiles",
                "path": standard_path,
                "exists": _path_exists(standard_path),
            }

    return {"source": "unavailable", "path": None, "exists": False}


def get_diagnostics() -> dict[str, Any]:
    """Collect JSON-serializable runtime facts without contacting any VNC endpoint."""
    return {
        "schema_version": 1,
        "application": {"name": "vnc-mcp", "version": __version__},
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "host": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
        },
        "runtime_versions": {
            name: _runtime_version(distribution)
            for name, distribution in _DISTRIBUTIONS.items()
        },
        "vmrun": _discover_vmrun(),
    }
