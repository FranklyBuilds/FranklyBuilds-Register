from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.protocol_registration import (
    ProtocolRegistrationRequest,
    ProtocolRegistrationResult,
)
from backend.resource_models import RunState
from backend.run_manager import (
    ProtocolRegistrationRunExecutor,
    RunExecutionContext,
    RunManager,
    _protocol_error_is_retryable,
    _protocol_proxy_url,
)


class _WorkerStore:
    def __init__(self, sources: list[dict[str, object]]) -> None:
        self.documents = {
            str(source["_workerId"]): {"status": "queued", "stage": "queued"}
            for source in sources
        }
        self.interrupt_calls = 0

    async def assign(self, _run_id: str, worker_id: str, **values: object) -> None:
        self.documents[worker_id].update(
            status="running",
            leaseOwner=values.get("lease_owner"),
        )

    async def stage(self, _run_id: str, worker_id: str, stage: str) -> None:
        self.documents[worker_id]["stage"] = stage

    async def finish(
        self, _run_id: str, worker_id: str, status: str, **values: object
    ) -> None:
        self.documents[worker_id].update(status=status, stage=status, **values)

    async def active_internal(self, _run_id: str) -> list[dict[str, object]]:
        terminal = {"success", "failed", "cancelled"}
        return [
            {"workerId": worker_id, **document}
            for worker_id, document in self.documents.items()
            if document["status"] not in terminal
        ]

    async def interrupt_run(self, _run_id: str) -> int:
        self.interrupt_calls += 1
        interrupted = 0
        for document in self.documents.values():
            if document["status"] in {"success", "failed", "cancelled"}:
                continue
            document.update(
                status="failed",
                stage="failed",
                error_code="worker_interrupted",
            )
            interrupted += 1
        return interrupted


class _ProbeStore:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[tuple[str, str]] = []
        self.released_owners: list[str] = []

    async def acquire_proxy(self, owner: str, **_values: object) -> SimpleNamespace:
        self.acquired.append(owner)
        return SimpleNamespace(
            id=f"proxy-{len(self.acquired)}",
            scheme="http",
            host="127.0.0.1",
            port=8080,
            username="",
            password="",
        )

    async def release_proxy(self, proxy_id: str, owner: str) -> None:
        self.released.append((proxy_id, owner))

    async def release_proxy_owner(self, owner: str) -> int:
        self.released_owners.append(owner)
        return 1


class _Resources:
    def __init__(self) -> None:
        self.released: list[str] = []
        self.completed: list[str] = []
        self.tokens: list[tuple[str, str]] = []
        self.reserved_ids: set[str] = set()
        self.run_releases: list[str] = []
        self.reconciled: list[str] = []

    async def release_email(self, email_id: str, _run_id: str) -> None:
        self.released.append(email_id)
        self.reserved_ids.discard(email_id)

    async def release_run_reservations(self, run_id: str) -> None:
        self.run_releases.append(run_id)
        self.released.extend(sorted(self.reserved_ids))
        self.reserved_ids.clear()

    async def reconcile_run_reservations(self, run_id: str) -> tuple[int, int]:
        self.reconciled.append(run_id)
        await self.release_run_reservations(run_id)
        return 0, 0

    async def complete_protocol_registration_success(
        self,
        source: dict[str, object],
        _run_id: str,
        *,
        access_token: str,
        access_token_expires_at: datetime,
        **_values: object,
    ) -> SimpleNamespace:
        _ = access_token_expires_at
        email_id = str(source["_id"])
        self.reserved_ids.discard(email_id)
        self.completed.append(email_id)
        self.tokens.append((f"account-{email_id}", access_token))
        return SimpleNamespace(id=f"account-{email_id}")


