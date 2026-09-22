from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from backend import email_change_adapter as email_change_adapter_module
from backend.email_change_adapter import (
    EmailChangeRemoteAdapter,
    EmailChangeSession,
    ProtocolEmailChangeLogin,
    _session_account_id,
    _session_email,
    build_default_email_change_adapter,
)
from backend.email_change_models import normalize_email
from backend.email_change_models import EmailChangeRemoteResult
from backend.email_change_service import EmailChangeService
from backend.protocol_registration.transport import FakeTransport
from backend.protocol_registration.otp import MailboxOtpProvider
from backend.protocol_registration.models import ProtocolRegistrationRequest
from backend.oai_payment_extractor.errors import NetworkError
from backend.oai_payment_extractor.web.tasks import classify_failure
from backend.mailbox_client import parse_mailbox_snapshot


def test_normalize_email_is_stable_for_cas_and_reservation_keys() -> None:
    assert normalize_email("  User@Example.COM ") == "user@example.com"


def test_session_account_id_ignores_generic_user_and_account_ids() -> None:
    session = EmailChangeSession(
        transport=FakeTransport([]),
        auth_session={
            "accountId": "generic-account-id",
            "account": {"id": "generic-nested-account-id"},
            "user": {"id": "user-id"},
        },
    )

    assert _session_account_id(session) == ""


def test_session_email_accepts_nested_auth_session_user() -> None:
    session = EmailChangeSession(
        transport=FakeTransport([]),
        auth_session={"session": {"user": {"email": "Nested@Example.COM"}}},
    )

    assert _session_email(session) == "nested@example.com"


def test_session_account_id_accepts_explicit_claims_and_jwt_auth_claim() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-jwt"}}).encode()
    ).rstrip(b"=").decode()
    token = f"{header}.{payload}.signature"

    explicit = EmailChangeSession(
        transport=FakeTransport([]),
        auth_session={"chatgptAccountId": "acct-explicit"},
    )
    from_token = EmailChangeSession(
        transport=FakeTransport([]),
        auth_session={},
        access_token=token,
    )

    assert _session_account_id(explicit) == "acct-explicit"
    assert _session_account_id(from_token) == "acct-jwt"


def test_default_liveness_sends_the_same_explicit_account_id_header() -> None:
    transport = FakeTransport([{"status_code": 200, "body": {"ok": True}}])
    adapter = build_default_email_change_adapter(None)
    session = EmailChangeSession(
        transport=transport,
        auth_session={"chatgpt_account_id": "acct-live"},
        access_token="live-token",
        device_id="device-live",
    )

    assert asyncio.run(adapter.liveness(session)) == 200
    headers = transport.calls[0].kwargs["headers"]
    assert headers["ChatGPT-Account-Id"] == "acct-live"


def test_mailbox_login_purpose_parses_login_code_without_widening_registration_parser() -> None:
    payload = "ChatGPT login code\n\n654321\n2026-08-24T00:00:00Z"
    assert parse_mailbox_snapshot(payload, "text/plain").verification_code is None
    assert parse_mailbox_snapshot(payload, "text/plain", purpose="login").verification_code == "654321"
    assert parse_mailbox_snapshot(payload, "text/plain", purpose="email_change").verification_code == "654321"


def test_mailbox_verification_purpose_does_not_widen_login_parser() -> None:
    payload = "Your temporary ChatGPT verification code\n\n123456\n2026-08-24T00:00:00Z"
    assert parse_mailbox_snapshot(payload, "text/plain").verification_code == "123456"
    assert parse_mailbox_snapshot(payload, "text/plain", purpose="login").verification_code is None
    assert parse_mailbox_snapshot(payload, "text/plain", purpose="email_change").verification_code == "123456"


