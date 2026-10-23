# SPDX-License-Identifier: Apache-2.0
# Modified and maintained for VMware VNC; historical attribution is in NOTICE.
"""Validated endpoints and byte-stream adapters for RFB transports."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import ipaddress
import math
import re
import socket
import ssl
from typing import Protocol
from urllib.parse import urlsplit


MAX_STREAM_READ = 64 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE = 64 * 1024 * 1024
_CLOSE_TIMEOUT = 2.0
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A validated network target; private URI details never appear in repr/public()."""

    transport: str
    host: str
    port: int
    uri: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_target(cls, host: str = "", port: int = 0, url: str | None = None) -> "Endpoint":
        if url is not None:
            if host != "" or port != 0:
                raise ValueError("provide either a URL or a native host and port")
            return cls._from_url(url)

        clean_host = _validate_host(host)
        clean_port = _validate_port(port)
        return cls("tcp", clean_host, clean_port)

    @classmethod
    def _from_url(cls, url: str) -> "Endpoint":
        if not isinstance(url, str) or not url or any(ord(char) <= 0x20 or ord(char) == 0x7f for char in url):
            raise ValueError("invalid transport URL")
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in {"vnc", "vnc+tls", "ws", "wss"}:
                raise ValueError("unsupported transport URL scheme")
            if not parts.netloc or parts.hostname is None or parts.username is not None or parts.password is not None:
                raise ValueError("invalid transport URL authority")
            if "#" in url:
                raise ValueError("transport URL fragments are not supported")
            endpoint_host = _validate_host(parts.hostname)
            parsed_port = parts.port
            if parsed_port is None:
                parsed_port = {"vnc": 5900, "vnc+tls": 5900, "ws": 80, "wss": 443}[scheme]
            endpoint_port = _validate_port(parsed_port)
        except (ValueError, UnicodeError, OSError):
            # urlsplit/IDNA errors can include the original URL. Keep diagnostics opaque.
            raise ValueError("invalid transport URL") from None

        if scheme in {"vnc", "vnc+tls"}:
            if parts.path not in {"", "/"} or parts.query:
                raise ValueError("native VNC URLs cannot include a path or query")
            transport = "tcp" if scheme == "vnc" else "tls"
            private_uri = None
        else:
            transport = scheme
            private_uri = url
        return cls(transport, endpoint_host, endpoint_port, private_uri)

    def public(self) -> dict[str, str | int]:
        return {"transport": self.transport, "host": self.host, "port": self.port}


def _validate_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    return port


def _validate_host(host: str) -> str:
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError("host must be a valid nonempty hostname or IP address")
    if any(ord(char) <= 0x20 or ord(char) == 0x7f for char in host) or "/" in host or "\\" in host or "@" in host:
        raise ValueError("host must be a valid nonempty hostname or IP address")
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("host must be a valid nonempty hostname or IP address") from None
    if len(ascii_host) > 253:
        raise ValueError("host must be a valid nonempty hostname or IP address")
    labels = ascii_host[:-1].split(".") if ascii_host.endswith(".") else ascii_host.split(".")
    if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("host must be a valid nonempty hostname or IP address")
    return ascii_host


class ByteStream(Protocol):
    async def readexactly(self, count: int) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class _TcpByteStream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def readexactly(self, count: int) -> bytes:
        _validate_count(count)
        if self._closed:
            raise ConnectionError("transport is closed")
        return await self._reader.readexactly(count)

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("transport is closed")
        payload = _validate_data(data)
        self._writer.write(payload)
        await self._writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await self._writer.wait_closed()
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError):
            pass


