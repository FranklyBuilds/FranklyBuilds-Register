"""Microsoft Graph 读信（refresh_token → access_token → messages）。"""

from __future__ import annotations

import re
from typing import Any

import requests

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default offline_access"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _proxies(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def refresh_access_token(
    client_id: str,
    refresh_token: str,
    proxy_url: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    try:
        res = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope or GRAPH_SCOPE,
            },
            timeout=30,
            proxies=_proxies(proxy_url),
        )
        if res.status_code != 200:
            return {
                "ok": False,
                "status": res.status_code,
                "error": res.text[:300],
                "auth_expired": res.status_code in (400, 401),
            }
        data = res.json()
        access = data.get("access_token")
        if not access:
            return {"ok": False, "error": "missing access_token", "auth_expired": True}
        return {
            "ok": True,
            "access_token": access,
            "refresh_token": data.get("refresh_token") or refresh_token,
            "scope": data.get("scope") or "",
            "expires_in": data.get("expires_in"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_messages(
    client_id: str,
    refresh_token: str,
    *,
    folder: str = "inbox",
    top: int = 20,
    skip: int = 0,
    proxy_url: str | None = None,
    to_contains: str | None = None,
) -> dict[str, Any]:
    """列出邮件；可选按收件人过滤（子邮箱 plus 地址）。"""
    token = refresh_access_token(client_id, refresh_token, proxy_url=proxy_url)
    if not token.get("ok"):
        return {
            "ok": False,
            "error": token.get("error") or "token failed",
            "auth_expired": bool(token.get("auth_expired")),
            "status": token.get("status"),
        }

    folder_map = {
        "inbox": "inbox",
        "junk": "junkemail",
        "junkemail": "junkemail",
        "deleted": "deleteditems",
        "deleteditems": "deleteditems",
    }
    folder_name = folder_map.get((folder or "inbox").lower(), "inbox")
    url = f"{GRAPH_BASE}/me/mailFolders/{folder_name}/messages"
    # 多取一些再本地过滤 to（Graph $filter toRecipients 不稳定）
    fetch_top = min(max(int(top) * 3, int(top), 20), 50) if to_contains else max(1, min(int(top), 50))
    params = {
        "$top": fetch_top,
        "$skip": max(0, int(skip)),
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,bodyPreview,hasAttachments",
        "$orderby": "receivedDateTime desc",
    }
    headers = {
        "Authorization": f"Bearer {token['access_token']}",
        "Prefer": "outlook.body-content-type='text'",
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=30, proxies=_proxies(proxy_url))
        if res.status_code != 200:
            return {
                "ok": False,
                "error": res.text[:300],
                "auth_expired": res.status_code == 401,
                "status": res.status_code,
                "new_refresh_token": token.get("refresh_token"),
            }
        items = res.json().get("value") or []
        needle = (to_contains or "").strip().lower()
        if needle:
            filtered = []
            for msg in items:
                recipients = []
                for key in ("toRecipients", "ccRecipients"):
                    for r in msg.get(key) or []:
                        addr = ((r.get("emailAddress") or {}).get("address") or "").lower()
                        if addr:
                            recipients.append(addr)
                if any(needle == a or needle in a for a in recipients):
                    filtered.append(msg)
            items = filtered[: max(1, int(top))]
        else:
            items = items[: max(1, min(int(top), 50))]

        normalized = []
        for msg in items:
            fr = msg.get("from") or {}
            fa = fr.get("emailAddress") or {}
            tos = []
            for r in msg.get("toRecipients") or []:
                ea = r.get("emailAddress") or {}
                if ea.get("address"):
                    tos.append(ea.get("address"))
            normalized.append(
                {
                    "id": msg.get("id"),
                    "subject": msg.get("subject") or "(无主题)",
                    "from": fa.get("address") or "",
                    "from_name": fa.get("name") or "",
                    "to": tos,
                    "received_at": msg.get("receivedDateTime") or "",
                    "is_read": bool(msg.get("isRead")),
                    "preview": msg.get("bodyPreview") or "",
                    "has_attachments": bool(msg.get("hasAttachments")),
                }
            )
        return {
            "ok": True,
            "messages": normalized,
            "new_refresh_token": token.get("refresh_token"),
            "scope": token.get("scope"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "new_refresh_token": token.get("refresh_token")}


def get_message_detail(
    client_id: str,
    refresh_token: str,
    message_id: str,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    token = refresh_access_token(client_id, refresh_token, proxy_url=proxy_url)
    if not token.get("ok"):
        return {"ok": False, "error": token.get("error"), "auth_expired": bool(token.get("auth_expired"))}
    try:
        url = f"{GRAPH_BASE}/me/messages/{message_id}"
        params = {
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,body,bodyPreview,hasAttachments"
        }
        headers = {
            "Authorization": f"Bearer {token['access_token']}",
            "Prefer": "outlook.body-content-type='html'",
        }
        res = requests.get(url, headers=headers, params=params, timeout=30, proxies=_proxies(proxy_url))
        if res.status_code != 200:
            return {
                "ok": False,
                "error": res.text[:300],
                "auth_expired": res.status_code == 401,
                "new_refresh_token": token.get("refresh_token"),
            }
        msg = res.json()
        fr = (msg.get("from") or {}).get("emailAddress") or {}
        body = msg.get("body") or {}
        return {
            "ok": True,
            "message": {
                "id": msg.get("id"),
                "subject": msg.get("subject") or "(无主题)",
                "from": fr.get("address") or "",
                "from_name": fr.get("name") or "",
                "received_at": msg.get("receivedDateTime") or "",
                "is_read": bool(msg.get("isRead")),
                "preview": msg.get("bodyPreview") or "",
                "body_html": body.get("content") if body.get("contentType") == "html" else "",
                "body_text": body.get("content") if body.get("contentType") != "html" else "",
                "content_type": body.get("contentType") or "text",
            },
            "new_refresh_token": token.get("refresh_token"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def extract_codes_from_text(text: str) -> list[str]:
    if not text:
        return []
    found = _CODE_RE.findall(text)
    # 去重保序
    out: list[str] = []
    for c in found:
        if c not in out:
            out.append(c)
    return out[:10]
