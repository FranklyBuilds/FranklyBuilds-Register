from __future__ import annotations

import base64
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .email_change_models import (
    EmailChangeRemoteResult,
    EmailChangeSession,
    normalize_email,
)
from .protocol_registration.models import (
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolResponse,
    SentinelData,
)
from .protocol_registration.otp import MailboxOtpProvider
from .protocol_registration.service import ProtocolRegistrationService, _extract_expiry
from .protocol_registration.transport import CurlCffiTransport


CHANGE_EMAIL_ELIGIBILITY = "/backend-api/accounts/change_email/eligibility"
CHANGE_EMAIL_BEGIN = "/backend-api/accounts/change_email/begin"
CHANGE_EMAIL_VERIFY = "/backend-api/accounts/change_email/verify"


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _session_email(session: EmailChangeSession) -> str:
    body = session.auth_session if isinstance(session.auth_session, Mapping) else {}
    nested_session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
    nested_user = (
        nested_session.get("user")
        if isinstance(nested_session.get("user"), Mapping)
        else {}
    )
    candidates = [
        body.get("email"),
        body.get("user", {}).get("email") if isinstance(body.get("user"), Mapping) else None,
        body.get("account", {}).get("email") if isinstance(body.get("account"), Mapping) else None,
        nested_user.get("email"),
        body.get("user", {}).get("emailAddress") if isinstance(body.get("user"), Mapping) else None,
    ]
    for value in candidates:
        normalized = normalize_email(str(value or ""))
        if normalized:
            return normalized
    return ""


def _target_login_account(
    account: Mapping[str, Any],
    target: Mapping[str, Any],
    target_email: str,
) -> dict[str, Any]:
    candidate = dict(account)
    for key in (
        "accessUrl",
        "access_url",
        "emailAccessUrl",
        "email_access_url",
        "mailboxKind",
        "mailbox_kind",
        "mailboxPassword",
        "mailbox_password",
    ):
        candidate.pop(key, None)
    candidate.update(target)
    candidate["email"] = target_email
    mailbox_kind = str(
        target.get("mailboxKind") or target.get("mailbox_kind") or "url"
    ).strip().casefold()
    candidate["mailboxKind"] = mailbox_kind
    candidate.pop("mailbox_kind", None)
    if mailbox_kind != "mailcom_imap":
        candidate.pop("mailboxPassword", None)
        candidate.pop("mailbox_password", None)
    return candidate


def _session_account_id(session: EmailChangeSession) -> str:
    """Return only the provider's ChatGPT account identifier.

    ``user.id`` and generic ``account.id`` values identify different objects
    in some auth-session responses.  Sending either as ``ChatGPT-Account-Id``
    can route a multi-account session to the wrong workspace, so extraction is
    deliberately limited to explicit ChatGPT claim names and JWT claims.
    """

    direct = str(getattr(session, "chatgpt_account_id", "") or "").strip()
    if direct:
        return direct

    body = session.auth_session if isinstance(session.auth_session, Mapping) else {}
    for mapping in _walk_mappings(body):
        for key in ("chatgpt_account_id", "chatgptAccountId"):
            value = mapping.get(key)
            text = str(value or "").strip()
            if text:
                return text
        for key in ("id_token", "idToken", "access_token", "accessToken", "token"):
            token_id = chatgpt_id_from_token(mapping.get(key))
            if token_id:
                return token_id

    for token in (
        getattr(session, "id_token", ""),
        getattr(session, "access_token", ""),
    ):
        token_id = chatgpt_id_from_token(token)
        if token_id:
            return token_id
    return ""


