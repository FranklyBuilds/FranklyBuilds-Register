from __future__ import annotations

import asyncio

import pytest

from backend.protocol_registration import (
    FakeTransport,
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationService,
    SentinelData,
    StaticOtpProvider,
    StaticSentinelProvider,
)


SECRET = "JBSWY3DPEHPK3PXP"


class _RecordingOtpProvider(StaticOtpProvider):
    def __init__(self) -> None:
        super().__init__(codes={"person@example.com": ["123456", "654321"]})
        self.selected: list[str | None] = []

    async def wait_for_code(self, request, **kwargs):  # type: ignore[no-untyped-def]
        value = await super().wait_for_code(request, **kwargs)
        self.selected.append(value)
        return value


def _service() -> ProtocolRegistrationService:
    return ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode="passwordless",
            enable_registration_totp=True,
            auth_session_delay_seconds=0,
            totp_reauth_delay_seconds=0,
        ),
        sentinel_provider=StaticSentinelProvider({"oai_did": "D"}),
        otp_provider=StaticOtpProvider(default="123456"),
    )


def _helper_transport(*, auth_url: str = "https://auth.openai.com/reauth") -> FakeTransport:
    state_ready = False

    def verification_page(call):  # type: ignore[no-untyped-def]
        nonlocal state_ready
        if call.method != "GET":
            return {
                "status_code": 400,
                "body": {"error": {"code": "invalid_state"}},
            }
        state_ready = True
        return {
            "status_code": 200,
            "url": "https://auth.openai.com/email-verification",
        }

    def validate_otp(_call):  # type: ignore[no-untyped-def]
        if not state_ready:
            return {
                "status_code": 400,
                "body": {"error": {"code": "invalid_state"}},
            }
        return {"status_code": 200, "body": {"continue_url": "/reauth-done"}}

    return FakeTransport(
        [
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {"url": auth_url}},
            {
                "status_code": 302,
                "headers": {"location": "https://auth.openai.com/email-verification"},
            },
            verification_page,
            validate_otp,
            {"status_code": 200, "body": {}},
            {
                "status_code": 200,
                "body": {
                    "accessToken": "REFRESHED",
                    "expiresAt": "2035-01-01T00:00:00Z",
                },
            },
            {
                "status_code": 200,
                "body": {"data": {"secret": SECRET, "session_id": "session-1"}},
            },
            {"status_code": 200, "body": {"data": {"success": True}}},
            {"status_code": 200, "body": {}},
        ]
    )


def test_protocol_totp_helper_enrolls_and_refreshes_session() -> None:
    service = _service()
    transport = _helper_transport()
    responses: dict[str, dict[str, object]] = {}
    provider = _RecordingOtpProvider()

    result = asyncio.run(
        service._enroll_totp(
            active_transport=transport,
            request=ProtocolRegistrationRequest("person@example.com"),
            did="D",
            base_headers=service._base_headers("D"),
            sentinel=SentinelData(oai_did="D"),
            otp_provider=provider,
            otp_baseline=None,
            responses=responses,
            excluded_codes={"123456"},
        )
    )

    assert result.secret == SECRET
    assert result.access_token == "REFRESHED"
    assert result.auth_session["accessToken"] == "REFRESHED"
    assert [call.url.rsplit("/", 1)[-1] for call in transport.calls[-3:]] == [
        "enroll",
        "activate_enrollment",
        "models",
    ]
    activate_call = transport.calls[-2]
    assert activate_call.kwargs["json"]["factor_type"] == "totp"
    assert activate_call.kwargs["json"]["code"] == "[redacted]"
    assert activate_call.kwargs["json"]["session_id"] == "[redacted]"
    assert "totp_enroll" in responses
    assert responses["totp_enroll"]["body"]["data"]["secret"] == "[redacted]"
    assert provider.calls[0]["email"] == "person@example.com"
    assert provider.selected == ["654321"]
    assert any(
        call.method == "GET"
        and call.url == "https://auth.openai.com/email-verification"
        for call in transport.calls
    )
    assert not any(
        call.url.endswith(
            ("/api/accounts/email-otp/send", "/api/accounts/email-otp/resend")
        )
        for call in transport.calls
    )


