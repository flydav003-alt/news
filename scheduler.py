"""
scheduler.py — 抓取流程模組 v3

重大改動：
  - 不再用 APScheduler 在 Streamlit 裡跑背景排程（Streamlit Cloud 休眠就停，資料庫又放 /tmp 會清空）
  - 改由 GitHub Actions 定時執行本檔：  python scheduler.py data_out/news.json
    流程：載入舊 news.json → 抓取 → 關鍵字分析 → AI 分析（挑最重要的前 N 則）
          → 英文標題翻中文 → 補縮圖 → 存庫 → 匯出 news.json
  - Streamlit 端只讀 news.json；手動「立即抓取」仍可呼叫 crawl_and_save()
  - 修正：以前已存在的舊文章也會被送去 AI（浪費 quota），現在只處理「新文章」
  - 修正：AI 名額改成「挑分數/個股/地緣最重要的」而不是照 RSS 順序先到先用
"""

import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from analyzer import Analyzer
from crawler import run_crawl
from database import (SessionLocal, init_db, save_article, log_crawl,
                      known_urls, export_json, import_json)
from groq_analyzer import should_use_ai, groq_analyze, _clear_ai_fields, _is_rate_limited

logger    = logging.getLogger(__name__)
_analyzer = Analyzer()
TZ_TW     = timezone(timedelta(hours=8))

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _get_groq_key() -> str:
    """st.secrets（Streamlit 內）→ 環境變數（GitHub Actions / 排程背景）"""
    try:
        import streamlit as st
        key = st.secrets.get("GROQ_API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("GROQ_API_KEY", "")


# ── 英文標題 → 繁體中文 ───────────────────────────────────────────────────────
def _needs_translation(title: str) -> bool:
    letters = sum(c.isascii() and c.isalpha() for c in title)
    return len(_CJK_RE.findall(title)) < 2 and letters >= 10


def _translate_one(title: str) -> str:
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="zh-TW").translate(title) or ""
    except Exception as e:
        logger.debug(f"翻譯失敗：{e}")
        return ""


def translate_titles(items: list[dict], limit: int = 150) -> int:
    """替英文標題加上 title_zh。失敗就留空（畫面會退回顯示原文）。回傳成功數。"""
    todo = [it for it in items if _needs_translation(it["title"])][:limit]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda it: _translate_one(it["title"]), todo))
    n = 0
    for it, zh in zip(todo, results):
        if zh and zh != it["title"]:
            it["title_zh"] = zh
            n += 1
    logger.info(f"標題翻譯 {n}/{len(todo)} 則")
    return n


