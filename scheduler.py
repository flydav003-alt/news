"""
scheduler.py — 背景排程模組
使用 APScheduler 定時執行抓取 + 分析 + 存庫。
Streamlit 多次 re-run 時用全域變數防止重複啟動。
"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from analyzer import Analyzer
from crawler import run_crawl
from database import SessionLocal, save_article, log_crawl

logger   = logging.getLogger(__name__)
_sched   = None
_analyzer = Analyzer()


def crawl_and_save(enabled_names=None) -> dict:
    """一次完整的抓取 → 分析 → 存庫流程，回傳摘要供 UI 顯示"""
    t0 = datetime.now()
    articles, logs = run_crawl(enabled_names)

    saved = skipped = 0
    db = SessionLocal()
    try:
        for item in articles:
            r = _analyzer.analyze(
                item["title"],
                item.get("summary", ""),
                item.get("language", "en"),
            )
            item.update({
                "sentiment":       r.sentiment,
                "sentiment_score": r.sentiment_score,
                "sentiment_label": r.sentiment_label,
                "tickers":         r.tickers,
                "sectors":         r.sectors,
                "is_geo":          r.is_geo,
            })
            if save_article(db, item):
                saved += 1
            else:
                skipped += 1

        for lg in logs:
            log_crawl(db,
                      source    = lg["source"],
                      status    = lg["status"],
                      count     = lg["count"],
                      new_saved = saved,
                      skipped   = skipped)
    except Exception as e:
        logger.error(f"存庫失敗：{e}")
    finally:
        db.close()

    elapsed = (datetime.now() - t0).seconds
    return {
        "total":   len(articles),
        "saved":   saved,
        "skipped": skipped,
        "elapsed": elapsed,
        "time":    datetime.now().strftime("%H:%M:%S"),
    }


def start_scheduler(interval_minutes: int = 30) -> None:
    """啟動背景排程（只啟動一次）"""
    global _sched
    if _sched and _sched.running:
        return
    _sched = BackgroundScheduler(timezone="Asia/Taipei")
    _sched.add_job(
        crawl_and_save,
        trigger         = IntervalTrigger(minutes=interval_minutes),
        id              = "news_crawl",
        replace_existing= True,
        misfire_grace_time = 120,
    )
    _sched.start()
    logger.info(f"排程啟動，每 {interval_minutes} 分鐘抓取一次")


def update_interval(minutes: int) -> None:
    global _sched
    if _sched and _sched.running:
        _sched.reschedule_job(
            "news_crawl",
            trigger=IntervalTrigger(minutes=minutes),
        )


def next_run_time() -> str:
    global _sched
    if _sched and _sched.running:
        job = _sched.get_job("news_crawl")
        if job and job.next_run_time:
            return job.next_run_time.strftime("%H:%M:%S")
    return "—"
