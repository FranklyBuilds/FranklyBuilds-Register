"""Local SOCKS5 bridge for browser-facing proxy configuration.

The resource store accepts both HTTP and SOCKS5 proxy nodes, while browser
launchers generally need one unauthenticated SOCKS endpoint.  This module
provides that small adapter without an external proxy manager: a local SOCKS5
client is authenticated against, and then tunnelled through, one configured
HTTP(S) or SOCKS5(S) upstream.

Only the CONNECT command is intentionally implemented.  DNS names received
from the local client are forwarded as SOCKS5 domain addresses, so the
upstream performs name resolution (the ``socks5h`` behaviour) and no local
DNS lookup is introduced for the destination.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import socket
import ssl
import struct
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .proxy_parser import ProxyEntry, parse_proxy_entry

logger = logging.getLogger("proxy_bridge")

# SOCKS5 protocol constants (RFC 1928 / RFC 1929).
_SOCKS5_VER = 0x05
_NO_AUTH = 0x00
_USERPASS = 0x02
_NO_ACCEPTABLE = 0xFF
_CMD_CONNECT = 0x01
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04
_REP_SUCCEEDED = 0x00
_REP_GENERAL_FAILURE = 0x01
_REP_CONN_REFUSED = 0x05
_REP_CMD_NOT_SUPPORTED = 0x07
_REP_ADDR_NOT_SUPPORTED = 0x08
_HTTP_SCHEMES = frozenset({"http", "https"})
BRIDGE_MODE_ENV = "AUTOREGISTER_PROXY_BRIDGE"
BRIDGE_MODES = frozenset({"auto", "always", "never"})


async def _read_exact(reader: asyncio.StreamReader, size: int) -> bytes:
    if size < 0:
        raise ValueError("read size must be non-negative")
    try:
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("connection closed during proxy handshake") from exc


async def _read_socks5_addr(
    reader: asyncio.StreamReader, address_type: int
) -> tuple[str, int]:
    """Read a SOCKS5 address and return ``(host, port)``."""

    if address_type == _ATYP_IPV4:
        host = socket.inet_ntoa(await _read_exact(reader, 4))
    elif address_type == _ATYP_IPV6:
        host = socket.inet_ntop(socket.AF_INET6, await _read_exact(reader, 16))
    elif address_type == _ATYP_DOMAIN:
        length = (await _read_exact(reader, 1))[0]
        if not length:
            raise ValueError("empty SOCKS5 domain")
        raw_host = await _read_exact(reader, length)
        try:
            host = raw_host.decode("idna")
        except UnicodeError as exc:
            raise ValueError("invalid SOCKS5 domain") from exc
    else:
        raise ValueError(f"unsupported SOCKS5 address type: {address_type:#x}")

    port = struct.unpack("!H", await _read_exact(reader, 2))[0]
    return host, port


def _encode_socks5_addr(host: str, port: int) -> bytes:
    """Encode an address for a SOCKS5 reply or upstream request."""

    value = str(host or "")
    try:
        packed = socket.inet_pton(socket.AF_INET, value)
    except OSError:
        try:
            packed = socket.inet_pton(socket.AF_INET6, value.split("%", 1)[0])
        except OSError:
            try:
                packed = value.encode("idna")
            except UnicodeError as exc:
                raise ValueError("invalid SOCKS5 domain") from exc
            if not 1 <= len(packed) <= 255:
                raise ValueError("SOCKS5 domain is too long")
            return bytes([_ATYP_DOMAIN, len(packed)]) + packed + struct.pack(
                "!H", int(port)
            )
        return bytes([_ATYP_IPV6]) + packed + struct.pack("!H", int(port))
    return bytes([_ATYP_IPV4]) + packed + struct.pack("!H", int(port))


def _build_socks5_reply(
    reply: int, bind_host: str = "0.0.0.0", bind_port: int = 0
) -> bytes:
    return bytes([_SOCKS5_VER, reply, 0x00]) + _encode_socks5_addr(
        bind_host, bind_port
    )


def _format_authority(host: str, port: int) -> str:
    """Format an HTTP CONNECT authority, including IPv6 brackets."""

    value = str(host or "")
    if ":" not in value:
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            value = value.encode("idna").decode("ascii")
    if ":" in value and not value.startswith("["):
        value = f"[{value}]"
    return f"{value}:{int(port)}"


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(
        f"{username}:{password}".encode("utf-8", errors="replace")
    ).decode("ascii")
    return token


def _coerce_entry(value: Any) -> ProxyEntry | None:
    """Normalize parser entries and lease-like objects for public helpers."""

    if value is None:
        return None
    if isinstance(value, ProxyEntry):
        return value
    if isinstance(value, str):
        return parse_proxy_entry(value)

    # ProxyLease and similar records expose the split fields used by the
    # resource service.  Keep this adapter duck-typed to avoid an import cycle.
    host = getattr(value, "host", None)
    port = getattr(value, "port", None)
    if host is None or port is None:
        return None
    return ProxyEntry(
        host=str(host),
        port=int(port),
        username=str(getattr(value, "username", "") or ""),
        password=str(getattr(value, "password", "") or ""),
        scheme=str(getattr(value, "scheme", "http") or "http"),
    )


def configured_bridge_mode(value: str | None = None) -> str:
    """Return the validated browser bridge mode.

    ``auto`` bridges browser-incompatible authenticated or HTTP(S) entries,
    plus ``socks5h`` entries whose remote-DNS semantics should be preserved,
    for Ant.  Ordinary unauthenticated ``socks5`` records stay native.  This
    avoids Chromium's inconsistent handling of credentials and the
    ``socks5h`` spelling.  ``always`` has the same decision for supported
    incompatible schemes and is useful as an explicit operational setting.
    """

    candidate = value if value is not None else os.environ.get(BRIDGE_MODE_ENV, "auto")
    normalized = str(candidate or "").strip().lower()
    return normalized if normalized in BRIDGE_MODES else "auto"


def should_bridge_proxy(
    proxy: Any,
    *,
    browser: str = "ant",
    mode: str | None = None,
) -> bool:
    """Return whether a browser profile should use a local bridge."""

    try:
        entry = _coerce_entry(proxy)
    except (TypeError, ValueError):
        return False
    if entry is None or configured_bridge_mode(mode) == "never":
        return False
    selected = configured_bridge_mode(mode)
    if selected == "always":
        return needs_bridge(entry) or entry.scheme == "socks5h"
    if str(browser).strip().lower() != "ant":
        return False
    return needs_bridge(entry) or entry.scheme == "socks5h"


@dataclass
class LocalProxyBridge:
    """Expose one upstream proxy as a local, unauthenticated SOCKS5 server.

    ``async_start`` and ``async_stop`` run entirely on the caller's event
    loop.  ``start``/``stop`` are provided as a convenience for synchronous
    browser launchers and use a private background event loop.
    """

    upstream: ProxyEntry | str | Any | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    connect_timeout: float = 10.0
    handshake_timeout: float = 10.0
    pipe_buf_size: int = 65536
    tls_context: ssl.SSLContext | None = None
    verify_upstream_tls: bool = True

    _server: asyncio.Server | None = field(default=None, init=False, repr=False)
    _port: int = field(default=0, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _active_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False, repr=False)
    _state_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.upstream = _coerce_entry(self.upstream)
        if self.listen_port < 0 or self.listen_port > 65535:
            raise ValueError("listen_port must be between 0 and 65535")
        if self.connect_timeout <= 0 or self.handshake_timeout <= 0:
            raise ValueError("proxy bridge timeouts must be positive")
        if self.pipe_buf_size <= 0:
            raise ValueError("pipe_buf_size must be positive")

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def local_url(self) -> str:
        if not self._port:
            raise RuntimeError("proxy_bridge: not started")
        host = self.listen_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"socks5h://{host}:{self._port}"

    async def async_start(self) -> int:
        """Start the bridge on the current running event loop and return port."""

        if self._server is not None:
            return self._port
        if self.upstream is None:
            raise RuntimeError("proxy_bridge: no upstream proxy configured")
        loop = asyncio.get_running_loop()
        self._loop = loop
        try:
            self._server = await asyncio.start_server(
                self._on_client,
                self.listen_host,
                self.listen_port,
                limit=max(65536, self.pipe_buf_size),
            )
            sockets = self._server.sockets or []
            if not sockets:
                raise RuntimeError("proxy_bridge: listener did not expose a socket")
            self._port = int(sockets[0].getsockname()[1])
        except Exception:
            self._server = None
            self._loop = None
            self._port = 0
            raise
        logger.info(
            "proxy_bridge listening on %s -> upstream %s",
            self.local_url,
            self.upstream.masked,
        )
        return self._port

    async def async_stop(self) -> None:
        """Close the listener and terminate active tunnels."""

        server = self._server
        if server is not None:
            server.close()

        # Closing client writers wakes relay tasks; then give them a short,
        # bounded drain period before cancellation.
        for writer in tuple(self._writers):
            writer.close()
        active = tuple(
            task
            for task in self._active_tasks
            if task is not asyncio.current_task()
        )
        # A read blocked in a platform transport is not guaranteed to wake up
        # merely because its writer was closed (notably on Windows Proactor),
        # so cancel client tasks before awaiting their completion.
        for task in active:
            if not task.done():
                task.cancel()
        if active:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    asyncio.gather(*active, return_exceptions=True), timeout=2.0
                )
        if server is not None:
            # ``wait_closed`` can wait on a platform accept operation even
            # after ``close`` (Windows Proactor); keep shutdown bounded.
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(server.wait_closed(), timeout=1.0)

        self._server = None
        self._loop = None
        self._port = 0
        self._active_tasks.clear()
        self._writers.clear()
        logger.info("proxy_bridge stopped")

    async def __aenter__(self) -> "LocalProxyBridge":
        await self.async_start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.async_stop()

    # Synchronous convenience lifecycle.  It deliberately owns only the
    # private loop it creates; async callers should use async_start/stop.
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> int:
        if self._server is not None:
            return self._port
        if self.upstream is None:
            raise RuntimeError("proxy_bridge: no upstream proxy configured")

        ready = threading.Event()
        failure: list[BaseException] = []

        requested_loop = loop

        def runner() -> None:
            loop = requested_loop or asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self.async_start())
            except BaseException as exc:  # pragma: no cover - startup failure path
                failure.append(exc)
                ready.set()
                loop.close()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(self.async_stop())
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
                self._thread = None

        thread = threading.Thread(target=runner, name="proxy-bridge", daemon=True)
        self._thread = thread
        thread.start()
        ready.wait(timeout=max(5.0, self.connect_timeout))
        if failure:
            self._thread = None
            raise failure[0]
        if not self._port:
            self._thread = None
            raise RuntimeError("proxy_bridge: startup timed out")
        return self._port

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        if loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self.async_stop(), loop)
                future.result(timeout=5.0)
            except Exception as exc:  # pragma: no cover - defensive cleanup
                logger.debug("proxy_bridge stop error: %s", exc)
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        # If a caller invokes stop after the loop has already exited, close it
        # here.  The normal runner path has already reset the state.
        if not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.close()
        self._loop = None
        self._server = None
        self._port = 0
        self._thread = None

    def __enter__(self) -> "LocalProxyBridge":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        self._writers.add(writer)
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("proxy_bridge client error: %s", exc)
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._active_tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        timeout = self.handshake_timeout
        try:
            version, method_count = await asyncio.wait_for(
                _read_exact(reader, 2), timeout=timeout
            )
            methods = await asyncio.wait_for(
                _read_exact(reader, method_count), timeout=timeout
            )
        except (asyncio.TimeoutError, ConnectionError):
            return
        if version != _SOCKS5_VER:
            return
        if _NO_AUTH not in methods:
            writer.write(bytes([_SOCKS5_VER, _NO_ACCEPTABLE]))
            await writer.drain()
            return
        writer.write(bytes([_SOCKS5_VER, _NO_AUTH]))
        await writer.drain()

        try:
            request = await asyncio.wait_for(_read_exact(reader, 4), timeout=timeout)
        except (asyncio.TimeoutError, ConnectionError):
            return
        version, command, _reserved, address_type = request
        if version != _SOCKS5_VER:
            return
        if command != _CMD_CONNECT:
            writer.write(_build_socks5_reply(_REP_CMD_NOT_SUPPORTED))
            await writer.drain()
            return
        try:
            dest_host, dest_port = await asyncio.wait_for(
                _read_socks5_addr(reader, address_type), timeout=timeout
            )
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            writer.write(_build_socks5_reply(_REP_ADDR_NOT_SUPPORTED))
            await writer.drain()
            return

        try:
            up_reader, up_writer = await asyncio.wait_for(
                self._connect_upstream(dest_host, dest_port, address_type),
                timeout=self.connect_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "proxy_bridge upstream connect failed for %s:%s via %s (%s)",
                dest_host,
                dest_port,
                self.upstream.masked if self.upstream else "<none>",
                type(exc).__name__,
            )
            writer.write(_build_socks5_reply(_REP_CONN_REFUSED))
            with contextlib.suppress(Exception):
                await writer.drain()
            return

        writer.write(_build_socks5_reply(_REP_SUCCEEDED))
        await writer.drain()
        try:
            await self._relay(reader, writer, up_reader, up_writer)
        finally:
            up_writer.close()
            with contextlib.suppress(Exception):
                await up_writer.wait_closed()

    async def _connect_upstream(
        self, dest_host: str, dest_port: int, dest_atyp: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        upstream = self.upstream
        if upstream is None:
            raise RuntimeError("proxy_bridge: no upstream proxy configured")
        scheme = upstream.scheme.lower()
        if scheme in _HTTP_SCHEMES:
            return await self._connect_http_upstream(upstream, dest_host, dest_port)
        if scheme not in {"socks5", "socks5h"}:
            raise ValueError(f"unsupported upstream scheme: {scheme}")
        return await self._connect_socks_upstream(
            upstream, dest_host, dest_port, dest_atyp
        )

    async def _connect_through_upstream(
        self,
        upstream: Any,
        dest_host: str,
        dest_port: int,
        dest_atyp: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Compatibility wrapper used by the installer bridge implementation."""

        entry = _coerce_entry(upstream)
        if entry is None:
            raise ValueError("invalid upstream proxy")
        # ``proxy_pool.UpstreamProxy`` predates scheme-aware entries and is
        # inherently SOCKS5; preserve that historical call contract.
        if not hasattr(upstream, "scheme"):
            entry = ProxyEntry(
                host=entry.host,
                port=entry.port,
                username=entry.username,
                password=entry.password,
                scheme="socks5",
            )
        if entry.scheme.lower() in _HTTP_SCHEMES:
            return await self._connect_http_upstream(entry, dest_host, dest_port)
        return await self._connect_socks_upstream(entry, dest_host, dest_port, dest_atyp)

    async def _open_upstream(
        self, upstream: ProxyEntry, *, tls: bool = False
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        kwargs: dict[str, Any] = {}
        if tls:
            context = self.tls_context
            if context is None:
                context = (
                    ssl.create_default_context()
                    if self.verify_upstream_tls
                    else ssl._create_unverified_context()
                )
            kwargs["ssl"] = context
            kwargs["server_hostname"] = upstream.host.split("%", 1)[0]
        return await asyncio.open_connection(upstream.host, upstream.port, **kwargs)

    async def _connect_socks_upstream(
        self,
        upstream: ProxyEntry,
        dest_host: str,
        dest_port: int,
        dest_atyp: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self._open_upstream(upstream)
        try:
            has_credentials = bool(upstream.username or upstream.password)
            methods = bytes([_NO_AUTH, _USERPASS]) if has_credentials else bytes([_NO_AUTH])
            writer.write(bytes([_SOCKS5_VER, len(methods)]) + methods)
            await writer.drain()
            response = await asyncio.wait_for(
                _read_exact(reader, 2), timeout=self.handshake_timeout
            )
            if response[0] != _SOCKS5_VER:
                raise ConnectionError("upstream is not SOCKS5")
            method = response[1]
            if method == _USERPASS:
                user = upstream.username.encode("utf-8")
                password = upstream.password.encode("utf-8")
                if len(user) > 255 or len(password) > 255:
                    raise ValueError("SOCKS5 credentials are too long")
                writer.write(
                    bytes([0x01, len(user)])
                    + user
                    + bytes([len(password)])
                    + password
                )
                await writer.drain()
                auth_response = await asyncio.wait_for(
                    _read_exact(reader, 2), timeout=self.handshake_timeout
                )
                if auth_response[0] != 0x01 or auth_response[1] != 0x00:
                    raise ConnectionError("upstream SOCKS5 authentication failed")
            elif method != _NO_AUTH:
                raise ConnectionError(f"upstream SOCKS5 rejected auth method {method:#x}")

            request = bytearray([_SOCKS5_VER, _CMD_CONNECT, 0x00, dest_atyp])
            if dest_atyp == _ATYP_DOMAIN:
                encoded = dest_host.encode("idna")
                if not 1 <= len(encoded) <= 255:
                    raise ValueError("destination domain is too long")
                request.append(len(encoded))
                request.extend(encoded)
            elif dest_atyp == _ATYP_IPV4:
                request.extend(socket.inet_pton(socket.AF_INET, dest_host))
            elif dest_atyp == _ATYP_IPV6:
                request.extend(socket.inet_pton(socket.AF_INET6, dest_host.split("%", 1)[0]))
            else:
                raise ValueError(f"unsupported destination address type {dest_atyp:#x}")
            request.extend(struct.pack("!H", int(dest_port)))
            writer.write(bytes(request))
            await writer.drain()

            reply = await asyncio.wait_for(
                _read_exact(reader, 4), timeout=self.handshake_timeout
            )
            if reply[0] != _SOCKS5_VER or reply[1] != _REP_SUCCEEDED:
                raise ConnectionError(f"upstream SOCKS5 CONNECT failed ({reply[1]:#x})")
            await asyncio.wait_for(
                self._consume_socks5_addr(reader, reply[3]),
                timeout=self.handshake_timeout,
            )
            return reader, writer
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    async def _consume_socks5_addr(
        self, reader: asyncio.StreamReader, address_type: int
    ) -> None:
        if address_type == _ATYP_IPV4:
            await _read_exact(reader, 4)
        elif address_type == _ATYP_IPV6:
            await _read_exact(reader, 16)
        elif address_type == _ATYP_DOMAIN:
            length = (await _read_exact(reader, 1))[0]
            await _read_exact(reader, length)
        else:
            raise ConnectionError("invalid upstream SOCKS5 bind address")
        await _read_exact(reader, 2)

    async def _connect_http_upstream(
        self, upstream: ProxyEntry, dest_host: str, dest_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self._open_upstream(
            upstream, tls=upstream.scheme.lower() == "https"
        )
        authority = _format_authority(dest_host, dest_port)
        headers = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: Keep-Alive",
        ]
        if upstream.username or upstream.password:
            headers.append(
                f"Proxy-Authorization: Basic {_basic_auth(upstream.username, upstream.password)}"
            )
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii", errors="replace"))
        await writer.drain()
        try:
            status_line = await asyncio.wait_for(
                reader.readline(), timeout=self.handshake_timeout
            )
            if not status_line or len(status_line) > 8192:
                raise ConnectionError("upstream HTTP proxy returned no status")
            parts = status_line.decode("latin-1", errors="replace").strip().split(None, 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise ConnectionError("invalid upstream HTTP proxy status")
            status = int(parts[1])
            header_bytes = len(status_line)
            while True:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=self.handshake_timeout
                )
                header_bytes += len(line)
                if header_bytes > 131072:
                    raise ConnectionError("upstream HTTP proxy headers are too large")
                if line in (b"\r\n", b"\n", b""):
                    break
            if not 200 <= status < 300:
                raise ConnectionError(f"HTTP CONNECT failed with status {status}")
            return reader, writer
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pipe(
            source: asyncio.StreamReader, target: asyncio.StreamWriter
        ) -> None:
            try:
                while True:
                    data = await source.read(self.pipe_buf_size)
                    if not data:
                        return
                    target.write(data)
                    await target.drain()
            except (asyncio.CancelledError, ConnectionError, OSError):
                return
            finally:
                with contextlib.suppress(Exception):
                    if target.can_write_eof():
                        target.write_eof()

        first = asyncio.create_task(pipe(client_reader, upstream_writer))
        second = asyncio.create_task(pipe(upstream_reader, client_writer))
        try:
            await asyncio.gather(first, second, return_exceptions=True)
        finally:
            for task in (first, second):
                if not task.done():
                    task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(first, second, return_exceptions=True)


def needs_bridge(proxy: Any) -> bool:
    """Return whether a proxy needs conversion for a browser SOCKS setting."""

    try:
        entry = _coerce_entry(proxy)
    except (TypeError, ValueError):
        return False
    if entry is None:
        return False
    return bool(entry.username or entry.password) or entry.scheme in _HTTP_SCHEMES


def proxy_for_browser(
    proxy: Any, *, env_prefix: str = "PROXY"
) -> tuple[str, Callable[[], None]]:
    """Return ``(browser_proxy_url, closer)`` for synchronous callers."""

    del env_prefix  # retained for compatibility with the standalone tool API
    try:
        entry = _coerce_entry(proxy)
    except (TypeError, ValueError):
        entry = None
    if entry is None:
        return "", _noop
    if not needs_bridge(entry):
        return entry.url, _noop
    bridge = LocalProxyBridge(upstream=entry)
    bridge.start()
    return bridge.local_url, bridge.stop


async def async_proxy_for_browser(
    proxy: Any,
    *,
    env_prefix: str = "PROXY",
) -> tuple[str, Callable[[], Awaitable[None]]]:
    """Async counterpart to :func:`proxy_for_browser`."""

    del env_prefix  # retained for compatibility with the standalone tool API
    try:
        entry = _coerce_entry(proxy)
    except (TypeError, ValueError):
        entry = None
    if entry is None:
        return "", _noop_async
    if not needs_bridge(entry):
        return entry.url, _noop_async
    bridge = LocalProxyBridge(upstream=entry)
    await bridge.async_start()
    return bridge.local_url, bridge.async_stop


def _noop() -> None:
    return None


async def _noop_async() -> None:
    return None


_basic_auth_header = _basic_auth


# Keep the installer terminology available to callers that prefer a clearly
# browser-specific name.
browser_proxy = proxy_for_browser


__all__ = [
    "BRIDGE_MODE_ENV",
    "BRIDGE_MODES",
    "LocalProxyBridge",
    "async_proxy_for_browser",
    "browser_proxy",
    "configured_bridge_mode",
    "needs_bridge",
    "proxy_for_browser",
    "should_bridge_proxy",
]
