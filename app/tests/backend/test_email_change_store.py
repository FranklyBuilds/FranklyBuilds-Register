from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.email_change_store import MongoEmailChangeStore
from backend.errors import DuplicateResourceError, ResourceNotFoundError
from backend.resource_models import AccountCreate
from backend.resource_service import MongoResourceStore


_MISSING = object()


@dataclass
class _WriteResult:
    matched_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    upserted_id: str | None = None


class _Cursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        selected = self._documents if length is None else self._documents[:length]
        return copy.deepcopy(selected)


def _lookup(document: dict[str, Any], path: str) -> tuple[bool, Any]:
    value: Any = document
    for component in path.split("."):
        if not isinstance(value, dict) or component not in value:
            return False, _MISSING
        value = value[component]
    return True, value


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(document, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(document, item) for item in expected):
                return False
            continue
        if key == "$nor":
            if any(_matches(document, item) for item in expected):
                return False
            continue

        exists, actual = _lookup(document, key)
        if not isinstance(expected, dict) or not any(
            str(operator).startswith("$") for operator in expected
        ):
            # Mongo treats a missing field as equal to null.
            if expected is None and not exists:
                continue
            if not exists or actual != expected:
                return False
            continue

        for operator, operand in expected.items():
            if operator == "$exists":
                if exists != bool(operand):
                    return False
            elif operator == "$ne":
                if exists and actual == operand:
                    return False
            elif operator == "$in":
                if not exists or actual not in operand:
                    return False
            elif operator == "$nin":
                if exists and actual in operand:
                    return False
            elif operator == "$regex":
                if not exists or re.search(str(operand), str(actual)) is None:
                    return False
            elif operator == "$options":
                # The tests only use regex options together with $regex.  The
                # in-memory matcher is intentionally case-sensitive by default.
                continue
            else:
                raise AssertionError(f"unsupported fake Mongo operator: {operator}")
    return True


def _project(document: dict[str, Any], projection: dict[str, int] | None) -> dict[str, Any]:
    if not projection:
        return copy.deepcopy(document)
    included = {key for key, value in projection.items() if value}
    if included:
        result = {"_id": document["_id"]} if "_id" in document else {}
        for key in included:
            exists, value = _lookup(document, key)
            if exists:
                result[key] = copy.deepcopy(value)
        return result
    result = copy.deepcopy(document)
    for key, value in projection.items():
        if not value:
            result.pop(key, None)
    return result


