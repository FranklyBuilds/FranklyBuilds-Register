from __future__ import annotations

"""Durable Outlook task state and the main-service Outlook execution bridge.

The task is deliberately separate from the GPT run store. It can validate
credential-bearing accounts already present in MongoDB and, when configured,
run the existing browser registration engine with Mongo-backed result and
proxy adapters.
"""

import asyncio
import copy
import inspect
import re
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from .resource_service import MongoResourceStore, utc_now


TASK_ID = "outlook-register"
ACTIVE_STATUSES = {"running", "stopping"}
STOP_TIMEOUT_SECONDS = 5
def _redact_outlook_log(value: Any) -> str:
    """Keep task logs useful without persisting account/proxy credentials."""
    text = str(value or "")
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)(?<![\w])([A-Za-z0-9_.-]+):([^@\s:]+)@((?:[A-Za-z0-9_.-]+|\[[^]]+\]):\d+)", r"[redacted]@\3", text)
    text = re.sub(r"(?i)\b(refresh[_-]?token|access[_-]?token|password|secret|client[_-]?id|authorization|cookie)\b\s*([=:])\s*[^\s,;]+", r"\1\2[redacted]", text)
    return text[:2000]


def _iso(value: Any = None) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    try:
        from outlook_register.services.config_store import default_config, normalize_config

        config = normalize_config(default_config())
    except Exception:
        config = {
            "email_suffix": "@outlook.com",
            "headless": False,
            "bot_protection_wait": 15,
            "max_captcha_retries": 3,
            "captcha_strategy": 0,
            "concurrent_flows": 1,
            "tasks": 1,
            "success_tasks": None,
            "batch_success_limit": 300,
            "proxy": {"mode": "single", "type": "http", "max_per_proxy": 20},
            "oauth2": {"enable_oauth2": True, "redirect_url": "https://localhost", "Scopes": []},
            "temp_mail": {"enabled": False, "base_url": "", "domain": "", "code_timeout": 120, "poll_interval": 3},
        }
    # This setting belongs to the main-service task, not the legacy JSON
    # runner.  Keep old config files valid while making the dispatch policy
    # explicit in Mongo and in the console.
    config["execution_mode"] = str(config.get("execution_mode") or "auto").strip().lower()
    return config


