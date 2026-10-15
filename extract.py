"""抽取层：从快讯正文中抽取个股与板块，构建提及统计。

这是目标站"股票统计 / 板块统计 / 热点题材"的底层逻辑：
  1. 维护 A 股名称字典（名称 → 代码）
  2. 维护板块/概念词典
  3. 对每条快讯做名称匹配，记录 mention
  4. 按时间窗口聚合，做热度排名与趋势变化
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import timedelta

import requests

from db import CST, fmt_cst, get_conn, log_fetch, now_cst, tx
from sources import HEADERS, _get

# 板块/概念基础词典（可扩展；与目标站"板块统计"同构）
SECTOR_SEED = [
    "半导体", "芯片", "集成电路", "光刻机", "存储器", "先进封装", "第三代半导体",
    "人工智能", "AI", "算力", "大模型", "AIGC", "CPO", "液冷", "数据中心", "服务器",
    "机器人", "人形机器人", "减速器", "谐波减速器", "丝杠", "灵巧手",
    "新能源", "光伏", "储能", "风电", "氢能", "核电", "锂电", "固态电池", "钠电池",
    "新能源汽车", "汽车零部件", "智能驾驶", "无人驾驶", "车路云", "充电桩",
    "医药", "创新药", "CXO", "医疗器械", "中药", "疫苗", "减肥药", "GLP-1", "脑机接口",
    "白酒", "食品饮料", "预制菜", "养殖", "猪肉", "种业", "农业",
    "券商", "银行", "保险", "金融科技", "数字货币", "跨境支付", "稳定币",
    "房地产", "基建", "建材", "钢铁", "煤炭", "有色", "黄金", "稀土", "锂矿", "铜",
    "军工", "大飞机", "卫星", "商业航天", "低空经济", "无人机", "eVTOL",
    "消费电子", "折叠屏", "MR", "AR", "VR", "苹果链", "华为链", "PCB", "面板",
    "通信", "5G", "6G", "光模块", "卫星互联网", "量子科技", "量子计算",
    "传媒", "游戏", "短剧", "影视", "IP", "谷子经济", "教育", "旅游", "免税",
    "电力", "虚拟电厂", "特高压", "智能电网", "环保", "碳中和", "CCER",
    "国企改革", "中特估", "并购重组", "破净", "高股息", "次新股", "北交所",
    "港口", "航运", "物流", "快递", "航空", "机场", "高速公路",
    "化工", "氟化工", "磷化工", "钛白粉", "农药", "染料", "MDI",
    "纺织", "服装", "家居", "家电", "造纸", "包装",
]

# 噪音词：避免把常见词误判为股票名
NAME_STOPWORDS = {
    "中国", "中信", "中科", "东方", "建设", "发展", "国际", "科技", "股份", "集团",
    "证券", "银行", "投资", "基金", "指数", "市场", "行业", "公司", "板块", "概念",
}


def load_stock_dict(force: bool = False) -> int:
    """从新浪行情中心拉取 A 股名称字典并入库。"""
    conn = get_conn()
    existing = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    if existing > 1000 and not force:
        return existing

    base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    total = 0
    page = 1
    rows: list[tuple] = []
    while page <= 60:
        url = (
            base
            + "Market_Center.getHQNodeData"
            + f"?page={page}&num=100&sort=symbol&asc=1&node=hs_a"
        )
        try:
            r = _get(url, headers={"Referer": "https://finance.sina.com.cn/"})
            data = r.json()
        except Exception:  # noqa: BLE001
            break
        if not data:
            break
        for it in data:
            code = str(it.get("code") or "").strip()
            name = str(it.get("name") or "").strip()
            symbol = str(it.get("symbol") or "")
            if not code or not name:
                continue
            market = symbol[:2] if symbol[:2] in ("sh", "sz", "bj") else ""
            rows.append((code, name, market, ""))
            total += 1
        page += 1

    if rows:
        with tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO stocks(code,name,market,pinyin) VALUES (?,?,?,?)",
                rows,
            )
    log_fetch("stock_dict", "ok", total, f"pages={page - 1}")
    return total


def load_sector_dict() -> int:
    conn = get_conn()
    with tx() as c:
        for name in SECTOR_SEED:
            c.execute("INSERT OR IGNORE INTO sectors(name, kind) VALUES (?,?)", (name, "concept"))
    return conn.execute("SELECT COUNT(*) FROM sectors").fetchone()[0]


def _build_matchers():
    conn = get_conn()
    stocks = [
        (r["code"], r["name"])
        for r in conn.execute("SELECT code,name FROM stocks WHERE LENGTH(name)>=2")
    ]
    sectors = [r["name"] for r in conn.execute("SELECT name FROM sectors")]
    # 长名优先，避免短名吃掉长名
    stocks.sort(key=lambda x: -len(x[1]))
    sectors.sort(key=lambda x: -len(x))
    return stocks, sectors


def extract_mentions(brief_id: int, title: str, body: str) -> list[tuple]:
    """对单条快讯抽取个股与板块提及。"""
    text = f"{title}\n{body or ''}"
    if not text.strip():
        return []
    stocks, sectors = _build_matchers()
    found: list[tuple] = []
    consumed: list[tuple[int, int]] = []  # 已被占用的区间，避免重复

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in consumed)

    # 股票代码直接命中（如 600519 / sh600519）
    for m in re.finditer(r"\b(?:sh|sz|bj)?(\d{6})\b", text):
        code = m.group(1)
        row = get_conn().execute("SELECT name FROM stocks WHERE code=?", (code,)).fetchone()
        if row and not overlaps(m.start(), m.end()):
            found.append(("stock", code, row["name"], 1))
            consumed.append((m.start(), m.end()))

    # 股票名称命中
    for code, name in stocks:
        if name in NAME_STOPWORDS or len(name) < 2:
            continue
        start = 0
        cnt = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            if not overlaps(i, i + len(name)):
                consumed.append((i, i + len(name)))
                cnt += 1
            start = i + len(name)
        if cnt:
            found.append(("stock", code, name, cnt))

    # 板块命中
    for name in sectors:
        start = 0
        cnt = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            if not overlaps(i, i + len(name)):
                consumed.append((i, i + len(name)))
                cnt += 1
            start = i + len(name)
        if cnt:
            found.append(("sector", name, name, cnt))

    return found


def run_extraction(limit: int = 500) -> dict:
    """对最近未抽取的快讯跑抽取。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.id, b.title, b.body FROM brief_items b
           LEFT JOIN mentions m ON m.brief_id = b.id
           WHERE m.id IS NULL
           ORDER BY b.published_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    total_mentions = 0
    with tx() as c:
        for r in rows:
            for kind, ref_code, ref_name, cnt in extract_mentions(r["id"], r["title"], r["body"] or ""):
                c.execute(
                    """INSERT OR IGNORE INTO mentions(brief_id,kind,ref_code,ref_name,hit_count)
                       VALUES (?,?,?,?,?)""",
                    (r["id"], kind, ref_code, ref_name, cnt),
                )
                total_mentions += 1
    log_fetch("extract", "ok", len(rows), f"mentions={total_mentions}")
    return {"scanned": len(rows), "mentions": total_mentions}