def test_protocol_email_change_login_uses_login_otp_for_old_and_target_phases() -> None:
    login = ProtocolEmailChangeLogin(None)
    purposes: list[str] = []

    class RequestCaptured(Exception):
        pass

    def capture_request(_account, *, purpose: str):
        purposes.append(purpose)
        raise RequestCaptured

    login._request = capture_request  # type: ignore[method-assign]
    for phase in ("old", "target"):
        with pytest.raises(RequestCaptured):
            asyncio.run(login({"email": "account@example.com"}, phase))

    assert purposes == ["login", "login"]


def test_mailbox_otp_provider_forwards_login_purpose_to_mailbox_client() -> None:
    calls: list[str] = []

    class Mailbox:
        async def wait_for_new_code(self, *_args, **kwargs):
            calls.append(str(kwargs.get("purpose")))
            return type("Code", (), {"verification_code": "654321"})()

    request = ProtocolRegistrationRequest(
        "login@example.com",
        email_access_url="https://mail.example.test/inbox",
        metadata={"otpPurpose": "login"},
    )
    result = asyncio.run(
        MailboxOtpProvider(Mailbox()).wait_for_code(
            request,
            issued_after=datetime.now(timezone.utc),
            timeout_seconds=1,
            poll_interval_seconds=0,
        )
    )
    assert result == "654321"
    assert calls == ["login"]


def test_remote_adapter_uses_baseline_endpoint_order_and_target_liveness() -> None:
    old_transport = FakeTransport(
        [
            {"status_code": 200, "body": {"eligible": True, "eligibility_type": "email"}},
            {"status_code": 202, "body": {"success": True}},
            {"status_code": 200, "body": {"success": True}},
        ]
    )
    target_transport = FakeTransport(
        [{"status_code": 200, "body": {"ok": True}}]
    )
    login_calls: list[str] = []
    mailbox_purposes: list[str] = []

    async def login(account: dict[str, object], purpose: str) -> EmailChangeSession:
        login_calls.append(f"{purpose}:{account['email']}")
        if purpose == "old":
            return EmailChangeSession(
                transport=old_transport,
                auth_session={"user": {"email": "old@example.com"}},
                access_token="old-token",
                cookie_header="session=old",
                device_id="old-device",
            )
        return EmailChangeSession(
            transport=target_transport,
            auth_session={"user": {"email": "new@example.com"}},
            access_token="new-token",
            cookie_header="session=new",
            device_id="new-device",
        )

    async def capture_baseline(_target: dict[str, object], *, purpose: str):
        mailbox_purposes.append(f"baseline:{purpose}")
        return "target-baseline"

    async def wait_for_code(
        _target: dict[str, object],
        *,
        issued_after: datetime,
        purpose: str,
        baseline: object,
    ) -> str:
        assert issued_after.tzinfo is not None
        assert baseline == "target-baseline"
        mailbox_purposes.append(f"wait:{purpose}")
        return "123456"

    async def liveness(session: EmailChangeSession) -> int:
        assert session.access_token == "new-token"
        return 200

    adapter = EmailChangeRemoteAdapter(
        login=login,
        wait_for_code=wait_for_code,
        capture_baseline=capture_baseline,
        liveness=liveness,
    )
    result = asyncio.run(
        adapter.change(
            {"email": "old@example.com", "emailAccessUrl": "old-url"},
            {"email": "new@example.com", "accessUrl": "new-url"},
        )
    )

    assert result.ok is True
    assert login_calls == ["old:old@example.com", "target:new@example.com"]
    assert mailbox_purposes == ["baseline:email_change", "wait:email_change"]
    assert [call.method for call in old_transport.calls] == ["GET", "POST", "POST"]
    assert old_transport.calls[0].url.endswith("/change_email/eligibility")
    assert old_transport.calls[1].url.endswith("/change_email/begin")
    assert old_transport.calls[2].url.endswith("/change_email/verify")
    assert old_transport.calls[1].kwargs["json"] == {"email": "new@example.com"}
    assert old_transport.calls[2].kwargs["json"] == {
        "email": "new@example.com",
        "code": "[redacted]",
    }
    assert target_transport.calls == []


