from __future__ import annotations

"""Durable Outlook registration task state.

The service owns task state independently from the GPT run store.  It deliberately
keeps the registration executor behind an injectable adapter: the repository's
third-party account-registration engine is not enabled by the default service.
This gives the console durable lifecycle/configuration primitives without making
an external registration workflow an implicit part of the main service.
"""

import asyncio
import copy
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from .resource_service import MongoResourceStore, utc_now


TASK_ID = "outlook-register"
ACTIVE_STATUSES = {"running", "stopping"}
DEFAULT_FAILURE_STATS = {
    "adapter_disabled": 0,
    "executor_error": 0,
}


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
    return config


def _normalize_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = copy.deepcopy(dict(value or {}))
    try:
        from outlook_register.services.config_store import normalize_config

        config = normalize_config(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
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


class OutlookRegistrationDisabled:
    """Default adapter: lifecycle smoke-test only, no external registration."""

    def __call__(self, _config: dict[str, Any], control: "OutlookTaskControl") -> Mapping[str, Any]:
        control.on_log("[Outlook] 注册执行适配器未启用；仅完成任务生命周期检查", "WARN")
        control.on_stats({}, {"adapter_disabled": 1}, {"status": "disabled"})
        return {"status": "disabled", "reason": "registration_adapter_disabled"}


class OutlookTaskControl:
    def __init__(self, service: "OutlookRegisterTaskService", task_id: str) -> None:
        self.service = service
        self.task_id = task_id
        self.stop_event = threading.Event()

    def should_stop(self) -> bool:
        return self.stop_event.is_set()

    def on_log(self, line: str, level: str | None = None) -> None:
        self.service._schedule(self.service._append_log(self.task_id, line, level))

    def on_stats(self, runtime_stats: Mapping[str, Any] | None, failure_stats: Mapping[str, Any] | None, extra: Mapping[str, Any] | None = None) -> None:
        self.service._schedule(self.service._apply_stats(self.task_id, runtime_stats or {}, failure_stats or {}, extra or {}))

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
    ) -> None:
        self.resources = resources
        self.manager = resources.manager
        self.adapter = adapter or OutlookRegistrationDisabled()
        self.result_sink = result_sink
        self._thread: threading.Thread | None = None
        self._control: OutlookTaskControl | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_futures: set[asyncio.Future[Any]] = set()
        self._lock = threading.RLock()
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
        await self.resources._guard(self.config_collection.create_index([("_id", 1)], unique=True, name="outlook_register_config_id"))
        await self.resources._guard(self.task_collection.create_index([("_id", 1)], unique=True, name="outlook_register_task_id"))
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
        with self._lock:
            if self._thread and self._thread.is_alive():
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
            # The legacy controller still accepts a host/port config. The adapter
            # receives the authoritative Mongo candidates separately.
            runtime_config["proxy"]["mode"] = "mongo"
            self._thread = threading.Thread(target=self._run_worker, args=(runtime_config, self._control, self._loop), name="outlook-register-task", daemon=True)
            self._thread.start()
        return await self.status()

    async def stop(self) -> dict[str, Any]:
        with self._lock:
            control = self._control
            thread = self._thread
        if not thread or not thread.is_alive() or control is None:
            try:
                return await self.status()
            except Exception:
                return {"taskId": TASK_ID, "enabled": False, "status": "idle", "stats": _empty_stats(), "failure_stats": {}}
        control.stop_event.set()
        await self.resources._guard(self.task_collection.update_one({"_id": TASK_ID}, {"$set": {"status": "stopping", "updatedAt": utc_now()}}, upsert=True))
        await asyncio.to_thread(thread.join, 5)
        return await self.status()

    async def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Outlook 注册任务运行中，先停止任务")
        config = await self._internal_config()
        await self.resources._guard(self.task_collection.update_one({"_id": TASK_ID}, {"$set": {"status": "idle", "stats": _empty_stats(config), "failureStats": {}, "error": None, "startedAt": None, "finishedAt": None, "updatedAt": utc_now()}}, upsert=True))
        await self.resources._guard(self.log_collection.delete_many({"taskId": TASK_ID}))
        return await self.status()

    def _schedule(self, coroutine: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            # Keep scheduled Mongo writes alive even when the worker exits.
            self._pending_futures.add(future)
            future.add_done_callback(lambda item: self._pending_futures.discard(item))
        except RuntimeError:
            return

    async def _append_log(self, task_id: str, line: str, level: str | None = None) -> None:
        text = str(line or "").strip()
        if not text:
            return
        level = level or ("WARN" if "WARN" in text.upper() else "INFO")
        await self.resources._guard(self.log_collection.insert_one({"_id": uuid4().hex, "taskId": task_id, "createdAt": utc_now(), "level": level, "line": text[:2000]}))

    async def _apply_stats(self, task_id: str, runtime: Mapping[str, Any], failures: Mapping[str, Any], extra: Mapping[str, Any]) -> None:
        row = await self.resources._guard(self.task_collection.find_one({"_id": task_id})) or {}
        stats = dict(row.get("stats") or {})
        for key in ("submitted", "running", "succeeded", "failed", "batch_index", "tasks", "success_tasks", "concurrent_flows", "elapsed_seconds"):
            if key in runtime and runtime[key] is not None:
                stats[key] = runtime[key]
        stats.update({key: extra[key] for key in ("batch_index", "tasks", "success_tasks", "concurrent_flows") if key in extra})
        done = int(stats.get("succeeded") or 0) + int(stats.get("failed") or 0)
        stats["success_rate"] = round(int(stats.get("succeeded") or 0) * 100 / max(done, 1), 1)
        stats["status"] = str(extra.get("status") or row.get("status") or stats.get("status") or "running")
        stats["updated_at"] = _iso()
        current_failures = dict(row.get("failureStats") or {})
        for key, value in failures.items():
            try:
                current_failures[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                continue
        changes: dict[str, Any] = {"stats": stats, "failureStats": current_failures, "updatedAt": utc_now()}
        if extra.get("status"):
            changes["status"] = extra["status"]
        if extra.get("finished"):
            changes["finishedAt"] = utc_now()
            stats["finished_at"] = _iso()
        await self.resources._guard(self.task_collection.update_one({"_id": task_id}, {"$set": changes}, upsert=True))

    def _run_worker(self, config: dict[str, Any], control: OutlookTaskControl, loop: asyncio.AbstractEventLoop) -> None:
        try:
            result = self.adapter(config, control)
            result = dict(result or {})
            status = str(result.get("status") or "completed")
            if status not in {"completed", "disabled", "interrupted", "failed"}:
                status = "completed"
            self._schedule(self._apply_stats(TASK_ID, {}, {}, {"status": status, "finished": True}))
        except Exception as exc:
            control.on_log(f"[Outlook] 任务异常: {type(exc).__name__}", "ERROR")
            self._schedule(self._apply_stats(TASK_ID, {}, {"executor_error": 1}, {"status": "failed", "finished": True}))
            self._schedule(self._set_error(TASK_ID, "outlook_registration_task_failed"))
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
        if control is not None:
            control.stop_event.set()
        if thread and thread.is_alive():
            await asyncio.to_thread(thread.join, 5)
        # Let scheduled Mongo updates finish before the event loop is torn down.
        pending = tuple(self._pending_futures)
        if pending:
            await asyncio.gather(*(asyncio.wrap_future(item) for item in pending), return_exceptions=True)

    async def record_oauth_result(self, *, email: str, password: str, refresh_token: str, client_id: str, proxy: str = "", country: str = "") -> None:
        if self.result_sink is None:
            return
        account_id, _ = await self.result_sink.upsert_account(email=email, password=password, client_id=client_id, refresh_token=refresh_token, source="registration")
        await self.result_sink.update_validation(account_id, oauth_status="ok", refresh_token=refresh_token, error=None)

    async def record_registered_result(self, *, email: str, password: str, proxy: str = "", country: str = "") -> None:
        if self.result_sink is None:
            return
        await self.result_sink.upsert_account(email=email, password=password, source="registration")
