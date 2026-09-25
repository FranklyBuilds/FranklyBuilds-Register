from __future__ import annotations

"""Mongo-backed Outlook mailbox-pool compatibility service.

This module replaces the legacy JSON pool for runtime operations.  The old
Results files remain migration inputs only; all generated sub-addresses,
receive tokens and OAuth-check state are persisted in MongoDB.
"""

import asyncio
import copy
import os
import re
import secrets
import string
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from .errors import ResourceNotFoundError
from .resource_service import MongoResourceStore, normalize_email, utc_now


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _base_url() -> str:
    configured = str(os.environ.get("PUBLIC_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    host = str(os.environ.get("HOST") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{str(os.environ.get('PORT') or '8000').strip()}"


def _random_tag(prefix: str = "") -> str:
    alphabet = string.ascii_lowercase + string.digits
    clean = re.sub(r"[^a-zA-Z0-9._-]", "", prefix or "")[:20]
    return f"{clean}{''.join(secrets.choice(alphabet) for _ in range(8))}" if clean else "".join(
        secrets.choice(alphabet) for _ in range(10)
    )


def _age_filter(value: Any, selector: str) -> bool:
    if not selector:
        return True
    if not value:
        return False
    try:
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        else:
            raw = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400
    except (TypeError, ValueError):
        return False
    if selector in {"older7", "older_than_7_days"}:
        return days >= 7
    if selector in {"within7", "within_7_days"}:
        return days < 7
    return True


def _status_bucket(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in {"ok", "valid", "alive", "good"}:
        return "ok"
    if value in {"expired", "error", "invalid", "bad"}:
        return "bad"
    return "unknown"


def _extract_codes(value: str) -> list[str]:
    result: list[str] = []
    for code in _CODE_RE.findall(value or ""):
        if code not in result:
            result.append(code)
    return result[:10]


class OutlookPoolService:
    """Runtime Outlook pool operations backed exclusively by MongoDB."""

    def __init__(self, resources: MongoResourceStore, outlook_store: Any, outlook_service: Any) -> None:
        self.resources = resources
        self.outlook_store = outlook_store
        self.outlook_service = outlook_service
        self.manager = resources.manager
        self._check_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._scheduler_stop = asyncio.Event()

    @property
    def sub_mailboxes(self) -> Any:
        return self.manager.database["outlook_sub_mailboxes"]

    @property
    def check_config(self) -> Any:
        return self.manager.database["outlook_oauth_check_config"]

    async def ensure_indexes(self) -> None:
        await self.resources._guard(
            self.sub_mailboxes.create_index("emailNormalized", unique=True, name="outlook_sub_email_unique")
        )
        await self.resources._guard(
            self.sub_mailboxes.create_index("receiveToken", unique=True, name="outlook_sub_receive_token_unique")
        )
        await self.resources._guard(
            self.sub_mailboxes.create_index(
                [("parentAccountId", 1), ("tag", 1)], unique=True, name="outlook_sub_parent_tag_unique"
            )
        )
        await self.resources._guard(self.sub_mailboxes.create_index([("parentAccountId", 1), ("createdAt", -1)], name="outlook_sub_parent_created"))
        await self.resources._guard(self.check_config.create_index("_id", name="outlook_oauth_check_config_id"))

    @staticmethod
    def _category(account: Mapping[str, Any]) -> str:
        if str(account.get("clientId") or "") and str(account.get("refreshToken") or ""):
            return "oauth2"
        return "registered"

    @staticmethod
    def _is_recovery(account: Mapping[str, Any]) -> bool:
        return bool(account.get("recoveryEmail") or account.get("recoveryBound"))

    @staticmethod
    def _public_parent(account: Mapping[str, Any], mailbox: Mapping[str, Any] | None = None) -> dict[str, Any]:
        category = OutlookPoolService._category(account)
        return {
            "id": str(account.get("_id") or ""),
            "category": category,
            "email": str(account.get("email") or ""),
            "passwordConfigured": bool(account.get("password")),
            "hasClientId": bool(account.get("clientId")),
            "hasRefreshToken": bool(account.get("refreshToken")),
            "oauthStatus": str(account.get("oauthStatus") or "unknown"),
            "graphStatus": str(account.get("graphStatus") or "unknown"),
            "oauthStatusBucket": _status_bucket(account.get("oauthStatus")),
            "poolStatus": str((mailbox or {}).get("status") or account.get("poolStatus") or "not_published"),
            "country": str(account.get("country") or ""),
            "recoveryEmailConfigured": OutlookPoolService._is_recovery(account),
            "recoveryEmail": str(account.get("recoveryEmail") or "") if OutlookPoolService._is_recovery(account) else None,
            "createdAt": account.get("createdAt"),
            "updatedAt": account.get("updatedAt"),
            "source": str(account.get("source") or "manual"),
            "lastError": str(account.get("lastError") or "")[:180] or None,
        }

    @staticmethod
    def _public_sub(row: Mapping[str, Any], parent: Mapping[str, Any] | None = None) -> dict[str, Any]:
        token = str(row.get("receiveToken") or "")
        return {
            "id": str(row.get("_id") or ""),
            "category": "sub",
            "email": str(row.get("email") or ""),
            "tag": str(row.get("tag") or ""),
            "parentId": str(row.get("parentAccountId") or ""),
            "parentEmail": str((parent or {}).get("email") or ""),
            "oauthStatus": str((parent or {}).get("oauthStatus") or "unknown"),
            "graphStatus": str((parent or {}).get("graphStatus") or "unknown"),
            "oauthStatusBucket": _status_bucket((parent or {}).get("oauthStatus")),
            "status": str(row.get("status") or "active"),
            "createdAt": row.get("createdAt"),
            "updatedAt": row.get("updatedAt"),
            "receiveUrl": f"{_base_url()}/api/outlook/pool/receive/{token}" if token else None,
            "receiveUiUrl": f"{_base_url()}/r/{token}" if token else None,
        }

    async def _parent_mailbox(self, account_id: str) -> dict[str, Any] | None:
        return await self.resources._guard(self.resources.emails.find_one({"outlookAccountId": account_id}))

    async def stats(self) -> dict[str, int]:
        parents = await self.resources._guard(
            self.outlook_store.accounts.find({}, {"clientId": 1, "refreshToken": 1, "oauthStatus": 1, "recoveryEmail": 1, "recoveryBound": 1}).to_list(length=None)
        )
        subs = await self.resources._guard(self.sub_mailboxes.count_documents({}))
        return {
            "registered": sum(1 for row in parents if self._category(row) == "registered"),
            "oauth2": sum(1 for row in parents if self._category(row) == "oauth2"),
            "recovery": sum(1 for row in parents if self._is_recovery(row)),
            "sub": int(subs),
            "oauth_ok": sum(1 for row in parents if self._category(row) == "oauth2" and row.get("oauthStatus") == "ok"),
            "oauth_bad": sum(1 for row in parents if self._category(row) == "oauth2" and _status_bucket(row.get("oauthStatus")) == "bad"),
            "oauth_unknown": sum(1 for row in parents if self._category(row) == "oauth2" and _status_bucket(row.get("oauthStatus")) == "unknown"),
            "total": len(parents),
        }

    async def list_accounts(
        self,
        *,
        category: str = "all",
        keyword: str = "",
        country: str = "",
        age: str = "",
        oauth_status: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        parents = await self.resources._guard(self.outlook_store.accounts.find({}).sort("updatedAt", -1).to_list(length=5000))
        parent_ids = [str(row.get("_id") or "") for row in parents]
        sub_rows = await self.resources._guard(
            self.sub_mailboxes.find({"parentAccountId": {"$in": parent_ids}}).sort("createdAt", -1).to_list(length=10000)
        ) if parent_ids else []
        by_parent = {str(row.get("_id")): row for row in parents}
        mailboxes = await self.resources._guard(
            self.resources.emails.find({"outlookAccountId": {"$in": parent_ids}}, {"outlookAccountId": 1, "status": 1}).to_list(length=len(parent_ids))
        ) if parent_ids else []
        mailbox_by_parent = {str(row.get("outlookAccountId")): row for row in mailboxes}
        items: list[dict[str, Any]] = []
        normalized_keyword = keyword.strip().casefold()
        for parent in parents:
            parent_category = self._category(parent)
            if category not in {"", "all", "none"}:
                if category == "recovery" and not self._is_recovery(parent):
                    continue
                if category in {"registered", "oauth2"} and category != parent_category:
                    continue
                if category == "sub":
                    continue
            if country and str(parent.get("country") or "").upper() != country.upper():
                continue
            if not _age_filter(parent.get("createdAt") or parent.get("updatedAt"), age):
                continue
            if oauth_status and _status_bucket(parent.get("oauthStatus")) != _status_bucket(oauth_status):
                continue
            public = self._public_parent(parent, mailbox_by_parent.get(str(parent.get("_id"))))
            if normalized_keyword and normalized_keyword not in public["email"].casefold():
                continue
            items.append(public)
            if category in {"", "all", "none", "sub"}:
                for sub in sub_rows:
                    if str(sub.get("parentAccountId")) != str(parent.get("_id")):
                        continue
                    sub_public = self._public_sub(sub, parent)
                    if normalized_keyword and normalized_keyword not in sub_public["email"].casefold():
                        continue
                    if oauth_status and sub_public["oauthStatusBucket"] != _status_bucket(oauth_status):
                        continue
                    if category == "sub":
                        items.append(sub_public)
                    elif category in {"", "all", "none"}:
                        # Parent rows remain the primary list; sub rows are exposed
                        # in the dedicated sub category to avoid double counting.
                        continue
        if category == "sub":
            items = [self._public_sub(row, by_parent.get(str(row.get("parentAccountId")))) for row in sub_rows]
            if normalized_keyword:
                items = [row for row in items if normalized_keyword in row["email"].casefold() or normalized_keyword in row["parentEmail"].casefold()]
            if oauth_status:
                items = [row for row in items if row["oauthStatusBucket"] == _status_bucket(oauth_status)]
        total = len(items)
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        start = (page - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size, "items": items[start:start + page_size], "stats": await self.stats()}

    async def resolve_parent_id(self, account_id: str) -> str:
        parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": account_id}, {"_id": 1}))
        if parent:
            return str(account_id)
        sub = await self.resources._guard(self.sub_mailboxes.find_one({"_id": account_id}, {"parentAccountId": 1}))
        if sub and sub.get("parentAccountId"):
            return str(sub["parentAccountId"])
        raise ResourceNotFoundError("Outlook 邮箱池账号不存在")

    async def get_sub(self, sub_id: str) -> dict[str, Any]:
        row = await self.resources._guard(self.sub_mailboxes.find_one({"_id": sub_id}))
        if not row:
            raise ResourceNotFoundError("Outlook 子邮箱不存在")
        parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": row.get("parentAccountId")}))
        if not parent:
            raise ResourceNotFoundError("Outlook 子邮箱对应的主账号不存在")
        return {"row": row, "parent": parent}

    async def get_detail(self, account_id: str) -> dict[str, Any]:
        parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": account_id}))
        if parent:
            mailbox = await self._parent_mailbox(account_id)
            subs = await self.resources._guard(self.sub_mailboxes.find({"parentAccountId": account_id}).sort("createdAt", -1).to_list(length=500))
            result = self._public_parent(parent, mailbox)
            result["subEmails"] = [self._public_sub(row, parent) for row in subs]
            return result
        sub = await self.resources._guard(self.sub_mailboxes.find_one({"_id": account_id}))
        if sub:
            parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": sub.get("parentAccountId")}))
            return self._public_sub(sub, parent)
        raise ResourceNotFoundError("Outlook 邮箱池账号不存在")

    async def generate_sub_emails(self, account_id: str, *, count: int = 1, tag_prefix: str = "") -> dict[str, Any]:
        count = max(1, min(int(count or 1), 50))
        parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": account_id}))
        if not parent:
            raise ResourceNotFoundError("Outlook 主账号不存在")
        if not parent.get("clientId") or not parent.get("refreshToken"):
            raise ValueError("主账号缺少 Client ID 或 Refresh Token，无法生成接码地址")
        email = normalize_email(str(parent.get("email") or ""))
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError("主邮箱格式无效")
        local, domain = email.split("@", 1)
        local = local.split("+", 1)[0]
        existing = await self.resources._guard(self.sub_mailboxes.find({"parentAccountId": account_id}, {"tag": 1, "emailNormalized": 1}).to_list(length=5000))
        tags = {str(row.get("tag") or "").casefold() for row in existing}
        addresses = {str(row.get("emailNormalized") or "").casefold() for row in existing}
        created: list[dict[str, Any]] = []
        for _ in range(count):
            for _attempt in range(40):
                tag = _random_tag(tag_prefix)
                sub_email = f"{local}+{tag}@{domain}"
                normalized = normalize_email(sub_email)
                token = secrets.token_urlsafe(24)
                if tag.casefold() in tags or normalized in addresses:
                    continue
                row = {
                    "_id": str(uuid4()),
                    "parentAccountId": account_id,
                    "email": sub_email,
                    "emailNormalized": normalized,
                    "tag": tag,
                    "receiveToken": token,
                    "status": "active",
                    "source": "generated",
                    "createdAt": utc_now(),
                    "updatedAt": utc_now(),
                }
                try:
                    await self.resources._guard(self.sub_mailboxes.insert_one(row))
                except DuplicateKeyError:
                    continue
                tags.add(tag.casefold())
                addresses.add(normalized)
                created.append(self._public_sub(row, parent))
                break
        return {"account_id": account_id, "parent_email": email, "created": created, "sub_count": len(existing) + len(created)}

    async def batch_generate_sub_emails(self, ids: Iterable[str], *, count: int = 1, tag_prefix: str = "") -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for account_id in dict.fromkeys(str(value).strip() for value in ids if str(value).strip()):
            try:
                results.append(await self.generate_sub_emails(account_id, count=count, tag_prefix=tag_prefix))
            except Exception as exc:
                errors.append({"id": account_id, "error": "outlook_sub_generation_failed"})
        return {"ok": not errors, "success": len(results), "failed": len(errors), "results": results, "errors": errors}

    async def delete_ids(self, ids: Iterable[str]) -> dict[str, int]:
        deleted = 0
        for item_id in dict.fromkeys(str(value).strip() for value in ids if str(value).strip()):
            parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": item_id}))
            if parent:
                mailbox = await self._parent_mailbox(item_id)
                if mailbox and mailbox.get("status") in {"reserved", "assigned"}:
                    continue
                if mailbox:
                    await self.resources._guard(self.resources.emails.delete_one({"_id": mailbox.get("_id"), "status": {"$in": ["available", "unavailable"]}}))
                await self.resources._guard(self.sub_mailboxes.delete_many({"parentAccountId": item_id}))
                result = await self.resources._guard(self.outlook_store.accounts.delete_one({"_id": item_id}))
                deleted += int(getattr(result, "deleted_count", 0) or 0)
                continue
            result = await self.resources._guard(self.sub_mailboxes.delete_one({"_id": item_id}))
            deleted += int(getattr(result, "deleted_count", 0) or 0)
        return {"deleted": deleted}

    async def export_text(self, category: str, ids: Iterable[str] | None = None, country: str | None = None) -> str:
        selected = {str(value) for value in ids or [] if str(value)}
        parents = await self.resources._guard(self.outlook_store.accounts.find({}).sort("emailNormalized", 1).to_list(length=10000))
        if country:
            parents = [row for row in parents if str(row.get("country") or "").upper() == country.upper()]
        lines: list[str] = []
        for parent in parents:
            parent_id = str(parent.get("_id") or "")
            if selected and parent_id not in selected:
                continue
            parent_category = self._category(parent)
            if category == "registered" and parent_category == "registered":
                lines.append(f"{parent.get('email','')}----{parent.get('password','')}")
            elif category == "oauth2" and parent_category == "oauth2":
                lines.append(f"{parent.get('email','')}----{parent.get('password','')}----{parent.get('clientId','')}----{parent.get('refreshToken','')}")
            elif category == "recovery" and self._is_recovery(parent):
                lines.append(f"{parent.get('email','')}----{parent.get('password','')}----{parent.get('recoveryEmail','')}")
        if category == "sub":
            query: dict[str, Any] = {}
            if selected:
                query["$or"] = [{"_id": {"$in": list(selected)}}, {"parentAccountId": {"$in": list(selected)}}]
            subs = await self.resources._guard(self.sub_mailboxes.find(query).sort("createdAt", 1).to_list(length=10000))
            for row in subs:
                token = str(row.get("receiveToken") or "")
                lines.append(f"{row.get('email','')}----{_base_url()}/api/outlook/pool/receive/{token}----{_base_url()}/r/{token}")
        if category not in {"registered", "oauth2", "sub", "recovery"}:
            raise ValueError("category 必须是 registered / oauth2 / sub / recovery")
        return "\n".join(lines) + ("\n" if lines else "")

    async def get_oauth_check_config(self) -> dict[str, Any]:
        row = await self.resources._guard(self.check_config.find_one({"_id": "default"}))
        if row:
            result = copy.deepcopy(row)
            result.pop("_id", None)
            return result
        result = {"enabled": False, "interval_sec": 3600, "delay_ms": 200, "last_run_at": None, "last_result": None, "running": False}
        await self.resources._guard(self.check_config.update_one({"_id": "default"}, {"$set": result}, upsert=True))
        return result

    async def update_oauth_check_config(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        current = await self.get_oauth_check_config()
        if "enabled" in updates and updates["enabled"] is not None:
            current["enabled"] = bool(updates["enabled"])
        if "interval_sec" in updates and updates["interval_sec"] is not None:
            current["interval_sec"] = max(300, min(int(updates["interval_sec"]), 604800))
        if "delay_ms" in updates and updates["delay_ms"] is not None:
            current["delay_ms"] = max(0, min(int(updates["delay_ms"]), 5000))
        await self.resources._guard(self.check_config.update_one({"_id": "default"}, {"$set": current}, upsert=True))
        if current["enabled"]:
            self.start_scheduler()
        else:
            await self.stop_scheduler()
        return await self.get_oauth_check_config()

    async def run_oauth_check(self, ids: Iterable[str] | None = None, *, source: str = "manual") -> dict[str, Any]:
        if self._check_lock.locked():
            config = await self.get_oauth_check_config()
            return {"ok": False, "started": False, "running": True, "error": "已有 Outlook OAuth 检查任务在运行", "last_result": config.get("last_result")}
        async with self._check_lock:
            config = await self.get_oauth_check_config()
            await self.resources._guard(self.check_config.update_one({"_id": "default"}, {"$set": {"running": True}}, upsert=True))
            started = utc_now()
            requested_ids = list(dict.fromkeys(str(value).strip() for value in ids or [] if str(value).strip()))
            parent_ids = []
            for requested_id in requested_ids:
                try:
                    parent_ids.append(await self.resolve_parent_id(requested_id))
                except ResourceNotFoundError:
                    parent_ids.append(requested_id)
            parent_ids = list(dict.fromkeys(parent_ids))
            if not parent_ids:
                candidates = await self.resources._guard(self.outlook_store.accounts.find({"clientId": {"$exists": True, "$nin": [""]}, "refreshToken": {"$exists": True, "$nin": [""]}}, {"_id": 1, "email": 1}).sort("updatedAt", 1).to_list(length=5000))
                parent_ids = [str(row.get("_id")) for row in candidates]
            results: list[dict[str, Any]] = []
            ok_count = failed_count = 0
            for index, parent_id in enumerate(parent_ids):
                try:
                    result = await self.outlook_service.check_oauth(parent_id)
                    row = {"id": parent_id, **result}
                    ok_count += int(bool(result.get("ok")))
                    failed_count += int(not result.get("ok"))
                except Exception as exc:
                    row = {"id": parent_id, "ok": False, "error": "outlook_oauth_check_failed"}
                    failed_count += 1
                results.append(row)
                if index + 1 < len(parent_ids) and int(config.get("delay_ms") or 0) > 0:
                    await asyncio.sleep(int(config["delay_ms"]) / 1000)
            summary = {"ok": failed_count == 0, "source": source, "started_at": started, "finished_at": utc_now(), "total": len(results), "ok_count": ok_count, "failed_count": failed_count, "results": results}
            await self.resources._guard(self.check_config.update_one({"_id": "default"}, {"$set": {"running": False, "last_run_at": summary["finished_at"], "last_result": summary}}, upsert=True))
            return summary

    def start_scheduler(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_stop = asyncio.Event()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="outlook-oauth-check")

    async def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            config = await self.get_oauth_check_config()
            try:
                await asyncio.wait_for(self._scheduler_stop.wait(), timeout=max(1, int(config.get("interval_sec") or 3600)))
                return
            except TimeoutError:
                pass
            config = await self.get_oauth_check_config()
            if config.get("enabled") and not self._check_lock.locked():
                try:
                    await self.run_oauth_check(source="scheduled")
                except Exception:
                    pass

    async def stop_scheduler(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
        self._scheduler_task = None

    async def receive_payload(self, token: str, *, folder: str = "inbox", top: int = 10) -> dict[str, Any]:
        row = await self.resources._guard(self.sub_mailboxes.find_one({"receiveToken": token, "status": "active"}))
        if not row:
            raise ResourceNotFoundError("接码地址无效")
        parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": row.get("parentAccountId")}))
        if not parent:
            raise ResourceNotFoundError("接码地址对应的 Outlook 账号不存在")
        result = await self.outlook_service.messages(str(parent.get("_id")), folder=folder, top=top, recipient=str(row.get("email") or ""))
        codes: list[str] = []
        for message in result.get("messages") or []:
            extracted = _extract_codes(f"{message.get('subject','')} {message.get('preview','')}")
            message["codes"] = extracted
            for code in extracted:
                if code not in codes:
                    codes.append(code)
        return {"ok": True, "email": row.get("email"), "parent_email": parent.get("email"), "receive_url": f"{_base_url()}/api/outlook/pool/receive/{token}", "receive_ui_url": f"{_base_url()}/r/{token}", "messages": result.get("messages") or [], "codes": codes, "latest_code": codes[0] if codes else None}

    async def receive_message(self, token: str, message_id: str) -> dict[str, Any]:
        row = await self.resources._guard(self.sub_mailboxes.find_one({"receiveToken": token, "status": "active"}))
        if not row:
            raise ResourceNotFoundError("接码地址无效")
        parent = await self.resources._guard(self.outlook_store.accounts.find_one({"_id": row.get("parentAccountId")}))
        if not parent:
            raise ResourceNotFoundError("接码地址对应的 Outlook 账号不存在")
        result = await self.outlook_service.message_detail(str(parent.get("_id")), message_id)
        message = result.get("message") or {}
        codes = _extract_codes(f"{message.get('subject','')} {message.get('preview','')} {message.get('body','')}")
        message["codes"] = codes
        return {"ok": True, "email": row.get("email"), "message": message}