def chatgpt_id_from_token(value: Any) -> str:
    """Extract an explicit ChatGPT account claim from a token or claim map."""

    if isinstance(value, Mapping):
        for key in ("chatgpt_account_id", "chatgptAccountId"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        auth = value.get("https://api.openai.com/auth")
        if isinstance(auth, Mapping):
            for key in ("chatgpt_account_id", "chatgptAccountId"):
                text = str(auth.get(key) or "").strip()
                if text:
                    return text
        return ""

    token = str(value or "").strip()
    if token.casefold().startswith("bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except Exception:
        return ""
    return chatgpt_id_from_token(payload)


def _walk_mappings(value: Any):
    """Yield nested JSON mappings without treating generic IDs as claims."""

    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_mappings(child)


class EmailChangeRemoteAdapter:
    """Provider adapter for the source repository's authenticated email flow."""

    def __init__(
        self,
        *,
        base_url: str = "https://chatgpt.com",
        login: Callable[[dict[str, Any], str], Any],
        wait_for_code: Callable[..., Any],
        capture_baseline: Callable[..., Any] | None = None,
        liveness: Callable[[EmailChangeSession], Any],
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = str(base_url or "https://chatgpt.com").rstrip("/")
        self.login = login
        self.wait_for_code = wait_for_code
        self.capture_baseline = capture_baseline
        self.liveness = liveness
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    def _headers(self, session: EmailChangeSession, path: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
            "OAI-Language": "en-US",
            "X-OpenAI-Target-Path": path,
            "X-OpenAI-Target-Route": path,
        }
        if session.access_token:
            headers["Authorization"] = f"Bearer {session.access_token}"
        if session.cookie_header:
            headers["Cookie"] = session.cookie_header
        if session.device_id:
            headers["OAI-Device-Id"] = session.device_id
        account_id = _session_account_id(session)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        return headers

    async def _request(
        self,
        session: EmailChangeSession,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "timeout": self.timeout_seconds,
            "headers": self._headers(session, path),
        }
        if payload is not None:
            kwargs["json"] = payload
        response = await _resolve(
            session.transport.request(method, f"{self.base_url}{path}", **kwargs)
        )
        return response

    @staticmethod
    def _body(response: Any) -> dict[str, Any]:
        value = getattr(response, "body", response)
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _failure(
        stage: str,
        code: str,
        message: str = "",
        *,
        remote_confirmed: bool = False,
        remote_uncertain: bool = False,
        remote_rejected: bool = False,
        provider_account_id: str = "",
    ) -> EmailChangeRemoteResult:
        return EmailChangeRemoteResult(
            ok=False,
            stage=stage,
            error_code=str(code)[:120],
            error_message=str(message or code)[:300],
            remote_confirmed=remote_confirmed,
            remote_uncertain=remote_uncertain,
            remote_rejected=remote_rejected,
            provider_account_id=provider_account_id,
        )

    @staticmethod
    def _session_updates(session: EmailChangeSession) -> dict[str, Any]:
        return {
            "authSession": session.auth_session,
            "cookieHeader": session.cookie_header,
            "accessToken": session.access_token,
            "refreshToken": session.refresh_token,
            "accessTokenExpiresAt": session.access_token_expires_at,
            "deviceId": session.device_id,
        }

    async def _callback(self, callback: Callable[[str], Any] | None, account_id: str) -> None:
        if callback is not None:
            await _resolve(callback(account_id))

    async def change(
        self,
        account: dict[str, Any],
        target: dict[str, Any],
        *,
        before_verify: Callable[[str], Any] | None = None,
        after_verify: Callable[[str], Any] | None = None,
    ) -> EmailChangeRemoteResult:
        old_email = normalize_email(account.get("email"))
        target_email = normalize_email(target.get("email"))
        if not old_email or not target_email:
            return self._failure("input", "missing_email")
        if old_email == target_email:
            return self._failure("input", "target_email_same_as_current")

        old_session: EmailChangeSession | None = None
        target_session: EmailChangeSession | None = None
        try:
            old_session = await _resolve(self.login(dict(account), "old"))
            if not isinstance(old_session, EmailChangeSession):
                return self._failure("old_login", "old_login_failed")
            if _session_email(old_session) != old_email:
                return self._failure("old_login", "old_session_email_mismatch")
            provider_account_id = _session_account_id(old_session)

            # Capture the target mailbox before ``begin`` so an old message
            # cannot satisfy the verification step when the provider omits
            # reliable timestamps.
            baseline = None
            if self.capture_baseline is not None:
                try:
                    baseline = await _resolve(
                        self.capture_baseline(dict(target), purpose="email_change")
                    )
                except TypeError as exc:
                    if "purpose" not in str(exc):
                        raise
                    baseline = await _resolve(self.capture_baseline(dict(target)))

            eligibility = await self._request(old_session, "GET", CHANGE_EMAIL_ELIGIBILITY)
            body = self._body(eligibility)
            status = int(getattr(eligibility, "status_code", 0) or 0)
            if status < 200 or status >= 300:
                return self._failure("eligibility", f"eligibility_http_{status}")
            if body.get("eligible") is False or body.get("success") is False:
                return self._failure("eligibility", "change_email_ineligible")
            eligibility_type = str(body.get("eligibility_type") or body.get("eligibilityType") or "").strip().casefold()
            if eligibility_type and eligibility_type not in {"password", "email", "otp"}:
                return self._failure("eligibility", "social_account_change_not_supported")

            issued_after = datetime.now(timezone.utc)
            begun = await self._request(
                old_session,
                "POST",
                CHANGE_EMAIL_BEGIN,
                {"email": target_email},
            )
            status = int(getattr(begun, "status_code", 0) or 0)
            begun_body = self._body(begun)
            if status < 200 or status >= 300 or begun_body.get("success") is False:
                return self._failure("begin", f"change_email_begin_http_{status}")

            try:
                pending_code = self.wait_for_code(
                    dict(target),
                    issued_after=issued_after,
                    purpose="email_change",
                    baseline=baseline,
                )
            except TypeError as exc:
                if "purpose" not in str(exc) and "baseline" not in str(exc):
                    raise
                pending_code = self.wait_for_code(dict(target), issued_after=issued_after)
            code = await _resolve(pending_code)
            code = str(code or "").strip()
            if len(code) != 6 or not code.isdigit():
                return self._failure("otp", "otp_timeout")
            # Persist the uncertain boundary immediately before the provider
            # request.  A timeout after this point is never safe to replay.
            await self._callback(before_verify, provider_account_id)
            try:
                verified = await self._request(
                    old_session,
                    "POST",
                    CHANGE_EMAIL_VERIFY,
                    {"email": target_email, "code": code},
                )
            except Exception as exc:
                return self._failure(
                    "verify",
                    "change_email_verify_unknown",
                    str(exc),
                    remote_uncertain=True,
                    provider_account_id=provider_account_id,
                )
            status = int(getattr(verified, "status_code", 0) or 0)
            verified_body = self._body(verified)
            if status < 200 or status >= 300 or verified_body.get("success") is False:
                # A concrete 4xx/business rejection is definitive.  Network
                # and 5xx responses remain uncertain because the provider may
                # have committed the mutation before the response was lost.
                rejected = (
                    400 <= status < 500
                    or (200 <= status < 300 and verified_body.get("success") is False)
                )
                return self._failure(
                    "verify",
                    f"change_email_verify_http_{status}",
                    remote_uncertain=not rejected,
                    remote_rejected=rejected,
                    provider_account_id=provider_account_id,
                )

            marker_uncertain = False
            try:
                await self._callback(after_verify, provider_account_id)
            except Exception:
                # The provider has already accepted the change.  Keep going so
                # the caller can persist a durable uncertainty marker and
                # reconcile on the next worker pass.
                marker_uncertain = True

            target_account = _target_login_account(account, target, target_email)
            try:
                target_session = await _resolve(self.login(target_account, "target"))
            except Exception as exc:
                return self._failure(
                    "relogin",
                    "target_login_failed",
                    str(exc),
                    remote_confirmed=not marker_uncertain,
                    remote_uncertain=marker_uncertain,
                    provider_account_id=provider_account_id,
                )
            if not isinstance(target_session, EmailChangeSession):
                return self._failure(
                    "relogin",
                    "target_login_failed",
                    remote_confirmed=not marker_uncertain,
                    remote_uncertain=marker_uncertain,
                    provider_account_id=provider_account_id,
                )
            if _session_email(target_session) != target_email:
                return self._failure(
                    "relogin",
                    "target_session_email_mismatch",
                    remote_confirmed=not marker_uncertain,
                    remote_uncertain=marker_uncertain,
                    provider_account_id=provider_account_id,
                )
            target_account_id = _session_account_id(target_session)
            if provider_account_id and target_account_id and target_account_id != provider_account_id:
                return self._failure(
                    "relogin",
                    "target_session_account_mismatch",
                    remote_confirmed=not marker_uncertain,
                    remote_uncertain=marker_uncertain,
                    provider_account_id=provider_account_id,
                )

            try:
                live = await _resolve(self.liveness(target_session))
            except Exception as exc:
                return self._failure(
                    "liveness",
                    "liveness_failed",
                    str(exc),
                    remote_confirmed=not marker_uncertain,
                    remote_uncertain=marker_uncertain,
                    provider_account_id=provider_account_id,
                )
            status_code = int(live.get("status_code", 0) if isinstance(live, Mapping) else live or 0)
            if status_code != 200:
                return self._failure(
                    "liveness",
                    "liveness_failed",
                    remote_confirmed=not marker_uncertain,
                    remote_uncertain=marker_uncertain,
                    provider_account_id=provider_account_id,
                )
            return EmailChangeRemoteResult(
                ok=True,
                stage="remote_verified",
                account_updates=self._session_updates(target_session),
                diagnostics={"livenessStatus": status_code},
                remote_confirmed=not marker_uncertain,
                remote_uncertain=marker_uncertain,
                provider_account_id=provider_account_id,
            )
        finally:
            for session in (old_session, target_session):
                if session is None:
                    continue
                close = getattr(session.transport, "close", None)
                if callable(close):
                    try:
                        await _resolve(close())
                    except Exception:
                        pass

    async def reconcile(
        self,
        account: dict[str, Any],
        target: dict[str, Any],
        provider_account_id: str = "",
    ) -> EmailChangeRemoteResult:
        """Confirm an uncertain remote change using the target mailbox only.

        This path deliberately never logs into the old mailbox and never calls
        ``change_email/begin`` or ``change_email/verify`` again.
        """

        target_email = normalize_email(target.get("email") or account.get("email"))
        expected_id = str(provider_account_id or "").strip()
        if not target_email or not expected_id:
            return self._failure("reconcile", "provider_account_id_missing", remote_uncertain=True, provider_account_id=expected_id)
        session: EmailChangeSession | None = None
        try:
            target_account = _target_login_account(account, target, target_email)
            try:
                session = await _resolve(self.login(target_account, "target"))
            except Exception as exc:
                return self._failure("reconcile", "target_login_failed", str(exc), remote_uncertain=True, provider_account_id=expected_id)
            if not isinstance(session, EmailChangeSession):
                return self._failure("reconcile", "target_login_failed", remote_uncertain=True, provider_account_id=expected_id)
            if _session_email(session) != target_email:
                return self._failure("reconcile", "target_session_email_mismatch", remote_uncertain=True, provider_account_id=expected_id)
            actual_id = _session_account_id(session)
            if not actual_id or actual_id != expected_id:
                return self._failure("reconcile", "target_session_account_mismatch", remote_uncertain=True, provider_account_id=expected_id)
            try:
                live = await _resolve(self.liveness(session))
            except Exception as exc:
                return self._failure("reconcile", "liveness_failed", str(exc), remote_uncertain=True, provider_account_id=expected_id)
            status_code = int(live.get("status_code", 0) if isinstance(live, Mapping) else live or 0)
            if status_code != 200:
                return self._failure("reconcile", "liveness_failed", remote_uncertain=True, provider_account_id=expected_id)
            return EmailChangeRemoteResult(
                ok=True,
                stage="remote_verified",
                account_updates=self._session_updates(session),
                diagnostics={"livenessStatus": status_code, "reconciled": True},
                remote_confirmed=True,
                provider_account_id=expected_id,
            )
        finally:
            if session is not None:
                close = getattr(session.transport, "close", None)
                if callable(close):
                    try:
                        await _resolve(close())
                    except Exception:
                        pass


__all__ = [
    "CHANGE_EMAIL_BEGIN",
    "CHANGE_EMAIL_ELIGIBILITY",
    "CHANGE_EMAIL_VERIFY",
    "EmailChangeRemoteAdapter",
    "EmailChangeRemoteResult",
    "EmailChangeSession",
]


class ProtocolEmailChangeLogin:
    """Login bridge that reuses the protocol service's existing-account path."""

    def __init__(
        self,
        mailbox_client: Any,
        *,
        config: ProtocolRegistrationConfig | None = None,
        proxy: str | None = None,
    ) -> None:
        self.mailbox_client = mailbox_client
        self.config = config or ProtocolRegistrationConfig(
            registration_mode="passwordless",
            auth_session_delay_seconds=1.0,
        )
        self.proxy = str(proxy or "").strip() or None

    @staticmethod
    def _request(account: Mapping[str, Any], *, purpose: str) -> ProtocolRegistrationRequest:
        email = normalize_email(account.get("email"))
        metadata = {
            "otpPurpose": purpose,
            "mailboxKind": account.get("mailboxKind") or account.get("mailbox_kind") or "",
            "mailboxPassword": account.get("mailboxPassword") or account.get("mailbox_password") or "",
        }
        return ProtocolRegistrationRequest(
            email,
            email_access_url=str(
                account.get("accessUrl")
                or account.get("access_url")
                or account.get("emailAccessUrl")
                or account.get("email_access_url")
                or ""
            ),
            proxy=str(account.get("proxy") or "").strip() or None,
            device_id=str(account.get("deviceId") or account.get("device_id") or "").strip() or None,
            metadata=metadata,
        )

    async def capture_baseline(
        self,
        account: dict[str, Any],
        *,
        purpose: str = "email_change",
    ) -> Any | None:
        request = self._request(account, purpose=purpose)
        provider = MailboxOtpProvider(self.mailbox_client)
        return await provider.capture_baseline(request)

    async def __call__(self, account: dict[str, Any], purpose: str) -> EmailChangeSession:
        # ``purpose`` identifies the email-change phase (old/target).  Both
        # phases perform an account login and therefore expect login-code mail.
        request = self._request(account, purpose="login")
        proxy = str(account.get("proxy") or self.proxy or "").strip() or None
        request.proxy = proxy
        transport = CurlCffiTransport(
            proxy=proxy,
            timeout=self.config.timeout_seconds,
            user_agent=self.config.user_agent,
        )
        service = ProtocolRegistrationService(
            config=self.config,
            transport_factory=lambda **_kwargs: transport,
            otp_provider=MailboxOtpProvider(self.mailbox_client),
        )
        try:
            sentinel_provider = service.sentinel_provider
            sentinel_value = await sentinel_provider.get(request, self.config, transport)
            sentinel = sentinel_value if isinstance(sentinel_value, SentinelData) else SentinelData.from_value(sentinel_value)
            did = request.device_id or sentinel.oai_did or str(uuid4())
            await service._prime_transport(transport, sentinel, did, "passwordless")
            base_headers = service._base_headers(did)
            await service._request(
                transport,
                "GET",
                service._url(self.config.chat_base_url, "/"),
                label="email_change_prime",
                headers={**base_headers, "Accept": "text/html,application/xhtml+xml"},
            )
            csrf = await service._request(
                transport,
                "GET",
                service._url(self.config.chat_base_url, "/api/auth/csrf"),
                label="email_change_csrf",
                headers={**base_headers, "Accept": "application/json", "Referer": self.config.chat_base_url + "/"},
            )
            csrf_body = csrf.body if isinstance(csrf.body, Mapping) else {}
            csrf_token = str(csrf_body.get("csrfToken") or "").strip()
            if csrf.status_code >= 400 or not csrf_token:
                raise RuntimeError("email_change_csrf_failed")
            otp_provider = MailboxOtpProvider(self.mailbox_client)
            baseline = await otp_provider.capture_baseline(request)
            auth_session = await service._login_existing_account(
                transport,
                request,
                did,
                request.session_logging_id or uuid4().hex,
                csrf_token,
                base_headers,
                sentinel,
                otp_provider,
                baseline,
            )
            body = auth_session.body if isinstance(auth_session.body, Mapping) else {}
            session_body = body.get("session") if isinstance(body.get("session"), Mapping) else {}
            access_token = str(
                body.get("accessToken")
                or body.get("access_token")
                or session_body.get("accessToken")
                or session_body.get("access_token")
                or ""
            ).strip()
            if not access_token:
                raise RuntimeError("email_change_auth_session_missing_token")
            refresh_token = str(
                body.get("refreshToken")
                or body.get("refresh_token")
                or session_body.get("refreshToken")
                or session_body.get("refresh_token")
                or ""
            ).strip()
            return EmailChangeSession(
                transport=transport,
                auth_session=dict(body),
                access_token=access_token,
                refresh_token=refresh_token,
                access_token_expires_at=_extract_expiry(body),
                cookie_header=transport.cookie_header(),
                device_id=did,
            )
        except Exception:
            await transport.close()
            raise


def build_default_email_change_adapter(
    mailbox_client: Any,
    *,
    config: ProtocolRegistrationConfig | None = None,
    proxy: str | None = None,
    liveness_timeout_seconds: float = 30.0,
) -> EmailChangeRemoteAdapter:
    login = ProtocolEmailChangeLogin(mailbox_client, config=config, proxy=proxy)
    effective_config = config or login.config

    async def wait_for_code(
        target: dict[str, Any],
        *,
        issued_after: datetime,
        purpose: str = "email_change",
        baseline: Any | None = None,
    ) -> str | None:
        request = login._request(target, purpose=purpose)
        provider = MailboxOtpProvider(mailbox_client)
        return await provider.wait_for_code(
            request,
            issued_after=issued_after,
            timeout_seconds=effective_config.otp_timeout_seconds,
            poll_interval_seconds=effective_config.otp_poll_interval_seconds,
            baseline=baseline,
            purpose=purpose,
        )

    async def capture_baseline(
        target: dict[str, Any],
        *,
        purpose: str = "verification",
    ) -> Any | None:
        return await login.capture_baseline(target, purpose=purpose)

    async def liveness(session: EmailChangeSession) -> int:
        path = "/backend-api/wham/usage"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {session.access_token}",
            "User-Agent": effective_config.user_agent,
            "OAI-Device-Id": session.device_id,
        }
        account_id = _session_account_id(session)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        if session.cookie_header:
            headers["Cookie"] = session.cookie_header
        response = await _resolve(
            session.transport.request(
                "GET",
                f"{effective_config.chat_base_url.rstrip('/')}{path}",
                timeout=max(5.0, float(liveness_timeout_seconds)),
                headers=headers,
            )
        )
        return int(getattr(response, "status_code", 0) or 0)

    return EmailChangeRemoteAdapter(
        base_url=effective_config.chat_base_url,
        login=login,
        wait_for_code=wait_for_code,
        capture_baseline=capture_baseline,
        liveness=liveness,
        timeout_seconds=effective_config.timeout_seconds,
    )


__all__ += ["ProtocolEmailChangeLogin", "build_default_email_change_adapter"]
