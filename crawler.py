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

# 簡體 → 繁體（台灣用詞）。沒裝 opencc 也能跑，只是大陸簡體來源會維持簡體顯示、關鍵字比對較不準
try:
    from opencc import OpenCC
    _S2T = OpenCC("s2twp")
except Exception:
    _S2T = None


def _to_tw(text: str) -> str:
    if not text or _S2T is None:
        return text
    try:
        return _S2T.convert(text)
    except Exception:
        return text

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
TIMEOUT   = (5, 12)     # (連線, 讀取) 秒；以前單一來源最久會卡 20 秒
MAX_ITEMS = 50
CRAWL_WORKERS = 10       # 同時抓幾個來源（Google News 約 20 個，太高可能被限流，可視情況調 6~12）


def _gnews(query: str, en: bool = False, when: str = "1d", cn: bool = False) -> str:
    """Google News RSS（預設繁中台灣；en=True 為美國英文版；cn=True 為中國大陸簡體版，能抓到財聯社、證券時報等大陸媒體）"""
    q = quote(f"{query} when:{when}")
    if en:
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    if cn:
        return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
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
     "language": "zh", "category": "財經", "enabled": False},
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
     "language": "en", "category": "國際", "enabled": False},
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
    {"name": "BBC Sport",     "url": "https://feeds.bbci.co.uk/sport/rss.xml",
     "language": "en", "category": "運動", "enabled": True, "max_items": 8},
    {"name": "自由時報娛樂",  "url": "https://news.ltn.com.tw/rss/entertainment.xml",
     "language": "zh", "category": "娛樂", "enabled": True, "max_items": 12},
    {"name": "Variety",       "url": "https://variety.com/feed/",
     "language": "en", "category": "娛樂", "enabled": True, "max_items": 8},
    {"name": "巴哈姆特GNN",   "url": "https://gnn.gamer.com.tw/rss.xml",
     "language": "zh", "category": "電玩", "enabled": True, "max_items": 12},
    {"name": "IGN",           "url": "https://feeds.ign.com/ign/all",
     "language": "en", "category": "電玩", "enabled": True, "max_items": 8},
    # ETtoday / Yahoo奇摩：網址格式依 ETtoday 現行 feedburner 慣例與 Yahoo 的 /rss/<分類> 推測，我沒辦法連線驗證。
    # 跑過一輪後到設定頁「來源健康度」看，抓不到（0 篇）的就改網址或關掉
    {"name": "ETtoday運動",   "url": "https://feeds.feedburner.com/ettoday/sport",
     "language": "zh", "category": "運動", "enabled": True, "max_items": 10},
    {"name": "ETtoday影劇",   "url": "https://feeds.feedburner.com/ettoday/star",
     "language": "zh", "category": "娛樂", "enabled": True, "max_items": 10},
    {"name": "ETtoday遊戲",   "url": "https://feeds.feedburner.com/ettoday/game",
     "language": "zh", "category": "電玩", "enabled": True, "max_items": 10},
    {"name": "Yahoo奇摩運動", "url": "https://tw.news.yahoo.com/rss/sports",
     "language": "zh", "category": "運動", "enabled": True, "max_items": 10},
    {"name": "Yahoo奇摩娛樂", "url": "https://tw.news.yahoo.com/rss/entertainment",
     "language": "zh", "category": "娛樂", "enabled": True, "max_items": 10},
    {"name": "The Verge",     "url": "https://www.theverge.com/rss/index.xml",
     "language": "en", "category": "科技", "enabled": True, "max_items": 10},
    # ── 🏮 大陸（category="大陸"）────────────────────────────
    # s2t=True：簡體來源，抓回來後用 opencc 轉成繁體。cn=True 的 Google News 簡體版能直接抓到財聯社、證券時報、第一財經等大陸媒體。
    {"name": "GN-大陸A股",   "url": _gnews("A股 OR 上证指数 OR 沪深300 OR 创业板 OR 北向资金", cn=True),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 30},
    {"name": "GN-大陸政策",  "url": _gnews("央行 OR 国务院 OR 证监会 OR 发改委 OR 财政部 降准 OR 降息 OR 刺激 OR 稳增长 OR 新政", cn=True),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 30},
    {"name": "GN-大陸經濟",  "url": _gnews("中国 经济 数据 OR 房地产 OR 消费 OR 出口 OR 人民币 OR PMI OR 通缩", cn=True),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 30},
    {"name": "GN-大陸產業",  "url": _gnews("华为 OR 中芯国际 OR 比亚迪 OR 宁德时代 OR 小米 OR 阿里巴巴 OR 腾讯 财报 OR 芯片 OR 新能源车", cn=True),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 30},
    {"name": "GN-中美關係",  "url": _gnews("中美 OR 美中 關稅 OR 出口管制 OR 晶片禁令 OR 稀土 OR 實體清單 OR 脫鉤"),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "max_items": 30},
    {"name": "GN-港股",      "url": _gnews("港股 OR 恒生指數 OR 恆生科技 OR 港交所"),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "max_items": 25},
    {"name": "GN-兩岸",      "url": _gnews("兩岸 OR 台海 OR 國台辦 OR 陸委會 OR 解放軍", when="2d"),
     "language": "zh", "category": "大陸", "enabled": True, "strip_suffix": True, "max_items": 25},
    {"name": "GN-China Markets", "url": _gnews("China stocks OR yuan OR PBOC OR Shanghai Composite OR Hang Seng OR China economy", en=True),
     "language": "en", "category": "大陸", "enabled": True, "strip_suffix": True, "max_items": 25},
    # 以下 4 個我有確認過網址可用
    {"name": "BBC中文",      "url": "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml",
     "language": "zh", "category": "大陸", "enabled": True, "max_items": 20},
    {"name": "BBC China",    "url": "http://feeds.bbci.co.uk/news/world/asia/china/rss.xml",
     "language": "en", "category": "大陸", "enabled": True, "max_items": 15},
    {"name": "SCMP China Biz 1", "url": "https://www.scmp.com/rss/523893/feed",
     "language": "en", "category": "大陸", "enabled": True, "max_items": 15},
    {"name": "SCMP China Biz 2", "url": "https://www.scmp.com/rss/523082/feed",
     "language": "en", "category": "大陸", "enabled": True, "max_items": 15},
    # ── 🏮 大陸生活（娛樂／電玩／旅遊）：category 刻意跟「大陸」分開，
    #    不會進今日新聞主牆、不算頭條/利多利空統計、不送 AI、不進 AI 專區中國分頁，
    #    只在「🏮 大陸」篩選按鈕底下的子分類才看得到 ──
    {"name": "GN-大陸娛樂", "url": _gnews("娱乐 明星 OR 电影票房 OR 综艺 OR 春晚", cn=True),
     "language": "zh", "category": "大陸生活", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 15},
    {"name": "GN-大陸電玩", "url": _gnews("游戏 腾讯游戏 OR 网易游戏 OR 原神 OR 黑神话 OR 米哈游", cn=True),
     "language": "zh", "category": "大陸生活", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 15},
    {"name": "GN-大陸旅遊", "url": _gnews("旅游 OR 景区 OR 免签 OR 出境游 OR 文旅", cn=True),
     "language": "zh", "category": "大陸生活", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 15},
    # 以下網址我沒辦法確認是否還有效，預設關閉；想試的話改成 True，再到設定頁「來源健康度」看有沒有抓到
    {"name": "FT中文網",     "url": "https://www.ftchinese.com/rss/feed",
     "language": "zh", "category": "大陸", "enabled": False, "s2t": True, "max_items": 20},
    {"name": "中新網財經",   "url": "https://www.chinanews.com.cn/rss/finance.xml",
     "language": "zh", "category": "大陸", "enabled": False, "s2t": True, "max_items": 20},
    {"name": "德國之聲中文", "url": "https://rss.dw.com/rdf/rss-chi-all",
     "language": "zh", "category": "大陸", "enabled": False, "s2t": True, "max_items": 20},
    # ── 🧳 旅遊（category="旅遊"；region 決定分頁上的 台灣／國外／大陸 篩選）──────────────
    # 主題以「風景」為主。標「直連」的是網站自己的 RSS（有縮圖）；標「GN」的是用 Google News 的 site: 搜尋，
    # 沒有官方 RSS 的網站只能這樣抓（Google 連結沒有縮圖，卡片會顯示來源色塊）。
    # 直連網址我沒辦法連線驗證，跑過一輪後到設定頁「來源健康度」看，0 篇的就改網址或關掉。
    # ── 台灣（繁中）──
    {"name": "ETtoday旅遊",   "url": "https://feeds.feedburner.com/ettoday/travel",
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "max_items": 12},
    {"name": "GN-ETtoday旅遊", "url": _gnews("site:travel.ettoday.net", when="3d"),
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "strip_suffix": True, "max_items": 10},
    {"name": "GN-自由玩咖",   "url": _gnews("site:playing.ltn.com.tw 旅行 OR 景點 OR 住宿 OR 美食", when="3d"),
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "strip_suffix": True, "max_items": 12},
    {"name": "GN-Yahoo奇摩旅遊", "url": _gnews("site:tw.news.yahoo.com 旅遊 OR 景點 OR 旅行 OR 美食", when="3d"),
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "strip_suffix": True, "max_items": 12},
    {"name": "GN-台灣風景",   "url": _gnews("風景 OR 秘境 OR 步道 OR 雲海 OR 日出 OR 夕陽 OR 賞花 OR 楓葉 台灣 景點", when="3d"),
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "strip_suffix": True, "max_items": 12},
    {"name": "GN-台灣美食旅遊", "url": _gnews("美食 OR 小吃 OR 夜市 OR 老街 OR 民宿 台灣 旅遊", when="3d"),
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "strip_suffix": True, "max_items": 8},
    # ── 大陸（簡體，抓回來轉繁體）──
    {"name": "GN-新浪旅遊",   "url": _gnews("site:travel.sina.com.cn OR site:travel.sina.cn", cn=True, when="7d"),
     "language": "zh", "category": "旅遊", "region": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 12},
    {"name": "GN-搜狐旅遊",   "url": _gnews("site:travel.sohu.com", cn=True, when="7d"),
     "language": "zh", "category": "旅遊", "region": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 12},
    {"name": "GN-大陸風景",   "url": _gnews("风景 OR 美景 OR 古镇 OR 雪景 OR 赏花 OR 秘境 OR 5A景区", cn=True, when="3d"),
     "language": "zh", "category": "旅遊", "region": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 12},
    {"name": "GN-大陸美食旅遊", "url": _gnews("美食 OR 小吃 OR 民宿 OR 自驾游 旅游攻略", cn=True, when="3d"),
     "language": "zh", "category": "旅遊", "region": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 8},
    # ── 國外（歐美，英文；標題自動翻中文）──
    {"name": "Wanderlust",    "url": "https://www.wanderlustmagazine.com/feed/",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 10},
    {"name": "GN-國家地理旅遊", "url": _gnews("site:nationalgeographic.com/travel", en=True, when="14d"),
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "strip_suffix": True, "max_items": 10},
    {"name": "GN-Yahoo Travel", "url": _gnews("site:travel.yahoo.com", en=True, when="7d"),
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "strip_suffix": True, "max_items": 10},
    {"name": "Guardian Travel", "url": "https://www.theguardian.com/travel/rss",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 10},
    {"name": "NYT Travel",    "url": "https://rss.nytimes.com/services/xml/rss/nyt/Travel.xml",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 10},
    {"name": "CN Traveler",   "url": "https://www.cntraveler.com/feed/rss",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 10},
    {"name": "Atlas Obscura", "url": "https://www.atlasobscura.com/feeds/latest",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 10},
    {"name": "Smithsonian Travel", "url": "https://www.smithsonianmag.com/rss/travel/",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 8},
    {"name": "BBC Travel",    "url": "https://www.bbc.com/travel/feed.rss",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 8},
    {"name": "GN-Scenic EN",  "url": _gnews("scenic OR landscape OR breathtaking OR \"national park\" OR \"hidden gem\" travel destination", en=True, when="3d"),
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "strip_suffix": True, "max_items": 12},
    # ── 📷 風景／攝影專站（照片為主；直連 RSS 多半有大圖，網址同樣沒辦法連線驗證，看來源健康度）──
    {"name": "Wikimedia 每日精選圖", "url": "https://commons.wikimedia.org/w/api.php?action=featuredfeed&feed=potd&feedformat=rss&language=en",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 8},
    {"name": "NASA 地球觀測每日圖", "url": "https://earthobservatory.nasa.gov/feeds/image-of-the-day.rss",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 6},
    {"name": "Outdoor Photographer", "url": "https://www.outdoorphotographer.com/feed/",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 8},
    {"name": "500px ISO",     "url": "https://iso.500px.com/feed/",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "max_items": 8},
    {"name": "My Modern Met", "url": "https://mymodernmet.com/feed/",
     "language": "en", "category": "旅遊", "region": "國外", "enabled": False, "max_items": 8},  # 已停用：圖片常破圖
    {"name": "GN-國家地理攝影", "url": _gnews("site:nationalgeographic.com photography landscape OR \"photo of the day\" OR \"picture of the day\"", en=True, when="14d"),
     "language": "en", "category": "旅遊", "region": "國外", "enabled": True, "strip_suffix": True, "max_items": 8},
    {"name": "GN-風景攝影(中文)", "url": _gnews("風景攝影 OR 攝影師 OR 空拍 OR 絕美 OR 美景 照片 台灣", when="7d"),
     "language": "zh", "category": "旅遊", "region": "台灣", "enabled": True, "strip_suffix": True, "max_items": 10},
    {"name": "GN-中國國家地理", "url": _gnews("site:dili360.com OR site:natgeomedia.com", cn=True, when="14d"),
     "language": "zh", "category": "旅遊", "region": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 8},
    {"name": "GN-大陸風景攝影", "url": _gnews("风光摄影 OR 航拍 OR 摄影师 OR 绝美 OR 云海 OR 日照金山", cn=True, when="7d"),
     "language": "zh", "category": "旅遊", "region": "大陸", "enabled": True, "strip_suffix": True, "s2t": True, "max_items": 10},
    # ── 已停用（保留設定，需要時到設定頁開）──────────────────
    {"name": "信傳媒",        "url": "https://www.cmmedia.com.tw/rss/yahoo/article",
     "language": "zh", "category": "財經", "enabled": False},
]


