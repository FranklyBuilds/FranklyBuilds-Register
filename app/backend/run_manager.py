from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import re
import subprocess
from contextlib import suppress
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
from time import monotonic
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4

from .ant_browser_client import AntBrowserClient
from .errors import (
    LocalProxyUnavailableError,
    MongoUnavailableError,
    ProxyCountryUnavailableError,
    RoxyWorkspaceMissingError,
    RunConflictError,
    RunNotFoundError,
)
from .browser_automation import mask_ip
from .browser_probe import DEFAULT_ARTIFACT_DIR
from .browser_worker import WindowsChildJob, worker_process_main
from .mongo_manager import MongoManager
from .probe_store import DEFAULT_PROXY_GROUP, LOCAL_PROXY_GROUP, MongoProbeStore
from .proxy_parser import ProxyEntry
from .resource_models import RunState, WorkerSnapshot
from .resource_service import MongoResourceStore
from .mailbox_client import MailboxClient, mailbox_source_for_document
from .protocol_registration import (
    AutoSentinelProvider,
    MailboxOtpProvider,
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationResult,
    ProtocolRegistrationService,
    SentinelProvider,
    StaticOtpProvider,
    StaticSentinelProvider,
)
from .run_log_store import (
    CorruptRunLogError,
    RunLogAppendInput,
    RunLogCreateInput,
    RunLogEntryInput,
    RunLogNotFoundError,
    RunLogStore,
)
from .roxy_client import (
    MANAGED_BROWSER_PREFIX,
    MANAGED_BROWSER_REMARK,
    RoxyApiError,
    RoxyClient,
    RoxyWorkspace,
)
from .run_store import (
    ACTIVE_RUN_STATUSES,
    MongoRunStore,
    MongoRunWorkerStore,
    TERMINAL_RUN_STATUSES,
)
from .settings_store import SettingsStore, StoredExecutionSettings


T = TypeVar("T")
ACTIVE_STATUSES = set(ACTIVE_RUN_STATUSES)
UNLIMITED_CLEANUP_GRACE_SECONDS = 30.0
ROXY_CIRCUIT_FAILURE_THRESHOLD = 5
ROXY_INFRASTRUCTURE_FAILURE_CODES = frozenset(
    {
        "roxy_api_failed",
        "roxy_api_unavailable",
        "roxy_workspace_not_ready",
        "roxy_browser_not_ready",
        "browser_cleanup_failed",
        "cdp_connection_failed",
    }
)
SAFE_ERROR_STAGES = frozenset(
    {
        "queued",
        "roxy_starting",
        "roxy_health",
        "roxy_workspace",
        "roxy_browser",
        "roxy_browser_cleanup",
        "proxy_check",
        "login",
        "email",
        "verification",
        "profile",
        "access_token",
        "two_factor",
        "password_setup",
        "sentinel",
        "auth_flow",
        "user_register",
        "email_otp_send",
        "email_otp_wait",
        "email_otp_validate",
        "create_account",
        "auth_session",
        "existing_login",
        "cleanup",
    }
)
PROTOCOL_MAX_ERROR_RETRIES = 2
PROTOCOL_RETRY_BACKOFF_SECONDS = (0.5, 1.0)
PROTOCOL_RETRYABLE_ERROR_CODES = frozenset(
    {
        "sentinel_provider_failed",
        "transport_error",
        "proxy_unavailable",
        "proxy_timeout",
        "proxy_connection_failed",
        "proxy_connect_error",
        "request_timeout",
        "connect_timeout",
        "read_timeout",
        "unexpected_timeout",
        "unexpected_timeouterror",
        "unexpected_sslerror",
        "unexpected_connectionerror",
        "unexpected_proxyerror",
        "http_429",
        "rate_limit_exceeded",
        "rate_limited",
        "too_many_requests",
        "cloudflare_challenge",
    }
)


def resolve_registration_proxy_group(group: str | None) -> str:
    """Resolve the launch form's empty selection to the Mongo proxy pool.

    The empty value is intentionally mapped to Mongo's default group so a
    freshly imported HTTP/SOCKS pool is usable without EasyProxy or Clash.
    The legacy local listener remains available only when its sentinel is sent
    explicitly by an API client or the UI compatibility option.
    """

    normalized = " ".join(str(group or "").split())
    return normalized or DEFAULT_PROXY_GROUP
SAFE_ERROR_OPERATIONS = frozenset(
    {
        "health",
        "workspace_list",
        "browser_list",
        "browser_create",
        "browser_open",
        "browser_connection_info",
        "browser_close",
        "browser_delete",
        "cdp_connect",
        "proxy_bridge_start",
    }
)
SAFE_ERROR_KINDS = frozenset(
    {
        "transport",
        "http",
        "api",
        "invalid_json",
        "invalid_structure",
        "contract",
    }
)
SAFE_PROFILE_DIAGNOSTICS = {
    "profileFormVariant": frozenset(
        {"numeric_age", "birthday", "already_configured", "unknown"}
    ),
    "profileLocatorStrategy": frozenset(
        {"strict_attributes", "semantic_labels", "account_home", "unresolved"}
    ),
    "profileSubmitVariant": frozenset(
        {
            "finish_creating_account",
            "continue",
            "structural_submit",
            "not_applicable",
            "unresolved",
        }
    ),
}


class _RunCancellationRequested(RuntimeError):
    """Internal signal used when an unlimited database wait is cancelled."""


class _RoxyCircuitOpened(RuntimeError):
    """Internal signal used after Roxy infrastructure failures trip the circuit."""


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _safe_worker_diagnostics(event: dict[str, Any]) -> dict[str, str | int]:
    diagnostics: dict[str, str | int] = {}
    stage = event.get("errorStage")
    if isinstance(stage, str) and stage in SAFE_ERROR_STAGES:
        diagnostics["errorStage"] = stage
    operation = event.get("errorOperation")
    if isinstance(operation, str) and operation in SAFE_ERROR_OPERATIONS:
        diagnostics["errorOperation"] = operation
    error_kind = event.get("errorKind")
    if isinstance(error_kind, str) and error_kind in SAFE_ERROR_KINDS:
        diagnostics["errorKind"] = error_kind
    numeric_fields = {
        "errorHttpStatus": (100, 599),
        "errorApiCode": (-(2**31), 2**31 - 1),
        "errorRetryCount": (0, 10_000),
        "errorElapsedMs": (0, 86_400_000),
    }
    for key, (minimum, maximum) in numeric_fields.items():
        safe_value = _bounded_int(
            event.get(key), minimum=minimum, maximum=maximum
        )
        if safe_value is not None:
            diagnostics[key] = safe_value
    for key, allowed in SAFE_PROFILE_DIAGNOSTICS.items():
        value = event.get(key)
        if isinstance(value, str) and value in allowed:
            diagnostics[key] = value
    return diagnostics


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class RunExecutionContext:
    state: RunState
    reserved: list[dict[str, Any]]
    concurrency: int
    cancel_event: asyncio.Event
    resources: MongoResourceStore
    database_call: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]
    record_result: Callable[[bool], Awaitable[None]]
    append_log: Callable[..., None]
    save_state: Callable[[], Awaitable[None]]
    kind: str = "mock"
    workspace_ids: tuple[int, ...] = ()
    settings_snapshot: StoredExecutionSettings | None = None
    probe_store: MongoProbeStore | None = None
    worker_store: MongoRunWorkerStore | None = None


@dataclass(slots=True)
class BrowserProcessHandle:
    process: Any
    cancel_event: Any
    worker_id: str
    sequence: int
    email_id: str
    email: str
    workspace_id: int
    lease_owner: str
    final_received: bool = False
    final_status: str | None = None
    final_code: str | None = None
    egress_ip: str | None = None


class RunExecutor(Protocol):
    async def execute(self, context: RunExecutionContext) -> bool:
        """Run work and return True when cancellation was requested."""