def _normalize_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = copy.deepcopy(dict(value or {}))
    try:
        from outlook_register.services.config_store import normalize_config

        config = normalize_config(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    execution_mode = str(config.get("execution_mode") or "auto").strip().lower()
    if execution_mode not in {"auto", "registration", "authorized", "both"}:
        raise ValueError("execution_mode 只能是 auto / registration / authorized / both")
    config["execution_mode"] = execution_mode
    proxy = config.setdefault("proxy", {})
    proxy["source"] = str(proxy.get("source") or "mongo").strip().lower()
    if proxy["source"] not in {"mongo", "legacy"}:
        raise ValueError("proxy.source 只能是 mongo 或 legacy")
    proxy["group"] = " ".join(str(proxy.get("group") or "").split())[:64]
    return config


def _public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return config metadata without secrets or credential-bearing fields."""
    result = copy.deepcopy(dict(config))
    oauth = result.get("oauth2") if isinstance(result.get("oauth2"), dict) else {}
    oauth.pop("client_id", None)
    oauth.pop("clientId", None)
    oauth["clientIdConfigured"] = bool(config.get("oauth2", {}).get("client_id")) if isinstance(config.get("oauth2"), dict) else False
    if "Scopes" in oauth:
        oauth["scopes"] = oauth.pop("Scopes")
    result["oauth2"] = oauth
    temp_mail = result.get("temp_mail") if isinstance(result.get("temp_mail"), dict) else {}
    temp_mail.pop("admin_password", None)
    temp_mail["adminPasswordConfigured"] = bool(config.get("temp_mail", {}).get("admin_password")) if isinstance(config.get("temp_mail"), dict) else False
    result["temp_mail"] = temp_mail
    return result


def _empty_stats(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    return {
        "status": "idle",
        "submitted": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "success_rate": 0.0,
        "elapsed_seconds": 0.0,
        "batch_index": 0,
        "tasks": int(cfg.get("tasks") or 0),
        "success_tasks": cfg.get("success_tasks"),
        "concurrent_flows": int(cfg.get("concurrent_flows") or 0),
        "started_at": None,
        "updated_at": None,
        "finished_at": None,
    }


class OutlookRegistrationAdapter(Protocol):
    def __call__(self, config: dict[str, Any], control: "OutlookTaskControl") -> Mapping[str, Any] | None: ...


class OutlookRegistrationEngineAdapter:
    """Run the existing Outlook registration engine inside the main service.

    The legacy engine remains the implementation of the browser/OAuth flow,
    while this adapter supplies the main task control, Mongo result sink and
    Mongo proxy candidates.  The engine's Results files and JSON pool are
    bypassed for this runtime path.
    """

    def __init__(self, outlook_store: Any, outlook_service: Any) -> None:
        self.outlook_store = outlook_store
        self.outlook_service = outlook_service

    @staticmethod
    def _proxy_mapping(value: str) -> dict[str, Any] | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        parsed = urlsplit(raw)
        if not parsed.hostname or parsed.port is None:
            return None
        return {
            "scheme": parsed.scheme or "http",
            "host": parsed.hostname,
            "port": parsed.port,
            "username": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
        }

    def _load_runner(self) -> Any:
        # Do not import the legacy entry point as top-level ``main``: uvicorn
        # deployments and test runners may already have a different module by
        # that name.  Keep the old ``controllers`` imports working by exposing
        # its directory on sys.path, but load the runner under a stable alias.
        from pathlib import Path
        from importlib.util import module_from_spec, spec_from_file_location

        root = Path(__file__).resolve().parents[1] / "outlook_register"
        root_text = str(root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        module_name = "register_outlook_runtime"
        module = sys.modules.get(module_name)
        if module is None:
            spec = spec_from_file_location(module_name, root / "main.py")
            if spec is None or spec.loader is None:
                raise RuntimeError("无法加载 Outlook 注册执行引擎")
            module = module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                # Do not leave a half-imported runner cached after a failed
                # dependency import; a later task must be able to retry cleanly.
                sys.modules.pop(module_name, None)
                raise
        runner = getattr(module, "run_registration_job", None)
        if runner is None:
            raise RuntimeError("Outlook 注册引擎缺少 run_registration_job")
        return module, runner

    def __call__(self, config: dict[str, Any], control: "OutlookTaskControl") -> Mapping[str, Any]:
        proxy = config.get("proxy") if isinstance(config.get("proxy"), Mapping) else {}
        if str(proxy.get("source") or "mongo") == "mongo" and not list(proxy.get("candidates") or []):
            raise ValueError("主 MongoDB 所选代理组没有可用代理，未启动浏览器注册引擎")
        module, runner = self._load_runner()
        controller_type = getattr(module, "OutlookController", None)
        if controller_type is not None:
            controller_type.set_log_sink(control.on_log)

        def sink(payload: Mapping[str, Any]) -> None:
            # The legacy engine invokes this callback from its worker thread;
            # synchronously bridge the Mongo write so completion means durable.
            control.call(self._persist_result(dict(payload), control))

        try:
            control.on_log("[Outlook] 浏览器注册执行引擎已启用")
            result = runner(
                copy.deepcopy(config),
                control=control,
                clear_profiles=True,
                install_signals=False,
                result_sink=sink,
            )
            summary = dict(result or {})
            return {"status": "interrupted" if summary.get("interrupted") else "completed", **summary}
        finally:
            if controller_type is not None:
                controller_type.set_log_sink(None)

    async def _persist_result(self, payload: Mapping[str, Any], control: "OutlookTaskControl") -> None:
        kind = str(payload.get("kind") or "registered")
        email = str(payload.get("email") or "").strip()
        if not email:
            await control.log("[Outlook] 注册引擎返回空邮箱，结果已跳过", "WARN")
            return
        proxy_url = str(payload.get("proxy") or "")
        account_id, _ = await self.outlook_store.store_registration_result(
            email=email,
            password=str(payload.get("password") or ""),
            client_id=str(payload.get("client_id") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            oauth=kind == "oauth2",
            recovery_bound=bool(payload.get("recovery_bound")),
            recovery_email=str(payload.get("recovery_email") or ""),
            country=str(payload.get("country") or ""),
            proxy=proxy_url,
        )
        if kind == "oauth2" and payload.get("refresh_token") and payload.get("client_id"):
            check = await self.outlook_service.check_graph(
                account_id,
                proxy=self._proxy_mapping(proxy_url),
            )
            if check.get("ok"):
                await control.log(f"[Outlook] {email} 注册结果已写入 Mongo，Graph 校验通过并发布邮箱池", "OK")
            else:
                await control.log(f"[Outlook] {email} 注册结果已写入 Mongo，但 Graph 校验失败：{str(check.get('error') or 'unknown')[:180]}", "WARN")
        else:
            await control.log(f"[Outlook] {email} 注册结果已写入 Mongo（待 OAuth）", "OK")


class OutlookAuthorizedAccountAdapter:
    """Validate imported Outlook accounts through the main Mongo-backed service."""

    def __init__(self, outlook_service: Any) -> None:
        self.outlook_service = outlook_service

    async def run_async(self, config: dict[str, Any], control: "OutlookTaskControl") -> Mapping[str, Any]:
        task_limit = max(1, int(config.get("tasks") or 1))
        success_limit = config.get("success_tasks")
        if success_limit is not None:
            task_limit = min(task_limit, max(1, int(success_limit)))
        accounts = await self.outlook_service.store.registration_candidates(limit=task_limit)
        proxies = list((config.get("proxy") or {}).get("candidates") or [])
        await control.log(f"[Outlook] 授权账号执行器已启用：候选 {len(accounts)} 个，Mongo 代理 {len(proxies)} 个")
        if not accounts:
            await control.log("[Outlook] 没有同时配置 Client ID 和 Refresh Token 的账号", "WARN")
            await control.stats({"submitted": 0, "running": 0}, {}, {"status": "completed"})
            return {"status": "completed", "processed": 0}

        submitted = succeeded = failed = 0
        failures: dict[str, int] = {}
        concurrent = max(1, int(config.get("concurrent_flows") or 1))
        for index, account in enumerate(accounts):
            if control.should_stop():
                await control.log("[Outlook] 收到停止请求，任务在当前账号后退出", "WARN")
                await control.stats(
                    {"submitted": submitted, "running": 0, "succeeded": succeeded, "failed": failed},
                    failures,
                    {"status": "interrupted"},
                )
                return {"status": "interrupted", "processed": submitted}
            submitted += 1
            proxy = proxies[index % len(proxies)] if proxies else None
            email = str(account.get("email") or "")
            await control.stats(
                {"submitted": submitted, "running": 1, "succeeded": succeeded, "failed": failed},
                failures,
                {"status": "running", "concurrent_flows": concurrent},
            )
            try:
                result = await self.outlook_service.check_graph(str(account.get("_id") or ""), proxy=proxy)
                if result.get("ok") and result.get("poolStatus") in {"available", "assigned"}:
                    succeeded += 1
                    await control.log(f"[Outlook] {email} OAuth/Graph 校验通过，邮箱池状态 {result.get('poolStatus')}")
                else:
                    failed += 1
                    reason = "pool_conflict" if result.get("poolStatus") == "conflict" else str(result.get("oauthStatus") or "validation_failed")
                    failures[reason] = failures.get(reason, 0) + 1
                    await control.log(f"[Outlook] {email} 校验失败：{str(result.get('error') or reason)[:180]}", "WARN")
            except Exception as exc:
                failed += 1
                failures["executor_error"] = failures.get("executor_error", 0) + 1
                await control.log(f"[Outlook] {email} 执行异常：{type(exc).__name__}", "ERROR")
            await control.stats(
                {"submitted": submitted, "running": 0, "succeeded": succeeded, "failed": failed},
                failures,
                {"status": "running", "batch_index": submitted},
            )
        await control.log(f"[Outlook] 授权账号任务完成：成功 {succeeded}，失败 {failed}")
        await control.stats(
            {"submitted": submitted, "running": 0, "succeeded": succeeded, "failed": failed},
            failures,
            {"status": "completed"},
        )
        return {"status": "completed", "processed": submitted}


class _OutlookStatsOffsetControl:
    """Preserve registration counts while the authorized phase reports progress."""

    def __init__(self, control: "OutlookTaskControl", base: Mapping[str, Any]) -> None:
        self._control = control
        self._base = {key: int(base.get(key) or 0) for key in ("submitted", "succeeded", "failed")}

    def should_stop(self) -> bool:
        return self._control.should_stop()

    async def log(self, line: str, level: str | None = None) -> None:
        await self._control.log(line, level)

    def on_log(self, line: str, level: str | None = None) -> None:
        self._control.on_log(line, level)

    @staticmethod
    def _merge(runtime: Mapping[str, Any] | None, base: Mapping[str, int]) -> dict[str, Any]:
        result = dict(runtime or {})
        for key in ("submitted", "succeeded", "failed"):
            if key in result and result[key] is not None:
                result[key] = int(result[key] or 0) + int(base.get(key) or 0)
        return result

    async def stats(self, runtime_stats: Mapping[str, Any] | None, failure_stats: Mapping[str, Any] | None, extra: Mapping[str, Any] | None = None) -> None:
        await self._control.stats(self._merge(runtime_stats, self._base), failure_stats, extra)

    def on_stats(self, runtime_stats: Mapping[str, Any] | None, failure_stats: Mapping[str, Any] | None, extra: Mapping[str, Any] | None = None) -> None:
        self._control.on_stats(self._merge(runtime_stats, self._base), failure_stats, extra)

    def on_batch(self, batch_index: int, total_succeeded: int, total_failed: int, total_submitted: int) -> None:
        self._control.on_batch(batch_index, total_succeeded + self._base["succeeded"], total_failed + self._base["failed"], total_submitted + self._base["submitted"])

    def call(self, coroutine: Any) -> Any:
        return self._control.call(coroutine)

    def set_controller(self, controller: Any) -> None:
        self._control.set_controller(controller)


class OutlookRegistrationUnifiedAdapter:
    """Dispatch the complete Outlook task without silently disabling execution.

    ``auto`` preserves the existing imported-account workflow when Mongo has
    OAuth credentials, and falls back to the legacy browser registration engine
    when it does not.  The other modes make the choice explicit for operators
    and tests.
    """

    MODES = {"auto", "registration", "authorized", "both"}

    def __init__(self, outlook_store: Any, outlook_service: Any) -> None:
        self.outlook_service = outlook_service
        self.engine = OutlookRegistrationEngineAdapter(outlook_store, outlook_service)
        self.authorized = OutlookAuthorizedAccountAdapter(outlook_service)

    async def _authorized_candidates(self, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        task_limit = max(1, int(config.get("tasks") or 1))
        success_limit = config.get("success_tasks")
        if success_limit is not None:
            task_limit = min(task_limit, max(1, int(success_limit)))
        return await self.outlook_service.store.registration_candidates(limit=task_limit)

    async def _run_registration(self, config: dict[str, Any], control: "OutlookTaskControl") -> Mapping[str, Any]:
        # The legacy engine is synchronous and owns its browser threads. Run it
        # outside the FastAPI event loop, while its callbacks still bridge back
        # through OutlookTaskControl to persist Mongo state.
        runner = asyncio.create_task(asyncio.to_thread(self.engine, config, control))
        try:
            result = await asyncio.shield(runner)
        except asyncio.CancelledError:
            control.stop_event.set()
            # Cancellation of the coordinator must not orphan the browser
            # worker.  Wait for its cooperative cleanup before propagating.
            await runner
            raise
        return dict(result or {})

    async def run_async(self, config: dict[str, Any], control: "OutlookTaskControl") -> Mapping[str, Any]:
        mode = str(config.get("execution_mode") or "auto").strip().lower()
        if mode not in self.MODES:
            raise ValueError(f"不支持的 Outlook 执行模式: {mode}")

        if mode == "authorized":
            await control.log("[Outlook] 执行模式=authorized：仅校验 Mongo 中已有 OAuth 账号")
            return await self.authorized.run_async(config, control)

        if mode == "registration":
            await control.log("[Outlook] 执行模式=registration：启动浏览器注册执行引擎")
            return await self._run_registration(config, control)

        if mode == "both":
            await control.log("[Outlook] 执行模式=both：先执行浏览器注册，再校验 Mongo OAuth 账号")
            registration = await self._run_registration(config, control)
            if control.should_stop() or str(registration.get("status") or "") in {"failed", "interrupted"}:
                return registration
            base = {key: int(registration.get(key) or 0) for key in ("submitted", "succeeded", "failed")}
            authorized = await self.authorized.run_async(config, _OutlookStatsOffsetControl(control, base))
            return {
                "status": str(authorized.get("status") or registration.get("status") or "completed"),
                "submitted": base["submitted"] + int(authorized.get("submitted") or 0),
                "succeeded": base["succeeded"] + int(authorized.get("succeeded") or 0),
                "failed": base["failed"] + int(authorized.get("failed") or 0),
                "registration": registration,
                "authorized": authorized,
            }

        candidates = await self._authorized_candidates(config)
        if candidates:
            await control.log(f"[Outlook] 执行模式=auto：发现 {len(candidates)} 个 Mongo OAuth 账号，执行授权校验")
            return await self.authorized.run_async(config, control)

        await control.log("[Outlook] 执行模式=auto：没有可校验的 Mongo OAuth 账号，启动浏览器注册执行引擎")
        return await self._run_registration(config, control)


class OutlookTaskControl:
    def __init__(self, service: "OutlookRegisterTaskService", task_id: str) -> None:
        self.service = service
        self.task_id = task_id
        self.stop_event = threading.Event()

    def should_stop(self) -> bool:
        return self.stop_event.is_set()

    def call(self, coroutine: Any) -> Any:
        """Run a main-loop coroutine synchronously from the worker thread."""
        loop = self.service._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Outlook task event loop is unavailable")
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def on_log(self, line: str, level: str | None = None) -> None:
        self.service._schedule(self.service._append_log(self.task_id, line, level))

    async def log(self, line: str, level: str | None = None) -> None:
        await self.service._append_log(self.task_id, line, level)

    @staticmethod
    def _progress_extra(extra: Mapping[str, Any] | None) -> dict[str, Any]:
        # The legacy runner reports ``done``/``finished`` from progress
        # callbacks.  Only the main task worker owns terminal transitions.
        value = dict(extra or {})
        value.pop("finished", None)
        status = str(value.get("status") or "")
        if status and status not in {"running"}:
            value["status"] = "running"
        return value

    async def stats(self, runtime_stats: Mapping[str, Any] | None, failure_stats: Mapping[str, Any] | None, extra: Mapping[str, Any] | None = None) -> None:
        await self.service._apply_stats(self.task_id, runtime_stats or {}, failure_stats or {}, self._progress_extra(extra))

    def on_stats(self, runtime_stats: Mapping[str, Any] | None, failure_stats: Mapping[str, Any] | None, extra: Mapping[str, Any] | None = None) -> None:
        self.service._schedule(self.service._apply_stats(self.task_id, runtime_stats or {}, failure_stats or {}, self._progress_extra(extra)))

    def on_batch(self, batch_index: int, total_succeeded: int, total_failed: int, total_submitted: int) -> None:
        self.on_stats(
            {"succeeded": total_succeeded, "failed": total_failed, "submitted": total_submitted},
            {},
            {"batch_index": batch_index},
        )

    def set_controller(self, _controller: Any) -> None:
        return None


class OutlookRegisterTaskService:
    def __init__(
        self,
        resources: MongoResourceStore,
        *,
        adapter: OutlookRegistrationAdapter | None = None,
        result_sink: Any | None = None,
        outlook_service: Any | None = None,
    ) -> None:
        self.resources = resources
        self.manager = resources.manager
        if adapter is None:
            # The main service passes these dependencies explicitly.  Keep the
            # standalone constructor equally functional instead of silently
            # falling back to a lifecycle-only task that never executes Outlook.
            from .outlook_service import OutlookService, OutlookStore

            if result_sink is None:
                result_sink = OutlookStore(resources)
            if outlook_service is None:
                outlook_service = OutlookService(result_sink)
            adapter = OutlookRegistrationUnifiedAdapter(result_sink, outlook_service)
        self.adapter = adapter
        self.result_sink = result_sink
        self._thread: threading.Thread | None = None
        self._async_task: asyncio.Task[Any] | None = None
        self._control: OutlookTaskControl | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_futures: set[Any] = set()
        self._pending_lock = threading.Lock()
        self._lock = threading.RLock()
        self._lifecycle_lock = asyncio.Lock()
        self._stats_lock = asyncio.Lock()
        self._config_cache: dict[str, Any] | None = None

    @property
    def config_collection(self) -> Any:
        return self.manager.database["outlook_register_config"]

    @property
    def task_collection(self) -> Any:
        return self.manager.database["outlook_register_tasks"]

    @property
    def log_collection(self) -> Any:
        return self.manager.database["outlook_register_logs"]

    async def ensure_indexes(self) -> None:
        await self.resources._guard(self.config_collection.create_index("_id", name="outlook_register_config_id"))
        await self.resources._guard(self.task_collection.create_index("_id", name="outlook_register_task_id"))
        await self.resources._guard(self.log_collection.create_index([("taskId", 1), ("createdAt", -1)], name="outlook_register_logs_task"))

    async def get_config(self) -> dict[str, Any]:
        if self._config_cache is not None:
            return _public_config(self._config_cache)
        row = await self.resources._guard(self.config_collection.find_one({"_id": "default"}))
        config = _normalize_config((row or {}).get("config") if row else None) if row else _normalize_config(_default_config())
        self._config_cache = config
        if not row:
            await self.resources._guard(self.config_collection.update_one({"_id": "default"}, {"$set": {"config": config, "updatedAt": utc_now()}}, upsert=True))
        return _public_config(config)

    async def _internal_config(self) -> dict[str, Any]:
        if self._config_cache is not None:
            return copy.deepcopy(self._config_cache)
        await self.get_config()
        return copy.deepcopy(self._config_cache or _normalize_config(_default_config()))

    async def update_config(self, value: Mapping[str, Any]) -> dict[str, Any]:
        current = await self._internal_config()
        merged = copy.deepcopy(current)
        for key, item in value.items():
            if isinstance(item, Mapping) and isinstance(merged.get(key), dict):
                merged[key].update(copy.deepcopy(dict(item)))
            else:
                merged[key] = copy.deepcopy(item)
        # Blank secret fields mean "keep existing", not erase credentials.
        for section, field in (("oauth2", "client_id"), ("temp_mail", "admin_password")):
            incoming = merged.get(section, {}).get(field) if isinstance(merged.get(section), dict) else None
            if incoming == "" and current.get(section, {}).get(field):
                merged[section][field] = current[section][field]
        normalized = _normalize_config(merged)
        await self.resources._guard(self.config_collection.update_one({"_id": "default"}, {"$set": {"config": normalized, "updatedAt": utc_now()}}, upsert=True))
        self._config_cache = normalized
        return _public_config(normalized)

    async def _state(self) -> dict[str, Any]:
        row = await self.resources._guard(self.task_collection.find_one({"_id": TASK_ID}))
        if row:
            return self._public_state(row)
        config = await self._internal_config()
        return self._public_state({"_id": TASK_ID, "status": "idle", "stats": _empty_stats(config), "failureStats": {}, "logs": 0})

    @staticmethod
    def _public_state(row: Mapping[str, Any]) -> dict[str, Any]:
        stats = copy.deepcopy(row.get("stats") or {})
        failure = {str(key): int(value or 0) for key, value in (row.get("failureStats") or {}).items() if isinstance(value, (int, float))}
        return {
            "taskId": str(row.get("_id") or TASK_ID),
            "enabled": str(row.get("status") or "idle") in ACTIVE_STATUSES,
            "status": str(row.get("status") or "idle"),
            "stats": stats,
            "failure_stats": failure,
            "error": str(row.get("error") or "")[:180] or None,
            "startedAt": row.get("startedAt"),
            "finishedAt": row.get("finishedAt"),
            "updatedAt": row.get("updatedAt"),
            "proxyGroup": str(row.get("proxyGroup") or ""),
            "proxyCount": int(row.get("proxyCount") or 0),
        }

    async def status(self) -> dict[str, Any]:
        config = await self.get_config()
        state = await self._state()
        state["config"] = config
        state["result_count"] = await self.manager.database["outlook_accounts"].count_documents({})
        logs = await self.resources._guard(self.log_collection.count_documents({"taskId": TASK_ID}))
        state["log_count"] = int(logs)
        return state

    async def logs(self, limit: int = 200) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        cursor = self.log_collection.find({"taskId": TASK_ID}, {"taskId": 0}).sort("createdAt", -1).limit(bounded)
        rows = await self.resources._guard(cursor.to_list(length=bounded))
        return list(reversed(rows))

    async def resolve_proxy_candidates(self, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        proxy = config.get("proxy") if isinstance(config.get("proxy"), Mapping) else {}
        if str(proxy.get("source") or "mongo") != "mongo":
            return []
        docs = await self.resources.proxy_documents_for_test(group=str(proxy.get("group") or "") or None, limit=500)
        candidates: list[dict[str, Any]] = []
        for doc in docs:
            if not doc.get("enabled", True) or str(doc.get("status") or "available") == "quarantined":
                continue
            host = str(doc.get("host") or "").strip()
            try:
                port = int(doc.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if host and 1 <= port <= 65535:
                candidates.append({key: doc.get(key) for key in ("host", "port", "username", "password", "scheme", "country", "group")})
        return candidates

    async def start(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            with self._lock:
                if (self._thread and self._thread.is_alive()) or (self._async_task and not self._async_task.done()):
                    return await self.status()
            config = await self._internal_config()
            candidates = await self.resolve_proxy_candidates(config)
            task = {
                "_id": TASK_ID,
                "status": "running",
                "stats": {**_empty_stats(config), "status": "running", "started_at": _iso(), "updated_at": _iso()},
                "failureStats": {},
                "error": None,
                "startedAt": _iso(),
                "finishedAt": None,
                "updatedAt": utc_now(),
                "proxyGroup": str(config.get("proxy", {}).get("group") or ""),
                "proxyCount": len(candidates),
            }
            await self.resources._guard(self.task_collection.update_one({"_id": TASK_ID}, {"$set": task}, upsert=True))
            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._control = OutlookTaskControl(self, TASK_ID)
                runtime_config = copy.deepcopy(config)
                # Credentials remain in the worker's memory only and never enter Mongo task state.
                runtime_config.setdefault("proxy", {})["candidates"] = candidates
                if str(runtime_config["proxy"].get("source") or "mongo") == "mongo":
                    runtime_config["proxy"]["mode"] = "mongo"
                if hasattr(self.adapter, "run_async"):
                    self._async_task = asyncio.create_task(self._run_async_worker(runtime_config, self._control))
                else:
                    self._thread = threading.Thread(target=self._run_worker, args=(runtime_config, self._control, self._loop), name="outlook-register-task", daemon=True)
                    self._thread.start()
            return await self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            with self._lock:
                control = self._control
                thread = self._thread
                async_task = self._async_task
            if ((not thread or not thread.is_alive()) and (not async_task or async_task.done())) or control is None:
                try:
                    return await self.status()
                except Exception:
                    return {"taskId": TASK_ID, "enabled": False, "status": "idle", "stats": _empty_stats(), "failure_stats": {}}
            control.stop_event.set()
            await self._apply_stats(TASK_ID, {}, {}, {"status": "stopping"})
            if thread and thread.is_alive():
                await asyncio.to_thread(thread.join, STOP_TIMEOUT_SECONDS)
            if async_task and not async_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(async_task), timeout=STOP_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    # The browser runner is still stopping cooperatively. Do not
                    # cancel a to_thread task and orphan its browser threads.
                    pass
            return await self.status()

    async def reset(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            with self._lock:
                if (self._thread and self._thread.is_alive()) or (self._async_task and not self._async_task.done()):
                    raise RuntimeError("Outlook 注册任务运行中，先停止任务")
            config = await self._internal_config()
            await self.resources._guard(self.task_collection.update_one({"_id": TASK_ID}, {"$set": {"status": "idle", "stats": _empty_stats(config), "failureStats": {}, "error": None, "startedAt": None, "finishedAt": None, "proxyGroup": "", "proxyCount": 0, "updatedAt": utc_now()}}, upsert=True))
            await self.resources._guard(self.log_collection.delete_many({"taskId": TASK_ID}))
            return await self.status()

    def _schedule(self, coroutine: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            return
        with self._pending_lock:
            self._pending_futures.add(future)
        def _discard(item: Any) -> None:
            with self._pending_lock:
                self._pending_futures.discard(item)
        future.add_done_callback(_discard)

    async def _append_log(self, task_id: str, line: str, level: str | None = None) -> None:
        text = _redact_outlook_log(line).strip()
        if not text:
            return
        level = level or ("WARN" if "WARN" in text.upper() else "INFO")
        await self.resources._guard(self.log_collection.insert_one({"_id": uuid4().hex, "taskId": task_id, "createdAt": utc_now(), "level": level, "line": text[:2000]}))

    async def _apply_stats(self, task_id: str, runtime: Mapping[str, Any], failures: Mapping[str, Any], extra: Mapping[str, Any]) -> None:
        async with self._stats_lock:
            await self._apply_stats_unlocked(task_id, runtime, failures, extra)

    async def _apply_stats_unlocked(self, task_id: str, runtime: Mapping[str, Any], failures: Mapping[str, Any], extra: Mapping[str, Any]) -> None:
        row = await self.resources._guard(self.task_collection.find_one({"_id": task_id})) or {}
        # Once the owner has written a terminal state, a late browser callback
        # must not reopen the task or mutate its final status.
        if row.get("finishedAt") is not None and not extra.get("finished"):
            return
        stats = dict(row.get("stats") or {})
        for key in ("submitted", "running", "succeeded", "failed", "batch_index", "tasks", "success_tasks", "concurrent_flows", "elapsed_seconds"):
            if key in runtime and runtime[key] is not None:
                stats[key] = runtime[key]
        stats.update({key: extra[key] for key in ("batch_index", "tasks", "success_tasks", "concurrent_flows") if key in extra})
        done = int(stats.get("succeeded") or 0) + int(stats.get("failed") or 0)
        stats["success_rate"] = round(int(stats.get("succeeded") or 0) * 100 / max(done, 1), 1)
        requested_status = str(extra.get("status") or row.get("status") or stats.get("status") or "running")
        if row.get("status") == "stopping" and requested_status not in {"completed", "failed", "interrupted", "disabled"}:
            requested_status = "stopping"
        stats["status"] = requested_status
        stats["updated_at"] = _iso()
        current_failures = dict(row.get("failureStats") or {})
        for key, value in failures.items():
            try:
                current_failures[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                continue
        changes: dict[str, Any] = {"stats": stats, "failureStats": current_failures, "updatedAt": utc_now()}
        if extra.get("status"):
            changes["status"] = requested_status
        if extra.get("finished"):
            stats["running"] = 0
            changes["finishedAt"] = utc_now()
            stats["finished_at"] = _iso()
        await self.resources._guard(self.task_collection.update_one({"_id": task_id}, {"$set": changes}, upsert=True))

    async def _drain_pending(self) -> None:
        # Engine callbacks are scheduled from browser worker threads. Drain in
        # rounds so a final progress callback cannot overwrite completed state.
        while True:
            with self._pending_lock:
                pending = tuple(self._pending_futures)
            if not pending:
                return
            await asyncio.gather(*(asyncio.wrap_future(item) for item in pending), return_exceptions=True)


    async def _run_async_worker(self, config: dict[str, Any], control: OutlookTaskControl) -> None:
        try:
            result = dict(await self.adapter.run_async(config, control) or {})
            await self._drain_pending()
            status = str(result.get("status") or "completed")
            if control.should_stop() and status != "failed":
                status = "interrupted"
            if status not in {"completed", "interrupted", "failed"}:
                status = "completed"
            await self._apply_stats(TASK_ID, {}, {}, {"status": status, "finished": True})
        except asyncio.CancelledError:
            await self._drain_pending()
            await self._apply_stats(TASK_ID, {}, {}, {"status": "interrupted", "finished": True})
            raise
        except Exception as exc:
            await control.log(f"[Outlook] 任务异常：{type(exc).__name__}", "ERROR")
            await self._apply_stats(TASK_ID, {}, {"executor_error": 1}, {"status": "failed", "finished": True})
            await self._set_error(TASK_ID, "outlook_registration_task_failed")
        finally:
            with self._lock:
                self._control = None
                self._async_task = None

    async def _finish_sync_worker(self, status: str, failures: Mapping[str, Any] | None = None, error: str | None = None) -> None:
        await self._drain_pending()
        await self._apply_stats(TASK_ID, {}, failures or {}, {"status": status, "finished": True})
        if error:
            await self._set_error(TASK_ID, error)

    def _run_worker(self, config: dict[str, Any], control: OutlookTaskControl, loop: asyncio.AbstractEventLoop) -> None:
        status = "completed"
        failures: dict[str, int] = {}
        error: str | None = None
        try:
            result = self.adapter(config, control)
            result = dict(result or {})
            status = str(result.get("status") or "completed")
            if status not in {"completed", "disabled", "interrupted", "failed"}:
                status = "completed"
        except Exception as exc:
            control.on_log(f"[Outlook] 任务异常: {type(exc).__name__}", "ERROR")
            status = "failed"
            failures["executor_error"] = 1
            error = "outlook_registration_task_failed"
        try:
            # Run finalization directly from this worker rather than scheduling
            # a tracked future that would wait on itself in _drain_pending().
            if control.should_stop() and status != "failed":
                status = "interrupted"
            control.call(self._finish_sync_worker(status, failures, error))
        except Exception:
            # The event loop may be closing during process shutdown.
            pass
        finally:
            with self._lock:
                self._control = None
                self._thread = None

    async def _set_error(self, task_id: str, error: str) -> None:
        await self.resources._guard(self.task_collection.update_one({"_id": task_id}, {"$set": {"error": error, "updatedAt": utc_now()}}, upsert=True))

    async def close(self) -> None:
        with self._lock:
            control = self._control
            thread = self._thread
            async_task = self._async_task
        if control is not None:
            control.stop_event.set()
        if thread and thread.is_alive():
            await asyncio.to_thread(thread.join)
        if async_task and not async_task.done():
            await asyncio.gather(async_task, return_exceptions=True)
        # Let scheduled Mongo updates finish before the event loop is torn down.
        await self._drain_pending()

    async def record_oauth_result(self, *, email: str, password: str, refresh_token: str, client_id: str, proxy: str = "", country: str = "") -> None:
        if self.result_sink is None:
            return
        account_id, _ = await self.result_sink.upsert_account(email=email, password=password, client_id=client_id, refresh_token=refresh_token, source="registration")
        await self.result_sink.update_validation(account_id, oauth_status="ok", refresh_token=refresh_token, error=None)

    async def record_registered_result(self, *, email: str, password: str, proxy: str = "", country: str = "") -> None:
        if self.result_sink is None:
            return
        await self.result_sink.upsert_account(email=email, password=password, source="registration")
