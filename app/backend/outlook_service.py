from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pymongo.errors import DuplicateKeyError

from .errors import ResourceNotFoundError
from .mailbox_client import MailboxClient, MailboxClientError, MailboxSnapshot, parse_mail_datetime, parse_mailbox_snapshot
from .resource_service import MongoResourceStore, normalize_email, utc_now

RESULTS_ROOT = Path(__file__).resolve().parents[1] / "outlook_register" / "Results"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _public_account(
    document: dict[str, Any], email_document: dict[str, Any] | None = None
) -> dict[str, Any]:
    if email_document:
        pool_status = str(email_document.get("status") or "not_published")
    else:
        pool_status = "conflict" if document.get("poolStatus") == "conflict" else "not_published"
    return {
        "id": str(document.get("_id") or ""),
        "email": str(document.get("email") or ""),
        "oauthStatus": str(document.get("oauthStatus") or "unknown"),
        "oauthCheckedAt": document.get("oauthCheckedAt"),
        "graphStatus": str(document.get("graphStatus") or "unknown"),
        "graphCheckedAt": document.get("graphCheckedAt"),
        "lastError": str(document.get("lastError") or "")[:180] or None,
        "poolStatus": pool_status,
        "hasClientId": bool(document.get("clientId")),
        "hasRefreshToken": bool(document.get("refreshToken")),
        "outlookEmailId": str(email_document.get("_id")) if email_document else None,
        "importedAt": document.get("createdAt") or utc_now(),
        "source": str(document.get("source") or "manual"),
    }


class OutlookAccountImport(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(default="", max_length=1024)
    clientId: str = Field(default="", max_length=256, validation_alias=AliasChoices("clientId", "client_id"))
    refreshToken: str = Field(default="", max_length=16384, validation_alias=AliasChoices("refreshToken", "refresh_token"))


class OutlookImportBody(BaseModel):
    accounts: list[OutlookAccountImport] = Field(default_factory=list, max_length=5000)


class OutlookEditBody(BaseModel):
    password: str | None = Field(default=None, max_length=1024)
    clientId: str | None = Field(default=None, max_length=256)
    refreshToken: str | None = Field(default=None, max_length=16384)


class OutlookExportBody(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=5000)


class OutlookPoolSubGenerateBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    count: int = Field(default=1, ge=1, le=50)
    tag_prefix: str = Field(default="", validation_alias=AliasChoices("tag_prefix", "tagPrefix"), max_length=20)


class OutlookPoolBatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    ids: list[str] = Field(default_factory=list, max_length=5000)
    count: int = Field(default=1, ge=1, le=50)
    tag_prefix: str = Field(default="", validation_alias=AliasChoices("tag_prefix", "tagPrefix"), max_length=20)


class OutlookPoolDeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=5000)


class OutlookPoolExportBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    category: str = Field(min_length=1, max_length=20)
    ids: list[str] | None = Field(default=None, max_length=5000)
    country: str | None = Field(default=None, max_length=2)


class OutlookOAuthCheckBody(BaseModel):
    enabled: bool | None = None
    interval_sec: int | None = Field(default=None, ge=300, le=604800)
    delay_ms: int | None = Field(default=None, ge=0, le=5000)


class GraphCheckResponse(BaseModel):
    ok: bool
    oauthStatus: str
    graphStatus: str
    poolStatus: str = "not_published"
    error: str | None = None


class OutlookProxyView(BaseModel):
    id: str
    host: str
    port: int
    enabled: bool
    status: str
    latencyMs: int | None = None
    country: str
    group: str
    scheme: str


