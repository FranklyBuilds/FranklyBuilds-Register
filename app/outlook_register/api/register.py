"""注册控制 API。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import require_admin
from services.register_service import register_service


class RegisterConfigUpdate(BaseModel):
    email_suffix: str | None = None
    headless: bool | None = None
    bot_protection_wait: int | None = Field(default=None, ge=1, le=300)
    max_captcha_retries: int | None = Field(default=None, ge=0, le=20)
    captcha_strategy: int | None = Field(default=None, ge=0, le=2)
    concurrent_flows: int | None = Field(default=None, ge=1, le=50)
    tasks: int | None = Field(default=None, ge=1)
    success_tasks: int | None = None
    batch_success_limit: int | None = Field(default=None, ge=1)
    proxy: dict[str, Any] | None = None
    oauth2: dict[str, Any] | None = None
    temp_mail: dict[str, Any] | None = None
    browser: dict[str, Any] | None = None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register")
    async def get_register(_auth: str = Depends(require_admin)):
        return register_service.get()

    @router.post("/api/register")
    async def update_register(body: RegisterConfigUpdate, _auth: str = Depends(require_admin)):
        try:
            # exclude_unset 保留 success_tasks=null 的显式清空：用 model_dump 再手工处理
            raw = body.model_dump(exclude_unset=True)
            return register_service.update(raw)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/register/start")
    async def start_register(_auth: str = Depends(require_admin)):
        try:
            return register_service.start()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/register/stop")
    async def stop_register(_auth: str = Depends(require_admin)):
        return register_service.stop()

    @router.post("/api/register/reset")
    async def reset_register(_auth: str = Depends(require_admin)):
        try:
            return register_service.reset()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/register/events")
    async def register_events(token: str = ""):
        # EventSource 无法带 Authorization，走 query token
        from api.auth import get_admin_token

        expected = get_admin_token()
        if expected and token.strip() != expected:
            raise HTTPException(status_code=401, detail="未授权")

        async def stream():
            last = ""
            while True:
                try:
                    payload = json.dumps(register_service.get(), ensure_ascii=False, default=str)
                    if payload != last:
                        yield f"data: {payload}\n\n"
                        last = payload
                    else:
                        yield ": keepalive\n\n"
                except Exception as exc:
                    yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