def test_change_target_login_drops_old_mailcom_credentials_for_legacy_url_mailbox() -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"eligible": True}},
            {"status_code": 202, "body": {"success": True}},
            {"status_code": 200, "body": {"success": True}},
        ]
    )
    target_inputs: list[dict[str, object]] = []

    async def login(account: dict[str, object], purpose: str) -> EmailChangeSession:
        if purpose == "target":
            target_inputs.append(dict(account))
        return EmailChangeSession(
            transport=transport,
            auth_session={
                "chatgpt_account_id": "provider-1",
                "email": account["email"],
            },
            access_token="token",
        )

    result = asyncio.run(
        EmailChangeRemoteAdapter(
            login=login,
            wait_for_code=lambda *_args, **_kwargs: "123456",
            liveness=lambda _session: 200,
        ).change(
            {
                "email": "old@example.com",
                "mailboxKind": "mailcom_imap",
                "mailboxPassword": "old-secret",
            },
            {"email": "new@example.com", "accessUrl": "https://mail.test/new"},
        )
    )

    assert result.ok is True
    assert target_inputs[0]["mailboxKind"] == "url"
    assert "mailboxPassword" not in target_inputs[0]


def test_reconcile_target_login_drops_old_mailcom_credentials_for_legacy_url_mailbox() -> None:
    target_inputs: list[dict[str, object]] = []

    async def login(account: dict[str, object], purpose: str) -> EmailChangeSession:
        assert purpose == "target"
        target_inputs.append(dict(account))
        return EmailChangeSession(
            transport=FakeTransport([]),
            auth_session={
                "chatgpt_account_id": "provider-1",
                "email": account["email"],
            },
            access_token="token",
        )

    result = asyncio.run(
        EmailChangeRemoteAdapter(
            login=login,
            wait_for_code=lambda *_args, **_kwargs: "123456",
            liveness=lambda _session: 200,
        ).reconcile(
            {
                "email": "old@example.com",
                "mailboxKind": "mailcom_imap",
                "mailboxPassword": "old-secret",
            },
            {"email": "new@example.com", "accessUrl": "https://mail.test/new"},
            provider_account_id="provider-1",
        )
    )

    assert result.ok is True
    assert target_inputs[0]["mailboxKind"] == "url"
    assert "mailboxPassword" not in target_inputs[0]


def test_remote_adapter_captures_otp_cutoff_before_begin(monkeypatch) -> None:
    begin_started = False
    cutoff = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    class Clock:
        @classmethod
        def now(cls, tz=None):
            assert begin_started is False, "OTP cutoff was captured after begin"
            return cutoff

    class Transport:
        async def request(self, _method: str, url: str, **_kwargs):
            nonlocal begin_started
            if url.endswith("/eligibility"):
                return type("Response", (), {"status_code": 200, "body": {"eligible": True}})()
            if url.endswith("/begin"):
                begin_started = True
                return type("Response", (), {"status_code": 202, "body": {"success": True}})()
            return type("Response", (), {"status_code": 200, "body": {"success": True}})()

        async def close(self):
            return None

    transport = Transport()

    async def login(account: dict[str, object], _purpose: str) -> EmailChangeSession:
        return EmailChangeSession(
            transport=transport,
            auth_session={"user": {"email": account["email"]}},
            access_token="token",
        )

    async def wait_for_code(_target: dict[str, object], *, issued_after: datetime) -> str:
        assert issued_after == cutoff
        return "123456"

    monkeypatch.setattr(email_change_adapter_module, "datetime", Clock)
    result = asyncio.run(
        EmailChangeRemoteAdapter(
            login=login,
            wait_for_code=wait_for_code,
            liveness=lambda _session: 200,
        ).change(
            {"email": "old@example.com"},
            {"email": "new@example.com", "accessUrl": "target"},
        )
    )

    assert result.ok is True


