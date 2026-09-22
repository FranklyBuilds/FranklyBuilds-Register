"""邮箱池持久化：注册成功 / OAuth2 成功 / 子邮箱。"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from services import config_store

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT_DIR, "Results")
POOL_PATH = os.path.join(RESULTS_DIR, "pool.json")

Category = Literal["registered", "oauth2", "sub"]

_lock = threading.RLock()
_imported_oauth2 = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_pool() -> dict[str, Any]:
    return {"version": 1, "updated_at": _now(), "accounts": []}


def _ensure_dir() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)


def _load_raw() -> dict[str, Any]:
    _ensure_dir()
    if not os.path.isfile(POOL_PATH):
        return _empty_pool()
    try:
        with open(POOL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_pool()
        if not isinstance(data.get("accounts"), list):
            data["accounts"] = []
        return data
    except Exception:
        return _empty_pool()


def _save_raw(data: dict[str, Any]) -> None:
    _ensure_dir()
    data = copy.deepcopy(data)
    data["updated_at"] = _now()
    tmp = POOL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, POOL_PATH)


def _public_base() -> str:
    base = str(os.environ.get("PUBLIC_BASE_URL") or os.environ.get("CHATGPT2API_BASE_URL") or "").strip()
    if base:
        return base.rstrip("/")
    host = str(os.environ.get("HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = str(os.environ.get("PORT") or "8000").strip()
    return f"http://{host}:{port}"


def receive_url_for_token(token: str) -> str:
    """JSON 接码 API。"""
    return f"{_public_base()}/api/pool/receive/{token}"


def receive_ui_url_for_token(token: str) -> str:
    """可视化邮件页面。"""
    return f"{_public_base()}/r/{token}"


def _new_id(prefix: str = "acc") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _find_by_email(accounts: list[dict], email: str) -> dict | None:
    email_l = (email or "").strip().lower()
    for acc in accounts:
        if str(acc.get("email") or "").strip().lower() == email_l:
            return acc
    return None


def _parse_iso_dt(value: Any):
    """解析 created_at / updated_at，兼容 Z 与无时区。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _age_fields(created_at: Any, updated_at: Any = None) -> dict[str, Any]:
    """注册时长与是否超过 7 天。优先 created_at，没有则用 updated_at。"""
    dt = _parse_iso_dt(created_at) or _parse_iso_dt(updated_at)
    if not dt:
        return {
            "registered_at": str(created_at or updated_at or "") or None,
            "age_days": None,
            "age_hours": None,
            "is_older_than_7_days": False,
            "age_label": "未知",
        }
    now = datetime.now(timezone.utc)
    delta = now - dt.astimezone(timezone.utc)
    age_days = max(0, int(delta.total_seconds() // 86400))
    age_hours = max(0, int(delta.total_seconds() // 3600))
    if age_days <= 0:
        age_label = f"{age_hours} 小时" if age_hours > 0 else "刚注册"
    elif age_days < 7:
        age_label = f"{age_days} 天"
    else:
        age_label = f"{age_days} 天"
    return {
        "registered_at": dt.astimezone(timezone.utc).isoformat(),
        "age_days": age_days,
        "age_hours": age_hours,
        "is_older_than_7_days": age_days >= 7,
        "age_label": age_label,
    }


def _account_view(acc: dict, mask_secrets: bool = True) -> dict:
    out = copy.deepcopy(acc)
    try:
        from services.geo import enrich_account_geo_fields, normalize_country_code
        # 清洗历史 ?? / 空值
        code = normalize_country_code(out.get("country"))
        out["country"] = code
        enrich_account_geo_fields(out)
    except Exception:
        out.setdefault("country_zh", "未知")
        out.setdefault("country_label", out.get("country") or "未知")
    out.update(_age_fields(out.get("created_at"), out.get("updated_at")))
    if mask_secrets:
        if out.get("password"):
            out["password_masked"] = _mask(out["password"])
        if out.get("refresh_token"):
            rt = str(out["refresh_token"])
            out["refresh_token_masked"] = (rt[:8] + "…" + rt[-6:]) if len(rt) > 16 else "***"
            # 列表默认不回传完整 token；详情接口再给
            out.pop("refresh_token", None)
            out.pop("password", None)
    subs = out.get("sub_emails") or []
    out["sub_count"] = len(subs)
    return out


def _mask(s: str) -> str:
    s = str(s or "")
    if len(s) <= 4:
        return "****"
    return s[:2] + "*" * max(len(s) - 4, 2) + s[-2:]


def stats() -> dict[str, int]:
    with _lock:
        data = _load_raw()
        maybe_import_oauth2_file(data)
        accounts = data.get("accounts") or []
        registered = sum(1 for a in accounts if a.get("category") == "registered")
        oauth2 = sum(1 for a in accounts if a.get("category") == "oauth2")
        recovery = sum(1 for a in accounts if a.get("recovery_bound"))
        subs = sum(len(a.get("sub_emails") or []) for a in accounts if a.get("category") == "oauth2")
        auth_ok = sum(1 for a in accounts if a.get("category") == "oauth2" and a.get("oauth_status") == "ok")
        auth_bad = sum(
            1 for a in accounts if a.get("category") == "oauth2" and a.get("oauth_status") in ("expired", "error")
        )
        auth_unknown = sum(
            1 for a in accounts if a.get("category") == "oauth2" and a.get("oauth_status") in (None, "", "unknown")
        )
        return {
            "registered": registered,
            "oauth2": oauth2,
            "recovery": recovery,
            "sub": subs,
            "oauth_ok": auth_ok,
            "oauth_bad": auth_bad,
            "oauth_unknown": auth_unknown,
            "total": registered + oauth2,
        }


def _normalize_oauth_filter(oauth_status: str = "") -> str:
    """ok|valid 正常；bad|expired|invalid 失效；unknown 未检测；空=全部。"""
    raw = str(oauth_status or "").strip().lower()
    if raw in ("ok", "valid", "alive", "good", "正常", "未失效", "有效"):
        return "ok"
    if raw in ("bad", "expired", "error", "invalid", "dead", "失效", "已失效", "异常"):
        return "bad"
    if raw in ("unknown", "unchecked", "pending", "未检测", "未知"):
        return "unknown"
    return ""


def _oauth_status_bucket(status: Any) -> str:
    s = str(status or "").strip().lower()
    if s == "ok":
        return "ok"
    if s in ("expired", "error"):
        return "bad"
    return "unknown"


def list_accounts(
    category: str | None = None,
    keyword: str = "",
    page: int = 1,
    page_size: int = 50,
    mask_secrets: bool = True,
    country: str = "",
    age: str = "",
    oauth_status: str = "",
) -> dict[str, Any]:
    """age: ''|all 全部；older7 超7天；within7 7天内。
    oauth_status: ''|all 全部；ok 正常；bad 失效；unknown 未检测。
    """
    with _lock:
        data = _load_raw()
        maybe_import_oauth2_file(data)
        items = list(data.get("accounts") or [])
        if category in ("registered", "oauth2"):
            items = [a for a in items if a.get("category") == category]
        elif category == "recovery":
            items = [a for a in items if a.get("recovery_bound")]
        elif category == "sub":
            # 展开子邮箱为虚拟行
            flat = []
            for a in items:
                if a.get("category") != "oauth2":
                    continue
                for sub in a.get("sub_emails") or []:
                    token = sub.get("receive_token") or ""
                    # 子邮箱自身创建时间；主号注册时间一并带上便于「超 7 天」标识
                    row = {
                        "id": sub.get("id"),
                        "category": "sub",
                        "email": sub.get("email"),
                        "parent_email": a.get("email"),
                        "parent_id": a.get("id"),
                        "receive_token": token,
                        "receive_url": sub.get("receive_url") or receive_url_for_token(token),
                        "receive_ui_url": sub.get("receive_ui_url") or receive_ui_url_for_token(token),
                        "created_at": sub.get("created_at") or a.get("created_at"),
                        "parent_created_at": a.get("created_at"),
                        "updated_at": a.get("updated_at"),
                        "oauth_status": a.get("oauth_status") or "unknown",
                        "tag": sub.get("tag"),
                        "country": a.get("country") or "",
                        "country_zh": a.get("country_zh") or "",
                        "proxy": a.get("proxy") or "",
                    }
                    try:
                        from services.geo import enrich_account_geo_fields
                        enrich_account_geo_fields(row)
                    except Exception:
                        row.setdefault("country_label", row.get("country") or "未知")
                    # 超 7 天按「主号注册时间」判断；展示注册时间用子邮箱自己的 created_at
                    parent_age = _age_fields(a.get("created_at"), a.get("updated_at"))
                    self_age = _age_fields(sub.get("created_at") or a.get("created_at"), a.get("updated_at"))
                    row.update(self_age)
                    row["is_older_than_7_days"] = bool(parent_age.get("is_older_than_7_days"))
                    row["parent_age_days"] = parent_age.get("age_days")
                    row["parent_age_label"] = parent_age.get("age_label")
                    flat.append(row)
            items = flat
        country_f = (country or "").strip().upper()
        if country_f and country_f not in ("ALL", "*", "全部", "未知"):
            items = [a for a in items if str(a.get("country") or "").upper() == country_f]
        elif country_f == "未知":
            items = [a for a in items if not str(a.get("country") or "").strip() or str(a.get("country")).upper() in ("??", "XX")]
        kw = (keyword or "").strip().lower()
        if kw:
            items = [
                a
                for a in items
                if kw in str(a.get("email") or "").lower()
                or kw in str(a.get("parent_email") or "").lower()
                or kw in str(a.get("note") or "").lower()
                or kw in str(a.get("country") or "").lower()
                or kw in str(a.get("country_zh") or "").lower()
                or kw in str(a.get("country_label") or "").lower()
                or kw in str(a.get("proxy") or "").lower()
            ]
        # 注册时长：先给每行补 is_older_than_7_days，再统计，最后按 age 筛选
        age_f = str(age or "").strip().lower()
        if age_f in ("older7", "older", "gt7", "over7", "7+", ">7", "超7天", "超过7天"):
            age_f = "older7"
        elif age_f in ("within7", "newer", "lt7", "<7", "7天内", "未满7天"):
            age_f = "within7"
        else:
            age_f = ""
        for a in items:
            if "is_older_than_7_days" not in a:
                a["is_older_than_7_days"] = bool(
                    _age_fields(a.get("created_at"), a.get("updated_at")).get("is_older_than_7_days")
                )
        # 统计「超 7 天」用筛选前数量，避免点「7 天内」时顶部数字变成 0
        older = sum(1 for a in items if a.get("is_older_than_7_days"))
        if age_f == "older7":
            items = [a for a in items if a.get("is_older_than_7_days")]
        elif age_f == "within7":
            items = [a for a in items if not a.get("is_older_than_7_days")]
        # OAuth 授权状态筛选（仅对有 oauth 的行有意义：oauth2 / sub / recovery 中带 token 的）
        oauth_f = _normalize_oauth_filter(oauth_status)
        if oauth_f:
            items = [a for a in items if _oauth_status_bucket(a.get("oauth_status")) == oauth_f]
        # 国家列表（代码 + 中文标签），便于前端下拉
        code_set = set()
        for a in (
            [x for x in (data.get("accounts") or []) if x.get("category") == category]
            if category in ("registered", "oauth2")
            else [x for x in (data.get("accounts") or []) if x.get("recovery_bound")]
            if category == "recovery"
            else [x for x in (data.get("accounts") or []) if x.get("category") == "oauth2"]
            if category == "sub"
            else (data.get("accounts") or [])
        ):
            c = str(a.get("country") or "").strip().upper()
            if c and c not in ("??", "XX", "ZZ"):
                code_set.add(c)
        countries = []
        try:
            from services.geo import country_label, country_zh
            for c in sorted(code_set):
                countries.append({"code": c, "zh": country_zh(c), "label": country_label(c)})
        except Exception:
            countries = [{"code": c, "zh": c, "label": c} for c in sorted(code_set)]
        # 新的在前
        items.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        total = len(items)
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 50), 200))
        start = (page - 1) * page_size
        slice_items = items[start : start + page_size]
        if category != "sub":
            slice_items = [_account_view(a, mask_secrets=mask_secrets) for a in slice_items]
        else:
            # sub 行也统一补全展示字段
            slice_items = [dict(x) for x in slice_items]
            for row in slice_items:
                try:
                    from services.geo import enrich_account_geo_fields
                    enrich_account_geo_fields(row)
                except Exception:
                    pass
                if "age_days" not in row:
                    row.update(_age_fields(row.get("created_at"), row.get("updated_at")))
        st = stats_unlocked(data)
        st["older_than_7_days"] = older
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": slice_items,
            "stats": st,
            "countries": countries,
            "age_filter": age_f or "all",
            "oauth_status_filter": oauth_f or "all",
        }