def _context(
    *, count: int, concurrency: int = 1, timeout_seconds: float = 0
) -> tuple[RunExecutionContext, _Resources, _ProbeStore, _WorkerStore]:
    now = datetime.now(timezone.utc)
    sources: list[dict[str, object]] = [
        {
            "_id": f"email-{sequence}",
            "_workerId": f"worker-{sequence}",
            "_sequence": sequence,
            "email": f"worker-{sequence}@example.com",
            "accessUrl": f"https://mail.example/{sequence}",
        }
        for sequence in range(1, count + 1)
    ]
    state = RunState(
        runId="22222222-2222-4222-8222-222222222222",
        kind="protocol_registration",
        status="running",
        requested=count,
        pending=count,
        processed=0,
        succeeded=0,
        failed=0,
        workerCount=min(count, concurrency),
        activeWorkers=0,
        startedAt=now,
        updatedAt=now,
        registrationCountry="JP",
        registrationProxyGroup="default",
    )
    resources = _Resources()
    resources.reserved_ids = {str(source["_id"]) for source in sources}
    probe_store = _ProbeStore()
    worker_store = _WorkerStore(sources)

    async def database_call(operation):
        return await operation()

    async def record_result(succeeded: bool) -> None:
        state.processed += 1
        state.pending = max(0, state.requested - state.processed)
        state.succeeded += int(succeeded)
        state.failed += int(not succeeded)

    context = RunExecutionContext(
        state=state,
        reserved=sources,
        concurrency=concurrency,
        cancel_event=asyncio.Event(),
        resources=resources,  # type: ignore[arg-type]
        database_call=database_call,
        record_result=record_result,
        append_log=lambda *_args, **_kwargs: None,
        save_state=lambda: asyncio.sleep(0),
        kind="protocol_registration",
        settings_snapshot=SimpleNamespace(
            requireRegistrationPassword=False,
            taskTimeoutSeconds=timeout_seconds,
        ),  # type: ignore[arg-type]
        probe_store=probe_store,  # type: ignore[arg-type]
        worker_store=worker_store,  # type: ignore[arg-type]
    )
    return context, resources, probe_store, worker_store


def test_protocol_proxy_url_formats_ipv6_authorities() -> None:
    assert _protocol_proxy_url(
        SimpleNamespace(
            scheme="http",
            host="2001:db8::1",
            port=8080,
            username="",
            password="",
        )
    ) == "http://[2001:db8::1]:8080"
    assert _protocol_proxy_url(
        SimpleNamespace(
            scheme="socks5h",
            host="2001:db8::2",
            port=1080,
            username="user@example.com",
            password="p:a/ss",
        )
    ) == "socks5h://user%40example.com:p%3Aa%2Fss@[2001:db8::2]:1080"


@pytest.mark.parametrize(
    "error_code",
    [
        "http_429",
        "user_register_http_429",
        "rate_limit_exceeded",
        "cloudflare_challenge",
    ],
)
def test_protocol_retry_classifier_accepts_transient_http_failures(
    error_code: str,
) -> None:
    assert _protocol_error_is_retryable(error_code) is True


@pytest.mark.parametrize(
    "error_code",
    ["email_otp_timeout", "user_register_http_400", "user_register_http_403"],
)
def test_protocol_retry_classifier_keeps_deterministic_failures_final(
    error_code: str,
) -> None:
    assert _protocol_error_is_retryable(error_code) is False


def test_protocol_executor_pre_cancel_never_starts_registration() -> None:
    calls: list[str] = []

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            calls.append(request.email)
            raise AssertionError("pre-cancelled registration started")

    context, resources, probe_store, worker_store = _context(count=2)
    context.cancel_event.set()

    cancelled = asyncio.run(
        ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
    )

    assert cancelled is True
    assert calls == []
    assert probe_store.acquired == []
    assert resources.released == ["email-1", "email-2"]
    assert {item["status"] for item in worker_store.documents.values()} == {
        "cancelled"
    }


