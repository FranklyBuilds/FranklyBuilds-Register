"""OAuth 授权定时全量检测。"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from services import graph_mail, pool_store

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT_DIR, "Results", "oauth_check.json")

_lock = threading.RLock()
_auto_thread: threading.Thread | None = None
_auto_stop = threading.Event()
_job_lock = threading.Lock()

_DEFAULT: dict[str, Any] = {
    "enabled": False,
    "interval_sec": 3600,
    "delay_ms": 200,
    "last_run_at": None,
    "last_result": None,
    "running": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict[str, Any]:
    with _lock:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        if not os.path.isfile(CFG_PATH):
            return copy.deepcopy(_DEFAULT)
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return copy.deepcopy(_DEFAULT)
            out = copy.deepcopy(_DEFAULT)
            out.update(data)
            return out
        except Exception:
            return copy.deepcopy(_DEFAULT)


def _save(data: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        cur = copy.deepcopy(data)
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CFG_PATH)
        return cur


def get_config() -> dict[str, Any]:
    cfg = _load()
    cfg["running"] = bool(_job_lock.locked() or cfg.get("running"))
    return cfg


def update_config(updates: dict[str, Any] | None = None) -> dict[str, Any]:
    cur = _load()
    for k, v in (updates or {}).items():
        if k in ("enabled", "interval_sec", "delay_ms") and v is not None:
            cur[k] = v
    cur["enabled"] = bool(cur.get("enabled"))
    try:
        cur["interval_sec"] = max(300, min(int(cur.get("interval_sec") or 3600), 86400 * 7))
    except Exception:
        cur["interval_sec"] = 3600
    try:
        cur["delay_ms"] = max(0, min(int(cur.get("delay_ms") or 200), 5000))
    except Exception:
        cur["delay_ms"] = 200
    saved = _save(cur)
    ensure_auto_running()
    return get_config()


def check_one(account_id: str) -> dict[str, Any]:
    parent_id, client_id, refresh, _ = pool_store.resolve_oauth_credentials(account_id)
    if not refresh:
        raise ValueError("缺少 refresh_token")
    token = graph_mail.refresh_access_token(client_id, refresh)
    if token.get("ok"):
        pool_store.update_oauth_status(
            parent_id, "ok", error=None, new_refresh=token.get("refresh_token")
        )
        return {"id": account_id, "ok": True, "oauth_status": "ok"}
    status = "expired" if token.get("auth_expired") else "error"
    pool_store.update_oauth_status(parent_id, status, error=str(token.get("error") or ""))
    return {
        "id": account_id,
        "ok": False,
        "oauth_status": status,
        "error": token.get("error"),
    }


def run_full_check(*, delay_ms: int | None = None, source: str = "manual") -> dict[str, Any]:
    """串行检测全部 OAuth2 账号。并发一次只允许一个任务。"""
    if not _job_lock.acquire(blocking=False):
        cfg = get_config()
        return {
            "ok": False,
            "started": False,
            "running": True,
            "error": "已有检测任务在运行",
            "last_result": cfg.get("last_result"),
        }

    started_at = _now()
    ok_n = fail_n = 0
    results: list[dict[str, Any]] = []
    try:
        cfg = _load()
        cfg["running"] = True
        _save(cfg)
        accounts = pool_store.list_oauth2_account_ids()
        wait = delay_ms
        if wait is None:
            wait = int(cfg.get("delay_ms") or 200)
        wait = max(0, min(int(wait), 5000))

        for acc in accounts:
            aid = acc.get("id") or ""
            email = acc.get("email") or ""
            try:
                row = check_one(aid)
                row["email"] = email
                if row.get("ok"):
                    ok_n += 1
                else:
                    fail_n += 1
                results.append(row)
            except Exception as exc:
                fail_n += 1
                results.append({"id": aid, "email": email, "ok": False, "error": str(exc)})
            if wait > 0:
                time.sleep(wait / 1000.0)

        finished_at = _now()
        summary = {
            "ok": fail_n == 0,
            "source": source,
            "started_at": started_at,
            "finished_at": finished_at,
            "total": len(accounts),
            "success": ok_n,
            "failed": fail_n,
            # 结果可能很长，状态里只保留最近 30 条摘要
            "sample": results[:30],
        }
        cfg = _load()
        cfg["running"] = False
        cfg["last_run_at"] = finished_at
        cfg["last_result"] = {
            "ok": summary["ok"],
            "source": source,
            "started_at": started_at,
            "finished_at": finished_at,
            "total": summary["total"],
            "success": ok_n,
            "failed": fail_n,
        }
        _save(cfg)
        return {"ok": True, "started": True, "running": False, **summary}
    except Exception as exc:
        cfg = _load()
        cfg["running"] = False
        cfg["last_run_at"] = _now()
        cfg["last_result"] = {
            "ok": False,
            "source": source,
            "started_at": started_at,
            "finished_at": _now(),
            "error": str(exc),
        }
        _save(cfg)
        return {"ok": False, "started": True, "running": False, "error": str(exc)}
    finally:
        try:
            _job_lock.release()
        except Exception:
            pass


def start_full_check_async(*, delay_ms: int | None = None, source: str = "manual") -> dict[str, Any]:
    """后台启动全量检测，立即返回。"""
    if _job_lock.locked():
        return {"ok": False, "started": False, "running": True, "error": "已有检测任务在运行"}

    def _worker():
        run_full_check(delay_ms=delay_ms, source=source)

    t = threading.Thread(target=_worker, name="oauth-full-check", daemon=True)
    t.start()
    # 等一下让 running 状态写入
    time.sleep(0.05)
    return {"ok": True, "started": True, "running": True, "message": "已开始全量检测"}


def _auto_loop():
    while not _auto_stop.is_set():
        cfg = _load()
        interval = max(300, int(cfg.get("interval_sec") or 3600))
        if cfg.get("enabled"):
            # 到点就跑；若正在跑则跳过本轮
            if not _job_lock.locked():
                try:
                    run_full_check(source="schedule")
                except Exception:
                    pass
            # 按 interval 分段 sleep，便于 enabled 关闭后尽快退出
            slept = 0
            while slept < interval and not _auto_stop.is_set():
                step = min(5, interval - slept)
                time.sleep(step)
                slept += step
                # 运行中配置关闭则尽快结束本轮等待
                if not _load().get("enabled"):
                    break
        else:
            time.sleep(5)


def ensure_auto_running() -> None:
    global _auto_thread
    with _lock:
        cfg = _load()
        if not cfg.get("enabled"):
            return
        if _auto_thread and _auto_thread.is_alive():
            return
        _auto_stop.clear()
        _auto_thread = threading.Thread(target=_auto_loop, name="oauth-auto-check", daemon=True)
        _auto_thread.start()


def stop_auto() -> None:
    _auto_stop.set()
