"""简单 Bearer Token 鉴权。"""

from __future__ import annotations

import os
from fastapi import Header, HTTPException, Query


def get_admin_token() -> str:
    return str(os.environ.get("ADMIN_TOKEN") or "").strip()


def require_admin(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> str:
    expected = get_admin_token()
    if not expected:
        # 未配置 token 时允许本地使用，但生产应设置 ADMIN_TOKEN
        return ""

    provided = ""
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()
        else:
            provided = authorization.strip()
    if not provided and token:
        provided = token.strip()

    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="未授权：请提供正确的 ADMIN_TOKEN")
    return provided