def stats_unlocked(data: dict) -> dict[str, int]:
    accounts = data.get("accounts") or []
    registered = sum(1 for a in accounts if a.get("category") == "registered")
    oauth2 = sum(1 for a in accounts if a.get("category") == "oauth2")
    recovery = sum(1 for a in accounts if a.get("recovery_bound"))
    subs = sum(len(a.get("sub_emails") or []) for a in accounts if a.get("category") == "oauth2")
    auth_ok = sum(1 for a in accounts if a.get("category") == "oauth2" and a.get("oauth_status") == "ok")
    auth_bad = sum(
        1 for a in accounts if a.get("category") == "oauth2" and a.get("oauth_status") in ("expired", "error")
    )
    auth_unknown = sum(
        1
        for a in accounts
        if a.get("category") == "oauth2" and a.get("oauth_status") in (None, "", "unknown")
    )
    return {
        "registered": registered,
        "oauth2": oauth2,
        "recovery": recovery,
        "sub": subs,
        "oauth_ok": auth_ok,
        "oauth_bad": auth_bad,
        "oauth_unknown": auth_unknown,
        "total": registered + oauth2,
    }


def list_oauth2_account_ids() -> list[dict[str, str]]:
    """返回全部 OAuth2 主账号 id/email，供全量检测。"""
    with _lock:
        data = _load_raw()
        maybe_import_oauth2_file(data)
        out = []
        for acc in data.get("accounts") or []:
            if acc.get("category") != "oauth2":
                continue
            if not acc.get("refresh_token"):
                continue
            out.append({"id": str(acc.get("id") or ""), "email": str(acc.get("email") or "")})
        return out


