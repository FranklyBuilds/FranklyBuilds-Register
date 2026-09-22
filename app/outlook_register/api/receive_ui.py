"""公开可视化接码页：/r/{token}"""

from __future__ import annotations

import html
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from services import pool_store


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/r/{token}", response_class=HTMLResponse)
    async def receive_ui(token: str):
        parent, sub = pool_store.get_by_receive_token(token)
        if not parent or not sub:
            raise HTTPException(status_code=404, detail="接码地址无效")
        email = html.escape(str(sub.get("email") or ""))
        parent_email = html.escape(str(parent.get("email") or ""))
        json_url = html.escape(pool_store.receive_url_for_token(token))
        # 前端自拉 /api/pool/receive/{token} 与详情接口
        page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>接码邮箱 · {email}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg:#0b1220; --panel:#151c2c; --border:#243047; --text:#e5eefb;
      --muted:#94a3b8; --primary:#3b82f6; --ok:#22c55e; --card:#0f172a;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; font-family: "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
      background: radial-gradient(900px 400px at 10% -10%, #1e3a5f 0%, transparent 50%), var(--bg);
      color: var(--text); min-height:100vh;
    }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 18px; }}
    .top {{
      display:flex; flex-wrap:wrap; gap:12px; justify-content:space-between; align-items:flex-start;
      background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:14px 16px; margin-bottom:14px;
    }}
    h1 {{ margin:0 0 6px; font-size:18px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; word-break:break-all; }}
    .muted {{ color: var(--muted); font-size:12px; }}
    .actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
    button {{
      appearance:none; border:1px solid var(--border); background:#0f172a; color:var(--text);
      border-radius:10px; padding:8px 12px; cursor:pointer; font-weight:600; font-size:13px;
    }}
    button.primary {{ background:var(--primary); border-color:transparent; color:#fff; }}
    .code-box {{
      margin-top:10px; background:#052e1a; border:1px solid rgba(34,197,94,.35); color:#86efac;
      border-radius:10px; padding:10px 12px; font-size:22px; font-weight:800; letter-spacing:2px;
    }}
    .layout {{ display:grid; grid-template-columns: 1.05fr 1fr; gap:12px; }}
    @media (max-width: 860px) {{ .layout {{ grid-template-columns: 1fr; }} }}
    .panel {{
      background:var(--panel); border:1px solid var(--border); border-radius:14px; overflow:hidden; min-height:420px;
      display:flex; flex-direction:column;
    }}
    .panel h2 {{
      margin:0; padding:12px 14px; font-size:14px; border-bottom:1px solid var(--border); background:rgba(255,255,255,.02);
    }}
    .list {{ overflow:auto; flex:1; max-height:70vh; }}
    .item {{
      padding:12px 14px; border-bottom:1px solid var(--border); cursor:pointer;
    }}
    .item:hover, .item.active {{ background: rgba(59,130,246,.12); }}
    .item .sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}
    .badge {{
      display:inline-block; font-size:11px; padding:2px 8px; border-radius:999px;
      background:rgba(34,197,94,.15); color:#86efac; border:1px solid rgba(34,197,94,.3); margin-left:6px;
    }}
    .detail {{ padding:14px; overflow:auto; flex:1; max-height:70vh; }}
    .meta {{ font-size:12px; color:var(--muted); line-height:1.7; margin-bottom:12px; }}
    .body-frame {{
      width:100%; min-height:320px; border:1px solid var(--border); border-radius:10px; background:#fff;
    }}
    .body-text {{
      white-space:pre-wrap; word-break:break-word; background:var(--card); border:1px solid var(--border);
      border-radius:10px; padding:12px; font-size:13px; line-height:1.55;
    }}
    .empty {{ color:var(--muted); padding:28px; text-align:center; }}
    a {{ color:#93c5fd; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>可视化接码邮箱</h1>
        <div class="mono">{email}</div>
        <div class="muted">主账号 {parent_email}</div>
        <div class="code-box" id="latestCode">验证码：加载中…</div>
        <div class="muted" style="margin-top:8px">JSON 接口：<a href="{json_url}" target="_blank" rel="noopener">{json_url}</a></div>
      </div>
      <div class="actions">
        <button class="primary" id="btnRefresh">刷新邮件</button>
        <button id="btnCopyCode">复制验证码</button>
        <button id="btnCopyJson">复制 JSON 链接</button>
      </div>
    </div>
    <div class="layout">
      <section class="panel">
        <h2>收件箱 <span class="muted" id="count"></span></h2>
        <div class="list" id="list"><div class="empty">加载中…</div></div>
      </section>
      <section class="panel">
        <h2>邮件详情</h2>
        <div class="detail" id="detail"><div class="empty">选择左侧邮件</div></div>
      </section>
    </div>
  </div>
  <script>
    const TOKEN = {json.dumps(token)};
    const JSON_URL = {json.dumps(pool_store.receive_url_for_token(token))};
    let messages = [];
    let latestCode = '';
    let activeId = '';

    function esc(s) {{
      return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}

    async function loadList() {{
      const list = document.getElementById('list');
      list.innerHTML = '<div class="empty">加载中…</div>';
      try {{
        const res = await fetch('/api/pool/receive/' + encodeURIComponent(TOKEN) + '?top=20');
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || '加载失败');
        messages = data.messages || [];
        latestCode = data.latest_code || (data.codes && data.codes[0]) || '';
        document.getElementById('latestCode').textContent = latestCode ? ('验证码：' + latestCode) : '验证码：暂无';
        document.getElementById('count').textContent = '(' + messages.length + ')';
        if (!messages.length) {{
          list.innerHTML = '<div class="empty">暂无邮件</div>';
          return;
        }}
        list.innerHTML = messages.map(m => `
          <div class="item ${{m.id === activeId ? 'active' : ''}}" data-id="${{esc(m.id)}}">
            <div><strong>${{esc(m.subject)}}</strong>
              ${{(m.codes && m.codes.length) ? `<span class="badge">${{esc(m.codes.join(','))}}</span>` : ''}}
            </div>
            <div class="sub">${{esc(m.from)}} · ${{esc(m.received_at)}}</div>
            <div class="sub">${{esc(m.preview || '')}}</div>
          </div>
        `).join('');
        list.querySelectorAll('.item').forEach(el => {{
          el.addEventListener('click', () => openDetail(el.dataset.id));
        }});
      }} catch (err) {{
        list.innerHTML = '<div class="empty" style="color:#fca5a5">' + esc(err.message || err) + '</div>';
      }}
    }}

    async function openDetail(id) {{
      activeId = id;
      document.querySelectorAll('.item').forEach(el => el.classList.toggle('active', el.dataset.id === id));
      const detail = document.getElementById('detail');
      detail.innerHTML = '<div class="empty">加载详情…</div>';
      try {{
        const res = await fetch('/api/pool/receive/' + encodeURIComponent(TOKEN) + '/message/' + encodeURIComponent(id));
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || '详情失败');
        const m = data.message || {{}};
        const codes = (m.codes || []).join(',');
        let bodyHtml = '';
        if (m.body_html) {{
          bodyHtml = '<iframe class="body-frame" sandbox="allow-same-origin" id="mailFrame"></iframe>';
        }} else {{
          bodyHtml = '<div class="body-text">' + esc(m.body_text || m.preview || '(无正文)') + '</div>';
        }}
        detail.innerHTML = `
          <div style="font-size:16px;font-weight:700;margin-bottom:8px">${{esc(m.subject || '(无主题)')}}
            ${{codes ? `<span class="badge">${{esc(codes)}}</span>` : ''}}
          </div>
          <div class="meta">
            发件人：${{esc(m.from_name || '')}} &lt;${{esc(m.from || '')}}&gt;<br/>
            时间：${{esc(m.received_at || '')}}
          </div>
          ${{bodyHtml}}
        `;
        if (m.body_html) {{
          const iframe = document.getElementById('mailFrame');
          const doc = iframe.contentDocument || iframe.contentWindow.document;
          const safe = String(m.body_html)
            .replace(/<script[\\s\\S]*?<\\/script>/gi, '')
            .replace(/on\\w+=(".*?"|'.*?'|[^\\s>]+)/gi, '');
          doc.open();
          doc.write('<!DOCTYPE html><html><head><meta charset="UTF-8"><style>body{{font-family:sans-serif;font-size:14px;color:#111;margin:12px;word-break:break-word}} img{{max-width:100%}}</style></head><body>' + safe + '</body></html>');
          doc.close();
        }}
      }} catch (err) {{
        detail.innerHTML = '<div class="empty" style="color:#fca5a5">' + esc(err.message || err) + '</div>';
      }}
    }}

    document.getElementById('btnRefresh').onclick = loadList;
    document.getElementById('btnCopyCode').onclick = async () => {{
      if (!latestCode) return alert('暂无验证码');
      try {{ await navigator.clipboard.writeText(latestCode); alert('已复制 ' + latestCode); }}
      catch {{ prompt('复制验证码', latestCode); }}
    }};
    document.getElementById('btnCopyJson').onclick = async () => {{
      try {{ await navigator.clipboard.writeText(JSON_URL); alert('已复制 JSON 链接'); }}
      catch {{ prompt('JSON 链接', JSON_URL); }}
    }};
    loadList();
    setInterval(loadList, 15000);
  </script>
</body>
</html>"""
        return HTMLResponse(page)

    return router
