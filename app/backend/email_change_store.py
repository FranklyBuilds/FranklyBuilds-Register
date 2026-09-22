from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .email_change_models import normalize_email, public_error_message
from .errors import DuplicateResourceError, MongoUnavailableError, ResourceNotFoundError


ACTIVE_STATUSES = {"queued", "target_reserved", "remote_submitted", "remote_verified", "committing", "cleanup_pending"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
# Keep terminal state available long enough for the UI to display the result,
# while ensuring transient auth material cannot remain indefinitely.
EMAIL_CHANGE_TERMINAL_TTL_SECONDS = 7 * 24 * 60 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MongoEmailChangeStore:
    """Mongo persistence for email changes, separate from registration runs."""

    def __init__(self, resource_store: Any) -> None:
        self.resources = resource_store
        self.manager = resource_store.manager

    @property
    def runs(self) -> Any:
        return self.manager.database["email_change_runs"]

    @property
    def accounts(self) -> Any:
        return self.manager.database["accounts"]

    @property
    def emails(self) -> Any:
        return self.manager.database["emails"]

    async def _guard(self, awaitable: Any) -> Any:
        self.manager.require_online()
        try:
            return await awaitable
        except DuplicateKeyError:
            raise
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务") from exc

    async def ensure_indexes(self) -> None:
        self.manager.require_online()
        await self._guard(
            self.runs.create_index("runId", unique=True, name="email_change_run_id_unique")
        )
        await self._guard(
            self.runs.create_index(
                "activeAccountKey",
                unique=True,
                partialFilterExpression={
                    "activeAccountKey": {"$exists": True},
                    "status": {"$in": list(ACTIVE_STATUSES)},
                },
                name="email_change_active_account_unique",
            )
        )
        await self._guard(
            self.runs.create_index(
                [("status", ASCENDING), ("createdAt", ASCENDING)],
                name="email_change_status_created",
            )
        )
        await self._guard(
            self.runs.create_index(
                [("updatedAt", DESCENDING)],
                name="email_change_updated_desc",
            )
        )
        await self._guard(
            self.runs.create_index(
                "finishedAt",
                expireAfterSeconds=EMAIL_CHANGE_TERMINAL_TTL_SECONDS,
                partialFilterExpression={
                    "status": {"$in": list(TERMINAL_STATUSES)},
                    "finishedAt": {"$exists": True},
                },
                name="email_change_terminal_ttl",
            )
        )

    @staticmethod
    def _public(document: dict[str, Any]) -> dict[str, Any]:
        result = {
            "runId": str(document.get("runId") or document.get("_id") or ""),
            "kind": "email_change",
            "status": str(document.get("status") or "failed"),
            "accountId": str(document.get("accountId") or ""),
            "oldEmail": str(document.get("oldEmail") or ""),
            "targetEmailId": str(document.get("targetEmailId") or ""),
            "targetEmail": str(document.get("targetEmail") or ""),
            "stage": str(document.get("stage") or document.get("status") or ""),
            "attempts": int(document.get("attempts") or 0),
            "errorCode": document.get("errorCode"),
            "errorMessage": public_error_message(document.get("errorCode")),
            "cancelRequested": bool(document.get("cancelRequested", False)),
            "createdAt": document.get("createdAt") or _now(),
            "startedAt": document.get("startedAt"),
            "updatedAt": document.get("updatedAt") or _now(),
            "finishedAt": document.get("finishedAt"),
        }
        return result

    async def create_run(self, account_id: str, target_email_id: str) -> dict[str, Any]:
        account = await self._guard(self.accounts.find_one({"_id": account_id}))
        if account is None:
            raise ResourceNotFoundError("账号不存在")
        old_email = str(account.get("email") or "").strip()
        old_normalized = normalize_email(account.get("emailNormalized") or old_email)
        target = await self._guard(
            self.emails.find_one({"_id": target_email_id, "status": "available"})
        )
        if target is None:
            raise ResourceNotFoundError("目标邮箱不存在或已被占用")
        target_normalized = normalize_email(target.get("emailNormalized") or target.get("email"))
        if not target_normalized:
            raise ValueError("目标邮箱格式无效")
        if target_normalized == old_normalized:
            raise ValueError("目标邮箱不能与当前邮箱相同")
        registered = await self._guard(
            self.accounts.find_one(
                {"emailNormalized": target_normalized, "_id": {"$ne": account_id}},
                {"_id": 1},
            )
        )
        if registered is not None:
            raise DuplicateResourceError("目标邮箱已绑定其他账号")
        active = await self._guard(
            self.runs.find_one(
                {"activeAccountKey": str(account_id), "status": {"$in": list(ACTIVE_STATUSES)}},
                {"runId": 1},
            )
        )
        if active is not None:
            raise DuplicateResourceError("该账号已有进行中的邮箱换绑任务")

        run_id = str(uuid4())
        owner = f"email-change:{run_id}"
        now = _now()
        document = {
            "_id": run_id,
            "runId": run_id,
            "activeAccountKey": str(account_id),
            "status": "queued",
            "kind": "email_change",
            "accountId": str(account_id),
            "oldEmail": old_email,
            "oldEmailNormalized": old_normalized,
            "oldEmailAccessUrl": str(account.get("emailAccessUrl") or ""),
            "targetEmailId": str(target_email_id),
            "targetEmail": str(target.get("email") or target_normalized),
            "targetEmailNormalized": target_normalized,
            "targetEmailAccessUrl": str(target.get("accessUrl") or ""),
            "reservationOwner": owner,
            "stage": "target_reserved",
            "attempts": 0,
            "cancelRequested": False,
            "remoteConfirmed": False,
            "remoteUncertain": False,
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            await self._guard(self.runs.insert_one(document))
            reserved = await self._guard(
                self.emails.find_one_and_update(
                    {
                        "_id": target_email_id,
                        "status": "available",
                        "emailNormalized": target_normalized,
                    },
                    {
                        "$set": {
                            "status": "reserved",
                            "reservedBy": owner,
                            "reservationKind": "email_change",
                            "reservedAt": _now(),
                        }
                    },
                    return_document=ReturnDocument.AFTER,
                )
            )
            if reserved is None:
                await self._guard(self.runs.delete_one({"runId": run_id}))
                raise DuplicateResourceError("目标邮箱刚刚被其他任务占用")
            transitioned = await self._guard(
                self.runs.find_one_and_update(
                    {"runId": run_id, "status": "queued"},
                    {"$set": {"status": "target_reserved", "stage": "target_reserved", "updatedAt": _now()}},
                    return_document=ReturnDocument.AFTER,
                )
            )
            if transitioned is None:
                raise MongoUnavailableError("邮箱换绑任务状态更新失败")
            document = dict(transitioned)
            marker = await self._guard(
                self.accounts.update_one(
                    {
                        "_id": account_id,
                        "emailNormalized": old_normalized,
                        "$or": [
                            {"emailChangeRunId": {"$exists": False}},
                            {"emailChangeRunId": None},
                            {"emailChangeRunId": run_id},
                        ],
                    },
                    {
                        "$set": {
                            "emailChangeRunId": run_id,
                            "emailChangeTargetEmailId": target_email_id,
                            "emailChangeUpdatedAt": now,
                        }
                    },
                )
            )
            if int(marker.matched_count) != 1:
                raise DuplicateResourceError("账号邮箱已被其他操作修改")
        except DuplicateKeyError as exc:
            await self.release_target(document)
            raise DuplicateResourceError("该账号已有进行中的邮箱换绑任务") from exc
        except Exception:
            await self.release_target(document)
            with_exception = await self._guard(self.runs.delete_one({"runId": run_id}))
            del with_exception
            raise
        return self._public(document)

    async def get(self, run_id: str) -> dict[str, Any]:
        document = await self._guard(self.runs.find_one({"runId": str(run_id)}))
        if document is None:
            raise ResourceNotFoundError("邮箱换绑任务不存在")
        return self._public(document)

    async def get_private(self, run_id: str) -> dict[str, Any]:
        document = await self._guard(self.runs.find_one({"runId": str(run_id)}))
        if document is None:
            raise ResourceNotFoundError("邮箱换绑任务不存在")
        return dict(document)

    async def claim(self, run_id: str) -> dict[str, Any] | None:
        now = _now()
        claim_owner = f"email-change-worker:{uuid4()}"
        document = await self._guard(
            self.runs.find_one_and_update(
                {
                    "runId": str(run_id),
                    "status": {"$in": ["target_reserved", "queued"]},
                    "cancelRequested": {"$ne": True},
                    "$or": [
                        {"claimOwner": {"$exists": False}},
                        {"claimOwner": None},
                        {"claimOwner": ""},
                    ],
                },
                {
                    "$set": {
                        "status": "remote_submitted",
                        "stage": "remote_submitted",
                        "startedAt": now,
                        "updatedAt": now,
                        "claimOwner": claim_owner,
                        "claimStartedAt": now,
                    },
                    "$inc": {"attempts": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is not None:
            return dict(document)
        document = await self._guard(
            self.runs.find_one_and_update(
                {
                    "runId": str(run_id),
                    "status": {"$in": ["remote_verified", "cleanup_pending"]},
                    "$and": [
                        {"$or": [{"remoteConfirmed": True}, {"remoteUncertain": True}]},
                        {
                            "$or": [
                                {"claimOwner": {"$exists": False}},
                                {"claimOwner": None},
                                {"claimOwner": ""},
                            ]
                        },
                    ],
                },
                {
                    "$set": {
                        "status": "committing",
                        "stage": "committing",
                        "updatedAt": now,
                        "claimOwner": claim_owner,
                        "claimStartedAt": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is not None:
            return dict(document)
        # A run that is already claimed belongs to another worker. Returning
        # it here would let a concurrent caller replay the remote operation.
        return None

    async def begin_commit(
        self,
        run_id: str,
        claim_owner: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim the local commit boundary while observing cancellation."""

        query: dict[str, Any] = {
            "runId": str(run_id),
            "status": {"$in": ["remote_verified", "committing", "cleanup_pending"]},
            "cancelRequested": {"$ne": True},
        }
        if str(claim_owner or "").strip():
            query["claimOwner"] = str(claim_owner).strip()
        document = await self._guard(
            self.runs.find_one_and_update(
                query,
                {"$set": {"status": "committing", "stage": "committing", "updatedAt": _now()}},
                return_document=ReturnDocument.AFTER,
            )
        )
        return dict(document) if document is not None else None

    async def begin_remote_verify(
        self,
        run_id: str,
        provider_account_id: str = "",
    ) -> dict[str, Any] | None:
        """Persist the irreversible boundary only if cancellation has not won."""

        values: dict[str, Any] = {
            "status": "remote_submitted",
            "stage": "remote_submitted",
            "remoteUncertain": True,
            "updatedAt": _now(),
        }
        if str(provider_account_id or "").strip():
            values["providerAccountId"] = str(provider_account_id).strip()
        document = await self._guard(
            self.runs.find_one_and_update(
                {
                    "runId": str(run_id),
                    "status": "remote_submitted",
                    "cancelRequested": {"$ne": True},
                    "remoteConfirmed": {"$ne": True},
                },
                {"$set": values},
                return_document=ReturnDocument.AFTER,
            )
        )
        return dict(document) if document is not None else None

    async def finish_remote_rejection(
        self,
        run_id: str,
        *,
        claim_owner: str = "",
    ) -> dict[str, Any] | None:
        """Clear the verify boundary after a definitive provider rejection."""

        owner = str(claim_owner or "").strip()
        if not owner:
            return None
        query: dict[str, Any] = {
            "runId": str(run_id),
            "status": "remote_submitted",
            "remoteConfirmed": {"$ne": True},
            "remoteUncertain": True,
            "claimOwner": owner,
        }
        document = await self._guard(
            self.runs.find_one_and_update(
                query,
                {"$set": {"remoteUncertain": False, "updatedAt": _now()}},
                return_document=ReturnDocument.AFTER,
            )
        )
        return dict(document) if document is not None else None

    async def claim_next(self) -> dict[str, Any] | None:
        now = _now()
        claim_owner = f"email-change-worker:{uuid4()}"
        return await self._guard(
            self.runs.find_one_and_update(
                {
                    "status": "target_reserved",
                    "cancelRequested": {"$ne": True},
                    "$or": [
                        {"claimOwner": {"$exists": False}},
                        {"claimOwner": None},
                        {"claimOwner": ""},
                    ],
                },
                {
                    "$set": {
                        "status": "remote_submitted",
                        "stage": "remote_submitted",
                        "startedAt": now,
                        "updatedAt": now,
                        "claimOwner": claim_owner,
                        "claimStartedAt": now,
                    },
                    "$inc": {"attempts": 1},
                },
                sort=[("createdAt", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )
        )

    async def mark(self, run_id: str, **changes: Any) -> dict[str, Any]:
        values = dict(changes)
        values.setdefault("updatedAt", _now())
        unset: dict[str, str] = {}
        requested_status = str(values.get("status") or "").casefold()
        if requested_status in TERMINAL_STATUSES:
            unset.update({
                "activeAccountKey": "",
                "claimOwner": "",
                "claimStartedAt": "",
                "remoteAccountUpdates": "",
            })
            values.setdefault("finishedAt", _now())
        elif requested_status == "cleanup_pending":
            # cleanup_pending is a hand-off point. Release the worker claim so
            # a reconnect can acquire the run and resume reconciliation.
            unset.update({"claimOwner": "", "claimStartedAt": ""})
        if requested_status == "completed":
            unset.update({
                "errorCode": "",
                "errorMessage": "",
                "errorDiagnostic": "",
            })
        query: dict[str, Any] = {"runId": str(run_id)}
        if requested_status:
            query["$or"] = [
                {"status": {"$nin": list(TERMINAL_STATUSES)}},
                {"status": requested_status},
            ]
        document = await self._guard(
            self.runs.find_one_and_update(
                query,
                {"$set": values, **({"$unset": unset} if unset else {})},
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            existing = await self._guard(self.runs.find_one({"runId": str(run_id)}))
            if existing is None:
                raise ResourceNotFoundError("邮箱换绑任务不存在")
            return self._public(existing)
        return self._public(document)

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        document = await self._guard(self.runs.find_one({"runId": str(run_id)}))
        if document is None:
            raise ResourceNotFoundError("邮箱换绑任务不存在")
        if document.get("status") in TERMINAL_STATUSES:
            return self._public(document)
        if bool(document.get("remoteConfirmed")) or bool(document.get("remoteUncertain")):
            # A remote success or unknown remote outcome is irreversible.
            # Keep reconciling locally and retain the owned target mailbox.
            if bool(document.get("cancelRequested")):
                updated = await self._guard(
                    self.runs.find_one_and_update(
                        {"runId": str(run_id)},
                        {
                            "$set": {
                                "cancelRequested": False,
                                "cancelIgnoredAt": _now(),
                                "updatedAt": _now(),
                            }
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                )
                return self._public(updated or document)
            return self._public(document)
        updated = await self._guard(
            self.runs.find_one_and_update(
                {
                    "runId": str(run_id),
                    "status": {"$nin": list(TERMINAL_STATUSES)},
                    "remoteConfirmed": {"$ne": True},
                    "remoteUncertain": {"$ne": True},
                },
                {"$set": {"cancelRequested": True, "updatedAt": _now()}},
                return_document=ReturnDocument.AFTER,
            )
        )
        if updated is None:
            latest = await self._guard(self.runs.find_one({"runId": str(run_id)}))
            return self._public(latest or document)
        if updated.get("status") in {"target_reserved", "queued"}:
            await self.release_target(updated)
            await self.clear_account_marker(updated)
            updated = await self._guard(
                self.runs.find_one_and_update(
                    {"runId": str(run_id)},
                    {
                        "$set": {"status": "cancelled", "stage": "cancelled", "finishedAt": _now(), "updatedAt": _now()},
                        "$unset": {"claimOwner": "", "claimStartedAt": "", "activeAccountKey": ""},
                    },
                    return_document=ReturnDocument.AFTER,
                )
            )
        return self._public(updated or document)

    async def load_private_inputs(
        self,
        run: dict[str, Any],
        *,
        allow_missing_target: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        account = await self._guard(self.accounts.find_one({"_id": str(run["accountId"])}))
        target = await self._guard(
            self.emails.find_one(
                {
                    "_id": str(run["targetEmailId"]),
                    "reservedBy": str(run["reservationOwner"]),
                    "reservationKind": "email_change",
                }
            )
        )
        if account is None:
            raise ResourceNotFoundError("邮箱换绑账号不存在")
        if target is None:
            if allow_missing_target and normalize_email(account.get("email")) == normalize_email(
                run.get("targetEmailNormalized") or run.get("targetEmail")
            ):
                # The target may have been consumed just before a process
                # crashed.  Return a synthetic marker so local commit/recovery
                # remains idempotent in the same pass.
                target = {
                    "_id": str(run.get("targetEmailId") or ""),
                    "email": str(run.get("targetEmail") or ""),
                    "emailNormalized": normalize_email(
                        run.get("targetEmailNormalized") or run.get("targetEmail")
                    ),
                    "accessUrl": str(run.get("targetEmailAccessUrl") or ""),
                    "_already_consumed": True,
                }
                return dict(account), target
            raise ResourceNotFoundError("邮箱换绑目标邮箱不存在")
        return dict(account), dict(target)

    async def target_exists(self, run: dict[str, Any]) -> bool:
        """Check whether a target reservation still exists after a crash."""

        target = await self._guard(
            self.emails.find_one(
                {
                    "_id": str(run.get("targetEmailId") or ""),
                    "reservedBy": str(run.get("reservationOwner") or ""),
                    "reservationKind": "email_change",
                },
                {"_id": 1},
            )
        )
        return target is not None

    async def _ensure_account_marker(self, run: dict[str, Any]) -> bool:
        result = await self._guard(
            self.accounts.update_one(
                {
                    "_id": str(run.get("accountId") or ""),
                    "emailNormalized": normalize_email(run.get("oldEmailNormalized") or run.get("oldEmail")),
                    "$or": [
                        {"emailChangeRunId": {"$exists": False}},
                        {"emailChangeRunId": None},
                        {"emailChangeRunId": str(run.get("runId") or "")},
                    ],
                },
                {
                    "$set": {
                        "emailChangeRunId": str(run.get("runId") or ""),
                        "emailChangeTargetEmailId": str(run.get("targetEmailId") or ""),
                        "emailChangeUpdatedAt": _now(),
                    }
                },
            )
        )
        return int(result.matched_count) == 1

    async def _recover_queued_run(self, run: dict[str, Any]) -> dict[str, Any] | None:
        owner = str(run.get("reservationOwner") or "")
        target = await self._guard(
            self.emails.find_one(
                {
                    "_id": str(run.get("targetEmailId") or ""),
                    "reservedBy": owner,
                    "reservationKind": "email_change",
                }
            )
        )
        if target is None:
            target = await self._guard(
                self.emails.find_one_and_update(
                    {
                        "_id": str(run.get("targetEmailId") or ""),
                        "status": "available",
                        "emailNormalized": normalize_email(run.get("targetEmailNormalized") or run.get("targetEmail")),
                    },
                    {
                        "$set": {
                            "status": "reserved",
                            "reservedBy": owner,
                            "reservationKind": "email_change",
                            "reservedAt": _now(),
                        }
                    },
                    return_document=ReturnDocument.AFTER,
                )
            )
        if target is None:
            return None
        if not await self._ensure_account_marker(run):
            return None
        return await self._guard(
            self.runs.find_one_and_update(
                {"runId": str(run.get("runId") or ""), "status": "queued"},
                {"$set": {"status": "target_reserved", "stage": "target_reserved", "updatedAt": _now()}},
                return_document=ReturnDocument.AFTER,
            )
        )

    async def release_target(self, run: dict[str, Any]) -> bool:
        # Re-read the durable boundary before releasing.  A stale worker may
        # still hold an old snapshot after another worker crossed verify; in
        # that case releasing the mailbox would make a remotely bound address
        # available for reuse.
        run_id = str(run.get("runId") or "").strip()
        if run_id:
            current = await self._guard(
                self.runs.find_one(
                    {"runId": run_id},
                    {"remoteConfirmed": 1, "remoteUncertain": 1},
                )
            )
            if current and (
                bool(current.get("remoteConfirmed"))
                or bool(current.get("remoteUncertain"))
            ):
                return False
        result = await self._guard(
            self.emails.update_one(
                {
                    "_id": str(run.get("targetEmailId") or ""),
                    "reservedBy": str(run.get("reservationOwner") or ""),
                    "reservationKind": "email_change",
                },
                {
                    "$set": {"status": "available", "lastAttemptAt": _now()},
                    "$unset": {"reservedBy": "", "reservedAt": "", "reservationKind": ""},
                },
            )
        )
        return int(result.modified_count) > 0

    async def consume_target(self, run: dict[str, Any]) -> bool:
        result = await self._guard(
            self.emails.delete_one(
                {
                    "_id": str(run.get("targetEmailId") or ""),
                    "reservedBy": str(run.get("reservationOwner") or ""),
                    "reservationKind": "email_change",
                }
            )
        )
        return int(result.deleted_count) > 0

    async def clear_account_marker(self, run: dict[str, Any]) -> bool:
        result = await self._guard(
            self.accounts.update_one(
                {
                    "_id": str(run.get("accountId") or ""),
                    "emailChangeRunId": str(run.get("runId") or ""),
                },
                {
                    "$unset": {
                        "emailChangeRunId": "",
                        "emailChangeTargetEmailId": "",
                        "emailChangeUpdatedAt": "",
                    }
                },
            )
        )
        return int(result.modified_count) > 0

    async def commit_account_email(self, run: dict[str, Any], target: dict[str, Any], remote: Any) -> bool:
        target_email = str(target.get("email") or run.get("targetEmail") or "").strip()
        target_normalized = normalize_email(target.get("emailNormalized") or target_email)
        set_values: dict[str, Any] = {
            "email": target_email,
            "emailNormalized": target_normalized,
            "emailAccessUrl": str(target.get("accessUrl") or run.get("targetEmailAccessUrl") or ""),
            "sourceEmailId": str(target.get("_id") or run.get("targetEmailId") or ""),
            "updatedAt": _now(),
        }
        target_mailbox_kind = str(target.get("mailboxKind") or "url").strip().casefold()
        set_values["mailboxKind"] = target_mailbox_kind
        if target_mailbox_kind == "mailcom_imap" and target.get("mailboxPassword"):
            set_values["mailboxPassword"] = target["mailboxPassword"]
        updates = dict(getattr(remote, "account_updates", {}) or {})
        if updates.get("authSession"):
            set_values["authSession"] = updates["authSession"]
        if updates.get("cookieHeader"):
            set_values["cookieHeader"] = updates["cookieHeader"]
        if updates.get("deviceId"):
            set_values["deviceId"] = updates["deviceId"]
        if updates.get("accessToken"):
            set_values.update(
                {
                    "accessToken": updates["accessToken"],
                    "accessTokenConfigured": True,
                    "accessTokenExpiresAt": updates.get("accessTokenExpiresAt"),
                    "accessTokenUpdatedAt": _now(),
                }
            )
        if "refreshToken" in updates:
            if str(updates.get("refreshToken") or "").strip():
                set_values["refreshToken"] = str(updates["refreshToken"]).strip()
        try:
            unset_values = {
                "emailChangeRunId": "",
                "emailChangeTargetEmailId": "",
                "emailChangeUpdatedAt": "",
            }
            if updates.get("accessToken") and not str(updates.get("refreshToken") or "").strip():
                unset_values["refreshToken"] = ""
            if target_mailbox_kind != "mailcom_imap":
                unset_values["mailboxPassword"] = ""
            result = await self._guard(
                self.accounts.update_one(
                    {
                        "_id": str(run.get("accountId") or ""),
                        "emailNormalized": normalize_email(run.get("oldEmailNormalized") or run.get("oldEmail")),
                        "emailChangeRunId": str(run.get("runId") or ""),
                    },
                    {
                        "$set": set_values,
                        "$unset": unset_values,
                    },
                )
            )
        except DuplicateKeyError:
            return False
        return int(result.matched_count) == 1

    async def recover_cleanup(self) -> int:
        cursor = self.runs.find({"status": "cleanup_pending"})
        documents = await self._guard(cursor.to_list(length=None))
        completed = 0
        for run in documents:
            account = await self._guard(self.accounts.find_one({"_id": str(run.get("accountId") or "")}))
            if account is None:
                await self.mark(
                    str(run["runId"]),
                    status="cleanup_pending",
                    stage="cleanup_pending",
                    errorCode="account_missing",
                )
                continue
            if normalize_email(account.get("email")) != normalize_email(run.get("targetEmailNormalized")):
                # The remote operation is already past the irreversible
                # boundary. Keep the target and marker until a later recovery
                # pass can confirm the local account identity.
                await self.mark(
                    str(run["runId"]),
                    status="cleanup_pending",
                    stage="cleanup_pending",
                    errorCode="account_not_committed",
                )
                continue
            # Deleting the target is idempotent: a crash may have happened
            # after the delete but before the terminal run update.  A missing
            # target is therefore already-consumed success when the local CAS
            # shows the new address.
            await self.consume_target(run)
            await self.mark(str(run["runId"]), status="completed", stage="completed", finishedAt=_now())
            completed += 1
        return completed

    async def recover_active(self) -> list[str]:
        """Reconcile runs left active by a process or Mongo reconnect."""

        await self.recover_cleanup()
        cursor = self.runs.find({"status": {"$in": list(ACTIVE_STATUSES)}})
        documents = await self._guard(cursor.to_list(length=None))
        runnable: list[str] = []
        for run in documents:
            status = str(run.get("status") or "")
            run_id = str(run.get("runId") or "")
            if (
                bool(run.get("cancelRequested"))
                and not bool(run.get("remoteConfirmed"))
                and not bool(run.get("remoteUncertain"))
                and status in {"queued", "target_reserved", "remote_submitted"}
            ):
                await self.release_target(run)
                await self.clear_account_marker(run)
                await self.mark(
                    run_id,
                    status="cancelled",
                    stage="cancelled",
                    finishedAt=_now(),
                )
                continue
            if status == "queued":
                recovered = await self._recover_queued_run(run)
                if recovered is None:
                    await self.release_target(run)
                    await self.clear_account_marker(run)
                    await self.mark(
                        run_id,
                        status="failed",
                        stage="target_reserved",
                        errorCode="target_missing",
                        finishedAt=_now(),
                    )
                    continue
                run = dict(recovered)
                status = "target_reserved"
            elif status == "remote_submitted":
                # ``before_verify`` durably sets remoteUncertain before the
                # irreversible request is sent.  If that bit is still false,
                # the worker died during old-login/eligibility/OTP and the
                # whole flow is safe to retry.  Preserve a true bit so an
                # interrupted verify is reconciled through the target only.
                verify_may_have_been_sent = bool(run.get("remoteUncertain"))
                recovered = await self._guard(
                    self.runs.find_one_and_update(
                        {"runId": run_id, "status": "remote_submitted"},
                        {
                            "$set": {
                                "status": "target_reserved",
                                "stage": "target_reserved",
                                "remoteUncertain": verify_may_have_been_sent,
                                "cancelRequested": False,
                                "updatedAt": _now(),
                            },
                            "$unset": {"claimOwner": "", "claimStartedAt": ""},
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                )
                if recovered is not None:
                    run = dict(recovered)
                    status = "target_reserved"
            elif status == "committing":
                # A committing run is always past the remote request.  If the
                # durable boundary bit is missing, recover conservatively as
                # uncertain rather than allowing cancellation to release the
                # target mailbox.
                recovered_status = "remote_verified" if bool(run.get("remoteConfirmed")) else "target_reserved"
                recovered_stage = recovered_status
                recovered_set: dict[str, Any] = {
                    "status": recovered_status,
                    "stage": recovered_stage,
                    "updatedAt": _now(),
                    "cancelRequested": False,
                }
                if recovered_status == "target_reserved":
                    recovered_set["remoteUncertain"] = True
                recovered = await self._guard(
                    self.runs.find_one_and_update(
                        {"runId": run_id, "status": "committing"},
                        {
                            "$set": recovered_set,
                            "$unset": {"claimOwner": "", "claimStartedAt": ""},
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                )
                if recovered is not None:
                    run = dict(recovered)
                    status = recovered_status
                else:
                    # Another recovery worker won the transition.  The stale
                    # snapshot must not enqueue a second committing worker.
                    continue

            # A worker can die after claiming remote_verified/cleanup_pending,
            # leaving the claim fields behind.  These stages are idempotent
            # hand-offs, so clear the stale claim atomically before enqueueing
            # another reconciliation pass.  Also normalize legacy records that
            # have cleanup_pending without an explicit boundary bit.
            if status in {"remote_verified", "cleanup_pending"}:
                boundary_set: dict[str, Any] = {"updatedAt": _now(), "cancelRequested": False}
                if not bool(run.get("remoteConfirmed")) and not bool(run.get("remoteUncertain")):
                    boundary_set["remoteUncertain"] = True
                recovered = await self._guard(
                    self.runs.find_one_and_update(
                        {"runId": run_id, "status": status},
                        {
                            "$set": boundary_set,
                            "$unset": {"claimOwner": "", "claimStartedAt": ""},
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                )
                if recovered is None:
                    continue
                run = dict(recovered)
                status = str(run.get("status") or status)
            elif (
                bool(run.get("remoteConfirmed"))
                or bool(run.get("remoteUncertain"))
            ) and bool(run.get("cancelRequested")):
                # Cancellation is advisory once the remote boundary is
                # crossed.  Clear a crash-left flag but keep the target owned.
                recovered = await self._guard(
                    self.runs.find_one_and_update(
                        {"runId": run_id, "status": status},
                        {"$set": {"cancelRequested": False, "updatedAt": _now()}},
                        return_document=ReturnDocument.AFTER,
                    )
                )
                if recovered is None:
                    continue
                run = dict(recovered)
            if status == "target_reserved" and not bool(run.get("remoteUncertain")):
                if not await self._ensure_account_marker(run):
                    await self.release_target(run)
                    await self.clear_account_marker(run)
                    await self.mark(
                        run_id,
                        status="failed",
                        stage="target_reserved",
                        errorCode="account_changed_concurrently",
                        finishedAt=_now(),
                    )
                    continue
            if status in {"target_reserved", "queued"}:
                runnable.append(run_id)
            elif status in {"remote_verified", "committing", "cleanup_pending"} and (
                bool(run.get("remoteConfirmed")) or bool(run.get("remoteUncertain"))
            ):
                runnable.append(run_id)
        return runnable


__all__ = [
    "MongoEmailChangeStore",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "EMAIL_CHANGE_TERMINAL_TTL_SECONDS",
]