class _Collection:
    """Small async collection model with Mongo-like atomic writes for tests."""

    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self._documents: dict[str, dict[str, Any]] = {
            str(document["_id"]): copy.deepcopy(document)
            for document in documents or []
        }
        self._indexes: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._documents)

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        self._indexes.append(
            {
                "keys": [keys] if isinstance(keys, str) else list(keys),
                "unique": bool(kwargs.get("unique")),
                "sparse": bool(kwargs.get("sparse")),
                "partial": kwargs.get("partialFilterExpression"),
            }
        )
        return str(kwargs.get("name") or "fake-index")

    def _check_unique(self, candidate: dict[str, Any], *, exclude_id: str | None = None) -> None:
        for index in self._indexes:
            if not index["unique"]:
                continue
            partial = index["partial"]
            if partial and not _matches(candidate, partial):
                continue
            values: list[Any] = []
            missing = False
            for key in index["keys"]:
                field = key[0] if isinstance(key, tuple) else key
                exists, value = _lookup(candidate, str(field))
                if not exists:
                    missing = True
                    values.append(None)
                else:
                    values.append(value)
            if missing and index["sparse"]:
                continue
            for document_id, existing in self._documents.items():
                if exclude_id is not None and document_id == exclude_id:
                    continue
                if index["partial"] and not _matches(existing, index["partial"]):
                    continue
                existing_values: list[Any] = []
                existing_missing = False
                for key in index["keys"]:
                    field = key[0] if isinstance(key, tuple) else key
                    exists, value = _lookup(existing, str(field))
                    if not exists:
                        existing_missing = True
                        existing_values.append(None)
                    else:
                        existing_values.append(value)
                if existing_missing and index["sparse"]:
                    continue
                if existing_values == values:
                    raise DuplicateKeyError("duplicate key in fake collection")

    @staticmethod
    def _apply(document: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(document)
        for key, values in update.items():
            if key == "$set":
                for field, value in values.items():
                    result[field] = copy.deepcopy(value)
            elif key == "$unset":
                for field in values:
                    result.pop(field, None)
            elif key == "$inc":
                for field, value in values.items():
                    result[field] = result.get(field, 0) + value
            else:
                raise AssertionError(f"unsupported fake Mongo update operator: {key}")
        return result

    async def find_one(
        self,
        query: dict[str, Any],
        projection: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            for document in self._documents.values():
                if _matches(document, query):
                    return _project(document, projection)
        return None

    def find(
        self,
        query: dict[str, Any],
        projection: dict[str, int] | None = None,
    ) -> _Cursor:
        documents = [
            _project(document, projection)
            for document in self._documents.values()
            if _matches(document, query)
        ]
        return _Cursor(documents)

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        return_document: ReturnDocument = ReturnDocument.BEFORE,
        sort: list[tuple[str, int]] | None = None,
        projection: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            documents = [
                document
                for document in self._documents.values()
                if _matches(document, query)
            ]
            if sort:
                for field, direction in reversed(sort):
                    documents.sort(
                        key=lambda item: _lookup(item, field)[1],
                        reverse=direction < 0,
                    )
            if not documents:
                return None
            before = copy.deepcopy(documents[0])
            after = self._apply(before, update)
            self._check_unique(after, exclude_id=str(before["_id"]))
            self._documents[str(before["_id"])] = after
            selected = after if return_document == ReturnDocument.AFTER else before
            return _project(selected, projection)

    async def insert_one(self, document: dict[str, Any]) -> Any:
        async with self._lock:
            candidate = copy.deepcopy(document)
            document_id = str(candidate["_id"])
            self._check_unique(candidate)
            if document_id in self._documents:
                raise DuplicateKeyError("duplicate _id in fake collection")
            self._documents[document_id] = candidate
            return SimpleNamespace(inserted_id=document_id)

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        **_kwargs: Any,
    ) -> _WriteResult:
        async with self._lock:
            for document_id, document in self._documents.items():
                if not _matches(document, query):
                    continue
                updated = self._apply(document, update)
                self._check_unique(updated, exclude_id=document_id)
                changed = updated != document
                self._documents[document_id] = updated
                return _WriteResult(matched_count=1, modified_count=int(changed))
        return _WriteResult()

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> _WriteResult:
        async with self._lock:
            modified = 0
            for document_id, document in list(self._documents.items()):
                if not _matches(document, query):
                    continue
                updated = self._apply(document, update)
                self._check_unique(updated, exclude_id=document_id)
                modified += int(updated != document)
                self._documents[document_id] = updated
            return _WriteResult(matched_count=modified, modified_count=modified)

    async def delete_one(self, query: dict[str, Any]) -> _WriteResult:
        async with self._lock:
            for document_id, document in list(self._documents.items()):
                if _matches(document, query):
                    del self._documents[document_id]
                    return _WriteResult(deleted_count=1)
        return _WriteResult()

    async def delete_many(self, query: dict[str, Any]) -> _WriteResult:
        async with self._lock:
            deleted = 0
            for document_id, document in list(self._documents.items()):
                if _matches(document, query):
                    del self._documents[document_id]
                    deleted += 1
            return _WriteResult(deleted_count=deleted)


class _Manager:
    def __init__(
        self,
        *,
        accounts: list[dict[str, Any]] | None = None,
        emails: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        outlook_accounts: list[dict[str, Any]] | None = None,
        migrations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.online = True
        self.database = {
            "accounts": _Collection(accounts),
            "emails": _Collection(emails),
            "email_change_runs": _Collection(runs),
            "outlook_accounts": _Collection(outlook_accounts),
            "outlook_migrations": _Collection(migrations),
        }

    def require_online(self) -> None:
        return None

    def mark_offline(self, _error: BaseException) -> None:
        self.online = False


def _account(account_id: str, email: str, **extra: Any) -> dict[str, Any]:
    document = {
        "_id": account_id,
        "email": email,
        "emailNormalized": email.casefold(),
        "emailAccessUrl": f"https://mail.test/{account_id}",
    }
    document.update(extra)
    return document


def _mailbox(mailbox_id: str, email: str, **extra: Any) -> dict[str, Any]:
    document = {
        "_id": mailbox_id,
        "email": email,
        "emailNormalized": email.casefold(),
        "accessUrl": f"https://mail.test/{mailbox_id}",
        "status": "available",
    }
    document.update(extra)
    return document


def _store(manager: _Manager) -> MongoEmailChangeStore:
    return MongoEmailChangeStore(SimpleNamespace(manager=manager))


def test_public_run_does_not_echo_private_error_diagnostics() -> None:
    result = MongoEmailChangeStore._public(
        {
            "runId": "run-secret",
            "status": "failed",
            "errorCode": "unexpected_RuntimeError",
            "errorMessage": "Authorization: Bearer SECRET_TOKEN",
            "errorDiagnostic": "https://mail.test/?token=SECRET_TOKEN",
        }
    )

    assert result["errorMessage"] == "邮箱换绑后台任务异常"
    assert "SECRET_TOKEN" not in repr(result)


def test_target_mailbox_is_reserved_by_only_one_concurrent_change() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[
                _account("account-1", "one@example.com"),
                _account("account-2", "two@example.com"),
            ],
            emails=[_mailbox("target", "target@example.com")],
        )
        store = _store(manager)
        await store.ensure_indexes()

        outcomes = await asyncio.gather(
            store.create_run("account-1", "target"),
            store.create_run("account-2", "target"),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], (DuplicateResourceError, ResourceNotFoundError))

        target = manager.database["emails"].documents["target"]
        assert target["status"] == "reserved"
        assert target["reservationKind"] == "email_change"
        assert target["reservedBy"] == f"email-change:{successes[0]['runId']}"
        assert len(manager.database["email_change_runs"].documents) == 1

    asyncio.run(scenario())


