from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from backend.protocol_registration import (
    AutoSentinelProvider,
    FakeTransport,
    HttpSentinelProvider,
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationService,
    QuickJsSentinelProvider,
    SentinelData,
)


def _request(index: int = 0) -> ProtocolRegistrationRequest:
    return ProtocolRegistrationRequest(
        f"sentinel-{index}@example.test",
        device_id=f"device-{index}",
    )


def test_auto_sentinel_prefers_quickjs_without_calling_http() -> None:
    events: list[str] = []

    class QuickJsProvider:
        async def get(self, *_args: Any) -> SentinelData:
            events.append("quickjs")
            return SentinelData(sentinel_token="quickjs-token", oai_did="device-0")

    class HttpProvider:
        async def get(self, *_args: Any) -> SentinelData:
            events.append("http")
            return SentinelData(sentinel_token="http-token", oai_did="device-0")

    provider = AutoSentinelProvider(
        mode="auto",
        quickjs_provider=QuickJsProvider(),
        http_provider=HttpProvider(),
    )
    result = asyncio.run(
        provider.get(_request(), ProtocolRegistrationConfig(), FakeTransport())
    )

    assert result.sentinel_token == "quickjs-token"
    assert events == ["quickjs"]


def test_auto_sentinel_falls_back_to_http_after_quickjs_failure() -> None:
    events: list[str] = []

    class QuickJsProvider:
        async def get(self, *_args: Any) -> SentinelData:
            events.append("quickjs")
            raise RuntimeError("sdk unavailable")

    class HttpProvider:
        async def get(self, *_args: Any) -> SentinelData:
            events.append("http")
            return SentinelData(sentinel_token="http-token", oai_did="device-0")

    provider = AutoSentinelProvider(
        mode="auto",
        quickjs_provider=QuickJsProvider(),
        http_provider=HttpProvider(),
    )
    result = asyncio.run(
        provider.get(_request(), ProtocolRegistrationConfig(), FakeTransport())
    )

    assert result.sentinel_token == "http-token"
    assert events == ["quickjs", "http"]


def test_shared_auto_sentinel_provider_limits_concurrency_to_two() -> None:
    async def scenario() -> int:
        release = asyncio.Event()
        two_started = asyncio.Event()

        class BlockingProvider:
            def __init__(self) -> None:
                self.active = 0
                self.maximum_active = 0

            async def get(
                self,
                request: ProtocolRegistrationRequest,
                *_args: Any,
            ) -> SentinelData:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                if self.active == 2:
                    two_started.set()
                try:
                    await release.wait()
                    return SentinelData(
                        sentinel_token=f"token-{request.email}",
                        oai_did=request.device_id or "",
                    )
                finally:
                    self.active -= 1

        quickjs = BlockingProvider()
        provider = AutoSentinelProvider(
            mode="quickjs",
            quickjs_provider=quickjs,
            http_provider=quickjs,
        )
        config = ProtocolRegistrationConfig()
        tasks = [
            asyncio.create_task(provider.get(_request(index), config, FakeTransport()))
            for index in range(6)
        ]

        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert quickjs.active == 2
        release.set()
        results = await asyncio.gather(*tasks)

        assert len(results) == 6
        return quickjs.maximum_active

    assert asyncio.run(scenario()) == 2


def test_quickjs_provider_reuses_cached_sdk_across_instances(tmp_path: Path) -> None:
    cache_dir = tmp_path / "sentinel-cache"
    transport = FakeTransport(
        [{"status_code": 200, "text": "globalThis.SentinelSDK = {};"}]
    )
    config = ProtocolRegistrationConfig()
    first = QuickJsSentinelProvider(cache_dir=cache_dir)
    second = QuickJsSentinelProvider(cache_dir=cache_dir)

    async def scenario() -> tuple[Path, Path, Path]:
        first_path, duplicate_path = await asyncio.gather(
            first._ensure_sdk_file(config, transport),
            first._ensure_sdk_file(config, transport),
        )
        second_path = await second._ensure_sdk_file(config, transport)
        return first_path, duplicate_path, second_path

    first_path, duplicate_path, second_path = asyncio.run(scenario())

    assert first_path == duplicate_path == second_path == cache_dir / "sdk.js"
    assert first_path.read_text(encoding="utf-8") == "globalThis.SentinelSDK = {};"
    assert [(call.method, call.url) for call in transport.calls] == [
        (
            "GET",
            "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
        )
    ]


