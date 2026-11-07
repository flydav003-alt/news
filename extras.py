"""
extras.py — 籌碼 / 行事曆 / 社群 資料抓取（由 GitHub Actions 執行，輸出 extras.json）

用法：  python extras.py data_out/extras.json

設計原則
  - 任何一個資料源失敗都不會讓整支程式失敗：該區塊保留上一次的資料，並在 status / parts 記下原因，
    App 的分頁底部會顯示「資料狀態」，方便你從畫面直接看出哪一塊壞了。
  - 證交所有流量限制：每次請求至少間隔 1 秒；各區塊有自己的更新頻率（REFRESH_MIN），不會每次都重抓。
  - 證交所欄位名稱用「名稱比對」而不是固定欄位位置，格式小改版時比較不容易整個壞掉。

區塊
  chips     三大法人（大盤＋個股買賣超前 10）、融資融券增減、注意股、處置股
  calendar  只讀 calendar_manual.json（總經、NVDA 財報、休市），只顯示未來 62 天
  social    Threads（需 token）、PTT 熱門、Reddit、微博熱搜
"""

import calendar as _cal
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone, date

import requests

logger = logging.getLogger("extras")
TZ_TW = timezone(timedelta(hours=8))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
REFRESH_MIN = {"chips": 40, "calendar": 170, "social": 20}      # 各區塊最短更新間隔（分鐘）
_last_call = [0.0]


# ─────────────────────────────────────────────
# 基礎工具
# ─────────────────────────────────────────────
def _get(url, params=None, headers=None, timeout=25, gap=1.0, cookies=None):
    """禮貌地 GET：兩次請求至少間隔 gap 秒。失敗回 None。"""
    wait = gap - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    try:
        r = requests.get(url, params=params, headers=headers or UA, timeout=timeout, cookies=cookies)
        if r.status_code != 200:
            logger.warning(f"HTTP {r.status_code}：{url[:80]}")
            return None
        return r
    except Exception as e:
        logger.warning(f"請求失敗：{url[:80]} → {type(e).__name__}: {e}")
        return None


def _json(url, params=None, headers=None, gap=1.0):
    r = _get(url, params=params, headers=headers, gap=gap)
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        logger.warning(f"不是 JSON：{url[:80]}")
        return None


def _n(v) -> float:
    try:
        s = str(v).replace(",", "").replace("+", "").strip()
        return float(s) if s not in ("", "--", "-", "X") else 0.0
    except Exception:
        return 0.0


def _tags(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def _rows(j):
    """TWSE JSON → (fields, data)。支援新版 tables[] 與舊版 fields/data、fields1/data1…，回傳「最大的那張表」。"""
    if not isinstance(j, dict):
        return [], []
    cands = []
    if j.get("fields") and j.get("data"):
        cands.append((j["fields"], j["data"]))
    for t in j.get("tables") or []:
        if isinstance(t, dict) and t.get("fields") and t.get("data"):
            cands.append((t["fields"], t["data"]))
    for i in range(1, 10):
        if j.get(f"fields{i}") and j.get(f"data{i}"):
            cands.append((j[f"fields{i}"], j[f"data{i}"]))
    if not cands:
        return [], []
    return max(cands, key=lambda c: len(c[1]))


def _idx(fields, needle, exact=False):
    for i, f in enumerate(fields):
        f = _tags(f)
        if (f == needle) if exact else (needle in f):
            return i
    return -1


def _pick(rec: dict, *needles):
    """從一筆 dict 依「欄位名稱包含」取值（不分大小寫），取不到回傳空字串"""
    for nd in needles:
        for k, v in rec.items():
            if nd.lower() in str(k).lower() and str(v).strip():
                return _tags(v)
    return ""


def _weekdays_back(today: date, n: int):
    d = today
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _roc(s):
    """'115年11月12日' / '115/11/12' / '20261112' → date；失敗回 None"""
    s = str(s or "").strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{2,4})\D+(\d{1,2})\D+(\d{1,2})", s)
        if m:
            y = int(m.group(1))
            y = y + 1911 if y < 1911 else y
            return date(y, int(m.group(2)), int(m.group(3)))
    except Exception:
        pass
    return None


