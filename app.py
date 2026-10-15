"""每日快讯系统 —— Web 服务层。

复刻目标站的分层逻辑：
  - 访客：只能看到"3 小时前"的快讯（对应目标站 search 页的开放策略）
  - 会员：看到完整时间线、股票/板块统计、自定义监控、提醒
  - 每日快讯主页面本身就是时间线（对应目标站 / 路由）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import urlsplit

import requests

import db
import extract
import imgs
import kline
import sources
from db import CST, fmt_cst, get_conn, now_cst, tx

try:  # AI 层为可选增强
    import ai
except Exception as _ai_import_err:  # noqa: BLE001
    ai = None  # type: ignore[assignment]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FREE_DELAY_HOURS = 3  # 访客可见延迟，对应目标站"当前只能查看3小时前信息"
# 是否对访客隐藏会员专属页（每日快讯/股票统计/板块统计/自定义监控），
# 与目标站一致；设为 0 则全部开放（自建站点可自由选择）。
GATE_ENABLED = os.environ.get("GATE_ENABLED", "1") not in ("0", "false", "False")

app = FastAPI(title="每日快讯", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "web")), name="static")


# --------------------------------------------------------------------------
# 轻量 Markdown 渲染（供 AI 复盘/个股分析使用，避免额外依赖）
# --------------------------------------------------------------------------
def md_to_html(text: str) -> str:
    import html as _html

    esc = _html.escape(text or "")
    out = []
    for line in esc.splitlines():
        s = line.rstrip()
        if not s.strip():
            out.append("")
            continue
        if s.startswith("### "):
            out.append(f"<h4 class='md-h4'>{s[4:]}</h4>")
        elif s.startswith("## "):
            out.append(f"<h3 class='md-h3'>{s[3:]}</h3>")
        elif s.startswith("# "):
            out.append(f"<h2 class='md-h2'>{s[2:]}</h2>")
        elif s.startswith("- "):
            out.append(f"<li class='md-li'>{s[2:]}</li>")
        elif re.match(r"^\d+\.\s", s):
            out.append(f"<li class='md-li'>{re.sub(r'^\\d+\\.\\s','',s)}</li>")
        else:
            out.append(f"<p class='md-p'>{s}</p>")
    return "\n".join(out)


templates.env.globals["md_to_html"] = md_to_html


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def create_member(username: str, password: str, role: str = "member", days: Optional[int] = None) -> int:
    salt = secrets.token_hex(16)
    expire = None
    if days:
        expire = (now_cst() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO members(username,password_hash,salt,role,expire_at,created_at)
               VALUES (?,?,?,?,?,?)""",
            (username, hash_password(password, salt), salt, role, expire, fmt_cst()),
        )
        return cur.lastrowid


