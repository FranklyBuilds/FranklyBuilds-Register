"""注册任务服务：start/stop/reset + 实时 stats/logs。"""

from __future__ import annotations

import copy
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from controllers.outlook_controller import OutlookController
from main import JobControl, clear_interrupt, request_stop, run_registration_job
from services import config_store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_failure_stats() -> dict[str, int]:
    return {
        "ip_cant_open": 0,
        "ip_blocked": 0,
        "captcha_fail": 0,
        "captcha_btn2_never_appeared": 0,
        "captcha_btn2_appeared_but_failed": 0,
        "funcaptcha": 0,
        "timeout": 0,
        "register_page_open_fail": 0,
        "register_form_fail": 0,
        "mail_init_fail": 0,
        "oauth_login_timeout": 0,
        "oauth_consent_fail": 0,
        "oauth_code_fail": 0,
        "oauth_token_network_fail": 0,
        "oauth_token_fail": 0,
        "oauth_password_wrong": 0,
        "oauth_password_blocked": 0,
        "oauth_retry_exhausted": 0,
        "recovery_bind_fail": 0,
        "browser_launch_fail": 0,
        "browser_context_fail": 0,
        "browser_page_fail": 0,
        "playwright_runtime_fail": 0,
    }


class _ServiceControl(JobControl):
    def __init__(self, service: "RegisterService"):
        self._service = service

    def should_stop(self) -> bool:
        return (not self._service._enabled) or self._service._stop_event.is_set()

    def on_log(self, line: str) -> None:
        self._service._append_log(line)

    def on_stats(self, runtime_stats, failure_stats, extra=None) -> None:
        self._service._apply_stats(runtime_stats, failure_stats, extra)

    def set_controller(self, controller) -> None:
        self._service._set_controller(controller)

    def on_batch(self, batch_index, total_succeeded, total_failed, total_submitted) -> None:
        self._service._apply_stats(
            {"succeeded": total_succeeded, "failed": total_failed, "submitted": total_submitted},
            None,
            {
                "batch_index": batch_index,
                "total_succeeded": total_succeeded,
                "total_failed": total_failed,
                "total_submitted": total_submitted,
            },
        )


