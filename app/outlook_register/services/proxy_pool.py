"""对接 Easy Proxies 管理 API，并在本项目内展示/同步端口池。"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from typing import Any

import requests

from services import config_store

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_CFG_PATH = os.path.join(ROOT_DIR, "proxy_pool.json")

_lock = threading.RLock()
_auto_thread: threading.Thread | None = None
_auto_stop = threading.Event()

_FLAG_COUNTRY = {
    "🇭🇰": "HK", "🇨🇳": "CN", "🇹🇼": "TW", "🇯🇵": "JP", "🇰🇷": "KR",
    "🇸🇬": "SG", "🇺🇸": "US", "🇬🇧": "GB", "🇩🇪": "DE", "🇫🇷": "FR",
    "🇨🇦": "CA", "🇦🇺": "AU", "🇳🇱": "NL", "🇷🇺": "RU", "🇮🇳": "IN",
    "🇧🇷": "BR", "🇹🇷": "TR", "🇻🇳": "VN", "🇹🇭": "TH", "🇲🇾": "MY",
    "🇵🇭": "PH", "🇮🇩": "ID", "🇲🇴": "MO", "🇦🇷": "AR", "🇮🇹": "IT",
    "🇪🇸": "ES", "🇵🇱": "PL", "🇺🇦": "UA", "🇦🇪": "AE", "🇮🇱": "IL",
}

_DEFAULT = {
    "enabled": True,
    "base_url": "http://127.0.0.1:9091",
    "password": "",
    "proxy_host": "127.0.0.1",
    "proxy_type": "http",
    "timeout_sec": 8,
    "auto_sync": True,
    "auto_sync_interval_sec": 60,
    "auto_sync_mode": "multiple",
    "last_sync_at": None,
    "last_sync_result": None,
    "notes": "",
}


def _load() -> dict[str, Any]:
    with _lock:
        if not os.path.isfile(PROXY_CFG_PATH):
            return copy.deepcopy(_DEFAULT)
        try:
            with open(PROXY_CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return copy.deepcopy(_DEFAULT)
            out = copy.deepcopy(_DEFAULT)
            out.update({k: v for k, v in data.items() if k in _DEFAULT or k in ("token",)})
            return out
        except Exception:
            return copy.deepcopy(_DEFAULT)


def _save(data: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        merged = copy.deepcopy(_DEFAULT)
        merged.update(data or {})
        # Docker 绑定挂载单文件时 os.replace 会报 Device or resource busy
        payload = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp = PROXY_CFG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, PROXY_CFG_PATH)
        except OSError:
            with open(PROXY_CFG_PATH, "w", encoding="utf-8") as f:
                f.write(payload)
            try:
                os.remove(PROXY_CFG_PATH + ".tmp")
            except OSError:
                pass
        return copy.deepcopy(merged)


def get_config() -> dict[str, Any]:
    """读取配置，并允许环境变量覆盖（Docker 一键部署用）。"""
    cfg = _load()
    # 环境变量优先，便于 compose 注入
    if os.environ.get("EASY_PROXIES_URL"):
        cfg["base_url"] = os.environ["EASY_PROXIES_URL"].rstrip("/")
    if os.environ.get("EASY_PROXIES_PASSWORD") is not None:
        cfg["password"] = os.environ.get("EASY_PROXIES_PASSWORD") or ""
    if os.environ.get("EASY_PROXIES_HOST"):
        cfg["proxy_host"] = os.environ["EASY_PROXIES_HOST"]
    if os.environ.get("EASY_PROXIES_TYPE"):
        cfg["proxy_type"] = os.environ["EASY_PROXIES_TYPE"]
    if os.environ.get("EASY_PROXIES_AUTO_SYNC") is not None:
        cfg["auto_sync"] = str(os.environ.get("EASY_PROXIES_AUTO_SYNC") or "").lower() in (
            "1", "true", "yes", "on",
        )
    return cfg


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    cur = _load()
    for k, v in (updates or {}).items():
        if k in _DEFAULT:
            cur[k] = v
    if cur.get("timeout_sec") is not None:
        cur["timeout_sec"] = max(2, min(int(cur["timeout_sec"] or 8), 60))
    if cur.get("auto_sync_interval_sec") is not None:
        cur["auto_sync_interval_sec"] = max(15, min(int(cur["auto_sync_interval_sec"] or 60), 3600))
    cur["base_url"] = str(cur.get("base_url") or "").rstrip("/")
    proxy_type = str(cur.get("proxy_type") or "http").lower()
    if proxy_type not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("proxy_type 只能是 http / https / socks5 / socks5h")
    cur["proxy_type"] = proxy_type
    cur["auto_sync"] = bool(cur.get("auto_sync"))
    saved = _save(cur)
    # 配置变更后重启自动同步线程
    ensure_auto_sync_running()
    return saved


def _session(cfg: dict | None = None) -> tuple[requests.Session, dict]:
    # 必须用 get_config()，才能吃到 Docker 注入的 EASY_PROXIES_* 环境变量
    cfg = cfg or get_config()
    return requests.Session(), cfg


def _login(session: requests.Session, cfg: dict) -> None:
    base = cfg.get("base_url") or ""
    if not base:
        raise ValueError("未配置 Easy Proxies base_url")
    password = str(cfg.get("password") or "")
    try:
        r = session.post(
            f"{base}/api/auth",
            json={"password": password},
            timeout=float(cfg.get("timeout_sec") or 8),
        )
        if r.status_code == 401:
            raise RuntimeError("Easy Proxies 管理密码错误")
    except requests.RequestException as exc:
        raise RuntimeError(f"连接 Easy Proxies 失败: {exc}") from exc


def _request(method: str, path: str, *, params=None, json_body=None) -> Any:
    session, cfg = _session()
    _login(session, cfg)
    base = cfg["base_url"]
    r = session.request(
        method,
        f"{base}{path}",
        params=params or {},
        json=json_body,
        timeout=float(cfg.get("timeout_sec") or 8),
    )
    if r.status_code == 401:
        raise RuntimeError("Easy Proxies 未授权，请检查管理密码")
    if r.status_code >= 400:
        try:
            detail = r.json().get("error") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RuntimeError(f"Easy Proxies {path} 错误: HTTP {r.status_code} {detail}")
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, body: dict | None = None) -> Any:
    return _request("POST", path, json_body=body or {})


def _guess_country(name: str, code: str = "") -> str:
    code = (code or "").strip().upper()
    if code and len(code) == 2:
        return code
    text = name or ""
    for flag, cc in _FLAG_COUNTRY.items():
        if flag in text:
            return cc
    # 中文关键词
    mapping = [
        ("香港", "HK"), ("台湾", "TW"), ("日本", "JP"), ("韩国", "KR"), ("新加坡", "SG"),
        ("美国", "US"), ("英国", "GB"), ("德国", "DE"), ("法国", "FR"), ("加拿大", "CA"),
        ("澳大利亚", "AU"), ("澳洲", "AU"), ("荷兰", "NL"), ("俄罗斯", "RU"), ("印度", "IN"),
        ("巴西", "BR"), ("土耳其", "TR"), ("越南", "VN"), ("泰国", "TH"), ("马来", "MY"),
        ("菲律宾", "PH"), ("印尼", "ID"), ("中国", "CN"), ("澳门", "MO"),
    ]
    for zh, cc in mapping:
        if zh in text:
            return cc
    m = re.search(r"\b([A-Z]{2})\b", text.upper())
    if m:
        return m.group(1)
    return ""


def summary() -> dict[str, Any]:
    cfg = get_config()
    if not cfg.get("enabled"):
        return {"enabled": False, "error": "代理池对接已关闭", "config": cfg}
    try:
        data = _get("/api/ui/summary")
        return {"enabled": True, "config": cfg, "summary": data, "ok": True}
    except Exception as exc:
        return {"enabled": True, "config": cfg, "ok": False, "error": str(exc)}


def list_ports(limit: int = 500) -> dict[str, Any]:
    """合并 ui/nodes(pool) 的 latency/country 与端口列表。"""
    cfg = get_config()
    if not cfg.get("enabled"):
        return {"enabled": False, "items": [], "error": "代理池对接已关闭"}
    try:
        # 主数据源：节点池（含 latency_ms / country_code / port）
        page = 1
        page_size = 120
        all_nodes: list[dict] = []
        total = None
        while True:
            data = _get(
                "/api/ui/nodes",
                params={
                    "scope": "pool",
                    "page": page,
                    "page_size": page_size,
                    "sort": "port",
                    "order": "asc",
                },
            )
            items = data.get("items") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
            all_nodes.extend(items)
            total = int(data.get("total") or len(all_nodes)) if isinstance(data, dict) else len(all_nodes)
            if len(all_nodes) >= total or not items or page >= 50:
                break
            page += 1

        # 辅助：ui/ports 只有 name/port/country_code（通常无 latency）
        port_meta: dict[int, dict] = {}
        try:
            ports_raw = _get("/api/ui/ports", params={"limit": min(max(int(limit), 1), 200)})
            if isinstance(ports_raw, list):
                for it in ports_raw:
                    if isinstance(it, dict) and it.get("port"):
                        port_meta[int(it["port"])] = it
        except Exception:
            pass

        # 实时健康检查延迟：/api/ui/nodes 的 latency_ms 多半是导入测速残留，
        # /api/nodes 的 last_latency_ms 才是 pool 周期探测（与 Clash 同风格 HTTP CF）。
        live_by_port: dict[int, dict[str, Any]] = {}
        try:
            live_raw = _get("/api/nodes")
            live_nodes = []
            if isinstance(live_raw, dict):
                live_nodes = live_raw.get("nodes") or live_raw.get("items") or []
            elif isinstance(live_raw, list):
                live_nodes = live_raw
            for ln in live_nodes:
                if not isinstance(ln, dict) or not ln.get("port"):
                    continue
                try:
                    pno = int(ln["port"])
                except Exception:
                    continue
                lat = ln.get("last_latency_ms")
                if lat is None:
                    lat = ln.get("latency_ms")
                try:
                    lat_i = int(lat) if lat is not None else None
                except Exception:
                    lat_i = None
                if lat_i is not None and lat_i < 0:
                    lat_i = None
                live_by_port[pno] = {
                    "latency_ms": lat_i,
                    "available": bool(ln.get("available", True)),
                    "node_name": ln.get("name") or ln.get("tag") or "",
                    "tag": ln.get("tag") or "",
                }
        except Exception:
            live_by_port = {}

        norm = []
        seen = set()
        for n in all_nodes:
            port = n.get("port")
            if not port:
                continue
            port = int(port)
            if port in seen:
                continue
            seen.add(port)
            name = n.get("name") or n.get("original_name") or ""
            country = _guess_country(name, n.get("country_code") or "")
            if not country and port in port_meta:
                country = _guess_country(
                    port_meta[port].get("name") or "",
                    port_meta[port].get("country_code") or "",
                )
            live = live_by_port.get(port) or {}
            latency = live.get("latency_ms")
            if latency is None:
                latency = n.get("latency_ms")
            try:
                latency = int(latency) if latency is not None else None
            except Exception:
                latency = None
            available = True
            if port in live_by_port:
                available = bool(live.get("available", True))
            try:
                from services.geo import country_label, country_zh
                c_zh = country_zh(country) if country else "未知"
                c_label = country_label(country) if country else "未知"
            except Exception:
                c_zh = country or "未知"
                c_label = country or "未知"
            norm.append(
                {
                    "id": n.get("id") or "",
                    "port": port,
                    "node_name": name,
                    "tag_prefix": n.get("tag_prefix") or "",
                    "country": country,
                    "country_code": country,
                    "country_zh": c_zh,
                    "country_label": c_label,
                    "latency_ms": latency,
                    "available": available,
                    "state": n.get("state") or "in_pool",
                    "proxy_url": f"{cfg.get('proxy_type') or 'http'}://{cfg.get('proxy_host') or '127.0.0.1'}:{port}",
                }
            )
        # 池内 live 节点若未出现在 ui/nodes（极少见），补齐端口
        for port, live in sorted(live_by_port.items()):
            if port in seen:
                continue
            if not live.get("available", True):
                continue
            name = live.get("node_name") or live.get("tag") or ""
            country = _guess_country(name, "")
            try:
                from services.geo import country_label, country_zh
                c_zh = country_zh(country) if country else "未知"
                c_label = country_label(country) if country else "未知"
            except Exception:
                c_zh = country or "未知"
                c_label = country or "未知"
            seen.add(port)
            norm.append(
                {
                    "id": "",
                    "port": port,
                    "node_name": name,
                    "tag_prefix": "",
                    "country": country,
                    "country_code": country,
                    "country_zh": c_zh,
                    "country_label": c_label,
                    "latency_ms": live.get("latency_ms"),
                    "available": True,
                    "state": "in_pool",
                    "proxy_url": f"{cfg.get('proxy_type') or 'http'}://{cfg.get('proxy_host') or '127.0.0.1'}:{port}",
                }
            )
        # 若 nodes 为空，退回 ports preview
        if not norm and port_meta:
            for port, it in sorted(port_meta.items()):
                name = it.get("name") or ""
                country = _guess_country(name, it.get("country_code") or "")
                norm.append(
                    {
                        "id": "",
                        "port": port,
                        "node_name": name,
                        "tag_prefix": it.get("tag_prefix") or "",
                        "country": country,
                        "country_code": country,
                        "latency_ms": None,
                        "available": True,
                        "state": "in_pool",
                        "proxy_url": f"{cfg.get('proxy_type') or 'http'}://{cfg.get('proxy_host') or '127.0.0.1'}:{port}",
                    }
                )

        norm.sort(key=lambda x: x["port"])
        if limit and len(norm) > int(limit):
            norm = norm[: int(limit)]
        countries = sorted({p["country"] for p in norm if p.get("country")})
        return {
            "enabled": True,
            "ok": True,
            "config": cfg,
            "items": norm,
            "count": len(norm),
            "countries": countries,
            "port_start": norm[0]["port"] if norm else None,
            "port_end": norm[-1]["port"] if norm else None,
        }
    except Exception as exc:
        return {"enabled": True, "ok": False, "config": cfg, "items": [], "error": str(exc)}


def list_pool_nodes(page: int = 1, page_size: int = 50, q: str = "") -> dict[str, Any]:
    cfg = get_config()
    if not cfg.get("enabled"):
        return {"enabled": False, "items": [], "error": "代理池对接已关闭"}
    try:
        data = _get(
            "/api/ui/nodes",
            params={
                "scope": "pool",
                "page": page,
                "page_size": page_size,
                "q": q or "",
                "sort": "latency",
                "order": "asc",
            },
        )
        return {"enabled": True, "ok": True, "config": cfg, "data": data}
    except Exception as exc:
        return {"enabled": True, "ok": False, "error": str(exc), "config": cfg}


def export_proxy_urls(limit: int = 2000) -> str:
    ports = list_ports(limit=limit)
    if not ports.get("ok"):
        raise RuntimeError(ports.get("error") or "无法读取端口")
    lines = []
    for it in ports.get("items") or []:
        url = it.get("proxy_url")
        if url:
            # 可选附带国家/延迟注释
            extra = []
            if it.get("country_label") or it.get("country"):
                extra.append(it.get("country_label") or it.get("country"))
            if it.get("latency_ms") is not None:
                extra.append(f"{it['latency_ms']}ms")
            if it.get("node_name"):
                extra.append(str(it["node_name"])[:40])
            if extra:
                lines.append(f"{url}  # {' | '.join(extra)}")
            else:
                lines.append(url)
    return "\n".join(lines) + ("\n" if lines else "")



def _select_healthy_ports(items: list[dict], max_latency_ms: int = 2500) -> list[int]:
    """优先选择有延迟且未超时的端口；无延迟数据时回退全部端口。"""
    scored: list[tuple[int, int]] = []  # (latency, port) 合格
    rejected: list[tuple[int, int]] = []  # 超阈值但仍有读数
    unknown: list[int] = []
    for it in items or []:
        try:
            port = int(it.get("port"))
        except Exception:
            continue
        # 明确不可用的 live 节点直接跳过
        if it.get("available") is False:
            continue
        lat = it.get("latency_ms")
        try:
            lat_i = int(lat) if lat is not None else None
        except Exception:
            lat_i = None
        # easy-proxies 常见：失败/超时 latency 很大或负值
        if lat_i is None or lat_i < 0:
            unknown.append(port)
            continue
        if lat_i > max_latency_ms:
            rejected.append((lat_i, port))
            continue
        scored.append((lat_i, port))
    scored.sort(key=lambda x: (x[0], x[1]))
    healthy = [p for _, p in scored]
    if healthy:
        return sorted(set(healthy))
    # 全无有效延迟读数时回退未知端口
    if unknown and not scored and not rejected:
        return sorted(set(unknown))
    # 全是高延迟：保留相对最低的一批，避免同步后无端口
    if rejected:
        rejected.sort(key=lambda x: (x[0], x[1]))
        keep = max(5, min(20, len(rejected) // 5 or 5))
        return sorted({p for _, p in rejected[:keep]})
    return []


def sync_to_register_config(mode: str = "multiple") -> dict[str, Any]:
    """把 Easy Proxies 可用端口范围写回注册机 config.json 的 proxy 段。"""
    ports_resp = list_ports(limit=2000)
    if not ports_resp.get("ok"):
        raise RuntimeError(ports_resp.get("error") or "无法读取端口")
    items = ports_resp.get("items") or []
    all_ports = sorted({int(i["port"]) for i in items if i.get("port")})
    cfg = get_config()
    try:
        max_lat = int(cfg.get("max_latency_ms") or 2500)
    except Exception:
        max_lat = 2500
    available = _select_healthy_ports(items, max_latency_ms=max_lat)
    if not available:
        available = all_ports
    if not available:
        raise RuntimeError("没有可用端口可同步")
    # 离散健康端口列表 + 起止范围；注册机会优先用 ports 列表
    reg = config_store.load_config()
    proxy = dict(reg.get("proxy") or {})
    proxy["type"] = cfg.get("proxy_type") or proxy.get("type") or "http"
    proxy["host"] = cfg.get("proxy_host") or proxy.get("host") or "127.0.0.1"
    proxy["ports"] = available
    lat_map: dict[int, int] = {}
    for it in items:
        try:
            pno = int(it["port"])
            if it.get("latency_ms") is None:
                continue
            lat_map[pno] = int(it["latency_ms"])
        except Exception:
            continue
    if mode == "single":
        proxy["mode"] = "single"
        best = sorted(available, key=lambda p: (lat_map.get(p, 10**9), p))[0]
        proxy["single_port"] = best
        proxy["port_start"] = best
        proxy["port_end"] = best
    else:
        proxy["mode"] = "multiple"
        proxy["port_start"] = available[0]
        proxy["port_end"] = available[-1]
    reg["proxy"] = proxy
    saved = config_store.save_config(reg)
    dropped = sorted(set(all_ports) - set(available))
    result = {
        "ok": True,
        "ports": available,
        "count": len(available),
        "port_start": available[0],
        "port_end": available[-1],
        "dropped_high_latency": dropped[:50],
        "dropped_count": len(dropped),
        "max_latency_ms": max_lat,
        "proxy": saved.get("proxy"),

        "countries": ports_resp.get("countries") or [],
        "note": "已写回健康端口列表(proxy.ports)与起止范围；高延迟端口已剔除",
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    cfg["last_sync_at"] = result["synced_at"]
    cfg["last_sync_result"] = {
        "count": result["count"],
        "port_start": result["port_start"],
        "port_end": result["port_end"],
    }
    _save(cfg)
    return result


# -------------------- 订阅导入 / 测速 --------------------

def list_import_sources() -> dict[str, Any]:
    try:
        data = _get("/api/import/sources")
        return {"ok": True, "sources": data if isinstance(data, list) else []}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "sources": []}


def import_subscription(url: str, tag_prefix: str = "sub", auto_test: bool = True, promote: bool = True) -> dict[str, Any]:
    """导入订阅 URL：parse → 可选 batch-test(start) 并 promote 进池。"""
    url = (url or "").strip()
    if not url:
        raise ValueError("订阅 URL 不能为空")
    tag_prefix = re.sub(r"[^a-zA-Z0-9._-]", "", tag_prefix or "sub")[:20] or "sub"

    parsed = _post(
        "/api/import/parse",
        {"mode": "url", "url": url, "tag_prefix": tag_prefix},
    )
    node_ids = []
    if isinstance(parsed, dict):
        for n in parsed.get("nodes") or []:
            if isinstance(n, dict) and n.get("id"):
                node_ids.append(n["id"])
    result: dict[str, Any] = {
        "ok": True,
        "parse": {
            "import_id": parsed.get("import_id") if isinstance(parsed, dict) else None,
            "format": parsed.get("format") if isinstance(parsed, dict) else None,
            "node_count": len(node_ids),
        },
        "test": None,
    }
    if auto_test and node_ids:
        # 异步批量测速
        try:
            job = _post(
                "/api/managed-nodes/batch-test/start",
                {
                    "node_ids": node_ids,
                    "retest": True,
                    "country": True,
                    "promote_passed": bool(promote),
                    "auto_reload": True,
                },
            )
            result["test"] = job
        except Exception as exc:
            # 兼容旧路径
            try:
                job = _post(
                    "/api/managed-nodes/batch-test",
                    {
                        "node_ids": node_ids,
                        "retest": True,
                        "country": True,
                        "promote_passed": bool(promote),
                        "auto_reload": True,
                    },
                )
                result["test"] = job
            except Exception as exc2:
                result["test_error"] = f"{exc}; fallback: {exc2}"
    return result


def refresh_sources(key: str = "") -> dict[str, Any]:
    body = {"key": key} if key else {}
    return {"ok": True, "job": _post("/api/import/refresh", body)}


def get_test_job(job_id: str) -> dict[str, Any]:
    # try several paths
    for path in (
        f"/api/managed-nodes/batch-test/status?job_id={job_id}",
        f"/api/import/jobs/{job_id}",
        f"/api/import/refresh/jobs/{job_id}",
    ):
        try:
            # status may be GET with query — use raw
            if "status" in path and "?" in path:
                p, q = path.split("?", 1)
                from urllib.parse import parse_qs
                params = {k: v[0] for k, v in parse_qs(q).items()}
                return {"ok": True, "job": _get(p, params=params)}
            return {"ok": True, "job": _get(path)}
        except Exception:
            continue
    return {"ok": False, "error": "无法查询任务状态"}


# -------------------- 自动同步 --------------------

def _auto_loop():
    while not _auto_stop.is_set():
        cfg = get_config()
        interval = max(15, int(cfg.get("auto_sync_interval_sec") or 60))
        if cfg.get("enabled") and cfg.get("auto_sync"):
            try:
                sync_to_register_config(mode=str(cfg.get("auto_sync_mode") or "multiple"))
            except Exception as exc:
                c = _load()
                c["last_sync_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                c["last_sync_result"] = {"error": str(exc)}
                _save(c)
        _auto_stop.wait(interval)


def ensure_auto_sync_running():
    global _auto_thread
    cfg = get_config()
    if not cfg.get("enabled") or not cfg.get("auto_sync"):
        return
    if _auto_thread and _auto_thread.is_alive():
        return
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=_auto_loop, name="proxy-auto-sync", daemon=True)
    _auto_thread.start()


def stop_auto_sync():
    _auto_stop.set()


# 模块导入时尝试启动
try:
    ensure_auto_sync_running()
except Exception:
    pass
