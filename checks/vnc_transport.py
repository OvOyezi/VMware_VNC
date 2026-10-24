"""Behavioral checks for validated VNC transports and runtime leases."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback


CHECK_NAMES = (
    "endpoint_validation_and_public_redaction",
    "connection_errors_are_useful_without_private_uri",
    "tcp_fragmented_read_write_and_idempotent_close",
    "tls_certificate_verification_and_custom_trust",
    "websocket_binary_stream_and_close",
    "websocket_text_message_rejected",
    "wss_binary_stream_and_tls_verification",
    "runtime_reference_count_and_restart",
    "runtime_timeout_cancels_and_drains_work",
    "runtime_rejects_io_thread_calls",
)


def _endpoint_validation() -> None:
    from vmware_vnc.transport import Endpoint

    native = Endpoint.from_target("127.0.0.1", 5900)
    assert native.public() == {"transport": "tcp", "host": "127.0.0.1", "port": 5900}
    assert Endpoint.from_target(url="vnc+tls://localhost:5901/").transport == "tls"
    assert Endpoint.from_target(url="ws://localhost:80/rfb").transport == "ws"
    assert Endpoint.from_target(url="wss://localhost/rfb").port == 443
    bad_targets = (
        ("", 0, None),
        ("localhost", True, None),
        ("localhost", 65536, None),
        ("bad host", 5900, None),
        ("", 0, "https://localhost/"),
        ("", 0, "vnc://user:password@localhost:5900/"),
        ("", 0, "vnc://localhost:5900/private/path?secret=value"),
        ("", 0, "ws://localhost:80/rfb?secret=value#fragment"),
    )
    for host, port, url in bad_targets:
        try:
            Endpoint.from_target(host, port, url)
        except (TypeError, ValueError):
            continue
        raise AssertionError("invalid endpoint was accepted")

    secret_url = "wss://user:credential@example.test:443/private/path?token=do-not-print"
    try:
        Endpoint.from_target(url=secret_url)
    except ValueError as exc:
        message = str(exc) + repr(exc)
        assert all(value not in message for value in ("credential", "/private/path", "do-not-print"))
    else:
        raise AssertionError("URL credentials were accepted")
    safe = Endpoint.from_target(url="wss://example.test/private/path?token=do-not-print")
    assert "private" not in repr(safe) and "do-not-print" not in repr(safe)
    assert safe.public() == {"transport": "wss", "host": "example.test", "port": 443}


def _make_tls_files(directory: Path) -> tuple[Path, Path, ssl.SSLContext]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "localhost-cert.pem"
    key_path = directory / "localhost-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))
    return cert_path, key_path, server_context


async def _test_tcp() -> None:
    from vmware_vnc.transport import Endpoint, open_transport

    observed: dict[str, bytes] = {}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"ab")
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.write(b"cdef")
            await writer.drain()
            observed["request"] = await reader.readexactly(4)
            writer.write(b"done")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    stream = None
    try:
        port = server.sockets[0].getsockname()[1]
        stream = await open_transport(Endpoint.from_target("127.0.0.1", port), 2)
        assert await stream.readexactly(6) == b"abcdef"
        await stream.write(b"ping")
        assert await stream.readexactly(4) == b"done"
    finally:
        if stream is not None:
            await stream.close()
            await stream.close()
        server.close()
        await server.wait_closed()
    assert observed["request"] == b"ping"


async def _test_tls(cert_path: Path, server_context: ssl.SSLContext) -> None:
    from vmware_vnc.transport import Endpoint, open_transport

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"trusted")
            await writer.drain()
            await reader.read(1)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_context)
    stream = None
    port = server.sockets[0].getsockname()[1]
    try:
        endpoint = Endpoint.from_target(url=f"vnc+tls://127.0.0.1:{port}")
        stream = await open_transport(endpoint, 2, str(cert_path))
        assert await stream.readexactly(7) == b"trusted"
        await stream.close()
        stream = None

        try:
            await open_transport(endpoint, 2)
        except ConnectionError as exc:
            assert "TLS" in str(exc)
        else:
            raise AssertionError("untrusted TLS certificate was accepted")
    finally:
        if stream is not None:
            await stream.close()
        server.close()
        await server.wait_closed()


async def _test_websocket(text_mode: bool = False) -> None:
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
    from vmware_vnc.transport import Endpoint, open_transport

    observed: dict[str, bytes] = {}

    async def handler(connection: object) -> None:
        try:
            if text_mode:
                await connection.send("RFB text is invalid")
                await asyncio.sleep(0.05)
                return
            await connection.send(b"ab")
            await connection.send(b"cd")
            observed["request"] = await connection.recv()
            await connection.send(b"done")
        except ConnectionClosed:
            return

    server = await serve(handler, "127.0.0.1", 0, subprotocols=["binary"], compression=None)
    stream = None
    try:
        port = server.sockets[0].getsockname()[1]
        endpoint = Endpoint.from_target(url=f"ws://127.0.0.1:{port}/rfb?token=private")
        stream = await open_transport(endpoint, 2)
        if text_mode:
            try:
                await stream.readexactly(1)
            except TypeError as exc:
                assert "binary" in str(exc).lower()
            else:
                raise AssertionError("text WebSocket message was accepted")
        else:
            assert await stream.readexactly(3) == b"abc"
            assert await stream.readexactly(1) == b"d"
            await stream.write(b"ping")
            assert await stream.readexactly(4) == b"done"
    finally:
        if stream is not None:
            await stream.close()
            await stream.close()
        server.close()
        await server.wait_closed()
    if not text_mode:
        assert observed["request"] == b"ping"


async def _test_wss(cert_path: Path, server_context: ssl.SSLContext) -> None:
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
    from vmware_vnc.transport import Endpoint, open_transport

    async def handler(connection: object) -> None:
        try:
            await connection.send(b"secure")
            await connection.recv()
        except ConnectionClosed:
            return

    server = await serve(
        handler,
        "127.0.0.1",
        0,
        subprotocols=["binary"],
        compression=None,
        ssl=server_context,
    )
    stream = None
    port = server.sockets[0].getsockname()[1]
    try:
        endpoint = Endpoint.from_target(url=f"wss://127.0.0.1:{port}/rfb?token=private")
        try:
            await open_transport(endpoint, 2)
        except ConnectionError as exc:
            assert "TLS" in str(exc)
        else:
            raise AssertionError("untrusted WSS certificate was accepted")
        stream = await open_transport(endpoint, 2, str(cert_path))
        assert await stream.readexactly(6) == b"secure"
    finally:
        if stream is not None:
            await stream.close()
        server.close()
        await server.wait_closed()


def _test_runtime() -> None:
    from vmware_vnc.runtime import RfbRuntime

    assert RfbRuntime.info()["io_threads"] == 0
    first = RfbRuntime.acquire()
    second = RfbRuntime.acquire()
    try:
        loop_one = first.call(asyncio.sleep(0, result="ready"), 1)
        assert loop_one == "ready"
        info = first.info()
        assert info["engine"] == "native-asyncio-rfb"
        assert info["io_threads"] == 1
        assert info["lease_count"] == 2 and info["connection_count"] == 2
        first.release()
        assert first.info()["lease_count"] == 1
        assert second.call(asyncio.sleep(0, result="still-live"), 1) == "still-live"
    finally:
        first.release()
        second.release()
    assert RfbRuntime.info()["io_threads"] == 0
    assert RfbRuntime.info()["lease_count"] == 0

    restarted = RfbRuntime.acquire()
    try:
        assert restarted.call(asyncio.sleep(0, result="restarted"), 1) == "restarted"
        assert restarted.info()["io_threads"] == 1
    finally:
        restarted.release()
    assert RfbRuntime.info()["io_threads"] == 0


def _test_runtime_cancel() -> None:
    from vmware_vnc.runtime import RfbRuntime

    runtime = RfbRuntime.acquire()
    cancelled = threading.Event()
    try:
        async def pending() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        try:
            runtime.call(pending(), 0.03)
        except TimeoutError as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("runtime call did not time out")
        assert cancelled.wait(1), "timed-out coroutine cancellation was not drained"

        async def remaining_tasks() -> int:
            current = asyncio.current_task()
            return len([task for task in asyncio.all_tasks() if task is not current])

        assert runtime.call(remaining_tasks(), 1) == 0

        loop_blocked = threading.Event()
        blocker_finished = threading.Event()
        queued_ran = threading.Event()
        blocker_errors: list[BaseException] = []

        async def block_loop_briefly() -> None:
            loop_blocked.set()
            time.sleep(0.12)

        def run_blocker() -> None:
            try:
                runtime.call(block_loop_briefly(), 1)
                blocker_finished.set()
            except BaseException as exc:
                blocker_errors.append(exc)

        blocker = threading.Thread(target=run_blocker, name="vnc-runtime-blocker-check")
        blocker.start()
        assert loop_blocked.wait(1), "runtime blocker did not start"

        async def queued_work() -> None:
            queued_ran.set()

        try:
            runtime.call(queued_work(), 0.02)
        except TimeoutError:
            pass
        else:
            raise AssertionError("queued runtime call did not time out")
        blocker.join(2)
        assert not blocker.is_alive() and blocker_finished.is_set() and not blocker_errors
        assert not queued_ran.is_set(), "timed-out work ran after its caller returned"
    finally:
        runtime.release()


def _test_runtime_io_thread() -> None:
    from vmware_vnc.runtime import RfbRuntime

    runtime = RfbRuntime.acquire()
    try:
        async def try_nested_call() -> bool:
            nested = asyncio.sleep(0)
            try:
                runtime.call(nested, 1)
            except RuntimeError as exc:
                return "I/O thread" in str(exc)
            return False

        assert runtime.call(try_nested_call(), 1)
    finally:
        runtime.release()


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run_async(checks: dict[str, bool], temp_dir: Path) -> None:
    await _test_connection_errors()
    checks["connection_errors_are_useful_without_private_uri"] = True
    cert_path, _key_path, server_context = _make_tls_files(temp_dir)
    await _test_tcp()
    checks["tcp_fragmented_read_write_and_idempotent_close"] = True
    await _test_tls(cert_path, server_context)
    checks["tls_certificate_verification_and_custom_trust"] = True
    await _test_websocket()
    checks["websocket_binary_stream_and_close"] = True
    await _test_websocket(text_mode=True)
    checks["websocket_text_message_rejected"] = True
    await _test_wss(cert_path, server_context)
    checks["wss_binary_stream_and_tls_verification"] = True


async def _test_connection_errors() -> None:
    from vmware_vnc.transport import Endpoint, open_transport

    # Reserve a non-listening port so the OS rejects an actual TCP connection.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        target = Endpoint.from_target("127.0.0.1", reserved.getsockname()[1])
        try:
            await open_transport(target, 4)
        except ConnectionError as exc:
            assert str(exc) == "transport connection refused"
        else:
            raise AssertionError("non-listening TCP target was accepted")

    async def reject(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        target = Endpoint.from_target(url=f"ws://127.0.0.1:{port}/private?token=hidden-value")
        try:
            await open_transport(target, 4)
        except ConnectionError as exc:
            assert str(exc) == "WebSocket handshake rejected (HTTP 403)"
            assert "hidden-value" not in str(exc) and "/private" not in str(exc)
        else:
            raise AssertionError("HTTP rejection was accepted")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    source = args.source.resolve()
    sys.path.insert(0, str(source / "src"))
    report: dict[str, object] = {"success": False, "checks": {name: False for name in CHECK_NAMES}}
    checks = report["checks"]
    assert isinstance(checks, dict)

    try:
        _endpoint_validation()
        checks["endpoint_validation_and_public_redaction"] = True
        with tempfile.TemporaryDirectory(prefix="vnc-transport-check-") as directory:
            asyncio.run(_run_async(checks, Path(directory)))
        _test_runtime()
        checks["runtime_reference_count_and_restart"] = True
        _test_runtime_cancel()
        checks["runtime_timeout_cancels_and_drains_work"] = True
        _test_runtime_io_thread()
        checks["runtime_rejects_io_thread_calls"] = True
        report["success"] = all(checks.values())
    except BaseException as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        report["success"] = False
    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
