from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.protocol_registration import (
    FakeTransport,
    MailboxOtpProvider,
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationService,
    SentinelData,
    StaticOtpProvider,
)


UTC = timezone.utc


def test_sentinel_data_accepts_baseline_authorize_continue_keys() -> None:
    sentinel = SentinelData.from_value(
        {
            "sentinel_authorize_continue_token": "authorize-token",
            "sentinel_authorize_continue_so_token": "authorize-so-token",
        }
    )

    assert sentinel.sentinel_authorize_token == "authorize-token"
    assert sentinel.sentinel_authorize_so_token == "authorize-so-token"


def test_sentinel_data_accepts_username_flow_so_aliases() -> None:
    sentinel = SentinelData.from_value(
        {"sentinel_username_password_create_so_token": "username-so-token"}
    )

    assert sentinel.sentinel_username_so_token == "username-so-token"
    assert sentinel.as_dict()["sentinel_username_so_token"] == "username-so-token"


def test_mailbox_otp_provider_waits_past_excluded_code_with_updated_baseline() -> None:
    class Clock:
        elapsed = 0.0

        def monotonic(self) -> float:
            return self.elapsed

        async def sleep(self, seconds: float) -> None:
            self.elapsed += seconds

    class Mailbox:
        def __init__(self) -> None:
            self.results = iter(
                [
                    SimpleNamespace(
                        verification_code="111111",
                        received_at_utc=datetime(2026, 8, 23, tzinfo=UTC),
                        received_offset="+00:00",
                    ),
                    SimpleNamespace(
                        verification_code="222222",
                        received_at_utc=datetime(2026, 8, 23, 0, 0, 5, tzinfo=UTC),
                        received_offset="+00:00",
                    ),
                ]
            )
            self.calls: list[dict[str, object]] = []

        async def wait_for_new_code(
            self, _url: str, _email: str, _issued_after: datetime, **kwargs: object
        ) -> object:
            self.calls.append(kwargs)
            return next(self.results)

    clock = Clock()
    mailbox = Mailbox()
    original_baseline = SimpleNamespace(verification_code="000000")
    provider = MailboxOtpProvider(
        mailbox,
        sleep=clock.sleep,
        monotonic_now=clock.monotonic,
    )

    code = asyncio.run(
        provider.wait_for_code(
            ProtocolRegistrationRequest(
                "person@example.com",
                email_access_url="https://mail.example/messages",
            ),
            issued_after=datetime(2026, 8, 23, tzinfo=UTC),
            timeout_seconds=30,
            poll_interval_seconds=5,
            excluded_codes={"111111"},
            baseline=original_baseline,
        )
    )

    assert code == "222222"
    assert len(mailbox.calls) == 2
    assert mailbox.calls[0]["baseline"] is original_baseline
    assert mailbox.calls[0]["timeout_seconds"] == 30
    assert mailbox.calls[1]["timeout_seconds"] == 25
    assert mailbox.calls[1]["baseline"].verification_code == "111111"


def test_existing_login_accepts_authorize_continue_http_400() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"url": "https://auth.openai.com/login"}},
            {
                "status_code": 302,
                "headers": {"location": "https://auth.openai.com/log-in"},
            },
            {
                "status_code": 400,
                "body": {"error": {"code": "login_state_already_selected"}},
            },
            {"status_code": 204},
            {"status_code": 200, "body": {}},
            {"status_code": 200, "body": {"accessToken": "AT"}},
        ]
    )
    service = ProtocolRegistrationService(
        config=ProtocolRegistrationConfig(
            auth_session_attempts=1,
            auth_session_delay_seconds=0,
        )
    )

    response = asyncio.run(
        service._login_existing_account(
            transport,
            ProtocolRegistrationRequest("existing@example.com"),
            "device-id",
            "session-id",
            "csrf",
            {"oai-device-id": "device-id"},
            SentinelData(oai_did="device-id"),
            StaticOtpProvider(default="123456"),
            None,
        )
    )

    assert response.body == {"accessToken": "AT"}
    assert any(call.url.endswith("/api/accounts/passwordless/send-otp") for call in transport.calls)