def get_account(account_id: str, include_secrets: bool = False) -> dict | None:
    with _lock:
        data = _load_raw()
        for acc in data.get("accounts") or []:
            if acc.get("id") == account_id:
                return copy.deepcopy(acc) if include_secrets else _account_view(acc, mask_secrets=True)
            for sub in acc.get("sub_emails") or []:
                if sub.get("id") == account_id:
                    row = copy.deepcopy(sub)
                    row["category"] = "sub"
                    row["parent_id"] = acc.get("id")
                    row["parent_email"] = acc.get("email")
                    row["oauth_status"] = acc.get("oauth_status")
                    if include_secrets:
                        row["parent"] = {
                            "email": acc.get("email"),
                            "client_id": acc.get("client_id"),
                            "refresh_token": acc.get("refresh_token"),
                        }
                    return row
        return None


def get_account_by_email(email: str, include_secrets: bool = True) -> dict | None:
    with _lock:
        data = _load_raw()
        acc = _find_by_email(data.get("accounts") or [], email)
        return copy.deepcopy(acc) if acc and include_secrets else (_account_view(acc) if acc else None)


def get_by_receive_token(token: str) -> tuple[dict | None, dict | None]:
    """返回 (parent_account, sub_email_dict)。"""
    token = (token or "").strip()
    if not token:
        return None, None
    with _lock:
        data = _load_raw()
        for acc in data.get("accounts") or []:
            for sub in acc.get("sub_emails") or []:
                if sub.get("receive_token") == token:
                    return copy.deepcopy(acc), copy.deepcopy(sub)
        return None, None


