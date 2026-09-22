from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EmailChangeStatus = Literal[
    "queued",
    "target_reserved",
    "remote_submitted",
    "remote_verified",
    "committing",
    "cleanup_pending",
    "completed",
    "failed",
    "cancelled",
]


_PUBLIC_ERROR_MESSAGES = {
    "change_email_ineligible": "当前账号不满足邮箱换绑条件",
    "social_account_change_not_supported": "社交账号不支持此邮箱换绑流程",
    "old_login_failed": "当前账号登录失败",
    "old_session_email_mismatch": "当前账号登录身份校验失败",
    "target_login_failed": "目标邮箱登录失败",
    "target_session_email_mismatch": "目标邮箱登录身份校验失败",
    "liveness_failed": "目标账号测活失败",
    "otp_timeout": "目标邮箱验证码获取超时",
    "target_cleanup_pending": "账号已完成换绑，目标邮箱资源等待清理",
    "account_changed_concurrently": "账号邮箱已被其他操作修改",
    "account_commit_uncertain": "账号已完成远端换绑，正在等待本地状态确认",
    "cancel_after_remote_change": "远端换绑已完成，取消请求等待本地状态确认",
    "account_missing": "换绑账号不存在，目标邮箱资源等待人工处理",
    "account_not_committed": "本地账号状态尚未确认，目标邮箱资源等待人工处理",
    "email_change_worker_failed": "邮箱换绑后台任务异常结束",
}


def public_error_message(code: str | None) -> str | None:
    """Return a fixed user-facing message without echoing transport details."""

    normalized = str(code or "").strip()
    if not normalized:
        return None
    if normalized in _PUBLIC_ERROR_MESSAGES:
        return _PUBLIC_ERROR_MESSAGES[normalized]
    if normalized.startswith("eligibility_http_"):
        return "远端换绑资格检查失败"
    if normalized.startswith("change_email_begin_http_"):
        return "远端换绑请求提交失败"
    if normalized.startswith("change_email_verify_http_"):
        return "远端换绑验证码校验失败"
    if normalized.startswith("unexpected_"):
        return "邮箱换绑后台任务异常"
    return "邮箱换绑任务失败"


def normalize_email(value: str) -> str:
    return str(value or "").strip().casefold()


class EmailChangeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accountId: str = Field(min_length=1, max_length=128)
    targetEmailId: str = Field(min_length=1, max_length=128)


class EmailChangeRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runId: str
    kind: Literal["email_change"] = "email_change"
    status: EmailChangeStatus
    accountId: str
    oldEmail: str
    targetEmailId: str
    targetEmail: str
    stage: str
    attempts: int = 0
    errorCode: str | None = None
    errorMessage: str | None = None
    cancelRequested: bool = False
    createdAt: datetime
    startedAt: datetime | None = None
    updatedAt: datetime
    finishedAt: datetime | None = None


@dataclass(frozen=True, slots=True)
class EmailChangeSession:
    """Authenticated session kept in memory for one remote change attempt."""

    transport: Any
    auth_session: dict[str, Any] = field(default_factory=dict)
    access_token: str = ""
    refresh_token: str = ""
    access_token_expires_at: datetime | None = None
    cookie_header: str = ""
    device_id: str = ""


@dataclass(frozen=True, slots=True)
class EmailChangeRemoteResult:
    ok: bool
    stage: str
    error_code: str = ""
    error_message: str = ""
    account_updates: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # ``verify`` is the irreversible provider boundary.  These flags let the
    # orchestration layer retain the target mailbox even when a later relogin
    # or liveness probe fails.
    remote_confirmed: bool = False
    remote_uncertain: bool = False
    remote_rejected: bool = False
    provider_account_id: str = ""
