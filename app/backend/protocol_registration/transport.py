"""HTTP transport adapters for protocol registration.

The service only depends on ``ProtocolTransport.request``.  Keeping response
normalisation here makes the registration state machine deterministic in tests
and lets production select curl-cffi (for browser-like TLS fingerprints) or a
small custom adapter without changing protocol code.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import suppress
from typing import Any, Protocol, runtime_checkable

from .models import ProtocolCall, ProtocolResponse


@runtime_checkable
class ProtocolTransport(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> ProtocolResponse:
        """Issue one request and return a transport-neutral response."""

    async def close(self) -> None:
        """Release the underlying client, if any."""

    def cookie_header(self) -> str:
        """Return the current cookie jar as a request header value."""


def _json_body(response: Any) -> Any:
    try:
        value = response.json()
        if value is not None:
            return value
    except Exception:
        pass
    try:
        import json

        return json.loads(str(getattr(response, "text", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


class CurlCffiTransport:
    """Async curl-cffi transport used by real protocol workers."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: float = 30.0,
        user_agent: str | None = None,
        impersonate: str = "chrome136",
        session: Any | None = None,
    ) -> None:
        self.proxy = str(proxy or "").strip() or None
        self.timeout = float(timeout)
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36"
        self.impersonate = impersonate
        self._session = session
        self._owns_session = session is None
        self._pending_cookies: list[tuple[str, str, str | None, str]] = []
        self.calls: list[ProtocolCall] = []
        self._ready = False

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - dependency is in app requirements
            raise RuntimeError("curl_cffi is required for CurlCffiTransport") from exc
        session_kwargs: dict[str, Any] = {}
        if self.proxy:
            session_kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        self._session = curl_requests.AsyncSession(**session_kwargs)
        for name, value, domain, path in self._pending_cookies:
            self._set_cookie_now(name, value, domain=domain, path=path)
        self._pending_cookies.clear()
        self._ready = True
        return self._session

    async def request(self, method: str, url: str, **kwargs: Any) -> ProtocolResponse:
        session = await self._ensure_session()
        request_kwargs = dict(kwargs)
        request_kwargs.setdefault("timeout", self.timeout)
        request_kwargs.setdefault("impersonate", self.impersonate)
        headers = dict(request_kwargs.get("headers") or {})
        headers.setdefault("User-Agent", self.user_agent)
        request_kwargs["headers"] = headers
        self.calls.append(ProtocolCall(method.upper(), str(url), _safe_call_kwargs(request_kwargs)))
        response = await session.request(method.upper(), url, **request_kwargs)
        return ProtocolResponse(
            status_code=int(getattr(response, "status_code", 0) or 0),
            body=_json_body(response),
            headers=dict(getattr(response, "headers", {}) or {}),
            url=str(getattr(response, "url", "") or url),
            text=str(getattr(response, "text", "") or ""),
        )

    def cookie_header(self) -> str:
        cookies = getattr(self._session, "cookies", None)
        if cookies is None:
            return "; ".join(
                f"{name}={value}" for name, value, _domain, _path in self._pending_cookies if name and value
            )
        try:
            if hasattr(cookies, "get_dict"):
                items = cookies.get_dict().items()
            else:
                items = ((getattr(item, "name", ""), getattr(item, "value", "")) for item in cookies)
            return "; ".join(f"{name}={value}" for name, value in items if name and value)
        except Exception:
            return ""

    def set_cookie(self, name: str, value: str, *, domain: str | None = None, path: str = "/") -> None:
        if self._session is None:
            self._pending_cookies.append((name, value, domain, path))
            return
        self._set_cookie_now(name, value, domain=domain, path=path)

    def _set_cookie_now(self, name: str, value: str, *, domain: str | None = None, path: str = "/") -> None:
        try:
            self._session.cookies.set(name, value, domain=domain, path=path)
        except Exception:
            with suppress(Exception):
                self._session.cookies.set(name, value)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and self._owns_session:
            close = getattr(session, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


def _safe_call_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Keep fake call records useful without retaining mutable/secret objects."""
    result: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in {"headers", "json", "data", "params"}:
            if isinstance(value, Mapping):
                result[key] = _redact_value(value)
            else:
                result[key] = value
        elif key in {"timeout", "allow_redirects", "impersonate"}:
            result[key] = value
    return result


def _redact_value(value: Any) -> Any:
    secret_names = {
        "authorization", "cookie", "set-cookie", "openai-sentinel-token",
        "openai-sentinel-so-token", "access_token", "accessToken",
        "refresh_token", "refreshToken", "password", "csrfToken", "csrf_token",
        "session_id", "sessionId", "secret", "totp_secret", "totpSecret",
        "code", "otp", "verification_code", "verificationCode",
    }
    if isinstance(value, Mapping):
        return {
            str(key): ("[redacted]" if str(key).casefold() in {item.casefold() for item in secret_names} else _redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


class FakeTransport:
    """Deterministic transport for endpoint-sequence tests.

    ``responses`` may contain :class:`ProtocolResponse`, dictionaries (with
    ``status_code``, ``body``, ``headers`` and ``url`` keys), callables, or
    async callables.  A callable receives the recorded ``ProtocolCall``.
    """

    def __init__(
        self,
        responses: Iterable[Any] = (),
        default_response: ProtocolResponse | None = None,
    ) -> None:
        self.default_response = default_response
        self._responses = deque(responses)
        self.calls: list[ProtocolCall] = []
        self.closed = False
        self._routes: list[tuple[str | None, str | None, deque[Any]]] = []

    def queue(self, *responses: Any) -> "FakeTransport":
        self._responses.extend(responses)
        return self

    def route(self, method: str | None, url: str | None, *responses: Any) -> "FakeTransport":
        self._routes.append((method.upper() if method else None, url, deque(responses)))
        return self

    async def request(self, method: str, url: str, **kwargs: Any) -> ProtocolResponse:
        if self.closed:
            raise RuntimeError("fake transport is closed")
        call = ProtocolCall(method.upper(), str(url), _safe_call_kwargs(kwargs))
        self.calls.append(call)
        item = self._take_route(call) if self._routes else None
        if item is None and self._responses:
            item = self._responses.popleft()
        if item is None:
            item = self.default_response
        if item is None:
            raise AssertionError(f"FakeTransport has no response for {call.method} {call.url}")
        if callable(item):
            item = item(call)
            if hasattr(item, "__await__"):
                item = await item
        return _coerce_response(item, url)

    def _take_route(self, call: ProtocolCall) -> Any | None:
        for method, pattern, responses in self._routes:
            if method and method != call.method:
                continue
            if pattern and pattern not in call.url:
                continue
            if responses:
                return responses.popleft()
        return None

    def cookie_header(self) -> str:
        return ""

    async def close(self) -> None:
        self.closed = True


def _coerce_response(value: Any, request_url: str) -> ProtocolResponse:
    if isinstance(value, ProtocolResponse):
        return value
    if isinstance(value, int):
        return ProtocolResponse(value, {}, url=request_url)
    if isinstance(value, Mapping):
        return ProtocolResponse(
            int(value.get("status_code", value.get("status", 200)) or 0),
            value.get("body", value.get("json")),
            dict(value.get("headers") or {}),
            str(value.get("url") or request_url),
            str(value.get("text") or ""),
        )
    # Accept requests/curl-cffi response-like objects as a convenience for
    # tests that already use the project's HTTP fixtures.
    if hasattr(value, "status_code"):
        body = _json_body(value)
        return ProtocolResponse(
            int(getattr(value, "status_code", 0) or 0),
            body,
            dict(getattr(value, "headers", {}) or {}),
            str(getattr(value, "url", "") or request_url),
            str(getattr(value, "text", "") or ""),
        )
    raise TypeError(f"unsupported fake response: {type(value).__name__}")


# Friendly alias used by callers that do not care which HTTP implementation is
# selected.
DefaultProtocolTransport = CurlCffiTransport
