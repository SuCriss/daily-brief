"""命令行工具：采集、抽取、建号、查统计。"""
from __future__ import annotations

import argparse
import getpass
import json

import app as web
import db
import extract
import sources


def cmd_init(_args) -> None:
    db.init_db()
    print("DB 初始化完成:", db.DB_PATH)


def cmd_dict(_args) -> None:
    db.init_db()
    print("stocks:", extract.load_stock_dict(force=True))
    print("sectors:", extract.load_sector_dict())


def cmd_fetch(_args) -> None:
    db.init_db()
    print(json.dumps(sources.run_all(), ensure_ascii=False, indent=2))


def cmd_extract(_args) -> None:
    db.init_db()
    print(json.dumps(extract.run_extraction(), ensure_ascii=False))


def cmd_pipeline(_args) -> None:
    """完整跑一轮：采集 → 抽取。"""
    db.init_db()
    extract.load_stock_dict()
    extract.load_sector_dict()
    print("fetch:", json.dumps(sources.run_all(), ensure_ascii=False))
    print("extract:", json.dumps(extract.run_extraction(), ensure_ascii=False))
    print("stats:", json.dumps(db.stats(), ensure_ascii=False, indent=2))


def cmd_stats(_args) -> None:
    db.init_db()
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))


def cmd_ranking(args) -> None:
    db.init_db()
    if args.kind == "stock":
        rows = extract.stock_ranking(hours=args.hours, limit=args.limit)
    else:
        rows = extract.sector_ranking(hours=args.hours, limit=args.limit)
    for r in rows:
        key = r.get("name")
        print(f"{r['rank']:>3}  {key:<14} 提及={r['mentions']:<3} 趋势={r['trend']:<5} 最近={r['last_at']}")


def cmd_adduser(args) -> None:
    db.init_db()
    pwd = args.password or getpass.getpass("请输入密码: ")
    mid = web.create_member(args.username, pwd, role=args.role, days=args.days)
    print(f"已创建用户 id={mid} username={args.username} role={args.role} days={args.days}")


def cmd_serve(args) -> None:
    import uvicorn

    db.init_db()
    uvicorn.run(web.app, host=args.host, port=args.port)


def main() -> None:
    p = argparse.ArgumentParser(prog="cli", description="每日快讯系统命令行")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库").set_defaults(func=cmd_init)
    sub.add_parser("dict", help="加载股票与板块字典").set_defaults(func=cmd_dict)
    sub.add_parser("fetch", help="跑一轮采集").set_defaults(func=cmd_fetch)
    sub.add_parser("extract", help="跑一轮抽取").set_defaults(func=cmd_extract)
    sub.add_parser("pipeline", help="采集+抽取完整一轮").set_defaults(func=cmd_pipeline)
    sub.add_parser("stats", help="查看统计").set_defaults(func=cmd_stats)

    pr = sub.add_parser("ranking", help="查看热度排名")
    pr.add_argument("kind", choices=["stock", "sector"])
    pr.add_argument("--hours", type=int, default=24)
    pr.add_argument("--limit", type=int, default=20)
    pr.set_defaults(func=cmd_ranking)

    pa = sub.add_parser("adduser", help="创建账号")
    pa.add_argument("username")
    pa.add_argument("--password", default="")
    pa.add_argument("--role", default="vip", choices=["member", "vip", "admin"])
    pa.add_argument("--days", type=int, default=365, help="会员有效天数，0 表示永久")
    pa.set_defaults(func=cmd_adduser)

    ps = sub.add_parser("serve", help="启动 Web 服务")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8848)
    ps.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
