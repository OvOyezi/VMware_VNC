# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Instance-owned VNC application; adapters share routing and lifecycle rules."""
from __future__ import annotations

import asyncio
import os
from typing import Callable, Mapping

from .client import VNCSession
from .config import VNCConfig
from .guest import GuestProfile, load_profiles
from .operations import SessionBackend
from .sessions import DEFAULT_SESSION, SessionRegistry, SessionResult
from .transport import Endpoint


OPERATIONS = frozenset({
    "status", "disconnect", "capture", "inspect", "click", "move", "drag", "scroll",
    "keypress", "key_down", "key_up", "mouse_down", "mouse_up", "send_text",
    "clipboard_get", "clipboard_set", "remember_visual", "load_visual_reference", "type_text", "wake_display",
})


class VNCApplication:
    """Own one registry, its configuration and lazily selected text profiles.

    Creating an instance does not read profile files, open sockets, or start a
    thread. Each adapter owns an instance and closes it after its final call.
    """

    def __init__(self, config: VNCConfig | None = None,
                 backend_factory: Callable[[], SessionBackend] | None = None,
                 environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment
        self.config = config or VNCConfig.from_env(self._environment)
        self.registry = SessionRegistry(
            backend_factory or self._new_session,
            max_sessions=self.config.max_sessions,
            idle_timeout=self.config.idle_timeout,
            max_pending=self.config.max_pending,
        )
        self._profiles: dict[str, GuestProfile] | None = None
        self._profile_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    def _new_session(self) -> VNCSession:
        return VNCSession(
            timeout=self.config.timeout,
            action_timeout=self.config.action_timeout,
            screenshot_mode=self.config.screenshot_mode,
            network_encoding=self.config.encoding,
            tls_ca_file=self.config.tls_ca_file,
        )

    def start(self) -> None:
        if self._closed:
            raise ConnectionError("VNC application is closed; create a new instance")
        if not self._started:
            self.registry.start()
            self._started = True

    async def close(self) -> None:
        self._closed = True
        # A failed cleanup remains visible and can be retried; never report a
        # successful shutdown while quarantined sessions still hold resources.
        await self.registry.close()

    async def __aenter__(self) -> VNCApplication:
        self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    async def _get_profiles(self) -> dict[str, GuestProfile]:
        if self._profiles is None:
            async with self._profile_lock:
                if self._profiles is None:
                    # An optional profile must not become a dependency of basic
                    # VNC startup or host/port connections.
                    self._profiles = await asyncio.to_thread(load_profiles, self.config.profiles_file or "")
        return self._profiles

    async def profiles(self) -> dict:
        self._ensure_open()
        profiles = await self._get_profiles()
        return {"profiles": [profiles[name].public() for name in sorted(profiles)]}

    async def diagnostics(self) -> dict:
        self._ensure_open()
        from .diagnostics import get_diagnostics
        result = await asyncio.to_thread(get_diagnostics)
        result.update(
            configured_profiles=len(self._profiles) if self._profiles is not None else (0 if not self.config.profiles_file else None),
            profiles_loaded=self._profiles is not None,
            profiles_configured=bool(self.config.profiles_file),
            max_sessions=self.config.max_sessions,
            max_pending_per_session=self.config.max_pending,
        )
        return result

    async def connect(self, host: str = "", port: int = 0, password: str = "",
                      session_id: str = DEFAULT_SESSION, profile: str = "", url: str = "") -> SessionResult:
        self.start()
        if any(not isinstance(value, str) for value in (host, url, password, profile)):
            raise ValueError("host, url, password and profile must be strings")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        host, url = host.strip(), url.strip()
        if url and (host or port or profile):
            raise ValueError("Use url alone, or an explicit host/port and optional guest profile")
        selected = None
        if profile:
            profiles = await self._get_profiles()
            if profile not in profiles:
                raise ValueError("unknown guest profile; use vnc_profiles to list configured targets")
            selected = profiles[profile]
        target_url = url or (self.config.default_url if not (host or port or selected) else "")
        if target_url:
            Endpoint.from_target(url=target_url)
            target_host, target_port = "", 0
        else:
            target_host = host or (selected.host if selected else self.config.default_host)
            target_port = port or (selected.port if selected else self.config.default_port)
            Endpoint.from_target(target_host, target_port)
            if selected is not None and not selected.matches(target_host, target_port):
                raise ValueError("guest profile endpoint does not match this VNC connection")
        secret = password or self._environment.get(self.config.password_env) or None
        return await self.registry.run(
            "connect", target_host, target_port, secret, selected, target_url or None,
            session_id=session_id, pin=False,
        )

    async def run(self, operation: str, *args, session_id: str = DEFAULT_SESSION,
                  connection_id: str | None = None, pin: bool = True) -> SessionResult:
        self.start()
        if operation not in OPERATIONS:
            raise ValueError("unsupported VNC application operation")
        return await self.registry.run(operation, *args, session_id=session_id,
                                       connection_id=connection_id, pin=pin)

    async def wait(self, seconds: float, session_id: str = DEFAULT_SESSION,
                   connection_id: str | None = None) -> SessionResult:
        self.start()
        return await self.registry.wait(seconds, session_id, connection_id)

    async def wait_for_visual(self, *args, **kwargs) -> SessionResult:
        self.start()
        return await self.registry.wait_for_visual(*args, **kwargs)

    def inventory(self) -> dict:
        self._ensure_open()
        return self.registry.inventory()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectionError("VNC application is closed; create a new instance")
