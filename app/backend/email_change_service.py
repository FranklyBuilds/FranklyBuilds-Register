from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

from .email_change_models import EmailChangeRemoteResult, normalize_email


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _accepts_keyword(callback: Any, name: str) -> bool:
    """Inspect an injected callback without retrying a potentially sent call."""

    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
        parameter.name == name for parameter in parameters
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EmailChangeService:
    """Small orchestration layer kept independent from registration runs."""

    def __init__(self, *, store: Any, adapter: Any) -> None:
        self.store = store
        self.adapter = adapter

    async def _clear_marker(self, run: dict[str, Any]) -> None:
        clear_marker = getattr(self.store, "clear_account_marker", None)
        if callable(clear_marker):
            await _resolve(clear_marker(run))

    async def _latest(self, run_id: str, fallback: dict[str, Any]) -> dict[str, Any]:
        for name in ("get_private", "get"):
            getter = getattr(self.store, name, None)
            if not callable(getter):
                continue
            try:
                value = await _resolve(getter(run_id))
                if isinstance(value, dict):
                    return value
            except Exception:
                continue
        return fallback

    async def _cancel_before_remote(self, run: dict[str, Any]) -> dict[str, Any]:
        # The caller may hold a stale snapshot.  Re-check the durable boundary
        # before releasing the target so a concurrent verify cannot turn a
        # cancellation into an accidentally reusable mailbox.
        run_id = str(run.get("runId") or "")
        latest = await self._latest(run_id, run)
        if bool(latest.get("remoteConfirmed")) or bool(latest.get("remoteUncertain")):
            await self._ignore_cancel_after_remote(run_id)
            return await self._cleanup_pending(
                run_id,
                code="cancel_after_remote_change",
            )
        await _resolve(self.store.release_target(run))
        await self._clear_marker(run)
        return await _resolve(
            self.store.mark(
                run_id,
                status="cancelled",
                stage="cancelled",
                finishedAt=_now(),
                updatedAt=_now(),
            )
        )

    async def _cleanup_pending(
        self,
        run_id: str,
        *,
        code: str = "account_commit_uncertain",
        diagnostic: str = "",
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {
            "status": "cleanup_pending",
            "stage": "cleanup_pending",
            "errorCode": code,
            "updatedAt": _now(),
        }
        # Diagnostics stay private in Mongo and are never copied by _public.
        if diagnostic:
            changes["errorDiagnostic"] = diagnostic[:300]
        return await _resolve(self.store.mark(run_id, **changes))

    async def _ignore_cancel_after_remote(self, run_id: str) -> None:
        await _resolve(
            self.store.mark(
                run_id,
                cancelRequested=False,
                cancelIgnoredAt=_now(),
                updatedAt=_now(),
            )
        )

    async def _mark_remote_uncertain(self, run_id: str, provider_account_id: str = "") -> None:
        changes: dict[str, Any] = {
            "status": "remote_submitted",
            "stage": "remote_submitted",
            "remoteUncertain": True,
            "updatedAt": _now(),
        }
        if provider_account_id:
            changes["providerAccountId"] = provider_account_id
        await _resolve(self.store.mark(run_id, **changes))

    async def _mark_remote_confirmed(
        self,
        run_id: str,
        remote: EmailChangeRemoteResult,
    ) -> None:
        changes: dict[str, Any] = {
            "status": "remote_verified",
            "stage": "remote_verified",
            "remoteConfirmed": True,
            "remoteUncertain": False,
            "remoteAccountUpdates": dict(remote.account_updates or {}),
            "updatedAt": _now(),
        }
        if remote.provider_account_id:
            changes["providerAccountId"] = remote.provider_account_id
        await _resolve(self.store.mark(run_id, **changes))

    @staticmethod
    def _coerce_remote(value: Any) -> EmailChangeRemoteResult:
        if isinstance(value, EmailChangeRemoteResult):
            return value
        return EmailChangeRemoteResult(
            ok=bool(getattr(value, "ok", False)),
            stage=str(getattr(value, "stage", "remote") or "remote"),
            error_code=str(getattr(value, "error_code", "") or ""),
            error_message=str(getattr(value, "error_message", "") or ""),
            account_updates=dict(getattr(value, "account_updates", {}) or {}),
            diagnostics=dict(getattr(value, "diagnostics", {}) or {}),
            remote_confirmed=bool(getattr(value, "remote_confirmed", False)),
            remote_uncertain=bool(getattr(value, "remote_uncertain", False)),
            remote_rejected=bool(getattr(value, "remote_rejected", False)),
            provider_account_id=str(getattr(value, "provider_account_id", "") or ""),
        )

    async def _finish_remote_rejection(self, run: dict[str, Any]) -> None:
        run_id = str(run.get("runId") or "")
        finish = getattr(self.store, "finish_remote_rejection", None)
        if callable(finish):
            if _accepts_keyword(finish, "claim_owner"):
                await _resolve(
                    finish(run_id, claim_owner=str(run.get("claimOwner") or ""))
                )
            else:
                await _resolve(finish(run_id))

    async def _load_inputs(
        self,
        run: dict[str, Any],
        *,
        remote_boundary: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        loader = getattr(self.store, "load_private_inputs")
        if remote_boundary and _accepts_keyword(loader, "allow_missing_target"):
            return await _resolve(loader(run, allow_missing_target=True))
        return await _resolve(loader(run))

    async def _reconcile_remote(
        self,
        run: dict[str, Any],
        account: dict[str, Any],
        target: dict[str, Any],
    ) -> EmailChangeRemoteResult:
        reconcile = getattr(self.adapter, "reconcile", None)
        if not callable(reconcile):
            return EmailChangeRemoteResult(
                ok=False,
                stage="reconcile",
                error_code="remote_reconciliation_unavailable",
                remote_uncertain=True,
                provider_account_id=str(run.get("providerAccountId") or ""),
            )
        provider_id = str(run.get("providerAccountId") or "")
        if _accepts_keyword(reconcile, "provider_account_id"):
            value = await _resolve(
                reconcile(account, target, provider_account_id=provider_id)
            )
        else:
            value = await _resolve(reconcile(account, target, provider_id))
        return self._coerce_remote(value)

    async def _change_remote(
        self,
        run: dict[str, Any],
        account: dict[str, Any],
        target: dict[str, Any],
    ) -> EmailChangeRemoteResult:
        async def before_verify(provider_account_id: str) -> None:
            run_id = str(run.get("runId") or "")
            begin_remote_verify = getattr(self.store, "begin_remote_verify", None)
            if callable(begin_remote_verify):
                begun = await _resolve(begin_remote_verify(run_id, provider_account_id))
                if begun is None:
                    # Cancellation and boundary marking race on one atomic run
                    # update.  When cancellation wins, abort before sending the
                    # provider verify request.
                    raise RuntimeError("email_change_cancelled_before_verify")
                return
            await self._mark_remote_uncertain(run_id, provider_account_id)

        async def after_verify(provider_account_id: str) -> None:
            await _resolve(
                self.store.mark(
                    str(run.get("runId") or ""),
                    status="remote_verified",
                    stage="remote_verified",
                    remoteConfirmed=True,
                    remoteUncertain=False,
                    providerAccountId=provider_account_id,
                    updatedAt=_now(),
                )
            )

        change = getattr(self.adapter, "change")
        if _accepts_keyword(change, "before_verify") or _accepts_keyword(change, "after_verify"):
            value = await _resolve(
                change(account, target, before_verify=before_verify, after_verify=after_verify)
            )
        else:
            value = await _resolve(change(account, target))
        return self._coerce_remote(value)

    async def process(self, run_id: str) -> dict[str, Any]:
        run = await _resolve(self.store.claim(run_id))
        if not run:
            return {"runId": run_id, "status": "missing"}
        if str(run.get("status") or "").casefold() in {
            "completed",
            "failed",
            "cancelled",
        }:
            return dict(run)
        if (
            bool(run.get("cancelRequested"))
            and not bool(run.get("remoteConfirmed"))
            and not bool(run.get("remoteUncertain"))
        ):
            return await self._cancel_before_remote(run)

        remote_succeeded = bool(run.get("remoteConfirmed"))
        remote_uncertain = bool(run.get("remoteUncertain"))
        remote: EmailChangeRemoteResult | None = None
        try:
            account, target = await self._load_inputs(
                run,
                remote_boundary=remote_succeeded or remote_uncertain,
            )
            target_already_consumed = bool(target.get("_already_consumed"))
            account_already_target = normalize_email(account.get("email")) == normalize_email(
                target.get("email") or run.get("targetEmail")
            )
            if remote_succeeded and (
                run.get("remoteAccountUpdates")
                or target_already_consumed
                or account_already_target
            ):
                remote = EmailChangeRemoteResult(
                    ok=True,
                    stage="remote_verified",
                    account_updates=dict(run.get("remoteAccountUpdates") or {}),
                    remote_confirmed=True,
                    provider_account_id=str(run.get("providerAccountId") or ""),
                )
            elif remote_succeeded or remote_uncertain:
                # Recovery after the irreversible boundary is target-only.  Do
                # not replay the old-email begin/OTP/verify sequence.
                remote = await self._reconcile_remote(run, account, target)
            else:
                remote = await self._change_remote(run, account, target)

                if (
                    remote.remote_rejected
                    and not remote.remote_confirmed
                    and not remote.remote_uncertain
                ):
                    await self._finish_remote_rejection(run)

                # Adapter results from older implementations may not carry the
                # boundary flags; consult the durable run marker as well.
                latest_after_change = await self._latest(str(run.get("runId") or run_id), run)
                if bool(latest_after_change.get("remoteConfirmed")):
                    remote_succeeded = True
                if bool(latest_after_change.get("remoteUncertain")):
                    remote_uncertain = True

            if remote is None:
                remote = EmailChangeRemoteResult(ok=False, stage="remote", error_code="remote_result_missing")

            if remote.provider_account_id:
                if remote.remote_uncertain and not remote.remote_confirmed:
                    await self._mark_remote_uncertain(
                        str(run.get("runId") or run_id), remote.provider_account_id
                    )
                    remote_uncertain = True
                elif remote.remote_confirmed:
                    await self._mark_remote_confirmed(str(run.get("runId") or run_id), remote)
                    remote_succeeded = True
            elif remote.remote_uncertain and not remote.remote_confirmed:
                await self._mark_remote_uncertain(str(run.get("runId") or run_id))
                remote_uncertain = True
            elif remote.remote_confirmed:
                await self._mark_remote_confirmed(str(run.get("runId") or run_id), remote)
                remote_succeeded = True

            if remote.ok and not remote_succeeded:
                # Compatibility for injected adapters that predate the explicit
                # metadata: a successful return still crosses the boundary.
                await self._mark_remote_confirmed(str(run.get("runId") or run_id), remote)
                remote_succeeded = True

            if not remote.ok:
                # If a process was recovered after the remote request had an
                # unknown outcome, retain the mailbox instead of releasing a
                # target that may already be bound remotely.
                latest = await self._latest(str(run.get("runId") or run_id), run)
                if (
                    remote_succeeded
                    or remote_uncertain
                    or bool(remote.remote_confirmed)
                    or bool(remote.remote_uncertain)
                    or bool(latest.get("remoteConfirmed"))
                    or bool(latest.get("remoteUncertain"))
                ):
                    return await self._cleanup_pending(
                        str(run.get("runId") or run_id),
                        code="account_commit_uncertain",
                        diagnostic=remote.error_code or remote.stage,
                    )
                if bool(latest.get("cancelRequested")):
                    return await self._cancel_before_remote(run)
                await _resolve(self.store.release_target(run))
                await self._clear_marker(run)
                return await _resolve(
                    self.store.mark(
                        str(run.get("runId") or run_id),
                        status="failed",
                        stage=remote.stage,
                        errorCode=remote.error_code or "remote_failed",
                        finishedAt=_now(),
                        updatedAt=_now(),
                    )
                )

            # A cancellation can arrive while the remote request is running.
            # Once remote_succeeded is true, retain ownership and reconcile;
            # never release a mailbox that may now be bound to the account.
            latest = await self._latest(str(run.get("runId") or run_id), run)
            if bool(latest.get("cancelRequested")):
                if remote_succeeded:
                    await self._ignore_cancel_after_remote(str(run.get("runId") or run_id))
                else:
                    return await self._cancel_before_remote(run)

            begin_commit = getattr(self.store, "begin_commit", None)
            if callable(begin_commit):
                try:
                    committing = await _resolve(
                        begin_commit(
                            str(run.get("runId") or run_id),
                            claim_owner=str(run.get("claimOwner") or ""),
                        )
                    )
                except TypeError as exc:
                    if "claim_owner" not in str(exc):
                        raise
                    committing = await _resolve(begin_commit(str(run.get("runId") or run_id)))
                if not committing:
                    latest = await self._latest(str(run.get("runId") or run_id), run)
                    if bool(latest.get("cancelRequested")) and remote_succeeded:
                        await self._ignore_cancel_after_remote(str(run.get("runId") or run_id))
                        try:
                            committing = await _resolve(
                                begin_commit(
                                    str(run.get("runId") or run_id),
                                    claim_owner=str(latest.get("claimOwner") or run.get("claimOwner") or ""),
                                )
                            )
                        except TypeError as exc:
                            if "claim_owner" not in str(exc):
                                raise
                            committing = await _resolve(begin_commit(str(run.get("runId") or run_id)))
                    if not committing:
                        return await self._cleanup_pending(str(run.get("runId") or run_id))
            else:
                await _resolve(
                    self.store.mark(
                        str(run.get("runId") or run_id),
                        status="committing",
                        stage="committing",
                        updatedAt=_now(),
                    )
                )

            target_email = normalize_email(target.get("email") or run.get("targetEmail"))
            if normalize_email(account.get("email")) == target_email:
                committed = True
            else:
                committed = await _resolve(
                    self.store.commit_account_email(run, target, remote)
                )
            if not committed:
                return await self._cleanup_pending(
                    str(run.get("runId") or run_id),
                    code="account_commit_uncertain",
                )

            consumed = await _resolve(self.store.consume_target(run))
            if not consumed:
                target_exists = getattr(self.store, "target_exists", None)
                still_present = None
                if callable(target_exists):
                    still_present = await _resolve(target_exists(run))
                if target.get("_already_consumed") or still_present is False:
                    consumed = True
                else:
                    return await self._cleanup_pending(
                        str(run.get("runId") or run_id),
                        code="target_cleanup_pending",
                    )
            return await _resolve(
                self.store.mark(
                    str(run.get("runId") or run_id),
                    status="completed",
                    stage="completed",
                    finishedAt=_now(),
                    updatedAt=_now(),
                )
            )
        except Exception as exc:
            latest = await self._latest(str(run.get("runId") or run_id), run)
            if (
                remote_succeeded
                or remote_uncertain
                or bool(remote and remote.remote_confirmed)
                or bool(remote and remote.remote_uncertain)
                or bool(latest.get("remoteConfirmed"))
                or bool(latest.get("remoteUncertain"))
            ):
                return await self._cleanup_pending(
                    str(run.get("runId") or run_id),
                    code="account_commit_uncertain",
                    diagnostic=f"unexpected_{type(exc).__name__}",
                )
            if bool(latest.get("cancelRequested")):
                return await self._cancel_before_remote(run)
            await _resolve(self.store.release_target(run))
            await self._clear_marker(run)
            return await _resolve(
                self.store.mark(
                    str(run.get("runId") or run_id),
                    status="failed",
                    stage=str(run.get("stage") or "remote"),
                    errorCode=f"unexpected_{type(exc).__name__}"[:120],
                    finishedAt=_now(),
                    updatedAt=_now(),
                )
            )


__all__ = ["EmailChangeService"]
