"""K 线数据层：取日/周/月与分钟级 K 线，做归一化与缓存。

数据源（均为实测可用的公开接口）：
  日/周/月：腾讯 https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
  分钟级  ：新浪 https://money.finance.sina.com.cn/.../CN_MarketData.getKLineData?scale=5|15|30|60
  分时    ：腾讯 https://web.ifzq.gtimg.cn/appstock/app/minute/query

腾讯返回结构（字段名随品种/周期变化，需归一化）：
  data.<sym>.qfqday / day / qfqweek / qfqmonth : [[日期, 开, 收, 高, 低, 成交量(手)], ...]
  注意数组顺序是 [日期, 开, 收, 高, 低, 量] —— 收盘在第 3 位，不是第 2 位。

新浪分钟返回结构：
  [{"day":"2026-10-15 09:40:00","open":"..","high":"..","low":"..","close":"..","volume":".."}, ...]
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from db import CST, fmt_cst, get_conn, now_cst, tx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://gu.qq.com/",
    "Accept": "application/json, text/plain, */*",
}

# 周期白名单 → (数据源, 参数)
#   腾讯 fqkline 支持 day/week/month
#   新浪 getKLineData 支持 5/15/30/60 分钟
PERIODS = {
    "day": ("tencent", "day"),
    "week": ("tencent", "week"),
    "month": ("tencent", "month"),
    "m5": ("sina", "5"),
    "m15": ("sina", "15"),
    "m30": ("sina", "30"),
    "m60": ("sina", "60"),
}

PERIOD_LABELS = {
    "day": "日K",
    "week": "周K",
    "month": "月K",
    "m5": "5分钟",
    "m15": "15分钟",
    "m30": "30分钟",
    "m60": "60分钟",
    "minute": "分时",
}

# 缓存时长（秒）：分钟级短缓存，日线长缓存
CACHE_TTL = {
    "day": 300,
    "week": 1800,
    "month": 3600,
    "m5": 60,
    "m15": 120,
    "m30": 180,
    "m60": 300,
    "minute": 60,
}

_lock = threading.Lock()
_mem: dict[str, tuple[float, dict]] = {}


# --------------------------------------------------------------------------
# 代码 → 腾讯符号
# --------------------------------------------------------------------------
def to_symbol(code: str) -> Optional[str]:
    """把 6 位股票代码转成腾讯符号（sh/sz/bj 前缀）。

    优先用 stocks 表里已存的 market 字段；缺失时按代码首位推断。
    """
    code = (code or "").strip().upper()
    if not code:
        return None
    # 已带前缀
    for p in ("SH", "SZ", "BJ"):
        if code.startswith(p) and len(code) == 8:
            return p.lower() + code[2:]
    if not code.isdigit() or len(code) != 6:
        return None

    conn = get_conn()
    row = conn.execute("SELECT market FROM stocks WHERE code=?", (code,)).fetchone()
    market = (row["market"] if row else "") or ""
    if market in ("sh", "sz", "bj"):
        return market + code

    # 兜底推断
    head = code[0]
    if head == "6":
        return "sh" + code
    if head in ("0", "3"):
        return "sz" + code
    if head in ("4", "8", "9"):
        return "bj" + code
    return None


def stock_name(code: str) -> str:
    row = get_conn().execute("SELECT name FROM stocks WHERE code=?", (code,)).fetchone()
    return row["name"] if row else code


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def _get_json(url: str, timeout: int = 15) -> dict:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {last}")


def _parse_bars(rows: list) -> list[dict]:
    """把腾讯 K 线数组归一化为标准 bar。

    输入行格式: [日期, 开, 收, 高, 低, 量]（部分品种可能带额外字段）
    """
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            d = {
                "date": str(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            }
        except (TypeError, ValueError):
            continue
        out.append(d)
    return out


def _add_ma(bars: list[dict], windows=(5, 10, 20, 60)) -> list[dict]:
    """就地计算均线。"""
    closes = [b["close"] for b in bars]
    for w in windows:
        key = f"ma{w}"
        for i, b in enumerate(bars):
            if i + 1 < w:
                b[key] = None
            else:
                seg = closes[i - w + 1 : i + 1]
                b[key] = round(sum(seg) / w, 3)
    return bars


def _fetch_tencent(symbol: str, period: str, count: int) -> tuple[list[dict], dict]:
    """取日/周/月 K 线（腾讯）。返回 (bars, quote)。"""
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},{period},,,{count},qfq"
    )
    data = _get_json(url)
    if str(data.get("code")) not in ("0", "None"):
        raise RuntimeError(f"tencent code={data.get('code')} msg={data.get('msg')}")

    node = (data.get("data") or {}).get(symbol) or {}
    rows = None
    for key in (f"qfq{period}", period, "qfqday", "day"):
        v = node.get(key)
        if isinstance(v, list) and v:
            rows = v
            break
    bars = _parse_bars(rows or [])

    quote = {}
    qt = node.get("qt")
    if isinstance(qt, dict):
        arr = qt.get(symbol)
        if isinstance(arr, list) and len(arr) > 5:
            try:
                quote = {
                    "price": float(arr[3]),
                    "prev_close": float(arr[4]),
                    "open": float(arr[5]),
                    "volume": float(arr[6]) if len(arr) > 6 else None,
                    "time": arr[30] if len(arr) > 30 else "",
                    "high": float(arr[33]) if len(arr) > 33 else None,
                    "low": float(arr[34]) if len(arr) > 34 else None,
                }
            except (TypeError, ValueError):
                quote = {}
    return bars, quote


def _fetch_sina_minute(symbol: str, scale: str, count: int) -> list[dict]:
    """取分钟级 K 线（新浪）。"""
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&ma=no&datalen={count}&scale={scale}"
    )
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={**HEADERS, "Referer": "https://finance.sina.com.cn/"}, timeout=20)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                time.sleep(0.5 * (attempt + 1))
                continue
            txt = r.text.strip()
            if not txt or txt in ("null", "[]"):
                return []
            arr = json.loads(txt)
            out = []
            for it in arr:
                if not isinstance(it, dict):
                    continue
                try:
                    out.append({
                        "date": str(it.get("day") or it.get("d") or ""),
                        "open": float(it["open"]),
                        "close": float(it["close"]),
                        "high": float(it["high"]),
                        "low": float(it["low"]),
                        "volume": float(it.get("volume") or 0),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            return out
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"sina minute failed: {last}")


def fetch_kline(code: str, period: str = "day", count: int = 120, use_cache: bool = True) -> dict:
    """取 K 线。返回 {code, name, symbol, period, bars, quote, source, fetched_at}。"""
    symbol = to_symbol(code)
    if not symbol:
        raise ValueError(f"无法识别的股票代码: {code}")

    period = (period or "day").lower()
    if period not in PERIODS:
        period = "day"
    count = max(20, min(int(count or 120), 500))

    cache_key = f"{symbol}:{period}:{count}"
    now = time.time()
    if use_cache:
        with _lock:
            hit = _mem.get(cache_key)
        if hit and (now - hit[0]) < CACHE_TTL.get(period, 300):
            return hit[1]

    source, param = PERIODS[period]
    quote: dict = {}
    if source == "tencent":
        bars, quote = _fetch_tencent(symbol, param, count)
    else:
        bars = _fetch_sina_minute(symbol, param, count)
        # 分钟线补一个快照，用于显示实时价
        if bars:
            last_close = bars[-1]["close"]
            prev = bars[-2]["close"] if len(bars) > 1 else last_close
            quote = {"price": last_close, "prev_close": prev, "time": bars[-1]["date"]}

    if not bars:
        raise RuntimeError(f"未取到K线数据: {symbol} {period}")

    _add_ma(bars)

    result = {
        "code": code,
        "name": stock_name(code),
        "symbol": symbol,
        "period": period,
        "period_label": PERIOD_LABELS.get(period, period),
        "bars": bars,
        "quote": quote,
        "source": source,
        "fetched_at": fmt_cst(),
    }

    with _lock:
        _mem[cache_key] = (now, result)
        if len(_mem) > 400:
            oldest = sorted(_mem.items(), key=lambda kv: kv[1][0])[:100]
            for k, _ in oldest:
                _mem.pop(k, None)
    return result


def fetch_minute(code: str) -> dict:
    """取当日分时（时间/价/量）。"""
    symbol = to_symbol(code)
    if not symbol:
        raise ValueError(f"无法识别的股票代码: {code}")

    cache_key = f"{symbol}:minute"
    now = time.time()
    with _lock:
        hit = _mem.get(cache_key)
    if hit and (now - hit[0]) < CACHE_TTL["minute"]:
        return hit[1]

    data = _get_json(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}")
    node = (data.get("data") or {}).get(symbol) or {}
    inner = (node.get("data") or {}) if isinstance(node.get("data"), dict) else {}
    lines = inner.get("data") or []
    prev_close = None
    try:
        prev_close = float(node.get("prec") or inner.get("prec") or 0) or None
    except (TypeError, ValueError):
        prev_close = None

    points = []
    for line in lines:
        parts = str(line).split()
        if len(parts) < 2:
            continue
        try:
            t = parts[0]
            hh, mm = t[:2], t[2:4]
            points.append({
                "time": f"{hh}:{mm}",
                "price": float(parts[1]),
                "volume": float(parts[2]) if len(parts) > 2 else 0.0,
            })
        except (TypeError, ValueError):
            continue

    result = {
        "code": code,
        "name": stock_name(code),
        "symbol": symbol,
        "period": "minute",
        "points": points,
        "prev_close": prev_close,
        "source": "tencent",
        "fetched_at": fmt_cst(),
    }
    with _lock:
        _mem[cache_key] = (now, result)
    return result


def market_indexes() -> list[dict]:
    """常用指数快照，弹窗里做参照用。"""
    out = []
    for sym, label in [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]:
        try:
            k = fetch_kline(sym, "day", 30)
            bars = k["bars"]
            if len(bars) >= 2:
                last, prev = bars[-1], bars[-2]
                chg = last["close"] - prev["close"]
                out.append({
                    "symbol": sym,
                    "label": label,
                    "close": last["close"],
                    "change": round(chg, 2),
                    "pct": round(chg / prev["close"] * 100, 2) if prev["close"] else 0.0,
                })
        except Exception:  # noqa: BLE001
            continue
    return out
