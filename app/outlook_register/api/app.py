"""FastAPI 应用工厂。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.auth import get_admin_token
from api.pool import create_router as create_pool_router
from api.proxy_pool import create_router as create_proxy_pool_router
from api.receive_ui import create_router as create_receive_ui_router
from api.register import create_router as create_register_router
from api.results import create_router as create_results_router
from services import config_store

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT_DIR / "web_dist"


def create_app() -> FastAPI:
    config_store.ensure_config_file()
    try:
        from services import proxy_pool as _proxy_pool
        _proxy_pool.ensure_auto_sync_running()
    except Exception:
        pass

    app = FastAPI(
        title="OutlookRegister",
        description="Outlook / Hotmail 自动注册 Web 管理台",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(create_register_router())
    app.include_router(create_results_router())
    app.include_router(create_pool_router())
    app.include_router(create_proxy_pool_router())
    app.include_router(create_receive_ui_router())

    @app.get("/api/health")
    async def health():
        token_set = bool(get_admin_token())
        return {
            "ok": True,
            "service": "OutlookRegister",
            "auth_required": token_set,
            # headless 由 config 控制，health 不再宣称强制无头
            "force_headless": False,
        }

    @app.post("/api/auth/login")
    async def login(body: dict):
        expected = get_admin_token()
        provided = str((body or {}).get("token") or "").strip()
        if expected and provided != expected:
            return JSONResponse({"ok": False, "error": "令牌错误"}, status_code=401)
        if not expected:
            # 未设置 ADMIN_TOKEN：任意非空或空都放行，并提示
            return {"ok": True, "token": provided or "dev", "warning": "未设置 ADMIN_TOKEN，鉴权已关闭"}
        return {"ok": True, "token": provided}

    # 静态前端
    if WEB_DIST.is_dir():
        assets = WEB_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        async def index():
            index_file = WEB_DIST / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)
            return JSONResponse(
                {
                    "message": "前端尚未构建。请进入 web/ 执行 npm install && npm run build，"
                    "或使用 Docker 一键部署。",
                    "api": "/api/health",
                }
            )

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            # 不拦截 API / 公开接码页
            if full_path.startswith("api/") or full_path.startswith("r/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = WEB_DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            index_file = WEB_DIST / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    else:

        @app.get("/")
        async def index_missing():
            return {
                "message": "web_dist 不存在。本地请先构建前端，或 docker compose up --build。",
                "api": "/api/health",
            }

    return app
