from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.protocol_registration import (
    FakeTransport,
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationResult,
    ProtocolRegistrationService,
    StaticOtpProvider,
    StaticSentinelProvider,
)
from backend.protocol_registration.models import SentinelData
from backend.resource_models import RunState
from backend.resource_service import MongoResourceStore
from backend.run_manager import (
    ProtocolRegistrationRunExecutor,
    RunExecutionContext,
    _jwt_expiry,
    _protocol_access_token_expiry,
)


def _success_transport(
    *, password: bool, transport_type: type[FakeTransport] = FakeTransport
) -> FakeTransport:
    responses = [
        {"status_code": 200, "body": {}, "url": "https://chatgpt.com/"},
        {"status_code": 200, "body": {"csrfToken": "csrf"}},
        {"status_code": 200, "body": {"url": "https://auth.openai.com/authorize"}},
        {
            "status_code": 302,
            "headers": {"location": "https://auth.openai.com/create-account/password"},
            "url": "https://auth.openai.com/create-account/password",
        },
    ]
    if password:
        responses.append({"status_code": 200, "body": {"continue_url": "/api/accounts/email-otp/send"}})
    responses.extend(
        [
            {"status_code": 204, "body": {}},
            {"status_code": 200, "body": {"continue_url": "/verify-email"}},
            {"status_code": 200, "body": {"continue_url": "/about-you"}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"accessToken": "AT", "expiresAt": "2030-01-01T00:00:00Z"}},
        ]
    )
    return transport_type(responses)


def _service(mode: str) -> ProtocolRegistrationService:
    return ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode=mode, auth_session_delay_seconds=0
        ),
        sentinel_provider=StaticSentinelProvider(
            {"sentinel_token": "S", "oai_did": "D"}
        ),
        otp_provider=StaticOtpProvider(default="123456"),
    )


def test_password_registration_follows_baseline_endpoint_order() -> None:
    transport = _success_transport(password=True)
    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("Test@Example.com"), transport=transport
        )
    )

    assert result.success is True
    assert result.email == "test@example.com"
    assert result.access_token == "AT"
    methods = [call.method for call in transport.calls]
    urls = [call.url for call in transport.calls]
    assert methods[:4] == ["GET", "GET", "POST", "GET"]
    assert any(url.endswith("/api/accounts/user/register") for url in urls)
    assert any(url.endswith("/api/accounts/email-otp/validate") for url in urls)
    assert transport.calls[-1].url.endswith("/api/auth/session")


def test_password_registration_does_not_save_password_after_register_failure() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}, "url": "https://chatgpt.com/"},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {
                "status_code": 200,
                "body": {"url": "https://auth.openai.com/authorize"},
            },
            {
                "status_code": 302,
                "headers": {
                    "location": "https://auth.openai.com/email-verification"
                },
                "url": "https://auth.openai.com/email-verification",
            },
            {
                "status_code": 400,
                "body": {"error": {"code": "password_not_set"}},
            },
            {"status_code": 204, "body": {}},
            {"status_code": 200, "body": {"continue_url": "/verify-email"}},
            {"status_code": 200, "body": {"continue_url": "/about-you"}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"accessToken": "AT"}},
        ]
    )

    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    assert result.stage == "user_register"
    assert result.error == "password_not_set"
    assert len(transport.calls) == 5


