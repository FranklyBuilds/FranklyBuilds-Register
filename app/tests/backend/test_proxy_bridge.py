from __future__ import annotations

import asyncio
import base64

from backend.proxy_bridge import LocalProxyBridge, needs_bridge, proxy_for_browser
from backend.proxy_parser import ProxyEntry


def _socks_request(host: str, port: int) -> bytes:
    encoded = host.encode("idna")
    return b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + port.to_bytes(2, "big")


def test_http_upstream_connects_authenticated_domain_and_relays() -> None:
    async def scenario() -> None:
        seen: dict[str, bytes] = {}

        async def http_upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                seen["request"] = request
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                payload = await reader.readexactly(4)
                writer.write(payload[::-1])
                await writer.drain()
            finally:
                writer.close()

        upstream_server = await asyncio.start_server(http_upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        bridge = LocalProxyBridge(
            upstream=ProxyEntry("127.0.0.1", upstream_port, "alice", "secret", "http"),
            handshake_timeout=1,
            connect_timeout=1,
        )
        await bridge.async_start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            assert await reader.readexactly(2) == b"\x05\x00"
            writer.write(_socks_request("example.test", 443))
            await writer.drain()
            assert (await reader.readexactly(2)) == b"\x05\x00"
            await reader.readexactly(8)  # RSV, ATYP, IPv4 bind address and port
            writer.write(b"abcd")
            await writer.drain()
            assert await reader.readexactly(4) == b"dcba"
            writer.close()
            await writer.wait_closed()
        finally:
            await bridge.async_stop()
            upstream_server.close()
            await upstream_server.wait_closed()

        request = seen["request"].decode("latin-1")
        assert request.startswith("CONNECT example.test:443 HTTP/1.1\r\n")
        expected = base64.b64encode(b"alice:secret").decode("ascii")
        assert f"Proxy-Authorization: Basic {expected}\r\n" in request

    asyncio.run(scenario())


def test_authenticated_socks5_upstream_preserves_remote_domain_dns() -> None:
    async def scenario() -> None:
        seen: dict[str, object] = {}

        async def socks_upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                assert version == 5 and b"\x02" in methods
                writer.write(b"\x05\x02")
                await writer.drain()
                auth_version, user_len = await reader.readexactly(2)
                username = await reader.readexactly(user_len)
                password_len = (await reader.readexactly(1))[0]
                password = await reader.readexactly(password_len)
                seen["credentials"] = (auth_version, username, password)
                writer.write(b"\x01\x00")
                await writer.drain()
                header = await reader.readexactly(4)
                assert header == b"\x05\x01\x00\x03"
                domain_len = (await reader.readexactly(1))[0]
                seen["domain"] = (await reader.readexactly(domain_len)).decode()
                seen["port"] = int.from_bytes(await reader.readexactly(2), "big")
                writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                payload = await reader.readexactly(4)
                writer.write(payload)
                await writer.drain()
            finally:
                writer.close()

        upstream_server = await asyncio.start_server(socks_upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        bridge = LocalProxyBridge(
            upstream=ProxyEntry("127.0.0.1", upstream_port, "bob", "pw", "socks5h"),
            handshake_timeout=1,
            connect_timeout=1,
        )
        await bridge.async_start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            assert await reader.readexactly(2) == b"\x05\x00"
            writer.write(_socks_request("remote.example", 8443))
            await writer.drain()
            assert (await reader.readexactly(2)) == b"\x05\x00"
            await reader.readexactly(8)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.close()
            await writer.wait_closed()
        finally:
            await bridge.async_stop()
            upstream_server.close()
            await upstream_server.wait_closed()

        assert seen["credentials"] == (1, b"bob", b"pw")
        assert seen["domain"] == "remote.example"
        assert seen["port"] == 8443

    asyncio.run(scenario())


def test_browser_helper_bridges_http_and_keeps_plain_socks() -> None:
    plain = ProxyEntry.parse("socks5h://proxy.example:1080")
    authenticated = ProxyEntry.parse("http://user:pass@proxy.example:8080")
    assert needs_bridge(plain) is False
    assert needs_bridge(authenticated) is True
    direct, close_direct = proxy_for_browser(plain)
    assert direct == plain.url
    close_direct()