def test_protocol_totp_reauth_walks_trusted_intermediate_redirects() -> None:
    service = _service()
    verification_url = "https://auth.openai.com/email-verification?state=state-1"
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {
                "status_code": 200,
                "body": {"url": "https://auth.openai.com/reauth"},
            },
            {
                "status_code": 302,
                "headers": {"location": "/authorize/resume?state=state-1"},
            },
            {
                "status_code": 302,
                "headers": {"location": "/email-verification?state=state-1"},
            },
            {"status_code": 200, "url": verification_url},
            {"status_code": 200, "body": {"continue_url": "/reauth-done"}},
            {"status_code": 200, "body": {}},
            {
                "status_code": 200,
                "body": {
                    "accessToken": "REFRESHED",
                    "expiresAt": "2035-01-01T00:00:00Z",
                },
            },
            {
                "status_code": 200,
                "body": {"data": {"secret": SECRET, "session_id": "session-1"}},
            },
            {"status_code": 200, "body": {"data": {"success": True}}},
            {"status_code": 200, "body": {}},
        ]
    )

    result = asyncio.run(
        service._enroll_totp(
            active_transport=transport,
            request=ProtocolRegistrationRequest("person@example.com"),
            did="D",
            base_headers=service._base_headers("D"),
            sentinel=SentinelData(oai_did="D"),
            otp_provider=StaticOtpProvider(default="654321"),
            otp_baseline=None,
            responses={},
        )
    )

    assert result.secret == SECRET
    page_gets = [
        call.url
        for call in transport.calls
        if call.method == "GET" and call.url.startswith("https://auth.openai.com/")
    ]
    assert page_gets[:3] == [
        "https://auth.openai.com/reauth?device_id=D",
        "https://auth.openai.com/authorize/resume?state=state-1",
        verification_url,
    ]


def test_protocol_totp_reauth_rejects_non_verification_terminal() -> None:
    service = _service()
    login_url = "https://auth.openai.com/login?return_to=/email-verification"
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {
                "status_code": 200,
                "body": {"url": "https://auth.openai.com/reauth"},
            },
            {"status_code": 302, "headers": {"location": login_url}},
            {"status_code": 200, "url": login_url},
        ]
    )

    with pytest.raises(Exception) as caught:
        asyncio.run(
            service._enroll_totp(
                active_transport=transport,
                request=ProtocolRegistrationRequest("person@example.com"),
                did="D",
                base_headers=service._base_headers("D"),
                sentinel=SentinelData(oai_did="D"),
                otp_provider=StaticOtpProvider(default="654321"),
                otp_baseline=None,
                responses={},
            )
        )

    assert getattr(caught.value, "code", "") == (
        "totp_reauth_email_verification_missing"
    )
    assert not any(
        call.url.endswith(
            (
                "/api/accounts/email-otp/send",
                "/api/accounts/email-otp/resend",
                "/api/accounts/email-otp/validate",
            )
        )
        for call in transport.calls
    )


def test_protocol_totp_reauth_requires_successful_verification_page() -> None:
    service = _service()
    verification_url = "https://auth.openai.com/email-verification"
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {
                "status_code": 200,
                "body": {"url": "https://auth.openai.com/reauth"},
            },
            {"status_code": 302, "headers": {"location": verification_url}},
            {"status_code": 503, "url": verification_url},
        ]
    )

    with pytest.raises(Exception) as caught:
        asyncio.run(
            service._enroll_totp(
                active_transport=transport,
                request=ProtocolRegistrationRequest("person@example.com"),
                did="D",
                base_headers=service._base_headers("D"),
                sentinel=SentinelData(oai_did="D"),
                otp_provider=StaticOtpProvider(default="654321"),
                otp_baseline=None,
                responses={},
            )
        )

    assert getattr(caught.value, "code", "") == (
        "totp_reauth_email_verification_http_503"
    )
    assert not any(
        call.url.endswith("/api/accounts/email-otp/validate")
        for call in transport.calls
    )


def test_protocol_totp_reauth_rejects_untrusted_authorize_effective_url() -> None:
    service = _service()
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {
                "status_code": 200,
                "body": {"url": "https://auth.openai.com/reauth"},
            },
            {
                "status_code": 200,
                "url": "https://evil.example/email-verification",
            },
        ]
    )

    with pytest.raises(Exception) as caught:
        asyncio.run(
            service._enroll_totp(
                active_transport=transport,
                request=ProtocolRegistrationRequest("person@example.com"),
                did="D",
                base_headers=service._base_headers("D"),
                sentinel=SentinelData(oai_did="D"),
                otp_provider=StaticOtpProvider(default="654321"),
                otp_baseline=None,
                responses={},
            )
        )

    assert getattr(caught.value, "code", "") == "totp_reauth_page_untrusted"
    assert not any(
        call.url.endswith("/api/accounts/email-otp/validate")
        for call in transport.calls
    )


