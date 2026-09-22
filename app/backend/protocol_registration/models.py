"""Contracts used by the HTTP registration protocol.

The browser registration path has a fairly large object graph and is tightly
coupled to Playwright.  These small, serialisable contracts deliberately keep
the protocol path independent from that implementation so it can be exercised
with a fake transport in unit tests and used by a worker process later on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping


RegistrationMode = Literal["password", "passwordless"]


def _normalize_mode(value: Any) -> RegistrationMode:
    raw = str(value or "").strip().casefold().replace("-", "_")
    if raw in {"password", "password_signup", "user_register", "legacy"}:
        return "password"
    if raw in {"passwordless", "passwordless_signup", "login_or_signup", "har", ""}:
        return "passwordless"
    raise ValueError("registration_mode must be password or passwordless")


def _clean_url(value: str, default: str) -> str:
    value = str(value or "").strip()
    return (value or default).rstrip("/")


@dataclass(frozen=True, slots=True)
class ProtocolRegistrationConfig:
    """Runtime knobs for :class:`ProtocolRegistrationService`.

    Values mirror the endpoints and retry limits used by the baseline
    registration tool.  Bounds are intentionally conservative; callers can
    still provide a custom transport when a different timeout policy is needed.
    """

    chat_base_url: str = "https://chatgpt.com"
    auth_base_url: str = "https://auth.openai.com"
    sentinel_base_url: str = "https://sentinel.openai.com"
    registration_mode: RegistrationMode = "passwordless"
    timeout_seconds: float = 30.0
    otp_timeout_seconds: float = 300.0
    otp_poll_interval_seconds: float = 5.0
    auth_session_attempts: int = 6
    auth_session_delay_seconds: float = 2.0
    signin_attempts: int = 3
    otp_validation_retries: int = 1
    otp_fallback_send: bool = False
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )
    # TOTP enrollment is opt-in at the protocol-service level.  The run
    # executor maps the persisted UI setting onto this flag; keeping the
    # service default off preserves the lightweight contract used by callers
    # that only want account creation.
    enable_registration_totp: bool = False
    # ``enable_totp`` is accepted as a short constructor alias for integrations
    # that do not use the UI naming convention.
    enable_totp: bool | None = None
    totp_timeout_seconds: float = 300.0
    totp_poll_interval_seconds: float = 5.0
    totp_reauth_delay_seconds: float = 20.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_base_url", _clean_url(self.chat_base_url, "https://chatgpt.com"))
        object.__setattr__(self, "auth_base_url", _clean_url(self.auth_base_url, "https://auth.openai.com"))
        object.__setattr__(self, "sentinel_base_url", _clean_url(self.sentinel_base_url, "https://sentinel.openai.com"))
        object.__setattr__(self, "registration_mode", _normalize_mode(self.registration_mode))
        if self.enable_totp is not None:
            object.__setattr__(self, "enable_registration_totp", bool(self.enable_totp))
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.totp_timeout_seconds <= 0:
            raise ValueError("totp_timeout_seconds must be positive")
        if self.totp_poll_interval_seconds < 0:
            raise ValueError("totp_poll_interval_seconds must not be negative")
        if self.totp_reauth_delay_seconds < 0:
            raise ValueError("totp_reauth_delay_seconds must not be negative")
        if self.otp_timeout_seconds <= 0:
            raise ValueError("otp_timeout_seconds must be positive")
        if self.otp_poll_interval_seconds < 0:
            raise ValueError("otp_poll_interval_seconds must not be negative")
        if self.auth_session_attempts < 1:
            raise ValueError("auth_session_attempts must be at least 1")
        if self.signin_attempts < 1:
            raise ValueError("signin_attempts must be at least 1")
        if self.otp_validation_retries < 0:
            raise ValueError("otp_validation_retries must not be negative")


@dataclass(slots=True)
class ProtocolRegistrationRequest:
    """Input for one account registration attempt."""

    email: str
    email_access_url: str = ""
    password: str = field(default="", repr=False)
    registration_mode: RegistrationMode | None = None
    registration_country: str = "ZZ"
    proxy: str | None = None
    source_email_id: str | None = None
    sentinel_data: dict[str, Any] | None = None
    device_id: str | None = None
    session_logging_id: str | None = None
    first_name: str = ""
    last_name: str = ""
    birthdate: str = ""
    otp_code: str | None = field(default=None, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.email = str(self.email or "").strip().lower()
        if not self.email or "@" not in self.email:
            raise ValueError("email is required")
        self.email_access_url = str(self.email_access_url or "").strip()
        self.registration_country = str(self.registration_country or "ZZ").strip().upper() or "ZZ"
        self.source_email_id = str(self.source_email_id or "").strip() or None
        self.device_id = str(self.device_id or "").strip() or None
        self.session_logging_id = str(self.session_logging_id or "").strip() or None
        if len(self.registration_country) != 2:
            self.registration_country = "ZZ"
        if self.registration_mode is not None:
            self.registration_mode = _normalize_mode(self.registration_mode)


@dataclass(frozen=True, slots=True)
class ProtocolResponse:
    """Transport-neutral HTTP response used by the state machine and fakes."""

    status_code: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    text: str = ""

    def json(self) -> Any:
        if self.body is not None:
            return self.body
        if self.text:
            try:
                return json.loads(self.text)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return None

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code) < 300

    def header(self, name: str, default: str = "") -> str:
        target = name.casefold()
        for key, value in self.headers.items():
            if str(key).casefold() == target:
                return str(value)
        return default


@dataclass(frozen=True, slots=True)
class ProtocolCall:
    """A redaction-friendly record of a request made by a transport."""

    method: str
    url: str
    kwargs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProtocolRegistrationResult:
    success: bool
    email: str
    password: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    id_token: str = field(default="", repr=False)
    device_id: str = ""
    registration_mode: RegistrationMode = "password"
    registration_country: str = "ZZ"
    stage: str = "finished"
    error: str = ""
    warning: str = ""
    auth_session: dict[str, Any] = field(default_factory=dict, repr=False)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    cookie_header: str = field(default="", repr=False)
    source_email_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    # Appended after the original contract fields to keep positional callers
    # source-compatible while exposing protocol MFA state explicitly.
    totp_secret: str = field(default="", repr=False)
    totp_activated_at: datetime | None = field(default=None, repr=False)
    totp_error: str = ""

    def to_dict(self, *, include_secrets: bool = True) -> dict[str, Any]:
        """Return a JSON-compatible result.

        ``include_secrets=False`` is used for progress/log responses.  The
        state machine itself retains the token so a caller can persist it.
        """

        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        if not include_secrets:
            for key in (
                "password",
                "totp_secret",
                "access_token",
                "refresh_token",
                "id_token",
                "cookie_header",
            ):
                data[key] = "" if key != "password" else ("[stored]" if self.password else "")
            data["auth_session"] = _redact_secrets(data["auth_session"])
            data["responses"] = _redact_secrets(data["responses"])
            data["metadata"] = _redact_secrets(data["metadata"])
        return _jsonable(data)


@dataclass(frozen=True, slots=True)
class SentinelData:
    """Normalised Sentinel values carried by auth requests."""

    sentinel_token: str = ""
    sentinel_oauth_token: str = ""
    sentinel_so_token: str = ""
    cookie_str: str = ""
    oai_did: str = ""
    sentinel_authorize_token: str = ""
    sentinel_authorize_so_token: str = ""
    sentinel_username_so_token: str = ""

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | None) -> "SentinelData":
        value = value or {}
        return cls(
            sentinel_token=str(value.get("sentinel_token") or value.get("sentinelToken") or ""),
            sentinel_oauth_token=str(value.get("sentinel_oauth_token") or value.get("sentinelOauthToken") or ""),
            sentinel_so_token=str(value.get("sentinel_so_token") or value.get("sentinelSoToken") or ""),
            cookie_str=str(value.get("cookie_str") or value.get("cookieStr") or ""),
            oai_did=str(value.get("oai_did") or value.get("oaiDid") or value.get("device_id") or value.get("deviceId") or ""),
            sentinel_authorize_token=str(
                value.get("sentinel_authorize_token")
                or value.get("sentinelAuthorizeToken")
                or value.get("sentinel_authorize_continue_token")
                or value.get("sentinelAuthorizeContinueToken")
                or ""
            ),
            sentinel_authorize_so_token=str(
                value.get("sentinel_authorize_so_token")
                or value.get("sentinelAuthorizeSoToken")
                or value.get("sentinel_authorize_continue_so_token")
                or value.get("sentinelAuthorizeContinueSoToken")
                or ""
            ),
            sentinel_username_so_token=str(
                value.get("sentinel_username_so_token")
                or value.get("sentinelUsernameSoToken")
                or value.get("sentinel_username_password_create_so_token")
                or value.get("sentinelUsernamePasswordCreateSoToken")
                or ""
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "sentinel_token": self.sentinel_token,
            "sentinel_oauth_token": self.sentinel_oauth_token,
            "sentinel_so_token": self.sentinel_so_token,
            "cookie_str": self.cookie_str,
            "oai_did": self.oai_did,
            "sentinel_authorize_token": self.sentinel_authorize_token,
            "sentinel_authorize_so_token": self.sentinel_authorize_so_token,
            "sentinel_username_so_token": self.sentinel_username_so_token,
        }


def _redact_secrets(value: Any) -> Any:
    secret_keys = {
        "access_token",
        "accessToken",
        "csrfToken",
        "csrf_token",
        "refresh_token",
        "refreshToken",
        "id_token",
        "idToken",
        "password",
        "totp_secret",
        "totpSecret",
        "secret",
        "session_id",
        "sessionId",
        "cookie",
        "cookie_header",
        "cookieHeader",
        "sentinel_token",
        "sentinel_authorize_token",
        "sentinel_authorize_so_token",
        "sentinel_username_so_token",
        "sentinel_oauth_token",
        "sentinel_so_token",
    }
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]"
            if (
                str(key) in secret_keys
                or str(key).casefold().endswith("token")
                or str(key).casefold().endswith("secret")
            )
            else _redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_secrets(item) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