def test_same_account_concurrent_changes_leave_one_run_and_release_loser_target() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "one@example.com")],
            emails=[
                _mailbox("target-a", "target-a@example.com"),
                _mailbox("target-b", "target-b@example.com"),
            ],
        )
        store = _store(manager)
        await store.ensure_indexes()

        outcomes = await asyncio.gather(
            store.create_run("account-1", "target-a"),
            store.create_run("account-1", "target-b"),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], DuplicateResourceError)
        winner = successes[0]
        documents = manager.database["emails"].documents
        assert documents[winner["targetEmailId"]]["status"] == "reserved"
        loser_id = "target-b" if winner["targetEmailId"] == "target-a" else "target-a"
        assert documents[loser_id]["status"] == "available"
        assert len(manager.database["email_change_runs"].documents) == 1
        account = manager.database["accounts"].documents["account-1"]
        assert account["emailChangeRunId"] == winner["runId"]

    asyncio.run(scenario())


def test_manual_account_without_source_loses_to_concurrent_email_change_reservation() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "old@example.com")],
            emails=[_mailbox("target", "target@example.com")],
        )
        resources = MongoResourceStore(manager)
        changes = _store(manager)
        await changes.ensure_indexes()

        emails = manager.database["emails"]
        original_claim = emails.find_one_and_update
        manual_claim_ready = asyncio.Event()
        resume_manual_claim = asyncio.Event()

        async def gated_claim(query, update, **kwargs):
            reservation = update.get("$set", {}).get("reservationKind")
            if reservation == "manual_account":
                manual_claim_ready.set()
                await resume_manual_claim.wait()
            return await original_claim(query, update, **kwargs)

        emails.find_one_and_update = gated_claim  # type: ignore[method-assign]
        manual = asyncio.create_task(
            resources.create_account(
                AccountCreate(
                    email="target@example.com",
                    chatgptPassword="password",
                    totpSecret="totp-secret",
                    emailAccessUrl="https://mail.test/target",
                )
            )
        )
        await manual_claim_ready.wait()
        run = await changes.create_run("account-1", "target")
        resume_manual_claim.set()

        with pytest.raises(DuplicateResourceError, match="邮箱"):
            await manual
        assert run["status"] == "target_reserved"
        accounts = manager.database["accounts"].documents
        assert set(accounts) == {"account-1"}
        target = manager.database["emails"].documents["target"]
        assert target["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_outlook_account_creation_keeps_assigned_graph_mailbox_and_association() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "outlook:account-1",
                    "target@example.com",
                    accessUrl="outlook://account-1",
                    mailboxKind="outlook_graph",
                    sourceType="outlook",
                    outlookAccountId="account-1",
                )
            ]
        )
        manager.database["emails"]._indexes.append({"keys": [("emailNormalized", 1)], "unique": True, "sparse": False, "partial": None})
        resources = MongoResourceStore(manager)
        created = await resources.create_account(
            AccountCreate(
                email="target@example.com",
                chatgptPassword="password",
                totpSecret="totp-secret",
                emailAccessUrl="outlook://account-1",
                sourceEmailId="outlook:account-1",
            )
        )
        mailbox = manager.database["emails"].documents["outlook:account-1"]
        account = manager.database["accounts"].documents[created.id]
        assert mailbox["status"] == "assigned"
        assert mailbox["outlookAccountId"] == "account-1"
        assert account["mailboxKind"] == "outlook_graph"
        assert account["outlookAccountId"] == "account-1"
        assert account["emailAccessUrl"] == "outlook://account-1"

    asyncio.run(scenario())