class _WebSocketByteStream:
    def __init__(self, connection: object) -> None:
        self._connection = connection
        self._chunks: deque[bytes] = deque()
        self._available = 0
        self._closed = False
        self._close_called = False
        self._close_lock = asyncio.Lock()

    async def readexactly(self, count: int) -> bytes:
        _validate_count(count)
        if count == 0:
            return b""
        if self._closed:
            raise ConnectionError("transport is closed")
        while self._available < count:
            try:
                message = await self._connection.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._closed = True
                raise ConnectionError("WebSocket transport closed while reading") from None
            if isinstance(message, str):
                self._closed = True
                await self.close()
                raise TypeError("WebSocket transport accepts binary messages only")
            if not isinstance(message, bytes):
                self._closed = True
                await self.close()
                raise TypeError("WebSocket transport received an invalid message")
            if message:
                self._chunks.append(message)
                self._available += len(message)

        if self._available == count:
            result = b"".join(self._chunks)
            self._chunks.clear()
            self._available = 0
            return result

        remaining = count
        pieces: list[bytes] = []
        while remaining:
            first = self._chunks[0]
            if len(first) <= remaining:
                pieces.append(self._chunks.popleft())
                remaining -= len(first)
            else:
                pieces.append(first[:remaining])
                self._chunks[0] = first[remaining:]
                remaining = 0
        self._available -= count
        return b"".join(pieces)

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("transport is closed")
        payload = _validate_data(data)
        try:
            await self._connection.send(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._closed = True
            raise ConnectionError("WebSocket transport closed while writing") from None

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_called:
                return
            self._close_called = True
            self._closed = True
            try:
                async with asyncio.timeout(_CLOSE_TIMEOUT):
                    await self._connection.close()
            except Exception:
                # Some websockets versions signal a normal remote close as an exception.
                pass


def _validate_count(count: int) -> None:
    if type(count) is not int or count < 0 or count > MAX_STREAM_READ:
        raise ValueError("requested transport read is outside the byte limit")


def _validate_data(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("transport writes require bytes")
    payload = bytes(data)
    if len(payload) > MAX_STREAM_READ:
        raise ValueError("transport write exceeds the byte limit")
    return payload


async def open_transport(endpoint: Endpoint, timeout: float, tls_ca_file: str | None = None) -> ByteStream:
    if not isinstance(endpoint, Endpoint):
        raise TypeError("endpoint must be an Endpoint")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("transport timeout must be finite and greater than zero")
    timeout = float(timeout)

    try:
        async with asyncio.timeout(timeout):
            if endpoint.transport in {"tcp", "tls"}:
                context = _verified_ssl_context(tls_ca_file) if endpoint.transport == "tls" else None
                reader, writer = await asyncio.open_connection(
                    endpoint.host,
                    endpoint.port,
                    ssl=context,
                    server_hostname=endpoint.host if context else None,
                    limit=64 * 1024,
                )
                return _TcpByteStream(reader, writer)
            if endpoint.transport in {"ws", "wss"}:
                from websockets.asyncio.client import connect

                context = _verified_ssl_context(tls_ca_file) if endpoint.transport == "wss" else None
                uri = endpoint.uri
                if uri is None:
                    raise ValueError("WebSocket endpoint URI is unavailable")
                connection = await connect(
                    uri,
                    subprotocols=["binary"],
                    compression=None,
                    proxy=None,
                    open_timeout=timeout,
                    close_timeout=_CLOSE_TIMEOUT,
                    max_size=MAX_WEBSOCKET_MESSAGE,
                    max_queue=(4, 1),
                    write_limit=64 * 1024,
                    ssl=context,
                )
                if connection.subprotocol != "binary":
                    await connection.close()
                    raise ConnectionError("WebSocket server did not negotiate the binary subprotocol")
                return _WebSocketByteStream(connection)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise TimeoutError("transport connection timed out") from None
    except ssl.SSLCertVerificationError:
        raise ConnectionError("TLS certificate verification failed") from None
    except ssl.SSLError:
        raise ConnectionError("TLS transport handshake failed") from None
    except ConnectionRefusedError:
        raise ConnectionError("transport connection refused") from None
    except socket.gaierror:
        raise ConnectionError("transport host name resolution failed") from None
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            raise ConnectionError(f"WebSocket handshake rejected (HTTP {status})") from None
        # Exception messages may contain private proxy URLs. Keep the failure
        # category useful without returning the exception's original text.
        raise ConnectionError(f"transport connection failed ({type(exc).__name__})") from None
    raise ValueError("unsupported transport")


def _verified_ssl_context(tls_ca_file: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=tls_ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context
