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
  calendar  台股除權息、美股重點財報、calendar_manual.json 裡你自己維護的事件（總經、休市…）
  social    PTT 股票版、Reddit（wallstreetbets / stocks）
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
def _get(url, params=None, headers=None, timeout=25, gap=1.0):
    """禮貌地 GET：兩次請求至少間隔 gap 秒。失敗回 None。"""
    wait = gap - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    try:
        r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
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

    # 4) 注意股 / 處置股
    notice = fetch_announce("notice", today - timedelta(days=3), today)
    out["notice"] = notice
    parts["notice"] = "ok" if notice else "empty（可能今天沒有公布，或格式變動）"
    punish = fetch_announce("punish", today - timedelta(days=14), today + timedelta(days=14))
    out["punish"] = punish
    parts["punish"] = "ok" if punish else "empty（可能目前沒有處置股，或格式變動）"

    out["parts"] = parts
    bad = [k for k, v in parts.items() if v.startswith("error")]
    out["status"] = "ok" if not bad else ("error" if len(bad) == len(parts) else "partial")
    return out


# ─────────────────────────────────────────────
# 行事曆
# ─────────────────────────────────────────────
EARN_WATCH = {"NVDA", "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO", "AMD", "MU", "ORCL",
              "INTC", "QCOM", "TSM", "ASML", "ARM", "SMCI", "DELL", "MRVL", "PLTR", "NFLX", "CSCO", "AMAT",
              "LRCX", "KLAC", "COST", "CRM", "ADBE", "JPM", "GS", "WMT"}


def fetch_exdiv(today: date):
    """台股除權息預告（證交所 TWT48U）。回傳事件 list"""
    s, e = today.strftime("%Y%m%d"), (today + timedelta(days=45)).strftime("%Y%m%d")
    recs = []
    for base in (f"{TWSE}/rwd/zh/exRight/TWT48U", f"{TWSE}/exchangeReport/TWT48U"):
        j = _json(base, {"response": "json", "strDate": s, "startDate": s, "endDate": e})
        f, d = _rows(j)
        if d:
            recs = [dict(zip([_tags(x) for x in f], row)) for row in d]
            break
    if not recs:
        j = _json("https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL")
        if isinstance(j, list):
            recs = [r for r in j if isinstance(r, dict)]
    ev = []
    for r in recs:
        dt = _roc(_pick(r, "資料日期", "日期", "Date"))
        code = _pick(r, "股票代號", "代號", "Code")
        if not dt or not code or not (today <= dt <= today + timedelta(days=45)):
            continue
        kind = _pick(r, "除權息", "Exdividend") or "息"
        cash = _pick(r, "現金股利", "CashDividend")
        stock = _pick(r, "無償配股率", "StockDividend")
        note = " · ".join(x for x in (f"現金股利 {cash}" if cash and cash not in ("0", "0.0", "0.00000000") else "",
                                      f"配股率 {stock}" if stock and stock not in ("0", "0.0", "0.00000000") else "") if x)
        ev.append({"date": dt.isoformat(), "time": "", "kind": "exdiv",
                   "title": f"{code} {_pick(r, '股票名稱', '名稱', 'Name')} {kind if kind.startswith('除') else '除' + kind}", "note": note})
    ev.sort(key=lambda x: (x["date"], x["title"]))
    return ev[:150]