def test_manual_account_without_source_can_claim_mailbox_before_email_change() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "old@example.com")],
            emails=[_mailbox("target", "target@example.com")],
        )
        resources = MongoResourceStore(manager)
        changes = _store(manager)

        created = await resources.create_account(
            AccountCreate(
                email="target@example.com",
                chatgptPassword="password",
                totpSecret="totp-secret",
                emailAccessUrl="https://mail.test/target",
            )
        )

        stored = manager.database["accounts"].documents[created.id]
        assert stored["sourceEmailId"] == "target"
        assert "target" not in manager.database["emails"].documents
        with pytest.raises(ResourceNotFoundError):
            await changes.create_run("account-1", "target")

    asyncio.run(scenario())


def test_explicit_source_account_and_email_change_have_one_concurrent_winner() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "old@example.com")],
            emails=[_mailbox("target", "target@example.com")],
        )
        resources = MongoResourceStore(manager)
        changes = _store(manager)
        await changes.ensure_indexes()

        outcomes = await asyncio.gather(
            resources.create_account(
                AccountCreate(
                    email="target@example.com",
                    chatgptPassword="password",
                    totpSecret="totp-secret",
                    emailAccessUrl="https://mail.test/target",
                    sourceEmailId="target",
                )
            ),
            changes.create_run("account-1", "target"),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, BaseException)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], (DuplicateResourceError, ResourceNotFoundError))
        target_accounts = [
            item
            for item in manager.database["accounts"].documents.values()
            if item.get("emailNormalized") == "target@example.com"
        ]
        runs = manager.database["email_change_runs"].documents
        assert bool(target_accounts) is not bool(runs)

    asyncio.run(scenario())


def test_commit_account_email_uses_account_email_and_run_marker_as_cas() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[
                _account(
                    "account-1",
                    "changed@example.com",
                    emailChangeRunId="run-1",
                    emailChangeTargetEmailId="target",
                )
            ],
            emails=[_mailbox("target", "target@example.com", status="reserved")],
        )
        store = _store(manager)
        run = {
            "runId": "run-1",
            "accountId": "account-1",
            "oldEmail": "one@example.com",
            "oldEmailNormalized": "one@example.com",
            "targetEmail": "target@example.com",
            "targetEmailId": "target",
        }
        target = manager.database["emails"].documents["target"]
        remote = SimpleNamespace(account_updates={"accessToken": "new-token"})

        assert await store.commit_account_email(run, target, remote) is False
        account = manager.database["accounts"].documents["account-1"]
        assert account["email"] == "changed@example.com"
        assert account["emailChangeRunId"] == "run-1"
        assert "accessToken" not in account

    asyncio.run(scenario())


def test_commit_account_email_clears_mailcom_credentials_for_url_target() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[
                _account(
                    "account-1",
                    "old@example.com",
                    mailboxKind="mailcom_imap",
                    mailboxPassword="old-secret",
                    emailChangeRunId="run-1",
                )
            ],
            emails=[_mailbox("target", "target@example.com", status="reserved")],
            runs=[],
        )
        store = _store(manager)
        run = {
            "runId": "run-1",
            "accountId": "account-1",
            "oldEmail": "old@example.com",
            "oldEmailNormalized": "old@example.com",
            "targetEmail": "target@example.com",
            "targetEmailId": "target",
        }
        target = manager.database["emails"].documents["target"]
        target["status"] = "reserved"
        remote = SimpleNamespace(account_updates={})

        assert await store.commit_account_email(run, target, remote) is True
        account = manager.database["accounts"].documents["account-1"]
        assert account["mailboxKind"] == "url"
        assert "mailboxPassword" not in account

    asyncio.run(scenario())


def test_delete_accounts_rejects_active_email_change_lock() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "old@example.com", emailChangeRunId="run-1")]
        )
        resources = MongoResourceStore(manager)
        with pytest.raises(DuplicateResourceError, match="邮箱换绑"):
            await resources.delete_accounts(["account-1"])
        assert "account-1" in manager.database["accounts"].documents

    asyncio.run(scenario())


def test_claim_is_single_consumer_for_one_email_change_run() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "target_reserved",
                    "stage": "target_reserved",
                    "accountId": "account-1",
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                    "attempts": 0,
                    "cancelRequested": False,
                }
            ]
        )
        store = _store(manager)
        await store.ensure_indexes()

        outcomes = await asyncio.gather(
            store.claim("run-1"),
            store.claim("run-1"),
        )

        assert sum(item is not None and item.get("attempts") == 1 for item in outcomes) == 1
        assert manager.database["email_change_runs"].documents["run-1"]["attempts"] == 1

    asyncio.run(scenario())