def upsert_registered(email: str, password: str, **extra) -> dict:
    """注册成功进入邮箱页：写入 registered；若已是 oauth2 则只补 password。"""
    email = (email or "").strip()
    password = str(password or "")
    if not email:
        raise ValueError("email required")
    with _lock:
        data = _load_raw()
        accounts = data.setdefault("accounts", [])
        existing = _find_by_email(accounts, email)
        now = _now()
        if existing:
            if password:
                existing["password"] = password
            existing["updated_at"] = now
            if existing.get("category") != "oauth2":
                existing["category"] = "registered"
                existing["register_status"] = "mailbox_ok"
            if extra.get("recovery_bound"):
                existing["recovery_bound"] = True
                existing["recovery_email"] = extra.get("recovery_email") or existing.get("recovery_email") or ""
            if extra.get("country"):
                existing["country"] = str(extra.get("country") or "").strip().upper()
                if existing["country"] in ("??", "XX", "ZZ"):
                    existing["country"] = ""
            if extra.get("country_zh"):
                existing["country_zh"] = str(extra.get("country_zh") or "")
            elif extra.get("country"):
                try:
                    from services.geo import country_zh
                    existing["country_zh"] = country_zh(existing.get("country"))
                except Exception:
                    pass
            if extra.get("proxy"):
                existing["proxy"] = str(extra.get("proxy") or "")
            for k, v in extra.items():
                if v is not None and k not in ("recovery_bound",):
                    existing[k] = v
            try:
                from services.geo import enrich_account_geo_fields
                enrich_account_geo_fields(existing)
            except Exception:
                pass
            _save_raw(data)
            _append_line("registered.txt", f"{email}----{password}")
            return copy.deepcopy(existing)

        acc = {
            "id": _new_id("reg"),
            "category": "registered",
            "email": email,
            "password": password,
            "register_status": "mailbox_ok",
            "oauth_status": "none",
            "client_id": "",
            "refresh_token": "",
            "recovery_bound": bool(extra.get("recovery_bound")),
            "recovery_email": extra.get("recovery_email") or "",
            "country": str(extra.get("country") or "").strip().upper(),
            "country_zh": str(extra.get("country_zh") or ""),
            "proxy": str(extra.get("proxy") or ""),
            "sub_emails": [],
            "created_at": now,
            "updated_at": now,
            "note": extra.get("note") or "",
            "source": extra.get("source") or "register",
        }
        if acc["country"] in ("??", "XX", "ZZ"):
            acc["country"] = ""
        try:
            from services.geo import enrich_account_geo_fields
            enrich_account_geo_fields(acc)
        except Exception:
            acc.setdefault("country_zh", "未知")
            acc.setdefault("country_label", "未知")
        accounts.append(acc)
        _save_raw(data)
        _append_line("registered.txt", f"{email}----{password}")
        return copy.deepcopy(acc)


