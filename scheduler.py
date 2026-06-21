"""
scheduler.py — 背景排程模組
- APScheduler 定時抓取
- 整合 Groq AI 選擇性分析（符合觸發條件才送）
- 所有時間顯示台灣時間
- [修改] should_use_ai 呼叫新增 has_tickers、category 參數
- [修改] _get_groq_key 加環境變數備援，排程器固定帶 use_ai=True
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from analyzer import Analyzer
from crawler import run_crawl
from database import SessionLocal, save_article, log_crawl
from groq_analyzer import should_use_ai, groq_analyze, _clear_ai_fields, _is_rate_limited

logger    = logging.getLogger(__name__)
_sched    = None
_analyzer = Analyzer()
TZ_TW     = timezone(timedelta(hours=8))


def _get_groq_key() -> str:
    """
    取得 Groq API Key。
    優先從 st.secrets 讀（Streamlit Cloud 正常情況）；
    若 Streamlit context 不存在（排程背景執行時），
    退而從環境變數讀（Streamlit Cloud 會把 secrets 注入成環境變數）。
    """
    # 第一優先：st.secrets
    try:
        import streamlit as st
        key = st.secrets.get("GROQ_API_KEY", "")
        if key:
            return key
    except Exception:
        pass

    # 備援：環境變數（排程器背景執行、或 Streamlit reboot 後 session 遺失時）
    return os.environ.get("GROQ_API_KEY", "")


def crawl_and_save(enabled_names=None,
                   custom_bull=None,
                   custom_bear=None,
                   use_ai: bool = True,
                   max_ai_per_run: int = 10) -> dict:
    """
    完整流程：抓取 → 關鍵字分析 → AI 分析（選擇性）→ 存庫

    max_ai_per_run：每次執行最多送幾則給 Groq（避免單次跑太久 & 耗盡 quota）
    預設 10 則（每則 ~2 秒 = 共 ~20 秒），為每日總結保留足夠 RPM 餘裕。
    Groq 免費版 RPM=30，10 則文章用掉 ~10 RPM，總結再用 1 RPM，共 11/30，安全。
    若需要更多分析可手動傳入 max_ai_per_run=20，但需確認當下 quota 未被耗盡。
    """
    analyzer = Analyzer(
        extra_bullish=custom_bull,
        extra_bearish=custom_bear,
    ) if (custom_bull or custom_bear) else _analyzer

    t0 = datetime.now()
    articles, logs = run_crawl(enabled_names)

    saved = skipped = ai_count = 0
    groq_key = _get_groq_key() if use_ai else ""

    if use_ai:
        if groq_key:
            logger.info("Groq key 取得成功，AI 分析已啟用")
        else:
            logger.warning("use_ai=True 但 Groq key 為空，AI 分析將跳過")

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

            # ── Step 2：AI 分析（條件觸發 + 每批上限 + 熔斷檢查）──
            has_tickers = bool(r.tickers)
            can_use_ai  = (
                groq_key
                and not _is_rate_limited()             # 熔斷中直接跳過整批
                and ai_count < max_ai_per_run          # 每批上限
                and should_use_ai(
                    r.sentiment_score, item["title"], r.is_geo, has_tickers,
                    item.get("category", ""),          # [修改] 傳入 category 供非財經過濾
                )
            )
            if can_use_ai:
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
                    # 429 速率限制時 groq_analyze 內部已 sleep(5)，這裡不再額外等待
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

        logger.info(
            f"完成：抓取 {len(articles)} 則，新增 {saved}，跳過 {skipped}，AI 分析 {ai_count} 則"
        )

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
        trigger            = IntervalTrigger(minutes=interval_minutes),
        id                 = "news_crawl",
        replace_existing   = True,
        misfire_grace_time = 120,
        kwargs             = {"use_ai": True},   # 排程永遠帶 AI，不依賴 session_state
    )
    _sched.start()
    logger.info(f"排程啟動，每 {interval_minutes} 分鐘抓取一次（AI 分析已強制開啟）")


def update_interval(minutes: int) -> None:
    global _sched
    if _sched and _sched.running:
        _sched.reschedule_job(
            "news_crawl",
            trigger = IntervalTrigger(minutes=minutes),
            kwargs  = {"use_ai": True},   # reschedule 也保留 AI 參數
        )


def next_run_time() -> str:
    global _sched
    if _sched and _sched.running:
        job = _sched.get_job("news_crawl")
        if job and job.next_run_time:
            tw = job.next_run_time.astimezone(TZ_TW)
            return tw.strftime("%H:%M:%S")
    return "—"