def test_recovery_claim_accepts_remote_confirmed_run_with_stale_cancel_flag() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "remote_verified",
                    "stage": "remote_verified",
                    "remoteConfirmed": True,
                    "cancelRequested": True,
                    "attempts": 1,
                }
            ]
        )
        store = _store(manager)

        claimed = await store.claim("run-1")

        assert claimed is not None
        assert claimed["status"] == "committing"
        assert claimed["remoteConfirmed"] is True

    asyncio.run(scenario())


def test_remote_confirmed_recovery_claim_is_single_consumer() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "remote_verified",
                    "stage": "remote_verified",
                    "remoteConfirmed": True,
                }
            ]
        )
        store = _store(manager)

        outcomes = await asyncio.gather(store.claim("run-1"), store.claim("run-1"))

        assert sum(item is not None for item in outcomes) == 1
        assert manager.database["email_change_runs"].documents["run-1"]["status"] == "committing"

    asyncio.run(scenario())


def test_late_worker_cannot_resurrect_completed_run() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "completed",
                    "stage": "completed",
                    "finishedAt": datetime.now(timezone.utc),
                }
            ]
        )
        store = _store(manager)

        result = await store.mark(
            "run-1",
            status="cleanup_pending",
            stage="cleanup_pending",
            errorCode="target_cleanup_pending",
        )

        assert result["status"] == "completed"
        stored = manager.database["email_change_runs"].documents["run-1"]
        assert stored["status"] == "completed"
        assert "errorCode" not in stored

    asyncio.run(scenario())


def test_completed_run_drops_private_remote_session_updates() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "committing",
                    "stage": "committing",
                    "remoteAccountUpdates": {"refreshToken": "SECRET_REFRESH", "accessToken": "SECRET_ACCESS"},
                    "claimOwner": "worker-1",
                }
            ]
        )
        store = _store(manager)
        result = await store.mark("run-1", status="completed", stage="completed")
        assert result["status"] == "completed"
        stored = manager.database["email_change_runs"].documents["run-1"]
        assert "remoteAccountUpdates" not in stored
        assert "claimOwner" not in stored

    asyncio.run(scenario())


def test_cancel_cannot_release_remote_uncertain_target() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "target",
                    "target@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                )
            ],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "target_reserved",
                    "stage": "target_reserved",
                    "remoteUncertain": True,
                    "cancelRequested": True,
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                }
            ],
        )
        store = _store(manager)

        result = await store.request_cancel("run-1")

        assert result["status"] == "target_reserved"
        assert result["cancelRequested"] is False
        target = manager.database["emails"].documents["target"]
        assert target["status"] == "reserved"
        assert target["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_registration_release_does_not_touch_email_change_reservation() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "registration",
                    "registration@example.com",
                    status="reserved",
                    reservedBy="registration-run",
                    reservationKind="registration",
                    reservedAt="registration-time",
                ),
                _mailbox(
                    "email-change",
                    "change@example.com",
                    status="reserved",
                    reservedBy="registration-run",
                    reservationKind="email_change",
                    reservedAt="change-time",
                ),
            ]
        )
        resources = MongoResourceStore(manager)

        await resources.release_run_reservations("registration-run")

        documents = manager.database["emails"].documents
        assert documents["registration"]["status"] == "available"
        assert "reservedBy" not in documents["registration"]
        assert documents["email-change"]["status"] == "reserved"
        assert documents["email-change"]["reservedBy"] == "registration-run"
        assert documents["email-change"]["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_bulk_email_delete_does_not_remove_email_change_target() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox("available", "available@example.com"),
                _mailbox(
                    "change",
                    "change@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                ),
            ]
        )
        resources = MongoResourceStore(manager)  # type: ignore[arg-type]

        result = await resources.delete_emails(["available", "change"])

        assert result.deleted == 1
        assert set(manager.database["emails"].documents) == {"change"}
        assert manager.database["emails"].documents["change"]["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_single_registration_email_release_does_not_touch_email_change_target() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "target",
                    "change@example.com",
                    status="reserved",
                    reservedBy="same-owner",
                    reservationKind="email_change",
                )
            ]
        )
        resources = MongoResourceStore(manager)

        await resources.release_email("target", "same-owner")
        assert manager.database["emails"].documents["target"]["status"] == "reserved"
        assert manager.database["emails"].documents["target"]["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_orphan_release_does_not_touch_email_change_reservation() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "orphan-registration",
                    "registration@example.com",
                    status="reserved",
                    reservedBy="stale-run",
                    reservationKind="registration",
                ),
                _mailbox(
                    "orphan-change",
                    "change@example.com",
                    status="reserved",
                    reservedBy="stale-run",
                    reservationKind="email_change",
                ),
            ]
        )
        resources = MongoResourceStore(manager)

        assert await resources.release_orphaned_reservations([]) == 1

        documents = manager.database["emails"].documents
        assert documents["orphan-registration"]["status"] == "available"
        assert documents["orphan-change"]["status"] == "reserved"
        assert documents["orphan-change"]["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_reconcile_registration_run_does_not_consume_email_change_target() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "registration",
                    "registration@example.com",
                    status="reserved",
                    reservedBy="run-1",
                    reservationKind="registration",
                ),
                _mailbox(
                    "change",
                    "change@example.com",
                    status="reserved",
                    reservedBy="run-1",
                    reservationKind="email_change",
                ),
            ]
        )
        resources = MongoResourceStore(manager)

        consumed, released = await resources.reconcile_run_reservations("run-1")

        assert consumed == 0
        assert released == 1
        documents = manager.database["emails"].documents
        assert documents["registration"]["status"] == "available"
        assert documents["change"]["status"] == "reserved"
        assert documents["change"]["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_cleanup_recovery_is_idempotent_when_target_was_already_consumed() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "target@example.com")],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "cleanup_pending",
                    "stage": "cleanup_pending",
                    "accountId": "account-1",
                    "targetEmailId": "target",
                    "targetEmailNormalized": "target@example.com",
                    "reservationOwner": "email-change:run-1",
                }
            ],
        )
        store = _store(manager)
        await store.ensure_indexes()

        assert await store.recover_cleanup() == 1
        run = manager.database["email_change_runs"].documents["run-1"]
        assert run["status"] == "completed"

    asyncio.run(scenario())


