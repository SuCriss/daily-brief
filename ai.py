"""AI 智能分析层。

能力（全部走 OpenAI 兼容 /chat/completions 协议，可通过环境变量切换网关）：

  1. 快讯 AI 解读：对单条快讯做情绪判断（利好/利空/中性）、市场影响评分、
     题材标签与一句话解读 —— 结果按 brief_id 缓存到 ai_reports(kind=brief_ai)。
  2. 每日 AI 复盘：汇总当日全部快讯 + 个股/板块提及 + 热搜 + 情绪分布，
     生成 Markdown 复盘 —— 按日期缓存（kind=daily_review）。
  3. 个股 AI 分析：K 线技术面摘要 + 近期提及快讯 + 情绪，生成个股解读
     —— 按股票代码缓存（kind=stock_ai），24h 过期。

环境变量：
  AI_ENABLED   1/0，默认 1
  AI_BASE_URL  默认 https://agent2api.chrisite.eu.cc/v1
               服务器上建议设为 http://127.0.0.1:3065/v1（内网直连 agent2api 网关，绕开 CF WAF）
  AI_API_KEY   agent2api 网关密钥
  AI_MODEL     默认 Qwen3.8-Flash
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import timedelta
from typing import Any, Optional

import requests

import db
from db import fmt_cst, get_conn, now_cst

AI_ENABLED = os.environ.get("AI_ENABLED", "1") not in ("0", "false", "False")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://agent2api.chrisite.eu.cc/v1").rstrip("/")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "Qwen3.8-Flash")
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "120"))

# 每轮调度最多解读的快讯条数（控制 token 消耗）
AI_BRIEF_BUDGET = int(os.environ.get("AI_BRIEF_BUDGET", "10"))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def ai_available() -> bool:
    return AI_ENABLED and bool(AI_BASE_URL)


def _chat(messages: list[dict], *, json_mode: bool = False, max_tokens: int = 900,
          temperature: float = 0.3) -> dict:
    """调用 OpenAI 兼容 chat/completions，返回 {content, usage}。"""
    payload: dict[str, Any] = {
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _UA,
        "Accept": "application/json",
        "Origin": AI_BASE_URL,
        "Referer": AI_BASE_URL + "/",
    }
    if AI_API_KEY:
        headers["Authorization"] = "Bearer " + AI_API_KEY

    last = None
    for attempt in range(2):
        try:
            r = requests.post(
                AI_BASE_URL + "/chat/completions",
                json=payload, headers=headers, timeout=AI_TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                msg = d["choices"][0]["message"]["content"] or ""
                usage = d.get("usage") or {}
                return {
                    "content": msg,
                    "tokens_in": int(usage.get("prompt_tokens") or 0),
                    "tokens_out": int(usage.get("completion_tokens") or 0),
                    "credits": float(usage.get("credits") or 0.0),
                }
            last = f"HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"LLM call failed: {last}")


def _parse_json(text: str) -> Optional[dict]:
    """宽容地解析模型返回的 JSON（剥掉 code fence / 前后缀）。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            t = m.group(0)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return None


def _cached(kind: str, ref_key: str) -> Optional[dict]:
    return db.get_ai_report(kind, ref_key)


# --------------------------------------------------------------------------
# 1) 快讯 AI 解读
# --------------------------------------------------------------------------
SENTI_CN = {"positive": "利好", "negative": "利空", "neutral": "中性"}

_BRIEF_SYS = (
    "你是A股市场快讯分析助手。对用户给出的财经快讯做结构化分析，只输出JSON，"
    '字段：sentiment(利好/利空/中性之一，英文 positive/negative/neutral)、'
    "impact(0-100整数，对市场的影响程度，情绪化纯八卦给低分)、"
    "tags(1-5个题材关键词字符串数组，用中文)、"
    "summary(不超过60字的一句话解读，说清消息实质与潜在影响)。"
    "不要输出JSON以外的任何内容。"
)


