"""邮箱池 API：列表 / 导出 / 子邮箱 / 读信 / OAuth 状态。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from api.auth import require_admin
from services import graph_mail, oauth_check, pool_store


class SubGenerateBody(BaseModel):
    count: int = Field(default=1, ge=1, le=50)
    tag_prefix: str = ""


class BatchIdsBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


class BatchSubGenerateBody(BaseModel):
    ids: list[str] = Field(default_factory=list)
    count: int = Field(default=1, ge=1, le=50)
    tag_prefix: str = ""


class DeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


class ExportBody(BaseModel):
    category: Literal["registered", "oauth2", "sub", "recovery"]
    ids: list[str] | None = None
    country: str | None = None


class OAuthCheckConfigBody(BaseModel):
    enabled: bool | None = None
    interval_sec: int | None = Field(default=None, ge=300, le=604800)
    delay_ms: int | None = Field(default=None, ge=0, le=5000)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/pool/stats")
    async def pool_stats(_auth: str = Depends(require_admin)):
        pool_store.maybe_import_oauth2_file()
        return {"stats": pool_store.stats()}

    @router.get("/api/pool/accounts")
    async def pool_list(
        category: str = Query(default="all"),
        keyword: str = "",
        country: str = "",
        age: str = Query(default="", description="older7=超7天；within7=7天内；空=全部"),
        oauth_status: str = Query(
            default="",
            description="ok=授权正常；bad=授权失效；unknown=未检测；空=全部",
        ),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        _auth: str = Depends(require_admin),
    ):
        cat = None if category in ("", "all", "none") else category
        return pool_store.list_accounts(
            category=cat,
            keyword=keyword,
            page=page,
            page_size=page_size,
            country=country,
            age=age,
            oauth_status=oauth_status,
        )

    @router.get("/api/pool/accounts/{account_id}")
    async def pool_detail(account_id: str, _auth: str = Depends(require_admin)):
        acc = pool_store.get_account(account_id, include_secrets=True)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        # 详情返回敏感字段（管理台已鉴权）
        return {"account": acc}

    @router.post("/api/pool/accounts/{account_id}/sub-emails")
    async def generate_subs(account_id: str, body: SubGenerateBody, _auth: str = Depends(require_admin)):
        try:
            return pool_store.generate_sub_emails(account_id, count=body.count, tag_prefix=body.tag_prefix)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/pool/batch/sub-emails")
    async def batch_generate_subs(body: BatchSubGenerateBody, _auth: str = Depends(require_admin)):
        if not body.ids:
            raise HTTPException(status_code=400, detail="请选择账号")
        return pool_store.batch_generate_sub_emails(body.ids, count=body.count, tag_prefix=body.tag_prefix)

    @router.post("/api/pool/batch/check-oauth")
    async def batch_check_oauth(body: BatchIdsBody, _auth: str = Depends(require_admin)):
        if not body.ids:
            raise HTTPException(status_code=400, detail="请选择账号")
        results = []
        ok_n = fail_n = 0
        for aid in body.ids:
            try:
                parent_id, client_id, refresh, _ = pool_store.resolve_oauth_credentials(aid)
                if not refresh:
                    raise ValueError("缺少 refresh_token")
                token = graph_mail.refresh_access_token(client_id, refresh)
                if token.get("ok"):
                    pool_store.update_oauth_status(
                        parent_id, "ok", error=None, new_refresh=token.get("refresh_token")
                    )
                    ok_n += 1
                    results.append({"id": aid, "ok": True, "oauth_status": "ok"})
                else:
                    status = "expired" if token.get("auth_expired") else "error"
                    pool_store.update_oauth_status(parent_id, status, error=str(token.get("error") or ""))
                    fail_n += 1
                    results.append({"id": aid, "ok": False, "oauth_status": status, "error": token.get("error")})
            except Exception as exc:
                fail_n += 1
                results.append({"id": aid, "ok": False, "error": str(exc)})
        return {"ok": fail_n == 0, "success": ok_n, "failed": fail_n, "results": results}

    @router.delete("/api/pool/accounts")
    async def delete_accounts(body: DeleteBody, _auth: str = Depends(require_admin)):
        return pool_store.delete_accounts(body.ids)

    @router.post("/api/pool/export")
    async def export_accounts(body: ExportBody, _auth: str = Depends(require_admin)):
        try:
            text = pool_store.export_text(body.category, body.ids, country=body.country)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        filename = {
            "registered": "registered.txt",
            "oauth2": "oauth2_export.txt",
            "sub": "sub_emails.txt",
            "recovery": "recovery_bound.txt",
        }.get(body.category, "export.txt")
        if body.country:
            filename = f"{body.country}_{filename}"
        return Response(
            content=text.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/api/pool/export")
    async def export_accounts_get(
        category: Literal["registered", "oauth2", "sub", "recovery"] = "oauth2",
        country: str = "",
        _auth: str = Depends(require_admin),
    ):
        text = pool_store.export_text(category, None, country=country or None)
        return PlainTextResponse(text)

    @router.post("/api/pool/config/oauth-check")
    async def update_oauth_check_config(body: OAuthCheckConfigBody, _auth: str = Depends(require_admin)):
        cfg = oauth_check.update_config(
            {
                "enabled": body.enabled,
                "interval_sec": body.interval_sec,
                "delay_ms": body.delay_ms,
            }
        )
        return {"ok": True, "config": cfg}

    @router.get("/api/pool/config/oauth-check")
    async def get_oauth_check_config(_auth: str = Depends(require_admin)):
        cfg = oauth_check.get_config()
        return {"config": cfg}

    @router.post("/api/pool/accounts/{account_id}/check-oauth")
    async def check_oauth(account_id: str, _auth: str = Depends(require_admin)):
        acc = pool_store.get_account(account_id, include_secrets=True)
        if not acc or acc.get("category") not in ("oauth2",):
            # 也可能是 sub → 用 parent
            if acc and acc.get("category") == "sub":
                parent = acc.get("parent") or {}
                client_id = parent.get("client_id") or ""
                refresh = parent.get("refresh_token") or ""
                parent_id = acc.get("parent_id") or ""
            else:
                raise HTTPException(status_code=404, detail="仅 OAuth2 账号可检测")
        else:
            client_id = acc.get("client_id") or ""
            refresh = acc.get("refresh_token") or ""
            parent_id = acc.get("id")
        if not refresh:
            raise HTTPException(status_code=400, detail="缺少 refresh_token")
        result = graph_mail.refresh_access_token(client_id, refresh)
        if result.get("ok"):
            pool_store.update_oauth_status(
                parent_id,
                "ok",
                error=None,
                new_refresh=result.get("refresh_token"),
            )
            return {"ok": True, "oauth_status": "ok", "scope": result.get("scope")}
        status = "expired" if result.get("auth_expired") else "error"
        pool_store.update_oauth_status(parent_id, status, error=str(result.get("error") or ""))
        return {"ok": False, "oauth_status": status, "error": result.get("error")}

    @router.get("/api/pool/accounts/{account_id}/messages")
    async def list_messages(
        account_id: str,
        folder: str = "inbox",
        top: int = Query(default=20, ge=1, le=50),
        _auth: str = Depends(require_admin),
    ):
        acc = pool_store.get_account(account_id, include_secrets=True)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")

        to_filter = None
        if acc.get("category") == "sub":
            parent = acc.get("parent") or {}
            client_id = parent.get("client_id") or ""
            refresh = parent.get("refresh_token") or ""
            parent_id = acc.get("parent_id") or ""
            to_filter = acc.get("email")
        elif acc.get("category") == "oauth2":
            client_id = acc.get("client_id") or ""
            refresh = acc.get("refresh_token") or ""
            parent_id = acc.get("id")
        else:
            raise HTTPException(status_code=400, detail="注册成功但未 OAuth 的账号无法 Graph 读信")

        if not refresh:
            raise HTTPException(status_code=400, detail="缺少 refresh_token")

        result = graph_mail.list_messages(
            client_id,
            refresh,
            folder=folder,
            top=top,
            to_contains=to_filter,
        )
        if result.get("new_refresh_token") and result.get("new_refresh_token") != refresh:
            pool_store.update_oauth_status(parent_id, "ok" if result.get("ok") else "error", new_refresh=result.get("new_refresh_token"))
        if result.get("ok"):
            pool_store.update_oauth_status(parent_id, "ok", new_refresh=result.get("new_refresh_token"))
            # 尝试提取验证码
            messages = result.get("messages") or []
            for m in messages:
                codes = graph_mail.extract_codes_from_text(
                    f"{m.get('subject','')} {m.get('preview','')}"
                )
                m["codes"] = codes
            return {
                "ok": True,
                "email": acc.get("email"),
                "parent_email": acc.get("parent_email") or acc.get("email"),
                "messages": messages,
            }
        if result.get("auth_expired"):
            pool_store.update_oauth_status(parent_id, "expired", error=str(result.get("error") or ""))
        else:
            pool_store.update_oauth_status(parent_id, "error", error=str(result.get("error") or ""))
        raise HTTPException(status_code=400, detail=result.get("error") or "读信失败")

    @router.get("/api/pool/accounts/{account_id}/messages/{message_id}")
    async def message_detail(account_id: str, message_id: str, _auth: str = Depends(require_admin)):
        acc = pool_store.get_account(account_id, include_secrets=True)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        if acc.get("category") == "sub":
            parent = acc.get("parent") or {}
            client_id = parent.get("client_id") or ""
            refresh = parent.get("refresh_token") or ""
        elif acc.get("category") == "oauth2":
            client_id = acc.get("client_id") or ""
            refresh = acc.get("refresh_token") or ""
        else:
            raise HTTPException(status_code=400, detail="无法读信")
        result = graph_mail.get_message_detail(client_id, refresh, message_id)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "读取失败")
        msg = result["message"]
        text_for_code = f"{msg.get('subject','')} {msg.get('preview','')} {msg.get('body_text','')}"
        msg["codes"] = graph_mail.extract_codes_from_text(text_for_code)
        return {"ok": True, "message": msg}

    def _receive_payload(token: str, folder: str = "inbox", top: int = 10) -> dict[str, Any]:
        parent, sub = pool_store.get_by_receive_token(token)
        if not parent or not sub:
            raise HTTPException(status_code=404, detail="接码地址无效")
        client_id = parent.get("client_id") or ""
        refresh = parent.get("refresh_token") or ""
        if not refresh:
            raise HTTPException(status_code=400, detail="主账号缺少 refresh_token")
        result = graph_mail.list_messages(
            client_id,
            refresh,
            folder=folder,
            top=top,
            to_contains=sub.get("email"),
        )
        if result.get("new_refresh_token"):
            pool_store.update_oauth_status(
                parent.get("id"),
                "ok" if result.get("ok") else parent.get("oauth_status") or "unknown",
                new_refresh=result.get("new_refresh_token"),
            )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "读信失败")
        messages = result.get("messages") or []
        codes: list[str] = []
        for m in messages:
            extracted = graph_mail.extract_codes_from_text(f"{m.get('subject','')} {m.get('preview','')}")
            m["codes"] = extracted
            for c in extracted:
                if c not in codes:
                    codes.append(c)
        return {
            "ok": True,
            "email": sub.get("email"),
            "parent_email": parent.get("email"),
            "receive_url": pool_store.receive_url_for_token(token),
            "receive_ui_url": pool_store.receive_ui_url_for_token(token),
            "messages": messages,
            "codes": codes,
            "latest_code": codes[0] if codes else None,
            "parent_id": parent.get("id"),
            "client_id": client_id,
            "refresh_token": refresh,
        }

    # 公开接码地址（凭 token，无需 ADMIN_TOKEN）— JSON，供外部系统轮询
    @router.get("/api/pool/receive/{token}")
    async def receive_by_token(
        token: str,
        folder: str = "inbox",
        top: int = Query(default=10, ge=1, le=30),
        format: str = Query(default="json"),
    ):
        payload = _receive_payload(token, folder=folder, top=top)
        # 不把 refresh 暴露给公开 JSON
        payload.pop("refresh_token", None)
        payload.pop("client_id", None)
        payload.pop("parent_id", None)
        if format == "text":
            lines = [
                f"email: {payload.get('email')}",
                f"latest_code: {payload.get('latest_code') or ''}",
                f"codes: {','.join(payload.get('codes') or [])}",
                f"messages: {len(payload.get('messages') or [])}",
                f"ui: {payload.get('receive_ui_url')}",
            ]
            return PlainTextResponse("\n".join(lines) + "\n")
        return payload

    @router.get("/api/pool/receive/{token}/message/{message_id}")
    async def receive_message_detail(token: str, message_id: str):
        parent, sub = pool_store.get_by_receive_token(token)
        if not parent or not sub:
            raise HTTPException(status_code=404, detail="接码地址无效")
        result = graph_mail.get_message_detail(
            parent.get("client_id") or "",
            parent.get("refresh_token") or "",
            message_id,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "读取失败")
        msg = result["message"]
        text_for_code = f"{msg.get('subject','')} {msg.get('preview','')} {msg.get('body_text','')}"
        # 粗提取 HTML 文本
        if msg.get("body_html") and not msg.get("body_text"):
            import re as _re
            text_for_code += " " + _re.sub(r"<[^>]+>", " ", msg.get("body_html") or "")
        msg["codes"] = graph_mail.extract_codes_from_text(text_for_code)
        return {"ok": True, "email": sub.get("email"), "message": msg}

    @router.post("/api/pool/import-oauth2-file")
    async def import_oauth2_file(_auth: str = Depends(require_admin)):
        n = pool_store.maybe_import_oauth2_file()
        return {"imported_or_updated": n, "stats": pool_store.stats()}

    return router