def test_active_recovery_reserves_queued_target_and_repairs_account_marker() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("account-1", "old@example.com")],
            emails=[_mailbox("target", "target@example.com")],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "queued",
                    "stage": "queued",
                    "accountId": "account-1",
                    "oldEmail": "old@example.com",
                    "oldEmailNormalized": "old@example.com",
                    "targetEmailId": "target",
                    "targetEmail": "target@example.com",
                    "targetEmailNormalized": "target@example.com",
                    "reservationOwner": "email-change:run-1",
                    "activeAccountKey": "account-1",
                }
            ],
        )
        store = _store(manager)
        await store.ensure_indexes()

        assert await store.recover_active() == ["run-1"]
        run = manager.database["email_change_runs"].documents["run-1"]
        target = manager.database["emails"].documents["target"]
        account = manager.database["accounts"].documents["account-1"]
        assert run["status"] == "target_reserved"
        assert target["reservationKind"] == "email_change"
        assert account["emailChangeRunId"] == "run-1"

    asyncio.run(scenario())


def test_active_recovery_marks_interrupted_remote_attempt_uncertain() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[_mailbox("target", "target@example.com", status="reserved", reservedBy="email-change:run-1", reservationKind="email_change")],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "remote_submitted",
                    "stage": "remote_submitted",
                    "remoteUncertain": True,
                    "accountId": "account-1",
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                    "activeAccountKey": "account-1",
                }
            ],
        )
        store = _store(manager)
        await store.ensure_indexes()

        assert await store.recover_active() == ["run-1"]
        run = manager.database["email_change_runs"].documents["run-1"]
        assert run["status"] == "target_reserved"
        assert run["remoteUncertain"] is True

    asyncio.run(scenario())


def test_active_recovery_retries_remote_attempt_that_stopped_before_verify() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[
                _account(
                    "account-1",
                    "old@example.com",
                    emailChangeRunId="run-1",
                    emailChangeTargetEmailId="target",
                )
            ],
            emails=[
                _mailbox(
                    "target",
                    "target@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                )
            ],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "remote_submitted",
                    "stage": "remote_submitted",
                    "remoteUncertain": False,
                    "accountId": "account-1",
                    "oldEmail": "old@example.com",
                    "oldEmailNormalized": "old@example.com",
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                    "claimOwner": "dead-worker",
                }
            ],
        )
        store = _store(manager)

        assert await store.recover_active() == ["run-1"]
        recovered = manager.database["email_change_runs"].documents["run-1"]
        assert recovered["status"] == "target_reserved"
        assert recovered["remoteUncertain"] is False
        assert "claimOwner" not in recovered
        claimed = await store.claim("run-1")
        assert claimed is not None
        assert claimed["status"] == "remote_submitted"

    asyncio.run(scenario())