@pytest.mark.parametrize("did_source", ["response_header", "cookie_jar"])
def test_password_registration_uses_server_issued_did_before_sentinel(
    did_source: str,
) -> None:
    class ServerDidTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__(
                [
                    {
                        "status_code": 200,
                        "body": {},
                        "headers": (
                            {"set-cookie": "oai-did=server-issued-did; Path=/; Secure"}
                            if did_source == "response_header"
                            else {}
                        ),
                        "url": "https://chatgpt.com/",
                    },
                    {"status_code": 200, "body": {"csrfToken": "csrf"}},
                    {"status_code": 200, "body": {"url": "https://auth.openai.com/authorize"}},
                    {
                        "status_code": 302,
                        "headers": {"location": "https://auth.openai.com/create-account/password"},
                        "url": "https://auth.openai.com/create-account/password",
                    },
                    {"status_code": 200, "body": {"continue_url": "/api/accounts/email-otp/send"}},
                    {"status_code": 204, "body": {}},
                    {"status_code": 200, "body": {"continue_url": "/verify-email"}},
                    {"status_code": 200, "body": {"continue_url": "/about-you"}},
                    {"status_code": 200, "body": {}},
                    {"status_code": 200, "body": {"accessToken": "AT"}},
                ]
            )
            self.server_did = ""

        async def request(self, method: str, url: str, **kwargs):
            response = await super().request(method, url, **kwargs)
            if len(self.calls) == 1:
                self.server_did = "server-issued-did"
            return response

        def cookie_header(self) -> str:
            return (
                f"oai-did={self.server_did}"
                if did_source == "cookie_jar" and self.server_did
                else ""
            )

    seen_dids: list[str | None] = []

    class RecordingSentinelProvider:
        async def get(self, request, *_args):
            seen_dids.append(request.device_id)
            did = request.device_id or "provider-generated-did"
            return SentinelData(
                sentinel_token="username-token",
                sentinel_oauth_token="oauth-token",
                sentinel_authorize_token="authorize-token",
                oai_did=did,
            )

    transport = ServerDidTransport()
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode="password", auth_session_delay_seconds=0
        ),
        sentinel_provider=RecordingSentinelProvider(),
        otp_provider=StaticOtpProvider(default="123456"),
    )

    result = asyncio.run(
        service.register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is True
    assert transport.calls[0].url == "https://chatgpt.com/"
    assert seen_dids == ["server-issued-did"]
    assert result.device_id == "server-issued-did"
    signin_call = next(call for call in transport.calls if "/api/auth/signin/openai" in call.url)
    assert "ext-oai-did=server-issued-did" in signin_call.url
    csrf_call = next(call for call in transport.calls if call.url.endswith("/api/auth/csrf"))
    assert csrf_call.kwargs["headers"]["oai-device-id"] == "server-issued-did"


def test_passwordless_registration_uses_resend_and_does_not_send_password() -> None:
    transport = _success_transport(password=False)
    result = asyncio.run(
        _service("passwordless").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is True
    urls = [call.url for call in transport.calls]
    assert not any(url.endswith("/api/accounts/user/register") for url in urls)
    assert any(url.endswith("/api/accounts/email-otp/resend") for url in urls)


def test_authorize_continue_uses_its_flow_specific_sentinel_token() -> None:
    class HeaderTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__(
                [
                    {"status_code": 200, "body": {"url": "https://auth.openai.com/authorize"}},
                    {
                        "status_code": 302,
                        "headers": {"location": "https://auth.openai.com/create-account"},
                    },
                    {"status_code": 200, "body": {}},
                ]
            )
            self.raw_headers: list[dict[str, str]] = []

        async def request(self, method: str, url: str, **kwargs):
            self.raw_headers.append(dict(kwargs.get("headers") or {}))
            return await super().request(method, url, **kwargs)

    transport = HeaderTransport()
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(signin_attempts=1)
    )
    result = asyncio.run(
        service._prepare_auth_state(
            transport,
            "x@example.com",
            "D",
            "session-id",
            "csrf",
            {"oai-device-id": "D"},
            SentinelData(
                sentinel_token="username-token",
                sentinel_authorize_token="authorize-token",
                sentinel_so_token="oauth-so-token",
                sentinel_authorize_so_token="authorize-so-token",
                oai_did="D",
            ),
            "password",
        )
    )

    assert result["ok"] is True
    assert transport.raw_headers[-1]["openai-sentinel-token"] == "authorize-token"
    assert (
        transport.raw_headers[-1]["openai-sentinel-so-token"]
        == "authorize-so-token"
    )


def test_user_register_uses_username_flow_specific_sentinel_headers() -> None:
    class HeaderTransport(FakeTransport):
        def __init__(self, responses) -> None:
            super().__init__(responses)
            self.raw_headers: dict[str, dict[str, str]] = {}

        async def request(self, method: str, url: str, **kwargs):
            self.raw_headers[url] = dict(kwargs.get("headers") or {})
            return await super().request(method, url, **kwargs)

    transport = _success_transport(password=True, transport_type=HeaderTransport)
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode="password", auth_session_delay_seconds=0
        ),
        sentinel_provider=StaticSentinelProvider(
            SentinelData(
                sentinel_token="username-token",
                sentinel_oauth_token="oauth-token",
                sentinel_so_token="oauth-so-token",
                oai_did="D",
                sentinel_username_so_token="username-so-token",
            )
        ),
        otp_provider=StaticOtpProvider(default="123456"),
    )

    result = asyncio.run(
        service.register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is True
    register_url = next(
        call.url
        for call in transport.calls
        if call.url.endswith("/api/accounts/user/register")
    )
    register_headers = transport.raw_headers[register_url]
    assert register_headers["openai-sentinel-token"] == "username-token"
    assert (
        register_headers["openai-sentinel-so-token"] == "username-so-token"
    )


def test_failure_result_redacts_tokens_from_public_dict() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
        ]
    )
    result = asyncio.run(
        _service("passwordless").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    public = result.to_dict(include_secrets=False)
    assert public["access_token"] == ""
    assert public["responses"]["csrf"]["body"]["csrfToken"] == "[redacted]"


@pytest.mark.parametrize(
    ("csrf_response", "expected_error"),
    [
        ({"status_code": 429, "body": {}}, "http_429"),
        (
            {
                "status_code": 403,
                "headers": {"cf-ray": "fixture"},
                "text": "Just a moment... Cloudflare",
            },
            "cloudflare_challenge",
        ),
    ],
)
def test_transient_http_failures_are_normalized_for_worker_retry(
    csrf_response: dict[str, object], expected_error: str
) -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            csrf_response,
        ]
    )

    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    assert result.error == expected_error