def test_protocol_totp_email_verification_detection_uses_exact_path_segment() -> None:
    detector = ProtocolRegistrationService._is_totp_email_verification_url

    assert detector("https://auth.openai.com/u/email-verification?state=state-1")
    assert not detector("https://auth.openai.com/login/email-verification-return")


def test_protocol_registration_waits_before_totp_reauthentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record_delay)
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {"url": "https://auth.openai.com/authorize"}},
            {
                "status_code": 302,
                "headers": {
                    "location": "https://auth.openai.com/create-account/password"
                },
            },
            {"status_code": 204, "body": {}},
            {"status_code": 200, "body": {"continue_url": "/verify-email"}},
            {"status_code": 200, "body": {"continue_url": "/about-you"}},
            {"status_code": 200, "body": {}},
            {
                "status_code": 200,
                "body": {
                    "accessToken": "AT",
                    "expiresAt": "2035-01-01T00:00:00Z",
                },
            },
            {"status_code": 503, "body": {"message": "unavailable"}},
        ]
    )
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            registration_mode="passwordless",
            enable_registration_totp=True,
            auth_session_delay_seconds=0,
        ),
        sentinel_provider=StaticSentinelProvider({"oai_did": "D"}),
        otp_provider=StaticOtpProvider(default="123456"),
    )

    result = asyncio.run(
        service.register(
            ProtocolRegistrationRequest("person@example.com"),
            transport=transport,
        )
    )

    assert result.totp_error == "totp_reauth_csrf_failed"
    assert delays == [20]


def test_protocol_totp_reauthentication_delay_rejects_negative_values() -> None:
    with pytest.raises(
        ValueError,
        match="totp_reauth_delay_seconds must not be negative",
    ):
        ProtocolRegistrationConfig(totp_reauth_delay_seconds=-0.1)


def test_protocol_totp_reauth_requires_authorization_url() -> None:
    service = _service()
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {}},
        ]
    )

    with pytest.raises(Exception) as caught:
        asyncio.run(
            service._enroll_totp(
                active_transport=transport,
                request=ProtocolRegistrationRequest("person@example.com"),
                did="D",
                base_headers=service._base_headers("D"),
                sentinel=SentinelData(oai_did="D"),
                otp_provider=StaticOtpProvider(default="654321"),
                otp_baseline=None,
                responses={},
            )
        )

    assert getattr(caught.value, "code", "") == "totp_reauth_missing_url"
    assert len(transport.calls) == 2


def test_protocol_totp_helper_rejects_untrusted_reauth_url() -> None:
    service = _service()
    transport = _helper_transport(auth_url="https://evil.example/collect")

    with pytest.raises(Exception) as caught:
        asyncio.run(
            service._enroll_totp(
                active_transport=transport,
                request=ProtocolRegistrationRequest("person@example.com"),
                did="D",
                base_headers=service._base_headers("D"),
                sentinel=SentinelData(oai_did="D"),
                otp_provider=StaticOtpProvider(default="123456"),
                otp_baseline=None,
                responses={},
            )
        )

    assert getattr(caught.value, "code", "") == "totp_reauth_url_untrusted"
    assert len(transport.calls) == 2


def test_protocol_registration_keeps_account_success_when_totp_fails() -> None:
    # Baseline passwordless registration succeeds, then the TOTP CSRF request
    # fails.  Enrollment is intentionally best effort for a usable account.
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"csrfToken": "csrf"}},
            {"status_code": 200, "body": {"url": "https://auth.openai.com/authorize"}},
            {
                "status_code": 302,
                "headers": {"location": "https://auth.openai.com/create-account/password"},
            },
            {"status_code": 204, "body": {}},
            {"status_code": 200, "body": {"continue_url": "/verify-email"}},
            {"status_code": 200, "body": {"continue_url": "/about-you"}},
            {"status_code": 200, "body": {}},
            {
                "status_code": 200,
                "body": {"accessToken": "AT", "expiresAt": "2035-01-01T00:00:00Z"},
            },
            {"status_code": 503, "body": {"message": "unavailable"}},
        ]
    )
    service = _service()

    result = asyncio.run(
        service.register(
            ProtocolRegistrationRequest("person@example.com"),
            transport=transport,
        )
    )

    assert result.success is True
    assert result.access_token == "AT"
    assert result.totp_secret == ""
    assert result.totp_error == "totp_reauth_csrf_failed"
    public = result.to_dict(include_secrets=False)
    assert public["totp_secret"] == ""
