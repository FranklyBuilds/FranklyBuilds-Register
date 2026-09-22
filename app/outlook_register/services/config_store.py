"""config.json 读写与校验。"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
EXAMPLE_PATH = os.path.join(ROOT_DIR, "config.example.json")

_DEFAULTS: dict[str, Any] = {
    "email_suffix": "@outlook.com",
    "headless": False,
    "bot_protection_wait": 15,
    "max_captcha_retries": 3,
    "captcha_strategy": 0,
    "concurrent_flows": 1,
    "tasks": 1,
    "success_tasks": None,
    "batch_success_limit": 300,
    "proxy": {
        "mode": "single",
        "type": "http",
        "host": "",
        "single_port": 0,
        "port_start": 0,
        "port_end": 0,
        "max_per_proxy": 20,
    },
    "oauth2": {
        "enable_oauth2": True,
        "client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        "redirect_url": "https://localhost",
        "Scopes": ["offline_access", "https://graph.microsoft.com/.default"],
    },
    "temp_mail": {
        "enabled": False,
        "base_url": "",
        "admin_password": "",
        "domain": "",
        "name_prefix": "",
        "enable_prefix": False,
        "code_timeout": 120,
        "poll_interval": 3,
    },
}

_lock = threading.RLock()


def _strip_json_comments(raw: str) -> str:
    lines = [line for line in raw.split("\n") if not line.strip().startswith("//")]
    return "\n".join(lines)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_config() -> dict[str, Any]:
    return copy.deepcopy(_DEFAULTS)


def ensure_config_file(path: str = CONFIG_PATH) -> str:
    """若 config.json 不存在，从 example 或默认值创建。"""
    if os.path.isfile(path):
        return path
    src = EXAMPLE_PATH if os.path.isfile(EXAMPLE_PATH) else None
    if src:
        with open(src, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(_strip_json_comments(raw))
    else:
        data = default_config()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    with _lock:
        ensure_config_file(path)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(_strip_json_comments(raw))
        if not isinstance(data, dict):
            raise ValueError("config.json 根节点必须是对象")
        return _deep_merge(_DEFAULTS, data)


def save_config(data: dict[str, Any], path: str = CONFIG_PATH) -> dict[str, Any]:
    normalized = normalize_config(data)
    with _lock:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Docker 绑定挂载单文件时 os.replace 会报 Device or resource busy
        payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            with open(path, "w", encoding="utf-8") as f:
                f.write(payload)
            try:
                os.remove(path + ".tmp")
            except OSError:
                pass
        return copy.deepcopy(normalized)


def normalize_config(data: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _deep_merge(_DEFAULTS, data or {})

    suffix = str(cfg.get("email_suffix") or "@outlook.com").strip()
    if not suffix.startswith("@"):
        suffix = "@" + suffix
    cfg["email_suffix"] = suffix

    cfg["headless"] = bool(cfg.get("headless"))
    cfg["bot_protection_wait"] = max(1, int(cfg.get("bot_protection_wait") or 15))
    cfg["max_captcha_retries"] = max(0, int(cfg.get("max_captcha_retries") or 0))

    strategy = int(cfg.get("captcha_strategy") if cfg.get("captcha_strategy") is not None else 0)
    if strategy not in (0, 1, 2):
        raise ValueError("captcha_strategy 只能是 0 / 1 / 2")
    cfg["captcha_strategy"] = strategy

    cfg["concurrent_flows"] = max(1, int(cfg.get("concurrent_flows") or 1))
    cfg["tasks"] = max(1, int(cfg.get("tasks") or 1))
    cfg["batch_success_limit"] = max(1, int(cfg.get("batch_success_limit") or 300))

    raw_success = cfg.get("success_tasks")
    if raw_success is None or raw_success == "" or str(raw_success).lower() in ("null", "none", "不限"):
        cfg["success_tasks"] = None
    else:
        cfg["success_tasks"] = max(1, int(raw_success))

    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    mode = str(proxy.get("mode") or "single").strip().lower()
    if mode not in ("single", "multiple"):
        raise ValueError("proxy.mode 只能是 single 或 multiple")
    proxy["mode"] = mode
    proxy_type = str(proxy.get("type") or "http").strip().lower() or "http"
    if proxy_type not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("proxy.type 只能是 http / https / socks5 / socks5h")
    proxy["type"] = proxy_type
    proxy["host"] = str(proxy.get("host") or "").strip()
    proxy["single_port"] = max(0, int(proxy.get("single_port") or 0))
    proxy["port_start"] = max(0, int(proxy.get("port_start") or 0))
    proxy["port_end"] = max(0, int(proxy.get("port_end") or 0))
    proxy["max_per_proxy"] = max(1, int(proxy.get("max_per_proxy") or 20))
    if mode == "multiple" and proxy["port_end"] < proxy["port_start"]:
        raise ValueError("proxy.port_end 必须 >= port_start")
    cfg["proxy"] = proxy

    oauth = cfg.get("oauth2") if isinstance(cfg.get("oauth2"), dict) else {}
    oauth["enable_oauth2"] = bool(oauth.get("enable_oauth2", True))
    oauth["client_id"] = str(oauth.get("client_id") or "").strip()
    oauth["redirect_url"] = str(
        oauth.get("redirect_url") or oauth.get("redirect_uri") or "https://localhost"
    ).strip()
    scopes = oauth.get("Scopes") or oauth.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.replace(",", " ").split() if s.strip()]
    elif isinstance(scopes, (list, tuple)):
        scopes = [str(s).strip() for s in scopes if str(s).strip()]
    else:
        scopes = list(_DEFAULTS["oauth2"]["Scopes"])
    oauth["Scopes"] = scopes
    oauth.pop("scopes", None)
    oauth.pop("redirect_uri", None)
    cfg["oauth2"] = oauth

    mail = cfg.get("temp_mail") if isinstance(cfg.get("temp_mail"), dict) else {}
    mail["enabled"] = bool(mail.get("enabled"))
    mail["base_url"] = str(mail.get("base_url") or "").strip().rstrip("/")
    mail["admin_password"] = str(mail.get("admin_password") or "")
    mail["domain"] = str(mail.get("domain") or "").strip()
    mail["name_prefix"] = str(mail.get("name_prefix") or "").strip()
    mail["enable_prefix"] = bool(mail.get("enable_prefix"))
    mail["code_timeout"] = max(10, int(mail.get("code_timeout") or 120))
    mail["poll_interval"] = max(1, int(mail.get("poll_interval") or 3))
    cfg["temp_mail"] = mail

    # 保留未识别的顶层键（如 browser）
    for key, value in (data or {}).items():
        if key not in cfg:
            cfg[key] = copy.deepcopy(value)

    return cfg


def count_oauth_results(results_dir: str | None = None) -> int:
    path = os.path.join(results_dir or os.path.join(ROOT_DIR, "Results"), "oauth2.txt")
    if not os.path.isfile(path):
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def read_oauth_results(limit: int = 50, results_dir: str | None = None) -> list[str]:
    path = os.path.join(results_dir or os.path.join(ROOT_DIR, "Results"), "oauth2.txt")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []
    if limit <= 0:
        return lines
    return lines[-limit:]


def oauth_results_path(results_dir: str | None = None) -> str:
    return os.path.join(results_dir or os.path.join(ROOT_DIR, "Results"), "oauth2.txt")