class MockRunExecutor:
    """Deterministic debug executor; it never starts a real browser."""

    async def execute(self, context: RunExecutionContext) -> bool:
        state = context.state
        success_ordinal = 0
        work: list[tuple[int, dict[str, Any], bool, bool | None]] = []
        for index, email in enumerate(context.reserved, start=1):
            should_fail = index % 4 == 0
            if not should_fail:
                success_ordinal += 1
            eligible = None if should_fail else success_ordinal % 2 == 1
            work.append((index, email, should_fail, eligible))

        queue: asyncio.Queue[tuple[int, dict[str, Any], bool, bool | None]] = (
            asyncio.Queue()
        )
        for item in work:
            queue.put_nowait(item)

        async def worker() -> None:
            while not context.cancel_event.is_set():
                try:
                    sequence, email, should_fail, eligible = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    context.append_log(
                        state.runId,
                        "info",
                        "email_started",
                        f"开始处理第 {sequence} 个邮箱",
                        email=email["email"],
                        sequence=sequence,
                        details=RunManager.details(state),
                    )
                    await asyncio.sleep(0.42 + ((sequence - 1) % 3) * 0.08)
                    if context.cancel_event.is_set():
                        await context.database_call(
                            lambda: context_release_email(context, email)
                        )
                        return
                    if should_fail:
                        await context.database_call(
                            lambda: context_release_email(context, email)
                        )
                        await context.record_result(False)
                        context.append_log(
                            state.runId,
                            "error",
                            "email_failed",
                            "Mock 确定性规则触发：注册失败，邮箱已释放",
                            email=email["email"],
                            sequence=sequence,
                            details={
                                **RunManager.details(state),
                                "reasonCode": "mock_deterministic_failure",
                            },
                        )
                    else:
                        await context.database_call(
                            lambda: context_complete_success(context, email, bool(eligible))
                        )
                        await context.record_result(True)
                        context.append_log(
                            state.runId,
                            "success",
                            "email_succeeded",
                            f"Mock 注册成功，优惠资格：{'有' if eligible else '无'}",
                            email=email["email"],
                            sequence=sequence,
                            details={
                                **RunManager.details(state),
                                "accountType": "free",
                                "promotionEligible": bool(eligible),
                            },
                        )
                finally:
                    queue.task_done()

        async def context_release_email(
            execution: RunExecutionContext, email: dict[str, Any]
        ) -> None:
            await execution.resources.release_email(
                email["_id"], execution.state.runId
            )

        async def context_complete_success(
            execution: RunExecutionContext,
            email: dict[str, Any],
            eligible: bool,
        ) -> None:
            await execution.resources.complete_mock_success(
                email, execution.state.runId, eligible
            )

        workers = [
            asyncio.create_task(worker(), name=f"mock-worker-{state.runId}-{index}")
            for index in range(min(context.concurrency, len(work)))
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        return context.cancel_event.is_set()


class BrowserProbeRunExecutor:
    """One fresh spawned process per email with parent-only aggregation/logging."""

    def __init__(
        self,
        *,
        process_context: Any | None = None,
        worker_target: Callable[..., None] = worker_process_main,
        job_factory: Callable[[], WindowsChildJob] = WindowsChildJob,
        roxy_factory: Callable[..., RoxyClient] = RoxyClient,
        artifact_root: Path = DEFAULT_ARTIFACT_DIR / "runs",
    ) -> None:
        self.process_context = process_context or multiprocessing.get_context("spawn")
        self.worker_target = worker_target
        self.job_factory = job_factory
        self.roxy_factory = roxy_factory
        self.artifact_root = Path(artifact_root)

    def _browser_client(
        self,
        settings: StoredExecutionSettings,
        *,
        timeout_seconds: float = 15,
    ) -> RoxyClient | AntBrowserClient:
        if settings.browserProvider == "ant" and self.roxy_factory is RoxyClient:
            return AntBrowserClient(
                settings.antApiPort,
                settings.antApiKey,
                timeout_seconds=timeout_seconds,
            )
        return self.roxy_factory(
            settings.roxyApiPort,
            settings.roxyApiKey,
            timeout_seconds=timeout_seconds,
        )

    async def execute(self, context: RunExecutionContext) -> bool:
        if (
            context.settings_snapshot is None
            or context.probe_store is None
            or context.worker_store is None
        ):
            raise RuntimeError("browser probe execution context is incomplete")

        settings = context.settings_snapshot
        probe_store = context.probe_store
        worker_store = context.worker_store
        event_queue = self.process_context.Queue()
        job = self.job_factory()
        pending = list(context.reserved)
        if not context.workspace_ids:
            raise RuntimeError("browser probe workspace snapshot is missing")
        workspace_id = context.workspace_ids[0]
        active: dict[str, BrowserProcessHandle] = {}
        next_controller_heartbeat = monotonic() + 15
        next_workspace_heartbeat = monotonic() + 20
        cancel_started: float | None = None
        timeout_reached = False
        circuit_opened = False
        consecutive_roxy_failures = 0
        deadline = (
            None
            if settings.taskTimeoutSeconds == 0
            else monotonic() + settings.taskTimeoutSeconds
        )

        async def save_active_workers() -> None:
            context.state.activeWorkers = len(active)
            await context.save_state()

        async def launch_one(email: dict[str, Any]) -> None:
            worker_id = str(email["_workerId"])
            sequence = int(email["_sequence"])
            lease_owner = f"run:{context.state.runId}:worker:{worker_id}"
            await context.database_call(
                lambda: worker_store.assign(
                    context.state.runId,
                    worker_id,
                    workspace_id=workspace_id,
                    lease_owner=lease_owner,
                )
            )
            cancel_event = self.process_context.Event()
            settings_payload = settings.model_dump(mode="json")
            settings_payload["roxyApiKey"] = settings.roxyApiKey.get_secret_value()
            config = {
                "runId": context.state.runId,
                "workerId": worker_id,
                "emailId": str(email["_id"]),
                "workspaceId": workspace_id,
                "leaseOwner": lease_owner,
                "settings": settings_payload,
                "mongoUri": context.resources.manager.uri,
                "mongoDatabase": context.resources.manager.database_name,
                "artifactDir": str(
                    self.artifact_root / context.state.runId / worker_id
                ),
                "registrationCountry": context.state.registrationCountry,
                "registrationProxyGroup": context.state.registrationProxyGroup,
            }
            process = self.process_context.Process(
                target=self.worker_target,
                args=(config, event_queue, cancel_event),
                name=f"browser-probe-{worker_id[:8]}",
                daemon=False,
            )
            try:
                process.start()
            except BaseException:
                await context.database_call(
                    lambda: worker_store.finish(
                        context.state.runId,
                        worker_id,
                        "failed",
                        error_code="worker_spawn_failed",
                    )
                )
                await context.database_call(
                    lambda: context.resources.release_email(
                        str(email["_id"]), context.state.runId
                    )
                )
                await context.record_result(False)
                context.append_log(
                    context.state.runId,
                    "error",
                    "worker_failed",
                    "浏览器 worker 进程启动失败",
                    email=str(email["email"]),
                    sequence=sequence,
                    details={
                        **RunManager.details(context.state),
                        "stage": "roxy_starting",
                        "reasonCode": "worker_spawn_failed",
                    },
                )
                return

            job.assign(int(process.pid))
            await context.database_call(
                lambda: worker_store.set_pid(
                    context.state.runId, worker_id, int(process.pid)
                )
            )
            active[worker_id] = BrowserProcessHandle(
                process=process,
                cancel_event=cancel_event,
                worker_id=worker_id,
                sequence=sequence,
                email_id=str(email["_id"]),
                email=str(email["email"]),
                workspace_id=workspace_id,
                lease_owner=lease_owner,
            )
            await save_active_workers()
            context.append_log(
                context.state.runId,
                "info",
                "worker_started",
                f"浏览器 worker {sequence} 已启动",
                email=str(email["email"]),
                sequence=sequence,
                details={
                    **RunManager.details(context.state),
                    "stage": "roxy_starting",
                },
            )

        async def record_event(event: dict[str, Any]) -> None:
            nonlocal cancel_started, circuit_opened, consecutive_roxy_failures
            worker_id = str(event.get("workerId") or "")
            handle = active.get(worker_id)
            if handle is None:
                return
            if event.get("type") == "stage":
                stage = str(event.get("stage") or "")
                if stage not in {
                    "roxy_starting",
                    "proxy_check",
                    "login",
                    "email",
                    "verification",
                    "profile",
                    "access_token",
                    "two_factor",
                    "password_setup",
                    "cleanup",
                }:
                    return
                egress_ip = (
                    str(event["egressIp"]) if event.get("egressIp") else None
                )
                if egress_ip is not None:
                    handle.egress_ip = egress_ip
                await context.database_call(
                    lambda: worker_store.stage(
                        context.state.runId,
                        worker_id,
                        stage,
                        egress_ip=egress_ip,
                        dir_id=(str(event["dirId"]) if event.get("dirId") else None),
                    )
                )
                details: dict[str, Any] = {
                    **RunManager.details(context.state),
                    "stage": stage,
                }
                if egress_ip is not None:
                    details["egressIpMasked"] = mask_ip(egress_ip)
                context.append_log(
                    context.state.runId,
                    "info",
                    "worker_stage_changed",
                    f"worker 进入阶段：{stage}",
                    email=handle.email,
                    sequence=handle.sequence,
                    details=details,
                )
                return

            if event.get("type") == "mailbox_poll":
                poll_details = event.get("details")
                if not isinstance(poll_details, dict):
                    poll_details = {}
                safe_details = {
                    key: poll_details[key]
                    for key in (
                        "attempt",
                        "flow",
                        "channel",
                        "status",
                        "errorCode",
                        "retryable",
                        "codePresent",
                        "codeLength",
                        "apiCode",
                        "apiSuccess",
                        "responseBody",
                        "receivedAtPresent",
                        "elapsedMs",
                    )
                    if key in poll_details
                }
                # A mailbox endpoint may return a complete HTML error page.
                # Preserve it in structured details, while keeping the visible
                # message inside RunLogEntryInput's 1,000-character limit.
                response_body = str(safe_details.get("responseBody") or "")
                response_preview = response_body
                if len(response_preview) > 500:
                    response_preview = response_preview[:500] + "…（完整内容见日志详情）"
                context.append_log(
                    context.state.runId,
                    "info" if safe_details.get("status") == "ok" else "warning",
                    "mailbox_poll_result",
                    (
                        f"邮箱取码调用 #{safe_details.get('attempt', '?')} "
                        f"[{safe_details.get('flow', 'unknown')}] "
                        f"[{safe_details.get('channel', 'unknown')}]："
                        f"{safe_details.get('status', 'unknown')}，"
                        f"apiCode={safe_details.get('apiCode', '-')}，"
                        f"发现验证码={bool(safe_details.get('codePresent', False))}，"
                        f"原始响应={response_preview}"
                    ),
                    email=handle.email,
                    sequence=handle.sequence,
                    details={
                        **RunManager.details(context.state),
                        "stage": "verification",
                        **safe_details,
                    },
                )
                return

            if event.get("type") == "verification_fill":
                fill_details = event.get("details")
                if not isinstance(fill_details, dict):
                    fill_details = {}
                safe_details = {
                    key: fill_details[key]
                    for key in (
                        "flow",
                        "status",
                        "pollCount",
                        "errorCode",
                        "nextStep",
                    )
                    if key in fill_details
                }
                status = str(safe_details.get("status") or "unknown")
                context.append_log(
                    context.state.runId,
                    "error" if status == "failed" else "info",
                    "verification_fill_result",
                    (
                        f"验证码填写 [{safe_details.get('flow', 'unknown')}]："
                        f"{status}"
                        + (
                            f"，errorCode={safe_details['errorCode']}"
                            if safe_details.get("errorCode")
                            else ""
                        )
                    ),
                    email=handle.email,
                    sequence=handle.sequence,
                    details={
                        **RunManager.details(context.state),
                        "stage": str(safe_details.get("flow") or "verification"),
                        **safe_details,
                    },
                )
                return

            if event.get("type") != "final" or handle.final_received:
                return
            status = str(event.get("status") or "failed")
            if status not in {"success", "partial_success", "failed", "cancelled"}:
                status = "failed"
            code = str(event.get("code") or "probe_failed")
            if circuit_opened and status not in {"success", "partial_success"}:
                status = "cancelled"
                code = "roxy_circuit_open"
            if event.get("egressIp"):
                handle.egress_ip = str(event["egressIp"])
            diagnostics = _safe_worker_diagnostics(event)
            handle.final_received = True
            handle.final_status = status
            handle.final_code = code
            await context.database_call(
                lambda: worker_store.finish(
                    context.state.runId,
                    worker_id,
                    status,
                    error_code=None if status == "success" else code,
                    egress_ip=handle.egress_ip,
                    error_stage=(
                        str(diagnostics["errorStage"])
                        if "errorStage" in diagnostics
                        else None
                    ),
                    error_operation=(
                        str(diagnostics["errorOperation"])
                        if "errorOperation" in diagnostics
                        else None
                    ),
                    error_kind=(
                        str(diagnostics["errorKind"])
                        if "errorKind" in diagnostics
                        else None
                    ),
                    error_http_status=(
                        int(diagnostics["errorHttpStatus"])
                        if "errorHttpStatus" in diagnostics
                        else None
                    ),
                    error_api_code=(
                        int(diagnostics["errorApiCode"])
                        if "errorApiCode" in diagnostics
                        else None
                    ),
                    error_retry_count=(
                        int(diagnostics["errorRetryCount"])
                        if "errorRetryCount" in diagnostics
                        else None
                    ),
                    error_elapsed_ms=(
                        int(diagnostics["errorElapsedMs"])
                        if "errorElapsedMs" in diagnostics
                        else None
                    ),
                )
            )
            if status != "cancelled":
                await context.record_result(status in {"success", "partial_success"})
            details = {
                **RunManager.details(context.state),
                "stage": status,
                "reasonCode": code,
            }
            details.update(diagnostics)
            if handle.egress_ip:
                details["egressIpMasked"] = mask_ip(handle.egress_ip)
            context.append_log(
                context.state.runId,
                (
                    "success"
                    if status == "success"
                    else "warning"
                    if status in {"partial_success", "cancelled"}
                    else "error"
                ),
                "worker_finished",
                f"浏览器 worker 结束：{status}",
                email=handle.email,
                sequence=handle.sequence,
                details=details,
            )

            if status == "cancelled":
                return
            if status == "failed" and (
                code in ROXY_INFRASTRUCTURE_FAILURE_CODES
                or code == "roxy_auth_failed"
            ):
                consecutive_roxy_failures += 1
            else:
                consecutive_roxy_failures = 0
            should_open_circuit = code == "roxy_auth_failed" or (
                consecutive_roxy_failures >= ROXY_CIRCUIT_FAILURE_THRESHOLD
            )
            if not should_open_circuit or circuit_opened:
                return

            circuit_opened = True
            context.state.terminalReasonCode = "roxy_circuit_open"
            cancel_started = monotonic()
            for active_handle in active.values():
                if not active_handle.final_received:
                    active_handle.cancel_event.set()
            await context.save_state()
            circuit_details: dict[str, Any] = {
                **RunManager.details(context.state),
                "reasonCode": "roxy_circuit_open",
                "triggerReasonCode": code,
                "consecutiveFailureCount": consecutive_roxy_failures,
            }
            circuit_details.update(diagnostics)
            context.append_log(
                context.state.runId,
                "error",
                "run_circuit_opened",
                "Roxy 连续异常，任务开始安全终止",
                details=circuit_details,
            )

        async def cleanup_handle(handle: BrowserProcessHandle) -> None:
            document: dict[str, Any] | None = None
            with suppress(Exception):
                document = await context.database_call(
                    lambda: worker_store.internal(
                        context.state.runId, handle.worker_id
                    )
                )
            dir_id = str(document.get("dirId")) if document and document.get("dirId") else None
            if dir_id:
                with suppress(Exception):
                    async with self._browser_client(settings) as roxy:
                        await roxy.close_browser(dir_id)
                        await roxy.delete_browser(handle.workspace_id, dir_id)
            with suppress(Exception):
                await context.database_call(
                    lambda: probe_store.release_proxy_owner(handle.lease_owner)
                )
            with suppress(Exception):
                await context.database_call(
                    lambda: context.resources.release_email(
                        handle.email_id, context.state.runId
                    )
                )

        try:
            while pending or active:
                while (
                    pending
                    and len(active) < context.concurrency
                    and cancel_started is None
                    and not timeout_reached
                    and not circuit_opened
                ):
                    email = pending.pop(0)
                    await launch_one(email)

                while True:
                    try:
                        event = event_queue.get_nowait()
                    except Empty:
                        break
                    if isinstance(event, dict):
                        await record_event(event)

                now = monotonic()
                if now >= next_controller_heartbeat:
                    renewed = await context.database_call(
                        lambda: probe_store.heartbeat_probe_lock(
                            f"run:{context.state.runId}", lease_seconds=90
                        )
                    )
                    if not renewed:
                        raise RuntimeError("browser probe controller lease was lost")
                    next_controller_heartbeat = now + 15

                if now >= next_workspace_heartbeat:
                    renewed = await context.database_call(
                        lambda: probe_store.heartbeat_workspace(
                            workspace_id,
                            f"run:{context.state.runId}",
                            lease_seconds=180,
                        )
                    )
                    if not renewed:
                        raise RuntimeError("browser probe workspace lease was lost")
                    next_workspace_heartbeat = now + 20

                if (
                    deadline is not None
                    and now >= deadline
                    and cancel_started is None
                ):
                    timeout_reached = True
                    cancel_started = now
                    for handle in active.values():
                        handle.cancel_event.set()

                if context.cancel_event.is_set() and cancel_started is None:
                    cancel_started = now
                    for handle in active.values():
                        handle.cancel_event.set()

                if cancel_started is not None and now - cancel_started >= 15:
                    for handle in active.values():
                        if handle.process.is_alive():
                            handle.process.terminate()
                    await asyncio.sleep(0.25)
                    for handle in active.values():
                        if handle.process.is_alive() and hasattr(handle.process, "kill"):
                            handle.process.kill()

                for worker_id, handle in list(active.items()):
                    if handle.process.is_alive():
                        continue
                    handle.process.join(timeout=0)
                    if not handle.final_received:
                        # multiprocessing.Queue feeder threads can publish the
                        # final event just after the process handle becomes dead.
                        await asyncio.sleep(0.05)
                        while True:
                            try:
                                late_event = event_queue.get_nowait()
                            except Empty:
                                break
                            if isinstance(late_event, dict):
                                await record_event(late_event)
                    if not handle.final_received:
                        status = "cancelled" if cancel_started is not None else "failed"
                        code = (
                            "roxy_circuit_open"
                            if circuit_opened
                            else "worker_cancelled"
                            if status == "cancelled"
                            else "worker_process_crashed"
                        )
                        await context.database_call(
                            lambda worker_id=worker_id, status=status, code=code: worker_store.finish(
                                context.state.runId,
                                worker_id,
                                status,
                                error_code=code,
                                egress_ip=handle.egress_ip,
                            )
                        )
                        if status == "failed":
                            await context.record_result(False)
                    await cleanup_handle(handle)
                    del active[worker_id]
                    await save_active_workers()

                if cancel_started is not None and not active:
                    break
                if pending or active:
                    await asyncio.sleep(0.1)

            if cancel_started is not None:
                cancellation_code = (
                    "roxy_circuit_open" if circuit_opened else "worker_cancelled"
                )
                for email in pending:
                    await context.database_call(
                        lambda email=email: worker_store.finish(
                            context.state.runId,
                            str(email["_workerId"]),
                            "cancelled",
                            error_code=cancellation_code,
                        )
                    )
                await context.database_call(
                    lambda: context.resources.release_run_reservations(
                        context.state.runId
                    )
                )
                if timeout_reached:
                    raise TimeoutError("browser probe run timed out")
                if circuit_opened:
                    raise _RoxyCircuitOpened("roxy_circuit_open")
                return True
            return False
        finally:
            for handle in active.values():
                handle.cancel_event.set()
                if handle.process.is_alive():
                    handle.process.terminate()
                handle.process.join(timeout=1)
                await cleanup_handle(handle)
            job.close()
            with suppress(Exception):
                event_queue.close()
            with suppress(Exception):
                event_queue.join_thread()


def _protocol_proxy_url(lease: Any) -> str:
    """Render a leased proxy with the shared, IPv6-aware formatter."""
    if lease is None:
        return ""
    return ProxyEntry(
        scheme=str(getattr(lease, "scheme", "http") or "http"),
        host=str(getattr(lease, "host", "") or ""),
        port=int(getattr(lease, "port", 0) or 0),
        username=str(getattr(lease, "username", "") or ""),
        password=str(getattr(lease, "password", "") or ""),
    ).direct_url


def _jwt_expiry(value: str) -> datetime | None:
    """Read an expiry from a JWT payload without retaining the token."""
    parts = str(value or "").split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        )
        if not isinstance(payload, Mapping):
            return None
        exp = float(payload.get("exp"))
        return datetime.fromtimestamp(exp, timezone.utc)
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeError,
        OverflowError,
        OSError,
    ):
        return None


