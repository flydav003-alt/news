"""
crawler.py — 新聞抓取模組 v3（中英文來源 + 縮圖）

改動重點：
  - 移除已停服的 feedburner（中央社）、非財經的新頭殼全站、需 session 的信傳媒
  - 新增 Google News 台股關鍵字 RSS（涵蓋鉅亨、MoneyDJ、工商時報、經濟日報等多家）
  - 新增英文來源：Yahoo Finance、CNBC、MarketWatch、BBC Business（英文標題由 scheduler 翻成中文）
  - 抓縮圖：RSS 內的 media_thumbnail / enclosure / <img>；缺圖再用 og:image 補（僅在背景排程啟用）
  - 每來源上限 30 → 50 則
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

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
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT   = 20
MAX_ITEMS = 50


def _gnews(query: str, en: bool = False, when: str = "1d") -> str:
    """Google News RSS（預設繁中台灣；en=True 為美國英文版）"""
    q = quote(f"{query} when:{when}")
    if en:
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"


# ── 來源清單 ──────────────────────────────────────────────────────────────────
# strip_suffix=True：Google News 標題尾巴有「 - 媒體名」，要拆掉並把媒體名當 source
SOURCES = [
    # ── 中文：原本穩定的 ──────────────────────────────────────
    {"name": "科技新報",     "url": "https://technews.tw/feed/",
     "language": "zh", "category": "科技", "enabled": True},
    {"name": "經濟日報",     "url": "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
     "language": "zh", "category": "財經", "enabled": True},
    {"name": "聯合報財經",   "url": "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
     "language": "zh", "category": "財經", "enabled": True},
    {"name": "Yahoo奇摩股市", "url": "https://tw.news.yahoo.com/rss/finance",
     "language": "zh", "category": "財經", "enabled": True},
    {"name": "自由時報財經", "url": "https://news.ltn.com.tw/rss/business.xml",
     "language": "zh", "category": "財經", "enabled": True},
    {"name": "風傳媒",       "url": "https://www.storm.mg/feed",
     "language": "zh", "category": "財經", "enabled": True},
    # ── 中文：Google News 關鍵字（取代死掉的中央社/鉅亨/MoneyDJ）──
    {"name": "GN-台股",      "url": _gnews("台股 OR 加權指數"),
     "language": "zh", "category": "財經", "enabled": True, "strip_suffix": True},
    {"name": "GN-法人籌碼",  "url": _gnews("外資 OR 投信 買超 OR 賣超 台股"),
     "language": "zh", "category": "財經", "enabled": True, "strip_suffix": True},
    {"name": "GN-AI供應鏈",  "url": _gnews("台積電 OR 輝達 OR 廣達 OR 緯創 AI 伺服器"),
     "language": "zh", "category": "科技", "enabled": True, "strip_suffix": True},
    {"name": "GN-重大訊息",  "url": _gnews("重大訊息 OR 法說會 OR 營收 公布 上市櫃"),
     "language": "zh", "category": "財經", "enabled": True, "strip_suffix": True},
    {"name": "GN-美股",      "url": _gnews("美股 OR 那斯達克 OR 費城半導體"),
     "language": "zh", "category": "國際", "enabled": True, "strip_suffix": True},
    # ── 英文（標題會自動翻成中文）────────────────────────────
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex",
     "language": "en", "category": "國際", "enabled": True},
    {"name": "CNBC",          "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
     "language": "en", "category": "國際", "enabled": True},
    {"name": "CNBC Tech",     "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
     "language": "en", "category": "科技", "enabled": True},
    {"name": "MarketWatch",   "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
     "language": "en", "category": "國際", "enabled": True},
    {"name": "BBC Business",  "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
     "language": "en", "category": "國際", "enabled": True},
    # ── 🤖 AI 專區（美國／中國，英文，標題自動翻中文）────────
    {"name": "GN-美國AI",     "url": _gnews("Nvidia OR OpenAI OR Anthropic OR Microsoft OR Google AI chips data center", en=True),
     "language": "en", "category": "AI", "enabled": True, "strip_suffix": True, "max_items": 25},
    {"name": "GN-中國AI",     "url": _gnews("DeepSeek OR Alibaba Qwen OR Huawei Ascend OR Moonshot OR Baidu AI China chips", en=True, when="3d"),
     "language": "en", "category": "AI", "enabled": True, "strip_suffix": True, "max_items": 25},
    {"name": "GN-AI模型產品", "url": _gnews("ChatGPT OR GPT-5 OR Claude OR Gemini OR Grok OR Llama new model launch release", en=True, when="2d"),
     "language": "en", "category": "AI", "enabled": True, "strip_suffix": True, "max_items": 25},
    {"name": "GN-大陸AI模型", "url": _gnews("DeepSeek OR 通義千問 OR 豆包 OR Kimi OR 智譜 OR 文心 大模型", when="3d"),
     "language": "zh", "category": "AI", "enabled": True, "strip_suffix": True, "max_items": 20},
    {"name": "GN-AI新工具(中文)", "url": _gnews("ChatGPT OR Claude OR Gemini OR Grok 新功能 OR 新模型 OR 發布", when="2d"),
     "language": "zh", "category": "AI", "enabled": True, "strip_suffix": True, "max_items": 20},
    {"name": "Ars Technica AI", "url": "https://arstechnica.com/ai/feed/",
     "language": "en", "category": "AI", "enabled": True, "max_items": 12},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/",
     "language": "en", "category": "AI", "enabled": True, "max_items": 12},
    {"name": "MIT Tech Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
     "language": "en", "category": "AI", "enabled": True, "max_items": 10},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
     "language": "en", "category": "AI", "enabled": True, "max_items": 15},
    {"name": "The Verge AI",  "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
     "language": "en", "category": "AI", "enabled": True, "max_items": 15},
    {"name": "SCMP Tech",     "url": "https://www.scmp.com/rss/36/feed",
     "language": "en", "category": "AI", "enabled": True, "max_items": 15},
    # ── 🎮 生活：運動／娛樂／電玩（資料量刻意少，每源 10~12 則）──
    {"name": "自由時報運動",  "url": "https://news.ltn.com.tw/rss/sports.xml",
     "language": "zh", "category": "運動", "enabled": True, "max_items": 12},
    {"name": "ESPN",          "url": "https://www.espn.com/espn/rss/news",
     "language": "en", "category": "運動", "enabled": True, "max_items": 8},
    {"name": "自由時報娛樂",  "url": "https://news.ltn.com.tw/rss/entertainment.xml",
     "language": "zh", "category": "娛樂", "enabled": True, "max_items": 12},
    {"name": "Variety",       "url": "https://variety.com/feed/",
     "language": "en", "category": "娛樂", "enabled": True, "max_items": 8},
    {"name": "巴哈姆特GNN",   "url": "https://gnn.gamer.com.tw/rss.xml",
     "language": "zh", "category": "電玩", "enabled": True, "max_items": 12},
    {"name": "IGN",           "url": "https://feeds.ign.com/ign/all",
     "language": "en", "category": "電玩", "enabled": True, "max_items": 8},
    {"name": "The Verge",     "url": "https://www.theverge.com/rss/index.xml",
     "language": "en", "category": "科技", "enabled": True, "max_items": 10},
    # ── 已停用（保留設定，需要時到設定頁開）──────────────────
    {"name": "信傳媒",        "url": "https://www.cmmedia.com.tw/rss/yahoo/article",
     "language": "zh", "category": "財經", "enabled": False},
]


# ── 圖片擷取 ──────────────────────────────────────────────────────────────────
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_OG_RE  = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I,
)
_BAD_IMG = ("pixel", "1x1", "feedburner", "spacer", "blank.gif", "/ads/", "logo", "favicon", "default-image", "placeholder", "googleusercontent.com/proxy")


def _ok_img(u: str) -> bool:
    return bool(u) and u.startswith("http") and not any(b in u.lower() for b in _BAD_IMG)


def _extract_image(entry) -> str:
    """從 RSS entry 找縮圖；找不到回傳空字串"""
    for key in ("media_thumbnail", "media_content"):
        for m in getattr(entry, key, None) or []:
            u = m.get("url", "")
            if _ok_img(u):
                return u
    for enc in getattr(entry, "enclosures", None) or []:
        if str(enc.get("type", "")).startswith("image") and _ok_img(enc.get("href", "")):
            return enc["href"]
    for lk in getattr(entry, "links", None) or []:
        if str(lk.get("type", "")).startswith("image") and _ok_img(lk.get("href", "")):
            return lk["href"]
    raw = getattr(entry, "summary", "") or ""
    for c in getattr(entry, "content", None) or []:
        raw += c.get("value", "")
    for m in _IMG_RE.finditer(raw):
        if _ok_img(m.group(1)):
            return m.group(1)
    return ""


def _fetch_og_image(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=6, stream=True)
        html = r.raw.read(80_000, decode_content=True).decode("utf-8", "ignore")
        m = _OG_RE.search(html)
        if m:
            u = (m.group(1) or m.group(2) or "").strip()
            return u if _ok_img(u) else ""
    except Exception:
        pass
    return ""


def fill_missing_images(articles: list[dict], limit: int = 400) -> int:
    """缺圖文章用 og:image 補（Google News 轉址連結跳過）。回傳補到幾張。
    生活類／AI 排在前面：以前照來源順序補，名額被前面的財經來源用完，運動/娛樂就永遠沒圖。"""
    prio = {"運動": 0, "娛樂": 0, "電玩": 0, "AI": 1, "科技": 1}
    todo = [a for a in articles
            if not a.get("image_url")
            and a.get("url", "").startswith("http")
            and "news.google.com" not in a["url"]]
    todo.sort(key=lambda a: prio.get(a.get("category", ""), 2))
    todo = todo[:limit]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        imgs = list(ex.map(lambda a: _fetch_og_image(a["url"]), todo))
    logger.info(f"og:image 嘗試 {len(todo)} 篇（各類別：" + ", ".join(
        f"{c}={sum(1 for a in todo if a.get('category') == c)}" for c in sorted({a.get('category') for a in todo})) + "）")
    n = 0
    for a, img in zip(todo, imgs):
        if img:
            a["image_url"] = img
            n += 1
    return n


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _parse_feed(url: str):
    """
    先用 requests 抓內容，清掉非法控制字元後再交給 feedparser。
    （以前直接 feedparser.parse(url)，遇到格式小瑕疵會整個來源變 0 則）
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        raw = r.content
        if r.status_code >= 400:
            logger.warning(f"HTTP {r.status_code}：{url[:70]}")
        txt = raw.decode(r.encoding or "utf-8", "ignore") if not raw[:100].lstrip().startswith(b"<?xml") else raw.decode("utf-8", "ignore")
        txt = _CTRL_RE.sub("", txt)
        # feedparser 對含 encoding 宣告的 str 會警告，改丟 bytes
        feed = feedparser.parse(txt.encode("utf-8"))
        if feed.entries:
            return feed
    except Exception as e:
        logger.debug(f"requests 抓取失敗，改用 feedparser 直連：{e}")
    return feedparser.parse(url, request_headers=HEADERS, agent=HEADERS["User-Agent"])