@pytest.mark.parametrize(
    ("prime_response", "expected_error"),
    [
        ({"status_code": 429, "body": {}}, "http_429"),
        (
            {
                "status_code": 403,
                "headers": {"cf-mitigated": "challenge"},
                "text": "blocked",
            },
            "cloudflare_challenge",
        ),
    ],
)
def test_prime_preserves_transient_http_failures(
    prime_response: dict[str, object], expected_error: str
) -> None:
    transport = FakeTransport([prime_response])

    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    assert result.error == expected_error
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_prime_classifies_cloudflare_edge_failures_as_transient(
    status_code: int,
) -> None:
    transport = FakeTransport(
        [
            {
                "status_code": status_code,
                "headers": {"server": "cloudflare", "cf-ray": "fixture"},
                "body": {},
            }
        ]
    )

    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    assert result.error == "cloudflare_challenge"
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("signin_response", "expected_error"),
    [
        ({"status_code": 429, "body": {}}, "http_429"),
        (
            {
                "status_code": 403,
                "headers": {"cf-ray": "fixture"},
                "text": "Just a moment... Cloudflare",
            },
            "cloudflare_challenge",
        ),
    ],
)
def test_auth_state_preserves_transient_signin_failures(
    signin_response: dict[str, object], expected_error: str
) -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            signin_response,
        ]
    )

    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    assert result.error == expected_error


def test_auth_state_preserves_transient_authorize_continue_failure() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {"url": "https://auth.openai.com/authorize"}},
            {"status_code": 302, "headers": {"location": "https://auth.openai.com/create-account"}},
            {"status_code": 429, "body": {}},
        ]
    )

    result = asyncio.run(
        _service("password").register(
            ProtocolRegistrationRequest("x@example.com"), transport=transport
        )
    )

    assert result.success is False
    assert result.error == "http_429"


def test_passwordless_resend_can_opt_into_send_fallback() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 400, "body": {"error": {"code": "not_ready"}}},
            {"status_code": 204, "body": {}},
        ]
    )
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode="passwordless", otp_fallback_send=True
        ),
        sentinel_provider=StaticSentinelProvider({"oai_did": "D"}),
    )
    response = asyncio.run(
        service._send_otp(
            transport,
            "https://auth.openai.com/email-verification",
            {"oai-device-id": "D"},
            "D",
            StaticSentinelProvider({"oai_did": "D"}).data,
            "passwordless",
            {},
        )
    )
    assert response.status_code == 204
    assert transport.calls[-1].url.endswith("/api/accounts/passwordless/send-otp")