def upsert_oauth2(
    email: str,
    password: str,
    client_id: str,
    refresh_token: str,
    **extra,
) -> dict:
    email = (email or "").strip()
    if not email or not refresh_token:
        raise ValueError("email and refresh_token required")
    with _lock:
        data = _load_raw()
        accounts = data.setdefault("accounts", [])
        existing = _find_by_email(accounts, email)
        now = _now()
        if existing:
            existing["category"] = "oauth2"
            if password:
                existing["password"] = password
            existing["client_id"] = client_id or existing.get("client_id") or ""
            existing["refresh_token"] = refresh_token
            existing["oauth_status"] = extra.get("oauth_status") or "ok"
            existing["register_status"] = "mailbox_ok"
            existing["updated_at"] = now
            existing["oauth_checked_at"] = now
            if extra.get("recovery_bound"):
                existing["recovery_bound"] = True
                existing["recovery_email"] = extra.get("recovery_email") or existing.get("recovery_email") or ""
            if extra.get("country"):
                existing["country"] = str(extra.get("country") or "").strip().upper()
                if existing["country"] in ("??", "XX", "ZZ"):
                    existing["country"] = ""
            if extra.get("country_zh"):
                existing["country_zh"] = str(extra.get("country_zh") or "")
            if extra.get("proxy"):
                existing["proxy"] = str(extra.get("proxy") or "")
            if "sub_emails" not in existing or existing["sub_emails"] is None:
                existing["sub_emails"] = []
            for k, v in extra.items():
                if k in ("oauth_status", "recovery_bound") or v is None:
                    continue
                existing[k] = v
            try:
                from services.geo import enrich_account_geo_fields
                enrich_account_geo_fields(existing)
            except Exception:
                pass
            _save_raw(data)
            return copy.deepcopy(existing)

        acc = {
            "id": _new_id("oauth"),
            "category": "oauth2",
            "email": email,
            "password": password or "",
            "client_id": client_id or "",
            "refresh_token": refresh_token,
            "register_status": "mailbox_ok",
            "oauth_status": extra.get("oauth_status") or "ok",
            "oauth_checked_at": now,
            "recovery_bound": bool(extra.get("recovery_bound")),
            "recovery_email": extra.get("recovery_email") or "",
            "country": str(extra.get("country") or "").strip().upper(),
            "country_zh": str(extra.get("country_zh") or ""),
            "proxy": str(extra.get("proxy") or ""),
            "sub_emails": [],
            "created_at": now,
            "updated_at": now,
            "note": extra.get("note") or "",
            "source": extra.get("source") or "register",
        }
        if acc["country"] in ("??", "XX", "ZZ"):
            acc["country"] = ""
        try:
            from services.geo import enrich_account_geo_fields
            enrich_account_geo_fields(acc)
        except Exception:
            acc.setdefault("country_zh", "未知")
            acc.setdefault("country_label", "未知")
        accounts.append(acc)
        _save_raw(data)
        return copy.deepcopy(acc)