# ── RSS 抓取核心 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    """抓取單一 RSS 來源，回傳標準化文章 list"""
    if not FEEDPARSER_OK:
        logger.warning("feedparser 未安裝")
        return []

    results = []
    try:
        feed = _parse_feed(source["url"])

        if not feed.entries:
            logger.warning(f"[{source['name']}] 沒有文章（{getattr(feed, 'bozo_exception', '空回應')}）")
            return []

        for entry in feed.entries[:source.get("max_items", MAX_ITEMS)]:
            title   = _clean(getattr(entry, "title", ""))
            summary = _clean(getattr(entry, "summary", ""))
            url     = getattr(entry, "link", "")
            src_name = source["name"]

            if source.get("strip_suffix"):
                pub = getattr(entry, "source", None)
                pub_name = pub.get("title", "") if pub else ""
                if pub_name and title.endswith(f" - {pub_name}"):
                    title = title[: -len(f" - {pub_name}")].strip()
                else:
                    title = re.sub(r"\s+-\s+[^-]{2,20}$", "", title)
                if pub_name:
                    src_name = pub_name
                summary = ""   # Google News 的 summary 只是重複標題

            if not title or len(title) < 4:
                continue

            results.append({
                "title":        title,
                "summary":      summary[:600],
                "url":          url,
                "source":       src_name,
                "language":     source["language"],
                "category":     source.get("category", "財經"),
                "image_url":    _extract_image(entry),
                "published_at": _parse_time(entry),
            })

        logger.info(f"[{source['name']}] 抓到 {len(results)} 則")

    except Exception as e:
        logger.error(f"[{source['name']}] 失敗：{e}")

    return results


# ── 主執行入口 ────────────────────────────────────────────────────────────────
def run_crawl(enabled_names: Optional[list[str]] = None,
              fetch_og: bool = False,
              known_urls: Optional[set] = None) -> tuple[list[dict], list[dict]]:
    """
    執行全部來源抓取。
    enabled_names：None 表示依 source["enabled"] 決定；傳入名稱 list 可讓設定頁動態控制。
    fetch_og：True 時對缺圖文章補抓 og:image（較慢，僅背景排程用）
    known_urls：已在資料庫的網址，不重複補圖
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
            "source": src["name"],
            "status": "success" if articles else "empty",
            "count":  len(articles),
        })
        all_articles.extend(articles)
        time.sleep(0.3)

    if fetch_og:
        known = known_urls or set()
        fresh = [a for a in all_articles if a.get("url") not in known]
        got = fill_missing_images(fresh)
        logger.info(f"og:image 補圖 {got} 張")

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
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)