def test_existing_account_restarts_passwordless_login_for_session() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {"url": "https://auth.openai.com/signup"}},
            {"status_code": 302, "headers": {"location": "https://auth.openai.com/create-account/password"}, "url": "https://auth.openai.com/create-account/password"},
            {"status_code": 200, "body": {"continue_url": "/api/accounts/email-otp/send"}},
            {"status_code": 204},
            {"status_code": 200, "body": {"continue_url": "/verify-email"}},
            {"status_code": 200, "body": {"continue_url": "/about-you"}},
            {"status_code": 409, "body": {"error": {"code": "user_already_exists"}}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"url": "https://auth.openai.com/login"}},
            {"status_code": 302, "headers": {"location": "https://auth.openai.com/log-in"}, "url": "https://auth.openai.com/log-in"},
            {"status_code": 200, "body": {}},
            {"status_code": 204},
            {"status_code": 200, "body": {"continue_url": "/login-done"}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"accessToken": "AT2"}},
        ]
    )
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(registration_mode="password", auth_session_delay_seconds=0),
        sentinel_provider=StaticSentinelProvider({"oai_did": "D"}),
        otp_provider=StaticOtpProvider(codes={"x@example.com": ["123456", "654321"]}),
    )
    result = asyncio.run(
        service.register(ProtocolRegistrationRequest("x@example.com"), transport=transport)
    )
    assert result.success is True
    assert result.access_token == "AT2"
    assert result.password == ""
    assert any("existing_login" in call.url or "prompt=login" in call.url for call in transport.calls)


def test_existing_account_can_use_fresh_injected_login_transport() -> None:
    class CookieTransport(FakeTransport):
        def __init__(self, responses) -> None:
            super().__init__(responses)
            self.cookies: list[tuple[str, str, str | None]] = []

        def set_cookie(
            self,
            name: str,
            value: str,
            *,
            domain: str | None = None,
            path: str = "/",
        ) -> None:
            del path
            self.cookies.append((name, value, domain))

    signup_transport = CookieTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {"url": "https://auth.openai.com/signup"}},
            {
                "status_code": 302,
                "headers": {"location": "https://auth.openai.com/create-account/password"},
            },
            {"status_code": 200, "body": {"continue_url": "/api/accounts/email-otp/send"}},
            {"status_code": 204},
            {"status_code": 200, "body": {"continue_url": "/verify-email"}},
            {"status_code": 200, "body": {"continue_url": "/about-you"}},
            {"status_code": 409, "body": {"error": {"code": "user_already_exists"}}},
            *({"status_code": 200, "body": {}} for _ in range(6)),
        ]
    )
    login_transport = CookieTransport(
        [
            {"status_code": 200, "body": {"url": "https://auth.openai.com/login"}},
            {
                "status_code": 302,
                "headers": {"location": "https://auth.openai.com/log-in"},
            },
            {"status_code": 400, "body": {"error": {"code": "login_state_already_selected"}}},
            {"status_code": 204},
            {"status_code": 200, "body": {"continue_url": "/login-done"}},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"accessToken": "AT2"}},
        ]
    )
    factory_calls: list[tuple[ProtocolRegistrationRequest, ProtocolRegistrationConfig]] = []

    def fresh_transport_factory(
        *, request: ProtocolRegistrationRequest, config: ProtocolRegistrationConfig
    ) -> FakeTransport:
        factory_calls.append((request, config))
        return login_transport

    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode="password",
            auth_session_delay_seconds=0,
        ),
        fresh_transport_factory=fresh_transport_factory,
        sentinel_provider=StaticSentinelProvider(
            {
                "oai_did": "D",
                "cookie_str": "sentinel-session=signup-only",
            }
        ),
        otp_provider=StaticOtpProvider(
            codes={"x@example.com": ["123456", "654321"]}
        ),
    )

    result = asyncio.run(
        service.register(
            ProtocolRegistrationRequest(
                "x@example.com",
                proxy="http://127.0.0.1:8080",
            ),
            transport=signup_transport,
        )
    )

    assert result.success is True
    assert result.access_token == "AT2"
    assert len(factory_calls) == 1
    assert factory_calls[0][0].proxy == "http://127.0.0.1:8080"
    assert signup_transport.closed is False
    assert login_transport.closed is True
    assert not any("prompt=login" in call.url for call in signup_transport.calls)
    assert any("prompt=login" in call.url for call in login_transport.calls)
    assert login_transport.cookies == [("oai-did", "D", ".openai.com")]
    assert login_transport.calls[0].kwargs["headers"]["oai-device-id"] == "D"


def test_run_executor_reads_access_token_jwt_expiry() -> None:
    expected = datetime(2033, 5, 18, 3, 33, 20, tzinfo=timezone.utc)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(expected.timestamp())}).encode("utf-8")
    ).decode("ascii").rstrip("=")

    assert _jwt_expiry(f"header.{payload}.signature") == expected
    assert _jwt_expiry("not-a-jwt") is None


