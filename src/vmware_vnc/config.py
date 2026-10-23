# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Validated runtime settings shared by the MCP and command-line adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import re
from typing import Mapping


def _number(value: object, name: str, minimum: float, maximum: float, *, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"{name} must be finite and between {minimum:g} and {maximum:g}")


@dataclass(frozen=True, slots=True)
class VNCConfig:
    timeout: float = 15.0
    action_timeout: float = 20.0
    screenshot_mode: str = "auto"
    encoding: str = "auto"
    max_sessions: int = 4
    max_pending: int = 16
    idle_timeout: float = 300.0
    default_host: str = ""
    default_port: int = 0
    default_url: str = field(default="", repr=False)
    password_env: str = "VNC_PASSWORD"
    profiles_file: str | None = None
    tls_ca_file: str | None = None

    def __post_init__(self) -> None:
        _number(self.timeout, "VNC_TIMEOUT", 0.001, 60)
        _number(self.action_timeout, "VNC_ACTION_TIMEOUT", 0.001, 60)
        _number(self.idle_timeout, "VNC_IDLE_TIMEOUT", 0, 86400)
        _number(self.max_sessions, "VNC_MAX_SESSIONS", 1, 32, integer=True)
        _number(self.max_pending, "VNC_MAX_PENDING", 1, 256, integer=True)
        _number(self.default_port, "VNC_PORT", 0, 65535, integer=True)
        if not isinstance(self.screenshot_mode, str) or self.screenshot_mode not in {"auto", "refresh", "fresh"}:
            raise ValueError("VNC_SCREENSHOT_MODE must be auto, refresh or fresh")
        if not isinstance(self.encoding, str) or self.encoding not in {"auto", "raw", "hextile", "zrle", "tight"}:
            raise ValueError("VNC_ENCODING must be auto, raw, hextile, zrle or tight")
        for name in ("default_host", "default_url", "password_env", "profiles_file", "tls_ca_file"):
            value = getattr(self, name)
            if value is None and name not in {"profiles_file", "tls_ca_file"}:
                raise ValueError(f"{name} must be a string")
            if value is not None and (not isinstance(value, str) or any(char in value for char in "\0\r\n")):
                raise ValueError(f"{name} must be a single-line string")
        if not self.password_env or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.password_env):
            raise ValueError("VNC_PASSWORD_ENV must name an environment variable")
        if self.default_url and (self.default_host or self.default_port):
            raise ValueError("Configure VNC_URL or VNC_HOST/VNC_PORT, not both")
        if self.default_url:
            from .transport import Endpoint
            Endpoint.from_target(url=self.default_url)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> VNCConfig:
        env = os.environ if environment is None else environment

        def numeric(name: str, default: str, convert):
            try:
                return convert(env.get(name, default))
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{name} has an invalid numeric value") from None

        return cls(
            timeout=numeric("VNC_TIMEOUT", "15", float),
            action_timeout=numeric("VNC_ACTION_TIMEOUT", "20", float),
            screenshot_mode=env.get("VNC_SCREENSHOT_MODE", "auto"),
            encoding=env.get("VNC_ENCODING", "auto"),
            max_sessions=numeric("VNC_MAX_SESSIONS", "4", int),
            max_pending=numeric("VNC_MAX_PENDING", "16", int),
            idle_timeout=numeric("VNC_IDLE_TIMEOUT", "300", float),
            default_host=env.get("VNC_HOST", "").strip(),
            default_port=numeric("VNC_PORT", "0", int),
            default_url=env.get("VNC_URL", "").strip(),
            password_env=env.get("VNC_PASSWORD_ENV", "VNC_PASSWORD"),
            profiles_file=env.get("VNC_PROFILES_FILE") or None,
            tls_ca_file=env.get("VNC_TLS_CA_FILE") or None,
        )
