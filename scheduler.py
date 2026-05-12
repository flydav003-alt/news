"""
scheduler.py — 背景排程模組
- APScheduler 定時抓取
- 整合 Groq AI 選擇性分析（符合觸發條件才送）
- 所有時間顯示台灣時間
"""

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import streamlit as st

from analyzer import Analyzer
from crawler import run_crawl
from database import SessionLocal, save_article, log_crawl
from groq_analyzer import should_use_ai, groq_analyze, _clear_ai_fields

logger    = logging.getLogger(__name__)
_sched    = None
_analyzer = Analyzer()
TZ_TW     = timezone(timedelta(hours=8))


def _get_groq_key() -> str:
    try:
        return st.secrets.get("GROQ_API_KEY", "")
    except Exception:
        return ""


def crawl_and_save(enabled_names=None,
                   custom_bull=None,
                   custom_bear=None,
                   use_ai: bool = True) -> dict:
    """
    完整流程：抓取 → 關鍵字分析 → AI 分析（選擇性）→ 存庫
    """
    analyzer = Analyzer(
        extra_bullish=custom_bull,
        extra_bearish=custom_bear,
    ) if (custom_bull or custom_bear) else _analyzer

    t0 = datetime.now()
    articles, logs = run_crawl(enabled_names)

    saved = skipped = ai_count = 0
    groq_key = _get_groq_key() if use_ai else ""

    db = SessionLocal()
    try:
        for item in articles:
            # ── Step 1：關鍵字分析 ─────────────────────────────
            r = analyzer.analyze(
                item["title"],
                item.get("summary", ""),
                item.get("language", "zh"),
            )
            item.update({
                "sentiment":       r.sentiment,
                "sentiment_score": r.sentiment_score,
                "sentiment_label": r.sentiment_label,
                "tickers":         r.tickers,
                "ticker_details":  r.ticker_details,
                "sectors":         r.sectors,
                "is_geo":          r.is_geo,
            })

            # ── Step 2：AI 分析（條件觸發）────────────────────
            if groq_key and should_use_ai(
                r.sentiment_score, item["title"], r.is_geo
            ):
                ai = groq_analyze(
                    title    = item["title"],
                    summary  = item.get("summary", ""),
                    category = item.get("category", "財經"),
                    api_key  = groq_key,
                )
                if ai:
                    item["ai_sentiment"]        = ai["sentiment"]
                    item["ai_score"]            = ai["score"]
                    item["ai_summary"]          = ai["summary"]
                    item["ai_affected_tickers"] = ",".join(
                        ai.get("affected_tickers", []))
                    item["ai_reason"]           = ai["reason"]
                    item["ai_confidence"]       = ai["confidence"]
                    ai_count += 1
                else:
                    _clear_ai_fields(item)
            else:
                _clear_ai_fields(item)

            # ── Step 3：存庫 ───────────────────────────────────
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
    tw_time = datetime.now(TZ_TW).strftime("%H:%M:%S")

    return {
        "total":    len(articles),
        "saved":    saved,
        "skipped":  skipped,
        "ai_count": ai_count,
        "elapsed":  elapsed,
        "time":     tw_time,
        "logs":     logs,
    }


def start_scheduler(interval_minutes: int = 30) -> None:
    global _sched
    if _sched and _sched.running:
        return
    _sched = BackgroundScheduler(timezone="Asia/Taipei")
    _sched.add_job(
        crawl_and_save,
        trigger          = IntervalTrigger(minutes=interval_minutes),
        id               = "news_crawl",
        replace_existing = True,
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
            tw = job.next_run_time.astimezone(TZ_TW)
            return tw.strftime("%H:%M:%S")
    return "—"