# ── 例行月營收公告過濾 ────────────────────────────────────────────────────────
_REV_RE  = re.compile(r"\d{1,2}月(份)?(合併|自結)?營收|營收(月增|月減|年增|年減|創|衰退|成長)|Q[1-4]營收")
_KEEP_RE = re.compile(r"股價|大跌|大漲|漲停|跌停|飆|暴|法說|財測|目標價|外資|砍|虧損|減資|增資")


def is_routine_revenue(title: str) -> bool:
    """「聯詠10月營收100億元」這種每月數百則的例行公告 → True（要過濾）。
    標題夠短、且沒有股價/法說/外資等有資訊量的字眼才算。"""
    t = title or ""
    return bool(_REV_RE.search(t)) and len(t) <= 30 and not _KEEP_RE.search(t)


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
    prio = {"旅遊": 0, "運動": 0, "娛樂": 0, "電玩": 0, "AI": 1, "科技": 1}
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
        if feed.entries or r.status_code < 400:
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

            if source.get("s2t"):                      # 大陸簡體來源：轉成繁體再往下游走（顯示、關鍵字分析都比較準）
                title, summary, src_name = _to_tw(title), _to_tw(summary), _to_tw(src_name)

            if not title or len(title) < 4:
                continue

            results.append({
                "title":        title,
                "summary":      summary[:600],
                "url":          url,
                "source":       src_name,
                "language":     source["language"],
                "category":     source.get("category", "財經"),
                "region":       source.get("region", ""),
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

    todo = []
    for src in SOURCES:
        if enabled_names is not None:
            if src["name"] not in enabled_names:
                continue
        elif not src["enabled"]:
            continue
        todo.append(src)

    # 以前是一個一個排隊抓（約 55 個來源，慢的來源最多各等 20 秒），現在同時抓 CRAWL_WORKERS 個。
    # ex.map 會照 SOURCES 原本順序回傳，所以文章與 log 順序、下游行為都跟以前一樣。
    with ThreadPoolExecutor(max_workers=CRAWL_WORKERS) as ex:
        results = list(ex.map(fetch_rss, todo))
    for src, articles in zip(todo, results):
        logs.append({
            "source": src["name"],
            "status": "success" if articles else "empty",
            "count":  len(articles),
        })
        all_articles.extend(articles)

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