@pytest.mark.parametrize("timeout_seconds", [0, 1])
def test_protocol_executor_cancels_in_flight_and_skips_queued_registration(
    timeout_seconds: int,
) -> None:
    async def scenario() -> tuple[
        RunExecutionContext, _Resources, _ProbeStore, _WorkerStore, list[str], bool
    ]:
        started = asyncio.Event()
        cleaned_up = asyncio.Event()
        calls: list[str] = []

        class Service:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def register(
                self, request: ProtocolRegistrationRequest
            ) -> ProtocolRegistrationResult:
                calls.append(request.email)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned_up.set()

        context, resources, probe_store, worker_store = _context(
            count=2,
            timeout_seconds=timeout_seconds,
        )
        execution = asyncio.create_task(
            ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        context.cancel_event.set()
        cancelled = await asyncio.wait_for(execution, timeout=1)
        return (
            context,
            resources,
            probe_store,
            worker_store,
            calls,
            cancelled and cleaned_up.is_set(),
        )

    context, resources, probe_store, worker_store, calls, stopped_cleanly = asyncio.run(
        scenario()
    )

    assert stopped_cleanly is True
    assert calls == ["worker-1@example.com"]
    assert len(probe_store.acquired) == 1
    assert len(probe_store.released) == 1
    assert resources.released == ["email-1", "email-2"]
    assert {item["status"] for item in worker_store.documents.values()} == {
        "cancelled"
    }
    assert context.state.activeWorkers == 0
    assert context.state.processed == 0


def test_protocol_executor_persists_success_that_wins_cancel_race() -> None:
    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            context.cancel_event.set()
            return ProtocolRegistrationResult(
                success=True,
                email=request.email,
                access_token="completed-access-token",
                metadata={"expires_at": "2999-01-01T00:00:00Z"},
            )

    context, resources, _probe_store, worker_store = _context(count=1)

    cancelled = asyncio.run(
        ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
    )

    assert cancelled is True
    assert resources.released == []
    assert resources.completed == ["email-1"]
    assert resources.tokens == [
        ("account-email-1", "completed-access-token")
    ]
    assert worker_store.documents["worker-1"]["status"] == "success"
    assert context.state.succeeded == 1


def test_protocol_executor_retries_transient_failure_with_a_new_proxy() -> None:
    seen_proxy_ids: list[str] = []

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            seen_proxy_ids.append(str(request.metadata.get("proxy_id")))
            if len(seen_proxy_ids) == 1:
                return ProtocolRegistrationResult(
                    success=False,
                    email=request.email,
                    stage="sentinel",
                    error="sentinel_provider_failed",
                )
            return ProtocolRegistrationResult(
                success=True,
                email=request.email,
                access_token="retried-access-token",
                metadata={"expires_at": "2999-01-01T00:00:00Z"},
            )

    context, resources, probe_store, worker_store = _context(count=1)

    asyncio.run(
        ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
    )

    assert seen_proxy_ids == ["proxy-1", "proxy-2"]
    assert len(probe_store.acquired) == 2
    assert probe_store.released == [
        ("proxy-1", f"run:{context.state.runId}:protocol:worker-1"),
        ("proxy-2", f"run:{context.state.runId}:protocol:worker-1"),
    ]
    assert resources.released == []
    assert resources.completed == ["email-1"]
    assert worker_store.documents["worker-1"]["status"] == "success"
    assert worker_store.documents["worker-1"]["error_retry_count"] == 1
    assert context.state.succeeded == 1


def test_protocol_executor_does_not_retry_deterministic_protocol_failure() -> None:
    calls = 0

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            nonlocal calls
            calls += 1
            return ProtocolRegistrationResult(
                success=False,
                email=request.email,
                stage="email_otp_wait",
                error="email_otp_timeout",
            )

    context, resources, probe_store, worker_store = _context(count=1)

    asyncio.run(
        ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
    )

    assert calls == 1
    assert len(probe_store.acquired) == 1
    assert len(probe_store.released) == 1
    assert resources.released == ["email-1"]
    assert worker_store.documents["worker-1"]["status"] == "failed"
    assert worker_store.documents["worker-1"]["error_retry_count"] == 0
    assert context.state.failed == 1


def test_protocol_executor_records_terminal_retry_count_after_transient_failures() -> None:
    calls = 0

    class Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def register(
            self, request: ProtocolRegistrationRequest
        ) -> ProtocolRegistrationResult:
            nonlocal calls
            calls += 1
            return ProtocolRegistrationResult(
                success=False,
                email=request.email,
                stage="auth_flow",
                error="transport_error",
            )

    context, resources, probe_store, worker_store = _context(count=1)

    asyncio.run(
        ProtocolRegistrationRunExecutor(service_factory=Service).execute(context)
    )

    assert calls == 3
    assert len(probe_store.acquired) == 3
    assert len(probe_store.released) == 3
    assert resources.released == ["email-1"]
    assert worker_store.documents["worker-1"]["status"] == "failed"
    assert worker_store.documents["worker-1"]["error_code"] == "transport_error"
    assert worker_store.documents["worker-1"]["error_retry_count"] == 2
    assert context.state.failed == 1


def test_protocol_executor_timeout_cancels_workers_and_releases_resources() -> None:
    async def scenario() -> tuple[
        RunExecutionContext, _Resources, _ProbeStore, _WorkerStore, bool
    ]:
        started = asyncio.Event()
        stopped = asyncio.Event()

        class Service:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def register(
                self, _request: ProtocolRegistrationRequest
            ) -> ProtocolRegistrationResult:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        context, resources, probe_store, worker_store = _context(
            count=2,
            concurrency=1,
            timeout_seconds=0.05,
        )
        with pytest.raises(
            TimeoutError,
            match="^protocol registration run timed out$",
        ) as raised:
            await asyncio.wait_for(
                ProtocolRegistrationRunExecutor(service_factory=Service).execute(
                    context
                ),
                timeout=1,
            )
        assert str(raised.value) == "protocol registration run timed out"
        assert started.is_set()
        return context, resources, probe_store, worker_store, stopped.is_set()

    context, resources, probe_store, worker_store, stopped_cleanly = asyncio.run(
        scenario()
    )

    assert stopped_cleanly is True
    assert context.cancel_event.is_set()
    assert context.state.activeWorkers == 0
    assert resources.released == ["email-1", "email-2"]
    assert resources.run_releases == [context.state.runId]
    assert len(probe_store.released) == 1
    assert set(probe_store.released_owners) == {
        f"run:{context.state.runId}:protocol:worker-1",
        f"run:{context.state.runId}:protocol:worker-2",
    }
    assert worker_store.interrupt_calls == 1
    assert {item["status"] for item in worker_store.documents.values()} == {"failed"}


def test_protocol_timeout_ends_run_failed_instead_of_cancelled() -> None:
    async def scenario() -> tuple[
        RunState, asyncio.Event, _Resources, _ProbeStore, _WorkerStore, list[str]
    ]:
        stopped = asyncio.Event()

        class Service:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def register(
                self, _request: ProtocolRegistrationRequest
            ) -> ProtocolRegistrationResult:
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        context, resources, probe_store, worker_store = _context(
            count=1,
            timeout_seconds=0.05,
        )
        saved_statuses: list[str] = []

        class Runs:
            async def save(self, state: RunState) -> None:
                saved_statuses.append(state.status)

        events: list[str] = []
        manager = object.__new__(RunManager)
        manager.mongo = SimpleNamespace(online=True)
        manager.resources = resources
        manager.runs = Runs()
        manager.protocol_executor = ProtocolRegistrationRunExecutor(
            service_factory=Service
        )
        manager.probe_store = probe_store
        manager.worker_store = worker_store
        manager._state_lock = asyncio.Lock()
        manager._workspace_snapshots = {}
        manager._settings_snapshots = {
            context.state.runId: context.settings_snapshot,
        }
        manager.logs = SimpleNamespace(prune_terminal_runs=lambda: None)
        manager._append = lambda _run_id, _level, event, message, **_values: (
            events.append(f"{event}:{message}")
        )

        await asyncio.wait_for(
            manager._execute(
                context.state,
                context.reserved,
                context.concurrency,
                0.05,  # type: ignore[arg-type]
                context.cancel_event,
            ),
            timeout=1,
        )
        assert saved_statuses[-1] == "failed"
        assert stopped.is_set()
        return (
            context.state,
            context.cancel_event,
            resources,
            probe_store,
            worker_store,
            events,
        )

    state, cancel_event, resources, probe_store, worker_store, events = asyncio.run(
        scenario()
    )

    assert state.status == "failed"
    assert state.cancelRequested is False
    assert cancel_event.is_set()
    assert resources.released == ["email-1"]
    assert resources.reconciled == [state.runId]
    assert probe_store.released
    assert worker_store.interrupt_calls >= 1
    assert any(
        event == "run_failed:协议注册异常终止：TimeoutError" for event in events
    )
    assert not any(event.startswith("run_cancelled:") for event in events)