def test_protocol_expiry_prefers_aware_metadata_and_rejects_naive_metadata() -> None:
    metadata_expiry = datetime(2035, 1, 2, 3, 4, tzinfo=timezone.utc)
    jwt_expiry = datetime(2036, 2, 3, 4, 5, tzinfo=timezone.utc)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(jwt_expiry.timestamp())}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    token = f"header.{payload}.signature"

    assert _protocol_access_token_expiry(
        token, {"expires_at": metadata_expiry.isoformat()}
    ) == metadata_expiry
    assert _protocol_access_token_expiry(
        token, {"expires_at": "2035-01-02T03:04:00"}
    ) == jwt_expiry


def test_service_expiry_reads_nested_auth_session_metadata() -> None:
    from backend.protocol_registration.service import _extract_expiry

    expected = datetime(2037, 3, 4, 5, 6, tzinfo=timezone.utc)
    assert _extract_expiry({"session": {"expires_at": expected.isoformat()}}) == expected


def test_protocol_persistence_writes_token_before_email_delete_and_is_retryable() -> None:
    events: list[str] = []

    class Accounts:
        def __init__(self) -> None:
            self.document: dict[str, object] | None = None
            self.updates: list[dict[str, dict[str, object]]] = []

        async def update_one(self, _query, update, *, upsert):
            assert upsert is True
            events.append("account_upsert")
            self.updates.append(update)
            assert not set(update["$setOnInsert"]).intersection(update["$set"])
            if self.document is None:
                self.document = dict(update["$setOnInsert"])
            self.document.update(update["$set"])
            return SimpleNamespace(matched_count=1, upserted_id=self.document["_id"])

        async def find_one(self, _query):
            events.append("account_find")
            return dict(self.document or {}) or None

    class Emails:
        def __init__(self) -> None:
            self.fail_first_delete = True
            self.queries: list[dict[str, object]] = []

        async def delete_one(self, query):
            events.append("email_delete")
            self.queries.append(query)
            if self.fail_first_delete:
                self.fail_first_delete = False
                raise RuntimeError("simulated delete interruption")
            return SimpleNamespace(deleted_count=1)

    class Manager:
        def __init__(self) -> None:
            self.accounts = Accounts()
            self.emails = Emails()
            self.database = {
                "accounts": self.accounts,
                "emails": self.emails,
            }

        def require_online(self) -> None:
            return None

    manager = Manager()
    store = MongoResourceStore(manager)  # type: ignore[arg-type]
    source = {
        "_id": "email-1",
        "email": "Retry@Example.com",
        "accessUrl": "https://mail.example/messages",
    }
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)

    async def scenario():
        with pytest.raises(RuntimeError, match="delete interruption"):
            await store.complete_protocol_registration_success(
                source,
                "run-1",
                access_token="TOKEN_FIXTURE",
                access_token_expires_at=expiry,
                chatgpt_password="Password1!",
                registration_country="jp",
                registration_proxy_group="default",
            )
        assert manager.accounts.document is not None
        assert manager.accounts.document["accessToken"] == "TOKEN_FIXTURE"

        first_account_id = manager.accounts.document["_id"]
        account = await store.complete_protocol_registration_success(
            source,
            "run-1",
            access_token="TOKEN_FIXTURE",
            access_token_expires_at=expiry,
            chatgpt_password="Password1!",
            registration_country="jp",
            registration_proxy_group="default",
        )
        return first_account_id, account

    first_account_id, account = asyncio.run(scenario())

    assert account.id == first_account_id
    assert account.accessTokenConfigured is True
    assert account.accessTokenExpiresAt == expiry
    assert events == [
        "account_upsert",
        "email_delete",
        "account_upsert",
        "email_delete",
        "account_find",
    ]
    assert manager.emails.queries == [
        {
            "_id": "email-1",
            "emailNormalized": "retry@example.com",
            "reservedBy": "run-1",
            "$or": [
                {"reservationKind": {"$exists": False}},
                {"reservationKind": {"$ne": "email_change"}},
            ],
        },
        {
            "_id": "email-1",
            "emailNormalized": "retry@example.com",
            "reservedBy": "run-1",
            "$or": [
                {"reservationKind": {"$exists": False}},
                {"reservationKind": {"$ne": "email_change"}},
            ],
        },
    ]


