from __future__ import annotations

"""MailCom data and IMAP service hosted by the main FastAPI process."""

import re
import sqlite3
import sys
import asyncio
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from uuid import uuid4

from .mailbox_client import MailboxClient, MailboxClientError, MailboxSnapshot
from .resource_service import MongoResourceStore, normalize_email, utc_now

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAILCOM_SCHEME = "mailcom"
MAILCOM_IMAP_HOST = "imap.mail.com"
MAILCOM_IMAP_PORT = 993


def _recipient_matches(value: Any, email: str) -> bool:
    """Match RFC-822 recipients, including the legacy pipe separator."""
    addresses = {
        address.strip().casefold()
        for _, address in getaddresses([str(value or "").replace("|", ",")])
        if address.strip()
    }
    return email.strip().casefold() in addresses


def _message_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payment_confirmation_match(message: Any, requested_email: str, since: datetime) -> tuple[bool, str | None]:
    if not _recipient_matches(getattr(message, "recipients", ""), requested_email):
        return False, None
    received_at = _message_datetime(getattr(message, "received_at", None))
    normalized_since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    if received_at is None or received_at < normalized_since.astimezone(timezone.utc) - timedelta(minutes=2):
        return False, None
    text = " ".join(
        str(value or "")
        for value in (
            getattr(message, "subject", ""),
            getattr(message, "sender", ""),
            getattr(message, "recipients", ""),
            getattr(message, "preview", ""),
        )
    )
    lowered = text.casefold()
    plus_marker = "chatgpt plus" in lowered or "plus subscription" in lowered
    provider_marker = "openai" in lowered or "chatgpt" in lowered
    success_marker = any(
        marker in lowered
        for marker in (
            "successfully subscribed",
            "subscription is active",
            "subscription confirmed",
            "正常に登録",
            "订阅成功",
            "訂閱成功",
            "erfolgreich abonniert",
            "abonnement confirmé",
            "başarıyla abone",
        )
    )
    order_match = re.search(r"\bsub_[a-z0-9]+\b", text, flags=re.IGNORECASE)
    return bool(plus_marker and provider_marker and success_marker and order_match), (
        order_match.group(0) if order_match else None
    )


class CredentialCipher(Protocol):
    def encrypt(self, value: str) -> bytes: ...
    def decrypt(self, value: bytes) -> str: ...


class MainCredentialCipher:
    """Current-user DPAPI with a main-service-specific envelope prefix."""

    prefix = b"AUTOREGISTER-DPAPI-1\0"
    legacy_prefix = b"MAILCOM-DPAPI-1\0"

    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[2] / "mailcom-manager"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from manager.crypto import DpapiCredentialCipher
        self._cipher = DpapiCredentialCipher()

    def encrypt(self, value: str) -> bytes:
        protected = self._cipher.encrypt(value)
        if not protected.startswith(self.legacy_prefix):
            raise ValueError("legacy DPAPI cipher returned an invalid envelope")
        return self.prefix + protected[len(self.legacy_prefix):]

    def decrypt(self, value: bytes) -> str:
        if not value.startswith(self.prefix):
            raise ValueError("credential is not protected by the main service")
        return self._cipher.decrypt(self.legacy_prefix + value[len(self.prefix):])


def _public_account(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("_id") or ""),
        "email": str(row.get("email") or ""),
        "status": str(row.get("status") or "unknown"),
        "messageCount": row.get("messageCount"),
        "lastCheckedAt": row.get("lastCheckedAt"),
        "lastError": str(row.get("lastError") or "")[:180] or None,
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
        "aliasCount": int(row.get("aliasCount") or 0),
    }


def _public_alias(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("_id") or ""),
        "accountId": str(row.get("accountId") or ""),
        "email": str(row.get("email") or ""),
        "label": str(row.get("label") or ""),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


