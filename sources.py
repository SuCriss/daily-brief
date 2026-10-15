"""采集层：抓取与目标站相同的公开上游源。

逆向确认的目标站数据来源：
  热搜类（source_kind=hot）：微博热搜、百度热搜、抖音热搜
  快讯类（source_kind=news）：财联社电报、东方财富 7×24、新浪财经滚动

关键逆向成果：
  财联社 /v1/roll/get_roll_list 的 sign = md5(sha1(排序后的query_string))
  这是该站能持续拉到电报数据的核心，这里原样复现。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

import requests

import imgs
from db import CST, fmt_cst, get_conn, log_fetch, now_cst, tx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _get(url: str, *, headers: dict | None = None, timeout: int = 20, **kw) -> requests.Response:
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers=h, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def _loads(text: str) -> Any:
    text = text.strip()
    # 处理 JSONP 包装：jQuery123({...});
    m = re.match(r"^[\w$]+\((.*)\);?$", text, re.S)
    if m:
        text = m.group(1)
    return json.loads(text)


# --------------------------------------------------------------------------
# 热搜源
# --------------------------------------------------------------------------
def fetch_weibo_hot() -> list[dict]:
    r = _get("https://weibo.com/ajax/side/hotSearch", headers={"Referer": "https://weibo.com/"})
    data = r.json()
    out = []
    for it in (data.get("data", {}) or {}).get("realtime", []) or []:
        word = it.get("word") or it.get("note") or ""
        if not word:
            continue
        out.append(
            {
                "external_id": f"weibo:{word}",
                "title": word,
                "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(word)}",
                "rank": it.get("rank"),
                "label": it.get("label_name") or it.get("icon_desc") or "",
                "payload": it,
            }
        )
    return out


def fetch_baidu_hot() -> list[dict]:
    # 百度对海外 IP 偶发限流/超时，缩短单次超时避免拖慢调度轮次
    r = _get("https://top.baidu.com/api/board?platform=wise&tab=realtime", timeout=8)
    # 百度该接口返回 UTF-8，但 requests 有时按 ISO-8859-1 猜编码
    r.encoding = "utf-8"
    data = r.json()
    out = []

    def _walk(node) -> None:
        """递归收集带 word/query 的条目，兼容 tabTextList 嵌套结构。"""
        if isinstance(node, dict):
            word = node.get("word") or node.get("query")
            if word and isinstance(word, str):
                out.append(
                    {
                        "external_id": f"baidu:{word}",
                        "title": word,
                        "url": node.get("url")
                        or f"https://www.baidu.com/s?wd={requests.utils.quote(word)}",
                        "rank": node.get("index"),
                        "label": "置顶" if node.get("isTop") else "",
                        "payload": node,
                    }
                )
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk((data.get("data") or {}).get("cards") or [])
    # 去重（同一 title 只留首个）
    seen, uniq = set(), []
    for it in out:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        uniq.append(it)
    return uniq


def fetch_douyin_hot() -> list[dict]:
    url = (
        "https://www.douyin.com/aweme/v1/web/hot/search/list/"
        "?device_platform=webapp&aid=6383&channel=channel_pc_web"
    )
    r = _get(url, headers={"Referer": "https://www.douyin.com/"})
    data = r.json()
    out = []
    words = ((data.get("data") or {}).get("word_list")) or []
    for idx, it in enumerate(words, 1):
        word = it.get("word") or ""
        if not word:
            continue
        out.append(
            {
                "external_id": f"douyin:{word}",
                "title": word,
                "url": f"https://www.douyin.com/search/{requests.utils.quote(word)}?type=general",
                "rank": it.get("position") or idx,
                "label": "",
                "payload": it,
            }
        )
    return out


# --------------------------------------------------------------------------
# 快讯源
# --------------------------------------------------------------------------
CLS_APP = "CailianpressWeb"
CLS_OS = "web"
CLS_SV = "7.7.5"


def cls_sign(params: dict) -> str:
    """财联社签名：md5(sha1(sorted query string))。

    这是逆向出的核心算法，参数按 key 字典序拼接成 k=v&k=v，
    再 sha1 取 hex，最后 md5 取 hex。
    """
    text = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sha1_hex = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return hashlib.md5(sha1_hex.encode("utf-8")).hexdigest()


def fetch_cls_telegraph(rn: int = 50) -> list[dict]:
    params = {
        "app": CLS_APP,
        "category": "",
        "last_time": "",
        "os": CLS_OS,
        "refresh_type": "1",
        "rn": str(rn),
        "sv": CLS_SV,
    }
    params["sign"] = cls_sign(params)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    r = _get(f"https://www.cls.cn/v1/roll/get_roll_list?{qs}", headers={"Referer": "https://www.cls.cn/telegraph"})
    data = r.json()
    if str(data.get("errno")) not in ("0", "None"):
        raise RuntimeError(f"cls errno={data.get('errno')} msg={data.get('msg')}")

    out = []
    for it in ((data.get("data") or {}).get("roll_data")) or []:
        title = it.get("title") or it.get("brief") or ""
        content = it.get("content") or ""
        if not (title or content):
            continue
        ts = it.get("ctime") or it.get("modified_time")
        pub = (
            datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else fmt_cst()
        )
        imgs_raw: list = []
        # 财联社有多个图片字段：images/img 是正文配图，imgs 是附图列表，
        # share_img 基本是分享占位图（由 imgs.normalize_images 过滤掉）
        for key in ("images", "img", "imgs", "image", "share_img"):
            v = it.get(key)
            if isinstance(v, list):
                imgs_raw.extend(v)
            elif v:
                imgs_raw.append(v)
        out.append(
            {
                "external_id": f"cls:{it.get('id')}",
                "title": (title or content)[:200],
                "body": re.sub(r"<[^>]+>", "", content),
                "url": f"https://www.cls.cn/detail/{it.get('id')}" if it.get("id") else "",
                "images": imgs.normalize_images(imgs_raw),
                "published_at": pub,
                "label": "重要" if it.get("level") == "A" else "",
                "payload": it,
            }
        )
    return out


def fetch_eastmoney_724(page_size: int = 50) -> list[dict]:
    url = (
        "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        f"?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={page_size}&req_trace=1"
    )
    r = _get(url, headers={"Referer": "https://kuaixun.eastmoney.com/"})
    data = r.json()
    out = []
    for it in ((data.get("data") or {}).get("fastNewsList")) or []:
        summary = it.get("summary") or it.get("title") or ""
        if not summary:
            continue
        ts = it.get("showTime") or it.get("time")
        pub = ts if ts else fmt_cst()
        out.append(
            {
                "external_id": f"eastmoney:{it.get('code')}",
                "title": summary[:200],
                "body": summary,
                "url": f"https://finance.eastmoney.com/a/{it.get('code')}.html" if it.get("code") else "",
                # 东财的配图在 image 字段（通常为空数组），有则取用
                "images": imgs.normalize_images(it.get("image")),
                "published_at": pub,
                "label": "",
                "payload": it,
            }
        )
    return out


def fetch_sina_roll(num: int = 50) -> list[dict]:
    url = (
        "https://feed.mix.sina.com.cn/api/roll/get"
        f"?pageid=153&lid=2516&num={num}&page=1"
    )
    r = _get(url, headers={"Referer": "https://finance.sina.com.cn/"})
    data = r.json()
    out = []
    for it in ((data.get("result") or {}).get("data")) or []:
        title = it.get("title") or ""
        if not title:
            continue
        ts = it.get("ctime")
        pub = (
            datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else fmt_cst()
        )
        out.append(
            {
                "external_id": f"sina:{it.get('docid') or it.get('oid') or title}",
                "title": title,
                "body": re.sub(r"<[^>]+>", "", it.get("intro") or ""),
                "url": it.get("url") or "",
                # 新浪的 img 是 dict（{"u":...,"w":...,"h":...}）或 str，
                # 统一交给 imgs.normalize_images 归一化
                "images": imgs.normalize_images(it.get("img")),
                "published_at": pub,
                "label": "",
                "payload": it,
            }
        )
    return out


HOT_SOURCES = {
    "weibo": ("微", fetch_weibo_hot),
    "baidu": ("百", fetch_baidu_hot),
    "douyin": ("抖", fetch_douyin_hot),
}
NEWS_SOURCES = {
    "cls": ("财", fetch_cls_telegraph),
    "eastmoney": ("东", fetch_eastmoney_724),
    "sina": ("新", fetch_sina_roll),
}
SOURCE_LABELS = {**{k: v[0] for k, v in HOT_SOURCES.items()}, **{k: v[0] for k, v in NEWS_SOURCES.items()}}


def _fingerprint(title: str, body: str) -> str:
    norm = re.sub(r"[\s\u3000]+", "", f"{title}{body or ''}")
    norm = re.sub(r"[\[\]【】()（）:：,，。.、!！?？\-—\"]", "", norm)
    return hashlib.sha1(norm[:400].encode("utf-8")).hexdigest()


def persist_raw(source: str, kind: str, items: Iterable[dict]) -> int:
    """写入原始层，返回本次新增条数。"""
    captured = fmt_cst()
    before = get_conn().execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    with tx() as conn:
        for it in items:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO raw_items
                       (source, source_kind, external_id, title, url, payload, captured_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        source,
                        kind,
                        str(it["external_id"]),
                        it["title"],
                        it.get("url"),
                        json.dumps(it.get("payload"), ensure_ascii=False)[:20000],
                        captured,
                    ),
                )
            except Exception:  # noqa: BLE001
                continue
    after = get_conn().execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    return after - before


def persist_brief(source: str, items: Iterable[dict]) -> int:
    """写入快讯层并去重，返回新增条数。

    图片统一在此归一化：无论适配器给出字符串、字典还是混合结构，
    入库前一律转成 URL 字符串列表，避免下游把 dict 当 URL 渲染。
    """
    label = SOURCE_LABELS.get(source, source[:1])
    captured = fmt_cst()
    n = 0
    with tx() as conn:
        for it in items:
            fp = _fingerprint(it["title"], it.get("body") or "")
            pub = it.get("published_at") or captured
            images = json.dumps(imgs.normalize_images(it.get("images")), ensure_ascii=False)
            important = 1 if it.get("label") == "重要" else 0
            cur = conn.execute(
                """INSERT OR IGNORE INTO brief_items
                   (source, source_label, title, body, url, images, is_important,
                    published_at, captured_at, fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    source,
                    label,
                    it["title"],
                    (it.get("body") or "")[:8000],
                    it.get("url") or "",
                    images,
                    important,
                    pub,
                    captured,
                    fp,
                ),
            )
            if cur.rowcount:
                n += 1
    return n