def test_protocol_persistence_atomically_writes_normalized_totp() -> None:
    """A successful protocol enrollment stores the factor with the token."""

    class Accounts:
        def __init__(self) -> None:
            self.document: dict[str, object] | None = None
            self.updates: list[dict[str, object]] = []

        async def update_one(self, _query, update, *, upsert):
            assert upsert is True
            self.updates.append(update)
            if self.document is None:
                self.document = dict(update["$setOnInsert"])
            self.document.update(update["$set"])
            return SimpleNamespace(matched_count=1, upserted_id=self.document["_id"])

        async def find_one(self, _query):
            return dict(self.document or {}) or None

    class Emails:
        async def delete_one(self, _query):
            return SimpleNamespace(deleted_count=1)

    class Manager:
        def __init__(self) -> None:
            self.accounts = Accounts()
            self.emails = Emails()
            self.database = {"accounts": self.accounts, "emails": self.emails}

        def require_online(self) -> None:
            return None

    manager = Manager()
    store = MongoResourceStore(manager)  # type: ignore[arg-type]
    activated_at = datetime(
        2026, 8, 23, 12, 34, 56, tzinfo=timezone(timedelta(hours=8))
    )
    source = {
        "_id": "email-totp",
        "email": "Totp@Example.com",
        "accessUrl": "https://mail.example/messages",
    }

    account = asyncio.run(
        store.complete_protocol_registration_success(
            source,
            "run-totp",
            access_token="TOKEN_TOTP",
            access_token_expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
            totp_secret=" jbsw y3dp ehpk 3pxp ",
            totp_activated_at=activated_at,
        )
    )

    assert account.totpSecret == "JBSWY3DPEHPK3PXP"
    assert manager.accounts.document is not None
    assert manager.accounts.document["totpSecret"] == "JBSWY3DPEHPK3PXP"
    assert manager.accounts.document["totpActivatedAt"] == datetime(
        2026, 8, 23, 4, 34, 56, tzinfo=timezone.utc
    )
    assert manager.accounts.document["totpStatus"] == "enabled"
    assert len(manager.accounts.updates) == 1
    update = manager.accounts.updates[0]
    assert not set(update["$setOnInsert"]).intersection(update["$set"])
    assert update["$set"]["accessToken"] == "TOKEN_TOTP"
    assert update["$set"]["totpSecret"] == "JBSWY3DPEHPK3PXP"
    assert update["$set"]["totpActivatedAt"] == datetime(
        2026, 8, 23, 4, 34, 56, tzinfo=timezone.utc
    )


def test_protocol_persistence_rejects_invalid_totp_fields() -> None:
    class Accounts:
        async def update_one(self, *_args, **_kwargs):
            raise AssertionError("invalid TOTP must be rejected before persistence")

    class Emails:
        async def delete_one(self, *_args, **_kwargs):
            raise AssertionError("invalid TOTP must be rejected before mailbox cleanup")

    class Manager:
        database = {"accounts": Accounts(), "emails": Emails()}

        def require_online(self) -> None:
            return None

    store = MongoResourceStore(Manager())  # type: ignore[arg-type]
    source = {
        "_id": "email-invalid-totp",
        "email": "invalid@example.com",
        "accessUrl": "https://mail.example/messages",
    }
    kwargs = {
        "access_token": "TOKEN_TOTP",
        "access_token_expires_at": datetime(2035, 1, 1, tzinfo=timezone.utc),
    }

    with pytest.raises(ValueError, match="TOTP Secret"):
        asyncio.run(
            store.complete_protocol_registration_success(
                source, "run-invalid-totp", **kwargs, totp_secret="not-base32!"
            )
        )
    with pytest.raises(ValueError, match="激活时间"):
        asyncio.run(
            store.complete_protocol_registration_success(
                source,
                "run-invalid-totp",
                **kwargs,
                totp_secret="JBSWY3DPEHPK3PXP",
                totp_activated_at=datetime(2026, 1, 1),
            )
        )


class _ExecutorWorkerStore:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}

    async def assign(self, run_id: str, worker_id: str, **values: object) -> None:
        self.documents[worker_id] = {"runId": run_id, **values}

    async def stage(self, _run_id: str, worker_id: str, stage: str) -> None:
        self.documents[worker_id]["stage"] = stage

    async def finish(
        self, _run_id: str, worker_id: str, status: str, **values: object
    ) -> None:
        self.documents[worker_id].update(status=status, **values)


class _ExecutorProbeStore:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    async def acquire_proxy(self, _owner: str, **_values: object) -> SimpleNamespace:
        return SimpleNamespace(
            id="proxy-1",
            scheme="http",
            host="127.0.0.1",
            port=8080,
            username="",
            password="",
        )

    async def release_proxy(self, proxy_id: str, owner: str) -> None:
        self.released.append((proxy_id, owner))


