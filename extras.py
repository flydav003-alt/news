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
  social    PTT 熱門、Reddit、微博熱搜
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
PROXY_URL = os.environ.get("PROXY_URL", "").strip().rstrip("/")
PROXY_KEY = os.environ.get("PROXY_KEY", "").strip()
# 這些網站對雲端機房 IP 不友善 → 先走 Cloudflare Worker，失敗再直連；證交所則相反（先直連，失敗才走代理）
PROXY_FIRST = {"www.ptt.cc", "s.weibo.com", "www.reddit.com", "old.reddit.com",
               "graph.threads.net", "www.bls.gov", "www.federalreserve.gov", "api.nasdaq.com"}
PROXY_AFTER = {"www.twse.com.tw", "openapi.twse.com.tw", "weibo.com"}   # weibo.com：代理一律 403，直連才通，所以改先直連


def _once(url, params, headers, timeout, cookies, via_proxy, proxy_extra=10):
    try:
        if via_proxy:
            full = requests.Request("GET", url, params=params).prepare().url
            h = dict(headers or UA)
            if cookies:
                h["X-Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            h["X-Proxy-Key"] = PROXY_KEY   # 金鑰放標頭，不再出現在網址 / Cloudflare 存取紀錄
            r = requests.get(PROXY_URL, params={"url": full}, headers=h, timeout=timeout + proxy_extra)
        else:
            r = requests.get(url, params=params, headers=headers or UA, timeout=timeout, cookies=cookies)
        if r.status_code != 200:
            logger.warning(f"HTTP {r.status_code}{'（代理）' if via_proxy else ''}：{url[:80]}")
            return None
        return r
    except Exception as e:
        logger.warning(f"請求失敗{'（代理）' if via_proxy else ''}：{url[:80]} → {type(e).__name__}: {e}")
        return None


def _get(url, params=None, headers=None, timeout=25, gap=1.0, cookies=None, deadline=None, proxy_extra=10):
    """禮貌地 GET：兩次請求至少間隔 gap 秒。依網站決定直連或走代理。失敗回 None。"""
    from urllib.parse import urlparse
    wait = gap - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    host = urlparse(url).hostname or ""
    if PROXY_URL and host in PROXY_FIRST:
        order = (True, False)
    elif PROXY_URL and host in PROXY_AFTER:
        order = (False, True)
    else:
        order = (False,)
    for via in order:
        tmo = timeout
        if deadline is not None:                      # 有整體時間上限：剩多少時間就最多等多少，用完就不再試
            rem = deadline - time.time()
            if rem <= 0.5:
                break
            tmo = min(timeout, rem)
        r = _once(url, params, headers, tmo, cookies, via, proxy_extra)
        if r is not None:
            return r
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
CAL_DAYS = 62      # 行事曆只往後看 62 天（約 2 個月），每次執行都重新計算，所以新事件會隨時間自動滾進來
HTML_HDR = {"User-Agent": UA["User-Agent"], "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9"}
_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _et_to_tw(d: date, hh: int, mm: int):
    """美東時間 → (台灣日期 iso, 'HH:MM')，自動處理夏令時間"""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo("America/New_York")).astimezone(TZ_TW)
    except Exception:
        dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ_TW) + timedelta(hours=13)
    return dt.date().isoformat(), dt.strftime("%H:%M")


# ── 美股休市日：用規則算，不用上網 ──
def _nth_wd(y, m, wd, n):
    if n > 0:
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))
        return d
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo, da = divmod(h + l - 7 * m + 114, 31)
    return date(y, mo, da + 1)


def us_market_days(y: int):
    """回傳 [(date, kind, 名稱)]，kind = 'closed' | 'early'（13:00 ET 收盤）"""
    def obs(d, newyear=False):
        if d.weekday() == 5:
            return None if newyear else d - timedelta(days=1)
        return d + timedelta(days=1) if d.weekday() == 6 else d
    out = []
    for d, name in ((date(y, 1, 1), "元旦"), (date(y, 6, 19), "六月節"), (date(y, 7, 4), "獨立紀念日"),
                    (date(y, 12, 25), "聖誕節")):
        o = obs(d, newyear=(name == "元旦"))
        if o:
            out.append((o, "closed", name))
    out += [(_nth_wd(y, 1, 0, 3), "closed", "馬丁路德金恩日"), (_nth_wd(y, 2, 0, 3), "closed", "總統日"),
            (_easter(y) - timedelta(days=2), "closed", "耶穌受難日"), (_nth_wd(y, 5, 0, -1), "closed", "陣亡將士紀念日"),
            (_nth_wd(y, 9, 0, 1), "closed", "勞動節")]
    tg = _nth_wd(y, 11, 3, 4)
    out.append((tg, "closed", "感恩節"))
    out.append((tg + timedelta(days=1), "early", "感恩節隔日"))
    d24 = date(y, 12, 24)
    if d24.weekday() < 5 and date(y, 12, 25).weekday() != 5:
        out.append((d24, "early", "平安夜"))
    j3 = date(y, 7, 3)
    if j3.weekday() < 4 and date(y, 7, 4).weekday() < 5:
        out.append((j3, "early", "獨立紀念日前一天"))
    return out


