"""OTP provider adapters used by protocol registration."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Callable, Protocol

from .models import ProtocolRegistrationRequest


class OtpProvider(Protocol):
    async def wait_for_code(
        self,
        request: ProtocolRegistrationRequest,
        *,
        issued_after: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        excluded_codes: set[str] | None = None,
        baseline: Any | None = None,
        purpose: str = "verification",
    ) -> str | None:
        """Wait for a fresh six-digit code."""


class StaticOtpProvider:
    """A deterministic OTP provider for tests and local operator workflows."""

    def __init__(self, codes: dict[str, str | list[str]] | None = None, default: str | None = None) -> None:
        self._codes: dict[str, deque[str]] = defaultdict(deque)
        for email, value in (codes or {}).items():
            values = value if isinstance(value, list) else [value]
            self._codes[str(email).strip().lower()].extend(str(item) for item in values)
        self.default = str(default or "")
        self.calls: list[dict[str, Any]] = []

    async def wait_for_code(
        self,
        request: ProtocolRegistrationRequest,
        *,
        issued_after: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        excluded_codes: set[str] | None = None,
        baseline: Any | None = None,
        purpose: str = "verification",
    ) -> str | None:
        self.calls.append(
            {
                "email": request.email,
                "issued_after": issued_after,
                "timeout_seconds": timeout_seconds,
                "baseline": baseline,
                "purpose": purpose,
            }
        )
        queue = self._codes.get(request.email)
        excluded = excluded_codes or set()
        while queue:
            code = queue.popleft()
            if code and code not in excluded:
                return code
        return self.default if self.default and self.default not in excluded else None

    def push(self, email: str, code: str) -> None:
        self._codes[str(email).strip().lower()].append(str(code))


class CallableOtpProvider:
    """Wrap a synchronous or asynchronous callback as an OTP provider."""

    def __init__(self, callback: Callable[..., Any]) -> None:
        self.callback = callback

    async def wait_for_code(
        self,
        request: ProtocolRegistrationRequest,
        *,
        issued_after: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        excluded_codes: set[str] | None = None,
        baseline: Any | None = None,
        purpose: str = "verification",
    ) -> str | None:
        del baseline
        try:
            value = self.callback(
                request,
                issued_after=issued_after,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                excluded_codes=excluded_codes or set(),
                purpose=purpose,
            )
        except TypeError as exc:
            if "purpose" not in str(exc):
                raise
            value = self.callback(request, issued_after=issued_after, timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds, excluded_codes=excluded_codes or set())
        if inspect.isawaitable(value):
            value = await value
        return str(value).strip() if value else None


class MailboxOtpProvider:
    """Bridge the app's async ``MailboxClient`` to the protocol contract."""

    def __init__(
        self,
        mailbox_client: Any,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        self.mailbox_client = mailbox_client
        self._sleep = sleep
        self._monotonic_now = monotonic_now

    @staticmethod
    def _access_url(request: ProtocolRegistrationRequest) -> str:
        if not request.email_access_url:
            return ""
        access_url = request.email_access_url
        # Keep mailbox URL handling consistent with the browser worker (notably
        # API798 auth-code links) without importing the browser registration
        # stack into the protocol package.
        try:
            from ..mailbox_client import direct_mailbox_access_url, mailbox_source_for_document

            metadata = request.metadata if isinstance(request.metadata, dict) else {}
            if metadata.get("mailboxKind") or metadata.get("mailbox_kind"):
                access_url = mailbox_source_for_document(
                    {
                        "email": request.email,
                        "accessUrl": access_url,
                        "mailboxKind": metadata.get("mailboxKind") or metadata.get("mailbox_kind"),
                        "mailboxPassword": metadata.get("mailboxPassword") or metadata.get("mailbox_password"),
                    }
                )

            access_url = direct_mailbox_access_url(access_url, request.email)
        except Exception:
            pass
        return access_url

    async def capture_baseline(self, request: ProtocolRegistrationRequest) -> Any | None:
        access_url = self._access_url(request)
        if not access_url:
            return None
        try:
            return await self.mailbox_client.get_snapshot(access_url, request.email)
        except Exception as exc:
            if not bool(getattr(exc, "retryable", False)):
                raise
            from ..mailbox_client import MailboxSnapshot

            return MailboxSnapshot(
                fingerprint="",
                verification_code=None,
                received_at_utc=None,
                received_offset=None,
            )

    async def wait_for_code(
        self,
        request: ProtocolRegistrationRequest,
        *,
        issued_after: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        excluded_codes: set[str] | None = None,
        baseline: Any | None = None,
        purpose: str = "verification",
    ) -> str | None:
        access_url = self._access_url(request)
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        requested_purpose = str(
            metadata.get("otpPurpose")
            or metadata.get("otp_purpose")
            or purpose
            or "verification"
        )
        if not access_url:
            return None
        loop = asyncio.get_running_loop()
        monotonic_now = self._monotonic_now or loop.time
        deadline = monotonic_now() + max(0.0, float(timeout_seconds))
        excluded = {str(code).strip() for code in (excluded_codes or set()) if code}
        current_baseline = baseline

        while True:
            remaining = deadline - monotonic_now()
            if remaining <= 0:
                return None
            try:
                result = await self.mailbox_client.wait_for_new_code(
                    access_url,
                    request.email,
                    issued_after,
                    timeout_seconds=remaining,
                    poll_interval_seconds=poll_interval_seconds,
                    baseline=current_baseline,
                    purpose=requested_purpose,
                )
            except TypeError as exc:
                if "purpose" not in str(exc):
                    raise
                result = await self.mailbox_client.wait_for_new_code(
                    access_url,
                    request.email,
                    issued_after,
                    timeout_seconds=remaining,
                    poll_interval_seconds=poll_interval_seconds,
                    baseline=current_baseline,
                )
            code = str(
                getattr(result, "verification_code", result or "") or ""
            ).strip()
            if not code or code not in excluded:
                return code or None

            # The mailbox client does not know which codes were already
            # rejected by the auth service. Treat the rejected code as the new
            # mailbox baseline and keep waiting within the original deadline.
            from ..mailbox_client import MailboxSnapshot

            current_baseline = MailboxSnapshot(
                fingerprint="",
                verification_code=code,
                received_at_utc=getattr(result, "received_at_utc", None),
                received_offset=getattr(result, "received_offset", None),
            )
            remaining = deadline - monotonic_now()
            if remaining <= 0:
                return None
            delay = min(max(0.0, float(poll_interval_seconds)), remaining)
            if delay > 0:
                await self._sleep(delay)
            else:
                await asyncio.sleep(0)