class RegisterService:
    def __init__(self):
        self._lock = threading.RLock()
        self._enabled = False
        self._runner: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._controller = None
        self._logs: deque[dict[str, str]] = deque(maxlen=500)
        self._job_id = ""
        self._status = "idle"
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._updated_at = time.time()
        self._log_path = ""
        self._stats = {
            "job_id": "",
            "submitted": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "success_rate": 0.0,
            "elapsed_seconds": 0.0,
            "batch_index": 0,
            "tasks": 0,
            "success_tasks": None,
            "concurrent_flows": 0,
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
            "status": "idle",
        }
        self._failure_stats = _empty_failure_stats()
        self._control = _ServiceControl(self)

    # ------------------------------------------------------------------ logs
    def _append_log(self, line: str, level: str | None = None) -> None:
        text = str(line or "").strip()
        if not text:
            return
        if level is None:
            upper = text.upper()
            if "[FAIL]" in upper or " FAIL " in upper:
                level = "FAIL"
            elif "[WARN]" in upper or " WARN " in upper:
                level = "WARN"
            elif "[OK]" in upper or " OK " in upper:
                level = "OK"
            else:
                level = "INFO"
        entry = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "line": text,
        }
        with self._lock:
            self._logs.append(entry)
            self._updated_at = time.time()

    def _set_controller(self, controller) -> None:
        with self._lock:
            self._controller = controller
            if controller is not None:
                self._log_path = getattr(controller, "log_path", "") or self._log_path

    def _apply_stats(self, runtime_stats, failure_stats, extra=None) -> None:
        with self._lock:
            runtime_stats = runtime_stats or {}
            extra = extra or {}
            for key in ("submitted", "running", "succeeded", "failed"):
                if key in runtime_stats and runtime_stats[key] is not None:
                    self._stats[key] = int(runtime_stats[key] or 0)
            if "started_at" in runtime_stats and runtime_stats["started_at"]:
                self._started_at = float(runtime_stats["started_at"])
            for key in (
                "batch_index",
                "tasks",
                "success_tasks",
                "concurrent_flows",
                "total_succeeded",
                "total_failed",
                "total_submitted",
            ):
                if key in extra and extra[key] is not None:
                    if key.startswith("total_"):
                        mapped = {
                            "total_succeeded": "succeeded",
                            "total_failed": "failed",
                            "total_submitted": "submitted",
                        }[key]
                        self._stats[mapped] = int(extra[key] or 0)
                    else:
                        self._stats[key] = extra[key]
            if extra.get("log_path"):
                self._log_path = str(extra["log_path"])
            if extra.get("status"):
                self._status = str(extra["status"])
                self._stats["status"] = self._status
            if extra.get("finished"):
                self._finished_at = time.time()
                self._stats["finished_at"] = _now_iso()
            if failure_stats and isinstance(failure_stats, dict):
                for k, v in failure_stats.items():
                    try:
                        self._failure_stats[k] = int(v or 0)
                    except Exception:
                        pass
            succeeded = int(self._stats.get("succeeded") or 0)
            failed = int(self._stats.get("failed") or 0)
            done = succeeded + failed
            self._stats["success_rate"] = round(succeeded * 100.0 / max(done, 1), 1)
            if self._started_at:
                self._stats["elapsed_seconds"] = round(time.time() - self._started_at, 1)
                self._stats["started_at"] = datetime.fromtimestamp(
                    self._started_at, tz=timezone.utc
                ).isoformat()
            self._stats["updated_at"] = _now_iso()
            self._stats["job_id"] = self._job_id
            self._updated_at = time.time()

    # ------------------------------------------------------------------ public
    def get(self) -> dict[str, Any]:
        with self._lock:
            # 运行中尽量从 controller 拉最新
            ctrl = self._controller
            if ctrl is not None and self._enabled:
                try:
                    runtime = ctrl.get_runtime_stats()
                    failures = (
                        ctrl.get_failure_stats()
                        if hasattr(ctrl, "get_failure_stats")
                        else dict(getattr(ctrl, "failure_stats", {}) or {})
                    )
                    self._apply_stats(runtime, failures, None)
                except Exception:
                    pass

            runner_alive = bool(self._runner and self._runner.is_alive())
            enabled = self._enabled and runner_alive
            if self._enabled and not runner_alive and self._status == "running":
                # 线程已结束但 flag 未清（极端竞态）
                enabled = False

            cfg = config_store.load_config()
            stats = copy.deepcopy(self._stats)
            stats["status"] = self._status if enabled or self._status != "idle" else "idle"
            if enabled:
                stats["status"] = "running"
            if self._started_at and enabled:
                stats["elapsed_seconds"] = round(time.time() - self._started_at, 1)

            return {
                "enabled": enabled,
                "config": cfg,
                "stats": stats,
                "failure_stats": copy.deepcopy(self._failure_stats),
                "logs": list(self._logs),
                "result_count": config_store.count_oauth_results(),
                "log_path": self._log_path,
                # 不再向 UI 上报强制无头：用户可自由开关 config headless
                "force_headless": False,
            }

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._enabled and self._runner and self._runner.is_alive():
                raise RuntimeError("任务运行中，禁止修改配置。请先停止。")
            current = config_store.load_config()
            # 允许 partial 更新：顶层与嵌套对象合并
            merged = copy.deepcopy(current)
            for key, value in (updates or {}).items():
                if key in ("enabled", "stats", "logs"):
                    continue
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
            saved = config_store.save_config(merged)
            self._append_log("[Service] 配置已保存", "OK")
            snap = self.get()
            snap["config"] = saved
            return snap

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._enabled = True
                self._stop_event.clear()
                self._append_log("[Service] 任务已在运行", "WARN")
                return self.get()

            cfg = config_store.load_config()
            self._stop_event.clear()
            clear_interrupt()
            self._enabled = True
            self._job_id = uuid.uuid4().hex
            self._status = "running"
            self._started_at = time.time()
            self._finished_at = None
            self._log_path = ""
            self._logs.clear()
            self._failure_stats = _empty_failure_stats()
            self._stats = {
                "job_id": self._job_id,
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
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "finished_at": None,
                "status": "running",
            }
            self._append_log(
                f"[Service] 启动注册任务 tasks={cfg.get('tasks')} "
                f"concurrent={cfg.get('concurrent_flows')} "
                f"success_tasks={cfg.get('success_tasks')}",
                "OK",
            )
            OutlookController.set_log_sink(self._append_log)
            self._runner = threading.Thread(
                target=self._run,
                name="outlook-register",
                daemon=True,
            )
            self._runner.start()
            return self.get()

    def stop(self) -> dict[str, Any]:
        # 先置位停投递标志；clean_up 切勿在持锁时同步执行（Playwright close 会卡死 API）
        with self._lock:
            self._enabled = False
            self._stop_event.set()
            request_stop()
            self._status = "stopping"
            self._stats["status"] = "stopping"
            self._stats["updated_at"] = _now_iso()
            self._append_log("[Service] 已请求停止，等待在途任务收尾…", "WARN")
            ctrl = self._controller

        def _async_cleanup(controller):
            if controller is None:
                return
            try:
                controller.clean_up(type="all_browser")
            except Exception as exc:
                try:
                    self._append_log(f"[Service][WARN] 异步清理浏览器异常: {exc}", "WARN")
                except Exception:
                    pass

        if ctrl is not None:
            t = threading.Thread(
                target=_async_cleanup,
                args=(ctrl,),
                name="register-stop-cleanup",
                daemon=True,
            )
            t.start()
            # 最多等 8s，避免 stop API 永久挂起；超时后后台继续关浏览器
            t.join(timeout=8.0)
            if t.is_alive():
                self._append_log(
                    "[Service][WARN] 浏览器清理超时，后台继续关闭，停止请求已生效",
                    "WARN",
                )
        return self.get()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._enabled and self._runner and self._runner.is_alive():
                raise RuntimeError("任务运行中，禁止重置统计。请先停止。")
            self._logs.clear()
            self._failure_stats = _empty_failure_stats()
            self._job_id = ""
            self._status = "idle"
            self._started_at = None
            self._finished_at = None
            self._log_path = ""
            self._stats = {
                "job_id": "",
                "submitted": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
                "success_rate": 0.0,
                "elapsed_seconds": 0.0,
                "batch_index": 0,
                "tasks": 0,
                "success_tasks": None,
                "concurrent_flows": 0,
                "started_at": None,
                "updated_at": _now_iso(),
                "finished_at": None,
                "status": "idle",
            }
            self._append_log("[Service] 统计与日志已重置", "INFO")
            return self.get()

    def _run(self) -> None:
        try:
            cfg = config_store.load_config()
            result = run_registration_job(
                cfg,
                control=self._control,
                clear_profiles=True,
                install_signals=False,
            )
            interrupted = bool(result.get("interrupted"))
            self._append_log(
                f"[Service] 任务结束 success={result.get('succeeded')} "
                f"fail={result.get('failed')} submitted={result.get('submitted')} "
                f"interrupted={interrupted}",
                "WARN" if interrupted else "OK",
            )
            with self._lock:
                self._stats["succeeded"] = int(result.get("succeeded") or 0)
                self._stats["failed"] = int(result.get("failed") or 0)
                self._stats["submitted"] = int(result.get("submitted") or 0)
                self._stats["batch_index"] = int(result.get("batches") or 0)
                self._stats["elapsed_seconds"] = round(float(result.get("elapsed") or 0), 1)
                done = self._stats["succeeded"] + self._stats["failed"]
                self._stats["success_rate"] = round(
                    self._stats["succeeded"] * 100.0 / max(done, 1), 1
                )
                if result.get("log_path"):
                    self._log_path = str(result["log_path"])
        except Exception as exc:
            self._append_log(f"[Service][FAIL] 任务异常: {exc}", "FAIL")
            self._append_log(traceback.format_exc().replace("\n", " | ")[:400], "FAIL")
        finally:
            OutlookController.set_log_sink(None)
            with self._lock:
                self._enabled = False
                self._stop_event.clear()
                self._controller = None
                self._finished_at = time.time()
                self._status = "idle"
                self._stats["status"] = "idle"
                self._stats["running"] = 0
                self._stats["finished_at"] = _now_iso()
                self._stats["updated_at"] = _now_iso()
                self._updated_at = time.time()
            clear_interrupt()


register_service = RegisterService()
