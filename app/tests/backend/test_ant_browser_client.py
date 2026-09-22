from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import SecretStr

from backend.ant_browser_client import (
    ANT_BROWSER_LAUNCH_ARGS,
    ANT_BROWSER_WINDOW_SIZE,
    AntBrowserClient,
)
from backend.probe_store import ProxyLease


def test_ant_profile_lifecycle_uses_direct_debug_url() -> None:
    asyncio.run(_exercise_ant_profile_lifecycle())


def test_ant_fingerprint_tracks_proxy_country() -> None:
    args = AntBrowserClient._fingerprint_args(
        ProxyLease(
            id="proxy-gb",
            host="proxy.test",
            port=8080,
            username="",
            password="",
            country="GB",
            group="default",
            scheme="http",
        )
    )

    assert "--lang=en-GB" in args
    assert "--accept-lang=en-GB,en" in args
    assert "--timezone=Europe/London" in args
    assert "--fingerprint-locale=en-GB" in args
    assert "--fingerprint-timezone=Europe/London" in args
    assert f"--window-size={ANT_BROWSER_WINDOW_SIZE}" in args


def test_ant_fingerprint_supports_vietnam_proxy_country() -> None:
    args = AntBrowserClient._fingerprint_args(
        ProxyLease(
            id="proxy-vn",
            host="proxy.test",
            port=8080,
            username="",
            password="",
            country="VN",
            group="default",
            scheme="http",
        )
    )

    assert "--lang=vi-VN" in args
    assert "--timezone=Asia/Ho_Chi_Minh" in args
    assert "--fingerprint-locale=vi-VN" in args
    assert "--fingerprint-timezone=Asia/Ho_Chi_Minh" in args


def test_ant_proxy_url_brackets_ipv6_hosts() -> None:
    proxy = ProxyLease(
        id="proxy-ipv6",
        host="2001:db8::10",
        port=1080,
        username="user",
        password="p@ss",
        scheme="socks5h",
    )

    assert AntBrowserClient._proxy_url(proxy) == "socks5://user:p%40ss@[2001:db8::10]:1080"


def test_ant_can_bridge_authenticated_http_proxy() -> None:
    async def scenario() -> None:
        created: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/profiles" and request.method == "POST":
                body = json.loads(request.content)
                created.update(body["profile"])
                return httpx.Response(201, json={"ok": True, "profileId": "bridged-profile"})
            if request.url.path == "/api/profiles/bridged-profile" and request.method == "DELETE":
                return httpx.Response(200, json={"ok": True})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        async with AntBrowserClient(
            19876,
            SecretStr("secret"),
            transport=httpx.MockTransport(handler),
            proxy_bridge_mode="auto",
        ) as client:
            profile_id = await client.create_browser(
                0,
                "probe-bridge",
                ProxyLease(
                    id="proxy-bridge",
                    host="proxy.test",
                    port=8080,
                    username="user",
                    password="secret",
                    country="US",
                    group="default",
                    scheme="http",
                ),
            )
            assert profile_id == "bridged-profile"
            proxy_config = str(created["proxyConfig"])
            assert proxy_config.startswith("socks5://127.0.0.1:")
            assert "user" not in proxy_config
            await client.delete_browser(0, profile_id)
            assert client._proxy_bridges == {}

    asyncio.run(scenario())


async def _exercise_ant_profile_lifecycle() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/profiles" and request.method == "POST":
            body = json.loads(request.content)
            assert body["profile"]["proxyConfig"] == "http://user:p%40ss@proxy.test:8080"
            fingerprint_args = body["profile"]["fingerprintArgs"]
            assert "--lang=en-US" in fingerprint_args
            assert "--timezone=America/New_York" in fingerprint_args
            assert f"--window-size={ANT_BROWSER_WINDOW_SIZE}" in fingerprint_args
            assert any(value.startswith("--fingerprint=") for value in fingerprint_args)
            assert body["profile"]["launchArgs"] == list(ANT_BROWSER_LAUNCH_ARGS)
            return httpx.Response(201, json={"ok": True, "profileId": "profile-1"})
        if request.url.path == "/api/runtime/session":
            body = json.loads(request.content)
            assert body["launchArgs"] == list(ANT_BROWSER_LAUNCH_ARGS)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "profileId": "profile-1",
                    "debugReady": True,
                    "directDebugUrl": "http://127.0.0.1:9333",
                    "pid": 42,
                },
            )
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"ok": True, "stopped": True})
        if request.url.path == "/api/profiles/profile-1" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True, "deleted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with AntBrowserClient(
        19876,
        SecretStr("secret"),
        transport=httpx.MockTransport(handler),
        proxy_bridge_mode="never",
    ) as client:
        await client.health()
        profile_id = await client.create_browser(
            0,
            "probe-12345678",
            ProxyLease(
                id="proxy-1",
                host="proxy.test",
                port=8080,
                username="user",
                password="p@ss",
                country="US",
                group="default",
                scheme="http",
            ),
        )
        opened = await client.open_browser(0, profile_id, headless=False)
        assert opened.ws == "http://127.0.0.1:9333"
        assert opened.pid == 42
        await client.close_browser(profile_id)
        await client.delete_browser(0, profile_id)

    assert all(request.headers.get("X-Ant-Api-Key") == "secret" for request in requests)