def test_quickjs_provider_constructs_tokens_for_all_registration_flows(tmp_path: Path) -> None:
    cache_dir = tmp_path / "sentinel-cache"
    cache_dir.mkdir()
    (cache_dir / "sdk.js").write_text("cached sdk", encoding="utf-8")
    adapter_file = tmp_path / "adapter.js"
    adapter_file.write_text("// fixture", encoding="utf-8")
    actions: list[tuple[str, dict[str, Any]]] = []
    requirements_count = 0

    def action_runner(*, action: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        nonlocal requirements_count
        actions.append((action, payload))
        if action == "requirements":
            requirements_count += 1
            return {"request_p": f"requirements-{requirements_count}"}
        challenge_token = str(payload["challenge"]["token"])
        return {
            "final_p": f"final-{challenge_token}",
            "t": f"proof-{challenge_token}",
            "so": f"observer-{challenge_token.removeprefix('challenge-')}",
        }

    class CookieTransport(FakeTransport):
        def cookie_header(self) -> str:
            return "sentinel-session=fixture"

    transport = CookieTransport(
        [
            {
                "status_code": 200,
                "body": {"token": "challenge-authorize"},
            },
            {"status_code": 200, "body": {"token": "challenge-username"}},
            {
                "status_code": 200,
                "body": {"token": "challenge-oauth"},
            },
        ]
    )
    provider = QuickJsSentinelProvider(
        adapter_file=adapter_file,
        cache_dir=cache_dir,
        action_runner=action_runner,
    )

    result = asyncio.run(
        provider.get(_request(), ProtocolRegistrationConfig(), transport)
    )

    assert json.loads(result.sentinel_authorize_token) == {
        "p": "final-challenge-authorize",
        "t": "proof-challenge-authorize",
        "c": "challenge-authorize",
        "id": "device-0",
        "flow": "authorize_continue",
    }
    assert json.loads(result.sentinel_token) == {
        "p": "final-challenge-username",
        "t": "proof-challenge-username",
        "c": "challenge-username",
        "id": "device-0",
        "flow": "username_password_create",
    }
    assert json.loads(result.sentinel_oauth_token) == {
        "p": "final-challenge-oauth",
        "t": "proof-challenge-oauth",
        "c": "challenge-oauth",
        "id": "device-0",
        "flow": "oauth_create_account",
    }
    assert json.loads(result.sentinel_so_token) == {
        "so": "observer-oauth",
        "c": "challenge-oauth",
        "id": "device-0",
        "flow": "oauth_create_account",
    }
    assert json.loads(result.sentinel_authorize_so_token) == {
        "so": "observer-authorize",
        "c": "challenge-authorize",
        "id": "device-0",
        "flow": "authorize_continue",
    }
    assert json.loads(result.sentinel_username_so_token) == {
        "so": "observer-username",
        "c": "challenge-username",
        "id": "device-0",
        "flow": "username_password_create",
    }
    assert result.cookie_str == "sentinel-session=fixture"
    assert [action for action, _payload in actions] == [
        "requirements",
        "solve",
        "requirements",
        "solve",
        "requirements",
        "solve",
    ]
    request_bodies = [json.loads(str(call.kwargs["data"])) for call in transport.calls]
    assert request_bodies == [
        {
            "p": "requirements-1",
            "id": "device-0",
            "flow": "authorize_continue",
        },
        {
            "p": "requirements-2",
            "id": "device-0",
            "flow": "username_password_create",
        },
        {
            "p": "requirements-3",
            "id": "device-0",
            "flow": "oauth_create_account",
        },
    ]


def test_quickjs_provider_accepts_pow_only_challenge_without_turnstile(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "sentinel-cache"
    cache_dir.mkdir()
    (cache_dir / "sdk.js").write_text("cached sdk", encoding="utf-8")
    adapter_file = tmp_path / "adapter.js"
    adapter_file.write_text("// fixture", encoding="utf-8")

    def action_runner(
        *, action: str, payload: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        if action == "requirements":
            return {"request_p": "requirements"}
        return {"final_p": "final-proof", "t": ""}

    provider = QuickJsSentinelProvider(
        adapter_file=adapter_file,
        cache_dir=cache_dir,
        action_runner=action_runner,
    )
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"token": f"challenge-{index}"}}
            for index in range(3)
        ]
    )

    result = asyncio.run(
        provider.get(_request(), ProtocolRegistrationConfig(), transport)
    )

    assert json.loads(result.sentinel_token)["t"] == ""
    assert json.loads(result.sentinel_oauth_token)["t"] == ""