def classify_brief(brief: dict) -> dict:
    """单条快讯 AI 解读。返回结构化 dict，失败抛异常。"""
    key = str(brief["id"])
    text = f"[来源:{brief['source_label']}] {brief['title']}"
    if brief.get("body"):
        text += f"\n{brief['body'][:1200]}"
    r = _chat(
        [{"role": "system", "content": _BRIEF_SYS},
         {"role": "user", "content": text}],
        json_mode=True, max_tokens=400,
    )
    parsed = _parse_json(r["content"])
    if not parsed:
        raise RuntimeError(f"bad json: {r['content'][:200]!r}")
    out = {
        "sentiment": str(parsed.get("sentiment", "neutral") or "neutral")[:12],
        "impact": max(0, min(100, int(parsed.get("impact") or 0))),
        "tags": [str(t)[:20] for t in (parsed.get("tags") or []) if str(t).strip()][:5],
        "summary": str(parsed.get("summary") or "")[:200],
    }
    db.save_ai_report(
        "brief_ai", key,
        json.dumps(out, ensure_ascii=False),
        model=AI_MODEL,
        meta=json.dumps({"sentiment": out["sentiment"]}, ensure_ascii=False),
        tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
    )
    db.log_ai_usage("brief_ai", "ok", tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                     credits=r["credits"])
    return out