def _append_line(filename: str, line: str) -> None:
    _ensure_dir()
    path = os.path.join(RESULTS_DIR, filename)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def _random_tag(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_sub_emails(account_id: str, count: int = 1, tag_prefix: str = "") -> dict:
    """为 OAuth2 账号生成 plus 子邮箱，并分配接码 token/URL。"""
    count = max(1, min(int(count or 1), 50))
    prefix = re.sub(r"[^a-zA-Z0-9._-]", "", tag_prefix or "")[:20]
    with _lock:
        data = _load_raw()
        acc = None
        for a in data.get("accounts") or []:
            if a.get("id") == account_id:
                acc = a
                break
        if not acc:
            raise ValueError("账号不存在")
        if acc.get("category") != "oauth2":
            raise ValueError("仅 OAuth2 成功的账号可生成子邮箱")
        if not acc.get("refresh_token"):
            raise ValueError("账号缺少 refresh_token，无法用于接码")

        email = str(acc.get("email") or "")
        if "@" not in email:
            raise ValueError("主邮箱格式无效")
        local, domain = email.split("@", 1)
        # 若 local 已含 +，取主 local
        if "+" in local:
            local = local.split("+", 1)[0]

        existing_tags = {str(s.get("tag") or "").lower() for s in (acc.get("sub_emails") or [])}
        existing_emails = {str(s.get("email") or "").lower() for s in (acc.get("sub_emails") or [])}
        created = []
        now = _now()
        subs = acc.setdefault("sub_emails", [])
        for i in range(count):
            for _ in range(30):
                tag = f"{prefix}{_random_tag(6)}" if prefix else _random_tag(8)
                if tag.lower() in existing_tags:
                    continue
                sub_email = f"{local}+{tag}@{domain}"
                if sub_email.lower() in existing_emails:
                    continue
                token = secrets.token_urlsafe(18)
                row = {
                    "id": _new_id("sub"),
                    "email": sub_email,
                    "tag": tag,
                    "receive_token": token,
                    "receive_url": receive_url_for_token(token),
                    "receive_ui_url": receive_ui_url_for_token(token),
                    "created_at": now,
                }
                subs.append(row)
                existing_tags.add(tag.lower())
                existing_emails.add(sub_email.lower())
                created.append(copy.deepcopy(row))
                break
        acc["updated_at"] = now
        _save_raw(data)
        return {"account_id": acc["id"], "parent_email": email, "created": created, "sub_count": len(subs)}


def delete_accounts(ids: list[str]) -> dict:
    idset = {str(i).strip() for i in ids if str(i).strip()}
    if not idset:
        return {"deleted": 0}
    with _lock:
        data = _load_raw()
        accounts = data.get("accounts") or []
        deleted = 0
        kept = []
        for acc in accounts:
            if acc.get("id") in idset:
                deleted += 1
                continue
            # 删子邮箱
            subs = acc.get("sub_emails") or []
            new_subs = []
            for s in subs:
                if s.get("id") in idset:
                    deleted += 1
                else:
                    new_subs.append(s)
            acc["sub_emails"] = new_subs
            kept.append(acc)
        data["accounts"] = kept
        _save_raw(data)
        return {"deleted": deleted}


def update_oauth_status(account_id: str, status: str, error: str | None = None, new_refresh: str | None = None) -> None:
    with _lock:
        data = _load_raw()
        for acc in data.get("accounts") or []:
            if acc.get("id") == account_id:
                acc["oauth_status"] = status
                acc["oauth_checked_at"] = _now()
                acc["updated_at"] = _now()
                if error is not None:
                    acc["oauth_error"] = str(error)[:300]
                elif status == "ok":
                    acc["oauth_error"] = ""
                if new_refresh:
                    acc["refresh_token"] = new_refresh
                _save_raw(data)
                return


def export_text(category: str, ids: list[str] | None = None, country: str | None = None) -> str:
    """按分类导出纯文本。可选按注册国家过滤。"""
    idset = {str(i).strip() for i in (ids or []) if str(i).strip()} or None
    country_f = (country or "").strip().upper()
    if country_f in ("", "ALL", "*", "全部"):
        country_f = ""

    def _country_ok(acc: dict) -> bool:
        if not country_f:
            return True
        return str(acc.get("country") or "").upper() == country_f

    with _lock:
        data = _load_raw()
        maybe_import_oauth2_file(data)
        lines: list[str] = []
        if category == "registered":
            for acc in data.get("accounts") or []:
                if acc.get("category") != "registered":
                    continue
                if idset and acc.get("id") not in idset:
                    continue
                if not _country_ok(acc):
                    continue
                lines.append(f"{acc.get('email','')}----{acc.get('password','')}")
        elif category == "oauth2":
            for acc in data.get("accounts") or []:
                if acc.get("category") != "oauth2":
                    continue
                if idset and acc.get("id") not in idset:
                    continue
                if not _country_ok(acc):
                    continue
                lines.append(
                    f"{acc.get('email','')}----{acc.get('password','')}----"
                    f"{acc.get('client_id','')}----{acc.get('refresh_token','')}"
                )
        elif category == "sub":
            for acc in data.get("accounts") or []:
                if acc.get("category") != "oauth2":
                    continue
                if not _country_ok(acc):
                    continue
                parent_selected = bool(idset and acc.get("id") in idset)
                for sub in acc.get("sub_emails") or []:
                    if idset and not parent_selected and sub.get("id") not in idset:
                        continue
                    token = sub.get("receive_token") or ""
                    json_url = sub.get("receive_url") or receive_url_for_token(token)
                    ui_url = sub.get("receive_ui_url") or receive_ui_url_for_token(token)
                    # 邮箱----JSON接码地址----可视化接码地址
                    lines.append(f"{sub.get('email','')}----{json_url}----{ui_url}")
        elif category == "recovery":
            for acc in data.get("accounts") or []:
                if not acc.get("recovery_bound"):
                    continue
                if idset and acc.get("id") not in idset:
                    continue
                if not _country_ok(acc):
                    continue
                lines.append(
                    f"{acc.get('email','')}----{acc.get('password','')}----"
                    f"{acc.get('recovery_email','')}"
                )
        else:
            raise ValueError("category 必须是 registered / oauth2 / sub / recovery")
        return "\n".join(lines) + ("\n" if lines else "")


def batch_generate_sub_emails(ids: list[str], count: int = 1, tag_prefix: str = "") -> dict:
    results = []
    errors = []
    for aid in ids:
        try:
            results.append(generate_sub_emails(aid, count=count, tag_prefix=tag_prefix))
        except Exception as exc:
            errors.append({"id": aid, "error": str(exc)})
    return {
        "ok": len(errors) == 0,
        "success": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


def resolve_oauth_credentials(account_id: str) -> tuple[str, str, str, str]:
    """返回 (parent_id, client_id, refresh_token, email_for_filter_or_empty)."""
    acc = get_account(account_id, include_secrets=True)
    if not acc:
        raise ValueError("账号不存在")
    if acc.get("category") == "sub":
        parent = acc.get("parent") or {}
        return (
            str(acc.get("parent_id") or ""),
            str(parent.get("client_id") or ""),
            str(parent.get("refresh_token") or ""),
            str(acc.get("email") or ""),
        )
    if acc.get("category") == "oauth2" or acc.get("refresh_token"):
        return (
            str(acc.get("id") or ""),
            str(acc.get("client_id") or ""),
            str(acc.get("refresh_token") or ""),
            "",
        )
    raise ValueError("仅 OAuth2 / 子邮箱可操作")


def maybe_import_oauth2_file(data: dict | None = None) -> int:
    """把历史 Results/oauth2.txt 导入池（仅一次内存标记 + 按邮箱去重）。"""
    global _imported_oauth2
    own_lock = data is None
    if own_lock:
        _lock.acquire()
    try:
        if data is None:
            data = _load_raw()
        path = os.path.join(RESULTS_DIR, "oauth2.txt")
        if not os.path.isfile(path):
            return 0
        # 用文件 mtime+size 做轻量同步：每次 list 都补缺，不重复
        added = 0
        accounts = data.setdefault("accounts", [])
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("----")
                    if len(parts) < 4:
                        continue
                    email, password, client_id, refresh = parts[0].strip(), parts[1], parts[2], "----".join(parts[3:])
                    if not email or not refresh:
                        continue
                    existing = _find_by_email(accounts, email)
                    now = _now()
                    if existing:
                        if existing.get("category") != "oauth2" or not existing.get("refresh_token"):
                            existing["category"] = "oauth2"
                            existing["password"] = password or existing.get("password") or ""
                            existing["client_id"] = client_id or existing.get("client_id") or ""
                            existing["refresh_token"] = refresh
                            existing["oauth_status"] = existing.get("oauth_status") or "unknown"
                            existing["register_status"] = "mailbox_ok"
                            existing["updated_at"] = now
                            if "sub_emails" not in existing:
                                existing["sub_emails"] = []
                            added += 1
                        continue
                    accounts.append(
                        {
                            "id": _new_id("oauth"),
                            "category": "oauth2",
                            "email": email,
                            "password": password,
                            "client_id": client_id,
                            "refresh_token": refresh,
                            "register_status": "mailbox_ok",
                            "oauth_status": "unknown",
                            "sub_emails": [],
                            "created_at": now,
                            "updated_at": now,
                            "source": "oauth2.txt",
                        }
                    )
                    added += 1
        except Exception:
            return 0
        if added:
            _save_raw(data)
        _imported_oauth2 = True
        return added
    finally:
        if own_lock:
            _lock.release()


def import_registered_file() -> int:
    path = os.path.join(RESULTS_DIR, "registered.txt")
    if not os.path.isfile(path):
        return 0
    added = 0
    with _lock:
        data = _load_raw()
        accounts = data.setdefault("accounts", [])
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("----")
                if len(parts) < 2:
                    continue
                email, password = parts[0].strip(), parts[1]
                if _find_by_email(accounts, email):
                    continue
                now = _now()
                accounts.append(
                    {
                        "id": _new_id("reg"),
                        "category": "registered",
                        "email": email,
                        "password": password,
                        "register_status": "mailbox_ok",
                        "oauth_status": "none",
                        "client_id": "",
                        "refresh_token": "",
                        "sub_emails": [],
                        "created_at": now,
                        "updated_at": now,
                        "source": "registered.txt",
                    }
                )
                added += 1
        if added:
            _save_raw(data)
    return added