class OutlookStore:
    def __init__(self, resources: MongoResourceStore) -> None:
        self.resources = resources
        self.manager = resources.manager

    @property
    def accounts(self) -> Any:
        return self.manager.database["outlook_accounts"]

    async def ensure_indexes(self) -> None:
        await self.resources._guard(
            self.accounts.create_index(
                [("emailNormalized", 1)], unique=True, name="outlook_email_unique"
            )
        )
        await self.resources._guard(
            self.accounts.create_index(
                [("oauthStatus", 1), ("graphStatus", 1)], name="outlook_validation_state"
            )
        )

    async def get(self, account_id: str) -> dict[str, Any]:
        document = await self.resources._guard(self.accounts.find_one({"_id": account_id}))
        if document is None:
            raise ResourceNotFoundError("Outlook 账号不存在")
        return document

    async def result_accounts(self, *, limit: int = 100, query: str = "") -> list[dict[str, Any]]:
        match: dict[str, Any] = {}
        if query.strip():
            match["emailNormalized"] = {"$regex": re.escape(query.strip().casefold())}
        bounded_limit = max(1, min(limit, 500))
        documents = await self.resources._guard(
            self.accounts.find(
                match,
                {"email": 1, "oauthStatus": 1, "graphStatus": 1, "createdAt": 1},
            ).sort("createdAt", -1).limit(bounded_limit).to_list(length=bounded_limit)
        )
        return [
            {
                "email": str(item.get("email") or ""),
                "oauthStatus": str(item.get("oauthStatus") or "unknown"),
                "graphStatus": str(item.get("graphStatus") or "unknown"),
                "createdAt": item.get("createdAt"),
            }
            for item in documents
        ]

    async def registration_candidates(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return credential-bearing accounts for the internal task worker."""
        bounded_limit = max(1, min(int(limit), 5000))
        cursor = (
            self.accounts.find(
                {
                    "clientId": {"$exists": True, "$nin": [""]},
                    "refreshToken": {"$exists": True, "$nin": [""]},
                },
                {
                    "email": 1,
                    "clientId": 1,
                    "refreshToken": 1,
                    "oauthStatus": 1,
                    "graphStatus": 1,
                },
            )
            .sort("updatedAt", 1)
            .limit(bounded_limit)
        )
        return await self.resources._guard(cursor.to_list(length=bounded_limit))

    async def list_accounts(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        query: str = "",
        source: str = "all",
        pool_status: str = "all",
    ) -> dict[str, Any]:
        match: dict[str, Any] = {}
        if query.strip():
            match["emailNormalized"] = {"$regex": re.escape(query.strip().casefold())}
        if source in {"migration", "registration", "manual"}:
            match["source"] = source
        if pool_status in {"available", "reserved", "assigned", "unavailable"}:
            matched_accounts = await self.resources._guard(
                self.resources.emails.distinct(
                    "outlookAccountId", {"sourceType": "outlook", "status": pool_status}
                )
            )
            match["_id"] = {"$in": matched_accounts}
        elif pool_status == "conflict":
            match["poolStatus"] = "conflict"
        elif pool_status == "not_published":
            match["$and"] = [
                *match.pop("$and", []),
                {"$or": [
                    {"poolStatus": {"$exists": False}},
                    {"poolStatus": "not_published"},
                    {"poolStatus": "conflict"},
                ]},
            ]
        total = await self.resources._guard(self.accounts.count_documents(match))
        cursor = (
            self.accounts.find(
                match,
                {
                    "email": 1,
                    "oauthStatus": 1,
                    "oauthCheckedAt": 1,
                    "graphStatus": 1,
                    "graphCheckedAt": 1,
                    "lastError": 1,
                    "poolStatus": 1,
                    "createdAt": 1,
                    "source": 1,
                },
            )
            .sort("createdAt", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self.resources._guard(cursor.to_list(length=page_size))
        account_ids = [str(item.get("_id")) for item in documents]
        pool_rows: dict[str, dict[str, Any]] = {}
        if account_ids:
            rows = await self.resources._guard(
                self.resources.emails.find(
                    {"outlookAccountId": {"$in": account_ids}},
                    {"outlookAccountId": 1, "status": 1, "_id": 1},
                ).to_list(length=len(account_ids))
            )
            pool_rows = {str(row.get("outlookAccountId")): row for row in rows}
        items = [
            _public_account(item, pool_rows.get(str(item.get("_id"))))
            for item in documents
        ]
        return {"items": items, "total": total, "page": page, "pageSize": page_size}

    async def upsert_account(
        self,
        *,
        email: str,
        password: str = "",
        client_id: str = "",
        refresh_token: str = "",
        source: str = "manual",
        oauth_status: str = "unknown",
        graph_status: str = "unknown",
        recovery_email: str = "",
        recovery_bound: bool = False,
        country: str = "",
        proxy: str = "",
    ) -> tuple[str, bool]:
        normalized = normalize_email(email)
        if not EMAIL_RE.fullmatch(normalized):
            raise ValueError("Outlook 邮箱格式无效")
        now = utc_now()
        account_id = str(uuid4())
        document: dict[str, Any] = {
            "_id": account_id,
            "email": normalized,
            "emailNormalized": normalized,
            "password": str(password or ""),
            "clientId": str(client_id or ""),
            "refreshToken": str(refresh_token or ""),
            "oauthStatus": oauth_status if refresh_token else "missing",
            "oauthCheckedAt": None,
            "graphStatus": graph_status if refresh_token else "unknown",
            "graphCheckedAt": None,
            "lastError": None,
            "poolStatus": "not_published",
            "source": source,
            "recoveryEmail": str(recovery_email or ""),
            "recoveryBound": bool(recovery_bound),
            "country": str(country or "").strip().upper(),
            "registrationProxy": str(proxy or ""),
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            result = await self.resources._guard(self.accounts.update_one({"emailNormalized": normalized}, {"$setOnInsert": document}, upsert=True))
        except DuplicateKeyError:
            result = None
        if result is not None and result.upserted_id is not None:
            return account_id, True
        existing = await self.resources._guard(self.accounts.find_one({"emailNormalized": normalized}))
        if existing is None:
            raise RuntimeError("Outlook 账号导入后无法读取")
        updates: dict[str, Any] = {"updatedAt": now}
        # Imports and legacy migration may be replayed against live records. Fill
        # missing credentials, but never let a stale duplicate overwrite the
        # currently stored OAuth material; intentional changes use the PATCH API.
        missing_auth_fields: dict[str, str] = {}
        if not existing.get("clientId") and client_id:
            missing_auth_fields["clientId"] = str(client_id)
        if not existing.get("refreshToken") and refresh_token:
            missing_auth_fields["refreshToken"] = str(refresh_token)
        if missing_auth_fields:
            updates.update(missing_auth_fields)
            has_refresh_token = bool(existing.get("refreshToken") or missing_auth_fields.get("refreshToken"))
            has_client_id = bool(existing.get("clientId") or missing_auth_fields.get("clientId"))
            updates.update({
                "oauthStatus": "unknown" if has_refresh_token and has_client_id else "missing",
                "graphStatus": "unknown",
                "oauthCheckedAt": None,
                "graphCheckedAt": None,
                "poolStatus": "not_published",
            })
            await self.resources._guard(
                self.resources.emails.update_one(
                    {
                        "outlookAccountId": str(existing["_id"]),
                        "status": "available",
                    },
                    {
                        "$set": {
                            "status": "unavailable",
                            "unavailableReason": "Outlook credentials changed; validation required",
                            "updatedAt": now,
                        },
                    },
                )
            )
        if not existing.get("password") and password:
            updates["password"] = str(password)
        if recovery_email and not existing.get("recoveryEmail"):
            updates["recoveryEmail"] = str(recovery_email)
        if recovery_bound and not existing.get("recoveryBound"):
            updates["recoveryBound"] = True
        if country and not existing.get("country"):
            updates["country"] = str(country).strip().upper()
        if proxy and not existing.get("registrationProxy"):
            updates["registrationProxy"] = str(proxy)
        await self.resources._guard(self.accounts.update_one({"_id": existing["_id"]}, {"$set": updates}))
        return str(existing["_id"]), False

    async def store_registration_result(
        self,
        *,
        email: str,
        password: str = "",
        client_id: str = "",
        refresh_token: str = "",
        oauth: bool = False,
        recovery_bound: bool = False,
        recovery_email: str = "",
        country: str = "",
        proxy: str = "",
    ) -> tuple[str, bool]:
        """Persist a legacy registration-engine result in Mongo.

        This is the runtime sink for the integrated engine.  The legacy
        Results files are intentionally not involved here.
        """
        return await self.upsert_account(
            email=email,
            password=password,
            client_id=client_id,
            refresh_token=refresh_token,
            source="registration",
            oauth_status="ok" if oauth and refresh_token else ("missing" if not refresh_token else "unknown"),
            graph_status="unknown",
            recovery_email=recovery_email,
            recovery_bound=recovery_bound,
            country=country,
            proxy=proxy,
        )

    async def update_validation(
        self,
        account_id: str,
        *,
        oauth_status: str | None = None,
        graph_status: str | None = None,
        refresh_token: str | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        changes: dict[str, Any] = {
            "updatedAt": now,
            "lastError": (error or "")[:180] or None,
        }
        if oauth_status is not None:
            changes.update({"oauthStatus": oauth_status, "oauthCheckedAt": now})
        if graph_status is not None:
            changes.update({"graphStatus": graph_status, "graphCheckedAt": now})
        if refresh_token:
            changes["refreshToken"] = refresh_token
            changes["oauthStatus"] = "ok"
            changes["oauthCheckedAt"] = now
        await self.resources._guard(
            self.accounts.update_one({"_id": account_id}, {"$set": changes})
        )
        account = await self.get(account_id)
        validation_failed = bool(
            (oauth_status is not None and oauth_status != "ok")
            or (graph_status is not None and graph_status != "ok")
        ) and (
            account.get("oauthStatus") != "ok"
            or account.get("graphStatus") != "ok"
            or not account.get("refreshToken")
            or not account.get("clientId")
        )
        if validation_failed:
            await self.resources._guard(
                self.resources.emails.update_many(
                    {
                        "outlookAccountId": account_id,
                        "status": "available",
                    },
                    {
                        "$set": {
                            "status": "unavailable",
                            "unavailableReason": "OAuth or Graph validation pending",
                            "updatedAt": now,
                        },
                    },
                )
            )
            await self.resources._guard(
                self.accounts.update_one(
                    {"_id": account_id},
                    {"$set": {"poolStatus": "not_published"}},
                )
            )
        # Pool publication is an explicit check_graph responsibility. This
        # prevents status-only updates from bypassing the /me identity check.

    async def publish_if_valid(self, account_id: str) -> str:
        account = await self.get(account_id)
        if (
            account.get("oauthStatus") != "ok"
            or account.get("graphStatus") != "ok"
            or not account.get("refreshToken")
            or not account.get("clientId")
        ):
            return "not_published"
        existing = await self.resources._guard(
            self.resources.emails.find_one({"emailNormalized": account["emailNormalized"]})
        )
        if existing:
            if existing.get("sourceType") == "outlook" and existing.get("outlookAccountId") == account_id:
                status = str(existing.get("status") or "available")
                if status == "unavailable":
                    await self.resources._guard(
                        self.resources.emails.update_one(
                            {"_id": existing["_id"], "status": "unavailable"},
                            {"$set": {"status": "available", "updatedAt": utc_now()}, "$unset": {"unavailableReason": ""}},
                        )
                    )
                    status = "available"
                await self.resources._guard(self.accounts.update_one({"_id": account_id}, {"$set": {"poolStatus": "published"}}))
                return status
            # Never take over an unrelated manual or MailCom mailbox that
            # happens to use the same address. Preserve that resource intact
            # and surface the collision for an explicit operator decision.
            await self.resources._guard(self.accounts.update_one({"_id": account_id}, {"$set": {"poolStatus": "conflict"}}))
            return "conflict"
        now = utc_now()
        mailbox = {
            "_id": f"outlook:{account_id}",
            "email": account["email"],
            "emailNormalized": account["emailNormalized"],
            "accessUrl": f"outlook://{account_id}",
            "importedAt": now,
            "updatedAt": now,
            "status": "available",
            "mailboxKind": "outlook_graph",
            "sourceType": "outlook",
            "outlookAccountId": account_id,
        }
        try:
            await self.resources._guard(self.resources.emails.insert_one(mailbox))
        except DuplicateKeyError:
            existing = await self.resources._guard(self.resources.emails.find_one({"emailNormalized": account["emailNormalized"]}))
            if existing and existing.get("sourceType") == "outlook" and existing.get("outlookAccountId") == account_id:
                status = str(existing.get("status") or "available")
                if status == "unavailable":
                    await self.resources._guard(
                        self.resources.emails.update_one(
                            {"_id": existing["_id"], "status": "unavailable"},
                            {"$set": {"status": "available", "updatedAt": utc_now()}, "$unset": {"unavailableReason": ""}},
                        )
                    )
                    status = "available"
                await self.resources._guard(self.accounts.update_one({"_id": account_id}, {"$set": {"poolStatus": "published"}}))
                return status
            # Never take over an unrelated manual or MailCom mailbox that
            # happens to use the same address. Preserve that resource intact
            # and surface the collision for an explicit operator decision.
            await self.resources._guard(self.accounts.update_one({"_id": account_id}, {"$set": {"poolStatus": "conflict"}}))
            return "conflict"
        await self.resources._guard(self.accounts.update_one({"_id": account_id}, {"$set": {"poolStatus": "published"}}))
        return "available"

    async def store_migration(self, summary: dict[str, Any]) -> None:
        safe_summary = {
            "sources": list(summary.get("sources") or []),
            "sourceCount": int(summary.get("sourceCount") or 0),
            "parsedAccounts": int(summary.get("parsedAccounts") or 0),
            "imported": int(summary.get("imported") or 0),
            "duplicates": int(summary.get("duplicates") or 0),
            "errors": int(summary.get("errors") or 0),
            "backupPaths": [Path(str(value)).name for value in summary.get("backupPaths") or []],
            "completedAt": summary.get("completedAt") or utc_now(),
        }
        await self.resources._guard(
            self.manager.database["outlook_migrations"].update_one(
                {"_id": "legacy-outlook-v1"},
                {"$set": {"summary": safe_summary, "lastRunAt": utc_now()}},
                upsert=True,
            )
        )


class OutlookService:
    def __init__(self, store: OutlookStore, *, http_client_factory: Any = httpx.AsyncClient) -> None:
        self.store = store
        self.http_client_factory = http_client_factory

    @staticmethod
    def _proxy_url(proxy: Mapping[str, Any] | None = None) -> str | None:
        if not proxy:
            return None
        scheme = str(proxy.get("scheme") or "http").strip().lower()
        if scheme == "socks5h":
            scheme = "socks5"
        host = str(proxy.get("host") or "").strip()
        try:
            port = int(proxy.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not host or not 1 <= port <= 65535:
            return None
        username = quote(str(proxy.get("username") or ""), safe="")
        password = quote(str(proxy.get("password") or ""), safe="")
        auth = f"{username}:{password}@" if username or password else ""
        return f"{scheme}://{auth}{host}:{port}"

    @classmethod
    def _proxy_from_account(cls, account: Mapping[str, Any]) -> dict[str, Any] | None:
        raw = str(account.get("registrationProxy") or "").strip()
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

    async def _refresh(self, account: dict[str, Any], *, proxy: Mapping[str, Any] | None = None) -> dict[str, Any]:
        refresh = str(account.get("refreshToken") or "")
        client_id = str(account.get("clientId") or "")
        if not refresh or not client_id:
            raise PermissionError("missing")
        client_options: dict[str, Any] = {"timeout": 25, "trust_env": False}
        proxy_url = self._proxy_url(proxy)
        if proxy_url:
            client_options["proxy"] = proxy_url
        async with self.http_client_factory(**client_options) as client:
            response = await client.post(TOKEN_URL, data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": "https://graph.microsoft.com/.default offline_access",
            })
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("OAuth token endpoint returned invalid response") from None
        if not isinstance(payload, dict) or response.status_code != 200 or not payload.get("access_token"):
            oauth_error = str(payload.get("error") or "") if isinstance(payload, dict) else ""
            status = "expired" if oauth_error == "invalid_grant" else "error"
            raise PermissionError(status)
        return {"access_token": str(payload["access_token"]), "refresh_token": str(payload.get("refresh_token") or refresh)}

    async def _graph_get(self, path: str, token: str, params: dict[str, Any] | None = None, *, proxy: Mapping[str, Any] | None = None) -> dict[str, Any]:
        client_options: dict[str, Any] = {"timeout": 25, "trust_env": False}
        proxy_url = self._proxy_url(proxy)
        if proxy_url:
            client_options["proxy"] = proxy_url
        async with self.http_client_factory(**client_options) as client:
            response = await client.get(
                f"{GRAPH_BASE}{path}",
                headers={"Authorization": f"Bearer {token}", "Prefer": "outlook.body-content-type='text'"},
                params=params,
            )
        if response.status_code == 401:
            raise PermissionError("expired")
        if response.status_code != 200:
            raise RuntimeError(f"Microsoft Graph returned HTTP {response.status_code}")
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def check_oauth(self, account_id: str, *, proxy: Mapping[str, Any] | None = None) -> dict[str, Any]:
        account = await self.store.get(account_id)
        proxy = proxy or self._proxy_from_account(account)
        try:
            tokens = await self._refresh(account, proxy=proxy)
        except PermissionError as exc:
            status = str(exc) if str(exc) in {"expired", "error"} else "error"
            message = "OAuth token expired" if status == "expired" else "OAuth refresh failed"
            await self.store.update_validation(account_id, oauth_status=status, graph_status="unknown", error=message)
            return {"ok": False, "oauthStatus": status, "graphStatus": "unknown", "poolStatus": "not_published", "error": message}
        except Exception:
            await self.store.update_validation(account_id, oauth_status="error", graph_status="unknown", error="OAuth request failed")
            return {"ok": False, "oauthStatus": "error", "graphStatus": "unknown", "poolStatus": "not_published", "error": "OAuth request failed"}
        # Refreshing OAuth must not erase a previously verified Graph identity.
        # A newly edited credential pair already resets both states in PATCH.
        await self.store.update_validation(
            account_id,
            oauth_status="ok",
            refresh_token=tokens["refresh_token"],
            error=None,
        )
        current = await self.store.get(account_id)
        mailbox = await self.store.resources._guard(
            self.store.resources.emails.find_one(
                {"sourceType": "outlook", "outlookAccountId": account_id},
                {"status": 1},
            )
        )
        return {
            "ok": True,
            "oauthStatus": "ok",
            "graphStatus": str(current.get("graphStatus") or "unknown"),
            "poolStatus": str(mailbox.get("status")) if mailbox else "not_published",
        }

    async def check_graph(self, account_id: str, *, proxy: Mapping[str, Any] | None = None) -> dict[str, Any]:
        account = await self.store.get(account_id)
        proxy = proxy or self._proxy_from_account(account)
        try:
            tokens = await self._refresh(account, proxy=proxy)
        except PermissionError as exc:
            oauth_status = "expired" if str(exc) == "expired" else "error"
            message = "OAuth token expired" if oauth_status == "expired" else "OAuth refresh failed"
            await self.store.update_validation(
                account_id, oauth_status=oauth_status, graph_status="unknown", error=message
            )
            return {
                "ok": False, "oauthStatus": oauth_status, "graphStatus": "unknown",
                "poolStatus": "not_published", "error": message,
            }
        except Exception:
            await self.store.update_validation(
                account_id, oauth_status="error", graph_status="unknown",
                error="OAuth request failed",
            )
            return {
                "ok": False, "oauthStatus": "error", "graphStatus": "unknown",
                "poolStatus": "not_published", "error": "OAuth request failed",
            }

        try:
            identity = await self._graph_get(
                "/me", tokens["access_token"], {"$select": "id,mail,userPrincipalName"}, proxy=proxy
            )
            addresses = {
                normalize_email(str(identity.get(field) or ""))
                for field in ("mail", "userPrincipalName")
                if str(identity.get(field) or "").strip()
            }
            if not addresses or normalize_email(str(account.get("email") or "")) not in addresses:
                await self.store.update_validation(
                    account_id, oauth_status="ok", graph_status="error",
                    refresh_token=tokens["refresh_token"],
                    error="Microsoft Graph identity does not match the Outlook account",
                )
                return {
                    "ok": False, "oauthStatus": "ok", "graphStatus": "error",
                    "poolStatus": "not_published",
                    "error": "Graph account identity does not match this Outlook address",
                }
        except PermissionError:
            await self.store.update_validation(
                account_id, oauth_status="expired", graph_status="unknown",
                error="OAuth token expired",
            )
            return {
                "ok": False, "oauthStatus": "expired", "graphStatus": "unknown",
                "poolStatus": "not_published", "error": "OAuth token expired",
            }
        except Exception:
            await self.store.update_validation(
                account_id, oauth_status="ok", graph_status="error",
                refresh_token=tokens["refresh_token"],
                error="Microsoft Graph connectivity check failed",
            )
            return {
                "ok": False, "oauthStatus": "ok", "graphStatus": "error",
                "poolStatus": "not_published",
                "error": "Microsoft Graph connectivity check failed",
            }
        await self.store.update_validation(
            account_id, oauth_status="ok", graph_status="ok",
            refresh_token=tokens["refresh_token"], error=None,
        )
        return {
            "ok": True, "oauthStatus": "ok", "graphStatus": "ok",
            "poolStatus": await self.store.publish_if_valid(account_id),
        }

    async def _ensure_graph_identity(self, account: dict[str, Any]) -> None:
        if account.get("oauthStatus") == "ok" and account.get("graphStatus") == "ok":
            return
        result = await self.check_graph(str(account.get("_id") or ""))
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Graph identity check failed"))

    async def messages(
        self,
        account_id: str,
        *,
        folder: str = "inbox",
        top: int = 20,
        recipient: str | None = None,
        proxy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = await self.store.get(account_id)
        proxy = proxy or self._proxy_from_account(account)
        await self._ensure_graph_identity(account)
        try:
            tokens = await self._refresh(account, proxy=proxy)
        except PermissionError as exc:
            status = "expired" if str(exc) == "expired" else "error"
            await self.store.update_validation(account_id, oauth_status=status, graph_status="unknown", error="OAuth token expired" if status == "expired" else "OAuth refresh failed")
            raise
        except Exception:
            await self.store.update_validation(account_id, oauth_status="error", graph_status="unknown", error="OAuth request failed")
            raise
        safe_folder = {"inbox": "inbox", "junk": "junkemail", "deleted": "deleteditems"}.get(folder.casefold(), "inbox")
        try:
            # Validate that the token still has Graph access before treating the
            # mailbox as healthy. A read failure removes it from the allocatable pool.
            page = await self._graph_get(
                f"/me/mailFolders/{safe_folder}/messages",
                tokens["access_token"],
                {"$top": max(1, min(int(top), 50)), "$orderby": "receivedDateTime desc", "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,bodyPreview,hasAttachments"},
                proxy=proxy,
            )
        except PermissionError:
            await self.store.update_validation(account_id, oauth_status="expired", graph_status="unknown", error="OAuth token expired")
            raise
        except Exception:
            await self.store.update_validation(account_id, oauth_status="ok", graph_status="error", refresh_token=tokens["refresh_token"], error="Microsoft Graph inbox read failed")
            raise
        messages = []
        recipient_needle = normalize_email(recipient) if recipient else ""
        for item in page.get("value") or []:
            sender = ((item.get("from") or {}).get("emailAddress") or {})
            if recipient_needle:
                addresses = []
                for key in ("toRecipients", "ccRecipients"):
                    addresses.extend(
                        normalize_email(str(((entry.get("emailAddress") or {}).get("address") or "")))
                        for entry in item.get(key) or []
                    )
                if recipient_needle not in addresses:
                    continue
            messages.append({
                "id": str(item.get("id") or ""), "subject": str(item.get("subject") or "(无主题)"),
                "from": str(sender.get("address") or ""), "fromName": str(sender.get("name") or ""),
                "receivedAt": str(item.get("receivedDateTime") or ""), "isRead": bool(item.get("isRead")),
                "preview": str(item.get("bodyPreview") or ""), "hasAttachments": bool(item.get("hasAttachments")),
            })
        await self.store.update_validation(account_id, oauth_status="ok", refresh_token=tokens["refresh_token"], error=None)
        return {"email": account["email"], "messages": messages}

    async def message_detail(
        self,
        account_id: str,
        message_id: str,
        *,
        recipient: str | None = None,
        proxy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = await self.store.get(account_id)
        proxy = proxy or self._proxy_from_account(account)
        await self._ensure_graph_identity(account)
        try:
            tokens = await self._refresh(account, proxy=proxy)
        except PermissionError as exc:
            status = "expired" if str(exc) == "expired" else "error"
            await self.store.update_validation(account_id, oauth_status=status, graph_status="unknown", error="OAuth token expired" if status == "expired" else "OAuth refresh failed")
            raise
        except Exception:
            await self.store.update_validation(account_id, oauth_status="error", graph_status="unknown", error="OAuth request failed")
            raise
        try:
            payload = await self._graph_get(
                f"/me/messages/{quote(message_id, safe='')}", tokens["access_token"],
                {"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,body,bodyPreview,hasAttachments"},
                proxy=proxy,
            )
        except PermissionError:
            await self.store.update_validation(account_id, oauth_status="expired", graph_status="unknown", error="OAuth token expired")
            raise
        except Exception:
            await self.store.update_validation(account_id, oauth_status="ok", graph_status="error", refresh_token=tokens["refresh_token"], error="Microsoft Graph message read failed")
            raise
        if recipient:
            addresses = {
                normalize_email(str((entry.get("emailAddress") or {}).get("address") or ""))
                for key in ("toRecipients", "ccRecipients")
                for entry in payload.get(key) or []
            }
            if normalize_email(recipient) not in addresses:
                raise ResourceNotFoundError("该邮件不属于此接码邮箱")
        sender = ((payload.get("from") or {}).get("emailAddress") or {})
        body = payload.get("body") or {}
        await self.store.update_validation(account_id, oauth_status="ok", refresh_token=tokens["refresh_token"], error=None)
        return {"email": account["email"], "message": {
            "id": str(payload.get("id") or message_id), "subject": str(payload.get("subject") or "(无主题)"),
            "from": str(sender.get("address") or ""), "fromName": str(sender.get("name") or ""),
            "receivedAt": str(payload.get("receivedDateTime") or ""), "isRead": bool(payload.get("isRead")),
            "preview": str(payload.get("bodyPreview") or ""), "body": str(body.get("content") or ""),
            "bodyType": str(body.get("contentType") or "text"),
        }}

    async def mailbox_snapshot(
        self,
        account_id: str,
        email: str,
        *,
        purpose: str = "verification",
        proxy: Mapping[str, Any] | None = None,
    ) -> MailboxSnapshot:
        account = await self.store.get(account_id)
        proxy = proxy or self._proxy_from_account(account)
        await self._ensure_graph_identity(account)
        account = await self.store.get(account_id)
        if normalize_email(email) != normalize_email(str(account.get("email") or "")):
            raise MailboxClientError("mailbox_account_mismatch", "Outlook 邮箱与账号关联不匹配")
        try:
            tokens = await self._refresh(account, proxy=proxy)
        except PermissionError as exc:
            status = "expired" if str(exc) == "expired" else "error"
            message = "OAuth token expired" if status == "expired" else "OAuth refresh failed"
            await self.store.update_validation(
                account_id, oauth_status=status, graph_status="unknown", error=message
            )
            raise
        except Exception:
            await self.store.update_validation(
                account_id, oauth_status="error", graph_status="unknown",
                error="OAuth request failed",
            )
            raise
        try:
            page = await self._graph_get(
                "/me/mailFolders/inbox/messages",
                tokens["access_token"],
                {
                    "$top": 20,
                    "$orderby": "receivedDateTime desc",
                    "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,body",
                },
                proxy=proxy,
            )
        except PermissionError:
            await self.store.update_validation(
                account_id, oauth_status="expired", graph_status="unknown",
                error="OAuth token expired",
            )
            raise
        except Exception:
            await self.store.update_validation(
                account_id, oauth_status="ok", graph_status="error",
                refresh_token=tokens["refresh_token"],
                error="Microsoft Graph inbox read failed",
            )
            raise
        await self.store.update_validation(
            account_id, oauth_status="ok", refresh_token=tokens["refresh_token"], error=None,
        )
        values = page.get("value") or []
        needle = email.strip().casefold()
        selected: list[dict[str, Any]] = []
        for item in values:
            addresses = []
            for key in ("toRecipients", "ccRecipients"):
                addresses.extend(
                    str(((recipient.get("emailAddress") or {}).get("address") or "")).casefold()
                    for recipient in item.get(key) or []
                )
            # Some test accounts expose only the primary mailbox and omit recipient metadata.
            if not addresses or needle in addresses:
                selected.append(item)
        serialized = [
            {"subject": item.get("subject") or "", "body": (item.get("body") or {}).get("content") or item.get("bodyPreview") or "", "receivedDateTime": item.get("receivedDateTime") or ""}
            for item in selected[:20]
        ]
        snapshot = parse_mailbox_snapshot(json.dumps(serialized, ensure_ascii=False), "application/json", purpose=purpose)
        if snapshot.received_at_utc is None and selected:
            parsed = parse_mail_datetime(str(selected[0].get("receivedDateTime") or ""))
            if parsed:
                return MailboxSnapshot(
                    fingerprint=snapshot.fingerprint, verification_code=snapshot.verification_code,
                    received_at_utc=parsed[0], received_offset=parsed[1], response_body="Graph inbox",
                )
        return MailboxSnapshot(
            fingerprint=snapshot.fingerprint, verification_code=snapshot.verification_code,
            received_at_utc=snapshot.received_at_utc, received_offset=snapshot.received_offset,
            response_body="Graph inbox", received_precision_seconds=snapshot.received_precision_seconds,
        )

    async def close(self) -> None:
        self.session.close()

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/api/outlook/accounts")
        async def list_accounts(request: Request, page: int = Query(1, ge=1), page_size: int = Query(50, alias="pageSize", ge=1, le=200), q: str = "", source: str = "all", pool_status: str = Query("all", alias="poolStatus")):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_store.list_accounts(page=page, page_size=page_size, query=q, source=source, pool_status=pool_status)

        @router.get("/api/outlook/register")
        async def get_register_status(request: Request):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_register_tasks.status()

        @router.put("/api/outlook/register")
        async def update_register_config(request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError("配置必须是 JSON 对象")
                return {"config": await request.app.state.outlook_register_tasks.update_config(payload)}
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail={"code": "outlook_register_config_invalid", "message": str(exc)}) from exc

        @router.post("/api/outlook/register/start", status_code=202)
        async def start_register_task(request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_register_tasks.start()
            except (TypeError, ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=409, detail={"code": "outlook_register_start_failed", "message": str(exc)}) from exc

        @router.post("/api/outlook/register/stop", status_code=202)
        async def stop_register_task(request: Request):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_register_tasks.stop()

        @router.post("/api/outlook/register/reset")
        async def reset_register_task(request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_register_tasks.reset()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail={"code": "outlook_register_reset_failed", "message": str(exc)}) from exc

        @router.get("/api/outlook/register/logs")
        async def get_register_logs(request: Request, limit: int = Query(200, ge=1, le=500)):
            request.app.state.mongo_manager.require_online()
            return {"items": await request.app.state.outlook_register_tasks.logs(limit)}

        @router.get("/api/outlook/proxy-groups")
        async def list_shared_proxy_groups(request: Request):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.resource_store.proxy_group_summaries()

        # Outlook mailbox-pool compatibility surface. Runtime reads and writes
        # are Mongo-backed; the old Results/pool.json files are migration-only.
        @router.get("/api/outlook/pool/stats")
        @router.get("/api/pool/stats")
        async def outlook_pool_stats(request: Request):
            request.app.state.mongo_manager.require_online()
            return {"stats": await request.app.state.outlook_pool_service.stats()}

        @router.get("/api/outlook/pool/accounts")
        @router.get("/api/pool/accounts")
        async def outlook_pool_accounts(
            request: Request,
            category: str = "all",
            keyword: str = "",
            country: str = "",
            age: str = "",
            oauth_status: str = Query("", alias="oauth_status"),
            page: int = Query(1, ge=1),
            page_size: int = Query(50, alias="page_size", ge=1, le=200),
        ):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_pool_service.list_accounts(
                category=category, keyword=keyword, country=country, age=age,
                oauth_status=oauth_status, page=page, page_size=page_size,
            )

        @router.get("/api/outlook/pool/accounts/{account_id}")
        @router.get("/api/pool/accounts/{account_id}")
        async def outlook_pool_account_detail(account_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return {"account": await request.app.state.outlook_pool_service.get_detail(account_id)}
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @router.post("/api/outlook/pool/accounts/{account_id}/sub-emails")
        @router.post("/api/pool/accounts/{account_id}/sub-emails")
        async def outlook_pool_generate_subs(account_id: str, body: OutlookPoolSubGenerateBody, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_pool_service.generate_sub_emails(
                    account_id, count=body.count, tag_prefix=body.tag_prefix,
                )
            except (ResourceNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @router.post("/api/outlook/pool/batch/sub-emails")
        @router.post("/api/pool/batch/sub-emails")
        async def outlook_pool_batch_subs(body: OutlookPoolBatchBody, request: Request):
            request.app.state.mongo_manager.require_online()
            if not body.ids:
                raise HTTPException(status_code=400, detail="请选择账号")
            return await request.app.state.outlook_pool_service.batch_generate_sub_emails(
                body.ids, count=body.count, tag_prefix=body.tag_prefix,
            )

        @router.post("/api/outlook/pool/batch/check-oauth")
        @router.post("/api/pool/batch/check-oauth")
        async def outlook_pool_batch_check(body: OutlookPoolDeleteBody, request: Request):
            request.app.state.mongo_manager.require_online()
            if not body.ids:
                raise HTTPException(status_code=400, detail="请选择账号")
            return await request.app.state.outlook_pool_service.run_oauth_check(ids=body.ids, source="manual")

        @router.delete("/api/outlook/pool/accounts")
        @router.delete("/api/pool/accounts")
        async def outlook_pool_delete(body: OutlookPoolDeleteBody, request: Request):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_pool_service.delete_ids(body.ids)

        @router.post("/api/outlook/pool/export")
        @router.post("/api/pool/export")
        async def outlook_pool_export(body: OutlookPoolExportBody, request: Request):
            request.app.state.mongo_manager.require_online()
            remote = request.client.host if request.client else ""
            if remote not in {"127.0.0.1", "::1"}:
                raise HTTPException(status_code=403, detail="Outlook pool export is local-only")
            try:
                text = await request.app.state.outlook_pool_service.export_text(
                    body.category, body.ids, country=body.country,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            filename = {"registered": "registered.txt", "oauth2": "oauth2_export.txt", "sub": "sub_emails.txt", "recovery": "recovery_bound.txt"}.get(body.category, "outlook_export.txt")
            return PlainTextResponse(
                text,
                headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
            )

        @router.get("/api/outlook/pool/export")
        @router.get("/api/pool/export")
        async def outlook_pool_export_get(request: Request, category: str = "oauth2", country: str = ""):
            # FastAPI injects Request even though the compatibility query shape
            # is intentionally kept identical to the old manager.
            request.app.state.mongo_manager.require_online()
            remote = request.client.host if request.client else ""
            if remote not in {"127.0.0.1", "::1"}:
                raise HTTPException(status_code=403, detail="Outlook pool export is local-only")
            try:
                text = await request.app.state.outlook_pool_service.export_text(category, country=country or None)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return PlainTextResponse(text, headers={"Cache-Control": "no-store"})

        @router.get("/api/outlook/pool/config/oauth-check")
        @router.get("/api/pool/config/oauth-check")
        async def outlook_pool_oauth_config(request: Request):
            request.app.state.mongo_manager.require_online()
            return {"config": await request.app.state.outlook_pool_service.get_oauth_check_config()}

        @router.post("/api/outlook/pool/config/oauth-check")
        @router.put("/api/outlook/pool/config/oauth-check")
        @router.post("/api/pool/config/oauth-check")
        async def outlook_pool_update_oauth_config(body: OutlookOAuthCheckBody, request: Request):
            request.app.state.mongo_manager.require_online()
            return {"ok": True, "config": await request.app.state.outlook_pool_service.update_oauth_check_config(body.model_dump(exclude_unset=True))}

        @router.post("/api/outlook/pool/accounts/{account_id}/check-oauth")
        @router.post("/api/pool/accounts/{account_id}/check-oauth")
        async def outlook_pool_check_oauth(account_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                parent_id = await request.app.state.outlook_pool_service.resolve_parent_id(account_id)
                return await request.app.state.outlook_service.check_oauth(parent_id)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @router.get("/api/outlook/pool/accounts/{account_id}/messages")
        @router.get("/api/pool/accounts/{account_id}/messages")
        async def outlook_pool_messages(account_id: str, request: Request, folder: str = "inbox", top: int = Query(20, ge=1, le=50)):
            request.app.state.mongo_manager.require_online()
            try:
                detail = await request.app.state.outlook_pool_service.get_detail(account_id)
                parent_id = str(detail.get("parentId") or detail.get("id") or account_id)
                recipient = str(detail.get("email") or "") if detail.get("category") == "sub" else None
                return await request.app.state.outlook_service.messages(parent_id, folder=folder, top=top, recipient=recipient)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail="Graph 邮件读取失败") from exc

        @router.get("/api/outlook/pool/receive/{token}")
        @router.get("/api/pool/receive/{token}")
        async def outlook_pool_receive(token: str, request: Request, folder: str = "inbox", top: int = Query(10, ge=1, le=30), format: str = "json"):
            request.app.state.mongo_manager.require_online()
            try:
                payload = await request.app.state.outlook_pool_service.receive_payload(token, folder=folder, top=top)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if format == "text":
                return PlainTextResponse("\n".join([
                    f"email: {payload.get('email')}",
                    f"latest_code: {payload.get('latest_code') or ''}",
                    f"codes: {','.join(payload.get('codes') or [])}",
                    f"messages: {len(payload.get('messages') or [])}",
                    f"ui: {payload.get('receive_ui_url')}",
                ]) + "\n", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            return JSONResponse(payload, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

        @router.get("/api/outlook/pool/receive/{token}/message/{message_id}")
        @router.get("/api/pool/receive/{token}/message/{message_id}")
        async def outlook_pool_receive_message(token: str, message_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                payload = await request.app.state.outlook_pool_service.receive_message(token, message_id)
                return JSONResponse(payload, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail="Graph 邮件详情读取失败") from exc

        @router.get("/r/{token}", include_in_schema=False)
        async def outlook_receive_ui(token: str):
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
                raise HTTPException(status_code=404, detail="接码地址无效")
            token_json = json.dumps(token, ensure_ascii=False)
            return HTMLResponse(f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Outlook 接码</title><style>body{{font-family:system-ui,sans-serif;background:#f5f7fb;color:#182230;margin:0;padding:24px}}main{{max-width:900px;margin:auto;background:white;border-radius:12px;padding:20px;box-shadow:0 8px 30px #0001}}button{{padding:8px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}}.code{{font-size:28px;font-weight:700;color:#b42318;margin:14px 0}}.msg{{border-top:1px solid #eee;padding:12px 0}}.muted{{color:#667085}}</style></head><body><main><h2>Outlook 接码</h2><div id=\"email\" class=\"muted\"></div><div id=\"code\" class=\"code\">验证码：加载中</div><button onclick=\"load()\">刷新</button><div id=\"list\"></div></main><script>const TOKEN={token_json};const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({{ '&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;' }})[c]);async function load(){{const r=await fetch('/api/outlook/pool/receive/'+encodeURIComponent(TOKEN));const d=await r.json();if(!r.ok){{document.getElementById('list').textContent=d.detail||'读取失败';return}}document.getElementById('email').textContent=d.email||'';document.getElementById('code').textContent='验证码：'+(d.latest_code||'暂无');document.getElementById('list').innerHTML=(d.messages||[]).map(m=>'<div class=\"msg\"><b>'+esc(m.subject)+'</b><div class=\"muted\">'+esc(m.receivedAt||'')+'</div><div>'+esc(m.preview||'')+'</div></div>').join('')||'<div class=\"muted\">暂无邮件</div>'}}load();setInterval(load,15000)</script></body></html>""", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})

        @router.get("/api/outlook/results")
        async def list_outlook_results(
            request: Request,
            limit: int = Query(100, ge=1, le=500),
            q: str = Query(default="", max_length=320),
        ):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_store.result_accounts(limit=limit, query=q)

        @router.get("/api/outlook/proxies")
        async def list_shared_proxies(
            request: Request,
            page: int = Query(1, ge=1),
            page_size: int = Query(50, alias="pageSize", ge=1, le=100),
            q: str = Query(default="", max_length=320),
            country: str = Query(default="", max_length=2),
        ):
            request.app.state.mongo_manager.require_online()
            result = await request.app.state.resource_store.list_proxies(page, page_size, q, country)
            return {
                "items": [
                    OutlookProxyView(
                        id=item.id, host=item.host, port=item.port, enabled=item.enabled,
                        status=item.status, latencyMs=item.latencyMs, country=item.country,
                        group=item.group, scheme=item.scheme,
                    )
                    for item in result.items
                ],
                "total": result.total,
                "page": result.page,
                "pageSize": result.pageSize,
            }

        @router.get("/api/outlook/accounts/{account_id}")
        async def get_account(account_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                document = await request.app.state.outlook_store.get(account_id)
                email_document = await request.app.state.resource_store._guard(
                    request.app.state.resource_store.emails.find_one({"outlookAccountId": account_id})
                )
                return {"account": _public_account(document, email_document)}
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @router.patch("/api/outlook/accounts/{account_id}")
        async def update_account(account_id: str, payload: OutlookEditBody, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                account = await request.app.state.outlook_store.get(account_id)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            changes = payload.model_dump(exclude_unset=True)
            update: dict[str, Any] = {"updatedAt": utc_now()}
            credential_changed = False
            for field, mongo_field in (("password", "password"), ("clientId", "clientId"), ("refreshToken", "refreshToken")):
                value = changes.get(field)
                if value is None:
                    continue
                if mongo_field == "password":
                    update[mongo_field] = value
                elif str(account.get(mongo_field) or "") != str(value):
                    update[mongo_field] = value
                    credential_changed = True
            if credential_changed:
                update.update({
                    "oauthStatus": "unknown" if update.get("refreshToken", account.get("refreshToken")) and update.get("clientId", account.get("clientId")) else "missing",
                    "graphStatus": "unknown",
                    "oauthCheckedAt": None,
                    "graphCheckedAt": None,
                    "poolStatus": "not_published",
                    "lastError": "Credentials updated; validation required",
                })
                await request.app.state.resource_store._guard(
                    request.app.state.resource_store.emails.update_many(
                        {"outlookAccountId": account_id, "status": "available"},
                        {"$set": {"status": "unavailable", "unavailableReason": "Outlook credentials changed; validation required", "updatedAt": utc_now()}},
                    )
                )
            await request.app.state.resource_store._guard(
                request.app.state.outlook_store.accounts.update_one({"_id": account_id}, {"$set": update})
            )
            fresh = await request.app.state.outlook_store.get(account_id)
            email_document = await request.app.state.resource_store._guard(
                request.app.state.resource_store.emails.find_one({"outlookAccountId": account_id})
            )
            return {"account": _public_account(fresh, email_document)}

        @router.post("/api/outlook/accounts/{account_id}/delete")
        async def delete_account(account_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            account = await request.app.state.outlook_store.get(account_id)
            mailbox = await request.app.state.resource_store._guard(
                request.app.state.resource_store.emails.find_one({"outlookAccountId": account_id})
            )
            if mailbox and mailbox.get("status") in {"reserved", "assigned"}:
                raise HTTPException(status_code=409, detail="邮箱正在使用或已分配；为保留收信关联，请勿删除 Outlook 账号")
            if mailbox:
                await request.app.state.resource_store._guard(
                    request.app.state.resource_store.emails.delete_one({"_id": mailbox["_id"], "status": {"$in": ["available", "unavailable"]}})
                )
            await request.app.state.resource_store._guard(
                request.app.state.outlook_store.accounts.delete_one({"_id": account_id})
            )
            return {"deleted": 1, "email": account.get("email")}

        @router.post("/api/outlook/accounts/{account_id}/check-oauth", response_model=GraphCheckResponse)
        async def check_oauth(account_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_service.check_oauth(account_id)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @router.post("/api/outlook/accounts/{account_id}/check-graph", response_model=GraphCheckResponse)
        async def check_graph(account_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_service.check_graph(account_id)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @router.get("/api/outlook/accounts/{account_id}/messages")
        async def list_messages(account_id: str, request: Request, folder: str = "inbox", top: int = Query(20, ge=1, le=50)):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_service.messages(account_id, folder=folder, top=top)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail="Graph 邮件读取失败") from exc

        @router.get("/api/outlook/accounts/{account_id}/messages/{message_id}")
        async def get_message(account_id: str, message_id: str, request: Request):
            request.app.state.mongo_manager.require_online()
            try:
                return await request.app.state.outlook_service.message_detail(account_id, message_id)
            except ResourceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail="Graph 邮件详情读取失败") from exc

        @router.post("/api/outlook/import")
        async def import_accounts(payload: OutlookImportBody, request: Request):
            request.app.state.mongo_manager.require_online()
            imported = duplicates = errors = 0
            for item in payload.accounts:
                try:
                    _, created = await request.app.state.outlook_store.upsert_account(
                        email=item.email, password=item.password, client_id=item.clientId,
                        refresh_token=item.refreshToken, source="manual",
                    )
                    imported += int(created)
                    duplicates += int(not created)
                except ValueError:
                    errors += 1
            return {"total": len(payload.accounts), "imported": imported, "duplicates": duplicates, "errors": errors}

        @router.post("/api/outlook/import-legacy")
        async def import_legacy(request: Request):
            request.app.state.mongo_manager.require_online()
            return await migrate_legacy_outlook_data(request.app.state.outlook_store)

        @router.get("/api/outlook/migration")
        async def migration_status(request: Request):
            request.app.state.mongo_manager.require_online()
            return await request.app.state.outlook_migration_status()

        @router.post("/api/outlook/export")
        async def export_accounts(payload: OutlookExportBody, request: Request):
            request.app.state.mongo_manager.require_online()
            remote = request.client.host if request.client else ""
            if remote not in {"127.0.0.1", "::1"}:
                raise HTTPException(status_code=403, detail="Outlook credential export is local-only")
            query = {} if payload.ids is None else {"_id": {"$in": payload.ids}}
            cursor = request.app.state.outlook_store.accounts.find(query).sort("emailNormalized", 1)
            documents = await request.app.state.resource_store._guard(cursor.to_list(length=None))
            body = "\n".join(
                "----".join((str(item.get("email") or ""), str(item.get("password") or ""), str(item.get("clientId") or ""), str(item.get("refreshToken") or "")))
                for item in documents
            )
            return PlainTextResponse(body + ("\n" if body else ""), media_type="text/plain; charset=utf-8", headers={"Content-Disposition": "attachment; filename=outlook-accounts.txt", "Cache-Control": "no-store"})

        return router


async def migrate_legacy_outlook_data(store: OutlookStore) -> dict[str, Any]:
    """Read legacy sources, create immutable backups, and upsert accounts by normalized email."""
    source_paths = [RESULTS_ROOT / "pool.json", RESULTS_ROOT / "oauth2.txt", RESULTS_ROOT / "registered.txt"]
    existing_paths = [path for path in source_paths if path.is_file()]
    backup_paths: list[str] = []
    for path in existing_paths:
        source_bytes = path.read_bytes()
        digest = hashlib.sha256(source_bytes).hexdigest()[:16]
        backup = path.with_name(f"{path.name}.pre-mongo.{digest}.bak")
        if backup.exists():
            if backup.read_bytes() != source_bytes:
                raise RuntimeError(f"迁移备份校验失败：{backup.name}")
        else:
            temporary = backup.with_name(f".{backup.name}.{uuid4().hex}.tmp")
            try:
                shutil.copy2(path, temporary)
                if temporary.read_bytes() != source_bytes:
                    raise RuntimeError(f"迁移备份校验失败：{backup.name}")
                temporary.chmod(temporary.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
                try:
                    temporary.replace(backup)
                except FileExistsError:
                    if backup.read_bytes() != source_bytes:
                        raise RuntimeError(f"迁移备份校验失败：{backup.name}")
            finally:
                temporary.unlink(missing_ok=True)
        backup.chmod(backup.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
        backup_paths.append(str(backup))

    records: dict[str, dict[str, str]] = {}
    errors = 0
    pool_path = RESULTS_ROOT / "pool.json"
    if pool_path.is_file():
        try:
            payload = json.loads(pool_path.read_text(encoding="utf-8-sig"))
            for item in payload.get("accounts", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    errors += 1
                    continue
                email = normalize_email(str(item.get("email") or item.get("mail") or ""))
                if not EMAIL_RE.fullmatch(email):
                    errors += 1
                    continue
                previous = records.setdefault(email, {"email": email, "password": "", "client_id": "", "refresh_token": ""})
                previous["password"] = str(item.get("password") or previous["password"])
                previous["client_id"] = str(item.get("client_id") or previous["client_id"])
                previous["refresh_token"] = str(item.get("refresh_token") or previous["refresh_token"])
        except (OSError, ValueError, TypeError):
            errors += 1

    for filename, oauth in (("registered.txt", False), ("oauth2.txt", True)):
        path = RESULTS_ROOT / filename
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("----", 3 if oauth else 1)
                if len(parts) < (4 if oauth else 2):
                    errors += 1
                    continue
                email = normalize_email(parts[0])
                if not EMAIL_RE.fullmatch(email):
                    errors += 1
                    continue
                previous = records.setdefault(email, {"email": email, "password": "", "client_id": "", "refresh_token": ""})
                previous["password"] = parts[1] or previous["password"]
                if oauth:
                    previous["client_id"] = parts[2] or previous["client_id"]
                    previous["refresh_token"] = parts[3] or previous["refresh_token"]
        except OSError:
            errors += 1

    imported = duplicates = 0
    for item in records.values():
        _, created = await store.upsert_account(
            email=item["email"], password=item["password"], client_id=item["client_id"],
            refresh_token=item["refresh_token"], source="migration",
        )
        imported += int(created)
        duplicates += int(not created)
    summary = {
        "sources": [path.name for path in existing_paths],
        "sourceCount": len(existing_paths),
        "parsedAccounts": len(records),
        "imported": imported,
        "duplicates": duplicates,
        "errors": errors,
        "backupPaths": backup_paths,
        "completedAt": utc_now(),
    }
    await store.store_migration(summary)
    return summary


class MongoOutlookMailboxClient(MailboxClient):
    """MailboxClient-compatible Graph reader backed by Outlook account IDs in Mongo."""
    def __init__(self, manager: Any) -> None:
        super().__init__()
        self.manager = manager
        self.outlook_service = OutlookService(OutlookStore(MongoResourceStore(manager)))

    async def get_snapshot(self, access_url: str, email: str, *, purpose: str = "verification") -> MailboxSnapshot:
        parsed = urlsplit(access_url)
        if parsed.scheme.casefold() == "mailcom":
            from .mailcom_service import MongoMailComMailboxClient
            return await MongoMailComMailboxClient(self.manager).get_snapshot(access_url, email, purpose=purpose)
        if parsed.scheme.casefold() != "outlook":
            return await super().get_snapshot(access_url, email, purpose=purpose)
        account_id = parsed.netloc or parsed.path.lstrip("/")
        try:
            return await self.outlook_service.mailbox_snapshot(account_id, email, purpose=purpose)
        except MailboxClientError:
            raise
        except PermissionError as exc:
            raise MailboxClientError(
                "mailbox_auth_expired", "Outlook OAuth 授权已失效", retryable=False
            ) from exc
        except Exception as exc:
            raise MailboxClientError(
                "mailbox_unavailable", "Outlook Graph 收信暂时不可用", retryable=True
            ) from exc