def test_active_recovery_finishes_pre_remote_cancellation() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[
                _account(
                    "account-1",
                    "old@example.com",
                    emailChangeRunId="run-1",
                    emailChangeTargetEmailId="target",
                )
            ],
            emails=[
                _mailbox(
                    "target",
                    "target@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                )
            ],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "target_reserved",
                    "stage": "target_reserved",
                    "accountId": "account-1",
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                    "cancelRequested": True,
                    "activeAccountKey": "account-1",
                }
            ],
        )
        store = _store(manager)

        assert await store.recover_active() == []
        run = manager.database["email_change_runs"].documents["run-1"]
        account = manager.database["accounts"].documents["account-1"]
        target = manager.database["emails"].documents["target"]
        assert run["status"] == "cancelled"
        assert target["status"] == "available"
        assert "emailChangeRunId" not in account

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["remote_verified", "cleanup_pending"])
def test_active_recovery_clears_stale_claim_before_reclaim(status: str) -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": status,
                    "stage": status,
                    "remoteConfirmed": True,
                    "claimOwner": "dead-worker",
                    "claimStartedAt": datetime.now(timezone.utc),
                }
            ]
        )
        store = _store(manager)

        assert await store.recover_active() == ["run-1"]
        recovered = manager.database["email_change_runs"].documents["run-1"]
        assert "claimOwner" not in recovered
        claimed = await store.claim("run-1")
        assert claimed is not None
        assert claimed["status"] == "committing"
        assert claimed["claimOwner"] != "dead-worker"

    asyncio.run(scenario())


def test_cleanup_pending_remote_uncertain_is_reclaimable() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "cleanup_pending",
                    "stage": "cleanup_pending",
                    "remoteUncertain": True,
                    "claimOwner": "dead-worker",
                }
            ]
        )
        store = _store(manager)

        assert await store.recover_active() == ["run-1"]
        claimed = await store.claim("run-1")
        assert claimed is not None
        assert claimed["status"] == "committing"
        assert claimed["remoteUncertain"] is True

    asyncio.run(scenario())


def test_release_target_rechecks_remote_boundary_before_releasing() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "target",
                    "target@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                )
            ],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "remote_verified",
                    "remoteConfirmed": True,
                }
            ],
        )
        store = _store(manager)

        stale_snapshot = {
            "runId": "run-1",
            "targetEmailId": "target",
            "reservationOwner": "email-change:run-1",
            "remoteConfirmed": False,
        }
        assert await store.release_target(stale_snapshot) is False
        target = manager.database["emails"].documents["target"]
        assert target["status"] == "reserved"
        assert target["reservationKind"] == "email_change"

    asyncio.run(scenario())


def test_terminal_run_drops_remote_updates_and_sets_finished_at() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "cleanup_pending",
                    "remoteAccountUpdates": {"refreshToken": "SECRET_REFRESH"},
                    "claimOwner": "worker-1",
                }
            ]
        )
        store = _store(manager)

        result = await store.mark("run-1", status="failed", stage="failed")
        assert result["status"] == "failed"
        stored = manager.database["email_change_runs"].documents["run-1"]
        assert "remoteAccountUpdates" not in stored
        assert "claimOwner" not in stored
        assert stored["finishedAt"].tzinfo is not None

    asyncio.run(scenario())


def test_interrupted_committing_run_without_boundary_is_recovered_uncertain() -> None:
    async def scenario() -> None:
        manager = _Manager(
            emails=[
                _mailbox(
                    "target",
                    "target@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                )
            ],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "committing",
                    "stage": "committing",
                    "claimOwner": "dead-worker",
                    "cancelRequested": True,
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                }
            ],
        )
        store = _store(manager)

        assert await store.recover_active() == ["run-1"]
        recovered = manager.database["email_change_runs"].documents["run-1"]
        assert recovered["status"] == "target_reserved"
        assert recovered["remoteUncertain"] is True
        assert recovered["cancelRequested"] is False
        assert "claimOwner" not in recovered
        target = manager.database["emails"].documents["target"]
        assert target["status"] == "reserved"

    asyncio.run(scenario())


def test_active_recovery_finishes_remote_submitted_cancel_before_verify_boundary() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[
                _account(
                    "account-1",
                    "old@example.com",
                    emailChangeRunId="run-1",
                    emailChangeTargetEmailId="target",
                )
            ],
            emails=[
                _mailbox(
                    "target",
                    "target@example.com",
                    status="reserved",
                    reservedBy="email-change:run-1",
                    reservationKind="email_change",
                )
            ],
            runs=[
                {
                    "_id": "run-1",
                    "runId": "run-1",
                    "status": "remote_submitted",
                    "stage": "remote_submitted",
                    "accountId": "account-1",
                    "targetEmailId": "target",
                    "reservationOwner": "email-change:run-1",
                    "cancelRequested": True,
                    "remoteConfirmed": False,
                    "remoteUncertain": False,
                    "claimOwner": "dead-worker",
                    "activeAccountKey": "account-1",
                }
            ],
        )
        store = _store(manager)

        assert await store.recover_active() == []
        run = manager.database["email_change_runs"].documents["run-1"]
        account = manager.database["accounts"].documents["account-1"]
        target = manager.database["emails"].documents["target"]
        assert run["status"] == "cancelled"
        assert "claimOwner" not in run
        assert target["status"] == "available"
        assert "emailChangeRunId" not in account

    asyncio.run(scenario())