def verify_login(username: str, password: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM members WHERE username=?", (username,)).fetchone()
    if not row:
        return None
    if hash_password(password, row["salt"]) != row["password_hash"]:
        return None
    conn.execute("UPDATE members SET last_login_at=? WHERE id=?", (fmt_cst(), row["id"]))
    return dict(row)


def create_session(member_id: int, days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    expire = (now_cst() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with tx() as conn:
        conn.execute(
            "INSERT INTO sessions(token,member_id,created_at,expire_at) VALUES (?,?,?,?)",
            (token, member_id, fmt_cst(), expire),
        )
    return token


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("db_session")
    if not token:
        return None
    conn = get_conn()
    row = conn.execute(
        """SELECT m.* FROM sessions s JOIN members m ON m.id=s.member_id
           WHERE s.token=? AND s.expire_at >= ?""",
        (token, fmt_cst()),
    ).fetchone()
    if not row:
        return None
    user = dict(row)
    if user.get("expire_at") and user["expire_at"] < fmt_cst():
        user["role"] = "expired"
    return user


def is_vip(user: Optional[dict]) -> bool:
    if not user:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] != "vip":
        return False
    if user.get("expire_at") and user["expire_at"] < fmt_cst():
        return False
    return True


def ctx(request: Request, user: Optional[dict], **kw) -> dict:
    return {
        "request": request,
        "user": user,
        "is_vip": is_vip(user),
        "free_delay_hours": FREE_DELAY_HOURS,
        "stats": db.stats(),
        "today": now_cst().strftime("%Y-%m-%d"),
        **kw,
    }


# --------------------------------------------------------------------------
# 查询层
# --------------------------------------------------------------------------
def visibility_clause(user: Optional[dict]) -> tuple[str, list]:
    """返回可见性 SQL 条件与参数。

    分层逻辑（对应目标站 search 页的"当前只能查看3小时前信息"）：
      会员/管理员：可见全量（无时间限制）
      访客      ：只能看到 FREE_DELAY_HOURS 之前产生的信息（延迟视图）
    """
    if is_vip(user):
        return "1=1", []
    cutoff = (now_cst() - timedelta(hours=FREE_DELAY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    return "b.published_at <= ?", [cutoff]


def query_brief(
    user: Optional[dict],
    *,
    q: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 50,
    important_only: bool = False,
) -> dict:
    conn = get_conn()
    vis_sql, vis_params = visibility_clause(user)
    where = [vis_sql]
    params: list = list(vis_params)
    if q:
        where.append("(b.title LIKE ? OR b.body LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if source:
        where.append("b.source = ?")
        params.append(source)
    if important_only:
        where.append("b.is_important = 1")
    clause = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) FROM brief_items b WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT b.* FROM brief_items b WHERE {clause}
            ORDER BY b.published_at DESC LIMIT ? OFFSET ?""",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        # 兼容历史数据：早期入库的新浪图片是 dict，这里统一归一化为 URL 列表
        d["images"] = imgs.normalize_images(d.get("images"))
        d["mentions"] = [
            dict(m)
            for m in conn.execute(
                "SELECT kind,ref_code,ref_name FROM mentions WHERE brief_id=? LIMIT 20", (r["id"],)
            )
        ]
        items.append(d)
    return {
        "rows": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "gated": not is_vip(user),
    }


def query_hot(page: int = 1, page_size: int = 60) -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM hot_rows").fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM hot_rows ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page,
            "pages": max(1, (total + page_size - 1) // page_size)}


def global_search(q: str, user: Optional[dict]) -> dict:
    """全站搜索：快讯 / 热搜 / 个股 / 板块。访客受 FREE_DELAY_HOURS 延迟限制。"""
    conn = get_conn()
    out = {"query": q, "brief": [], "hot": [], "stocks": [], "sectors": []}
    if not q:
        return out
    like = f"%{q}%"
    if is_vip(user):
        brief_where, brief_params = "(title LIKE ? OR body LIKE ?)", [like, like]
    else:
        cutoff = (now_cst() - timedelta(hours=FREE_DELAY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        brief_where = "(title LIKE ? OR body LIKE ?) AND published_at <= ?"
        brief_params = [like, like, cutoff]
    out["brief"] = [
        dict(r)
        for r in conn.execute(
            f"""SELECT id,source,source_label,title,body,url,published_at FROM brief_items
                WHERE {brief_where}
                ORDER BY published_at DESC LIMIT 40""",
            brief_params,
        )
    ]
    out["hot"] = [
        dict(r)
        for r in conn.execute(
            "SELECT source,title,url,captured_at FROM hot_rows WHERE title LIKE ? LIMIT 40",
            (like,),
        )
    ]
    out["stocks"] = [
        dict(r)
        for r in conn.execute(
            "SELECT code,name FROM stocks WHERE name LIKE ? OR code LIKE ? LIMIT 30", (like, like)
        )
    ]
    out["sectors"] = [
        dict(r) for r in conn.execute("SELECT name FROM sectors WHERE name LIKE ? LIMIT 30", (like,))
    ]
    return out


def gate_response(request: Request, user: Optional[dict], title: str, desc: str):
    """访客访问会员专属页时返回的门禁页（对应目标站 member-only-page）。"""
    return templates.TemplateResponse(
        request,
        "gate.html",
        ctx(request, user, gate_title=title, gate_desc=desc),
    )


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def page_home(
    request: Request,
    q: str = Query("", max_length=80),
    source: str = Query(""),
    page: int = Query(1, ge=1),
    important: int = Query(0),
):
    user = current_user(request)
    if GATE_ENABLED and not is_vip(user) and not q:
        return gate_response(request, user, "每日快讯", "每日快讯汇总全网异动信息，方便按时间线快速查看市场变化。")
    data = query_brief(user, q=q, source=source, page=page, important_only=bool(important))
    return templates.TemplateResponse(
        request,
        "brief.html",
        ctx(request, user, data=data, q=q, source=source, important=important, active="brief"),
    )


@app.get("/stocks", response_class=HTMLResponse)
def page_stocks(request: Request, hours: int = Query(24, ge=1, le=720), code: str = Query("")):
    user = current_user(request)
    if GATE_ENABLED and not is_vip(user):
        return gate_response(request, user, "股票统计", "统计异动信息提及个股次数，做热度排名与趋势变化。")
    ranking = extract.stock_ranking(hours=hours, limit=120)
    timeline = extract.stock_timeline(code) if code else []
    return templates.TemplateResponse(
        request,
        "stocks.html",
        ctx(request, user, ranking=ranking, timeline=timeline, code=code, hours=hours, active="stocks"),
    )


@app.get("/sectors", response_class=HTMLResponse)
def page_sectors(request: Request, hours: int = Query(24, ge=1, le=720), name: str = Query("")):
    user = current_user(request)
    if GATE_ENABLED and not is_vip(user):
        return gate_response(request, user, "板块统计", "统计异动信息提及板块次数，做热度排名与趋势变化。")
    ranking = extract.sector_ranking(hours=hours, limit=80)
    timeline = extract.sector_timeline(name) if name else []
    return templates.TemplateResponse(
        request,
        "sectors.html",
        ctx(request, user, ranking=ranking, timeline=timeline, name=name, hours=hours, active="sectors"),
    )


@app.get("/news-wire", response_class=HTMLResponse)
def page_news_wire(request: Request, page: int = Query(1, ge=1)):
    user = current_user(request)
    return templates.TemplateResponse(
        request,
        "news_wire.html",
        ctx(request, user, data=query_hot(page), active="hot")
    )


@app.get("/search", response_class=HTMLResponse)
def page_search(request: Request, q: str = Query("", max_length=80)):
    user = current_user(request)
    return templates.TemplateResponse(
        request,
        "search.html",
        ctx(request, user, result=global_search(q, user), q=q, active="search")
    )


@app.get("/monitor-keywords", response_class=HTMLResponse)
def page_monitor(request: Request, kw: str = Query("", max_length=60)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/monitor-keywords", status_code=302)
    conn = get_conn()
    keywords = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM alerts WHERE member_id=? ORDER BY id DESC", (user["id"],)
        )
    ]
    hits = []
    if kw:
        hits = [
            dict(r)
            for r in conn.execute(
                """SELECT id,source_label,title,body,url,published_at FROM brief_items
                   WHERE (title LIKE ? OR body LIKE ?) ORDER BY published_at DESC LIMIT 100""",
                (f"%{kw}%", f"%{kw}%"),
            )
        ]
    return templates.TemplateResponse(
        request,
        "monitor.html",
        ctx(request, user, keywords=keywords, hits=hits, kw=kw, active="monitor")
    )


@app.post("/monitor-keywords")
def add_monitor(request: Request, keyword: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO alerts(member_id,keyword,created_at) VALUES (?,?,?)",
            (user["id"], keyword.strip()[:60], fmt_cst()),
        )
    return RedirectResponse("/monitor-keywords", status_code=302)


@app.post("/monitor-keywords/delete")
def del_monitor(request: Request, alert_id: int = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with tx() as conn:
        conn.execute("DELETE FROM alerts WHERE id=? AND member_id=?", (alert_id, user["id"]))
    return RedirectResponse("/monitor-keywords", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request, next: str = Query("/"), error: str = Query("")):
    return templates.TemplateResponse(
        request,
        "login.html",
        ctx(request, current_user(request), next_url=next, error=error, active="login")
    )


@app.post("/login")
def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str = Form(""),
    next: str = Form("/"),
):
    member = verify_login(username.strip(), password)
    if not member:
        return RedirectResponse(f"/login?error=1&next={next}", status_code=302)
    days = 30 if remember else 1
    token = create_session(member["id"], days)
    resp = RedirectResponse(next or "/", status_code=302)
    resp.set_cookie("db_session", token, max_age=days * 86400, httponly=True, samesite="lax")
    return resp


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request, error: str = Query("")):
    return templates.TemplateResponse(
        request,
        "register.html",
        ctx(request, current_user(request), error=error, active="register")
    )


@app.post("/register")
def do_register(username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if len(username) < 3 or len(password) < 6:
        return RedirectResponse("/register?error=weak", status_code=302)
    conn = get_conn()
    if conn.execute("SELECT 1 FROM members WHERE username=?", (username,)).fetchone():
        return RedirectResponse("/register?error=exists", status_code=302)
    # 首个注册用户给 admin，便于管理
    role = "admin" if conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0 else "member"
    create_member(username, password, role=role)
    return RedirectResponse("/login", status_code=302)


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("db_session")
    if token:
        with tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("db_session")
    return resp


# --------------------------------------------------------------------------
# 管理 / 采集
# --------------------------------------------------------------------------
@app.post("/api/refresh")
def api_refresh(request: Request, token: str = Query("")):
    """手动触发一轮采集。需 admin 会话或 REFRESH_TOKEN。"""
    user = current_user(request)
    expected = os.environ.get("REFRESH_TOKEN", "")
    if not (is_vip(user) and user["role"] == "admin") and (not expected or token != expected):
        raise HTTPException(403, "forbidden")
    report = sources.run_all()
    extract.run_extraction()
    return JSONResponse({"ok": True, "report": report, "stats": db.stats()})


@app.get("/api/stats")
def api_stats():
    return JSONResponse(db.stats())


@app.get("/api/brief")
def api_brief(request: Request, q: str = "", page: int = 1, page_size: int = 50):
    user = current_user(request)
    return JSONResponse(query_brief(user, q=q, page=page, page_size=page_size))


@app.get("/api/stocks")
def api_stocks(hours: int = 24):
    return JSONResponse(extract.stock_ranking(hours=hours, limit=120))


@app.get("/api/sectors")
def api_sectors(hours: int = 24):
    return JSONResponse(extract.sector_ranking(hours=hours, limit=80))


@app.get("/api/hot")
def api_hot(page: int = 1):
    return JSONResponse(query_hot(page))


# --------------------------------------------------------------------------
# K 线接口
# --------------------------------------------------------------------------
@app.get("/api/kline")
def api_kline(
    request: Request,
    code: str = Query(..., max_length=16),
    period: str = Query("day"),
    count: int = Query(120, ge=20, le=500),
):
    """取个股 K 线。会员或登录用户可用，未登录仅返回最近一段（与站点分层一致）。"""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "请先登录后查看K线")

    period = (period or "day").lower()
    if period not in kline.PERIODS:
        raise HTTPException(400, f"不支持的周期: {period}")

    # 访客/非会员限制历史长度，会员可看完整
    if not is_vip(user):
        count = min(count, 60)

    try:
        data = kline.fetch_kline(code, period, count)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"K线数据获取失败: {e}") from e

    # 附带该股在快讯里的提及，弹窗里做"异动"参照
    try:
        mentions = extract.stock_timeline(code, hours=72, limit=8)
    except Exception:  # noqa: BLE001
        mentions = []
    data["mentions"] = mentions
    data["is_vip"] = is_vip(user)
    return JSONResponse(data)


@app.get("/api/minute")
def api_minute(request: Request, code: str = Query(..., max_length=16)):
    """当日分时。"""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "请先登录后查看分时")
    try:
        return JSONResponse(kline.fetch_minute(code))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"分时数据获取失败: {e}") from e


@app.get("/api/indexes")
def api_indexes():
    """常用指数快照。"""
    return JSONResponse(kline.market_indexes())


@app.get("/img")
def img_proxy(u: str = Query(..., max_length=2048)):
    """图片代理：仅允许白名单图床，避免混合内容与防盗链问题，并防止 SSRF。"""
    if not imgs.proxy_allowed(u):
        raise HTTPException(403, "host not allowed")
    try:
        upstream = requests.get(
            u,
            headers={
                "User-Agent": sources.UA,
                "Referer": "https://" + urlsplit(u).netloc + "/",
            },
            timeout=15,
            stream=True,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"upstream error: {e}") from e
    if upstream.status_code != 200:
        raise HTTPException(502, f"upstream status {upstream.status_code}")

    ctype = upstream.headers.get("Content-Type", "image/png")
    if not ctype.startswith("image/"):
        raise HTTPException(415, "not an image")

    def _iter():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        _iter(),
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": fmt_cst(), "stats": db.stats()})


# --------------------------------------------------------------------------
# AI 智能分析层
# --------------------------------------------------------------------------
AI_ROUTES_ENABLED = ai is not None


def ai_status() -> dict:
    """AI 网关与启用状态（隐藏密钥）。"""
    return {
        "enabled": ai.ai_available() if ai else False,
        "model": ai.AI_MODEL if ai else "",
        "base_url": (ai.AI_BASE_URL if ai else ""),
        "import_ok": AI_ROUTES_ENABLED,
    }


@app.get("/insight", response_class=HTMLResponse)
def page_insight(request: Request):
    """AI 智能分析面板。会员/登录用户可看，访客走门禁。"""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/insight", status_code=302)
    senti = ai.sentiment_stats(24) if ai else {}
    return templates.TemplateResponse(
        request,
        "insight.html",
        ctx(request, user, senti=senti, ai_status=ai_status(), active="insight"),
    )


@app.get("/api/insight")
def api_insight(request: Request, date: str = ""):
    """AI 分析面板数据聚合：情绪统计 + 最新复盘 + AI 状态。"""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "请先登录")
    if not ai:
        raise HTTPException(503, "AI 模块不可用")
    senti = ai.sentiment_stats(24)
    review = {"cached": False, "content": "", "date": date}
    if date:
        review = ai.daily_review(date)
    else:
        latest = db.get_ai_report("daily_review", now_cst().strftime("%Y-%m-%d"))
        if latest:
            review = {"cached": True, "content": latest["content"], "date": now_cst().strftime("%Y-%m-%d"),
                      "model": latest["model"], "created_at": latest["created_at"]}
    return JSONResponse({"sentiment": senti, "review": review, "ai_status": ai_status()})


@app.get("/api/sentiment")
def api_sentiment(hours: int = Query(24, ge=1, le=720)):
    if not ai:
        raise HTTPException(503, "AI 模块不可用")
    return JSONResponse(ai.sentiment_stats(hours))


@app.get("/api/daily-review")
def api_daily_review(request: Request, date: str = Query(""), regenerate: int = Query(0)):
    """读取/生成每日复盘。regenerate=1 且为 admin 时强制重算。"""
    user = current_user(request)
    if not ai:
        raise HTTPException(503, "AI 模块不可用")
    if regenerate == 1:
        if not (is_vip(user) and user["role"] == "admin"):
            raise HTTPException(403, "仅管理员可强制重算")
        date = date or now_cst().strftime("%Y-%m-%d")
        try:
            db.save_ai_report("daily_review", date, "")  # 先清缓存避免复用
            return JSONResponse(ai.daily_review(date))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"复盘生成失败: {e}") from e
    date = date or now_cst().strftime("%Y-%m-%d")
    cached = db.get_ai_report("daily_review", date)
    if not cached:
        return JSONResponse({"cached": False, "content": "", "date": date, "pending": True})
    return JSONResponse({"cached": True, "content": cached["content"], "date": date,
                         "model": cached["model"], "created_at": cached["created_at"]})


@app.get("/api/ai/stock/{code}")
def api_stock_ai(request: Request, code: str, force: int = Query(0)):
    """个股 AI 分析（会员；force=1 需 admin）。"""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "请先登录")
    if not ai:
        raise HTTPException(503, "AI 模块不可用")
    if force == 1 and not (is_vip(user) and user["role"] == "admin"):
        raise HTTPException(403, "仅管理员可强制重算")
    try:
        return JSONResponse(ai.stock_analysis(code, force=bool(force)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"个股分析失败: {e}") from e


@app.get("/api/ai/stock")
def api_stock_ai_query(request: Request, code: str = Query(...), force: int = Query(0)):
    return api_stock_ai(request, code, force)


@app.post("/api/ai/trigger")
def api_ai_trigger(request: Request):
    """管理员手动触发：快讯批量解读 + 盘后复盘。"""
    user = current_user(request)
    if not (is_vip(user) and user["role"] == "admin"):
        raise HTTPException(403, "仅管理员可触发")
    if not ai:
        raise HTTPException(503, "AI 模块不可用")
    out = {"brief": ai.classify_pending(limit=20), "review": ai.generate_daily_review_if_due()}
    return JSONResponse(out)


@app.get("/api/ai/brief/{brief_id}")
def api_brief_ai(brief_id: int):
    """读取快讯 AI 解读结果。"""
    if not ai:
        raise HTTPException(503, "AI 模块不可用")
    row = db.get_ai_report("brief_ai", str(brief_id))
    if not row:
        return JSONResponse({"pending": True, "exists": False})
    try:
        content = json.loads(row["content"])
    except Exception:  # noqa: BLE001
        content = {"raw": row["content"]}
    return JSONResponse({"pending": False, "exists": True, "result": content,
                         "model": row["model"], "created_at": row["created_at"]})


@app.get("/api/stream")
def api_stream(request: Request, since_id: int = Query(0, ge=0)):
    """SSE：推送新快讯 + AI 解读结果（供 insight 页实时刷新）。"""
    import asyncio as _aio

    async def _iter():
        # 建立连接即发一条心跳，确认流可用
        yield "event: hello\ndata: {\"ok\":true,\"ai\":%s}\n\n" % ("true" if ai else "false")
        last_id = since_id
        if last_id == 0:
            # 未指定起点则从当前最新 id 开始，只推新数据，不回放历史
            last_id = get_conn().execute("SELECT COALESCE(MAX(id),0) FROM brief_items").fetchone()[0]
        while True:
            if await request.is_disconnected():
                break
            try:
                conn = get_conn()
                rows = conn.execute(
                    """SELECT b.id, b.source_label, b.title, b.published_at
                       FROM brief_items b WHERE b.id > ? ORDER BY b.id ASC LIMIT 20""",
                    (last_id,),
                ).fetchall()
                if rows:
                    for r in rows:
                        ai_row = db.get_ai_report("brief_ai", str(r["id"]))
                        ai_res = None
                        if ai_row:
                            try:
                                ai_res = json.loads(ai_row["content"])
                            except Exception:  # noqa: BLE001
                                ai_res = None
                        payload = {"id": r["id"], "source": r["source_label"],
                                   "title": r["title"], "published_at": r["published_at"],
                                   "ai": ai_res}
                        yield f"event: brief\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last_id = max(last_id, max(r["id"] for r in rows))
            except Exception as e:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            await _aio.sleep(60)

    return StreamingResponse(
        _iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


if __name__ == "__main__":
    import uvicorn

    db.init_db()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8848")))