def fetch_earnings(today: date, days: int = 14):
    """美股財報（Nasdaq 公開 API），只留大型股 / 關注名單。日期為美東日期。"""
    hdr = dict(UA, Origin="https://www.nasdaq.com", Referer="https://www.nasdaq.com/")
    ev, tried, okn = [], 0, 0
    for i in range(days):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        tried += 1
        j = _json("https://api.nasdaq.com/api/calendar/earnings", {"date": d.isoformat()}, headers=hdr, gap=0.5)
        rows = ((j or {}).get("data") or {}).get("rows") if isinstance(j, dict) else None
        if rows is None:
            continue
        okn += 1
        for r in rows:
            sym = str(r.get("symbol", "")).upper()
            cap = _n(re.sub(r"[^\d.]", "", str(r.get("marketCap", ""))))
            if sym in EARN_WATCH or cap >= 2e11:
                t = str(r.get("time", ""))
                lab = "盤前" if "pre" in t else ("盤後" if "after" in t else "")
                eps = str(r.get("epsForecast", "") or "").strip()
                ev.append({"date": d.isoformat(), "time": lab, "kind": "earn",
                           "title": f"{sym} {_tags(r.get('name', ''))[:28]} 財報（美東）",
                           "note": f"EPS 預估 {eps}" if eps else ""})
    return ev, (okn > 0 or tried == 0)


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
                    if dt and dt >= today - timedelta(days=1):
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
    parts, events = {}, []
    ex = fetch_exdiv(today)
    parts["exdiv"] = "ok" if ex else "empty（可能近期沒有除權息，或格式變動）"
    events += ex
    er, ok = fetch_earnings(today)
    parts["earnings"] = "ok" if er else ("empty" if ok else "error: Nasdaq API 連不上")
    events += er
    man, found = load_manual(today)
    parts["manual"] = f"ok（{len(man)} 筆）" if found else "empty（找不到 calendar_manual.json）"
    events += man
    events.sort(key=lambda e: (e["date"], e["time"] or "99", e["title"]))
    out = {"fetched_at": now.isoformat(timespec="seconds"), "events": events[:300], "parts": parts}
    bad = [k for k, v in parts.items() if v.startswith("error")]
    out["status"] = "ok" if not bad else "partial"
    return out


# ─────────────────────────────────────────────
# 社群
# ─────────────────────────────────────────────
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


_CODE_RE = re.compile(r"(?<![\d.])(\d{4})(?![\d年月日%元點.])")


def build_social(prev: dict, now: datetime) -> dict:
    parts = {}
    out = {"fetched_at": now.isoformat(timespec="seconds")}

    # PTT 股票版
    ent = _feed("https://www.ptt.cc/atom/Stock.xml")
    old = {p["u"]: p for p in ((prev or {}).get("ptt") or {}).get("posts", [])}
    if ent:
        for e in ent:
            u = e.get("link", "")
            if not u:
                continue
            t = _tags(e.get("title", ""))
            m = re.match(r"\s*\[(.+?)\]", t)
            old[u] = {"t": t, "u": u, "a": _tags(e.get("author", "")), "p": _ts(e) or int(now.timestamp()),
                      "k": m.group(1) if m else ""}
        parts["ptt"] = "ok"
    else:
        parts["ptt"] = "error: PTT 連不上或沒有資料（保留上次內容）"
    cutoff = now.timestamp() - 72 * 3600
    posts = sorted((p for p in old.values() if p["p"] >= cutoff), key=lambda p: p["p"], reverse=True)[:300]
    cnt = {}
    for p in posts:
        if p["p"] < now.timestamp() - 24 * 3600:
            continue
        for c in set(_CODE_RE.findall(p["t"])):
            if 1990 <= int(c) <= 2035:
                continue
            cnt.setdefault(c, []).append(p)
    hot = sorted(cnt.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:10]
    out["ptt"] = {"posts": posts, "hot": [{"c": c, "n": len(v), "t": v[0]["t"], "u": v[0]["u"]} for c, v in hot if len(v) >= 2]}

    # Reddit
    red = {}
    for sub in ("wallstreetbets", "stocks"):
        ent = _feed(f"https://www.reddit.com/r/{sub}/hot/.rss?limit=25")
        if ent:
            red[sub] = [{"t": _tags(e.get("title", "")), "u": e.get("link", ""), "p": _ts(e)} for e in ent[:25]]
            parts[f"reddit_{sub}"] = "ok"
        else:
            red[sub] = ((prev or {}).get("reddit") or {}).get(sub, [])
            parts[f"reddit_{sub}"] = "error: Reddit 擋住或連不上（雲端 IP 常被限制）"
    tag = {}
    for lst in red.values():
        for p in lst:
            for s in set(re.findall(r"\$([A-Z]{1,5})\b", p["t"])):
                tag[s] = tag.get(s, 0) + 1
    out["reddit"] = red
    out["reddit_tags"] = [{"s": s, "n": n} for s, n in sorted(tag.items(), key=lambda kv: -kv[1])[:10] if n >= 2]
    out["parts"] = parts
    bad = [k for k, v in parts.items() if v.startswith("error")]
    out["status"] = "ok" if not bad else ("error" if len(bad) == len(parts) else "partial")
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
