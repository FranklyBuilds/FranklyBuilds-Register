"""Sentinel providers for the protocol registration path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import uuid
from typing import Any, Protocol

from .models import ProtocolRegistrationConfig, ProtocolRegistrationRequest, ProtocolResponse, SentinelData
from .transport import ProtocolTransport


class SentinelProvider(Protocol):
    async def get(
        self,
        request: ProtocolRegistrationRequest,
        config: ProtocolRegistrationConfig,
        transport: ProtocolTransport,
    ) -> SentinelData:
        """Return tokens and cookies bound to this registration device."""


class StaticSentinelProvider:
    """Use caller-supplied tokens, generating only a device id when needed."""

    def __init__(self, data: SentinelData | dict[str, Any] | None = None) -> None:
        self.data = data if isinstance(data, SentinelData) else SentinelData.from_value(data)

    async def get(self, request: ProtocolRegistrationRequest, config: ProtocolRegistrationConfig, transport: ProtocolTransport) -> SentinelData:
        data = self.data
        did = request.device_id or data.oai_did
        if not did and data.sentinel_token:
            try:
                did = str(json.loads(data.sentinel_token).get("id") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                did = ""
        did = did or str(uuid.uuid4())
        return SentinelData(
            data.sentinel_token,
            data.sentinel_oauth_token,
            data.sentinel_so_token,
            data.cookie_str,
            did,
            data.sentinel_authorize_token,
            data.sentinel_authorize_so_token,
            data.sentinel_username_so_token,
        )


class HttpSentinelProvider:
    """Fetch Sentinel flows over the same injected transport.

    The proof-of-work envelope follows the baseline HTTP implementation.  A
    deployment that has a browser/QuickJS Sentinel solver can provide a custom
    provider instead; the state machine does not assume a particular solver.
    """

    def __init__(self, *, max_nonce: int = 500_000) -> None:
        self.max_nonce = max(1, int(max_nonce))

    async def get(self, request: ProtocolRegistrationRequest, config: ProtocolRegistrationConfig, transport: ProtocolTransport) -> SentinelData:
        did = request.device_id or str(uuid.uuid4())
        results: dict[str, dict[str, Any]] = {}
        for flow in (
            "authorize_continue",
            "username_password_create",
            "oauth_create_account",
        ):
            response = await transport.request(
                "POST",
                f"{config.sentinel_base_url}/backend-api/sentinel/req",
                timeout=config.timeout_seconds,
                data=json.dumps({"p": "", "id": did, "flow": flow}),
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Accept": "*/*",
                    "Origin": config.sentinel_base_url,
                    "Referer": f"{config.sentinel_base_url}/backend-api/sentinel/frame.html",
                    "User-Agent": config.user_agent,
                },
            )
            body = response.body if isinstance(response.body, dict) else {}
            if response.status_code < 200 or response.status_code >= 300 or not body.get("token"):
                raise RuntimeError(f"sentinel_{flow}_failed:{response.status_code}")
            results[flow] = body

        authorize = results["authorize_continue"]
        upc = results["username_password_create"]
        oauth = results["oauth_create_account"]
        authorize_token, username_token, oauth_token = await asyncio.gather(
            asyncio.to_thread(_build_token, authorize, did, "authorize_continue", self.max_nonce),
            asyncio.to_thread(_build_token, upc, did, "username_password_create", self.max_nonce),
            asyncio.to_thread(_build_token, oauth, did, "oauth_create_account", self.max_nonce),
        )
        return SentinelData(
            sentinel_token=username_token,
            sentinel_oauth_token=oauth_token or username_token,
            cookie_str=transport.cookie_header(),
            oai_did=did,
            sentinel_authorize_token=authorize_token,
        )


class AutoSentinelProvider:
    """Use the real SDK first, with the baseline HTTP provider as fallback."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        max_concurrency: int = 2,
        quickjs_provider: SentinelProvider | None = None,
        http_provider: SentinelProvider | None = None,
    ) -> None:
        raw_mode = str(
            mode
            or os.getenv("OPENAI_SENTINEL_MODE", "")
            or os.getenv("AUTOREGISTER_SENTINEL_MODE", "")
            or "auto"
        ).strip().casefold()
        self.mode = raw_mode if raw_mode in {"auto", "quickjs", "http"} else "auto"
        if quickjs_provider is None:
            from .sentinel_quickjs import QuickJsSentinelProvider

            quickjs_provider = QuickJsSentinelProvider()
        self.quickjs_provider = quickjs_provider
        self.http_provider = http_provider or HttpSentinelProvider()
        self._gate = asyncio.Semaphore(max(1, min(int(max_concurrency), 4)))

    async def get(
        self,
        request: ProtocolRegistrationRequest,
        config: ProtocolRegistrationConfig,
        transport: ProtocolTransport,
    ) -> SentinelData:
        providers = (
            [self.quickjs_provider, self.http_provider]
            if self.mode == "auto"
            else [self.quickjs_provider]
            if self.mode == "quickjs"
            else [self.http_provider]
        )
        errors: list[str] = []
        async with self._gate:
            for provider in providers:
                try:
                    value = provider.get(request, config, transport)
                    value = await value if hasattr(value, "__await__") else value
                    if isinstance(value, SentinelData) and value.sentinel_token:
                        return value
                    errors.append("empty_token")
                except Exception as exc:
                    errors.append(str(getattr(exc, "code", "") or type(exc).__name__))
        raise RuntimeError("sentinel_providers_failed:" + ",".join(errors[:2]))


def _build_token(flow: dict[str, Any], did: str, name: str, max_nonce: int) -> str:
    proof = flow.get("proofofwork") if isinstance(flow.get("proofofwork"), dict) else {}
    encoded = ""
    if proof.get("required") and proof.get("seed") and proof.get("difficulty"):
        encoded = _solve_pow(str(proof["seed"]), str(proof["difficulty"]), max_nonce)
    return json.dumps({"p": flow.get("p", ""), "t": encoded, "c": flow.get("token", ""), "id": did, "flow": name})


def _solve_pow(seed: str, difficulty_hex: str, max_nonce: int) -> str:
    try:
        difficulty = int(difficulty_hex, 16)
    except (TypeError, ValueError):
        return ""
    prefix_len = (len(difficulty_hex) + 1) // 2
    for nonce in range(max_nonce):
        digest = hashlib.sha3_512(f"{seed}{nonce}".encode()).digest()
        value = 0
        for byte in digest[:prefix_len]:
            value = (value << 8) + byte
        if value <= difficulty:
            return base64.b64encode(struct.pack(">Q", nonce)).decode()
    return ""