class _ExecutorResources:
    def __init__(self) -> None:
        self.released: list[str] = []
        self.completed: list[tuple[str, str]] = []
        self.tokens: list[tuple[str, str, datetime]] = []
        self.totp_values: list[dict[str, object]] = []

    async def release_email(self, email_id: str, _run_id: str) -> None:
        self.released.append(email_id)

    async def complete_protocol_registration_success(
        self,
        source: dict[str, object],
        run_id: str,
        *,
        access_token: str,
        access_token_expires_at: datetime,
        **values: object,
    ) -> SimpleNamespace:
        self.completed.append((str(source["_id"]), run_id))
        self.tokens.append(("account-1", access_token, access_token_expires_at))
        self.totp_values.append(values)
        return SimpleNamespace(id="account-1")


def _executor_context(
    *,
    resources: _ExecutorResources,
    probe_store: _ExecutorProbeStore,
    worker_store: _ExecutorWorkerStore,
    require_password: bool,
    enable_registration_totp: bool = False,
) -> RunExecutionContext:
    now = datetime.now(timezone.utc)
    state = RunState(
        runId="11111111-1111-4111-8111-111111111111",
        kind="protocol_registration",
        status="running",
        requested=1,
        pending=1,
        processed=0,
        succeeded=0,
        failed=0,
        workerCount=1,
        activeWorkers=0,
        startedAt=now,
        updatedAt=now,
        registrationCountry="JP",
        registrationProxyGroup="default",
    )

    async def database_call(operation):
        return await operation()

    async def record_result(succeeded: bool) -> None:
        state.processed += 1
        state.pending = 0
        state.succeeded += int(succeeded)
        state.failed += int(not succeeded)

    return RunExecutionContext(
        state=state,
        reserved=[
            {
                "_id": "email-1",
                "email": "worker@example.com",
                "accessUrl": "https://mail.example/messages",
                "_workerId": "worker-1",
                "_sequence": 1,
            }
        ],
        concurrency=1,
        cancel_event=asyncio.Event(),
        resources=resources,  # type: ignore[arg-type]
        database_call=database_call,
        record_result=record_result,
        append_log=lambda *_args, **_kwargs: None,
        save_state=lambda: asyncio.sleep(0),
        kind="protocol_registration",
        settings_snapshot=SimpleNamespace(
            requireRegistrationPassword=require_password,
            enableRegistrationTotp=enable_registration_totp,
        ),  # type: ignore[arg-type]
        probe_store=probe_store,  # type: ignore[arg-type]
        worker_store=worker_store,  # type: ignore[arg-type]
    )


def test_protocol_run_executor_persists_token_and_uses_settings_snapshot() -> None:
    expected_expiry = datetime(2033, 5, 18, 3, 33, 20, tzinfo=timezone.utc)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(expected_expiry.timestamp())}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    access_token = f"header.{payload}.signature"
    captured: dict[str, object] = {}

    class Service:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            captured["request"] = request
            return ProtocolRegistrationResult(
                success=True,
                email=request.email,
                password="Password1!",
                access_token=access_token,
                registration_mode="password",
            )

    resources = _ExecutorResources()
    probe_store = _ExecutorProbeStore()
    worker_store = _ExecutorWorkerStore()
    context = _executor_context(
        resources=resources,
        probe_store=probe_store,
        worker_store=worker_store,
        require_password=True,
    )

    cancelled = asyncio.run(
        ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
    )

    assert cancelled is False
    assert captured["config"].registration_mode == "password"  # type: ignore[union-attr]
    assert resources.completed == [("email-1", context.state.runId)]
    assert resources.tokens == [("account-1", access_token, expected_expiry)]
    assert worker_store.documents["worker-1"]["status"] == "success"
    assert context.state.activeWorkers == 0
    assert context.state.succeeded == 1
    assert len(probe_store.released) == 1


