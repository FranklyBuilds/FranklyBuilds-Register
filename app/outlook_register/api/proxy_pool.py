"""Easy Proxies 代理池对接 API。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.auth import require_admin
from services import proxy_pool


class ProxyPoolConfigUpdate(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    password: str | None = None
    proxy_host: str | None = None
    proxy_type: str | None = None
    timeout_sec: int | None = Field(default=None, ge=2, le=60)
    auto_sync: bool | None = None
    auto_sync_interval_sec: int | None = Field(default=None, ge=15, le=3600)
    auto_sync_mode: Literal["multiple", "single"] | None = None
    notes: str | None = None


class SyncBody(BaseModel):
    mode: Literal["multiple", "single"] = "multiple"


class ImportSubBody(BaseModel):
    url: str = Field(min_length=1)
    tag_prefix: str = "sub"
    auto_test: bool = True
    promote: bool = True


class RefreshBody(BaseModel):
    key: str = ""


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/proxy-pool/config")
    async def get_cfg(_auth: str = Depends(require_admin)):
        proxy_pool.ensure_auto_sync_running()
        return {"config": proxy_pool.get_config()}

    @router.post("/api/proxy-pool/config")
    async def set_cfg(body: ProxyPoolConfigUpdate, _auth: str = Depends(require_admin)):
        return {"config": proxy_pool.update_config(body.model_dump(exclude_unset=True))}

    @router.get("/api/proxy-pool/summary")
    async def get_summary(_auth: str = Depends(require_admin)):
        return proxy_pool.summary()

    @router.get("/api/proxy-pool/ports")
    async def get_ports(limit: int = Query(default=500, ge=1, le=5000), _auth: str = Depends(require_admin)):
        return proxy_pool.list_ports(limit=limit)

    @router.get("/api/proxy-pool/nodes")
    async def get_nodes(page: int = 1, page_size: int = 50, q: str = "", _auth: str = Depends(require_admin)):
        return proxy_pool.list_pool_nodes(page=page, page_size=page_size, q=q)

    @router.post("/api/proxy-pool/sync-register")
    async def sync_register(body: SyncBody, _auth: str = Depends(require_admin)):
        try:
            return proxy_pool.sync_to_register_config(mode=body.mode)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/proxy-pool/export-urls")
    async def export_urls(limit: int = Query(default=2000, ge=1, le=5000), _auth: str = Depends(require_admin)):
        try:
            text = proxy_pool.export_proxy_urls(limit=limit)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=text.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="proxy_urls.txt"'},
        )

    @router.get("/api/proxy-pool/sources")
    async def sources(_auth: str = Depends(require_admin)):
        return proxy_pool.list_import_sources()

    @router.post("/api/proxy-pool/import-subscription")
    async def import_sub(body: ImportSubBody, _auth: str = Depends(require_admin)):
        try:
            return proxy_pool.import_subscription(
                url=body.url,
                tag_prefix=body.tag_prefix,
                auto_test=body.auto_test,
                promote=body.promote,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/proxy-pool/refresh-sources")
    async def refresh_sources(body: RefreshBody, _auth: str = Depends(require_admin)):
        try:
            return proxy_pool.refresh_sources(key=body.key or "")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
