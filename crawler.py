"""
crawler.py — 新聞抓取模組（全中文來源版）
來源：
  RSS 類（feedparser）：
    1. 科技新報           (科技產業)      ✅ 穩定
    2. 經濟日報           (財經新聞)      ✅ 穩定
    3. Yahoo奇摩股市      (台股綜合)      ✅ 穩定
    4. 工商時報           (產業新聞)      🔧 修正 URL
    5. 聯合報財經         (財經綜合)      🔧 修正 URL
    6. MoneyDJ-台股       (台股深度)      🔧 加 Referer header
    7. MoneyDJ-國際       (國際財經)      🔧 加 Referer header

  JSON API 類（requests）：
    8. 鉅亨網-台股        (台股主力)      🔧 改用官方 JSON API
    9. 鉅亨網-美股        (美股中文)      🔧 改用官方 JSON API
   10. 鉅亨網-財經        (綜合財經)      🔧 改用官方 JSON API

修改說明：
  - 鉅亨網：feedburner 已停服，改用 news.cnyes.com JSON API
  - MoneyDJ：feedparser 直打會被擋，改用 requests + Referer header 手動解析
  - 工商時報：移除 www，改 https://ctee.com.tw/?feed=rss2
  - 聯合報財經：更新至可用的 channel id 5590
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

# MoneyDJ 需要 Referer 才不被擋
HEADERS_MONEYDJ = {
    **HEADERS,
    "Referer": "https://www.moneydj.com/",
    "Host": "www.moneydj.com",
}

TIMEOUT   = 20
MAX_ITEMS = 30


# ── 來源清單 ──────────────────────────────────────────────────────────────────
# fetch_type:
#   "rss"      → feedparser 直接解析
#   "rss_req"  → requests 抓原始 XML 再用 feedparser 解析（可帶自訂 header）
#   "cnyes"    → 鉅亨網 JSON API
SOURCES = [
    # ── 穩定 RSS ──────────────────────────────────────────────
    {
        "name":       "科技新報",
        "url":        "https://technews.tw/feed/",
        "language":   "zh",
        "category":   "科技",
        "enabled":    True,
        "fetch_type": "rss",
    },
    {
        "name":       "經濟日報",
        "url":        "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
        "language":   "zh",
        "category":   "財經",
        "enabled":    True,
        "fetch_type": "rss",
    },
    {
        "name":       "Yahoo奇摩股市",
        "url":        "https://tw.news.yahoo.com/rss/finance",
        "language":   "zh",
        "category":   "財經",
        "enabled":    True,
        "fetch_type": "rss",
    },
    # ── 修正 URL 的 RSS ───────────────────────────────────────
    {
        "name":       "工商時報",
        "url":        "https://ctee.com.tw/?feed=rss2",          # 移除 www，改標準 WP feed
        "language":   "zh",
        "category":   "產業",
        "enabled":    True,
        "fetch_type": "rss",
    },
    {
        "name":       "聯合報財經",
        "url":        "https://money.udn.com/rssfeed/news/1001/5590?ch=money",  # 修正 channel
        "language":   "zh",
        "category":   "財經",
        "enabled":    True,
        "fetch_type": "rss",
    },
    # ── MoneyDJ（需帶 Referer）────────────────────────────────
    {
        "name":       "MoneyDJ-台股",
        "url":        "https://www.moneydj.com/KMDJ/RssCenter/RssCenter.djrss?type=2",
        "language":   "zh",
        "category":   "台股",
        "enabled":    True,
        "fetch_type": "rss_req",
        "headers":    HEADERS_MONEYDJ,
    },
    {
        "name":       "MoneyDJ-國際",
        "url":        "https://www.moneydj.com/KMDJ/RssCenter/RssCenter.djrss?type=3",
        "language":   "zh",
        "category":   "國際財經",
        "enabled":    True,
        "fetch_type": "rss_req",
        "headers":    HEADERS_MONEYDJ,
    },
    # ── 鉅亨網 JSON API ───────────────────────────────────────
    {
        "name":       "鉅亨網-台股",
        "url":        "https://news.cnyes.com/api/v3/news/category/tw_stock?limit=30&startAt=&endAt=",
        "language":   "zh",
        "category":   "台股",
        "enabled":    True,
        "fetch_type": "cnyes",
    },
    {
        "name":       "鉅亨網-美股",
        "url":        "https://news.cnyes.com/api/v3/news/category/us_stock?limit=30&startAt=&endAt=",
        "language":   "zh",
        "category":   "美股",
        "enabled":    True,
        "fetch_type": "cnyes",
    },
    {
        "name":       "鉅亨網-財經",
        "url":        "https://news.cnyes.com/api/v3/news/category/headline?limit=30&startAt=&endAt=",
        "language":   "zh",
        "category":   "財經",
        "enabled":    True,
        "fetch_type": "cnyes",
    },
]


# ── RSS 抓取（feedparser 直接解析）───────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    """標準 RSS：feedparser 直接抓"""
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

        results = _parse_feed_entries(feed.entries, source)
        logger.info(f"[{source['name']}] 抓到 {len(results)} 則")

    except Exception as e:
        logger.error(f"[{source['name']}] 失敗：{e}")

    return results


# ── RSS 抓取（requests 先取原始 XML，再用 feedparser 解析）──────────────────
def fetch_rss_via_requests(source: dict) -> list[dict]:
    """
    需要自訂 header 的 RSS（如 MoneyDJ）：
    先用 requests 帶正確 header 拿到原始 XML bytes，
    再交給 feedparser.parse() 解析。
    """
    if not FEEDPARSER_OK:
        logger.warning("feedparser 未安裝")
        return []

    results = []
    try:
        hdrs = source.get("headers", HEADERS)
        resp = requests.get(source["url"], headers=hdrs, timeout=TIMEOUT)
        resp.raise_for_status()

        # feedparser 可以直接解析 bytes / str
        feed = feedparser.parse(resp.content)

        if feed.bozo and not feed.entries:
            logger.warning(f"[{source['name']}] RSS 格式異常：{feed.bozo_exception}")
            return []

        results = _parse_feed_entries(feed.entries, source)
        logger.info(f"[{source['name']}] 抓到 {len(results)} 則")

    except requests.HTTPError as e:
        logger.error(f"[{source['name']}] HTTP 錯誤：{e}")
    except Exception as e:
        logger.error(f"[{source['name']}] 失敗：{e}")

    return results


# ── 鉅亨網 JSON API 抓取 ─────────────────────────────────────────────────────
def fetch_cnyes(source: dict) -> list[dict]:
    """
    鉅亨網官方 JSON API：
    https://news.cnyes.com/api/v3/news/category/{category}?limit=30
    回傳 { data: { items: [ {newsId, title, summary, publishAt, ...} ] } }
    """
    CNYES_HEADERS = {
        **HEADERS,
        "Referer": "https://news.cnyes.com/",
        "Origin":  "https://news.cnyes.com",
    }

    results = []
    try:
        resp = requests.get(source["url"], headers=CNYES_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        items = (
            data.get("items") or                          # 部分版本直接在根層
            data.get("data", {}).get("items", [])         # 標準巢狀結構
        )

        for item in items[:MAX_ITEMS]:
            title   = _clean(item.get("title", ""))
            summary = _clean(item.get("summary") or item.get("content") or "")
            news_id = item.get("newsId") or item.get("_id", "")
            url     = f"https://news.cnyes.com/news/id/{news_id}" if news_id else ""

            pub_ts  = item.get("publishAt") or item.get("published_at")
            if pub_ts:
                try:
                    published_at = datetime.fromtimestamp(int(pub_ts), tz=timezone.utc)
                except Exception:
                    published_at = datetime.now(timezone.utc)
            else:
                published_at = datetime.now(timezone.utc)

            if not title or len(title) < 4:
                continue

            results.append({
                "title":        title,
                "summary":      summary[:600],
                "url":          url,
                "source":       source["name"],
                "language":     source["language"],
                "category":     source.get("category", "財經"),
                "published_at": published_at,
            })

        logger.info(f"[{source['name']}] 抓到 {len(results)} 則")

    except requests.HTTPError as e:
        logger.error(f"[{source['name']}] HTTP 錯誤：{e}")
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

        fetch_type = src.get("fetch_type", "rss")

        if fetch_type == "rss":
            articles = fetch_rss(src)
        elif fetch_type == "rss_req":
            articles = fetch_rss_via_requests(src)
        elif fetch_type == "cnyes":
            articles = fetch_cnyes(src)
        else:
            logger.warning(f"[{src['name']}] 未知 fetch_type: {fetch_type}")
            articles = []

        logs.append({
            "source": src["name"],
            "status": "success" if articles else "empty",
            "count":  len(articles),
        })
        all_articles.extend(articles)
        time.sleep(0.5)

    return all_articles, logs


# ── 共用：解析 feedparser entries ────────────────────────────────────────────
def _parse_feed_entries(entries, source: dict) -> list[dict]:
    results = []
    for entry in entries[:MAX_ITEMS]:
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
    return results


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