def test_protocol_run_executor_forwards_totp_result_and_setting() -> None:
    expected_expiry = datetime(2033, 5, 18, 3, 33, 20, tzinfo=timezone.utc)
    activated_at = datetime(2026, 8, 23, 4, 34, 56, tzinfo=timezone.utc)
    access_token = "header.eyJleHAiOjE5OTk5OTk5OTl9.signature"
    captured: dict[str, object] = {}

    class Service:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            return ProtocolRegistrationResult(
                success=True,
                email=request.email,
                access_token=access_token,
                metadata={"expires_at": expected_expiry.isoformat()},
                totp_secret="JBSWY3DPEHPK3PXP",
                totp_activated_at=activated_at,
                totp_error="",
            )

    resources = _ExecutorResources()
    probe_store = _ExecutorProbeStore()
    worker_store = _ExecutorWorkerStore()
    context = _executor_context(
        resources=resources,
        probe_store=probe_store,
        worker_store=worker_store,
        require_password=False,
        enable_registration_totp=True,
    )

    asyncio.run(ProtocolRegistrationRunExecutor(service_factory=Service).execute(context))

    config = captured["config"]
    assert config.enable_registration_totp is True  # type: ignore[union-attr]
    assert resources.totp_values == [
        {
            "chatgpt_password": "",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "totp_activated_at": activated_at,
            "totp_error": "",
            "registration_country": "JP",
            "registration_proxy_group": "default",
        }
    ]


def test_protocol_run_executor_releases_resources_on_protocol_failure() -> None:
    captured: dict[str, object] = {}

    class Service:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            return ProtocolRegistrationResult(
                success=False,
                email=request.email,
                stage="email_otp_wait",
                error="email_otp_timeout",
            )

    resources = _ExecutorResources()
    probe_store = _ExecutorProbeStore()
    worker_store = _ExecutorWorkerStore()
    context = _executor_context(
        resources=resources,
        probe_store=probe_store,
        worker_store=worker_store,
        require_password=False,
    )

    asyncio.run(ProtocolRegistrationRunExecutor(service_factory=Service).execute(context))

    assert captured["config"].registration_mode == "passwordless"  # type: ignore[union-attr]
    assert resources.released == ["email-1"]
    assert resources.completed == []
    assert worker_store.documents["worker-1"]["status"] == "failed"
    assert worker_store.documents["worker-1"]["error_code"] == "email_otp_timeout"
    assert context.state.failed == 1
    assert len(probe_store.released) == 1


def test_protocol_run_executor_marks_totp_failure_as_partial_success() -> None:
    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            return ProtocolRegistrationResult(
                success=True,
                email=request.email,
                access_token="header.eyJleHAiOjE5OTk5OTk5OTl9.signature",
                metadata={"expires_at": "2035-01-01T00:00:00Z"},
                totp_error="totp_activate_failed",
            )

    resources = _ExecutorResources()
    probe_store = _ExecutorProbeStore()
    worker_store = _ExecutorWorkerStore()
    context = _executor_context(
        resources=resources,
        probe_store=probe_store,
        worker_store=worker_store,
        require_password=False,
        enable_registration_totp=True,
    )

    asyncio.run(ProtocolRegistrationRunExecutor(service_factory=Service).execute(context))

    worker = worker_store.documents["worker-1"]
    assert worker["status"] == "partial_success"
    assert worker["error_code"] == "totp_activate_failed"
    assert worker["error_stage"] == "two_factor"
    assert context.state.succeeded == 1
    assert len(probe_store.released) == 1


@pytest.mark.parametrize(
    ("access_token", "metadata", "error_code"),
    [
        ("", {"expires_at": "2999-01-01T00:00:00Z"}, "access_token_missing"),
        ("opaque-token", {}, "access_token_expiry_missing"),
        (
            "opaque-token",
            {"expires_at": "2000-01-01T00:00:00Z"},
            "access_token_expired",
        ),
    ],
)
def test_protocol_run_executor_releases_email_for_invalid_token_result(
    access_token: str,
    metadata: dict[str, str],
    error_code: str,
) -> None:
    resources = _ExecutorResources()
    probe_store = _ExecutorProbeStore()
    worker_store = _ExecutorWorkerStore()
    context = _executor_context(
        resources=resources,
        probe_store=probe_store,
        worker_store=worker_store,
        require_password=False,
    )

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            return ProtocolRegistrationResult(
                success=True,
                email=request.email,
                access_token=access_token,
                metadata=metadata,
            )

    asyncio.run(ProtocolRegistrationRunExecutor(service_factory=Service).execute(context))

    assert resources.released == ["email-1"]
    assert resources.completed == []
    assert resources.tokens == []
    assert worker_store.documents["worker-1"]["status"] == "failed"
    assert worker_store.documents["worker-1"]["error_code"] == error_code
    assert worker_store.documents["worker-1"]["error_stage"] == "access_token"
    assert context.state.processed == 1
    assert context.state.failed == 1
    assert context.state.activeWorkers == 0
    assert len(probe_store.released) == 1