# ── 主流程 ────────────────────────────────────────────────────────────────────
def crawl_and_save(enabled_names=None,
                   custom_bull=None,
                   custom_bear=None,
                   use_ai: bool = True,
                   max_ai_per_run: int = 10,
                   fetch_og: bool = False,
                   translate: bool = True) -> dict:
    """
    抓取 → 去除已存在 → 關鍵字分析 → AI 分析（挑最重要的 N 則）→ 翻譯 → 存庫

    max_ai_per_run：每次最多送幾則給 Groq。GitHub Actions 每小時一次，預設 8 則較安全，
                    約 17 次/天 × 8 ≈ 136 次，落在 Groq 免費額度內。
    fetch_og：缺圖時補抓 og:image（較慢，僅 Actions 開）
    """
    analyzer = Analyzer(
        extra_bullish=custom_bull,
        extra_bearish=custom_bear,
    ) if (custom_bull or custom_bear) else _analyzer

    t0 = datetime.now()
    db = SessionLocal()
    try:
        known = known_urls(db)
    finally:
        db.close()

    articles, logs = run_crawl(enabled_names, fetch_og=fetch_og, known_urls=known)

    # 只處理「新」文章（網址不在庫、且同批內不重複）
    seen, fresh = set(), []
    for a in articles:
        u = a.get("url", "")
        if u in known or u in seen:
            continue
        seen.add(u)
        fresh.append(a)

    saved = skipped = ai_count = 0
    groq_key = _get_groq_key() if use_ai else ""

    # ── Step 1：關鍵字分析（全部新文章）────────────────────────
    for item in fresh:
        r = analyzer.analyze(item["title"], item.get("summary", ""),
                             item.get("language", "zh"))
        item.update({
            "sentiment":       r.sentiment,
            "sentiment_score": r.sentiment_score,
            "sentiment_label": r.sentiment_label,
            "tickers":         r.tickers,
            "ticker_details":  r.ticker_details,
            "sectors":         r.sectors,
            "is_geo":          r.is_geo,
        })
        _clear_ai_fields(item)
        item.setdefault("title_zh", "")

    # ── Step 2：AI 分析（挑最重要的前 N 則）────────────────────
    if use_ai and groq_key and not _is_rate_limited():
        cands = [
            it for it in fresh
            if should_use_ai(it["sentiment_score"], it["title"], it["is_geo"],
                             bool(it["tickers"]), it.get("category", ""))
        ]
        # 優先序：地緣 > 有個股 > 情緒強度；同分者較新的優先
        cands.sort(key=lambda it: (it["is_geo"], bool(it["tickers"]),
                                   abs(it["sentiment_score"]),
                                   it["published_at"].timestamp()), reverse=True)
        for it in cands[:max_ai_per_run]:
            if _is_rate_limited():
                break
            ai = groq_analyze(it["title"], it.get("summary", ""),
                              it.get("category", "財經"), groq_key)
            if ai:
                it["ai_sentiment"]        = ai["sentiment"]
                it["ai_score"]            = ai["score"]
                it["ai_summary"]          = ai["summary"]
                it["ai_affected_tickers"] = ",".join(ai.get("affected_tickers", []))
                it["ai_reason"]           = ai["reason"]
                it["ai_confidence"]       = ai["confidence"]
                ai_count += 1
    elif use_ai and not groq_key:
        logger.warning("use_ai=True 但 Groq key 為空，AI 分析將跳過")

    # ── Step 3：英文標題翻譯 ───────────────────────────────────
    tr_count = translate_titles(fresh) if translate else 0

    # ── Step 4：存庫 ───────────────────────────────────────────
    db = SessionLocal()
    try:
        for item in fresh:
            if save_article(db, item):
                saved += 1
            else:
                skipped += 1
        for lg in logs:
            log_crawl(db, source=lg["source"], status=lg["status"],
                      count=lg["count"], new_saved=0, skipped=0)
        logger.info(f"完成：抓取 {len(articles)}、新文章 {len(fresh)}、新增 {saved}、"
                    f"去重 {skipped}、AI {ai_count}、翻譯 {tr_count}")
    except Exception as e:
        logger.error(f"存庫失敗：{e}")
    finally:
        db.close()

    return {
        "total":    len(articles),
        "saved":    saved,
        "skipped":  skipped + (len(articles) - len(fresh)),
        "ai_count": ai_count,
        "translated": tr_count,
        "elapsed":  (datetime.now() - t0).seconds,
        "time":     datetime.now(TZ_TW).strftime("%H:%M:%S"),
        "logs":     logs,
    }


# ── GitHub Actions 入口 ───────────────────────────────────────────────────────
def run_job(json_path: str, max_ai: int = 8) -> None:
    init_db()

    # 1) 載入上次的 news.json，保留歷史並供去重
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            db = SessionLocal()
            try:
                n = import_json(db, payload)
            finally:
                db.close()
            logger.info(f"載入舊資料 {n} 則")
        except Exception as e:
            logger.error(f"讀取舊 news.json 失敗（當作空資料）：{e}")

    # 2) 抓取 + 分析 + 翻譯 + 補圖
    result = crawl_and_save(use_ai=True, max_ai_per_run=max_ai,
                            fetch_og=True, translate=True)
    logger.info(f"本次結果：{ {k: v for k, v in result.items() if k != 'logs'} }")
    for lg in result["logs"]:
        logger.info(f"  來源 {lg['source']}: {lg['status']} ({lg['count']})")

    # 3) 匯出（保留 72 小時）
    db = SessionLocal()
    try:
        total = export_json(db, json_path, keep_hours=72)
    finally:
        db.close()
    logger.info(f"已輸出 {json_path}（{total} 則）")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    out = sys.argv[1] if len(sys.argv) > 1 else "data_out/news.json"
    run_job(out, max_ai=int(os.environ.get("MAX_AI", "8")))