def _protocol_access_token_expiry(
    access_token: str,
    metadata: Mapping[str, Any] | None,
) -> datetime | None:
    raw_expiry = None
    if isinstance(metadata, Mapping):
        raw_expiry = metadata.get("expires_at") or metadata.get("expiresAt")

    parsed: datetime | None = None
    if isinstance(raw_expiry, datetime):
        parsed = raw_expiry
    elif isinstance(raw_expiry, (int, float)) and not isinstance(raw_expiry, bool):
        try:
            timestamp = float(raw_expiry)
            parsed = datetime.fromtimestamp(
                timestamp / (1000 if timestamp > 10_000_000_000 else 1),
                timezone.utc,
            )
        except (ValueError, TypeError, OverflowError, OSError):
            parsed = None
    elif isinstance(raw_expiry, str) and raw_expiry.strip():
        try:
            parsed = datetime.fromisoformat(
                raw_expiry.strip().replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            parsed = None

    if parsed is not None:
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    return _jwt_expiry(access_token)


def _safe_protocol_error_code(value: Any) -> str:
    """Keep protocol diagnostics bounded and free of credential-bearing URLs."""
    raw = str(value or "protocol_registration_failed")
    raw = re.sub(r"(?i)(https?://)([^/@\s]+):([^/@\s]+)@", r"\1[redacted]@", raw)
    raw = re.sub(r"(?i)(access[_-]?token|refresh[_-]?token|cookie|sentinel)[=:][^,;\s]+", r"\1=[redacted]", raw)
    return raw[:160]


def _protocol_error_is_retryable(error_code: Any) -> bool:
    """Whitelist transient protocol failures; mailbox/account errors stay final."""
    normalized = str(error_code or "").strip().casefold()
    return (
        normalized in PROTOCOL_RETRYABLE_ERROR_CODES
        or normalized.endswith("_http_429")
    )


class ProtocolRegistrationRunExecutor:
    """Execute one direct HTTP registration attempt per reserved mailbox."""

    def __init__(
        self,
        *,
        service_factory: Callable[..., ProtocolRegistrationService] = ProtocolRegistrationService,
        mailbox_factory: Callable[[], MailboxClient] = MailboxClient,
        sentinel_provider: SentinelProvider | None = None,
    ) -> None:
        self.service_factory = service_factory
        self.mailbox_factory = mailbox_factory
        self.sentinel_provider = sentinel_provider or AutoSentinelProvider()

    async def execute(self, context: RunExecutionContext) -> bool:
        if context.probe_store is None or context.worker_store is None:
            raise RuntimeError("protocol registration execution context is incomplete")
        state = context.state
        raw_timeout = getattr(context.settings_snapshot, "taskTimeoutSeconds", 0)
        batch_timeout_seconds = max(0.0, float(raw_timeout or 0))
        semaphore = asyncio.Semaphore(max(1, context.concurrency))
        active_lock = asyncio.Lock()
        tasks: list[asyncio.Task[None]] = []

        async def database(operation: Callable[[], Awaitable[Any]]) -> Any:
            return await context.database_call(operation)

        async def cleanup_timed_out_run() -> None:
            context.cancel_event.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            # Per-attempt cleanup releases known leases in finally. Releasing
            # by owner as well covers a lease acquired immediately before task
            # cancellation, while run-level release covers queued mailboxes.
            for source in context.reserved:
                worker_id = str(source.get("_workerId") or "")
                if not worker_id:
                    continue
                owner = f"run:{state.runId}:protocol:{worker_id}"
                with suppress(Exception):
                    await database(
                        lambda owner=owner: context.probe_store.release_proxy_owner(owner)
                    )
            with suppress(Exception):
                await database(
                    lambda: context.resources.release_run_reservations(state.runId)
                )
            with suppress(Exception):
                await database(lambda: context.worker_store.interrupt_run(state.runId))
            state.activeWorkers = 0
            with suppress(Exception):
                await context.save_state()

        async def attempt(sequence: int, source: dict[str, Any]) -> None:
            worker_id = str(source.get("_workerId") or uuid4())
            owner = f"run:{state.runId}:protocol:{worker_id}"
            email = str(source.get("email") or "")

            async def mark_cancelled() -> None:
                await database(
                    lambda: context.worker_store.finish(
                        state.runId,
                        worker_id,
                        "cancelled",
                        error_code="run_cancelled",
                    )
                )
                await database(
                    lambda: context.resources.release_email(
                        str(source["_id"]), state.runId
                    )
                )

            # Tasks are created eagerly, so a cancellation gate is required
            # before waiting for a concurrency slot as well as after it.
            if context.cancel_event.is_set():
                await mark_cancelled()
                return

            async with semaphore:
                if context.cancel_event.is_set():
                    await mark_cancelled()
                    return

                lease = None
                finished = False
                active_marked = False
                retry_count = 0
                result: ProtocolRegistrationResult | None = None

                async def stage(name: str) -> None:
                    with suppress(Exception):
                        await database(
                            lambda: context.worker_store.stage(
                                state.runId, worker_id, name
                            )
                        )

                async def wait_for_retry(delay: float) -> bool:
                    """Wait between retries while allowing run cancellation."""
                    sleep_task = asyncio.create_task(
                        asyncio.sleep(max(0.0, delay)),
                        name=f"protocol-retry-delay-{sequence}",
                    )
                    cancel_task = asyncio.create_task(
                        context.cancel_event.wait(),
                        name=f"protocol-retry-cancel-{sequence}",
                    )
                    try:
                        done, _ = await asyncio.wait(
                            {sleep_task, cancel_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        return cancel_task in done
                    finally:
                        for task in (sleep_task, cancel_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(
                            sleep_task, cancel_task, return_exceptions=True
                        )

                async def register_once(
                    current_lease: Any,
                ) -> ProtocolRegistrationResult | None:
                    """Run one isolated registration using the current proxy."""
                    proxy_url = _protocol_proxy_url(current_lease)
                    metadata = (
                        source.get("protocolMetadata")
                        if isinstance(source.get("protocolMetadata"), dict)
                        else {}
                    )
                    request_metadata = {
                        **metadata,
                        "mailboxKind": source.get("mailboxKind"),
                        "mailboxPassword": source.get("mailboxPassword"),
                    }
                    request = ProtocolRegistrationRequest(
                        email=email,
                        # MailboxClient expects the synthetic IMAP URI for
                        # mail.com resources; ordinary URL-backed mailboxes
                        # continue to use their stored access URL.
                        email_access_url=mailbox_source_for_document(source),
                        registration_country=state.registrationCountry or "ZZ",
                        proxy=proxy_url,
                        source_email_id=str(source.get("_id") or ""),
                        sentinel_data=request_metadata.get("sentinel_data"),
                        otp_code=request_metadata.get("otp_code"),
                        metadata={
                            **request_metadata,
                            "proxy_id": str(getattr(current_lease, "id", "")),
                        },
                    )
                    static_sentinel = request.sentinel_data
                    sentinel_provider = (
                        StaticSentinelProvider(static_sentinel)
                        if static_sentinel
                        else self.sentinel_provider
                    )
                    static_otp = request.otp_code
                    otp_provider = (
                        StaticOtpProvider(default=str(static_otp))
                        if static_otp
                        else MailboxOtpProvider(self.mailbox_factory())
                    )

                    async def progress(name: str, details: Mapping[str, Any]) -> None:
                        await stage(name if name in SAFE_ERROR_STAGES else "cleanup")
                        context.append_log(
                            state.runId,
                            "info",
                            "protocol_stage",
                            f"协议注册阶段：{name}",
                            email=email,
                            sequence=sequence,
                            details={
                                **RunManager.details(state),
                                "stage": name,
                                "errorRetryCount": retry_count,
                            },
                        )

                    service = self.service_factory(
                        config=ProtocolRegistrationConfig(
                            # Match the existing registration setting: the
                            # baseline defaults to passwordless, while an
                            # operator who requires passwords gets the legacy
                            # user/register branch.
                            registration_mode=(
                                "password"
                                if context.settings_snapshot is not None
                                and context.settings_snapshot.requireRegistrationPassword
                                else "passwordless"
                            ),
                            enable_registration_totp=bool(
                                getattr(
                                    context.settings_snapshot,
                                    "enableRegistrationTotp",
                                    False,
                                )
                            ),
                            timeout_seconds=30,
                            otp_timeout_seconds=300,
                            auth_session_delay_seconds=0.5,
                        ),
                        sentinel_provider=sentinel_provider,
                        otp_provider=otp_provider,
                        progress=progress,
                    )
                    if context.cancel_event.is_set():
                        return None

                    registration_task = asyncio.create_task(
                        service.register(request),
                        name=f"protocol-register-request-{sequence}",
                    )
                    cancellation_task = asyncio.create_task(
                        context.cancel_event.wait(),
                        name=f"protocol-register-cancel-{sequence}",
                    )
                    try:
                        done, _ = await asyncio.wait(
                            {registration_task, cancellation_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if registration_task not in done:
                            registration_task.cancel()
                            await asyncio.gather(
                                registration_task, return_exceptions=True
                            )
                            return None
                        if registration_task.cancelled():
                            return None
                        # A completed result wins a same-tick cancellation race.
                        value = registration_task.result()
                        if not isinstance(value, ProtocolRegistrationResult):
                            raise TypeError(
                                "protocol registration service returned an invalid result"
                            )
                        return value
                    finally:
                        for task in (registration_task, cancellation_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(
                            registration_task,
                            cancellation_task,
                            return_exceptions=True,
                        )

                try:
                    await database(
                        lambda: context.worker_store.assign(
                            state.runId,
                            worker_id,
                            workspace_id=None,
                            lease_owner=owner,
                        )
                    )
                    if context.cancel_event.is_set():
                        finished = True
                        await mark_cancelled()
                        return
                    async with active_lock:
                        state.activeWorkers += 1
                        active_marked = True
                        await context.save_state()

                    for _try_index in range(PROTOCOL_MAX_ERROR_RETRIES + 1):
                        if context.cancel_event.is_set():
                            finished = True
                            await mark_cancelled()
                            return
                        await stage("proxy_check")
                        lease = await database(
                            lambda: context.probe_store.acquire_proxy(
                                owner,
                                country=state.registrationCountry,
                                group=state.registrationProxyGroup,
                                lease_seconds=180,
                            )
                        )
                        try:
                            if context.cancel_event.is_set():
                                finished = True
                                await mark_cancelled()
                                return
                            if lease is None:
                                result = ProtocolRegistrationResult(
                                    success=False,
                                    email=email,
                                    stage="proxy_check",
                                    error="proxy_unavailable",
                                )
                            else:
                                try:
                                    result = await register_once(lease)
                                except (
                                    OSError,
                                    TimeoutError,
                                    asyncio.TimeoutError,
                                    ConnectionError,
                                ):
                                    result = ProtocolRegistrationResult(
                                        success=False,
                                        email=email,
                                        stage="transport",
                                        error="transport_error",
                                    )
                            if result is None:
                                finished = True
                                await mark_cancelled()
                                return
                        finally:
                            if lease is not None:
                                with suppress(Exception):
                                    await database(
                                        lambda: context.probe_store.release_proxy(
                                            lease.id, owner
                                        )
                                    )
                                lease = None

                        if result.success:
                            break
                        error_code = _safe_protocol_error_code(result.error)
                        error_stage = str(result.stage or "cleanup")[:64]
                        await stage(error_stage if error_stage in SAFE_ERROR_STAGES else "cleanup")
                        if (
                            not _protocol_error_is_retryable(error_code)
                            or retry_count >= PROTOCOL_MAX_ERROR_RETRIES
                        ):
                            break
                        retry_count += 1
                        context.append_log(
                            state.runId,
                            "warning",
                            "protocol_retry",
                            "协议注册遇到瞬态错误，将更换代理重试",
                            email=email,
                            sequence=sequence,
                            details={
                                **RunManager.details(state),
                                "stage": error_stage,
                                "reasonCode": error_code,
                                "errorRetryCount": retry_count,
                            },
                        )
                        delay = PROTOCOL_RETRY_BACKOFF_SECONDS[
                            min(retry_count - 1, len(PROTOCOL_RETRY_BACKOFF_SECONDS) - 1)
                        ]
                        if await wait_for_retry(delay):
                            finished = True
                            await mark_cancelled()
                            return

                    if result is None:
                        raise RuntimeError("protocol registration result missing")
                    if not result.success:
                        await database(
                            lambda: context.resources.release_email(
                                str(source["_id"]), state.runId
                            )
                        )
                        await database(
                            lambda: context.worker_store.finish(
                                state.runId,
                                worker_id,
                                "failed",
                                error_code=_safe_protocol_error_code(result.error),
                                error_stage=str(result.stage or "cleanup")[:64],
                                error_retry_count=retry_count,
                            )
                        )
                        context.append_log(
                            state.runId,
                            "error",
                            "email_failed",
                            "协议注册失败，邮箱已释放",
                            email=email,
                            sequence=sequence,
                            details={
                                **RunManager.details(state),
                                "stage": result.stage,
                                "reasonCode": _safe_protocol_error_code(result.error),
                                "errorRetryCount": retry_count,
                            },
                        )
                        await context.record_result(False)
                        finished = True
                        return

                    access_token = str(result.access_token or "").strip()
                    expires_at = _protocol_access_token_expiry(
                        access_token,
                        result.metadata,
                    )
                    token_error = ""
                    if not access_token:
                        token_error = "access_token_missing"
                    elif expires_at is None:
                        token_error = "access_token_expiry_missing"
                    elif expires_at <= now_utc():
                        token_error = "access_token_expired"
                    if token_error:
                        await database(
                            lambda: context.resources.release_email(
                                str(source["_id"]), state.runId
                            )
                        )
                        await database(
                            lambda: context.worker_store.finish(
                                state.runId,
                                worker_id,
                                "failed",
                                error_code=token_error,
                                error_stage="access_token",
                                error_retry_count=retry_count,
                            )
                        )
                        context.append_log(
                            state.runId,
                            "error",
                            "email_failed",
                            "协议注册未返回有效 Access Token，邮箱已释放",
                            email=email,
                            sequence=sequence,
                            details={
                                **RunManager.details(state),
                                "stage": "access_token",
                                "reasonCode": token_error,
                                "errorRetryCount": retry_count,
                            },
                        )
                        await context.record_result(False)
                        finished = True
                        return

                    totp_error = str(getattr(result, "totp_error", "") or "")
                    totp_secret = str(getattr(result, "totp_secret", "") or "")
                    totp_activated_at = getattr(result, "totp_activated_at", None)
                    persistence_values: dict[str, Any] = {
                        "access_token": access_token,
                        "access_token_expires_at": expires_at,
                        "chatgpt_password": result.password,
                        "totp_secret": totp_secret,
                        "totp_activated_at": totp_activated_at,
                        "totp_error": totp_error,
                        "registration_country": state.registrationCountry,
                        "registration_proxy_group": state.registrationProxyGroup,
                    }
                    # These fields were added after the original resource
                    # contract. Omit empty values so injected/legacy stores
                    # keep receiving the original keyword set.
                    cookie_header = str(getattr(result, "cookie_header", "") or "").strip()
                    auth_session = getattr(result, "auth_session", None)
                    refresh_token = str(getattr(result, "refresh_token", "") or "").strip()
                    device_id = str(getattr(result, "device_id", "") or "").strip()
                    if cookie_header:
                        persistence_values["cookie_header"] = cookie_header
                    if isinstance(auth_session, dict) and auth_session:
                        persistence_values["auth_session"] = dict(auth_session)
                    if refresh_token:
                        persistence_values["refresh_token"] = refresh_token
                    if device_id:
                        persistence_values["device_id"] = device_id
                    await database(
                        lambda: context.resources.complete_protocol_registration_success(
                            source,
                            state.runId,
                            **persistence_values,
                        )
                    )
                    worker_status = "partial_success" if totp_error else "success"
                    await database(
                        lambda: context.worker_store.finish(
                            state.runId,
                            worker_id,
                            worker_status,
                            egress_ip=None,
                            error_code=(
                                _safe_protocol_error_code(totp_error)
                                if totp_error
                                else None
                            ),
                            error_stage="two_factor" if totp_error else None,
                            error_retry_count=retry_count,
                        )
                    )
                    context.append_log(
                        state.runId,
                        "warning" if totp_error else "success",
                        "email_succeeded",
                        (
                            "协议注册完成，但 2FA 设置失败；账号和 Access Token 已入库"
                            if totp_error
                            else "协议注册成功，账号和 Access Token 已入库"
                        ),
                        email=email,
                        sequence=sequence,
                        details={
                            **RunManager.details(state),
                            "stage": "two_factor" if totp_error else "success",
                            "accountType": "free",
                            "errorRetryCount": retry_count,
                            **(
                                {"twoFactorError": _safe_protocol_error_code(totp_error)}
                                if totp_error
                                else {}
                            ),
                        },
                    )
                    await context.record_result(True)
                    finished = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not finished:
                        with suppress(Exception):
                            await database(lambda: context.resources.release_email(str(source["_id"]), state.runId))
                        with suppress(Exception):
                            await database(lambda: context.worker_store.finish(state.runId, worker_id, "failed", error_code=type(exc).__name__, error_stage="cleanup", error_retry_count=retry_count))
                        context.append_log(
                            state.runId,
                            "error",
                            "email_failed",
                            "协议注册执行异常，邮箱已释放",
                            email=email,
                            sequence=sequence,
                            details={**RunManager.details(state), "stage": "cleanup", "reasonCode": type(exc).__name__, "errorRetryCount": retry_count},
                        )
                        await context.record_result(False)
                finally:
                    if active_marked:
                        with suppress(Exception):
                            async with active_lock:
                                state.activeWorkers = max(0, state.activeWorkers - 1)
                                await context.save_state()
                    if lease is not None:
                        with suppress(Exception):
                            await database(lambda: context.probe_store.release_proxy(lease.id, owner))

        for sequence, source in enumerate(context.reserved, start=1):
            tasks.append(asyncio.create_task(attempt(sequence, source), name=f"protocol-registration-{sequence}"))
        if tasks:
            try:
                if batch_timeout_seconds > 0:
                    _, pending = await asyncio.wait(
                        tasks,
                        timeout=batch_timeout_seconds,
                        return_when=asyncio.ALL_COMPLETED,
                    )
                    if pending:
                        await cleanup_timed_out_run()
                        raise TimeoutError("protocol registration run timed out")
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        return context.cancel_event.is_set()


class RunManager:
    def __init__(
        self,
        mongo: MongoManager,
        resources: MongoResourceStore,
        runs: MongoRunStore,
        logs: RunLogStore,
        settings: SettingsStore,
        executor: RunExecutor | None = None,
        browser_executor: RunExecutor | None = None,
        protocol_executor: RunExecutor | None = None,
        probe_store: MongoProbeStore | None = None,
        worker_store: MongoRunWorkerStore | None = None,
        roxy_factory: Callable[..., RoxyClient] = RoxyClient,
    ) -> None:
        self.mongo = mongo
        self.resources = resources
        self.runs = runs
        self.logs = logs
        self.settings = settings
        self.executor = executor or MockRunExecutor()
        self.browser_executor = browser_executor or BrowserProbeRunExecutor(
            roxy_factory=roxy_factory
        )
        self.protocol_executor = protocol_executor or ProtocolRegistrationRunExecutor()
        self.probe_store = probe_store or MongoProbeStore(mongo)
        self.worker_store = worker_store or MongoRunWorkerStore(mongo)
        self.roxy_factory = roxy_factory
        self.states: dict[str, RunState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._start_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._workspace_snapshots: dict[str, tuple[int, ...]] = {}
        self._settings_snapshots: dict[str, StoredExecutionSettings] = {}

    async def recover(self) -> None:
        await self.resources.ensure_indexes()
        await self.runs.ensure_indexes()
        await self.probe_store.ensure_indexes()
        await self.worker_store.ensure_indexes()
        await self.probe_store.clear_expired_probe_leases()
        await self.probe_store.clear_expired_worker_leases()
        await self.probe_store.clear_expired_locks()

        # A terminal state may have been reached while MongoDB was unavailable.
        for state in self.states.values():
            if state.status in TERMINAL_RUN_STATUSES:
                try:
                    await self.runs.save(state)
                except RunNotFoundError:
                    pass

        local_active = {
            run_id for run_id, task in self._tasks.items() if not task.done()
        }
        stale_states = await self.runs.active_states(local_active)
        for state in stale_states:
            if state.kind == "browser_probe":
                await self._recover_browser_workers(state.runId)
            elif state.kind == "protocol_registration":
                await self._recover_protocol_workers(state.runId)
            consumed, released = await self.resources.reconcile_run_reservations(
                state.runId
            )
            state.status = "interrupted"
            state.finishedAt = now_utc()
            state.cancelRequested = False
            await self.runs.save(state)
            self.states[state.runId] = state
            self._append_recovery_log(state, consumed, released)

        await self.resources.release_orphaned_reservations(sorted(local_active))
        self.logs.prune_terminal_runs()

    async def start(self, count: int) -> RunState:
        async with self._start_lock:
            if any(
                state.status in ACTIVE_STATUSES for state in self.states.values()
            ):
                raise RunConflictError("已有任务正在运行")
            self.mongo.require_online()
            if await self.runs.active() is not None:
                raise RunConflictError("已有任务正在运行")

            settings = self.settings.load()
            run_id = str(uuid4())
            now = now_utc()
            state = RunState(
                runId=run_id,
                kind="mock",
                status="queued",
                requested=count,
                pending=count,
                processed=0,
                succeeded=0,
                failed=0,
                workerCount=0,
                activeWorkers=0,
                startedAt=now,
                updatedAt=now,
                finishedAt=None,
                logPersisted=True,
                cancelRequested=False,
            )
            await self.runs.create(state)
            self.states[run_id] = state

            try:
                self.logs.create_run(
                    UUID(run_id),
                    RunLogCreateInput(
                        requestedCount=count,
                        concurrency=settings.concurrency,
                    ),
                )
                reserved = await self.resources.reserve_emails(count, run_id)
                await self.runs.set_reserved(
                    run_id, [str(item["_id"]) for item in reserved]
                )
            except Exception:
                if self.mongo.online:
                    await self.resources.release_run_reservations(run_id)
                state.status = "failed"
                state.finishedAt = now_utc()
                if self.mongo.online:
                    await self.runs.save(state)
                try:
                    self._append(
                        run_id,
                        "error",
                        "run_failed",
                        "任务启动失败：可用邮箱不足或数据库不可用",
                        details=self.details(state),
                    )
                except (OSError, RunLogNotFoundError, CorruptRunLogError):
                    state.logPersisted = False
                raise

            state.status = "running"
            await self.runs.save(state)
            self._append(
                run_id,
                "info",
                "run_started",
                f"Mock 任务开始，共 {count} 个邮箱，并发 {settings.concurrency}",
                details=self.details(state),
            )
            cancel_event = asyncio.Event()
            self._cancel_events[run_id] = cancel_event
            task = asyncio.create_task(
                self._execute(
                    state,
                    reserved,
                    settings.concurrency,
                    settings.taskTimeoutSeconds,
                    cancel_event,
                ),
                name=f"mock-run-{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._task_finished(run_id))
            return state.model_copy(deep=True)

    async def start_browser_probe(
        self,
        count: int,
        country: str = "JP",
        group: str = "",
        email_source: str = "all",
    ) -> RunState:
        async with self._start_lock:
            if any(
                state.status in ACTIVE_STATUSES for state in self.states.values()
            ):
                raise RunConflictError("已有任务正在运行")
            self.mongo.require_online()
            if await self.runs.active() is not None:
                raise RunConflictError("已有任务正在运行")

            settings = self.settings.load()
            effective_group = resolve_registration_proxy_group(group)
            if effective_group == LOCAL_PROXY_GROUP:
                await self._preflight_local_proxy()
            available_proxies = await self.probe_store.count_eligible_proxies(
                country, effective_group
            )
            if available_proxies < 1:
                raise ProxyCountryUnavailableError(
                    country,
                    1,
                    available_proxies,
                )
            required = min(count, settings.concurrency)
            workspaces = await self._preflight_workspaces(settings)
            if not workspaces:
                raise RoxyWorkspaceMissingError()
            workspace_id = workspaces[0].id
            workspace_ids = (workspace_id,)

            run_id = str(uuid4())
            controller_owner = f"run:{run_id}"
            await self.probe_store.clear_expired_probe_leases()
            await self.probe_store.clear_expired_locks()
            if not await self.probe_store.acquire_probe_lock(
                controller_owner, lease_seconds=90
            ):
                raise RunConflictError("已有真实浏览器探测控制器正在运行")
            try:
                workspace_acquired = await self.probe_store.acquire_workspace(
                    workspace_id,
                    controller_owner,
                    lease_seconds=180,
                )
            except BaseException:
                with suppress(Exception):
                    await self.probe_store.release_probe_lock(controller_owner)
                raise
            if not workspace_acquired:
                with suppress(Exception):
                    await self.probe_store.release_probe_lock(controller_owner)
                raise RunConflictError("Roxy workspace 正被其他真实探测任务使用")

            now = now_utc()
            state = RunState(
                runId=run_id,
                kind="browser_probe",
                status="queued",
                requested=count,
                pending=count,
                processed=0,
                succeeded=0,
                failed=0,
                workerCount=required,
                activeWorkers=0,
                startedAt=now,
                updatedAt=now,
                finishedAt=None,
                logPersisted=True,
                cancelRequested=False,
                registrationCountry=country,
                registrationProxyGroup=effective_group,
                emailSource=email_source,
            )
            created = False
            try:
                await self.runs.create(state)
                created = True
                self.states[run_id] = state
                self.logs.create_run(
                    UUID(run_id),
                    RunLogCreateInput(
                        requestedCount=count,
                        concurrency=settings.concurrency,
                    ),
                )
                reserved = await self.resources.reserve_emails(
                    count,
                    run_id,
                    email_source,
                )
                await self.runs.set_reserved(
                    run_id, [str(item["_id"]) for item in reserved]
                )
                worker_documents: list[dict[str, Any]] = []
                for sequence, email in enumerate(reserved, start=1):
                    worker_id = str(uuid4())
                    email["_workerId"] = worker_id
                    email["_sequence"] = sequence
                    worker_documents.append(
                        {
                            "workerId": worker_id,
                            "sequence": sequence,
                            "email": str(email["email"]),
                            "emailId": str(email["_id"]),
                        }
                    )
                await self.worker_store.create_many(run_id, worker_documents)
            except BaseException:
                with suppress(Exception):
                    await self.resources.release_run_reservations(run_id)
                with suppress(Exception):
                    await self.probe_store.release_workspace_owner(controller_owner)
                with suppress(Exception):
                    await self.probe_store.release_probe_lock(controller_owner)
                if created:
                    state.status = "failed"
                    state.finishedAt = now_utc()
                    with suppress(Exception):
                        await self.runs.save(state)
                raise

            state.status = "running"
            await self.runs.save(state)
            self._workspace_snapshots[run_id] = workspace_ids
            self._settings_snapshots[run_id] = settings.model_copy(deep=True)
            self._append(
                run_id,
                "info",
                "run_started",
                f"真实浏览器探测开始，共 {count} 个邮箱，并发 {required}，注册国家 {country}，代理分组 {effective_group}，邮箱来源 {email_source}",
                details=self.details(state),
            )
            cancel_event = asyncio.Event()
            self._cancel_events[run_id] = cancel_event
            task = asyncio.create_task(
                self._execute(
                    state,
                    reserved,
                    settings.concurrency,
                    settings.taskTimeoutSeconds,
                    cancel_event,
                ),
                name=f"browser-probe-run-{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._task_finished(run_id))
            return state.model_copy(deep=True)

    async def start_protocol_registration(
        self,
        count: int,
        country: str = "JP",
        group: str = "",
        email_source: str = "all",
    ) -> RunState:
        """Start the direct HTTP protocol registration batch."""
        country = str(country or "").strip().upper()
        if len(country) != 2 or country == "ZZ":
            raise ValueError("country must be a classified two-letter code")
        email_source = str(email_source or "all").strip() or "all"
        async with self._start_lock:
            if any(state.status in ACTIVE_STATUSES for state in self.states.values()):
                raise RunConflictError("已有任务正在运行")
            self.mongo.require_online()
            if await self.runs.active() is not None:
                raise RunConflictError("已有任务正在运行")
            settings = self.settings.load()
            effective_group = resolve_registration_proxy_group(group)
            if effective_group == LOCAL_PROXY_GROUP:
                await self._preflight_local_proxy()
            available_proxies = await self.probe_store.count_eligible_proxies(country, effective_group)
            if available_proxies < 1:
                raise ProxyCountryUnavailableError(country, 1, available_proxies)
            required = min(count, settings.concurrency)
            run_id = str(uuid4())
            now = now_utc()
            state = RunState(
                runId=run_id,
                kind="protocol_registration",
                status="queued",
                requested=count,
                pending=count,
                processed=0,
                succeeded=0,
                failed=0,
                workerCount=required,
                activeWorkers=0,
                startedAt=now,
                updatedAt=now,
                finishedAt=None,
                logPersisted=True,
                cancelRequested=False,
                registrationCountry=country,
                registrationProxyGroup=effective_group,
                emailSource=email_source,
            )
            created = False
            try:
                await self.runs.create(state)
                created = True
                self.states[run_id] = state
                self.logs.create_run(
                    UUID(run_id),
                    RunLogCreateInput(requestedCount=count, concurrency=settings.concurrency),
                )
                reserved = await self.resources.reserve_emails(count, run_id, email_source)
                await self.runs.set_reserved(run_id, [str(item["_id"]) for item in reserved])
                worker_documents: list[dict[str, Any]] = []
                for sequence, email in enumerate(reserved, start=1):
                    worker_id = str(uuid4())
                    email["_workerId"] = worker_id
                    email["_sequence"] = sequence
                    worker_documents.append(
                        {
                            "workerId": worker_id,
                            "sequence": sequence,
                            "email": str(email["email"]),
                            "emailId": str(email["_id"]),
                        }
                    )
                await self.worker_store.create_many(run_id, worker_documents)
            except BaseException:
                with suppress(Exception):
                    await self.resources.release_run_reservations(run_id)
                if created:
                    state.status = "failed"
                    state.finishedAt = now_utc()
                    with suppress(Exception):
                        await self.runs.save(state)
                raise
            state.status = "running"
            await self.runs.save(state)
            self._settings_snapshots[run_id] = settings.model_copy(deep=True)
            self._append(
                run_id,
                "info",
                "run_started",
                f"协议注册开始，共 {count} 个邮箱，并发 {required}，注册国家 {country}，代理分组 {effective_group}，邮箱来源 {email_source}",
                details=self.details(state),
            )
            cancel_event = asyncio.Event()
            self._cancel_events[run_id] = cancel_event
            task = asyncio.create_task(
                self._execute(state, reserved, settings.concurrency, settings.taskTimeoutSeconds, cancel_event),
                name=f"protocol-registration-run-{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._task_finished(run_id))
            return state.model_copy(deep=True)

    @staticmethod
    async def _preflight_local_proxy() -> None:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 7890), timeout=3
            )
            writer.write(
                b"CONNECT www.gstatic.com:443 HTTP/1.1\r\n"
                b"Host: www.gstatic.com:443\r\n"
                b"Proxy-Connection: close\r\n\r\n"
            )
            await asyncio.wait_for(writer.drain(), timeout=2)
            status_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not status_line.startswith(b"HTTP/1.1 200") and not status_line.startswith(
                b"HTTP/1.0 200"
            ):
                status = status_line.decode("ascii", errors="replace").strip()
                raise LocalProxyUnavailableError(status or "代理未返回响应")
        except LocalProxyUnavailableError:
            raise
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            raise LocalProxyUnavailableError(type(exc).__name__) from exc
        finally:
            if writer is not None:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()

    async def workers(self, run_id: str) -> list[WorkerSnapshot]:
        await self.get(run_id)
        return await self.worker_store.list(run_id)

    async def active(self) -> RunState | None:
        for state in reversed(list(self.states.values())):
            if state.status in ACTIVE_STATUSES:
                return state.model_copy(deep=True)
        return await self.runs.active()

    async def get(self, run_id: str) -> RunState:
        current = self.states.get(run_id)
        if current is not None:
            return current.model_copy(deep=True)
        return await self.runs.get(run_id)

    async def cancel(self, run_id: str) -> RunState:
        persisted = await self.runs.request_cancel(run_id)
        current = self.states.get(run_id)
        if current is None or current.status not in ACTIVE_STATUSES:
            return persisted
        current.cancelRequested = True
        current.updatedAt = persisted.updatedAt
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        return current.model_copy(deep=True)

    async def shutdown(self) -> None:
        for run_id, task in list(self._tasks.items()):
            state = self.states.get(run_id)
            if state and state.kind in {"browser_probe", "protocol_registration"}:
                event = self._cancel_events.get(run_id)
                if event is not None:
                    event.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15)
                except TimeoutError:
                    task.cancel()
            else:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if state and (
                state.status in ACTIVE_STATUSES
                or (state.kind == "browser_probe" and state.status == "cancelled")
                or (state.kind == "protocol_registration" and state.status == "cancelled")
            ):
                if self.mongo.online:
                    await self.resources.reconcile_run_reservations(run_id)
                    if state.kind == "browser_probe":
                        await self.worker_store.interrupt_run(run_id)
                        await self.probe_store.release_workspace_owner(
                            f"run:{run_id}"
                        )
                        await self.probe_store.release_probe_lock(f"run:{run_id}")
                    elif state.kind == "protocol_registration":
                        await self._recover_protocol_workers(run_id)
                state.status = "interrupted"
                state.activeWorkers = 0
                state.finishedAt = now_utc()
                state.cancelRequested = False
                if self.mongo.online:
                    await self.runs.save(state)
                self._append(
                    run_id,
                    "warning",
                    "run_interrupted",
                    "FastAPI 服务关闭，任务已中断",
                    details=self.details(state),
                )
        self.logs.prune_terminal_runs()

    async def _execute(
        self,
        state: RunState,
        reserved: list[dict[str, Any]],
        concurrency: int,
        timeout_seconds: int,
        cancel_event: asyncio.Event,
    ) -> None:
        # A value of zero removes the batch deadline. Positive browser and
        # protocol runs retain a database cleanup grace period after their
        # executors stop new work at the configured timeout.
        deadline = (
            None
            if timeout_seconds == 0
            else monotonic()
            + timeout_seconds
            + (
                UNLIMITED_CLEANUP_GRACE_SECONDS
                if state.kind in {"browser_probe", "protocol_registration"}
                else 0
            )
        )

        async def database_call(operation: Callable[[], Awaitable[Any]]) -> Any:
            return await self._database_call(
                state,
                deadline,
                operation,
                cancel_event=cancel_event,
            )

        async def record_result(succeeded: bool) -> None:
            async with self._state_lock:
                state.processed += 1
                state.pending = max(0, state.requested - state.processed)
                if succeeded:
                    state.succeeded += 1
                else:
                    state.failed += 1
            await database_call(lambda: self.runs.save(state))

        async def save_state() -> None:
            await database_call(lambda: self.runs.save(state))

        context = RunExecutionContext(
            state=state,
            reserved=reserved,
            concurrency=concurrency,
            cancel_event=cancel_event,
            resources=self.resources,
            database_call=database_call,
            record_result=record_result,
            append_log=self._append,
            save_state=save_state,
            kind=state.kind,
            workspace_ids=self._workspace_snapshots.get(state.runId, ()),
            settings_snapshot=self._settings_snapshots.get(state.runId),
            probe_store=self.probe_store,
            worker_store=self.worker_store,
        )
        try:
            selected_executor = (
                self.browser_executor
                if state.kind == "browser_probe"
                else self.protocol_executor
                if state.kind == "protocol_registration"
                else self.executor
            )
            cancelled = False
            try:
                cancelled = await selected_executor.execute(context)
                if not cancelled:
                    state.status = "completed"
                    state.finishedAt = now_utc()
                    await database_call(lambda: self.runs.save(state))
                    self._append(
                        state.runId,
                        "warning" if state.failed else "success",
                        "run_completed",
                        (
                            "真实浏览器探测完成：成功 {0}，失败 {1}".format(state.succeeded, state.failed)
                            if state.kind == "browser_probe"
                            else "协议注册完成：成功 {0}，失败 {1}".format(state.succeeded, state.failed)
                            if state.kind == "protocol_registration"
                            else f"Mock 任务完成：成功 {state.succeeded}，失败 {state.failed}"
                        ),
                        details=self.details(state),
                    )
            except _RunCancellationRequested:
                cancelled = True

            if cancelled:
                cleanup_deadline = monotonic() + UNLIMITED_CLEANUP_GRACE_SECONDS
                await self._database_call(
                    state,
                    cleanup_deadline,
                    lambda: self.resources.release_run_reservations(state.runId),
                )
                if state.kind == "protocol_registration" and self.mongo.online:
                    with suppress(Exception):
                        await self.worker_store.interrupt_run(state.runId)
                state.status = "cancelled"
                state.cancelRequested = False
                state.finishedAt = now_utc()
                await self._database_call(
                    state, cleanup_deadline, lambda: self.runs.save(state)
                )
                self._append(
                    state.runId,
                    "warning",
                    "run_cancelled",
                    "任务已取消，未处理邮箱已释放",
                    details=self.details(state),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.mongo.online:
                if state.kind == "protocol_registration":
                    with suppress(Exception):
                        await self._recover_protocol_workers(state.runId)
                await self.resources.reconcile_run_reservations(state.runId)
            circuit_opened = isinstance(exc, _RoxyCircuitOpened)
            state.status = "failed"
            if circuit_opened:
                state.terminalReasonCode = "roxy_circuit_open"
            state.finishedAt = now_utc()
            if self.mongo.online:
                await self.runs.save(state)
            self._append(
                state.runId,
                "error",
                "run_failed",
                (
                    "Roxy 连续异常，任务已安全终止；未处理邮箱已释放"
                    if circuit_opened
                    else
                    f"真实浏览器探测异常终止：{type(exc).__name__}"
                    if state.kind == "browser_probe"
                    else f"协议注册异常终止：{type(exc).__name__}"
                    if state.kind == "protocol_registration"
                    else f"Mock 任务异常终止：{type(exc).__name__}"
                ),
                details={
                    **self.details(state),
                    "reasonCode": (
                        "roxy_circuit_open"
                        if circuit_opened
                        else
                        "browser_probe_run_error"
                        if state.kind == "browser_probe"
                        else
                        "protocol_registration_run_error"
                        if state.kind == "protocol_registration"
                        else "mock_run_error"
                    ),
                },
            )
        finally:
            if state.kind == "browser_probe" and self.mongo.online:
                with suppress(Exception):
                    await self.probe_store.release_workspace_owner(
                        f"run:{state.runId}"
                    )
                with suppress(Exception):
                    await self.probe_store.release_probe_lock(f"run:{state.runId}")
            self._workspace_snapshots.pop(state.runId, None)
            self._settings_snapshots.pop(state.runId, None)
            self.logs.prune_terminal_runs()

    async def _preflight_workspaces(
        self,
        settings: StoredExecutionSettings,
    ) -> list[RoxyWorkspace]:
        started = monotonic()
        stable_count = 0
        stable_signature: tuple[int, ...] | None = None
        latest: list[RoxyWorkspace] = []
        launched = False
        last_error: RoxyApiError | None = None
        while monotonic() - started < 30:
            try:
                async with self._browser_client(
                    settings,
                    timeout_seconds=3,
                ) as roxy:
                    await roxy.health()
                    latest = await roxy.workspaces(timeout_seconds=3)
                signature = tuple(item.id for item in latest)
                if signature == stable_signature:
                    stable_count += 1
                else:
                    stable_signature = signature
                    stable_count = 1
                if stable_count >= 3:
                    async with self._browser_client(
                        settings,
                        timeout_seconds=5,
                    ) as cleanup_roxy:
                        await self._cleanup_stale_managed_browsers(
                            cleanup_roxy,
                            latest,
                        )
                    return latest
            except RoxyApiError as exc:
                if exc.is_auth_failure:
                    raise
                last_error = exc
                stable_count = 0
                stable_signature = None
                if not launched:
                    launched = True
                    executable = Path(
                        settings.antBrowserExecutablePath
                        if settings.browserProvider == "ant"
                        else settings.browserExecutablePath
                    )
                    if executable.is_file():
                        popen_options: dict[str, Any] = {
                            "cwd": str(executable.parent),
                            "close_fds": True,
                            "stdin": subprocess.DEVNULL,
                            "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.DEVNULL,
                        }
                        if os.name == "nt":
                            popen_options["creationflags"] = getattr(
                                subprocess, "CREATE_NO_WINDOW", 0
                            )
                        with suppress(OSError):
                            subprocess.Popen([str(executable)], **popen_options)
            await asyncio.sleep(1)
        raise RoxyApiError(
            "Roxy health 与 workspace 未在 30 秒内达到连续稳定状态",
            operation=(last_error.operation if last_error else "workspace_list"),
            elapsed_ms=max(0, int((monotonic() - started) * 1000)),
            error_kind=(last_error.error_kind if last_error else "transport"),
        )

    def _browser_client(
        self,
        settings: StoredExecutionSettings,
        *,
        timeout_seconds: float = 15,
    ) -> RoxyClient | AntBrowserClient:
        if settings.browserProvider == "ant" and self.roxy_factory is RoxyClient:
            return AntBrowserClient(
                settings.antApiPort,
                settings.antApiKey,
                timeout_seconds=timeout_seconds,
            )
        return self.roxy_factory(
            settings.roxyApiPort,
            settings.roxyApiKey,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    async def _cleanup_stale_managed_browsers(
        roxy: RoxyClient,
        workspaces: list[RoxyWorkspace],
    ) -> None:
        # Roxy can finish opening a window after its profile has already been
        # deleted.  Such a window is absent from /browser/list but remains in
        # /browser/connection_info and blocks later opens.  Close those ghost
        # connections before cleaning the ordinary stale profiles.
        connections = await roxy.connection_info(timeout_seconds=5)
        ghost_ids = {
            connection.dir_id
            for connection in connections
            if connection.window_name.startswith(MANAGED_BROWSER_PREFIX)
            and connection.window_remark == MANAGED_BROWSER_REMARK
        }
        for dir_id in sorted(ghost_ids):
            await roxy.close_browser(dir_id)

        for workspace in workspaces:
            browsers = await roxy.browsers(workspace.id, timeout_seconds=5)
            stale = [
                browser
                for browser in browsers
                if browser.window_name.startswith(MANAGED_BROWSER_PREFIX)
                and browser.window_remark == MANAGED_BROWSER_REMARK
            ]
            for browser in stale:
                with suppress(RoxyApiError):
                    await roxy.close_browser(browser.dir_id)
                await roxy.delete_browser(workspace.id, browser.dir_id)

        # Roxy close is asynchronous.  Confirm that no managed ghost remains;
        # retrying here also absorbs brief local-API transport interruptions.
        lingering_ids = set(ghost_ids)
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.5)
            connections = await roxy.connection_info(timeout_seconds=5)
            lingering_ids = {
                connection.dir_id
                for connection in connections
                if connection.window_name.startswith(MANAGED_BROWSER_PREFIX)
                and connection.window_remark == MANAGED_BROWSER_REMARK
            }
            if not lingering_ids:
                return
            for dir_id in sorted(lingering_ids):
                await roxy.close_browser(dir_id)
        raise RoxyApiError(
            "Roxy 托管临时窗口关闭后仍保持连接",
            operation="browser_close",
            error_kind="contract",
        )

    async def _recover_browser_workers(self, run_id: str) -> None:
        workers = await self.worker_store.active_internal(run_id)
        try:
            settings = self.settings.load()
        except Exception:
            settings = None
        for worker in workers:
            workspace_id = worker.get("workspaceId")
            lease_owner = worker.get("leaseOwner")
            dir_id = worker.get("dirId")
            if settings is not None and workspace_id is not None and dir_id:
                with suppress(Exception):
                    async with self._browser_client(settings) as roxy:
                        await roxy.close_browser(str(dir_id))
                        await roxy.delete_browser(int(workspace_id), str(dir_id))
            if lease_owner:
                with suppress(Exception):
                    await self.probe_store.release_proxy_owner(str(lease_owner))
        await self.worker_store.interrupt_run(run_id)
        await self.probe_store.release_workspace_owner(f"run:{run_id}")
        await self.probe_store.release_probe_lock(f"run:{run_id}")

    async def _recover_protocol_workers(self, run_id: str) -> None:
        """Release proxy leases left by a protocol run that outlived us."""
        workers = await self.worker_store.active_internal(run_id)
        for worker in workers:
            lease_owner = worker.get("leaseOwner")
            if lease_owner:
                with suppress(Exception):
                    await self.probe_store.release_proxy_owner(str(lease_owner))
        await self.worker_store.interrupt_run(run_id)

    async def _database_call(
        self,
        state: RunState,
        deadline: float | None,
        operation: Callable[[], Awaitable[T]],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> T:
        waiting_logged = False
        resume_status = state.status
        while deadline is None or monotonic() < deadline:
            try:
                if waiting_logged and self.mongo.online:
                    state.status = resume_status
                result = await operation()
                if waiting_logged:
                    self._append(
                        state.runId,
                        "info",
                        "database_reconnected",
                        "MongoDB 已恢复，任务继续执行",
                        details=self.details(state),
                    )
                return result
            except MongoUnavailableError:
                if cancel_event is not None and cancel_event.is_set():
                    raise _RunCancellationRequested from None
                state.status = "waiting_for_database"
                if not waiting_logged:
                    waiting_logged = True
                    self._append(
                        state.runId,
                        "warning",
                        "database_waiting",
                        "MongoDB 不可用，任务暂停并等待自动重连",
                        details=self.details(state),
                    )
                remaining = (
                    2.0
                    if deadline is None
                    else max(0.0, deadline - monotonic())
                )
                if not await self.mongo.wait_until_online(min(2.0, remaining)):
                    continue
        raise TimeoutError("等待 MongoDB 恢复超时")

    def _append_recovery_log(
        self, state: RunState, consumed: int, released: int
    ) -> None:
        try:
            self._append(
                state.runId,
                "warning",
                "run_interrupted",
                "FastAPI 已恢复遗留任务并释放未完成邮箱",
                details={
                    **self.details(state),
                    "consumedRecovered": consumed,
                    "releasedRecovered": released,
                },
            )
        except RunLogNotFoundError:
            settings = self.settings.load()
            self.logs.create_run(
                UUID(state.runId),
                RunLogCreateInput(
                    requestedCount=max(1, state.requested),
                    concurrency=settings.concurrency,
                ),
            )
            self._append_recovery_log(state, consumed, released)
        except (CorruptRunLogError, OSError):
            state.logPersisted = False

    def _append(
        self,
        run_id: str,
        level: str,
        event: str,
        message: str,
        *,
        email: str | None = None,
        sequence: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.logs.append_entries(
            UUID(run_id),
            RunLogAppendInput(
                entries=[
                    RunLogEntryInput(
                        timestamp=now_utc(),
                        level=level,
                        event=event,
                        message=message,
                        email=email,
                        sequence=sequence,
                        details=details or {},
                    )
                ]
            ),
        )

    def _task_finished(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)

    @staticmethod
    def details(state: RunState) -> dict[str, int]:
        return {
            "requested": state.requested,
            "pending": state.pending,
            "processed": state.processed,
            "succeeded": state.succeeded,
            "failed": state.failed,
        }


# Backward-compatible import name inside this worktree only; public HTTP routes
# remain unchanged while the implementation is now persistent.
MockRunManager = RunManager
