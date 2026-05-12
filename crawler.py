"""
crawler.py — 新聞抓取模組（全中文來源版）
來源（全部 RSS，穩定免費）：
  1. 鉅亨網-台股        (台股主力)
  2. 鉅亨網-美股        (美股中文)
  3. 鉅亨網-總覽        (綜合財經)
  4. MoneyDJ-台股       (台股深度)
  5. MoneyDJ-國際       (國際財經)
  6. Yahoo奇摩股市      (台股綜合)
  7. 經濟日報           (財經新聞)
  8. 工商時報           (產業新聞)
  9. 聯合報財經         (財經綜合)
 10. 科技新報           (科技產業)
"""

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    import feedparser
    FEEDPARSER_OK = True
except ImportError:
    FEEDPARSER_OK = False

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/html,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.3",
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT   = 20
MAX_ITEMS = 30


# ── 中文來源清單 ──────────────────────────────────────────────────────────────
SOURCES = [
    # ── 鉅亨網 ────────────────────────────────────────────────
    {
        "name":     "鉅亨網-台股",
        "url":      "https://feeds.feedburner.com/cnyes-tw-stock",
        "language": "zh",
        "category": "台股",
        "enabled":  True,
    },
    {
        "name":     "鉅亨網-美股",
        "url":      "https://feeds.feedburner.com/cnyes-us-stock",
        "language": "zh",
        "category": "美股",
        "enabled":  True,
    },
    {
        "name":     "鉅亨網-總覽",
        "url":      "https://feeds.feedburner.com/cnyes",
        "language": "zh",
        "category": "財經",
        "enabled":  True,
    },
    # ── MoneyDJ ───────────────────────────────────────────────
    {
        "name":     "MoneyDJ-台股",
        "url":      "https://www.moneydj.com/KMDJ/RssCenter/RssCenter.djrss?type=2",
        "language": "zh",
        "category": "台股",
        "enabled":  True,
    },
    {
        "name":     "MoneyDJ-國際",
        "url":      "https://www.moneydj.com/KMDJ/RssCenter/RssCenter.djrss?type=3",
        "language": "zh",
        "category": "國際財經",
        "enabled":  True,
    },
    # ── Yahoo 台灣 ────────────────────────────────────────────
    {
        "name":     "Yahoo奇摩股市",
        "url":      "https://tw.news.yahoo.com/rss/finance",
        "language": "zh",
        "category": "財經",
        "enabled":  True,
    },
    # ── 報紙類 ────────────────────────────────────────────────
    {
        "name":     "經濟日報",
        "url":      "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
        "language": "zh",
        "category": "財經",
        "enabled":  True,
    },
    {
        "name":     "工商時報",
        "url":      "https://www.ctee.com.tw/feed",
        "language": "zh",
        "category": "產業",
        "enabled":  True,
    },
    {
        "name":     "科技新報",
        "url":      "https://technews.tw/feed/",
        "language": "zh",
        "category": "科技",
        "enabled":  True,
    },
    {
        "name":     "聯合報財經",
        "url":      "https://udn.com/rssfeed/news/2/6641?ch=news",
        "language": "zh",
        "category": "財經",
        "enabled":  True,
    },
]


# ── RSS 抓取核心 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    """抓取單一 RSS 來源，回傳標準化文章 list"""
    if not FEEDPARSER_OK:
        logger.warning("feedparser 未安裝")
        return []

    results = []
    try:
        feed = feedparser.parse(
            source["url"],
            request_headers=HEADERS,
            agent=HEADERS["User-Agent"],
        )

        if feed.bozo and not feed.entries:
            logger.warning(f"[{source['name']}] RSS 格式異常：{feed.bozo_exception}")
            return []

        for entry in feed.entries[:MAX_ITEMS]:
            title   = _clean(getattr(entry, "title",   ""))
            summary = _clean(getattr(entry, "summary", ""))
            url     = getattr(entry, "link", "")

            if not title or len(title) < 4:
                continue

            results.append({
                "title":        title,
                "summary":      summary[:600],
                "url":          url,
                "source":       source["name"],
                "language":     source["language"],
                "category":     source.get("category", "財經"),
                "published_at": _parse_time(entry),
            })

        logger.info(f"[{source['name']}] 抓到 {len(results)} 則")

    except Exception as e:
        logger.error(f"[{source['name']}] 失敗：{e}")

    return results


# ── 主執行入口 ────────────────────────────────────────────────────────────────
def run_crawl(enabled_names: Optional[list[str]] = None) -> tuple[list[dict], list[dict]]:
    """
    執行全部來源抓取。
    enabled_names：None 表示依 source["enabled"] 決定；
                   傳入名稱 list 可讓設定頁動態控制。
    回傳 (articles, logs)
    """
    all_articles, logs = [], []

    for src in SOURCES:
        if enabled_names is not None:
            if src["name"] not in enabled_names:
                continue
        elif not src["enabled"]:
            continue

        articles = fetch_rss(src)
        logs.append({
            "source":  src["name"],
            "status":  "success" if articles else "empty",
            "count":   len(articles),
        })
        all_articles.extend(articles)
        time.sleep(0.5)

    return all_articles, logs


# ── 工具函式 ──────────────────────────────────────────────────────────────────
def _clean(text: str) -> str:
    import html as html_mod
    text = html_mod.unescape(text or "")
    text = BeautifulSoup(text, "lxml").get_text(separator=" ")
    return " ".join(text.split())


def _parse_time(entry) -> datetime:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
    if raw:
        try:
            return parsedate_to_datetime(raw).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)