# --------------------------------------------------------------------------
# 统计层：热度排名 / 趋势
# --------------------------------------------------------------------------
def stock_ranking(hours: int = 24, limit: int = 100) -> list[dict]:
    since = (now_cst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    rows = conn.execute(
        """SELECT m.ref_code AS code, m.ref_name AS name,
                  COUNT(*) AS mentions, SUM(m.hit_count) AS hits,
                  MAX(b.published_at) AS last_at
           FROM mentions m JOIN brief_items b ON b.id = m.brief_id
           WHERE m.kind='stock' AND b.published_at >= ?
           GROUP BY m.ref_code, m.ref_name
           ORDER BY mentions DESC, hits DESC
           LIMIT ?""",
        (since, limit),
    ).fetchall()
    out = []
    prev_since = (now_cst() - timedelta(hours=hours * 2)).strftime("%Y-%m-%d %H:%M:%S")
    for i, r in enumerate(rows, 1):
        prev = conn.execute(
            """SELECT COUNT(*) FROM mentions m JOIN brief_items b ON b.id=m.brief_id
               WHERE m.kind='stock' AND m.ref_code=? AND b.published_at>=? AND b.published_at<?""",
            (r["code"], prev_since, since),
        ).fetchone()[0]
        cur = r["mentions"]
        trend = "up" if cur > prev else ("down" if cur < prev else "flat")
        out.append(
            {
                "rank": i,
                "code": r["code"],
                "name": r["name"],
                "mentions": cur,
                "hits": r["hits"] or cur,
                "prev_mentions": prev,
                "trend": trend,
                "change": cur - prev,
                "last_at": r["last_at"],
            }
        )
    return out


def sector_ranking(hours: int = 24, limit: int = 60) -> list[dict]:
    since = (now_cst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    rows = conn.execute(
        """SELECT m.ref_name AS name, COUNT(*) AS mentions, SUM(m.hit_count) AS hits,
                  MAX(b.published_at) AS last_at
           FROM mentions m JOIN brief_items b ON b.id = m.brief_id
           WHERE m.kind='sector' AND b.published_at >= ?
           GROUP BY m.ref_name
           ORDER BY mentions DESC, hits DESC
           LIMIT ?""",
        (since, limit),
    ).fetchall()
    prev_since = (now_cst() - timedelta(hours=hours * 2)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for i, r in enumerate(rows, 1):
        prev = conn.execute(
            """SELECT COUNT(*) FROM mentions m JOIN brief_items b ON b.id=m.brief_id
               WHERE m.kind='sector' AND m.ref_name=? AND b.published_at>=? AND b.published_at<?""",
            (r["name"], prev_since, since),
        ).fetchone()[0]
        cur = r["mentions"]
        out.append(
            {
                "rank": i,
                "name": r["name"],
                "mentions": cur,
                "hits": r["hits"] or cur,
                "prev_mentions": prev,
                "trend": "up" if cur > prev else ("down" if cur < prev else "flat"),
                "change": cur - prev,
                "last_at": r["last_at"],
            }
        )
    return out


def stock_timeline(code: str, hours: int = 72, limit: int = 60) -> list[dict]:
    since = (now_cst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.id, b.source, b.source_label, b.title, b.body, b.url, b.published_at, m.hit_count
           FROM mentions m JOIN brief_items b ON b.id = m.brief_id
           WHERE m.kind='stock' AND m.ref_code=? AND b.published_at >= ?
           ORDER BY b.published_at DESC LIMIT ?""",
        (code, since, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def sector_timeline(name: str, hours: int = 72, limit: int = 60) -> list[dict]:
    since = (now_cst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.id, b.source, b.source_label, b.title, b.body, b.url, b.published_at, m.hit_count
           FROM mentions m JOIN brief_items b ON b.id = m.brief_id
           WHERE m.kind='sector' AND m.ref_name=? AND b.published_at >= ?
           ORDER BY b.published_at DESC LIMIT ?""",
        (name, since, limit),
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    from db import init_db, stats

    init_db()
    print("stocks:", load_stock_dict())
    print("sectors:", load_sector_dict())
    print(json.dumps(run_extraction(), ensure_ascii=False))
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    print("TOP stocks:", json.dumps(stock_ranking(limit=10), ensure_ascii=False, indent=2))
    print("TOP sectors:", json.dumps(sector_ranking(limit=10), ensure_ascii=False, indent=2))