def test_begin_remote_verify_is_atomic_with_cancellation() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "cancel-won",
                    "runId": "cancel-won",
                    "status": "remote_submitted",
                    "cancelRequested": True,
                    "remoteUncertain": False,
                },
                {
                    "_id": "verify-won",
                    "runId": "verify-won",
                    "status": "remote_submitted",
                    "cancelRequested": False,
                    "remoteUncertain": False,
                },
            ]
        )
        store = _store(manager)

        assert await store.begin_remote_verify("cancel-won", "provider-1") is None
        cancel_won = manager.database["email_change_runs"].documents["cancel-won"]
        assert cancel_won["remoteUncertain"] is False
        assert "providerAccountId" not in cancel_won

        verify_won = await store.begin_remote_verify("verify-won", "provider-1")
        assert verify_won is not None
        assert verify_won["remoteUncertain"] is True
        assert verify_won["providerAccountId"] == "provider-1"
        cancelled = await store.request_cancel("verify-won")
        assert cancelled["cancelRequested"] is False

    asyncio.run(scenario())


def test_finish_remote_rejection_clears_only_the_current_unconfirmed_boundary() -> None:
    async def scenario() -> None:
        manager = _Manager(
            runs=[
                {
                    "_id": "rejected",
                    "runId": "rejected",
                    "status": "remote_submitted",
                    "remoteConfirmed": False,
                    "remoteUncertain": True,
                    "claimOwner": "worker-1",
                },
                {
                    "_id": "confirmed",
                    "runId": "confirmed",
                    "status": "remote_verified",
                    "remoteConfirmed": True,
                    "remoteUncertain": False,
                    "claimOwner": "worker-1",
                },
            ]
        )
        store = _store(manager)

        assert await store.finish_remote_rejection("rejected") is None
        assert await store.finish_remote_rejection(
            "rejected", claim_owner="other-worker"
        ) is None
        cleared = await store.finish_remote_rejection(
            "rejected", claim_owner="worker-1"
        )
        assert cleared is not None
        assert cleared["remoteUncertain"] is False

        assert await store.finish_remote_rejection(
            "confirmed", claim_owner="worker-1"
        ) is None
        confirmed = manager.database["email_change_runs"].documents["confirmed"]
        assert confirmed["remoteConfirmed"] is True

    asyncio.run(scenario())


def test_releasing_outlook_target_checks_oauth_and_graph_before_republishing() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("gpt-1", "gpt@example.com")],
            emails=[_mailbox("target", "target@example.com", sourceType="outlook", outlookAccountId="outlook-1", mailboxKind="outlook_graph")],
            outlook_accounts=[{"_id": "outlook-1", "oauthStatus": "ok", "graphStatus": "error"}],
        )
        store = _store(manager)
        await store.ensure_indexes()
        run = await store.create_run("gpt-1", "target")
        run["reservationOwner"] = f"email-change:{run['runId']}"

        assert await store.release_target(run)
        target = manager.database["emails"].documents["target"]
        assert target["status"] == "unavailable"
        assert target.get("reservedBy") is None
        assert target["outlookAccountId"] == "outlook-1"

    asyncio.run(scenario())


def test_releasing_valid_outlook_target_returns_it_to_available_pool() -> None:
    async def scenario() -> None:
        manager = _Manager(
            accounts=[_account("gpt-1", "gpt@example.com")],
            emails=[_mailbox("target", "target@example.com", sourceType="outlook", outlookAccountId="outlook-1", mailboxKind="outlook_graph")],
            outlook_accounts=[{"_id": "outlook-1", "oauthStatus": "ok", "graphStatus": "ok"}],
        )
        store = _store(manager)
        await store.ensure_indexes()
        run = await store.create_run("gpt-1", "target")
        run["reservationOwner"] = f"email-change:{run['runId']}"

        assert await store.release_target(run)
        target = manager.database["emails"].documents["target"]
        assert target["status"] == "available"
        assert target.get("reservationKind") is None
        assert target["outlookAccountId"] == "outlook-1"

    asyncio.run(scenario())