def ai_pending_briefs(limit: int = AI_BRIEF_BUDGET) -> list[dict]:
    """取尚未 AI 解读、且与市场相关（有提及/重要标记）的快讯，按时间倒序。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.id, b.source, b.source_label, b.title, b.body, b.is_important
           FROM brief_items b
           LEFT JOIN ai_reports a ON a.kind='brief_ai' AND a.ref_key = CAST(b.id AS TEXT)
           LEFT JOIN mentions m ON m.brief_id = b.id
           WHERE a.id IS NULL
             AND (b.is_important = 1 OR m.id IS NOT NULL)
           GROUP BY b.id
           ORDER BY b.published_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def classify_pending(limit: int = AI_BRIEF_BUDGET) -> dict:
    """批量解读待处理快讯，返回统计。"""
    if not ai_available():
        return {"ok": False, "reason": "AI disabled"}
    done, errs = 0, []
    for brief in ai_pending_briefs(limit):
        try:
            classify_brief(brief)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"{brief['id']}: {e}")
    return {"done": done, "errors": errs}


# --------------------------------------------------------------------------
# 2) 每日 AI 复盘
# --------------------------------------------------------------------------
_DAILY_SYS = (
    "你是A股盘后复盘研究员。根据用户给出的当日市场数据（快讯统计、个股/板块热度、"
    "热搜与情绪分布），输出一份中文 Markdown 复盘。"
    "结构：## 市场情绪总览 / ## 主线板块 / ## 强势个股 / ## 风险提示 / ## 明日关注。"
    "语言精炼、数据驱动，不要编造数据中没有的内容，不要输出JSON，直接输出Markdown。"
)


def daily_review(date: str = "") -> dict:
    """生成或读取指定日期的 AI 复盘（date 格式 YYYY-MM-DD，默认今天）。"""
    date = date or now_cst().strftime("%Y-%m-%d")
    cached = _cached("daily_review", date)
    if cached:
        return {"date": date, "cached": True, "content": cached["content"],
                "model": cached["model"], "created_at": cached["created_at"]}

    from extract import sector_ranking, stock_ranking  # 延迟导入避免环

    conn = get_conn()
    since = f"{date} 00:00:00"
    until = f"{date} 23:59:59"

    n_brief = conn.execute(
        "SELECT COUNT(*) FROM brief_items WHERE published_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]
    n_hot = conn.execute(
        "SELECT COUNT(*) FROM hot_rows WHERE captured_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]
    n_imp = conn.execute(
        "SELECT COUNT(*) FROM brief_items WHERE is_important=1 AND published_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]

    hot_titles = [
        r["title"] for r in conn.execute(
            "SELECT DISTINCT title FROM hot_rows WHERE captured_at BETWEEN ? AND ? ORDER BY captured_at DESC LIMIT 20",
            (since, until))
    ]
    # 情绪分布（来自快讯 AI 解读）
    senti_rows = conn.execute(
        """SELECT a.meta FROM ai_reports a JOIN brief_items b
           ON a.kind='brief_ai' AND a.ref_key = CAST(b.id AS TEXT)
           WHERE b.published_at BETWEEN ? AND ?""",
        (since, until)).fetchall()
    senti_cnt = {"positive": 0, "negative": 0, "neutral": 0}
    for r in senti_rows:
        try:
            s = json.loads(r["meta"] or "{}").get("sentiment")
        except Exception:  # noqa: BLE001
            s = None
        if s in senti_cnt:
            senti_cnt[s] += 1

    stocks = stock_ranking(hours=48, limit=15)
    sectors = sector_ranking(hours=48, limit=12)

    ctx = {
        "date": date,
        "brief_count": n_brief,
        "hot_count": n_hot,
        "important_count": n_imp,
        "sentiment": {SENTI_CN.get(k, k): v for k, v in senti_cnt.items()},
        "ai_brief_count": sum(senti_cnt.values()),
        "hot_top": hot_titles[:20],
        "top_stocks": [{"name": s["name"], "code": s["code"], "mentions": s["mentions"]} for s in stocks],
        "top_sectors": [{"name": s["name"], "mentions": s["mentions"]} for s in sectors],
    }
    user_msg = "当日市场数据如下：\n" + json.dumps(ctx, ensure_ascii=False, indent=1)
    r = _chat(
        [{"role": "system", "content": _DAILY_SYS},
         {"role": "user", "content": user_msg}],
        max_tokens=1600,
    )
    db.save_ai_report(
        "daily_review", date, r["content"], model=AI_MODEL,
        meta=json.dumps({"sentiment": senti_cnt}, ensure_ascii=False),
        tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
    )
    db.log_ai_usage("daily_review", "ok", tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                     credits=r["credits"])
    return {"date": date, "cached": False, "content": r["content"],
            "model": AI_MODEL, "created_at": fmt_cst()}


def generate_daily_review_if_due() -> dict:
    """盘后 15:30 以后为当天生成复盘（幂等）。"""
    now = now_cst()
    if not ai_available() or now.hour < 15 or (now.hour == 15 and now.minute < 30):
        return {"skipped": True, "reason": "not after 15:30"}
    date = now.strftime("%Y-%m-%d")
    if _cached("daily_review", date):
        return {"skipped": True, "reason": "cached"}
    try:
        return {"ok": True, **daily_review(date)}
    except Exception as e:  # noqa: BLE001
        db.log_ai_usage("daily_review", "error", message=str(e))
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# 3) 个股 AI 分析
# --------------------------------------------------------------------------
_STOCK_SYS = (
    "你是A股个股研究员。根据用户给出的个股技术面数据与近期消息面（快讯提及+情绪），"
    "输出中文 Markdown 分析：## 一句话结论 / ## 消息面 / ## 技术面 / ## 风险提示。"
    "数据驱动、客观中立，不要编造，明确说明这是信息整理而非投资建议。"
    "不要输出JSON，直接输出Markdown。"
)


def _kline_tech_summary(code: str) -> dict:
    import kline

    try:
        d = kline.fetch_kline(code, "day", 60)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    bars = d.get("bars") or []
    if not bars:
        return {"error": "no kline data"}
    closes = [b["close"] for b in bars]
    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last

    def pct(a, b):
        return round((a / b - 1) * 100, 2) if b else 0.0

    def ma(n):
        s = closes[-n:]
        return round(sum(s) / len(s), 3) if s else 0.0

    highs = [b["high"] for b in bars[-20:]]
    lows = [b["low"] for b in bars[-20:]]
    vols = [b.get("volume") or 0 for b in bars]
    vol_avg5 = sum(vols[-5:]) / 5 if vols else 0
    vol_avg20 = sum(vols[-20:]) / 20 if vols else 0
    return {
        "name": d.get("name", ""),
        "code": code,
        "last": last,
        "day_pct": pct(last, prev),
        "pct_5d": pct(last, closes[-6]) if len(closes) >= 6 else None,
        "pct_20d": pct(last, closes[-21]) if len(closes) >= 21 else None,
        "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60),
        "high_20d": max(highs), "low_20d": min(lows),
        "vol_ratio": round(vol_avg5 / vol_avg20, 2) if vol_avg20 else 0.0,
        "vol_trend": "放量" if vol_avg5 > vol_avg20 * 1.15 else ("缩量" if vol_avg5 < vol_avg20 * 0.85 else "平量"),
    }


def stock_analysis(code: str, force: bool = False) -> dict:
    """个股 AI 分析（缓存 24h，force=True 强制重算）。"""
    key = f"stock:{code}"
    cached = _cached("stock_ai", key)
    if cached and not force:
        age_ok = True
        try:
            from datetime import datetime

            age_ok = (now_cst() - datetime.strptime(cached["created_at"], "%Y-%m-%d %H:%M:%S")) < timedelta(hours=24)
        except Exception:  # noqa: BLE001
            age_ok = False
        if age_ok:
            return {"code": code, "cached": True, "content": cached["content"],
                    "model": cached["model"], "created_at": cached["created_at"]}

    if not ai_available():
        return {"code": code, "error": "AI disabled"}

    tech = _kline_tech_summary(code)
    from extract import stock_timeline

    mentions = stock_timeline(code, hours=72, limit=12)
    senti = []
    for m in mentions:
        row = _cached("brief_ai", str(m["id"]))
        s = None
        if row:
            try:
                s = json.loads(row["content"]).get("sentiment")
            except Exception:  # noqa: BLE001
                s = None
        senti.append(s)

    ctx = {
        "tech": tech,
        "mentions": [
            {"time": m["published_at"], "source": m["source_label"],
             "title": m["title"][:160], "sentiment": s}
            for m, s in zip(mentions, senti)
        ],
    }
    r = _chat(
        [{"role": "system", "content": _STOCK_SYS},
         {"role": "user", "content": json.dumps(ctx, ensure_ascii=False, indent=1)}],
        max_tokens=1200,
    )
    db.save_ai_report(
        "stock_ai", key, r["content"], model=AI_MODEL,
        meta=json.dumps({"code": code, "name": tech.get("name", "")}, ensure_ascii=False),
        tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
    )
    db.log_ai_usage("stock_ai", "ok", tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                     credits=r["credits"])
    return {"code": code, "cached": False, "content": r["content"],
            "model": AI_MODEL, "created_at": fmt_cst()}


# --------------------------------------------------------------------------
# 情绪统计
# --------------------------------------------------------------------------
def sentiment_stats(hours: int = 24) -> dict:
    """基于快讯 AI 解读做情绪聚合统计。"""
    since = (now_cst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    rows = conn.execute(
        """SELECT a.meta, a.content, a.created_at FROM ai_reports a JOIN brief_items b
           ON a.kind='brief_ai' AND a.ref_key = CAST(b.id AS TEXT)
           WHERE a.created_at >= ?""",
        (since,),
    ).fetchall()
    cnt = {"positive": 0, "negative": 0, "neutral": 0}
    impact_sum, impact_n = 0, 0
    hourly: dict[str, dict] = {}
    for r in rows:
        try:
            d = json.loads(r["meta"] or "{}")
            content = json.loads(r["content"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        s = d.get("sentiment")
        if s in cnt:
            cnt[s] += 1
            impact_sum += int(content.get("impact") or 0)
            impact_n += 1
        hour = (r["created_at"] or "")[:13]
        bucket = hourly.setdefault(hour, {"positive": 0, "negative": 0, "neutral": 0})
        if s in bucket:
            bucket[s] += 1
    total = sum(cnt.values())
    return {
        "hours": hours,
        "total": total,
        "counts": cnt,
        "ratio": {
            k: round(v / total, 4) if total else 0.0 for k, v in cnt.items()
        },
        "avg_impact": round(impact_sum / impact_n, 1) if impact_n else 0.0,
        "hourly": [
            {"hour": h, **v} for h, v in sorted(hourly.items())
        ],
        "bull_index": round(
            (cnt["positive"] - cnt["negative"]) / total * 100, 1
        ) if total else 0.0,  # 多空指数 -100~100
    }


def markdown_preview(md: str, n: int = 140) -> str:
    """Markdown 转纯文本摘要（用于列表页预览）。"""
    t = re.sub(r"^#+\s*", "", md, flags=re.M)
    t = re.sub(r"[*_`>\[\]()]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]