def test_quickjs_provider_ignores_observer_metadata_without_solved_token(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "sentinel-cache"
    cache_dir.mkdir()
    (cache_dir / "sdk.js").write_text("cached sdk", encoding="utf-8")
    adapter_file = tmp_path / "adapter.js"
    adapter_file.write_text("// fixture", encoding="utf-8")

    def action_runner(
        *, action: str, payload: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        if action == "requirements":
            return {"request_p": "requirements"}
        return {"final_p": "final-proof", "t": "proof"}

    provider = QuickJsSentinelProvider(
        adapter_file=adapter_file,
        cache_dir=cache_dir,
        action_runner=action_runner,
    )
    transport = FakeTransport(
        [
            {
                "status_code": 200,
                "body": {
                    "token": "challenge-authorize",
                    "so": {"required": True, "seed": "metadata"},
                },
            },
            {
                "status_code": 200,
                "body": {
                    "token": "challenge-username",
                    "so": "raw-challenge-value",
                },
            },
            {"status_code": 200, "body": {"token": "challenge-oauth"}},
        ]
    )

    result = asyncio.run(
        provider.get(_request(), ProtocolRegistrationConfig(), transport)
    )

    assert result.sentinel_authorize_so_token == ""
    assert result.sentinel_username_so_token == ""
    assert result.sentinel_so_token == ""


def test_http_provider_does_not_fabricate_session_observer_tokens() -> None:
    transport = FakeTransport(
        [
            {
                "status_code": 200,
                "body": {
                    "token": f"challenge-{index}",
                    "so": "raw-challenge-value",
                },
            }
            for index in range(3)
        ]
    )

    result = asyncio.run(
        HttpSentinelProvider().get(
            _request(), ProtocolRegistrationConfig(), transport
        )
    )

    assert result.sentinel_authorize_so_token == ""
    assert result.sentinel_username_so_token == ""
    assert result.sentinel_so_token == ""


def test_passwordless_transport_does_not_import_sentinel_auth_cookies() -> None:
    class CookieRecordingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cookies: list[tuple[str, str, str | None, str]] = []

        def set_cookie(
            self,
            name: str,
            value: str,
            *,
            domain: str | None = None,
            path: str = "/",
        ) -> None:
            self.cookies.append((name, value, domain, path))

    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(registration_mode="passwordless")
    )
    transport = CookieRecordingTransport()
    sentinel = SentinelData(
        sentinel_token="token",
        cookie_str="legacy-auth=session; oai-did=stale-device",
        oai_did="fresh-device",
    )

    asyncio.run(
        service._prime_transport(
            transport,
            sentinel,
            "fresh-device",
            "passwordless",
        )
    )

    assert transport.cookies == [
        ("oai-did", "fresh-device", ".openai.com", "/")
    ]


def test_password_transport_does_not_import_stale_sentinel_did() -> None:
    class CookieRecordingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cookies: list[tuple[str, str, str | None, str]] = []

        def set_cookie(
            self,
            name: str,
            value: str,
            *,
            domain: str | None = None,
            path: str = "/",
        ) -> None:
            self.cookies.append((name, value, domain, path))

    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(registration_mode="password")
    )
    transport = CookieRecordingTransport()

    asyncio.run(
        service._prime_transport(
            transport,
            SentinelData(
                cookie_str="oai-did=stale-device; sentinel-session=session-value",
                oai_did="stale-device",
            ),
            "fresh-device",
            "password",
        )
    )

    assert transport.cookies == [
        ("oai-did", "fresh-device", ".openai.com", "/"),
        ("sentinel-session", "session-value", "auth.openai.com", "/"),
    ]