def test_remote_adapter_rejects_social_eligibility_before_begin() -> None:
    transport = FakeTransport(
        [{"status_code": 200, "body": {"eligible": True, "eligibility_type": "google"}}]
    )

    async def login(_account: dict[str, object], _purpose: str) -> EmailChangeSession:
        return EmailChangeSession(
            transport=transport,
            auth_session={"user": {"email": "old@example.com"}},
            access_token="token",
            cookie_header="session=old",
            device_id="device",
        )

    adapter = EmailChangeRemoteAdapter(
        login=login,
        wait_for_code=lambda *_args, **_kwargs: "123456",
        liveness=lambda _session: 200,
    )
    result = asyncio.run(
        adapter.change(
            {"email": "old@example.com"},
            {"email": "new@example.com", "accessUrl": "target"},
        )
    )

    assert result.ok is False
    assert result.stage == "eligibility"
    assert result.error_code == "social_account_change_not_supported"
    assert len(transport.calls) == 1


def test_remote_adapter_marks_verify_boundary_when_target_relogin_fails() -> None:
    """A successful provider verify must never look like a reversible failure."""

    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"eligible": True}},
            {"status_code": 202, "body": {"success": True}},
            {"status_code": 200, "body": {"success": True}},
        ]
    )
    callbacks: list[str] = []

    async def login(account: dict[str, object], purpose: str) -> EmailChangeSession:
        if purpose == "target":
            raise RuntimeError("target mailbox temporarily unavailable")
        return EmailChangeSession(
            transport=transport,
            auth_session={"chatgpt_account_id": "provider-1", "email": account["email"]},
            access_token="token",
        )

    async def before_verify(provider_id: str) -> None:
        callbacks.append(f"before:{provider_id}")

    async def after_verify(provider_id: str) -> None:
        callbacks.append(f"after:{provider_id}")

    adapter = EmailChangeRemoteAdapter(
        login=login,
        wait_for_code=lambda *_args, **_kwargs: "123456",
        liveness=lambda _session: 200,
    )
    result = asyncio.run(
        adapter.change(
            {"email": "old@example.com"},
            {"email": "new@example.com"},
            before_verify=before_verify,
            after_verify=after_verify,
        )
    )

    assert result.ok is False
    assert result.stage == "relogin"
    assert result.remote_confirmed is True
    assert result.provider_account_id == "provider-1"
    assert callbacks == ["before:provider-1", "after:provider-1"]


@pytest.mark.parametrize(
    ("status_code", "response_body", "remote_rejected", "remote_uncertain"),
    [
        (400, {"success": False}, True, False),
        (200, {"success": False}, True, False),
        (302, {}, False, True),
        (500, {"success": False}, False, True),
        (0, {}, False, True),
    ],
)
def test_remote_adapter_classifies_verify_outcome(
    status_code: int,
    response_body: dict[str, object],
    remote_rejected: bool,
    remote_uncertain: bool,
) -> None:
    transport = FakeTransport(
        [
            {"status_code": 200, "body": {"eligible": True}},
            {"status_code": 202, "body": {"success": True}},
            {"status_code": status_code, "body": response_body},
        ]
    )

    async def login(account: dict[str, object], _purpose: str) -> EmailChangeSession:
        return EmailChangeSession(
            transport=transport,
            auth_session={"chatgpt_account_id": "provider-1", "email": account["email"]},
            access_token="token",
        )

    result = asyncio.run(
        EmailChangeRemoteAdapter(
            login=login,
            wait_for_code=lambda *_args, **_kwargs: "123456",
            liveness=lambda _session: 200,
        ).change(
            {"email": "old@example.com"},
            {"email": "new@example.com"},
        )
    )

    assert result.ok is False
    assert result.stage == "verify"
    assert result.remote_rejected is remote_rejected
    assert result.remote_uncertain is remote_uncertain


