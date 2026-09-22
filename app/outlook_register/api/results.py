"""注册结果预览与下载。"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from api.auth import require_admin
from services import config_store


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/results/oauth2")
    async def preview_oauth2(
        limit: int = Query(default=50, ge=1, le=500),
        _auth: str = Depends(require_admin),
    ):
        lines = config_store.read_oauth_results(limit=limit)
        return {
            "count": config_store.count_oauth_results(),
            "items": lines,
        }

    @router.get("/api/results/oauth2/download")
    async def download_oauth2(_auth: str = Depends(require_admin)):
        path = config_store.oauth_results_path()
        if not os.path.isfile(path):
            # 空文件也允许下载
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "a", encoding="utf-8").close()
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="结果文件不存在")
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            filename="oauth2.txt",
        )

    return router