def _ago_min(iso: str) -> float:
    try:
        return (datetime.now(TZ_TW) - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return 1e9


# ─────────────────────────────────────────────
# 籌碼
# ─────────────────────────────────────────────
TWSE = "https://www.twse.com.tw"


def fetch_t86(ds: str):
    """三大法人買賣超日報（個股）。回傳 [{c,n,f,t,d,a}]（單位：張），沒資料回傳 []"""
    for base in (f"{TWSE}/rwd/zh/fund/T86", f"{TWSE}/fund/T86"):
        j = _json(base, {"response": "json", "date": ds, "selectType": "ALLBUT0999"})
        if not isinstance(j, dict) or j.get("stat") not in (None, "OK"):
            continue
        f, d = _rows(j)
        if not d:
            continue
        ic, inm = _idx(f, "證券代號"), _idx(f, "證券名稱")
        i_f1 = _idx(f, "外陸資買賣超股數")
        i_f2 = _idx(f, "外資自營商買賣超股數", exact=True)
        i_t = _idx(f, "投信買賣超股數")
        i_d = _idx(f, "自營商買賣超股數", exact=True)
        i_a = _idx(f, "三大法人買賣超股數")
        if min(ic, inm, i_t) < 0 or i_f1 < 0:
            logger.warning(f"T86 欄位對不上：{f}")
            continue
        out = []
        for row in d:
            code = str(row[ic]).strip()
            if not re.fullmatch(r"\d{4}", code):          # 只要 4 碼個股，排除 ETF / 債券
                continue
            fr = _n(row[i_f1]) + (_n(row[i_f2]) if i_f2 >= 0 else 0)
            tr = _n(row[i_t])
            dl = _n(row[i_d]) if i_d >= 0 else 0.0
            al = _n(row[i_a]) if i_a >= 0 else fr + tr + dl
            out.append({"c": code, "n": _tags(row[inm]), "f": round(fr / 1000), "t": round(tr / 1000),
                        "d": round(dl / 1000), "a": round(al / 1000)})
        if out:
            return out
    return []


def fetch_bfi82u(ds: str):
    """三大法人買賣金額統計（大盤）。回傳 {foreign,trust,dealer,total}（億元）或 None"""
    for base in (f"{TWSE}/rwd/zh/fund/BFI82U", f"{TWSE}/fund/BFI82U"):
        j = _json(base, {"response": "json", "dayDate": ds, "type": "day"})
        f, d = _rows(j)
        if not d:
            continue
        rows = [(_tags(r[0]), _n(r[3]) / 1e8) for r in d if len(r) >= 4]
        main = next((x for x in rows if "外資及陸資" in x[0]), None)
        fdlr = next((x for x in rows if x[0] == "外資自營商"), None)
        foreign = (main[1] if main else 0.0) + (fdlr[1] if (main and "不含" in main[0] and fdlr) else 0.0)
        trust = next((x[1] for x in rows if x[0].startswith("投信")), 0.0)
        dealer = sum(x[1] for x in rows if x[0].startswith("自營商"))
        total = next((x[1] for x in rows if "合計" in x[0]), foreign + trust + dealer)
        return {"foreign": round(foreign, 2), "trust": round(trust, 2), "dealer": round(dealer, 2),
                "total": round(total, 2)}
    return None


def fetch_margin(ds: str):
    """融資融券。回傳 (個股 [{c,n,fin,finp,sh,shp}], 大盤融資金額 {bal,chg}(億元) 或 None)；沒資料回傳 ([], None)"""
    for base in (f"{TWSE}/rwd/zh/marginTrading/MI_MARGN", f"{TWSE}/exchangeReport/MI_MARGN"):
        j = _json(base, {"response": "json", "date": ds, "selectType": "ALL"})
        if not isinstance(j, dict) or j.get("stat") not in (None, "OK"):
            continue
        f, d = _rows(j)
        stocks = []
        for row in d:
            if len(row) < 13:
                continue
            code = str(row[0]).strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            stocks.append({"c": code, "n": _tags(row[1]), "fin": _n(row[6]), "finp": _n(row[5]),
                           "sh": _n(row[12]), "shp": _n(row[11])})
        if not stocks:
            continue
        amt = None
        tables = j.get("tables") or []
        for t in tables:
            for r in (t.get("data") or []) if isinstance(t, dict) else []:
                if str(r[0]).startswith("融資金額") and len(r) >= 6:
                    amt = {"bal": round(_n(r[5]) / 1e5, 1), "chg": round((_n(r[5]) - _n(r[4])) / 1e5, 1)}
                    # 欄位：買進、賣出、償還、前日餘額、今日餘額（仟元）→ 今日 r[5]、前日 r[4]
        return stocks, amt
    return [], None


def fetch_announce(kind: str, d_from: date, d_to: date):
    """注意股 / 處置股。kind = 'notice' | 'punish'。回傳 [{date,c,n,reason,period}]"""
    params = {"response": "json", "startDate": d_from.strftime("%Y%m%d"), "endDate": d_to.strftime("%Y%m%d")}
    for base in (f"{TWSE}/rwd/zh/announcement/{kind}", f"{TWSE}/announcement/{kind}"):
        j = _json(base, params)
        f, d = _rows(j)
        if not d:
            continue
        out = []
        for row in d:
            rec = dict(zip([_tags(x) for x in f], row))
            code = _pick(rec, "證券代號")
            if not code:
                continue
            out.append({
                "date": _pick(rec, "公布日期", "日期"),
                "c": code, "n": _pick(rec, "證券名稱"),
                "reason": _pick(rec, "注意交易資訊", "處置條件", "處置內容")[:120],
                "period": _pick(rec, "處置起迄時間", "處置起迄"),
            })
        if out:
            return out[:60]
    return []


def build_chips(prev: dict, now: datetime) -> dict:
    today = now.date()
    parts, out = {}, dict(prev or {})
    out["fetched_at"] = now.isoformat(timespec="seconds")

    # 1) 個股三大法人：從今天往回找最新有資料的一天
    ds, rows = "", []
    for d in _weekdays_back(today, 6):
        rows = fetch_t86(d.strftime("%Y%m%d"))
        if rows:
            ds = d.strftime("%Y%m%d")
            break
    if rows:
        parts["t86"] = "ok"
        out["asof"] = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        for key, label in (("f", "foreign"), ("t", "trust")):
            srt = sorted(rows, key=lambda r: r[key], reverse=True)
            out[f"{label}_buy"] = [{"c": r["c"], "n": r["n"], "v": r[key]} for r in srt[:10] if r[key] > 0]
            out[f"{label}_sell"] = [{"c": r["c"], "n": r["n"], "v": r[key]} for r in srt[::-1][:10] if r[key] < 0]
    else:
        parts["t86"] = "error: 近 6 個交易日都抓不到"

    # 2) 大盤三大法人金額（今天這一天 + 補歷史，每次最多補 3 天）
    hist = dict(out.get("history") or {})
    miss = set(out.get("miss") or [])
    if ds:
        mk = fetch_bfi82u(ds)
        if mk:
            hist[ds] = mk
            parts["bfi82u"] = "ok"
        else:
            parts["bfi82u"] = "error"
    filled = 0
    for d in _weekdays_back(today, 14):
        k = d.strftime("%Y%m%d")
        if filled >= 3:
            break
        if k in hist or k in miss or (ds and k > ds):
            continue
        mk = fetch_bfi82u(k)
        filled += 1
        if mk:
            hist[k] = mk
        elif (today - d).days > 2:
            miss.add(k)                                    # 超過 2 天仍沒有 = 休市日，不再重試
    keep = sorted(hist.keys())[-20:]
    out["history"] = {k: hist[k] for k in keep}
    out["miss"] = sorted(k for k in miss if k >= (keep[0] if keep else "0"))
    if ds and ds in hist:
        out["market"] = dict(hist[ds], date=out["asof"])

    # 3) 融資融券（融資券通常晚於三大法人公布）
    mdone = False
    for d in _weekdays_back(today, 4):
        stocks, amt = fetch_margin(d.strftime("%Y%m%d"))
        if stocks:
            for r in stocks:
                r["dfin"] = r["fin"] - r["finp"]
                r["dsh"] = r["sh"] - r["shp"]
            fin_up = sorted(stocks, key=lambda r: r["dfin"], reverse=True)[:10]
            fin_dn = sorted(stocks, key=lambda r: r["dfin"])[:10]
            sh_up = sorted(stocks, key=lambda r: r["dsh"], reverse=True)[:10]
            out["margin"] = {
                "date": d.isoformat(), "amt": amt,
                "fin_up": [{"c": r["c"], "n": r["n"], "v": int(r["dfin"]), "bal": int(r["fin"])} for r in fin_up if r["dfin"] > 0],
                "fin_down": [{"c": r["c"], "n": r["n"], "v": int(r["dfin"]), "bal": int(r["fin"])} for r in fin_dn if r["dfin"] < 0],
                "short_up": [{"c": r["c"], "n": r["n"], "v": int(r["dsh"]), "bal": int(r["sh"])} for r in sh_up if r["dsh"] > 0],
            }
            parts["margin"] = "ok"
            mdone = True
            break
    if not mdone:
        parts["margin"] = "error: 近 4 個交易日都抓不到"

    out["parts"] = parts
    bad = [k for k, v in parts.items() if v.startswith("error")]
    out["status"] = "ok" if not bad else ("error" if len(bad) == len(parts) else "partial")
    return out


# ─────────────────────────────────────────────
# 行事曆
# ─────────────────────────────────────────────
CAL_DAYS = 62      # 行事曆只往後看 62 天（約 2 個月）


def load_manual(today: date):
    """calendar_manual.json：你自己維護的事件（總經、休市、法說會…）"""
    for p in ("calendar_manual.json", os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_manual.json")):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    items = json.load(fh)
                out = []
                for it in items:
                    dt = _roc(it.get("date"))
                    if dt and today <= dt <= today + timedelta(days=CAL_DAYS):
                        out.append({"date": dt.isoformat(), "time": str(it.get("time", "")),
                                    "kind": str(it.get("kind", "macro")), "title": str(it.get("title", "")),
                                    "note": str(it.get("note", ""))})
                return out, True
            except Exception as e:
                logger.warning(f"讀取 calendar_manual.json 失敗：{e}")
                return [], False
    return [], False


def build_calendar(prev: dict, now: datetime) -> dict:
    today = now.date()
    man, found = load_manual(today)
    parts = {"manual": f"ok（{len(man)} 筆）" if found else "error: 找不到或讀不了 calendar_manual.json"}
    man.sort(key=lambda e: (e["date"], e["time"] or "99", e["title"]))
    return {"fetched_at": now.isoformat(timespec="seconds"), "events": man, "parts": parts,
            "status": "ok" if found else "partial"}


# ─────────────────────────────────────────────
# 社群（不限股票：看各種事件的討論熱度）
#   Threads（台灣主流，需官方 API token）→ PTT 熱門文章（依推文數）→ Reddit（美國）→ 微博熱搜（大陸）
# ─────────────────────────────────────────────
PTT_BOARDS = ["Gossiping", "HatePolitics", "Stock", "Baseball", "NBA"]
REDDIT_SUBS = [("popular", 12), ("worldnews", 8), ("technology", 8)]
THREADS_KEYWORDS = [k.strip() for k in os.environ.get(
    "THREADS_KEYWORDS", "台積電,颱風,地震,選舉,AI,漲價").split(",") if k.strip()]


def _feed(url):
    import feedparser
    r = _get(url, headers={"User-Agent": UA["User-Agent"], "Accept": "application/atom+xml,application/xml,*/*"}, gap=1.0)
    if r is None:
        return None
    fp = feedparser.parse(r.content)
    return fp.entries or None


def _ts(e) -> int:
    for k in ("updated_parsed", "published_parsed"):
        t = e.get(k)
        if t:
            try:
                return int(_cal.timegm(t))
            except Exception:
                pass
    return 0


def _ptt_push(txt: str) -> int:
    txt = (txt or "").strip()
    if txt == "爆":
        return 100
    if txt.startswith("X"):
        return -int(txt[1:]) if txt[1:].isdigit() else -10
    return int(txt) if txt.isdigit() else 0


def fetch_ptt(pages: int = 2):
    """PTT 網頁版索引頁（帶 over18 cookie），各板抓最新 pages 頁並保留推文數。失敗回 (None, 原因)"""
    from bs4 import BeautifulSoup
    posts, last_err = [], ""
    for board in PTT_BOARDS:
        url = f"https://www.ptt.cc/bbs/{board}/index.html"
        for _ in range(pages):
            r = _get(url, headers={"User-Agent": UA["User-Agent"], "Accept": "text/html,*/*"},
                     cookies={"over18": "1"}, gap=1.2)
            if r is None:
                last_err = f"{board} 連不上（非 200，雲端 IP 可能被 PTT 擋）"
                break
            soup = BeautifulSoup(r.text, "lxml")
            for ent in soup.select("div.r-ent"):
                a = ent.select_one("div.title a")
                if not a:
                    continue
                nr, dt = ent.select_one("div.nrec"), ent.select_one("div.date")
                t = _tags(a.get_text())
                m = re.match(r"\s*\[(.+?)\]", t)
                posts.append({"t": t, "u": "https://www.ptt.cc" + a.get("href", ""), "b": board,
                              "n": _ptt_push(nr.get_text() if nr else ""),
                              "d": _tags(dt.get_text()) if dt else "", "k": m.group(1) if m else ""})
            prev = next((x for x in soup.select("div.btn-group-paging a") if "上頁" in x.get_text()), None)
            if not prev or not prev.get("href"):
                break
            url = "https://www.ptt.cc" + prev["href"]
    if not posts:
        return None, last_err or "沒有抓到任何文章"
    posts.sort(key=lambda p: p["n"], reverse=True)
    return posts[:30], ""


def fetch_reddit():
    out, errs = {}, []
    for sub, n in REDDIT_SUBS:
        ent = None
        for host in ("www.reddit.com", "old.reddit.com"):
            ent = _feed(f"https://{host}/r/{sub}/hot/.rss?limit=25")
            if ent:
                break
        if ent:
            out[sub] = [{"t": _tags(e.get("title", "")), "u": e.get("link", ""), "p": _ts(e)} for e in ent[:n]]
        else:
            errs.append(sub)
    return out, errs


def fetch_weibo():
    """微博熱搜榜（公開 JSON）。失敗回 None"""
    from urllib.parse import quote
    hdr = {"User-Agent": UA["User-Agent"], "Accept": "application/json, text/plain, */*",
           "Referer": "https://weibo.com/", "Accept-Language": "zh-CN,zh;q=0.9"}
    j = _json("https://weibo.com/ajax/side/hotSearch", headers=hdr)
    try:
        lst = j["data"]["realtime"]
    except Exception:
        return None
    out = []
    for it in lst:
        w = str(it.get("word") or it.get("note") or "").strip()
        if not w or it.get("is_ad"):
            continue
        out.append({"t": w, "u": f"https://s.weibo.com/weibo?q=%23{quote(w)}%23",
                    "n": int(it.get("num") or 0), "tag": str(it.get("label_name") or "")})
    return out[:25] or None


def fetch_threads(token: str):
    """Threads 官方 API 關鍵字搜尋（熱門）。Threads 沒有公開的趨勢榜或 RSS。回 (list, 錯誤原因)"""
    posts, err = [], ""
    for kw in THREADS_KEYWORDS:
        j = _json("https://graph.threads.net/v1.0/keyword_search",
                  {"q": kw, "search_type": "TOP", "access_token": token,
                   "fields": "id,text,permalink,username,like_count,reply_count,timestamp"}, gap=1.0)
        if not isinstance(j, dict) or not j.get("data"):
            err = f"「{kw}」沒有回傳（token 過期、沒有 keyword_search 權限，或額度用完）"
            continue
        for it in j["data"][:6]:
            txt = _tags(it.get("text", ""))
            if not txt or not it.get("permalink"):
                continue
            posts.append({"t": txt[:90], "u": it["permalink"], "a": it.get("username", ""), "kw": kw,
                          "n": int(it.get("like_count") or 0) + 3 * int(it.get("reply_count") or 0)})
    posts.sort(key=lambda p: p["n"], reverse=True)
    return posts[:20], ("" if posts else err)


def build_social(prev: dict, now: datetime) -> dict:
    prev = prev or {}
    parts = {}
    out = {"fetched_at": now.isoformat(timespec="seconds")}

    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if token:
        th, err = fetch_threads(token)
        out["threads"] = th or prev.get("threads") or []
        parts["threads"] = "ok" if th else f"error: {err}"
    else:
        out["threads"] = prev.get("threads") or []
        parts["threads"] = "skip: 尚未設定 THREADS_ACCESS_TOKEN（Threads 沒有公開 RSS，必須用官方 API）"

    ptt, err = fetch_ptt()
    old_ptt = prev.get("ptt") if isinstance(prev.get("ptt"), list) else []
    out["ptt"] = ptt or old_ptt
    parts["ptt"] = "ok" if ptt else f"error: {err}"

    red, errs = fetch_reddit()
    old = prev.get("reddit") if isinstance(prev.get("reddit"), dict) else {}
    for sub, _n2 in REDDIT_SUBS:
        if sub not in red and old.get(sub):
            red[sub] = old[sub]
    out["reddit"] = red
    parts["reddit"] = "ok" if not errs else f"error: {', '.join(errs)} 抓不到（Reddit 常擋雲端 IP）"

    wb = fetch_weibo()
    out["weibo"] = wb or prev.get("weibo") or []
    parts["weibo"] = "ok" if wb else "error: 微博熱搜抓不到（保留上次內容）"

    out["parts"] = parts
    real = [v for v in parts.values() if not v.startswith("skip")]
    bad = [v for v in real if v.startswith("error")]
    out["status"] = "ok" if not bad else ("error" if len(bad) == len(real) else "partial")
    return out


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def run_job(path: str) -> None:
    old = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = json.load(fh)
        except Exception as e:
            logger.error(f"讀取舊 extras.json 失敗（當作空資料）：{e}")
    now = datetime.now(TZ_TW)
    force = os.environ.get("EXTRAS_FORCE", "") == "1"
    out = dict(old)
    for name, fn in (("chips", build_chips), ("calendar", build_calendar), ("social", build_social)):
        prev = old.get(name) or {}
        if not force and prev.get("status") == "ok" and _ago_min(prev.get("fetched_at", "")) < REFRESH_MIN[name]:
            logger.info(f"{name}：{_ago_min(prev['fetched_at']):.0f} 分鐘前才更新過，略過")
            continue
        try:
            out[name] = fn(prev, now)
            logger.info(f"{name}：{out[name].get('status')} {out[name].get('parts')}")
        except Exception as e:                                # 任何意外都不能讓整個流程失敗
            logger.exception(f"{name} 失敗")
            keep = dict(prev)
            keep.update(status=f"error: {type(e).__name__}: {e}", fetched_at=prev.get("fetched_at", ""))
            out[name] = keep
    out["generated_at"] = now.isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    logger.info(f"已輸出 {path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_job(sys.argv[1] if len(sys.argv) > 1 else "data_out/extras.json")