class MailComService:
    def __init__(
        self,
        resources: MongoResourceStore,
        *,
        cipher: CredentialCipher | None = None,
        sqlite_path: Path | None = None,
        imap_service: Any | None = None,
        server_sync_service: Any | None = None,
        legacy_cipher: CredentialCipher | None = None,
    ) -> None:
        self.resources = resources
        self.manager = resources.manager
        self.cipher = cipher
        self.sqlite_path = sqlite_path or Path(__file__).resolve().parents[2] / "mailcom-manager" / "data" / "mailcom.db"
        root = Path(__file__).resolve().parents[2] / "mailcom-manager"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from manager.imap_client import ImapMailboxService
        from manager.server_sync import ServerSyncService

        self.imap_service = imap_service or ImapMailboxService()
        self.server_sync_service = server_sync_service or ServerSyncService()
        self.legacy_cipher = legacy_cipher
        self._imap_semaphore = asyncio.Semaphore(3)

    @property
    def accounts(self) -> Any:
        return self.manager.database["mailcom_accounts"]

    @property
    def aliases(self) -> Any:
        return self.manager.database["mailcom_aliases"]

    @property
    def migrations(self) -> Any:
        return self.manager.database["mailcom_migrations"]

    async def ensure_indexes(self) -> None:
        await self.resources._guard(self.accounts.create_index([("emailNormalized", 1)], unique=True, name="mailcom_account_email"))
        await self.resources._guard(self.aliases.create_index([("emailNormalized", 1)], unique=True, name="mailcom_alias_email"))
        await self.resources._guard(self.aliases.create_index([("accountId", 1)], name="mailcom_alias_account"))
        await self.resources._guard(self.migrations.create_index([("_id", 1)], unique=True, name="mailcom_migration_id"))

    def _get_cipher(self) -> CredentialCipher:
        if self.cipher is None:
            self.cipher = MainCredentialCipher()
        return self.cipher

    async def list_accounts(self, query: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
        match: dict[str, Any] = {}
        if query.strip():
            match["emailNormalized"] = {"$regex": re.escape(query.strip().casefold())}
        total = await self.resources._guard(self.accounts.count_documents(match))
        rows = await self.resources._guard(self.accounts.find(match).sort("createdAt", -1).skip((page - 1) * page_size).limit(page_size).to_list(length=page_size))
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["aliasCount"] = await self.resources._guard(self.aliases.count_documents({"accountId": row.get("_id")}))
            items.append(_public_account(item))
        return {"items": items, "total": int(total), "page": page, "pageSize": page_size}

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = await self.resources._guard(self.accounts.find_one({"_id": account_id}))
        if row is None:
            return None
        item = dict(row)
        item["aliasCount"] = await self.resources._guard(self.aliases.count_documents({"accountId": account_id}))
        return _public_account(item)

    async def import_account(self, email: str, password: str) -> tuple[bool, str]:
        normalized = normalize_email(email)
        if not EMAIL_RE.fullmatch(normalized) or not password:
            return False, "invalid"
        if await self.resources._guard(self.aliases.find_one({"emailNormalized": normalized})) or await self.resources._guard(self.accounts.find_one({"emailNormalized": normalized})):
            return False, "duplicate"
        now = utc_now()
        document = {"_id": uuid4().hex, "email": email.strip(), "emailNormalized": normalized, "passwordEncrypted": self._get_cipher().encrypt(password), "status": "unknown", "messageCount": None, "lastCheckedAt": None, "lastError": None, "createdAt": now, "updatedAt": now}
        try:
            await self.resources._guard(self.accounts.insert_one(document))
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                return False, "duplicate"
            raise
        return True, "inserted"

    async def aliases_for(self, account_id: str) -> list[dict[str, Any]]:
        rows = await self.resources._guard(self.aliases.find({"accountId": account_id}).sort("createdAt", -1).to_list(length=500))
        return [_public_alias(row) for row in rows]

    async def import_alias(self, account_id: str, email: str, label: str = "") -> str:
        account = await self.resources._guard(self.accounts.find_one({"_id": account_id}))
        normalized = normalize_email(email)
        if account is None:
            return "missing_account"
        if not EMAIL_RE.fullmatch(normalized):
            return "invalid"
        if await self.resources._guard(self.accounts.find_one({"emailNormalized": normalized})) or await self.resources._guard(self.aliases.find_one({"emailNormalized": normalized})):
            return "duplicate"
        now = utc_now()
        try:
            await self.resources._guard(self.aliases.insert_one({"_id": uuid4().hex, "accountId": account_id, "email": email.strip(), "emailNormalized": normalized, "label": label.strip()[:200], "createdAt": now, "updatedAt": now}))
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                return "duplicate"
            raise
        return "inserted"

    async def delete_alias(self, alias_id: str) -> bool:
        result = await self.resources._guard(self.aliases.delete_one({"_id": alias_id}))
        return bool(result.deleted_count)

    async def delete_account(self, account_id: str) -> bool:
        await self.resources._guard(self.aliases.delete_many({"accountId": account_id}))
        result = await self.resources._guard(self.accounts.delete_one({"_id": account_id}))
        return bool(result.deleted_count)

    async def registration_items(self) -> list[dict[str, Any]]:
        rows = await self.resources._guard(self.accounts.find({}, {"_id": 1, "email": 1, "createdAt": 1}).sort("createdAt", -1).to_list(length=5000))
        aliases = await self.resources._guard(self.aliases.find({}, {"accountId": 1, "email": 1, "createdAt": 1}).sort("createdAt", -1).to_list(length=5000))
        by_id = {str(row.get("_id")): row for row in rows}
        items = [{"email": row.get("email", ""), "accountEmail": row.get("email", ""), "isAlias": False, "accessUrl": f"{MAILCOM_SCHEME}://account/{row.get('_id')}"} for row in rows]
        for row in aliases:
            account = by_id.get(str(row.get("accountId")))
            if account:
                items.append({"email": row.get("email", ""), "accountEmail": account.get("email", ""), "isAlias": True, "accessUrl": f"{MAILCOM_SCHEME}://alias/{row.get('_id')}"})
        return items

    async def _credentials(self, access_url: str, email: str) -> tuple[str, str]:
        parsed = urlsplit(access_url)
        if parsed.scheme.casefold() != MAILCOM_SCHEME:
            raise MailboxClientError("mailbox_auth_invalid", "MailCom 访问句柄无效")
        kind, identifier = parsed.netloc, parsed.path.strip("/")
        if kind not in {"account", "alias"} or not identifier or parsed.query or parsed.fragment:
            raise MailboxClientError("mailbox_auth_invalid", "MailCom 访问句柄无效")
        collection = self.accounts if kind == "account" else self.aliases
        if collection is None:
            raise MailboxClientError("mailbox_auth_invalid", "MailCom 访问句柄无效")
        row = await self.resources._guard(collection.find_one({"_id": identifier}))
        if row is None:
            raise MailboxClientError("mailbox_not_found", "MailCom 邮箱不存在")
        if kind == "alias":
            row = await self.resources._guard(self.accounts.find_one({"_id": row.get("accountId")}))
            if row is None:
                raise MailboxClientError("mailbox_not_found", "MailCom 主邮箱不存在")
        try:
            password = self._get_cipher().decrypt(bytes(row.get("passwordEncrypted") or b""))
        except Exception as exc:
            raise MailboxClientError("mailbox_auth_failed", "MailCom 邮箱凭据不可用") from exc
        return str(row.get("email") or email), password

    async def credentials_for_email(self, email: str) -> tuple[str, str, bool]:
        """Resolve an account or alias without exposing credentials to API callers."""
        normalized = normalize_email(email)
        account = await self.resources._guard(self.accounts.find_one({"emailNormalized": normalized}))
        if account is not None:
            password, login_email = await self._decrypt_account(account)
            return password, login_email, False
        alias = await self.resources._guard(self.aliases.find_one({"emailNormalized": normalized}))
        if alias is None:
            raise MailboxClientError("mailbox_not_found", "MailCom 邮箱不存在")
        account = await self.resources._guard(self.accounts.find_one({"_id": alias.get("accountId")}))
        if account is None:
            raise MailboxClientError("mailbox_not_found", "MailCom 主邮箱不存在")
        password, login_email = await self._decrypt_account(account)
        return password, login_email, True

    async def _decrypt_account(self, account: Mapping[str, Any]) -> tuple[str, str]:
        try:
            password = self._get_cipher().decrypt(bytes(account.get("passwordEncrypted") or b""))
        except Exception as exc:
            raise MailboxClientError("mailbox_auth_failed", "MailCom 邮箱凭据不可用") from exc
        return password, str(account.get("email") or "")

    async def test_account(self, account_id: str) -> dict[str, Any]:
        account = await self.resources._guard(self.accounts.find_one({"_id": account_id}))
        if account is None:
            raise MailboxClientError("mailbox_not_found", "MailCom 邮箱不存在")
        password, email = await self._decrypt_account(account)
        try:
            async with self._imap_semaphore:
                result = await asyncio.to_thread(self.imap_service.test, email, password)
        except Exception as exc:
            now = utc_now()
            await self.resources._guard(self.accounts.update_one(
                {"_id": account_id},
                {"$set": {"status": "failed", "messageCount": None, "lastCheckedAt": now, "lastError": str(getattr(exc, "message", "IMAP 测试失败"))[:180], "updatedAt": now}},
            ))
            raise
        now = utc_now()
        await self.resources._guard(self.accounts.update_one(
            {"_id": account_id},
            {"$set": {"status": "online", "messageCount": int(result.get("messageCount") or 0), "lastCheckedAt": now, "lastError": None, "updatedAt": now}},
        ))
        return {"id": account_id, "email": email, "ok": True, "messageCount": int(result.get("messageCount") or 0)}

    async def messages_for_email(self, email: str, *, folder: str = "INBOX", limit: int = 20) -> list[Any]:
        password, login_email, is_alias = await self.credentials_for_email(email)
        async with self._imap_semaphore:
            messages = await asyncio.to_thread(self.imap_service.messages, login_email, password, folder=folder, limit=limit)
        if is_alias:
            requested = normalize_email(email)
            messages = [item for item in messages if _recipient_matches(item.recipients, requested)]
        return messages

    async def latest_code(self, email: str) -> dict[str, Any]:
        messages: list[Any] = []
        for folder in ("INBOX", "Spam", "Junk"):
            messages.extend(await self.messages_for_email(email, folder=folder, limit=20))
        messages.sort(key=lambda item: str(item.received_at or ""), reverse=True)
        selected = next((item for item in messages if item.verification_code), None)
        return {"found": selected is not None, "email": email, "message": selected.public() if selected else None}

    async def latest_mail(self, email: str) -> dict[str, Any]:
        messages: list[Any] = []
        for folder in ("INBOX", "Spam", "Junk"):
            messages.extend(await self.messages_for_email(email, folder=folder, limit=20))
        messages.sort(key=lambda item: str(item.received_at or ""), reverse=True)
        latest = messages[0] if messages else None
        verification = next((item for item in messages if item.verification_code), None)
        selected = verification or latest
        return {
            "ok": True,
            "email": email,
            "isAlias": (await self.credentials_for_email(email))[2],
            "found": verification is not None,
            "subject": selected.subject if selected else "",
            "body": selected.preview if selected else "",
            "receivedAt": selected.received_at if selected else None,
            "folder": selected.folder if selected else None,
            "verification_code": verification.verification_code if verification else None,
            "message": selected.public() if selected else None,
        }

    async def payment_confirmation(self, email: str, since: datetime) -> dict[str, Any]:
        messages: list[Any] = []
        for folder in ("INBOX", "Spam", "Junk"):
            messages.extend(await self.messages_for_email(email, folder=folder, limit=30))
        messages.sort(key=lambda item: str(item.received_at or ""), reverse=True)
        for item in messages:
            matched, order_id = _payment_confirmation_match(item, email, since)
            if matched:
                return {"ok": True, "email": email, "status": "confirmed", "found": True, "subject": item.subject, "receivedAt": item.received_at, "orderId": order_id, "folder": item.folder, "message": item.public()}
        return {"ok": True, "email": email, "status": "waiting", "found": False, "subject": messages[0].subject if messages else "", "receivedAt": None, "orderId": None, "folder": None, "message": None}

    async def server_sync(self, *, host: str, port: int, username: str, password: str) -> dict[str, Any]:
        account_rows = await self.resources._guard(self.accounts.find({}, {"email": 1, "passwordEncrypted": 1}).sort("createdAt", 1).to_list(length=5000))
        alias_rows = await self.resources._guard(self.aliases.find({}, {"accountId": 1, "email": 1, "label": 1}).sort("createdAt", 1).to_list(length=5000))
        account_by_id = {str(row.get("_id")): row for row in account_rows}
        snapshot = {
            "version": 1,
            "accounts": [
                {"email": str(row.get("email") or ""), "password": self._get_cipher().decrypt(bytes(row.get("passwordEncrypted") or b""))}
                for row in account_rows
            ],
            "aliases": [
                {"email": str(row.get("email") or ""), "accountEmail": str(account_by_id.get(str(row.get("accountId")), {}).get("email") or ""), "label": str(row.get("label") or "")}
                for row in alias_rows if str(row.get("accountId")) in account_by_id
            ],
        }
        result = await asyncio.to_thread(self.server_sync_service.push, snapshot, host=host, port=port, username=username, password=password)
        return {"ok": True, "accounts": result.accounts, "aliases": result.aliases, "hostKeySha256": result.host_key_sha256}

    async def migrate_legacy(self) -> dict[str, Any]:
        source = self.sqlite_path
        if not source.exists():
            return {"status": "missing", "source": str(source), "accounts": 0, "aliases": 0, "imported": 0, "duplicates": 0, "errors": 0}
        backup = source.with_suffix(source.suffix + ".pre-mongo-readonly.bak")
        if not backup.exists():
            self._sqlite_snapshot(source, backup)
            try:
                backup.chmod(0o444)
            except OSError:
                pass
        handle = tempfile.NamedTemporaryFile(prefix="mailcom-migration-", suffix=".sqlite", dir=source.parent, delete=False)
        source_copy = Path(handle.name)
        handle.close()
        try:
            self._sqlite_snapshot(backup, source_copy)
        except Exception:
            source_copy.unlink(missing_ok=True)
            raise
        imported = duplicates = errors = account_count = alias_count = 0
        root = source.parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        old_cipher = self.legacy_cipher
        if old_cipher is None:
            from manager.crypto import DpapiCredentialCipher
            old_cipher = DpapiCredentialCipher()
        account_ids: dict[str, str] = {}
        imported_account_ids: set[str] = set()
        try:
            with sqlite3.connect(source_copy) as db:
                db.row_factory = sqlite3.Row
                accounts = db.execute("SELECT id,email,password_encrypted FROM accounts").fetchall()
                aliases = db.execute("SELECT account_id,email,label FROM aliases").fetchall()
            for row in accounts:
                account_count += 1
                try:
                    password = old_cipher.decrypt(bytes(row["password_encrypted"]))
                    ok, result = await self.import_account(str(row["email"]), password)
                    imported += int(ok)
                    duplicates += int(not ok and result == "duplicate")
                    account = await self.resources._guard(self.accounts.find_one({"emailNormalized": normalize_email(str(row["email"]))}))
                    if account:
                        account_ids[str(row["id"])] = str(account["_id"])
                        if ok:
                            imported_account_ids.add(str(row["id"]))
                except Exception:
                    errors += 1
            for row in aliases:
                alias_count += 1
                try:
                    parent_id = str(row["account_id"])
                    if parent_id not in account_ids:
                        errors += 1
                        continue
                    result = await self.import_alias(account_ids[parent_id], str(row["email"]), str(row["label"] or ""))
                    imported += int(result == "inserted")
                    duplicates += int(result == "duplicate")
                    errors += int(result in {"invalid", "missing_account"})
                except Exception:
                    errors += 1
        finally:
            try:
                source_copy.unlink()
            except OSError:
                pass
        summary = {
            "status": "completed" if errors == 0 else "completed_with_errors",
            "source": str(source),
            "backup": str(backup),
            "accounts": account_count,
            "aliases": alias_count,
            "imported": imported,
            "duplicates": duplicates,
            "errors": errors,
            "completedAt": datetime.now(timezone.utc).isoformat(),
        }
        await self.resources._guard(self.migrations.update_one({"_id": "sqlite-v1"}, {"$set": summary}, upsert=True))
        return summary

    @staticmethod
    def _sqlite_snapshot(source: Path, destination: Path) -> None:
        """Use SQLite's online backup API so WAL data is included consistently."""
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_db, sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)


class MongoMailComMailboxClient(MailboxClient):
    def __init__(self, manager: Any) -> None:
        super().__init__()
        self.service = MailComService(MongoResourceStore(manager))

    async def get_snapshot(self, access_url: str, email: str, *, purpose: str = "verification") -> MailboxSnapshot:
        username, password = await self.service._credentials(access_url, email)
        synthetic = f"mailcom-imap://{quote(username, safe='')}:{quote(password, safe='')}@{MAILCOM_IMAP_HOST}:{MAILCOM_IMAP_PORT}"
        return await super().get_snapshot(synthetic, email, purpose=purpose)