def build_holidays(today: date):
    ev = []
    for y in (today.year, today.year + 1):
        for d, kind, name in us_market_days(y):
            if not (today <= d <= today + timedelta(days=CAL_DAYS)):
                continue
            if kind == "closed":
                ev.append({"date": d.isoformat(), "time": "", "kind": "holiday",
                           "title": f"美股休市（{name}）", "note": "NYSE / Nasdaq 全天休市"})
            else:
                dd, tt = _et_to_tw(d, 13, 0)
                ev.append({"date": d.isoformat(), "time": "", "kind": "holiday",
                           "title": f"美股提早收盤（{name}）", "note": f"美東 13:00 收盤＝台灣 {dd[5:].replace('-', '/')} {tt}"})
    return ev


# ── FOMC：Fed 官網 ──
def fetch_fomc(today: date):
    r = _get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers=HTML_HDR)
    if r is None:
        return None
    from bs4 import BeautifulSoup
    lines = [x.strip() for x in BeautifulSoup(r.text, "lxml").get_text("\n").split("\n") if x.strip()]
    year = month = None
    ev = []
    for ln in lines:
        m = re.search(r"(20\d\d)\s+FOMC\s+Meetings", ln)
        if m and len(ln) < 40:
            year, month = int(m.group(1)), None
            continue
        if year is None:
            continue
        mm = re.fullmatch(r"([A-Za-z]{3,9})(?:/([A-Za-z]{3,9}))?", ln)
        if mm and (mm.group(2) or mm.group(1))[:3].lower() in _MON:
            month = _MON[(mm.group(2) or mm.group(1))[:3].lower()]
            continue
        dm = re.fullmatch(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\*?", ln)
        if dm and month:
            try:
                d = date(year, month, int(dm.group(2)))
            except ValueError:
                continue
            if today <= d <= today + timedelta(days=CAL_DAYS):
                dd, tt = _et_to_tw(d, 14, 0)
                ev.append({"date": dd, "time": tt, "kind": "macro",
                           "title": f"FOMC 利率決議（{dm.group(1)}–{dm.group(2)} 日會議）",
                           "note": f"美東 {d.month}/{d.day} 14:00；來源：Fed 官方行程"})
            month = None
    return ev or None


# ── 非農 / CPI：FRED（需免費 API key）→ BLS 官網 ──
def _macro_events(dates, title, src, today):
    ev = []
    for d, hh, mm in dates:
        if today <= d <= today + timedelta(days=CAL_DAYS):
            dd, tt = _et_to_tw(d, hh, mm)
            ev.append({"date": dd, "time": tt, "kind": "macro", "title": title,
                       "note": f"美東 {d.month}/{d.day} {hh}:{mm:02d}；來源：{src}"})
    return ev


def fetch_fred_dates(release_id: int, key: str, today: date):
    j = _json("https://api.stlouisfed.org/fred/release/dates",
              {"release_id": release_id, "api_key": key, "file_type": "json", "sort_order": "asc",
               "realtime_start": today.isoformat(), "realtime_end": "9999-12-31",
               "include_release_dates_with_no_data": "true"})
    try:
        return [(date.fromisoformat(x["date"]), 8, 30) for x in j["release_dates"]]
    except Exception:
        return None


def fetch_bls_dates(slug: str):
    r = _get(f"https://www.bls.gov/schedule/news_release/{slug}.htm", headers=HTML_HDR)
    if r is None:
        return None
    from bs4 import BeautifulSoup
    out = []
    for tr in BeautifulSoup(r.text, "lxml").select("tr"):
        txt = " ".join(td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"]))
        m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(20\d\d)", txt)
        if not m or m.group(1)[:3].lower() not in _MON:
            continue
        t = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", txt)
        hh, mi = (int(t.group(1)) % 12 + (12 if t.group(3) == "PM" else 0), int(t.group(2))) if t else (8, 30)
        try:
            out.append((date(int(m.group(3)), _MON[m.group(1)[:3].lower()], int(m.group(2))), hh, mi))
        except ValueError:
            pass
    return out or None


def fetch_macro(label, fred_id, slug, title, today):
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        d = fetch_fred_dates(fred_id, key, today)
        if d:
            return _macro_events(d, title, "FRED / BLS 發布行程", today)
    d = fetch_bls_dates(slug)
    return _macro_events(d, title, "BLS 官方行程", today) if d else None


# ── 輝達財報：Nasdaq ──
def fetch_nvda(today: date):
    hdr = dict(UA, Origin="https://www.nasdaq.com", Referer="https://www.nasdaq.com/")
    j = _json("https://api.nasdaq.com/api/analyst/NVDA/earnings-date", headers=hdr)
    try:
        txt = json.dumps(j.get("data") or {}, ensure_ascii=False)
        m = re.search(r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),\s+(20\d\d)", txt)
        d = date(int(m.group(3)), _MON[m.group(1).lower()], int(m.group(2)))
    except Exception:
        return None
    if not (today <= d <= today + timedelta(days=CAL_DAYS)):
        return []
    return [{"date": d.isoformat(), "time": "", "kind": "earn", "title": "輝達（NVDA）財報",
             "note": "美東盤後公布，台灣時間為隔天清晨；日期來源 Nasdaq，可能是預估"}]


def _cat(title: str) -> str:
    t = title.upper()
    if "休市" in title or "提早收盤" in title:
        return "holiday"
    for k, c in (("FOMC", "fomc"), ("非農", "nfp"), ("NFP", "nfp"), ("CPI", "cpi"), ("NVDA", "nvda"), ("輝達", "nvda")):
        if k in t:
            return c
    return "other"


def load_manual(today: date):
    """calendar_manual.json：備援＋臨時事件。某類事件自動抓失敗時才會用到同類的手動資料；沒分到類的一律顯示"""
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
    parts, auto = {}, {}
    auto["holiday"] = build_holidays(today)
    for cat, label, fn in (
        ("fomc", "FOMC", lambda: fetch_fomc(today)),
        ("nfp", "非農", lambda: fetch_macro("非農", 50, "empsit", "美國非農就業報告（NFP）", today)),
        ("cpi", "CPI", lambda: fetch_macro("CPI", 10, "cpi", "美國 CPI", today)),
        ("nvda", "輝達財報", lambda: fetch_nvda(today)),
    ):
        try:
            r = fn()
        except Exception as e:
            logger.warning(f"{label} 例外：{type(e).__name__}: {e}")
            r = None
        auto[cat] = r
    man, found = load_manual(today)
    events = list(auto["holiday"])
    parts["holiday"] = f"ok（規則計算 {len(auto['holiday'])} 筆）"
    for cat, label in (("fomc", "FOMC"), ("nfp", "非農"), ("cpi", "CPI"), ("nvda", "輝達財報")):
        if auto[cat] is not None:
            events += auto[cat]
            extra = 0
            if cat in ("nfp", "cpi"):                     # 自動資料沒有的日期，用手動檔補上（日期相同不重複）
                have = {e["date"] for e in auto[cat]}
                for e in man:
                    if _cat(e["title"]) == cat and e["date"] not in have:
                        events.append(e)
                        extra += 1
            parts[cat] = f"ok（{len(auto[cat])} 筆" + (f"，手動補 {extra} 筆）" if extra else "）")
        else:
            fb = [e for e in man if _cat(e["title"]) == cat]
            events += fb
            parts[cat] = f"error: {label} 自動抓取失敗，改用手動檔 {len(fb)} 筆"
    events += [e for e in man if _cat(e["title"]) == "other"]
    events.sort(key=lambda e: (e["date"], e["time"] or "99", e["title"]))
    bad = [v for v in parts.values() if v.startswith("error")]
    return {"fetched_at": now.isoformat(timespec="seconds"), "events": events[:300], "parts": parts,
            "status": "ok" if not bad else "partial"}


# ─────────────────────────────────────────────
# 社群（不限股票：看各種事件的討論熱度）
#   PTT 熱門文章（依推文數；雲端 IP 目前被擋）、Reddit（美國）、微博熱搜（大陸）
# ─────────────────────────────────────────────
PTT_ENABLED = False
PTT_BOARDS = ["Gossiping", "HatePolitics", "Stock", "Baseball", "NBA"]
REDDIT_SUBS = [("popular", 12), ("worldnews", 8), ("technology", 8)]
REDDIT_HOSTS = ("www.reddit.com",)      # old.reddit.com 在歷次 log 都是 403，先不試（要的話加回 "old.reddit.com"）
REDDIT_GAP = 2.0                        # 兩次 Reddit 請求至少間隔幾秒
REDDIT_TIMEOUT = 6                      # 單次請求最多等幾秒（代理也一樣，不再額外加 10 秒）
REDDIT_BUDGET = 20                      # 整段 Reddit 抓取最多花幾秒，用完就放棄、沿用舊資料


def _feed(url, gap=1.0, **kw):
    import feedparser
    r = _get(url, headers={"User-Agent": UA["User-Agent"], "Accept": "application/atom+xml,application/xml,*/*"}, gap=gap, **kw)
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
    deadline = time.time() + REDDIT_BUDGET
    for sub, n in REDDIT_SUBS:
        ent = None
        if time.time() < deadline:
            for host in REDDIT_HOSTS:
                ent = _feed(f"https://{host}/r/{sub}/hot/.rss?limit=25", gap=REDDIT_GAP,
                            timeout=REDDIT_TIMEOUT, deadline=deadline, proxy_extra=0)
                if ent:
                    break
        else:
            logger.warning(f"Reddit 時間預算（{REDDIT_BUDGET} 秒）用完，略過 r/{sub}，沿用舊資料")
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
        out.append({"t": w, "u": "https://m.weibo.cn/search?containerid=" + quote(f"100103type=1&q=#{w}#", safe=""),
                    "n": int(it.get("num") or 0), "tag": str(it.get("label_name") or "")})
    out = out[:25]
    try:
        from opencc import OpenCC
        cc = OpenCC("s2twp")
        for p in out:
            p["t"] = cc.convert(p["t"])
    except Exception:
        pass
    return out or None


def translate_reddit(red: dict, old: dict) -> None:
    """替 Reddit 標題加上 z（繁體中文）；已翻過的沿用上次結果。先用 Groq，失敗再用 Google 翻譯。"""
    cache = {p["u"]: p.get("z", "") for lst in old.values() for p in lst if p.get("z")}
    todo = []
    for lst in red.values():
        for p in lst:
            if cache.get(p["u"]):
                p["z"] = cache[p["u"]]
            elif not p.get("z"):
                todo.append(p)
    if not todo:
        return
    groq_n = 0
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        try:
            from groq_analyzer import groq_translate_titles
            for i in range(0, len(todo), 25):
                chunk = todo[i:i + 25]
                out = groq_translate_titles([p["t"] for p in chunk], key) or []
                for p, z in zip(chunk, out):
                    if z and z != p["t"]:
                        p["z"] = z
                        groq_n += 1
        except Exception as e:
            logger.warning(f"Reddit 標題 Groq 翻譯失敗：{type(e).__name__}: {e}")
    else:
        logger.warning("Reddit 標題翻譯：沒有 GROQ_API_KEY，只能用 Google 翻譯")
    left = [p for p in todo if not p.get("z")]
    g_n, err = 0, ""
    if left:
        try:
            from deep_translator import GoogleTranslator
            tr = GoogleTranslator(source="auto", target="zh-TW")
            for p in left:
                try:
                    time.sleep(0.3)
                    z = tr.translate(p["t"]) or ""
                    if z and z != p["t"]:
                        p["z"] = z
                        g_n += 1
                except Exception as e:
                    err = err or f"{type(e).__name__}: {e}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if err:
        logger.warning(f"Reddit 標題 Google 翻譯失敗（只記第一個錯誤）：{err}")
    logger.info(f"Reddit 標題翻譯 {groq_n + g_n}/{len(todo)}（Groq {groq_n}、Google {g_n}）")


def build_social(prev: dict, now: datetime) -> dict:
    prev = prev or {}
    parts = {}
    out = {"fetched_at": now.isoformat(timespec="seconds")}

    # PTT 已停用（雲端 IP 一律 403，且畫面不再使用）。要重新啟用，把 PTT_ENABLED 改成 True
    if PTT_ENABLED:
        ptt, err = fetch_ptt()
        old_ptt = prev.get("ptt") if isinstance(prev.get("ptt"), list) else []
        out["ptt"] = ptt or old_ptt
        parts["ptt"] = "ok" if ptt else f"error: {err}"
    else:
        out["ptt"] = []
        parts["ptt"] = "skip: 已停用"

    red, errs = fetch_reddit()
    old = prev.get("reddit") if isinstance(prev.get("reddit"), dict) else {}
    now_iso = now.isoformat(timespec="seconds")
    rat = dict(prev.get("reddit_at") or {}) if isinstance(prev.get("reddit_at"), dict) else {}
    for sub in red:                                  # 這次真的抓到的板：記下時間
        rat[sub] = now_iso
    stale = []
    for sub, _n2 in REDDIT_SUBS:
        if sub not in red and old.get(sub):
            red[sub] = old[sub]
            stale.append(sub)                        # 沿用上次內容
    out["reddit_at"], out["reddit_stale"] = rat, stale
    translate_reddit(red, old)
    out["reddit"] = red
    parts["reddit"] = "ok" if not errs else f"error: {', '.join(errs)} 抓不到（Reddit 常擋雲端 IP）"

    wb = fetch_weibo()
    out["weibo"] = wb or prev.get("weibo") or []
    out["weibo_at"] = now_iso if wb else (prev.get("weibo_at") or "")
    out["weibo_stale"] = not wb
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