def persist_hot(source: str, items: Iterable[dict]) -> int:
    label = SOURCE_LABELS.get(source, source[:1])
    captured = fmt_cst()
    n = 0
    with tx() as conn:
        for it in items:
            cur = conn.execute(
                """INSERT OR IGNORE INTO hot_rows(source, title, url, rank, captured_at)
                   VALUES (?,?,?,?,?)""",
                (label, it["title"], it.get("url") or "", it.get("rank"), captured),
            )
            if cur.rowcount:
                n += 1
    return n


def run_all() -> dict:
    """跑一轮完整采集。"""
    report: dict[str, Any] = {"hot": {}, "news": {}, "errors": []}

    for src, (_label, fn) in HOT_SOURCES.items():
        try:
            items = fn()
            hot_n = persist_hot(src, items)
            brief_n = persist_brief(src, items)
            persist_raw(src, "hot", items)
            log_fetch(src, "ok", len(items), f"hot+{hot_n} brief+{brief_n}")
            report["hot"][src] = {"fetched": len(items), "new_hot": hot_n, "new_brief": brief_n}
        except Exception as e:  # noqa: BLE001
            log_fetch(src, "error", 0, str(e))
            report["errors"].append(f"{src}: {e}")
            report["hot"][src] = {"error": str(e)}

    for src, (_label, fn) in NEWS_SOURCES.items():
        try:
            items = fn()
            brief_n = persist_brief(src, items)
            persist_raw(src, "news", items)
            log_fetch(src, "ok", len(items), f"brief+{brief_n}")
            report["news"][src] = {"fetched": len(items), "new_brief": brief_n}
        except Exception as e:  # noqa: BLE001
            log_fetch(src, "error", 0, str(e))
            report["errors"].append(f"{src}: {e}")
            report["news"][src] = {"error": str(e)}

    return report


if __name__ == "__main__":
    from db import init_db, stats

    init_db()
    rep = run_all()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
