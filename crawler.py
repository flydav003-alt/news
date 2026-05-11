"""
crawler.py — 新聞抓取模組
確定採用 5 個 RSS 來源：
  1. Yahoo Finance   (美股主力)
  2. Reuters         (財經 + 地緣政治)
  3. CNBC            (科技財經)
  4. 鉅亨網          (台股中文)
  5. BBC World       (戰爭 / 地緣政治)
全部走 RSS，穩定免費不會被擋。
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
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/html,*/*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
TIMEOUT    = 15
MAX_ITEMS  = 30   # 每個來源最多抓幾則


# ── 5 個確定來源 ──────────────────────────────────────────────────────────────
SOURCES = [
    {
        "name":     "Yahoo Finance",
        "url":      "https://finance.yahoo.com/news/rssindex",
        "language": "en",
        "category": "財經",
        "enabled":  True,
    },
    {
        "name":     "Reuters Business",
        "url":      "https://feeds.reuters.com/reuters/businessNews",
        "language": "en",
        "category": "財經",
        "enabled":  True,
    },
    {
        "name":     "Reuters World",
        "url":      "https://feeds.reuters.com/Reuters/worldNews",
        "language": "en",
        "category": "地緣政治",
        "enabled":  True,
    },
    {
        "name":     "CNBC Top News",
        "url":      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "language": "en",
        "category": "財經",
        "enabled":  True,
    },
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
        "name":     "BBC World News",
        "url":      "https://feeds.bbci.co.uk/news/world/rss.xml",
        "language": "en",
        "category": "地緣政治",
        "enabled":  True,
    },
    {
        "name":     "BBC Business",
        "url":      "https://feeds.bbci.co.uk/news/business/rss.xml",
        "language": "en",
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

        # bozo=True 表示 RSS 格式有問題，但還是可能有部分內容
        if feed.bozo and not feed.entries:
            logger.warning(f"[{source['name']}] RSS 格式異常：{feed.bozo_exception}")
            return []

        for entry in feed.entries[:MAX_ITEMS]:
            title   = _clean(getattr(entry, "title",   ""))
            summary = _clean(getattr(entry, "summary", ""))
            url     = getattr(entry, "link", "")

            if not title or len(title) < 5:
                continue

            results.append({
                "title":       title,
                "summary":     summary[:500],
                "url":         url,
                "source":      source["name"],
                "language":    source["language"],
                "category":    source.get("category", "財經"),
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
        # 判斷是否啟用
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
        time.sleep(0.8)   # 禮貌延遲，避免被擋

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