def test_service_reconciles_uncertain_run_through_target_login() -> None:
    store = FakeChangeStore(
        run={
            "runId": "run-reconcile",
            "status": "target_reserved",
            "accountId": "account-1",
            "remoteUncertain": True,
            "providerAccountId": "provider-1",
        },
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        changed = False
        reconciled = False

        async def change(self, _account, _target, **_kwargs):
            self.changed = True
            raise AssertionError("uncertain runs must not replay old-email flow")

        async def reconcile(self, _account, _target, provider_account_id=""):
            self.reconciled = True
            assert provider_account_id == "provider-1"
            return EmailChangeRemoteResult(
                ok=True,
                stage="remote_verified",
                remote_confirmed=True,
                provider_account_id="provider-1",
            )

    adapter = Adapter()
    result = asyncio.run(EmailChangeService(store=store, adapter=adapter).process("run-reconcile"))

    assert result["status"] == "cleanup_pending"
    assert adapter.reconciled is True
    assert adapter.changed is False


@dataclass
class FakeChangeStore:
    run: dict[str, object]
    account: dict[str, object]
    target: dict[str, object]
    consumed: bool = False
    released: bool = False
    committed: bool = False

    async def claim(self, _run_id: str) -> dict[str, object]:
        self.run.setdefault("claimOwner", "worker-1")
        return dict(self.run)

    async def load_private_inputs(self, _run: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        return dict(self.account), dict(self.target)

    async def mark(self, _run_id: str, **changes: object) -> dict[str, object]:
        self.run.update(changes)
        return dict(self.run)

    async def finish_remote_rejection(
        self,
        _run_id: str,
        *,
        claim_owner: str = "",
    ) -> dict[str, object] | None:
        if (
            claim_owner != self.run.get("claimOwner")
            or self.run.get("status") != "remote_submitted"
            or bool(self.run.get("remoteConfirmed"))
            or not bool(self.run.get("remoteUncertain"))
        ):
            return None
        self.run["remoteUncertain"] = False
        return dict(self.run)

    async def commit_account_email(self, _run: dict[str, object], _target: dict[str, object], _remote: object) -> bool:
        self.committed = True
        return True

    async def consume_target(self, _run: dict[str, object]) -> bool:
        self.consumed = True
        return False

    async def release_target(self, _run: dict[str, object]) -> bool:
        self.released = True
        return True


def test_service_marks_cleanup_pending_when_target_consumption_fails() -> None:
    store = FakeChangeStore(
        run={"runId": "run-1", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        async def change(self, _account: dict[str, object], _target: dict[str, object]):
            return type("Remote", (), {"ok": True, "stage": "remote_verified", "error_code": "", "error_message": "", "account_updates": {}})()

    service = EmailChangeService(store=store, adapter=Adapter())
    result = asyncio.run(service.process("run-1"))

    assert result["status"] == "cleanup_pending"
    assert store.committed is True
    assert store.consumed is True
    assert store.released is False


def test_service_retains_target_after_remote_verify_then_target_relogin_failure() -> None:
    store = FakeChangeStore(
        run={"runId": "run-post-verify", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        async def change(self, _account, _target, **_callbacks):
            return EmailChangeRemoteResult(
                ok=False,
                stage="relogin",
                error_code="target_login_failed",
                remote_confirmed=True,
                provider_account_id="provider-1",
            )

    result = asyncio.run(EmailChangeService(store=store, adapter=Adapter()).process("run-post-verify"))
    assert result["status"] == "cleanup_pending"
    assert store.released is False


def test_service_releases_target_after_definitive_verify_rejection() -> None:
    store = FakeChangeStore(
        run={"runId": "run-rejected", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        async def change(self, _account, _target, *, before_verify=None, after_verify=None):
            del after_verify
            assert before_verify is not None
            await before_verify("provider-1")
            return EmailChangeRemoteResult(
                ok=False,
                stage="verify",
                error_code="change_email_verify_http_400",
                remote_rejected=True,
                provider_account_id="provider-1",
            )

    result = asyncio.run(
        EmailChangeService(store=store, adapter=Adapter()).process("run-rejected")
    )

    assert result["status"] == "failed"
    assert result["errorCode"] == "change_email_verify_http_400"
    assert store.run["remoteUncertain"] is False
    assert store.released is True


def test_service_retains_target_when_rejection_marker_cannot_be_cleared_atomically() -> None:
    store = FakeChangeStore(
        run={"runId": "run-rejected-no-cas", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    async def get_private(_run_id):
        return dict(store.run)

    store.get_private = get_private  # type: ignore[attr-defined]
    store.finish_remote_rejection = None  # type: ignore[attr-defined]

    class Adapter:
        async def change(self, _account, _target, *, before_verify=None, after_verify=None):
            del after_verify
            assert before_verify is not None
            await before_verify("provider-1")
            return EmailChangeRemoteResult(
                ok=False,
                stage="verify",
                error_code="change_email_verify_http_400",
                remote_rejected=True,
                provider_account_id="provider-1",
            )

    result = asyncio.run(
        EmailChangeService(store=store, adapter=Adapter()).process("run-rejected-no-cas")
    )

    assert result["status"] == "cleanup_pending"
    assert store.run["remoteUncertain"] is True
    assert store.released is False


def test_service_retains_target_after_uncertain_verify_response() -> None:
    store = FakeChangeStore(
        run={"runId": "run-verify-unknown", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        async def change(self, _account, _target, *, before_verify=None, after_verify=None):
            del after_verify
            assert before_verify is not None
            await before_verify("provider-1")
            return EmailChangeRemoteResult(
                ok=False,
                stage="verify",
                error_code="change_email_verify_http_500",
                remote_uncertain=True,
                provider_account_id="provider-1",
            )

    result = asyncio.run(
        EmailChangeService(store=store, adapter=Adapter()).process("run-verify-unknown")
    )

    assert result["status"] == "cleanup_pending"
    assert store.run["remoteUncertain"] is True
    assert store.released is False


def test_service_retains_target_when_local_commit_ack_is_uncertain() -> None:
    store = FakeChangeStore(
        run={"runId": "run-uncertain", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    async def uncertain_commit(_run, _target, _remote):
        store.committed = True
        raise RuntimeError("Authorization: Bearer SECRET_TOKEN")

    store.commit_account_email = uncertain_commit  # type: ignore[method-assign]

    class Adapter:
        async def change(self, _account, _target):
            return EmailChangeRemoteResult(ok=True, stage="remote_verified")

    result = asyncio.run(EmailChangeService(store=store, adapter=Adapter()).process("run-uncertain"))

    assert result["status"] == "cleanup_pending"
    assert result["errorCode"] == "account_commit_uncertain"
    assert store.released is False


def test_service_retains_target_when_recovered_remote_outcome_raises() -> None:
    store = FakeChangeStore(
        run={
            "runId": "run-remote-uncertain",
            "status": "queued",
            "accountId": "account-1",
            "remoteUncertain": True,
        },
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        async def change(self, _account, _target):
            raise RuntimeError("request outcome unknown")

    result = asyncio.run(
        EmailChangeService(store=store, adapter=Adapter()).process("run-remote-uncertain")
    )

    assert result["status"] == "cleanup_pending"
    assert result["errorCode"] == "account_commit_uncertain"
    assert store.released is False


def test_service_observes_remote_uncertainty_recorded_during_active_request() -> None:
    store = FakeChangeStore(
        run={"runId": "run-reconnect", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    async def get_private(_run_id):
        return {**store.run, "remoteUncertain": True}

    store.get_private = get_private  # type: ignore[attr-defined]

    class Adapter:
        async def change(self, _account, _target):
            raise RuntimeError("transport disconnected during reconnect")

    result = asyncio.run(
        EmailChangeService(store=store, adapter=Adapter()).process("run-reconnect")
    )

    assert result["status"] == "cleanup_pending"
    assert store.released is False


def test_service_reconciles_local_state_when_cancel_races_after_remote_success() -> None:
    store = FakeChangeStore(
        run={"runId": "run-cancel", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    async def get_run(_run_id):
        return dict(store.run)

    begin_attempts = 0

    async def begin_commit(_run_id):
        nonlocal begin_attempts
        begin_attempts += 1
        if begin_attempts == 1:
            store.run["cancelRequested"] = True
            return None
        return dict(store.run)

    async def consume_target(_run):
        store.consumed = True
        return True

    store.get = get_run  # type: ignore[attr-defined]
    store.begin_commit = begin_commit  # type: ignore[attr-defined]
    store.consume_target = consume_target  # type: ignore[method-assign]

    class Adapter:
        async def change(self, _account, _target):
            return EmailChangeRemoteResult(ok=True, stage="remote_verified")

    result = asyncio.run(EmailChangeService(store=store, adapter=Adapter()).process("run-cancel"))

    assert result["status"] == "completed"
    assert store.committed is True
    assert store.consumed is True
    assert store.released is False


def test_service_rechecks_remote_boundary_before_honoring_stale_cancel() -> None:
    store = FakeChangeStore(
        run={
            "runId": "run-cancel-boundary",
            "status": "target_reserved",
            "accountId": "account-1",
            "cancelRequested": True,
        },
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    async def claim(_run_id):
        stale = dict(store.run)
        # Simulate before_verify persisting while this worker still owns the
        # cancellation snapshot returned by claim().
        store.run["remoteUncertain"] = True
        return stale

    async def get_private(_run_id):
        return dict(store.run)

    store.claim = claim  # type: ignore[method-assign]
    store.get_private = get_private  # type: ignore[attr-defined]

    class Adapter:
        async def change(self, _account, _target):
            raise AssertionError("stale cancellation must stop before adapter invocation")

    result = asyncio.run(
        EmailChangeService(store=store, adapter=Adapter()).process("run-cancel-boundary")
    )

    assert result["status"] == "cleanup_pending"
    assert result["errorCode"] == "cancel_after_remote_change"
    assert result["cancelRequested"] is False
    assert store.released is False


def test_service_aborts_verify_when_cancellation_wins_boundary_update() -> None:
    store = FakeChangeStore(
        run={"runId": "run-cancel-first", "status": "queued", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    async def get_private(_run_id):
        return dict(store.run)

    async def begin_remote_verify(_run_id, _provider_account_id=""):
        return None if store.run.get("cancelRequested") else dict(store.run)

    store.get_private = get_private  # type: ignore[attr-defined]
    store.begin_remote_verify = begin_remote_verify  # type: ignore[attr-defined]

    class Adapter:
        verify_sent = False

        async def change(self, _account, _target, *, before_verify=None, after_verify=None):
            del after_verify
            store.run["cancelRequested"] = True
            assert before_verify is not None
            await before_verify("provider-1")
            self.verify_sent = True
            raise AssertionError("verify request must not be sent after cancellation wins")

    adapter = Adapter()
    result = asyncio.run(
        EmailChangeService(store=store, adapter=adapter).process("run-cancel-first")
    )

    assert result["status"] == "cancelled"
    assert adapter.verify_sent is False
    assert store.released is True


def test_service_does_not_replay_terminal_run() -> None:
    store = FakeChangeStore(
        run={"runId": "run-done", "status": "completed", "accountId": "account-1"},
        account={"email": "old@example.com"},
        target={"email": "new@example.com", "accessUrl": "target"},
    )

    class Adapter:
        called = False

        async def change(self, _account, _target):
            self.called = True
            raise AssertionError("terminal run must not call remote adapter")

    adapter = Adapter()
    result = asyncio.run(EmailChangeService(store=store, adapter=adapter).process("run-done"))
    assert result["status"] == "completed"
    assert adapter.called is False


def test_side_effect_network_failure_is_not_automatically_retryable() -> None:
    assert classify_failure(NetworkError("payment_confirmation", "timeout"), "payment_confirmation")["retryable"] is False
    assert classify_failure(NetworkError("checkout", "timeout"), "checkout")["retryable"] is True
