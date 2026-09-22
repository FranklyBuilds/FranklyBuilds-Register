"""HTTP/protocol ChatGPT registration state machine.

This module is intentionally transport-first.  It follows the endpoint order
captured in the baseline tool while keeping Sentinel, mailbox OTP and account
persistence behind small injectable interfaces.  That makes failures explicit
and gives worker/API layers a stable integration point.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from time import time as unix_time
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from ..totp import TotpSecretError, generate_totp, normalize_totp_secret
from .models import (
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationResult,
    ProtocolResponse,
    RegistrationMode,
    SentinelData,
)
from .otp import OtpProvider
from .sentinel import AutoSentinelProvider, SentinelProvider, StaticSentinelProvider
from .transport import CurlCffiTransport, ProtocolTransport


UTC = timezone.utc
OTP_RE = re.compile(r"^\d{6}$")

# Kept public so API/worker diagnostics and contract tests can refer to the
# exact protocol surface without duplicating path literals.
PROTOCOL_ENDPOINTS = {
    "csrf": "/api/auth/csrf",
    "signin": "/api/auth/signin/openai",
    "authorize_continue": "/api/accounts/authorize/continue",
    "user_register": "/api/accounts/user/register",
    "otp_send": "/api/accounts/email-otp/send",
    "otp_resend": "/api/accounts/email-otp/resend",
    "otp_validate": "/api/accounts/email-otp/validate",
    "create_account": "/api/accounts/create_account",
    "auth_session": "/api/auth/session",
    "totp_enroll": "/backend-api/accounts/mfa/enroll",
    "totp_activate": "/backend-api/accounts/mfa/user/activate_enrollment",
    "models": "/backend-api/models",
}


@dataclass(frozen=True, slots=True)
class _ProtocolTotpResult:
    secret: str
    access_token: str
    expires_at: datetime | None
    activated_at: datetime
    auth_session: dict[str, Any] = field(default_factory=dict)


class ProtocolRegistrationError(RuntimeError):
    """A stage-labelled protocol failure."""

    def __init__(self, code: str, message: str = "", *, stage: str = "transport", response: ProtocolResponse | None = None) -> None:
        self.code = str(code)
        self.stage = stage
        self.response = response
        super().__init__(message or code)


ProgressCallback = Callable[[str, Mapping[str, Any]], Awaitable[Any] | Any]
PersistenceCallback = Callable[[ProtocolRegistrationRequest, ProtocolRegistrationResult], Awaitable[Any] | Any]


class ProtocolRegistrationService:
    """Run one protocol registration attempt."""

    def __init__(
        self,
        *,
        config: ProtocolRegistrationConfig | None = None,
        transport_factory: Callable[..., ProtocolTransport] | None = None,
        fresh_transport_factory: Callable[..., ProtocolTransport] | None = None,
        sentinel_provider: SentinelProvider | None = None,
        otp_provider: OtpProvider | None = None,
        progress: ProgressCallback | None = None,
        persist: PersistenceCallback | None = None,
        password_factory: Callable[[], str] | None = None,
        name_factory: Callable[[], tuple[str, str]] | None = None,
        birthdate_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or ProtocolRegistrationConfig()
        self.transport_factory = transport_factory
        self.fresh_transport_factory = fresh_transport_factory
        self.sentinel_provider = sentinel_provider or AutoSentinelProvider()
        self.otp_provider = otp_provider
        self.progress = progress
        self.persist = persist
        self.password_factory = password_factory or _generate_password
        self.name_factory = name_factory or (lambda: ("Alex", "Morgan"))
        self.birthdate_factory = birthdate_factory or (lambda: "1990-01-01")

    async def register(
        self,
        request: ProtocolRegistrationRequest,
        *,
        transport: ProtocolTransport | None = None,
        otp_provider: OtpProvider | None = None,
        sentinel_provider: SentinelProvider | None = None,
    ) -> ProtocolRegistrationResult:
        """Execute the baseline sequence and return a structured result."""

        mode: RegistrationMode = request.registration_mode or self.config.registration_mode
        password = request.password or ("" if mode == "passwordless" else self.password_factory())
        first, last = (request.first_name, request.last_name)
        if not first or not last:
            first, last = self.name_factory()
        birthdate = request.birthdate or self.birthdate_factory()
        did_hint = request.device_id or ""
        own_transport = transport is None
        active_transport = transport or self._make_transport(request)
        result_transport = active_transport
        auxiliary_transport: ProtocolTransport | None = None
        active_otp_provider = otp_provider or self.otp_provider
        result: ProtocolRegistrationResult | None = None
        responses: dict[str, dict[str, Any]] = {}
        totp_secret = ""
        totp_activated_at: datetime | None = None
        totp_error = ""
        stage = "sentinel"
        existing_account = False
        try:
            provider = sentinel_provider
            if provider is None and request.sentinel_data:
                provider = StaticSentinelProvider(request.sentinel_data)
            provider = provider or self.sentinel_provider

            # Let ChatGPT establish the device cookie before Sentinel binds its
            # flow proofs.  Caller-supplied static proofs keep their own DID.
            await self._emit("sentinel", {"email": request.email})
            if request.device_id:
                setter = getattr(active_transport, "set_cookie", None)
                if callable(setter):
                    setter("oai-did", request.device_id, domain=".openai.com")
            prime = await self._request(
                active_transport,
                "GET",
                self._url(self.config.chat_base_url, "/"),
                label="prime",
                headers={
                    **self._base_headers(request.device_id or ""),
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            responses["prime"] = _response_summary(prime)
            prime_error = _transient_response_error(prime)
            if prime_error:
                raise ProtocolRegistrationError(
                    prime_error,
                    "ChatGPT bootstrap was rate-limited or challenged",
                    stage="auth_flow",
                    response=prime,
                )
            server_did = (
                _cookie_value(prime.header("set-cookie"), "oai-did")
                or _cookie_value(active_transport.cookie_header(), "oai-did")
            )
            provider_did = request.device_id or (
                "" if isinstance(provider, StaticSentinelProvider) else server_did
            )
            provider_request = (
                replace(request, device_id=provider_did)
                if provider_did and provider_did != request.device_id
                else request
            )
            try:
                sentinel_value = provider.get(provider_request, self.config, active_transport)
                if inspect.isawaitable(sentinel_value):
                    sentinel_value = await sentinel_value
            except ProtocolRegistrationError:
                raise
            except Exception as exc:
                raise ProtocolRegistrationError(
                    str(getattr(exc, "code", "sentinel_provider_failed") or "sentinel_provider_failed"),
                    str(getattr(exc, "message", "") or exc),
                    stage="sentinel",
                ) from exc
            sentinel = sentinel_value if isinstance(sentinel_value, SentinelData) else SentinelData.from_value(sentinel_value)
            did = request.device_id or sentinel.oai_did or server_did or did_hint or str(uuid.uuid4())
            session_logging_id = request.session_logging_id or uuid.uuid4().hex
            await self._prime_transport(active_transport, sentinel, did, mode)
            base_headers = self._base_headers(did)

            # Auth state: CSRF -> signin/authorize attempts.
            stage = "auth_flow"
            await self._emit(stage, {"email": request.email, "mode": mode})
            csrf = await self._request(active_transport, "GET", self._url(self.config.chat_base_url, "/api/auth/csrf"), label="csrf", headers={**base_headers, "Accept": "application/json", "Referer": self.config.chat_base_url + "/"})
            responses["csrf"] = _response_summary(csrf)
            csrf_token = str(_body_value(csrf, "csrfToken") or "").strip()
            if csrf.status_code >= 400 or not csrf_token:
                raise ProtocolRegistrationError("csrf_failed", "csrf token missing", stage=stage, response=csrf)
            otp_baseline = await self._capture_otp_baseline(request, active_otp_provider)
            auth_flow_started = datetime.now(UTC)
            signup_state = await self._prepare_auth_state(active_transport, request.email, did, session_logging_id, csrf_token, base_headers, sentinel, mode)
            responses.update(signup_state.pop("responses", {}))
            current_url = str(signup_state.get("url") or "")
            if signup_state.get("existing_login_redirect"):
                raise ProtocolRegistrationError("email_already_registered", "account is already registered", stage=stage)
            if not signup_state.get("ok"):
                raise ProtocolRegistrationError(
                    str(signup_state.get("error") or "auth_state_failed"),
                    "auth state did not reach signup",
                    stage=stage,
                    response=signup_state.get("response")
                    if isinstance(signup_state.get("response"), ProtocolResponse)
                    else None,
                )

            registration_data: dict[str, Any] = {"mode": "passwordless_signup"} if mode == "passwordless" else {}
            if mode == "password":
                stage = "user_register"
                await self._emit(stage, {"email": request.email})
                register_response = await self._request(
                    active_transport,
                    "POST",
                    self._url(self.config.auth_base_url, "/api/accounts/user/register"),
                    label="user_register",
                    json={"password": password, "username": request.email},
                    headers=self._auth_headers(
                        base_headers,
                        did,
                        self.config.auth_base_url + "/create-account/password",
                        sentinel,
                        content_type="application/json",
                        sentinel_token=sentinel.sentinel_token,
                        sentinel_so_token=sentinel.sentinel_username_so_token,
                    ),
                )
                registration_data = _as_dict(register_response.body)
                responses["user_register"] = _response_summary(register_response)
                if register_response.status_code != 200:
                    error_code = _error_code(registration_data) or f"user_register_http_{register_response.status_code}"
                    raise ProtocolRegistrationError(
                        error_code,
                        _error_message(registration_data),
                        stage=stage,
                        response=register_response,
                    )

            stage = "email_otp_send"
            await self._emit(stage, {"email": request.email})
            otp_send_started = datetime.now(UTC)
            otp_send = await self._send_otp(active_transport, current_url, base_headers, did, sentinel, mode, registration_data)
            responses["email_otp_send"] = _response_summary(otp_send)
            assumed_pre_sent = bool(_as_dict(otp_send.body).get("assumed_pre_sent"))
            if otp_send.status_code not in (200, 202, 204):
                raise ProtocolRegistrationError(f"email_otp_send_http_{otp_send.status_code}", _error_message(_as_dict(otp_send.body)), stage=stage, response=otp_send)

            stage = "email_otp_wait"
            await self._emit(stage, {"email": request.email})
            otp_issued_after = (
                auth_flow_started - timedelta(seconds=5)
                if assumed_pre_sent
                else otp_send_started
            )
            code = await self._obtain_otp(
                request,
                active_otp_provider,
                issued_after=otp_issued_after,
                baseline=otp_baseline,
            )
            if not code:
                raise ProtocolRegistrationError("email_otp_timeout", "verification code was not received", stage=stage)
            if not OTP_RE.fullmatch(code):
                raise ProtocolRegistrationError("email_otp_invalid_format", "verification code must contain six digits", stage=stage)

            stage = "email_otp_validate"
            await self._emit(stage, {"email": request.email})
            otp_data: dict[str, Any] = {}
            validate_response: ProtocolResponse | None = None
            excluded = {code}
            for attempt in range(self.config.otp_validation_retries + 1):
                validate_response = await self._validate_otp(active_transport, base_headers, did, sentinel, code, mode)
                otp_data = _as_dict(validate_response.body)
                if validate_response.status_code == 200:
                    break
                if _is_wrong_otp(otp_data) and attempt < self.config.otp_validation_retries:
                    replacement = await self._obtain_otp(
                        request,
                        active_otp_provider,
                        excluded_codes=excluded,
                        issued_after=auth_flow_started - timedelta(seconds=5),
                        baseline=otp_baseline,
                    )
                    if replacement and OTP_RE.fullmatch(replacement):
                        code, excluded = replacement, excluded | {replacement}
                        continue
                raise ProtocolRegistrationError(_error_code(otp_data) or f"email_otp_validate_http_{validate_response.status_code}", _error_message(otp_data), stage=stage, response=validate_response)
            responses["email_otp_validate"] = _response_summary(validate_response)
            await self._follow_continue(active_transport, _continue_url(otp_data, self.config.auth_base_url), base_headers, self.config.auth_base_url + "/verify-email", "email_otp_continue")

            stage = "create_account"
            await self._emit(stage, {"email": request.email})
            create_response = await self._request(
                active_transport,
                "POST",
                self._url(self.config.auth_base_url, "/api/accounts/create_account"),
                label="create_account",
                json={"name": f"{first} {last}".strip(), "birthdate": birthdate},
                headers=self._auth_headers(base_headers, did, self.config.auth_base_url + "/about-you", sentinel, sentinel_token=sentinel.sentinel_oauth_token or sentinel.sentinel_token),
            )
            create_data = _as_dict(create_response.body)
            responses["create_account"] = _response_summary(create_response)
            existing_account = _error_code(create_data) == "user_already_exists"
            if create_response.status_code != 200 and not existing_account:
                raise ProtocolRegistrationError(_error_code(create_data) or f"create_account_http_{create_response.status_code}", _error_message(create_data), stage=stage, response=create_response)
            await self._follow_continue(active_transport, _continue_url(create_data, self.config.auth_base_url), base_headers, self.config.auth_base_url + "/about-you", "create_account_continue")

            stage = "auth_session"
            await self._emit(stage, {"email": request.email})
            auth_session = await self._fetch_auth_session(active_transport, base_headers)
            responses["auth_session"] = _response_summary(auth_session)
            auth_body = _as_dict(auth_session.body)
            access_token = _extract_access_token(auth_body)
            if not access_token and existing_account:
                stage = "existing_login"
                await self._emit(stage, {"email": request.email})
                existing_login_baseline = await self._capture_otp_baseline(
                    request, active_otp_provider
                )
                login_transport = active_transport
                if own_transport or self.fresh_transport_factory is not None:
                    auxiliary_transport = self._make_transport(
                        request,
                        factory=self.fresh_transport_factory,
                    )
                    await self._prime_transport(
                        auxiliary_transport, sentinel, did, "passwordless"
                    )
                    login_transport = auxiliary_transport
                result_transport = login_transport
                auth_session = await self._login_existing_account(
                    login_transport,
                    request,
                    did,
                    session_logging_id,
                    csrf_token,
                    base_headers,
                    sentinel,
                    active_otp_provider,
                    existing_login_baseline,
                )
                responses["existing_login_auth_session"] = _response_summary(auth_session)
                auth_body = _as_dict(auth_session.body)
                access_token = _extract_access_token(auth_body)
            if not access_token:
                raise ProtocolRegistrationError("missing_auth_session_access_token", "auth session did not contain an access token", stage=stage, response=auth_session)
            expires_at = _extract_expiry(auth_body)
            if self.config.enable_registration_totp:
                if not existing_account and self.config.totp_reauth_delay_seconds:
                    await asyncio.sleep(self.config.totp_reauth_delay_seconds)
                stage = "two_factor"
                await self._emit(stage, {"email": request.email})
                if existing_account:
                    # A protocol run may encounter an already-created mailbox
                    # while recovering a session.  Do not silently mutate an
                    # existing account's security factors in that branch.
                    totp_error = "totp_existing_account_skipped"
                else:
                    try:
                        totp_baseline = await self._capture_otp_baseline(
                            request, active_otp_provider, stage="two_factor"
                        )
                        totp_result = await self._enroll_totp(
                            active_transport=result_transport,
                            request=request,
                            did=did,
                            base_headers=base_headers,
                            sentinel=sentinel,
                            otp_provider=active_otp_provider,
                            otp_baseline=totp_baseline,
                            # The registration code is already consumed by
                            # the signup flow; MFA re-auth must wait for a
                            # distinct mailbox message.
                            excluded_codes={code},
                            responses=responses,
                        )
                        totp_secret = totp_result.secret
                        totp_activated_at = totp_result.activated_at
                        if totp_result.access_token:
                            access_token = totp_result.access_token
                        if totp_result.expires_at is not None:
                            expires_at = totp_result.expires_at
                        if totp_result.auth_session:
                            auth_body = totp_result.auth_session
                        await self._emit(
                            "two_factor",
                            {
                                "email": request.email,
                                "status": "enabled",
                                "activatedAt": totp_activated_at.isoformat()
                                if totp_activated_at is not None
                                else "",
                            },
                        )
                    except ProtocolRegistrationError as exc:
                        # Keep the newly-created account usable and let the
                        # backfill workflow retry 2FA later.
                        totp_error = exc.code
                        responses.setdefault(
                            "totp_error",
                            {
                                "stage": exc.stage,
                                "code": exc.code,
                                "status_code": int(exc.response.status_code)
                                if exc.response is not None
                                else 0,
                            },
                        )
                        await self._emit(
                            "two_factor",
                            {
                                "email": request.email,
                                "status": "failed",
                                "errorCode": exc.code,
                            },
                        )
                    except Exception as exc:
                        # Adapter/transport exceptions must not turn a valid
                        # registration into a failed account transaction.
                        totp_error = f"unexpected_{type(exc).__name__}".lower()
                        responses.setdefault(
                            "totp_error",
                            {
                                "stage": "two_factor",
                                "code": totp_error,
                                "status_code": 0,
                            },
                        )
                        await self._emit(
                            "two_factor",
                            {
                                "email": request.email,
                                "status": "failed",
                                "errorCode": totp_error,
                            },
                        )
            result = ProtocolRegistrationResult(
                success=True,
                email=request.email,
                password="" if existing_account else password,
                totp_secret=totp_secret,
                totp_activated_at=totp_activated_at,
                totp_error=totp_error,
                access_token=access_token,
                refresh_token=str(auth_body.get("refreshToken") or auth_body.get("refresh_token") or ""),
                id_token=str(auth_body.get("idToken") or auth_body.get("id_token") or ""),
                device_id=did,
                registration_mode=mode,
                registration_country=request.registration_country,
                stage="finished",
                warning=totp_error,
                auth_session=auth_body,
                responses=responses,
                cookie_header=result_transport.cookie_header(),
                source_email_id=request.source_email_id,
                metadata={
                    "expires_at": expires_at.isoformat() if expires_at else "",
                    "assumed_pre_sent": assumed_pre_sent,
                    "name": f"{first} {last}".strip(),
                    "birthdate": birthdate,
                    "existing_account": existing_account,
                    "totp_error": totp_error,
                },
            )
            stage = "persistence"
            await self._persist(request, result)
            await self._emit("success", result.to_dict(include_secrets=False))
            return result
        except ProtocolRegistrationError as exc:
            error_code = _transient_response_error(exc.response) or exc.code
            result = ProtocolRegistrationResult(
                success=False,
                email=request.email,
                password=password,
                device_id=locals().get("did", did_hint),
                registration_mode=mode,
                registration_country=request.registration_country,
                stage=exc.stage,
                error=error_code,
                responses=responses,
                cookie_header=result_transport.cookie_header(),
                source_email_id=request.source_email_id,
            )
            await self._emit("failed", result.to_dict(include_secrets=False))
            return result
        except Exception as exc:
            error_code = (
                "transport_error"
                if isinstance(exc, (OSError, TimeoutError, asyncio.TimeoutError, ConnectionError))
                else f"unexpected_{type(exc).__name__}".lower()
            )
            result = ProtocolRegistrationResult(
                success=False,
                email=request.email,
                password=password,
                device_id=locals().get("did", did_hint),
                registration_mode=mode,
                registration_country=request.registration_country,
                stage=stage,
                error=error_code,
                responses=responses,
                cookie_header=result_transport.cookie_header(),
                source_email_id=request.source_email_id,
            )
            await self._emit("failed", result.to_dict(include_secrets=False))
            return result
        finally:
            if auxiliary_transport is not None:
                with _suppress_exceptions():
                    closed = auxiliary_transport.close()
                    if inspect.isawaitable(closed):
                        await closed
            if own_transport:
                with _suppress_exceptions():
                    closed = active_transport.close()
                    if inspect.isawaitable(closed):
                        await closed

    def _make_transport(
        self,
        request: ProtocolRegistrationRequest,
        *,
        factory: Callable[..., ProtocolTransport] | None = None,
    ) -> ProtocolTransport:
        active_factory = factory or self.transport_factory
        if active_factory is not None:
            # Keep the integration seam friendly to dependency injection: a
            # factory may accept the modern keyword pair, a request only, or no
            # arguments (the latter is convenient for a fixed fake transport).
            try:
                return active_factory(request=request, config=self.config)
            except TypeError as first_error:
                try:
                    return active_factory(request)
                except TypeError:
                    try:
                        return active_factory()
                    except TypeError:
                        raise first_error
        return CurlCffiTransport(proxy=request.proxy, timeout=self.config.timeout_seconds, user_agent=self.config.user_agent)

    async def _prime_transport(
        self,
        transport: ProtocolTransport,
        sentinel: SentinelData,
        did: str,
        mode: RegistrationMode,
    ) -> None:
        setter = getattr(transport, "set_cookie", None)
        if callable(setter):
            setter("oai-did", did, domain=".openai.com")
            if mode == "passwordless":
                return
            for item in sentinel.cookie_str.split(";"):
                if "=" in item:
                    name, value = item.split("=", 1)
                    if name.strip().casefold() != "oai-did" and name.strip() and value.strip():
                        setter(name.strip(), value.strip(), domain="auth.openai.com")

    def _base_headers(self, did: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": self.config.user_agent,
        }
        if did:
            headers["oai-device-id"] = did
        return headers

    def _auth_headers(self, base: Mapping[str, str], did: str, referer: str, sentinel: SentinelData, *, content_type: str | None = None, sentinel_token: str | None = None, sentinel_so_token: str | None = None) -> dict[str, str]:
        headers = dict(base)
        headers.update({"Referer": referer, "Origin": self.config.auth_base_url, "oai-device-id": did})
        token = sentinel_token if sentinel_token is not None else sentinel.sentinel_token
        if token:
            headers["openai-sentinel-token"] = token
        so_token = sentinel_so_token if sentinel_so_token is not None else sentinel.sentinel_so_token
        if so_token:
            headers["openai-sentinel-so-token"] = so_token
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def _request(self, transport: ProtocolTransport, method: str, url: str, *, label: str, **kwargs: Any) -> ProtocolResponse:
        response = transport.request(method, url, timeout=self.config.timeout_seconds, **kwargs)
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, ProtocolResponse):
            # A custom adapter may return a requests-like object.  Reuse the
            # fake transport normaliser rather than leaking that dependency
            # into the state machine.
            from .transport import _coerce_response

            response = _coerce_response(response, url)
        return response

    async def _prepare_auth_state(self, transport: ProtocolTransport, username: str, did: str, session_logging_id: str, csrf_token: str, base_headers: Mapping[str, str], sentinel: SentinelData, mode: RegistrationMode) -> dict[str, Any]:
        attempts = (
            ({"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": ""}, {"name": "login_or_signup_prompt_signup", "screen_hint": "login_or_signup", "prompt": "signup"}, {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""})
            if mode == "passwordless" else
            ({"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""}, {"name": "signup_prompt_signup", "screen_hint": "signup", "prompt": "signup"}, {"name": "signup_legacy_prompt_login", "screen_hint": "signup", "prompt": "login"})
        )
        responses: dict[str, dict[str, Any]] = {}
        last: dict[str, Any] = {"ok": False, "error": "signup_auth_not_started", "responses": responses}
        for attempt in attempts[: self.config.signin_attempts]:
            name, screen_hint, prompt = attempt["name"], attempt["screen_hint"], attempt["prompt"]
            params = {"ext-oai-did": did, "auth_session_logging_id": session_logging_id, "login_hint": username, "screen_hint": screen_hint}
            if prompt:
                params["prompt"] = prompt
            signin_url = self._url(self.config.chat_base_url, "/api/auth/signin/openai") + "?" + urlencode(params)
            signin = await self._request(transport, "POST", signin_url, label=f"signin_{name}", data=urlencode({"csrfToken": csrf_token, "callbackUrl": self.config.chat_base_url + "/", "json": "true"}), headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded", "Origin": self.config.chat_base_url, "Referer": self.config.chat_base_url + "/"})
            responses[f"signin_{name}"] = _response_summary(signin)
            transient_error = _transient_response_error(signin)
            if transient_error:
                return {
                    "ok": False,
                    "error": transient_error,
                    "response": signin,
                    "responses": responses,
                }
            body = _as_dict(signin.body)
            auth_url = str(body.get("url") or signin.header("location") or signin.url or "")
            if not auth_url:
                transient_error = _transient_response_error(signin)
                last = {
                    "ok": False,
                    "error": transient_error or "missing_auth_session_url",
                    "response": signin,
                    "responses": responses,
                }
                if transient_error:
                    return last
                continue
            auth_url = urljoin(self.config.auth_base_url.rstrip("/") + "/", auth_url)
            auth_url = self._with_query(auth_url, "device_id", did)
            authorize = await self._request(transport, "GET", auth_url, label=f"authorize_{name}", allow_redirects=False, headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": self.config.chat_base_url + "/"})
            responses[f"authorize_{name}"] = _response_summary(authorize)
            transient_error = _transient_response_error(authorize)
            if transient_error:
                return {
                    "ok": False,
                    "error": transient_error,
                    "response": authorize,
                    "responses": responses,
                }
            current_url = self._redirect_url(authorize, self.config.auth_base_url)
            lower = current_url.casefold()
            if _is_existing_login_url(current_url):
                return {"ok": False, "existing_login_redirect": True, "url": current_url, "responses": responses}
            if _is_chatgpt_auth_login_landing(current_url):
                last = {"ok": False, "error": "redirected_to_chatgpt_login", "url": current_url, "responses": responses}
                continue
            if "create-account/password" in lower or "email-verification" in lower:
                return {"ok": True, "url": current_url, "responses": responses}
            continued = await self._request(transport, "POST", self._url(self.config.auth_base_url, "/api/accounts/authorize/continue"), label=f"authorize_continue_{name}", json={"username": {"value": username, "kind": "email"}}, headers=self._auth_headers(base_headers, did, current_url or self.config.auth_base_url + "/create-account", sentinel, content_type="application/json", sentinel_token=sentinel.sentinel_authorize_token or sentinel.sentinel_token, sentinel_so_token=sentinel.sentinel_authorize_so_token or sentinel.sentinel_so_token))
            responses[f"authorize_continue_{name}"] = _response_summary(continued)
            if continued.status_code != 200:
                transient_error = _transient_response_error(continued)
                last = {
                    "ok": False,
                    "status": continued.status_code,
                    "error": transient_error or _error_code(_as_dict(continued.body)) or "authorize_continue_failed",
                    "response": continued,
                    "responses": responses,
                }
                if transient_error:
                    return last
                continue
            next_url = self._next_url(continued, self.config.auth_base_url)
            if next_url and not urlsplit(next_url).path.rstrip("/").endswith("/api/accounts/authorize/continue"):
                follow = await self._request(transport, "GET", next_url, label=f"authorize_continue_follow_{name}", headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": current_url or self.config.auth_base_url + "/create-account"})
                responses[f"authorize_continue_follow_{name}"] = _response_summary(follow)
                current_url = str(follow.url or next_url)
            return {"ok": True, "url": current_url, "responses": responses}
        return last

    async def _send_otp(self, transport: ProtocolTransport, current_url: str, base_headers: Mapping[str, str], did: str, sentinel: SentinelData, mode: RegistrationMode, registration_data: Mapping[str, Any]) -> ProtocolResponse:
        if mode == "passwordless" and "email-verification" in current_url.casefold():
            await self._request(transport, "GET", current_url, label="email_verification_page", headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": current_url})
        endpoints = ["/api/accounts/email-otp/resend"] if mode == "passwordless" else []
        if mode == "passwordless" and self.config.otp_fallback_send:
            endpoints.extend(
                ["/api/accounts/passwordless/send-otp", "/api/accounts/email-otp/send"]
            )
        continue_url = _continue_url(registration_data, self.config.auth_base_url)
        if continue_url:
            # ``user/register`` returns a continuation URL.  The baseline
            # follows it with GET; posting to that URL skips the auth state
            # transition and commonly yields ``invalid_state``.
            response = await self._request(transport, "GET", continue_url, label="email_otp_send_continue", headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": current_url or self.config.auth_base_url + "/create-account/password"})
            if 300 <= response.status_code < 400 and response.header("location"):
                response = await self._request(
                    transport,
                    "GET",
                    urljoin(continue_url, response.header("location")),
                    label="email_otp_send_continue_follow",
                    headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": continue_url},
                )
            if response.status_code in (200, 202, 204):
                return response
            if mode == "password":
                return response
        if not endpoints:
            endpoints = ["/api/accounts/email-otp/send", "/api/accounts/email-otp/resend"]
        last: ProtocolResponse | None = None
        for endpoint in endpoints:
            url = self._url(self.config.auth_base_url, endpoint)
            # Password signup returns a continuation URL that must be followed
            # as a browser navigation; only the explicit email-otp endpoints
            # receive the JSON POST used by the passwordless flow.
            if mode == "password" and endpoint.startswith(("http://", "https://")) and "/api/accounts/" not in endpoint:
                response = await self._request(
                    transport,
                    "GET",
                    url,
                    label="email_otp_send_continue",
                    headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": current_url or self.config.auth_base_url + "/create-account/password"},
                )
            else:
                response = await self._request(transport, "POST", url, label="email_otp_send", json={}, headers=self._auth_headers(base_headers, did, current_url or self.config.auth_base_url + "/email-verification", sentinel, content_type="application/json"))
            last = response
            if response.status_code in (200, 202, 204):
                return response
            if response.status_code == 409 and _otp_pending_response(response):
                return ProtocolResponse(
                    204,
                    {"assumed_pre_sent": True, "pending_status": 409, "pending_body": _as_dict(response.body)},
                    response.headers,
                    response.url,
                    response.text,
                )
            if mode == "passwordless" and response.status_code in (400, 404, 405) and not self.config.otp_fallback_send:
                return ProtocolResponse(204, {"assumed_pre_sent": True, "resend_status": response.status_code, "resend_body": _as_dict(response.body)}, response.headers, response.url, response.text)
            if mode == "passwordless" and response.status_code in (400, 404, 405):
                continue
            if response.status_code not in (404, 405):
                return response
        return last or ProtocolResponse(0, {}, url=self._url(self.config.auth_base_url, "/api/accounts/email-otp/send"))

    async def _capture_otp_baseline(
        self,
        request: ProtocolRegistrationRequest,
        provider: OtpProvider | None,
        *,
        stage: str = "email_otp_wait",
    ) -> Any | None:
        if provider is None:
            return None
        reader = getattr(provider, "capture_baseline", None)
        if not callable(reader):
            return None
        try:
            value = reader(request)
            return await value if inspect.isawaitable(value) else value
        except ProtocolRegistrationError:
            raise
        except Exception as exc:
            code = str(
                getattr(exc, "code", "otp_baseline_failed")
                or "otp_baseline_failed"
            )
            raise ProtocolRegistrationError(
                code,
                str(getattr(exc, "message", "") or exc),
                stage=stage,
            ) from exc

    async def _obtain_otp(
        self,
        request: ProtocolRegistrationRequest,
        provider: OtpProvider | None,
        *,
        issued_after: datetime,
        excluded_codes: set[str] | None = None,
        baseline: Any | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        stage: str = "email_otp_wait",
        allow_request_code: bool = True,
    ) -> str | None:
        if allow_request_code and request.otp_code and not excluded_codes:
            return str(request.otp_code).strip()
        if provider is None:
            return None
        try:
            wait_for_code = provider.wait_for_code
            kwargs: dict[str, Any] = {
                "issued_after": issued_after,
                "timeout_seconds": (
                    self.config.otp_timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
                "poll_interval_seconds": (
                    self.config.otp_poll_interval_seconds
                    if poll_interval_seconds is None
                    else poll_interval_seconds
                ),
                "excluded_codes": excluded_codes or set(),
            }
            with _suppress_exceptions():
                parameters = inspect.signature(wait_for_code).parameters.values()
                if any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    or parameter.name == "baseline"
                    for parameter in parameters
                ):
                    kwargs["baseline"] = baseline
            value = wait_for_code(request, **kwargs)
            if inspect.isawaitable(value):
                value = await value
        except ProtocolRegistrationError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "otp_provider_failed") or "otp_provider_failed")
            raise ProtocolRegistrationError(
                code,
                str(getattr(exc, "message", "") or exc),
                stage=stage,
            ) from exc
        return str(value).strip() if value else None

    async def _validate_otp(self, transport: ProtocolTransport, base_headers: Mapping[str, str], did: str, sentinel: SentinelData, code: str, mode: RegistrationMode) -> ProtocolResponse:
        endpoints = ("/api/accounts/email-otp/validate", "/api/accounts/email-verification/validate", "/api/accounts/email-verification/verify", "/api/accounts/verify-email")
        last: ProtocolResponse | None = None
        for endpoint in endpoints:
            for payload in ({"code": code}, {"otp": code}) if endpoint != endpoints[0] else ({"code": code},):
                validation_sentinel = sentinel if mode == "password" else SentinelData(oai_did=did)
                response = await self._request(transport, "POST", self._url(self.config.auth_base_url, endpoint), label="email_otp_validate", json=payload, headers=self._auth_headers(base_headers, did, self.config.auth_base_url + "/email-verification", validation_sentinel, content_type="application/json"))
                last = response
                if response.status_code == 200 or response.status_code not in (404, 405):
                    return response
        return last or ProtocolResponse(0, {})

    async def _login_existing_account(
        self,
        transport: ProtocolTransport,
        request: ProtocolRegistrationRequest,
        did: str,
        session_logging_id: str,
        csrf_token: str,
        base_headers: Mapping[str, str],
        sentinel: SentinelData,
        otp_provider: OtpProvider | None,
        otp_baseline: Any | None,
    ) -> ProtocolResponse:
        """Recover a session when create_account reports an existing user.

        The caller supplies a clean production transport so signup-state auth
        cookies cannot force this login transaction back to ``/about-you``.
        """

        login_started = datetime.now(UTC)
        params = {
            "prompt": "login",
            "ext-oai-did": did,
            "auth_session_logging_id": session_logging_id,
            "screen_hint": "login",
            "login_hint": request.email,
        }
        signin = await self._request(
            transport,
            "POST",
            self._url(self.config.chat_base_url, "/api/auth/signin/openai") + "?" + urlencode(params),
            label="existing_login_signin",
            data=urlencode({"csrfToken": csrf_token, "callbackUrl": self.config.chat_base_url + "/", "json": "true"}),
            headers={
                **base_headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.config.chat_base_url,
                "Referer": self.config.chat_base_url + "/",
            },
        )
        body = _as_dict(signin.body)
        auth_url = str(body.get("url") or signin.header("location") or signin.url or "")
        if not auth_url:
            raise ProtocolRegistrationError("existing_login_missing_auth_url", stage="existing_login")
        auth_url = urljoin(self.config.auth_base_url.rstrip("/") + "/", auth_url)
        auth_url = self._with_query(auth_url, "device_id", did)
        authorize = await self._request(
            transport,
            "GET",
            auth_url,
            label="existing_login_authorize",
            allow_redirects=False,
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": self.config.chat_base_url + "/"},
        )
        current_url = self._redirect_url(authorize, self.config.auth_base_url)
        continued = await self._request(
            transport,
            "POST",
            self._url(self.config.auth_base_url, PROTOCOL_ENDPOINTS["authorize_continue"]),
            label="existing_login_continue",
            json={"username": {"value": request.email, "kind": "email"}},
            headers=self._auth_headers(
                base_headers,
                did,
                current_url or self.config.auth_base_url + "/log-in",
                sentinel,
                content_type="application/json",
                sentinel_token=sentinel.sentinel_authorize_token or sentinel.sentinel_token,
                sentinel_so_token=sentinel.sentinel_authorize_so_token or sentinel.sentinel_so_token,
            ),
        )
        if continued.status_code not in (200, 400, 409):
            raise ProtocolRegistrationError(
                _error_code(_as_dict(continued.body)) or f"existing_login_continue_http_{continued.status_code}",
                stage="existing_login",
                response=continued,
            )
        next_url = self._next_url(continued, self.config.auth_base_url)
        if next_url and not _is_existing_login_url(next_url):
            followed = await self._follow_continue(
                transport,
                next_url,
                base_headers,
                current_url or self.config.auth_base_url + "/log-in",
                "existing_login_continue_follow",
            )
            current_url = str(followed.url if followed is not None else next_url)
        otp_send_started = datetime.now(UTC)
        otp_send = await self._send_existing_login_otp(
            transport, current_url, base_headers, did, sentinel
        )
        if otp_send.status_code not in (200, 202, 204):
            raise ProtocolRegistrationError(
                f"existing_login_otp_send_http_{otp_send.status_code}",
                stage="existing_login",
                response=otp_send,
            )
        assumed_pre_sent = bool(_as_dict(otp_send.body).get("assumed_pre_sent"))
        code = await self._obtain_otp(
            request,
            otp_provider,
            issued_after=(
                login_started - timedelta(seconds=5)
                if assumed_pre_sent
                else otp_send_started
            ),
            baseline=otp_baseline,
        )
        if not code or not OTP_RE.fullmatch(code):
            raise ProtocolRegistrationError("existing_login_otp_timeout", stage="existing_login")
        validated = await self._validate_otp(transport, base_headers, did, sentinel, code, "passwordless")
        if validated.status_code != 200:
            raise ProtocolRegistrationError(
                _error_code(_as_dict(validated.body)) or f"existing_login_otp_validate_http_{validated.status_code}",
                stage="existing_login",
                response=validated,
            )
        await self._follow_continue(
            transport,
            _continue_url(_as_dict(validated.body), self.config.auth_base_url),
            base_headers,
            self.config.auth_base_url + "/email-verification",
            "existing_login_otp_continue",
        )
        return await self._fetch_auth_session(transport, base_headers)

    async def _send_existing_login_otp(
        self,
        transport: ProtocolTransport,
        current_url: str,
        base_headers: Mapping[str, str],
        did: str,
        sentinel: SentinelData,
    ) -> ProtocolResponse:
        """Use the login-specific OTP endpoint order from the baseline."""

        last: ProtocolResponse | None = None
        for endpoint in (
            "/api/accounts/passwordless/send-otp",
            "/api/accounts/email-otp/send",
            "/api/accounts/email-otp/resend",
        ):
            response = await self._request(
                transport,
                "POST",
                self._url(self.config.auth_base_url, endpoint),
                label="existing_login_otp_send",
                json={},
                headers=self._auth_headers(
                    base_headers,
                    did,
                    current_url or self.config.auth_base_url + "/email-verification",
                    sentinel,
                    content_type="application/json",
                ),
            )
            last = response
            if response.status_code in (200, 202, 204):
                return response
            if response.status_code == 409 and _otp_pending_response(response):
                return ProtocolResponse(204, {"assumed_pre_sent": True, "pending_status": 409}, response.headers, response.url, response.text)
            if response.status_code not in (400, 404, 405):
                return response
        return last or ProtocolResponse(0, {})

    async def _follow_continue(self, transport: ProtocolTransport, url: str, base_headers: Mapping[str, str], referer: str, label: str) -> ProtocolResponse | None:
        if not url:
            return None
        response = await self._request(transport, "GET", url, label=label, headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": referer})
        if 300 <= response.status_code < 400 and response.header("location"):
            return await self._request(
                transport,
                "GET",
                urljoin(url, response.header("location")),
                label=f"{label}_follow",
                headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": url},
            )
        return response

    async def _fetch_auth_session(self, transport: ProtocolTransport, base_headers: Mapping[str, str]) -> ProtocolResponse:
        last = ProtocolResponse(0, {})
        for attempt in range(self.config.auth_session_attempts):
            last = await self._request(transport, "GET", self._url(self.config.chat_base_url, "/api/auth/session"), label="auth_session", headers={**base_headers, "Accept": "application/json", "Origin": self.config.chat_base_url, "Referer": self.config.chat_base_url + "/"})
            if last.status_code == 200 and _extract_access_token(_as_dict(last.body)):
                return last
            if attempt + 1 < self.config.auth_session_attempts and self.config.auth_session_delay_seconds:
                await asyncio.sleep(self.config.auth_session_delay_seconds)
        return last

    async def _enroll_totp(
        self,
        *,
        active_transport: ProtocolTransport,
        request: ProtocolRegistrationRequest,
        did: str,
        base_headers: Mapping[str, str],
        sentinel: SentinelData,
        otp_provider: OtpProvider | None,
        otp_baseline: Any | None,
        responses: dict[str, dict[str, Any]],
        excluded_codes: set[str] | None = None,
    ) -> _ProtocolTotpResult:
        """Re-authenticate the fresh account and activate a TOTP factor.

        The browser flow performs this sequence through page navigation.  The
        protocol worker keeps the same session and mirrors the small set of
        requests needed by the MFA endpoints, so no browser workspace is
        required.  Every failure is reported as a stage-labelled protocol
        error; the caller deliberately treats that error as a partial success.
        """

        # Start a clean password re-auth transaction.  Consuming the auth
        # redirect through the email-verification page advances the server
        # state and sends the email code.
        reauth_requested_at = datetime.now(UTC)
        csrf = await self._request(
            active_transport,
            "GET",
            self._url(self.config.chat_base_url, PROTOCOL_ENDPOINTS["csrf"]),
            label="totp_reauth_csrf",
            headers={
                **base_headers,
                "Accept": "application/json",
                "Referer": self.config.chat_base_url + "/",
            },
        )
        responses["totp_reauth_csrf"] = _response_summary(csrf)
        csrf_token = str(_body_value(csrf, "csrfToken") or "").strip()
        if csrf.status_code >= 400 or not csrf_token:
            raise ProtocolRegistrationError(
                "totp_reauth_csrf_failed",
                "2FA 重认证缺少 CSRF token",
                stage="two_factor",
                response=csrf,
            )

        params = {
            "connection": "password",
            "login_hint": request.email,
            "reauth": "password",
            "max_age": "0",
        }
        signin_url = (
            self._url(self.config.chat_base_url, "/api/auth/signin/openai")
            + "?"
            + urlencode(params)
        )
        signin = await self._request(
            active_transport,
            "POST",
            signin_url,
            label="totp_reauth_signin",
            data=urlencode(
                {
                    "csrfToken": csrf_token,
                    "callbackUrl": self.config.chat_base_url
                    + "/?action=enable&factor=totp",
                    "json": "true",
                }
            ),
            headers={
                **base_headers,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.config.chat_base_url,
                "Referer": self.config.chat_base_url + "/",
            },
        )
        responses["totp_reauth_signin"] = _response_summary(signin)
        if signin.status_code < 200 or signin.status_code >= 300:
            raise ProtocolRegistrationError(
                f"totp_reauth_signin_http_{signin.status_code}",
                _error_message(_as_dict(signin.body)) or "2FA 重认证启动失败",
                stage="two_factor",
                response=signin,
            )
        signin_body = _as_dict(signin.body)
        auth_url = str(
            signin_body.get("url")
            or signin.header("location")
            or ""
        ).strip()
        if not auth_url:
            raise ProtocolRegistrationError(
                "totp_reauth_missing_url",
                "2FA 重认证缺少授权地址",
                stage="two_factor",
                response=signin,
            )
        normalized_auth_url = urljoin(
            self.config.auth_base_url.rstrip("/") + "/", auth_url
        )
        if not self._is_trusted_openai_page(normalized_auth_url):
            raise ProtocolRegistrationError(
                "totp_reauth_url_untrusted",
                "2FA 重认证返回了不受信任的地址",
                stage="two_factor",
                response=signin,
            )
        auth_url = normalized_auth_url
        auth_url = self._with_query(auth_url, "device_id", did)
        authorize = await self._request(
            active_transport,
            "GET",
            auth_url,
            label="totp_reauth_authorize",
            allow_redirects=False,
            headers={
                **base_headers,
                "Accept": "text/html,application/xhtml+xml",
                "Referer": self.config.chat_base_url + "/",
            },
        )
        responses["totp_reauth_authorize"] = _response_summary(authorize)
        page_response = authorize
        current_url = str(authorize.url or auth_url)
        if not self._is_trusted_openai_page(current_url):
            raise ProtocolRegistrationError(
                "totp_reauth_page_untrusted",
                "2FA 重认证页面地址不受信任",
                stage="two_factor",
                response=authorize,
            )
        redirect_hops = 0
        while 300 <= page_response.status_code < 400:
            location = page_response.header("location")
            if not location:
                raise ProtocolRegistrationError(
                    "totp_reauth_redirect_missing_location",
                    "2FA 重认证跳转缺少目标地址",
                    stage="two_factor",
                    response=page_response,
                )
            if redirect_hops >= 5:
                raise ProtocolRegistrationError(
                    "totp_reauth_redirect_limit",
                    "2FA 重认证跳转次数过多",
                    stage="two_factor",
                    response=page_response,
                )
            next_url = urljoin(current_url, location)
            if not self._is_trusted_openai_page(next_url):
                raise ProtocolRegistrationError(
                    "totp_reauth_redirect_untrusted",
                    "2FA 重认证重定向地址不受信任",
                    stage="two_factor",
                    response=page_response,
                )
            redirect_hops += 1
            label = (
                "totp_reauth_page"
                if redirect_hops == 1
                else f"totp_reauth_page_{redirect_hops}"
            )
            followed = await self._request(
                active_transport,
                "GET",
                next_url,
                label=label,
                allow_redirects=False,
                headers={
                    **base_headers,
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": current_url,
                },
            )
            responses[label] = _response_summary(followed)
            current_url = str(followed.url or next_url)
            if not self._is_trusted_openai_page(current_url):
                raise ProtocolRegistrationError(
                    "totp_reauth_page_untrusted",
                    "2FA 重认证页面重定向地址不受信任",
                    stage="two_factor",
                    response=followed,
                )
            page_response = followed

        if page_response.status_code < 200 or page_response.status_code >= 300:
            raise ProtocolRegistrationError(
                f"totp_reauth_email_verification_http_{page_response.status_code}",
                "2FA 重认证邮箱验证页面加载失败",
                stage="two_factor",
                response=page_response,
            )
        if not self._is_totp_email_verification_url(current_url):
            raise ProtocolRegistrationError(
                "totp_reauth_email_verification_missing",
                "2FA 重认证未进入邮箱验证页面",
                stage="two_factor",
                response=page_response,
            )
        otp_send = ProtocolResponse(
            204,
            {"assumed_pre_sent": True},
            url=current_url,
        )
        responses["totp_otp_send"] = _response_summary(otp_send)
        if otp_send.status_code not in (200, 202, 204):
            raise ProtocolRegistrationError(
                f"totp_otp_send_http_{otp_send.status_code}",
                _error_message(_as_dict(otp_send.body)),
                stage="two_factor",
                response=otp_send,
            )
        code = await self._obtain_otp(
            request,
            otp_provider,
            issued_after=(
                reauth_requested_at - timedelta(seconds=5)
                if bool(_as_dict(otp_send.body).get("assumed_pre_sent"))
                else reauth_requested_at
            ),
            baseline=otp_baseline,
            timeout_seconds=self.config.totp_timeout_seconds,
            poll_interval_seconds=self.config.totp_poll_interval_seconds,
            stage="two_factor",
            allow_request_code=False,
            excluded_codes=excluded_codes,
        )
        if not code or not OTP_RE.fullmatch(code):
            raise ProtocolRegistrationError(
                "totp_reauth_email_otp_invalid",
                "2FA 重认证邮箱验证码无效或超时",
                stage="two_factor",
            )
        validate = await self._validate_otp(
            active_transport,
            base_headers,
            did,
            sentinel,
            code,
            "password",
        )
        responses["totp_otp_validate"] = _response_summary(validate)
        validate_body = _as_dict(validate.body)
        if validate.status_code != 200:
            raise ProtocolRegistrationError(
                _error_code(validate_body)
                or f"totp_reauth_email_otp_http_{validate.status_code}",
                _error_message(validate_body),
                stage="two_factor",
                response=validate,
            )
        await self._follow_continue(
            active_transport,
            _continue_url(validate_body, self.config.auth_base_url),
            base_headers,
            self.config.auth_base_url + "/email-verification",
            "totp_otp_continue",
        )

        refreshed_session = await self._fetch_auth_session(
            active_transport, base_headers
        )
        responses["totp_auth_session"] = _response_summary(refreshed_session)
        refreshed_body = _as_dict(refreshed_session.body)
        access_token = _extract_access_token(refreshed_body)
        if refreshed_session.status_code != 200 or not access_token:
            raise ProtocolRegistrationError(
                "totp_session_refresh_failed",
                "2FA 重认证后未取得新的 Session Token",
                stage="two_factor",
                response=refreshed_session,
            )
        expires_at = _extract_expiry(refreshed_body)
        api_headers = {
            **base_headers,
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Origin": self.config.chat_base_url,
            "Referer": self.config.chat_base_url + "/",
            "oai-language": "en-US",
            "oai-device-id": did,
        }
        enroll = await self._request(
            active_transport,
            "POST",
            self._url(self.config.chat_base_url, PROTOCOL_ENDPOINTS["totp_enroll"]),
            label="totp_enroll",
            json={"factor_type": "totp"},
            headers=api_headers,
        )
        responses["totp_enroll"] = _response_summary(enroll)
        enroll_body = _as_dict(enroll.body)
        enroll_data = (
            _as_dict(enroll_body.get("data"))
            if isinstance(enroll_body.get("data"), Mapping)
            else enroll_body
        )
        try:
            secret = normalize_totp_secret(str(enroll_data.get("secret") or ""))
        except TotpSecretError as exc:
            raise ProtocolRegistrationError(
                "totp_enroll_response_invalid",
                "2FA enroll 响应缺少有效 Secret",
                stage="two_factor",
                response=enroll,
            ) from exc
        session_id = str(enroll_data.get("session_id") or "").strip()
        if enroll.status_code < 200 or enroll.status_code >= 300 or not session_id:
            raise ProtocolRegistrationError(
                "totp_enroll_failed",
                _error_message(enroll_body) or "2FA TOTP enroll 失败",
                stage="two_factor",
                response=enroll,
            )

        # Avoid submitting a code during the final seconds of a TOTP window.
        remaining = 30 - (unix_time() % 30)
        if remaining < 4:
            await asyncio.sleep(remaining + 0.25)
        activate = await self._request(
            active_transport,
            "POST",
            self._url(self.config.chat_base_url, PROTOCOL_ENDPOINTS["totp_activate"]),
            label="totp_activate",
            json={
                "code": generate_totp(secret),
                "factor_type": "totp",
                "session_id": session_id,
            },
            headers=api_headers,
        )
        responses["totp_activate"] = _response_summary(activate)
        activate_body = _as_dict(activate.body)
        activate_data = (
            _as_dict(activate_body.get("data"))
            if isinstance(activate_body.get("data"), Mapping)
            else activate_body
        )
        if (
            activate.status_code < 200
            or activate.status_code >= 300
            or activate_data.get("success") is not True
        ):
            raise ProtocolRegistrationError(
                "totp_activate_failed",
                _error_message(activate_body) or "2FA TOTP 激活失败",
                stage="two_factor",
                response=activate,
            )
        validation = await self._request(
            active_transport,
            "GET",
            self._url(self.config.chat_base_url, PROTOCOL_ENDPOINTS["models"]),
            label="totp_validate",
            headers={**api_headers, "Content-Type": ""},
        )
        responses["totp_validate"] = _response_summary(validation)
        if validation.status_code < 200 or validation.status_code >= 300:
            raise ProtocolRegistrationError(
                "totp_token_validation_failed",
                "2FA 已激活，但新的 Session Token 验证失败",
                stage="two_factor",
                response=validation,
            )
        activated_at = datetime.now(UTC)
        return _ProtocolTotpResult(
            secret=secret,
            access_token=access_token,
            expires_at=expires_at,
            activated_at=activated_at,
            auth_session=refreshed_body,
        )

    @staticmethod
    def _is_totp_email_verification_url(value: str) -> bool:
        try:
            path_segments = {
                segment
                for segment in urlsplit(str(value or "")).path.casefold().split("/")
                if segment
            }
        except ValueError:
            return False
        return bool(
            path_segments.intersection(
                {"email-verification", "email_otp", "verify-email"}
            )
        )

    @staticmethod
    def _is_trusted_openai_page(value: str) -> bool:
        """Reject MFA redirects that leave the OpenAI HTTPS host set."""

        try:
            parsed = urlsplit(str(value or ""))
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold()
        trusted_host = host in {"openai.com", "chatgpt.com"} or host.endswith(
            (".openai.com", ".chatgpt.com")
        )
        return (
            parsed.scheme.casefold() == "https"
            and trusted_host
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )

    async def _persist(self, request: ProtocolRegistrationRequest, result: ProtocolRegistrationResult) -> None:
        if self.persist is None:
            return
        value = self.persist(request, result)
        if hasattr(value, "__await__"):
            await value

    async def _emit(self, stage: str, details: Mapping[str, Any]) -> None:
        if self.progress is None:
            return
        try:
            value = self.progress(stage, details)
            if hasattr(value, "__await__"):
                await value
        except Exception:
            # Progress reporting is observational and must never invalidate an
            # otherwise usable account transaction.
            return

    def _url(self, base: str, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return base.rstrip("/") + "/" + path.lstrip("/")

    @staticmethod
    def _with_query(url: str, key: str, value: str) -> str:
        if f"{key}=" in url:
            return url
        return url + ("&" if "?" in url else "?") + urlencode({key: value})

    @staticmethod
    def _redirect_url(response: ProtocolResponse, base: str) -> str:
        location = response.header("location")
        if location:
            return urljoin(base.rstrip("/") + "/", location)
        return response.url or ""

    @staticmethod
    def _next_url(response: ProtocolResponse, base: str) -> str:
        body = _as_dict(response.body)
        raw = str(body.get("continue_url") or body.get("url") or response.header("location") or "").strip()
        return urljoin(base.rstrip("/") + "/", raw) if raw else ""


class _suppress_exceptions:
    def __enter__(self) -> "_suppress_exceptions":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return True


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _body_value(response: ProtocolResponse, key: str) -> Any:
    return _as_dict(response.body).get(key)


def _continue_url(body: Mapping[str, Any], base: str) -> str:
    value = str(body.get("continue_url") or body.get("redirect_uri") or body.get("redirect_url") or "").strip()
    return urljoin(base.rstrip("/") + "/", value) if value else ""


def _error_code(body: Mapping[str, Any]) -> str:
    error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
    return str(error.get("code") or body.get("code") or "").strip()


def _cookie_value(cookie_header: str, name: str) -> str:
    cookies = SimpleCookie()
    try:
        cookies.load(str(cookie_header or ""))
    except Exception:
        return ""
    morsel = cookies.get(name)
    return str(morsel.value if morsel is not None else "").strip()


def _transient_response_error(response: ProtocolResponse | None) -> str:
    if response is None:
        return ""
    status = int(response.status_code or 0)
    if status == 429:
        return "http_429"
    headers = {
        str(key).strip().casefold(): str(value or "").strip().casefold()
        for key, value in response.headers.items()
    }
    if headers.get("cf-mitigated") == "challenge":
        return "cloudflare_challenge"
    if status in {403, 500, 502, 503, 504} and (
        headers.get("server") == "cloudflare" or "cf-ray" in headers
    ):
        return "cloudflare_challenge"
    body = response.body
    body_text = json.dumps(body, ensure_ascii=False) if isinstance(body, Mapping) else str(body or "")
    challenge_text = f"{response.text} {body_text}".casefold()
    content_type = headers.get("content-type", "")
    if "text/html" in content_type and any(
        marker in challenge_text
        for marker in (
            "<title>just a moment",
            "cf-chl-",
            "cf_chl_",
            "verify you are human",
            "checking your browser",
            "enable javascript and cookies",
            "unable to load site",
            "using a vpn, try turning it off",
        )
    ):
        return "cloudflare_challenge"
    if status == 403 and any(
        marker in challenge_text
        for marker in ("cloudflare", "cf-chl", "challenge-platform", "just a moment")
    ):
        return "cloudflare_challenge"
    return ""


def _error_message(body: Mapping[str, Any]) -> str:
    error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
    return str(error.get("message") or body.get("message") or "").strip()


def _is_wrong_otp(body: Mapping[str, Any]) -> bool:
    code = _error_code(body).casefold()
    message = _error_message(body).casefold()
    return code == "wrong_email_otp_code" or "wrong code" in message


def _otp_pending_response(response: ProtocolResponse) -> bool:
    body = _as_dict(response.body)
    text = " ".join(
        [
            _error_code(body),
            _error_message(body),
            str(body.get("message") or ""),
        ]
    ).casefold()
    return any(marker in text for marker in ("already", "pending", "rate", "too_many"))


def _is_existing_login_url(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    path = (parsed.path or "").rstrip("/").casefold()
    return path in {"/log-in", "/login"} or path.startswith(("/log-in/", "/login/"))


def _is_chatgpt_auth_login_landing(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    host = (parsed.netloc or "").casefold()
    path = (parsed.path or "").rstrip("/").casefold()
    return host.endswith("chatgpt.com") and path in {"/auth/login", "/auth/log-in"}


def _extract_access_token(body: Mapping[str, Any]) -> str:
    session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
    return str(body.get("accessToken") or body.get("access_token") or session.get("access_token") or session.get("accessToken") or "").strip()


def _extract_expiry(body: Mapping[str, Any]) -> datetime | None:
    session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
    value = (
        body.get("expiresAt")
        or body.get("expires_at")
        or session.get("expiresAt")
        or session.get("expires_at")
    )
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                float(value) / (1000 if value > 10_000_000_000 else 1),
                UTC,
            )
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    token = _extract_access_token(body)
    parts = token.split(".")
    if len(parts) == 3:
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
            exp = float(payload.get("exp"))
            return datetime.fromtimestamp(exp, UTC)
        except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
            pass
    return None


def _response_summary(response: ProtocolResponse | None) -> dict[str, Any]:
    if response is None:
        return {}
    body = response.body
    # Keep endpoint diagnostics useful while avoiding accidental token leaks.
    if isinstance(body, Mapping):
        body = _redact_mapping(body)
    return {"status_code": int(response.status_code), "url": response.url, "headers": {k: v for k, v in response.headers.items() if str(k).casefold() in {"location", "content-type"}}, "body": body}


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    secret_keys = {"access_token", "accessToken", "refresh_token", "refreshToken", "id_token", "idToken", "csrf_token", "csrfToken", "cookie_header", "cookieHeader", "sentinel_token", "sentinel_so_token", "secret", "totp_secret", "totpSecret", "session_id", "sessionId"}
    def redact(item: Any) -> Any:
        if isinstance(item, Mapping):
            return _redact_mapping(item)
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, tuple):
            return [redact(child) for child in item]
        return item

    return {
        str(key): (
            "[redacted]"
            if (
                str(key) in secret_keys
                or str(key).casefold().endswith("token")
                or str(key).casefold().endswith("secret")
            )
            else redact(item)
        )
        for key, item in value.items()
    }


def _generate_password() -> str:
    import secrets
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghjkmnpqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#$%&*?"
    alphabet = upper + lower + digits + symbols
    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
        *(secrets.choice(alphabet) for _ in range(10)),
    ]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
