"""Sentinel SDK provider used by protocol registration.

The provider runs the real Sentinel SDK in Node for the requirements and solve
steps.  The network exchange remains on the worker's injected transport so the
SDK download, challenge, and registration share one proxy identity.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import (
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    SentinelData,
)
from .transport import ProtocolTransport


DEFAULT_SENTINEL_VERSION = "20260219f9f6"

_WRAPPER_JS = r"""
const fs = require('fs');
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || '10000');
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const scriptFile = process.env.OPENAI_SENTINEL_QUICKJS_SCRIPT;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const payload = JSON.parse(input || '{}');
    globalThis.__payload_json = JSON.stringify(payload);
    globalThis.__sdk_source = fs.readFileSync(sdkFile, 'utf8');
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';
    const script = fs.readFileSync(scriptFile, 'utf8');
    eval(script);

    const started = Date.now();
    while (!globalThis.__vm_done) {
      if ((Date.now() - started) > timeoutMs) {
        throw new Error('Sentinel SDK timeout');
      }
      await new Promise((resolve) => setTimeout(resolve, 1));
    }

    if (String(globalThis.__vm_error || '').trim()) {
      throw new Error(String(globalThis.__vm_error));
    }
    process.stdout.write(String(globalThis.__vm_output_json || ''));
  } catch (err) {
    const message = err && err.stack ? String(err.stack) : String(err);
    process.stderr.write(message);
    process.exit(1);
  }
});
""".strip()


def sentinel_version() -> str:
    value = str(os.getenv("OPENAI_SENTINEL_VERSION", "") or "").strip()
    if value and all(character.isalnum() or character in {"-", "_"} for character in value):
        return value
    return DEFAULT_SENTINEL_VERSION


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    adapter_file: Path,
    payload: dict[str, Any],
    timeout_ms: int,
    node_path: str,
) -> dict[str, Any]:
    body = {**payload, "action": action}
    creation_flags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    )
    completed = subprocess.run(
        [node_path, "-e", _WRAPPER_JS],
        input=json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=max(10, int(timeout_ms / 1000) + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            "OPENAI_SENTINEL_QUICKJS_SCRIPT": str(adapter_file),
            "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(min(timeout_ms, 30_000)),
        },
        creationflags=creation_flags,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("sentinel_sdk_execution_failed")
    output = (completed.stdout or "").strip()
    if not output:
        raise RuntimeError("sentinel_sdk_empty_output")
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("sentinel_sdk_invalid_output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("sentinel_sdk_invalid_output")
    return value


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class QuickJsSentinelProvider:
    """Generate Sentinel tokens with the SDK-backed baseline algorithm."""

    def __init__(
        self,
        *,
        version: str | None = None,
        node_path: str | None = None,
        adapter_file: Path | None = None,
        cache_dir: Path | None = None,
        timeout_ms: int = 45_000,
        action_runner: Callable[..., dict[str, Any]] = _run_quickjs_action,
    ) -> None:
        self.version = str(version or sentinel_version())
        if not self.version or not all(
            character.isalnum() or character in {"-", "_"}
            for character in self.version
        ):
            raise ValueError("invalid Sentinel version")
        self.node_path = str(
            node_path or os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "node"
        )
        self.adapter_file = adapter_file or Path(__file__).with_name(
            "openai_sentinel_quickjs.js"
        )
        self.cache_dir = cache_dir or (
            Path(tempfile.gettempdir()) / "autoregister-sentinel" / self.version
        )
        self.timeout_ms = max(10_000, min(int(timeout_ms), 120_000))
        self.action_runner = action_runner
        self._sdk_lock = asyncio.Lock()

    async def _ensure_sdk_file(
        self,
        config: ProtocolRegistrationConfig,
        transport: ProtocolTransport,
    ) -> Path:
        sdk_file = self.cache_dir / "sdk.js"
        if sdk_file.exists() and sdk_file.stat().st_size > 0:
            return sdk_file
        async with self._sdk_lock:
            if sdk_file.exists() and sdk_file.stat().st_size > 0:
                return sdk_file
            response = await transport.request(
                "GET",
                f"{config.sentinel_base_url}/sentinel/{self.version}/sdk.js",
                timeout=config.timeout_seconds,
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": f"{config.auth_base_url}/",
                    "Sec-Fetch-Dest": "script",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "same-site",
                    "User-Agent": config.user_agent,
                },
            )
            if response.status_code != 200:
                raise RuntimeError(f"sentinel_sdk_http_{response.status_code}")
            content = response.text
            if not content and isinstance(response.body, str):
                content = response.body
            if not str(content or "").strip():
                raise RuntimeError("sentinel_sdk_empty_response")
            await asyncio.to_thread(_write_atomic, sdk_file, str(content))
            return sdk_file

    async def _token(
        self,
        *,
        flow: str,
        did: str,
        sdk_file: Path,
        config: ProtocolRegistrationConfig,
        transport: ProtocolTransport,
    ) -> tuple[str, str]:
        runner_kwargs = {
            "sdk_file": sdk_file,
            "adapter_file": self.adapter_file,
            "timeout_ms": self.timeout_ms,
            "node_path": self.node_path,
        }
        requirements = await asyncio.to_thread(
            self.action_runner,
            action="requirements",
            payload={"device_id": did},
            **runner_kwargs,
        )
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            raise RuntimeError("sentinel_sdk_missing_requirements")
        challenge_response = await transport.request(
            "POST",
            f"{config.sentinel_base_url}/backend-api/sentinel/req",
            timeout=config.timeout_seconds,
            data=json.dumps(
                {"p": request_p, "id": did, "flow": flow},
                separators=(",", ":"),
            ),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Accept": "*/*",
                "Origin": config.sentinel_base_url,
                "Referer": (
                    f"{config.sentinel_base_url}/backend-api/sentinel/frame.html"
                    f"?sv={self.version}"
                ),
                "User-Agent": config.user_agent,
            },
        )
        challenge = (
            challenge_response.body
            if isinstance(challenge_response.body, dict)
            else {}
        )
        challenge_token = str(challenge.get("token") or "").strip()
        if challenge_response.status_code != 200 or not challenge_token:
            raise RuntimeError(
                f"sentinel_challenge_http_{challenge_response.status_code}"
            )
        solved = await asyncio.to_thread(
            self.action_runner,
            action="solve",
            payload={
                "device_id": did,
                "request_p": request_p,
                "challenge": challenge,
            },
            **runner_kwargs,
        )
        final_p = str(solved.get("final_p") or solved.get("p") or "").strip()
        t_value = str(solved.get("t") or "").strip()
        turnstile = (
            challenge.get("turnstile")
            if isinstance(challenge.get("turnstile"), dict)
            else {}
        )
        if not final_p or (turnstile.get("required") and not t_value):
            raise RuntimeError("sentinel_sdk_incomplete_solution")
        token = json.dumps(
            {
                "p": final_p,
                "t": t_value,
                "c": challenge_token,
                "id": did,
                "flow": flow,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        observer_token = solved.get("so")
        observer_token = (
            observer_token.strip()
            if isinstance(observer_token, str)
            else ""
        )
        so_token = (
            json.dumps(
                {
                    "so": observer_token,
                    "c": challenge_token,
                    "id": did,
                    "flow": flow,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if observer_token
            else ""
        )
        return token, so_token

    async def get(
        self,
        request: ProtocolRegistrationRequest,
        config: ProtocolRegistrationConfig,
        transport: ProtocolTransport,
    ) -> SentinelData:
        if not self.adapter_file.is_file():
            raise RuntimeError("sentinel_sdk_adapter_missing")
        did = request.device_id or str(uuid.uuid4())
        sdk_file = await self._ensure_sdk_file(config, transport)
        authorize_token, authorize_so_token = await self._token(
            flow="authorize_continue",
            did=did,
            sdk_file=sdk_file,
            config=config,
            transport=transport,
        )
        username_token, username_so_token = await self._token(
            flow="username_password_create",
            did=did,
            sdk_file=sdk_file,
            config=config,
            transport=transport,
        )
        oauth_token, oauth_so_token = await self._token(
            flow="oauth_create_account",
            did=did,
            sdk_file=sdk_file,
            config=config,
            transport=transport,
        )
        return SentinelData(
            sentinel_token=username_token,
            sentinel_oauth_token=oauth_token,
            sentinel_so_token=oauth_so_token,
            cookie_str=transport.cookie_header(),
            oai_did=did,
            sentinel_authorize_token=authorize_token,
            sentinel_authorize_so_token=authorize_so_token,
            sentinel_username_so_token=username_so_token,
        )
