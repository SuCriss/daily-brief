"""定时调度：按目标站的节奏周期采集。

目标站节奏（逆向观察）：
  热搜：每 2 分钟同步一次
  快讯：高频轮询（财联社/东财 7×24 为分钟级）
  抽取与统计：每次采集后重算
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

import db
import extract
import sources
from db import fmt_cst

try:  # AI 层为可选增强：导入失败不影响采集主链路
    import ai
except Exception:  # noqa: BLE001
    ai = None  # type: ignore[assignment]

HOT_INTERVAL = int(os.environ.get("HOT_INTERVAL", "120"))      # 热搜 2 分钟
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "60"))     # 快讯 1 分钟
EXTRACT_EVERY = int(os.environ.get("EXTRACT_EVERY", "3"))      # 每 3 轮做一次抽取

_running = True


def _stop(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    print(f"[{fmt_cst()}] 收到停止信号，准备退出…", flush=True)


def cycle_once(do_extract: bool = True) -> dict:
    report = sources.run_all()
    ex = extract.run_extraction() if do_extract else {"scanned": 0, "mentions": 0}
    ai_rep = {"skipped": True, "reason": "disabled"}
    if ai.ai_available() and do_extract:
        try:
            ai_rep = ai.classify_pending()
        except Exception as e:  # noqa: BLE001
            ai_rep = {"ok": False, "error": str(e)}
    return {"report": report, "extract": ex, "ai": ai_rep, "stats": db.stats()}


def _install_signal_handlers() -> None:
    """仅在主线程安装信号处理器；子线程调用时静默跳过。"""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    db.init_db()
    _install_signal_handlers()

    print(f"[{fmt_cst()}] 采集调度启动：热搜 {HOT_INTERVAL}s / 快讯 {NEWS_INTERVAL}s", flush=True)
    print(f"[{fmt_cst()}] 加载股票字典…", flush=True)
    print(f"[{fmt_cst()}] stocks={extract.load_stock_dict()} sectors={extract.load_sector_dict()}", flush=True)

    round_no = 0
    last_news = 0.0
    while _running:
        round_no += 1
        try:
            now = time.time()
            do_news = (now - last_news) >= NEWS_INTERVAL
            if do_news:
                last_news = now
                report = sources.run_all()
            else:
                # 只跑热搜
                report = {"hot": {}, "news": {}, "errors": []}
                for src, (_l, fn) in sources.HOT_SOURCES.items():
                    try:
                        items = fn()
                        report["hot"][src] = {
                            "fetched": len(items),
                            "new_hot": sources.persist_hot(src, items),
                            "new_brief": sources.persist_brief(src, items),
                        }
                        sources.persist_raw(src, "hot", items)
                    except Exception as e:  # noqa: BLE001
                        report["errors"].append(f"{src}: {e}")

            if round_no % EXTRACT_EVERY == 0:
                ex = extract.run_extraction()
            else:
                ex = {"scanned": 0, "mentions": 0}

            ai_rep = {"skipped": True, "reason": "disabled"}
            if ai is not None and ai.ai_available():
                try:
                    ai_rep = ai.classify_pending()
                except Exception as e:  # noqa: BLE001
                    ai_rep = {"ok": False, "error": str(e)}
                try:
                    ai.generate_daily_review_if_due()
                except Exception as e:  # noqa: BLE001
                    db.log_fetch("daily_review", "error", 0, str(e))

            st = db.stats()
            print(
                f"[{fmt_cst()}] round={round_no} brief={st['brief_items']} "
                f"mentions={st['mentions']} extract={ex['scanned']} "
                f"ai_done={ai_rep.get('done', 0)} err={len(report['errors'])}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            db.log_fetch("scheduler", "error", 0, str(e))
            print(f"[{fmt_cst()}] 调度异常: {e}", flush=True)

        # 分片睡眠，便于快速响应停止信号
        slept = 0.0
        while _running and slept < HOT_INTERVAL:
            time.sleep(min(1.0, HOT_INTERVAL - slept))
            slept += 1.0

    print(f"[{fmt_cst()}] 调度已退出。", flush=True)


if __name__ == "__main__":
    if "--once" in sys.argv:
        db.init_db()
        extract.load_stock_dict()
        extract.load_sector_dict()
        print(json.dumps(cycle_once(), ensure_ascii=False, indent=2)[:2000])
    else:
        main()
