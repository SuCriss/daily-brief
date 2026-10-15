"""数据库层：SQLite 存储，与目标站同构的数据模型。

数据模型（逆向自目标站页面结构）：
  raw_items     —— 采集层原始条目（上游源的原始返回）
  brief_items   —— 每日快讯条目（时间线展示单元）
  stocks        —— A股字典（名称/代码）
  sectors       —— 板块词典
  mentions      —— 条目 → 个股/板块 的提及关系（统计层）
  hot_rows      —— 全网热搜行
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
DB_PATH = os.environ.get(
    "DAILY_BRIEF_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "brief.db"),
)

_local = threading.local()


def now_cst() -> datetime:
    return datetime.now(CST)


def fmt_cst(dt: datetime | None = None) -> str:
    return (dt or now_cst()).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,              -- weibo / baidu / douyin / cls / eastmoney / sina
    source_kind  TEXT NOT NULL,              -- hot | news
    external_id  TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    payload      TEXT,                       -- 原始 JSON
    captured_at  TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_captured ON raw_items(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_kind ON raw_items(source_kind, captured_at DESC);

CREATE TABLE IF NOT EXISTS brief_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id       INTEGER REFERENCES raw_items(id),
    source       TEXT NOT NULL,
    source_label TEXT NOT NULL,              -- 展示用简称（抖/百/微/财/东/新）
    title        TEXT NOT NULL,
    body         TEXT,                       -- 正文/摘要
    url          TEXT,
    images       TEXT,                       -- JSON 数组
    is_important INTEGER NOT NULL DEFAULT 0,
    published_at TEXT NOT NULL,              -- 事件时间
    captured_at  TEXT NOT NULL,              -- 抓取时间
    fingerprint  TEXT NOT NULL UNIQUE,       -- 去重指纹
    UNIQUE(source, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_brief_pub ON brief_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_brief_cap ON brief_items(captured_at DESC);

CREATE TABLE IF NOT EXISTS stocks (
    code   TEXT PRIMARY KEY,                 -- 600519
    name   TEXT NOT NULL,                    -- 贵州茅台
    market TEXT,                             -- sh / sz / bj
    pinyin TEXT
);
CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks(name);

CREATE TABLE IF NOT EXISTS sectors (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT DEFAULT 'concept'              -- industry | concept
);

CREATE TABLE IF NOT EXISTS mentions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id   INTEGER NOT NULL REFERENCES brief_items(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                -- stock | sector
    ref_code   TEXT NOT NULL,                -- 股票代码 或 板块名
    ref_name   TEXT NOT NULL,
    hit_count  INTEGER NOT NULL DEFAULT 1,
    UNIQUE(brief_id, kind, ref_code)
);
CREATE INDEX IF NOT EXISTS idx_mentions_ref ON mentions(kind, ref_code);

CREATE TABLE IF NOT EXISTS hot_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,               -- 抖 / 百 / 微
    title       TEXT NOT NULL,
    url         TEXT,
    rank        INTEGER,
    captured_at TEXT NOT NULL,
    UNIQUE(source, title, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_hot_cap ON hot_rows(captured_at DESC);

CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',   -- member | vip | admin
    expire_at     TEXT,                             -- NULL = 永久
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expire_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    brief_id   INTEGER NOT NULL REFERENCES brief_items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE(member_id, brief_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    keyword    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(member_id, keyword)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL,
    item_count INTEGER DEFAULT 0,
    message    TEXT,
    ran_at     TEXT NOT NULL
);
"""


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    # 兼容老库：补齐列
    for table, col, ddl in [
        ("brief_items", "images", "ALTER TABLE brief_items ADD COLUMN images TEXT"),
        ("brief_items", "is_important", "ALTER TABLE brief_items ADD COLUMN is_important INTEGER NOT NULL DEFAULT 0"),
    ]:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(ddl)
    # 只在本进程首次初始化时记录一次，避免重复调用产生噪音日志
    if not getattr(_local, "initialized", False):
        _local.initialized = True
        log_fetch("system", "ok", 0, "db initialized")


def log_fetch(source: str, status: str, count: int, message: str = "") -> None:
    get_conn().execute(
        "INSERT INTO fetch_log(source, status, item_count, message, ran_at) VALUES (?,?,?,?,?)",
        (source, status, count, message[:500], fmt_cst()),
    )


def stats() -> dict:
    conn = get_conn()
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "brief_items": q("SELECT COUNT(*) FROM brief_items"),
        "raw_items": q("SELECT COUNT(*) FROM raw_items"),
        "stocks": q("SELECT COUNT(*) FROM stocks"),
        "sectors": q("SELECT COUNT(*) FROM sectors"),
        "mentions": q("SELECT COUNT(*) FROM mentions"),
        "hot_rows": q("SELECT COUNT(*) FROM hot_rows"),
        "members": q("SELECT COUNT(*) FROM members"),
        "latest_brief": q("SELECT MAX(published_at) FROM brief_items"),
        "latest_fetch": q("SELECT MAX(ran_at) FROM fetch_log"),
    }
