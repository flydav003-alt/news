"""
app.py - FinNews AI v3.0
讀 GitHub Actions 產生的 news.json · 行情條 · 頭條大圖 · 卡片網格 · 英文標題中譯
"""

import html as _html
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from database import (init_db, SessionLocal, get_articles_df,
                      get_sector_counts, get_ticker_counts, import_json, TZ_TW)
from scheduler import crawl_and_save
from crawler import SOURCES, is_routine_revenue

esc = _html.escape


# ─────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────
def _secret(name: str, default: str = "") -> str:
    try:
        v = st.secrets.get(name, "")
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, default)


def _h(s: str) -> str:
    """去掉每行縮排與換行：避免 Streamlit markdown 把縮排 HTML 當成程式碼區塊"""
    return "".join(line.strip() for line in s.splitlines())


def age_hours(dt) -> float:
    try:
        return max(0.0, (datetime.now(TZ_TW) - dt.astimezone(TZ_TW)).total_seconds() / 3600)
    except Exception:
        return 9999.0


def relative_time(dt) -> str:
    if dt is None:
        return ""
    h = age_hours(dt)
    if h >= 9999:
        return ""
    secs = h * 3600
    if secs < 60:
        return "剛剛"
    if secs < 3600:
        return f"{int(secs // 60)}分鐘前"
    if secs < 86400:
        return f"{int(secs // 3600)}小時前"
    if secs < 86400 * 2:
        return "昨天"
    return dt.astimezone(TZ_TW).strftime("%m/%d %H:%M")


def recent(df, hours_steps=(12, 24, 48, 72), min_n=8):
    """先取 12h，不足 min_n 則逐步放寬，絕不回傳空畫面。回傳 (df, 實際小時數)"""
    if df is None or df.empty:
        return df, 0
    ages = df["published_at"].apply(age_hours)
    for hrs in hours_steps:
        sub = df[ages <= hrs]
        if len(sub) >= min_n:
            return sub, hrs
    return df[ages <= hours_steps[-1]] if (ages <= hours_steps[-1]).any() else df, hours_steps[-1]


def _num(v, d=0.0) -> float:
    try:
        f = float(v)
        return d if f != f else f
    except Exception:
        return d


# ─────────────────────────────────────────────
# 頁面設定 + CSS（米色底 #FCF8F0，台灣紅漲綠跌）
# ─────────────────────────────────────────────
st.set_page_config(page_title="FinNews AI", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root{
  --bg:#FCF8F0; --card:#FFFFFF; --soft:#F5EEDF; --line:#EADFCB;
  --tx:#2B2418; --tx2:#7A6E5A; --tx3:#A89C86;
  --up:#C0392B; --up-bg:#FBE9E5; --dn:#1B7A34; --dn-bg:#E4F2E8;
  --warn:#92400E; --warn-bg:#FFF3D6; --accent:#B8742A;
}
html,body,[class*="css"],.stApp{font-family:'Noto Sans TC',sans-serif!important;background:var(--bg)!important;color:var(--tx)!important}
header[data-testid="stHeader"]{display:none!important}
section[data-testid="stSidebar"],button[data-testid="collapsedControl"]{display:none!important}
.main .block-container,[data-testid="stMain"] .block-container{padding-top:8px!important;padding-bottom:30px!important;max-width:1320px!important}
hr{border-color:var(--line)!important;margin:10px 0!important}

/* topbar */
.topbar{display:flex;align-items:center;gap:12px;padding:8px 2px;flex-wrap:wrap}
.logo{font-size:18px;font-weight:700;letter-spacing:.3px}
.pill{font-size:12px;border-radius:20px;padding:2px 10px;white-space:nowrap}
.pill-ok{background:var(--dn-bg);color:var(--dn)}
.pill-warn{background:var(--warn-bg);color:var(--warn)}
.tmono{font-size:12px;color:var(--tx2);font-family:'JetBrains Mono',monospace}

/* 行情條 */
.qbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:6px 0 12px}
.q{display:block;text-decoration:none!important;color:inherit!important;transition:box-shadow .15s,border-color .15s;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.q:hover{border-color:var(--accent);box-shadow:0 4px 12px rgba(80,60,20,.12)}
.q-n{font-size:12px;color:var(--tx2)}
.q-p{font-size:17px;font-weight:700;font-family:'JetBrains Mono',monospace;line-height:1.3}
.q-c{font-size:12px;font-family:'JetBrains Mono',monospace;font-weight:600}
.up{color:var(--up)} .dn{color:var(--dn)} .flat{color:var(--tx3)}

/* 指標 */
.mrow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.m{background:var(--soft);border-radius:10px;padding:8px 14px;min-width:96px}
a.m{display:block;color:inherit!important;cursor:pointer;transition:box-shadow .15s}
a.m:hover{box-shadow:0 2px 8px rgba(80,60,20,.12)}
.m-l{font-size:11px;color:var(--tx2)} .m-v{font-size:19px;font-weight:700}
.sbar{display:flex;height:8px;border-radius:6px;overflow:hidden;margin:6px 0 2px;background:var(--line)}
.sbar i{display:block;height:100%}

.sec{font-size:13px;font-weight:700;color:var(--tx2);margin:18px 0 8px;display:flex;align-items:center;gap:8px;letter-spacing:.5px}
.sec::after{content:'';flex:1;height:1px;background:var(--line)}

/* AI 總結 */
.ai-card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:0 12px 12px 0;padding:14px 18px}
.ai-badge{font-size:11px;font-weight:700;color:var(--warn);background:var(--warn-bg);border-radius:4px;padding:2px 8px;display:inline-block;margin-bottom:8px}
.ai-dir{font-size:16px;font-weight:700}
.ai-rsn{font-size:12px;color:var(--tx2);margin:2px 0 10px}
.tag{display:inline-block;border-radius:6px;padding:3px 10px;font-size:12px;font-weight:500;margin:0 6px 6px 0}
.tag-b{background:var(--up-bg);color:var(--up)} .tag-d{background:var(--dn-bg);color:var(--dn)}
.chip-t{display:inline-block;background:var(--soft);border-radius:4px;padding:2px 8px;font-size:12px;font-family:'JetBrains Mono',monospace;margin:0 5px 5px 0}
.ai-body{font-size:14px;line-height:1.8;border-top:1px solid var(--line);padding-top:10px;margin-top:4px}
.ai-ft{font-size:12px;color:var(--tx3);margin-top:8px}

/* 縮圖（圖片壞掉時，底下的漸層與文字佔位會露出來）*/
.thumb{position:relative;overflow:hidden;background:linear-gradient(135deg,#E9DFC9,#D8C8A6)}
.thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:rgba(255,255,255,.85);letter-spacing:1px;font-family:'JetBrains Mono',monospace}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}

/* 頭條區 */
.hero{display:grid;grid-template-columns:1.6fr 1fr;gap:14px;margin-bottom:6px}
@media(max-width:820px){.hero{grid-template-columns:1fr}}
.hero-main{position:relative;display:block;border-radius:14px;overflow:hidden;min-height:330px;text-decoration:none!important}
.hero-main .thumb{position:absolute;inset:0}
.hero-ov{position:absolute;left:0;right:0;bottom:0;padding:60px 20px 18px;background:linear-gradient(to top,rgba(20,14,4,.88),rgba(20,14,4,0))}
.hero-t{font-size:22px;font-weight:700;line-height:1.45;color:#fff!important}
.hero-o{font-size:12px;color:rgba(255,255,255,.7);margin-top:4px}
.hero-m{font-size:12px;color:rgba(255,255,255,.8);margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.hero-side{display:flex;flex-direction:column;gap:10px}
.si{display:flex;gap:10px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px;text-decoration:none!important;color:var(--tx)!important;flex:1;align-items:center}
.si:hover{border-color:var(--accent)}
.si .thumb{width:96px;height:72px;border-radius:8px;flex-shrink:0}
.si-t{font-size:13.5px;font-weight:500;line-height:1.45;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.si-m{font-size:11px;color:var(--tx3);margin-top:3px}

/* 卡片網格 */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;transition:box-shadow .15s,transform .15s}
.card{box-shadow:0 1px 3px rgba(80,60,20,.06)}
.card:hover{box-shadow:0 8px 22px rgba(80,60,20,.14);transform:translateY(-2px)}
.card .thumb{height:150px;display:block}
.card>a{text-decoration:none!important}
.strip{height:30px;display:flex;align-items:center;padding:0 13px;font-size:11.5px;font-weight:700;color:rgba(255,255,255,.95);letter-spacing:.5px}
.grid{align-items:start}
.card.read,.si.read{opacity:.55;transition:opacity .15s} .card.read:hover,.si.read:hover{opacity:1}
.ct:visited{color:var(--tx3)!important}
.fn-reco-wrap{margin-top:6px}
.fn-reco-chips{font-weight:500;color:var(--accent);margin-left:6px}
.fn-reco-clear{margin-left:auto;font-size:11.5px;font-weight:500;color:var(--tx3)!important;text-decoration:none!important;cursor:pointer}
.fn-reco-clear:hover{color:var(--accent)!important}
.fn-reco-note{font-size:12.5px;color:var(--tx2);margin:-2px 0 8px}
.fn-reco:empty{display:none}
.card.reco{border-color:var(--accent)}
.more{font-size:12px;color:var(--tx2);margin-top:2px}
.more summary{cursor:pointer;color:var(--accent);font-weight:600;list-style:none}
.more summary::-webkit-details-marker{display:none}
.more ul{margin:6px 0 0;padding-left:16px}
.more li{margin:4px 0;line-height:1.45}
.more li a{color:var(--tx)!important;text-decoration:none!important}
.more li a:hover{color:var(--accent)!important}
.more li span{color:var(--tx3)}
.seg{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px}
.subhead{font-size:12.5px;color:var(--tx2);margin:2px 0 8px}
.ph{font-size:20px}
.card.bull{border-top:3px solid var(--up)} .card.bear{border-top:3px solid var(--dn)}
.cb{padding:11px 13px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.ct{font-size:14.5px;font-weight:700;line-height:1.5;color:var(--tx)!important;text-decoration:none!important;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.ct:hover{color:var(--accent)!important}
.co{font-size:11.5px;color:var(--tx3);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cs{font-size:12.5px;color:var(--tx2);line-height:1.55;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.cm{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--tx3);margin-top:auto}
.bd{font-size:11.5px;font-weight:700;border-radius:5px;padding:1px 8px;font-family:'JetBrains Mono',monospace;white-space:nowrap}
.bd-b{background:var(--up-bg);color:var(--up)} .bd-d{background:var(--dn-bg);color:var(--dn)} .bd-n{background:var(--soft);color:var(--tx3)}
.bd-g{background:var(--warn-bg);color:var(--warn)}
.tk{font-size:11px;background:var(--soft);border-radius:4px;padding:1px 6px;font-family:'JetBrains Mono',monospace;color:var(--tx2)}
.tr{font-size:11.5px;color:var(--accent)!important;text-decoration:none!important}
.tr:hover{text-decoration:underline!important}
.fav{margin-left:auto;cursor:pointer;font-size:18px;line-height:1;color:var(--tx3);user-select:none;padding:0 2px}
.fav:hover{color:var(--accent)}
.fav.on{color:#E0A020}
.favc{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 1px 3px rgba(80,60,20,.06);display:flex;flex-direction:column}
.fn-fav:empty{display:none}
.fn-fav-note{font-size:12.5px;color:var(--tx2);margin:2px 0 10px}
.fav-clear{cursor:pointer;color:var(--tx3);margin-left:10px;text-decoration:underline}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.sg{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:10px 13px}
.sg-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sg-tk{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:15px}
.sg-nm{font-size:13px;color:var(--tx2)}
.sg-x{margin-left:auto;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:15px;color:var(--accent)}
.sg-m{font-size:11.5px;color:var(--tx3);margin:4px 0 6px}
.sg-l{margin:0;padding-left:16px;font-size:12.5px;line-height:1.5}
.sg-l a{color:var(--tx)!important;text-decoration:none!important}
.sg-l a:hover{color:var(--accent)!important}
.rkgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.rk{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 13px}
.rk-h{font-size:12.5px;font-weight:700;color:var(--tx2);margin-bottom:6px}
.rk-e{font-size:12px;color:var(--tx3)}
.rk-r{display:flex;align-items:center;gap:8px;font-size:12.5px;margin:4px 0}
.rk-c{width:46px;font-family:'JetBrains Mono',monospace;font-weight:600;flex-shrink:0}
.rk-c a{color:var(--tx)!important;text-decoration:none!important}
.rk-c a:hover{color:var(--accent)!important}
.rk-n{width:84px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--tx2)}
.rk-b{flex:1;background:var(--soft);height:10px;border-radius:5px;overflow:hidden;min-width:30px}
.rk-b i{display:block;height:100%;border-radius:5px}
.rk-v{width:70px;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:600;flex-shrink:0}
.tblw{overflow-x:auto;margin:6px 0}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.tbl th{background:var(--soft);color:var(--tx2);font-weight:600;text-align:right;padding:6px 10px;white-space:nowrap}
.tbl td{padding:6px 10px;border-top:1px solid var(--line);text-align:right;font-family:'JetBrains Mono',monospace}
.tbl th:first-child,.tbl td:first-child,.tbl td.l{text-align:left}
.tbl td.l{font-family:inherit}
.tbl a{color:var(--tx)!important;text-decoration:none!important}
.parts{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:11.5px;color:var(--tx3);margin:10px 0}
.cal-d{font-size:13px;margin:14px 0 4px;font-weight:700}
.cal-d span{font-weight:500;color:var(--tx3);margin-left:8px;font-size:12px}
.cal-d.today{color:var(--accent)}
.cal-r{display:flex;gap:10px;align-items:baseline;padding:6px 10px;background:var(--card);border:1px solid var(--line);border-radius:8px;margin:4px 0;font-size:13px;flex-wrap:wrap}
.cal-t{font-family:'JetBrains Mono',monospace;color:var(--tx3);min-width:42px;font-size:12px}
.cal-n{font-weight:600}
.cal-x{color:var(--tx3);font-size:12px}
.subhead{font-size:12.5px;color:var(--tx2);margin:6px 0 4px}
.pk{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--soft);color:var(--tx2);white-space:nowrap}
.post{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--line);font-size:13px;align-items:baseline}
.post a{color:var(--tx)!important;text-decoration:none!important}
.post a:hover{color:var(--accent)!important}
.post .pt{color:var(--tx3);font-size:11.5px;white-space:nowrap;margin-left:auto;padding-left:8px}
.lg{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.fn-new:empty{display:none}
.fn-new{display:flex;align-items:center;gap:10px;background:var(--soft);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:6px 12px;font-size:13px;margin:4px 0 10px}
.fn-new .tg{margin-left:auto;cursor:pointer;color:var(--accent);font-weight:600;white-space:nowrap}
.card.new{box-shadow:-3px 0 0 var(--accent)}
.card.new .ct::before{content:"NEW ";color:var(--accent);font-size:10.5px;font-weight:800;letter-spacing:.5px}
.fn-dens{display:flex;justify-content:flex-end;gap:4px;margin:2px 0 6px}
.fn-dens span{cursor:pointer;font-size:12px;padding:3px 10px;border:1px solid var(--line);border-radius:14px;color:var(--tx2);user-select:none;background:var(--card)}
.fn-dens span.on{background:var(--accent);color:#fff;border-color:var(--accent)}
html[data-fnjs] .card[data-lz]{display:none}
body.fn-onlynew .wall .card:not(.new){display:none!important}
body.fn-onlynew .wall .card.new[data-lz]{display:flex!important}
.fn-sentinel{height:1px}
body.fn-compact .grid{grid-template-columns:1fr;gap:0}
body.fn-compact .card{flex-direction:row;border-radius:0;border-width:0 0 1px 0;box-shadow:none;transform:none!important;align-items:center}
body.fn-compact .card:hover{box-shadow:none;background:var(--soft)}
body.fn-compact .card.bull{border-top:0;border-left:3px solid var(--up)}
body.fn-compact .card.bear{border-top:0;border-left:3px solid var(--dn)}
body.fn-compact .card>a:first-child{display:none}
body.fn-compact .cb{flex-direction:row;align-items:center;gap:10px;padding:7px 12px;flex-wrap:nowrap;flex:1;min-width:0}
body.fn-compact .ct{-webkit-line-clamp:1;flex:1;min-width:0;font-size:14px;font-weight:600}
body.fn-compact .co,body.fn-compact .cs,body.fn-compact .more{display:none}
body.fn-compact .cm-a{order:-1;flex-shrink:0;margin-top:0;flex-wrap:nowrap}
body.fn-compact .cm-a .tk~.tk{display:none}
body.fn-compact .cm-a .bd-g{display:none}
body.fn-compact .cm-b{flex-shrink:0;margin-top:0;white-space:nowrap}
body.fn-compact .cm-b .tr{display:none}
@media(max-width:700px){body.fn-compact .cb{flex-wrap:wrap}body.fn-compact .ct{flex-basis:100%;order:1}body.fn-compact .cm-a{order:0}body.fn-compact .cm-b{order:2}}

/* 地緣 */
.geo-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
@media(max-width:700px){.geo-grid{grid-template-columns:1fr}}
.geo{background:var(--warn-bg);border:1px solid #F1DCA0;border-left:4px solid var(--warn);border-radius:8px;padding:8px 12px;font-size:13px;line-height:1.5}
.geo a{color:var(--warn)!important;text-decoration:none!important;font-weight:500}

/* 橫條圖 */
.hb{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12.5px}
.hb-l{width:86px;color:var(--tx);font-family:'JetBrains Mono',monospace;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hb-t{flex:1;background:var(--line);height:14px;border-radius:7px;overflow:hidden}
.hb-t i{display:block;height:100%;border-radius:7px}
.hb-v{width:70px;color:var(--tx2);font-family:'JetBrains Mono',monospace;font-size:12px}

.empty{text-align:center;padding:40px;color:var(--tx3)}

/* Streamlit 元件 */
[role="tablist"]{gap:8px!important;padding:8px 2px 10px!important;flex-wrap:wrap;position:sticky;top:0;z-index:999;
  background:var(--bg);border-bottom:2px solid var(--line)!important}
[role="tab"]{height:auto!important;padding:9px 20px!important;border-radius:12px!important;background:var(--card)!important;
  border:1px solid var(--line)!important;color:var(--tx)!important;box-shadow:0 1px 3px rgba(80,60,20,.06);transition:all .15s;cursor:pointer}
[role="tab"]:hover{border-color:var(--accent)!important;transform:translateY(-1px)}
[role="tab"] p{font-size:17px!important;font-weight:700!important;margin:0!important}
[role="tab"][aria-selected="true"]{background:var(--accent)!important;border-color:var(--accent)!important;box-shadow:0 4px 12px rgba(80,60,20,.2)}
[role="tab"][aria-selected="true"] p{color:#fff!important}
.react-aria-SelectionIndicator,[data-baseweb="tab-highlight"],[data-baseweb="tab-border"]{display:none!important}
@media (max-width:640px){[role="tab"]{padding:7px 12px!important}[role="tab"] p{font-size:15px!important}}
@media (max-width:700px){
  [data-testid="stHeader"]{height:2.2rem!important;min-height:0!important}
  .main .block-container,[data-testid="stMain"] .block-container{padding-top:2.4rem!important;padding-left:.7rem!important;padding-right:.7rem!important;padding-bottom:20px!important}
  .topbar{padding:0;gap:6px}.logo{font-size:15px}.pill{font-size:11px;padding:1px 8px}.tmono{display:none}
  [data-testid="stHorizontalBlock"]:has(.topbar){flex-wrap:nowrap!important;gap:6px!important;align-items:center!important}
  [data-testid="stHorizontalBlock"]:has(.topbar)>div{min-width:0!important;width:auto!important;flex:0 1 auto!important}
  [data-testid="stHorizontalBlock"]:has(.topbar)>div:nth-child(1){flex:1 1 0!important}
  [data-testid="stHorizontalBlock"]:has(.topbar)>div:nth-child(2){flex:0 0 92px!important}
  [data-testid="stHorizontalBlock"]:has(.topbar)>div:nth-child(3){flex:0 0 84px!important}
  .qbar{display:flex;overflow-x:auto;gap:6px;margin:2px 0 6px;scrollbar-width:none}.qbar::-webkit-scrollbar{display:none}
  .q{flex:0 0 auto;min-width:98px;padding:4px 8px}.q-p{font-size:14px}.q-n,.q-c{font-size:11px}
  .mrow{gap:6px}.m{padding:5px 10px;min-width:0;flex:1 1 28%}.m-v{font-size:16px}
  [role="tab"]{padding:6px 10px!important}[role="tab"] p{font-size:14px!important}
}
.stButton>button{border-radius:8px;font-weight:500;border:1px solid var(--line);background:var(--card);color:var(--tx)}
.stButton>button[kind="primary"]{background:var(--accent);border-color:var(--accent);color:#fff}
.stSelectbox>div>div,.stTextInput>div>div>input{background:var(--card)!important;border:1px solid var(--line)!important;border-radius:8px!important}
[data-testid="stMetric"]{background:var(--soft);border-radius:10px;padding:8px 12px}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# 主題（顏色 + 背景圖案）。網址加 ?theme=night 可直接指定
# ─────────────────────────────────────────────
_LIGHT = dict(card="#FFFFFF", line="#EADFCB", tx="#2B2418", tx2="#7A6E5A", tx3="#A89C86",
              up="#C0392B", upbg="#FBE9E5", dn="#1B7A34", dnbg="#E4F2E8", warn="#92400E", warnbg="#FFF3D6")
THEMES = {
    "classic": ("🟫 經典米色", dict(_LIGHT, bg="#FCF8F0", soft="#F5EEDF", accent="#B8742A"), "none"),
    "dots":    ("⚪ 米色點點", dict(_LIGHT, bg="#FCF8F0", soft="#F5EEDF", accent="#B8742A"),
                "radial-gradient(rgba(184,116,42,.16) 1.2px,transparent 1.2px) 0 0/22px 22px"),
    "grid":    ("📐 方格紙", dict(_LIGHT, bg="#FBF8F1", soft="#F3EDDF", accent="#9A6B2F"),
                "linear-gradient(rgba(120,100,60,.09) 1px,transparent 1px) 0 0/28px 28px,"
                "linear-gradient(90deg,rgba(120,100,60,.09) 1px,transparent 1px) 0 0/28px 28px"),
    "forest":  ("🌲 森林", dict(_LIGHT, bg="#F1F6EE", soft="#E4EEDF", line="#D3E2CB", accent="#2F7D4F",
                                tx="#1F2A21", tx2="#5C6E5F", tx3="#8DA090"),
                "radial-gradient(circle at 12% 8%,rgba(120,180,120,.22),transparent 40%),"
                "radial-gradient(circle at 90% 85%,rgba(160,210,150,.25),transparent 45%)"),
    "ocean":   ("🌊 海洋", dict(_LIGHT, bg="#EEF5FA", soft="#DFEBF4", line="#CFE0EC", accent="#2E6FB5",
                                tx="#1B2733", tx2="#587186", tx3="#8AA1B3"),
                "linear-gradient(160deg,rgba(120,180,230,.28),transparent 45%),"
                "radial-gradient(circle at 95% 90%,rgba(90,150,210,.2),transparent 40%)"),
    "sakura":  ("🌸 櫻花", dict(_LIGHT, bg="#FDF2F4", soft="#F9E3E8", line="#F0D2D9", accent="#C2527A",
                                tx="#33212A", tx2="#7E5C69", tx3="#B398A3"),
                "radial-gradient(circle at 8% 12%,rgba(240,150,180,.28),transparent 38%),"
                "radial-gradient(circle at 92% 80%,rgba(250,190,205,.3),transparent 42%)"),
    "aurora":  ("🌌 極光", dict(_LIGHT, bg="#F4F2FB", soft="#E8E4F6", line="#DAD4EE", accent="#6C5CE7",
                                tx="#241F3A", tx2="#625B85", tx3="#9A94BB"),
                "radial-gradient(circle at 10% 10%,rgba(130,110,240,.25),transparent 38%),"
                "radial-gradient(circle at 85% 20%,rgba(80,170,240,.22),transparent 40%),"
                "radial-gradient(circle at 60% 95%,rgba(240,130,200,.18),transparent 40%)"),
    "night":   ("🌙 夜間", dict(bg="#15120E", card="#211C16", soft="#2B251D", line="#3A3228", tx="#F2EBDD",
                                tx2="#BDB19B", tx3="#8C816E", up="#FF6F5E", upbg="#3C211C", dn="#4FD27F",
                                dnbg="#17321F", warn="#F5C16C", warnbg="#3A2E12", accent="#E0A458"),
                "radial-gradient(circle at 15% 0%,rgba(224,164,88,.10),transparent 40%)"),
}
DEFAULT_THEME = "classic"
_qt = st.query_params.get("theme", DEFAULT_THEME)
st.session_state.setdefault("theme_sel", _qt if _qt in THEMES else DEFAULT_THEME)
_tv, _pat = THEMES[st.session_state["theme_sel"]][1], THEMES[st.session_state["theme_sel"]][2]
THEME_CSS = f"""<style>
:root{{--bg:{_tv['bg']};--card:{_tv['card']};--soft:{_tv['soft']};--line:{_tv['line']};--tx:{_tv['tx']};--tx2:{_tv['tx2']};
--tx3:{_tv['tx3']};--up:{_tv['up']};--up-bg:{_tv['upbg']};--dn:{_tv['dn']};--dn-bg:{_tv['dnbg']};--warn:{_tv['warn']};
--warn-bg:{_tv['warnbg']};--accent:{_tv['accent']}}}
.stApp{{background:{_pat + ',' if _pat != 'none' else ''}{_tv['bg']}!important;background-attachment:fixed!important}}
.stApp p,.stApp label,.stApp span,.stApp li,[data-testid="stMarkdownContainer"]{{color:var(--tx)}}
[data-testid="stCaptionContainer"],[data-testid="stCaptionContainer"] *{{color:var(--tx2)!important}}
.stSelectbox [data-baseweb="select"]>div,.stTextInput input{{background:var(--card)!important;color:var(--tx)!important}}
div[data-baseweb="popover"] ul,div[data-baseweb="popover"] li{{background:var(--card)!important;color:var(--tx)!important}}
.stButton>button{{background:var(--card);color:var(--tx)}}
div[data-baseweb="select"] *,[data-testid="stSelectbox"] *{{color:var(--tx)!important}}
div[data-baseweb="select"] svg{{fill:var(--tx)!important}}
[data-testid="stAlert"],[data-testid="stAlert"]>div,[data-testid="stAlertContainer"]{{background:var(--soft)!important;border:1px solid var(--line)!important;border-radius:10px}}
[data-testid="stAlert"] *,[data-testid="stAlertContainer"] *{{color:var(--tx)!important}}
.thumb .ph,.strip,.hero-t,.hero-m,.hero-o{{color:#fff}}
.q-p,.m-v{{color:var(--tx)}} .m-v.up,.up{{color:var(--up)!important}} .m-v.dn,.dn{{color:var(--dn)!important}}
</style>"""
st.markdown(THEME_CSS, unsafe_allow_html=True)


def _row_tickers(row) -> list:
    raw = str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "")
    return [x.strip() for x in raw.split(",") if x.strip()]


# ─────────────────────────────────────────────
# 初始化 + 從 GitHub 同步 news.json
# ─────────────────────────────────────────────
if "initialized" not in st.session_state:
    init_db()
    st.session_state.update({
        "initialized":  True,
        "last_update":  "—",
        "enabled_srcs": [s["name"] for s in SOURCES if s["enabled"]],
        "n_show":       24,
    })

GROQ_OK  = bool(_secret("GROQ_API_KEY"))
DATA_URL = _secret("NEWS_DATA_URL")


@st.cache_data(ttl=300, show_spinner=False)
def sync_remote(url: str) -> dict:
    """每 5 分鐘從 GitHub raw 讀一次 news.json 併入本機 SQLite"""
    if not url:
        return {"ok": False, "added": 0, "generated_at": "", "error": "未設定 NEWS_DATA_URL"}
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        payload = r.json()
        db = SessionLocal()
        try:
            added = import_json(db, payload)
        finally:
            db.close()
        return {"ok": True, "added": added,
                "generated_at": payload.get("generated_at", ""), "error": ""}
    except Exception as e:
        return {"ok": False, "added": 0, "generated_at": "", "error": f"{type(e).__name__}: {e}"}


sync = sync_remote(DATA_URL)


@st.cache_data(ttl=60, show_spinner=False)
def load_all():
    db = SessionLocal()
    try:
        return get_articles_df(db, limit=800)
    finally:
        db.close()


LIFE_CATS = ("運動", "娛樂", "電玩")


def clean_images(df):
    """同一張圖出現 3 次以上＝網站 logo / 預設圖，當作沒圖；明顯是 logo/icon 的網址也清掉"""
    if df is None or df.empty or "image_url" not in df.columns:
        return df
    df = df.copy()
    img = df["image_url"].fillna("").astype(str)
    vc = img[img != ""].value_counts()
    bad = set(vc[vc >= 3].index)
    low = img.str.lower()
    mask = img.isin(bad) | low.str.contains(r"logo|favicon|icon|placeholder|default[-_]?(?:image|img)|/ads/", regex=True)
    df.loc[mask, "image_url"] = ""
    return df


df_all = clean_images(load_all())
if not st.session_state.get("show_rev", False) and not df_all.empty:
    df_all = df_all[~df_all["title"].fillna("").apply(is_routine_revenue)]

# 保險：資料庫是空的（第一次部署 / Actions 還沒跑過）→ 自動抓一次，不讓畫面空白
if df_all.empty and not st.session_state.get("auto_crawled"):
    st.session_state["auto_crawled"] = True
    with st.spinner("資料庫是空的，正在自動抓取第一批新聞（約 30 秒）…"):
        crawl_and_save(enabled_names=st.session_state["enabled_srcs"], use_ai=False)
    st.cache_data.clear()
    df_all = clean_images(load_all())
    if not st.session_state.get("show_rev", False) and not df_all.empty:
        df_all = df_all[~df_all["title"].fillna("").apply(is_routine_revenue)]


# ─────────────────────────────────────────────
# 行情條（Yahoo Finance chart API；失敗就不顯示）
# ─────────────────────────────────────────────
QUOTE_SYMS = [("^TWII", "加權指數"), ("2330.TW", "台積電"), ("^SOX", "費城半導體"),
              ("^IXIC", "那斯達克"), ("^N225", "日經 225"), ("^KS11", "韓國 KOSPI"), ("TWD=X", "美元/台幣")]


def quote_url(sym: str) -> str:
    """點行情卡跳到 Yahoo 報價頁：台股用奇摩股市，其餘用 Yahoo Finance"""
    if sym in ("^TWII", "2330.TW"):
        return f"https://tw.stock.yahoo.com/quote/{quote(sym)}"
    return f"https://finance.yahoo.com/quote/{quote(sym)}"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _one_quote(sym_name):
    sym, name = sym_name
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym)}?range=5d&interval=1d",
            headers=_UA, timeout=6)
        res = r.json()["chart"]["result"][0]
        price = float(res["meta"]["regularMarketPrice"])
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) >= 2 and abs(closes[-1] - price) <= max(1e-4 * price, 1e-6):
            prev = closes[-2]
        elif closes:
            prev = closes[-1]
        else:
            return None
        return {"sym": sym, "name": name, "price": price, "chg": price - prev,
                "pct": (price - prev) / prev * 100 if prev else 0.0}
    except Exception:
        return None


@st.cache_data(ttl=120, show_spinner=False)
def fetch_quotes():
    with ThreadPoolExecutor(max_workers=7) as ex:
        return [q for q in ex.map(_one_quote, QUOTE_SYMS) if q]


def quotes_html(qs) -> str:
    if not qs:
        return ""
    cells = []
    for q in qs:
        cls = "up" if q["chg"] > 0 else ("dn" if q["chg"] < 0 else "flat")
        arrow = "▲" if q["chg"] > 0 else ("▼" if q["chg"] < 0 else "–")
        cells.append(
            f'<a class="q" href="{esc(quote_url(q["sym"]), quote=True)}" target="_blank" rel="noopener">'
            f'<div class="q-n">{esc(q["name"])}</div>'
            f'<div class="q-p">{q["price"]:,.2f}</div>'
            f'<div class="q-c {cls}">{arrow} {abs(q["chg"]):,.2f} ({q["pct"]:+.2f}%)</div></a>')
    return f'<div class="qbar">{"".join(cells)}</div>'


# ─────────────────────────────────────────────
# 卡片渲染
# ─────────────────────────────────────────────
SECTOR_GRAD = {
    "半導體": ("#6C5CE7", "#8E7CF0"), "AI": ("#2E86DE", "#5DADE2"), "伺服器": ("#2E86DE", "#48A9C5"),
    "電動車": ("#27AE60", "#58D68D"), "綠能": ("#27AE60", "#7FCB8A"), "金融": ("#C98A2B", "#E5B25D"),
    "航運": ("#16A0A8", "#5CC6CC"), "傳產": ("#8C7B63", "#B3A48C"), "科技": ("#4A6FA5", "#7C9BC9"),
    "戰爭/國防": ("#B03A2E", "#D9695F"), "地緣政治": ("#B03A2E", "#D9695F"), "能源": ("#CA6F1E", "#E59866"),
}


CAT_GRAD = {"AI": ("#2E86DE", "#6C5CE7"), "運動": ("#27AE60", "#16A0A8"), "娛樂": ("#D35D8C", "#E8A0BF"),
            "電玩": ("#6C5CE7", "#A29BFE"), "科技": ("#4A6FA5", "#7C9BC9"), "國際": ("#8C7B63", "#B3A48C"),
            "大陸": ("#B03A2E", "#E08A6F")}


def _first_sector(row) -> str:
    for s in str(row.get("sectors", "") or "").split(","):
        if s.strip():
            return s.strip()
    return ""


def _css_url(u: str) -> str:
    return esc(u.replace("'", "%27").replace('"', "%22").replace(" ", "%20"), quote=True)


def thumb_html(row, extra_cls="") -> str:
    sec = _first_sector(row)
    cat0 = str(row.get("category", "") or "")
    g1, g2 = SECTOR_GRAD.get(sec) or CAT_GRAD.get(cat0) or ("#B9A988", "#D8C8A6")
    traw = str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "")
    first_tk = next((t.strip() for t in traw.split(",") if t.strip()), "")
    ph = first_tk or sec or str(row.get("category", "") or "NEWS")
    img = str(row.get("image_url", "") or "")
    img_tag = (f'<img src="{_css_url(img)}" loading="lazy" referrerpolicy="no-referrer" alt="">'
               if img.startswith("http") else "")
    return (f'<div class="thumb {extra_cls}" style="background:linear-gradient(135deg,{g1},{g2})">'
            f'<span class="ph">{esc(ph)}</span>{img_tag}</div>')


CAT_ICON = {"AI": "🤖", "運動": "⚽", "娛樂": "🎬", "電玩": "🎮", "科技": "💡", "國際": "🌐", "財經": "💹", "大陸": "🏮"}


def strip_html(row) -> str:
    """沒有縮圖時用「細色帶 + 標籤」取代大塊空白漸層"""
    sec = _first_sector(row)
    cat = str(row.get("category", "") or "")
    g1, g2 = SECTOR_GRAD.get(sec) or CAT_GRAD.get(cat) or ("#B9A988", "#D8C8A6")
    traw = str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "")
    first_tk = next((t.strip() for t in traw.split(",") if t.strip()), "")
    label = " · ".join(x for x in (CAT_ICON.get(cat, "") + " " + cat if cat else "", sec, first_tk) if x.strip())
    return (f'<div class="strip" style="background:linear-gradient(90deg,{g1},{g2})">'
            f'<span>{esc(label.strip() or "NEWS")}</span></div>')


def eff_sent(row):
    if (str(row.get("category", "")) == "AI" and str(row.get("ai_summary", "") or "")
            and not str(row.get("ai_sentiment", "") or "")):
        return "neutral", 0.0          # AI 專區技術摘要：不顯示利多/利空
    ai_s = str(row.get("ai_sentiment", "") or "")
    if ai_s in ("bullish", "bearish", "neutral"):
        return ai_s, _num(row.get("ai_score"))
    return str(row.get("sentiment", "neutral")), _num(row.get("sentiment_score")) * 10


def badge_html(sent, sv) -> str:
    if sent == "bullish":
        return f'<span class="bd bd-b">利多 {sv:+.1f}</span>'
    if sent == "bearish":
        return f'<span class="bd bd-d">利空 {sv:+.1f}</span>'
    return '<span class="bd bd-n">中性</span>'


def titles(row):
    """回傳 (主標題, 原文標題或空字串)：有中譯顯示中文，原文放小字"""
    orig = str(row.get("title", "") or "")
    zh = str(row.get("title_zh", "") or "")
    return (zh, orig) if zh else (orig, "")


def translate_link(row) -> str:
    if str(row.get("language", "zh")) != "en":
        return ""
    url = str(row.get("url", "") or "")
    if not url.startswith("http"):
        return ""
    return (f'<a class="tr" href="https://translate.google.com/translate?sl=auto&tl=zh-TW&u={quote(url, safe="")}" '
            f'target="_blank" rel="noopener">🌐 翻譯全文</a>')


def tickers_chips(row, n=3) -> str:
    names = {}
    try:
        for it in json.loads(row.get("ticker_details") or "[]"):
            names[it.get("code")] = it.get("name")
    except Exception:
        pass
    traw = str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "")
    out = []
    for t in [x.strip() for x in traw.split(",") if x.strip()][:n]:
        nm = names.get(t)
        label = f"{t} {nm}" if nm and nm != t and not str(nm).startswith("台股") else t
        out.append(f'<span class="tk">{esc(label)}</span>')
    return "".join(out)


# ── 搜尋新聞模式（即時查 Google News，不存資料庫）─────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def live_search(q: str, lang: str, days: int):
    from crawler import fetch_rss, _gnews
    from analyzer import Analyzer
    from scheduler import translate_titles
    srcs = []
    if lang in ("中文", "中英文都找"):
        srcs.append({"name": "GN-搜尋", "url": _gnews(q, en=False, when=f"{days}d"), "language": "zh",
                     "category": "財經", "strip_suffix": True, "max_items": 40})
    if lang in ("英文（標題自動翻中文）", "中英文都找"):
        srcs.append({"name": "GN-搜尋EN", "url": _gnews(q, en=True, when=f"{days}d"), "language": "en",
                     "category": "國際", "strip_suffix": True, "max_items": 40})
    items = []
    for s in srcs:
        items.extend(fetch_rss(s))
    seen, uniq = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    an = Analyzer()
    for it in uniq:
        r = an.analyze(it["title"], it.get("summary", ""), it["language"])
        it.update({"sentiment": r.sentiment, "sentiment_score": r.sentiment_score,
                   "tickers": ",".join(r.tickers), "sectors": ",".join(r.sectors), "is_geo": r.is_geo,
                   "ticker_details": json.dumps([{"code": t.code, "name": t.name} for t in r.ticker_details],
                                                ensure_ascii=False),
                   "ai_sentiment": "", "ai_score": 0.0, "ai_summary": "", "ai_affected_tickers": "",
                   "importance_score": 0.0, "title_zh": "", "image_url": ""})
    translate_titles(uniq, limit=40, api_key=_secret("GROQ_API_KEY"))
    df = pd.DataFrame(uniq)
    if not df.empty:
        df = df.sort_values("published_at", ascending=False).reset_index(drop=True)
    return df


# ── 同一事件合併 ──────────────────────────────────────────────────────────────
_NUMTOK = re.compile(r"\d+\.\d+[%％兆億萬]?|\d{2,}[%％兆億萬點]")


def _sig(row):
    t = titles(row)[0]
    s = re.sub(r"[\W_]+", "", t.lower())
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    tk = {x.strip() for x in str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "").split(",") if x.strip()}
    nums = set(_NUMTOK.findall(t))
    for p in re.findall(r"(\d+(?:\.\d+)?)[%％]", t):      # 9.26% 與 9% 視為同一個數字
        nums.add(f"{int(float(p))}%")
    return grams, nums, tk


def _sim(a, b) -> float:
    ga, na, ta = a
    gb, nb, tb = b
    if len(ga) < 4 or len(gb) < 4:
        return 0.0
    j = len(ga & gb) / len(ga | gb)
    return j + (0.25 if na & nb else 0.0) + (0.2 if ta & tb else 0.0)


def cluster_events(df, threshold=0.4, window_h=48):
    """把「同一事件、多家報導」合成一組。回傳 [(代表列, [其他列…])]。
    代表 = 有圖 > 有 AI 摘要 > 重要度高 > 較新。"""
    if df is None or df.empty:
        return []
    d = df.copy()
    d["_k"] = ((d["image_url"].fillna("") != "").astype(int) * 4
               + (d["ai_summary"].fillna("") != "").astype(int) * 2
               + d["importance_score"].fillna(0).astype(float) / 10.0)
    order = list(df.index)                        # 保持呼叫端排序（例如最新優先）
    groups = []                                    # [{"rep":idx, "sig":sig, "ts":ts, "members":[idx]}]
    for idx in order:
        r = d.loc[idx]
        sg = _sig(r)
        ts = r.get("published_at")
        hit = None
        for g in groups:
            try:
                near = abs((ts - g["ts"]).total_seconds()) <= window_h * 3600
            except Exception:
                near = True
            if near and g["src"] != str(r.get("source", "")) or (near and g["src"] == str(r.get("source", "")) and False):
                pass
            if near and _sim(sg, g["sig"]) >= threshold:
                hit = g
                break
        if hit is None:
            groups.append({"sig": sg, "ts": ts, "src": str(r.get("source", "")), "members": [idx]})
        else:
            hit["members"].append(idx)
    out = []
    for g in groups:
        mem = d.loc[g["members"]].sort_values("_k", ascending=False)
        out.append((mem.iloc[0], [m for _, m in mem.iloc[1:].iterrows()]))
    return out


def card_html(row, others=None) -> str:
    sent, sv = eff_sent(row)
    cls = {"bullish": "bull", "bearish": "bear"}.get(sent, "")
    main, orig = titles(row)
    url = str(row.get("url", "") or "")
    href = esc(url, quote=True) if url else "#"
    summ = str(row.get("ai_summary", "") or "")
    geo = '<span class="bd bd-g">⚑ 地緣</span>' if row.get("is_geo") else ""
    has_img = str(row.get("image_url", "") or "").startswith("http")
    others = others or []
    more_h = ""
    if others:
        li = "".join(
            f'<li><a href="{esc(str(o.get("url", "#")), quote=True)}" target="_blank" rel="noopener">'
            f'{esc(titles(o)[0])}</a> <span>· {esc(str(o.get("source", "")))}</span></li>' for o in others[:8])
        more_h = (f'<details class="more"><summary>📚 另有 {len(others)} 則相關報導</summary><ul>{li}</ul></details>')
    orig_h = f'<div class="co">{esc(orig)}</div>' if orig else ""
    summ_h = f'<div class="cs">✦ {esc(summ)}</div>' if summ else ""
    _sec = ",".join(x.strip() for x in str(row.get("sectors", "") or "").split(",") if x.strip())
    _tk = ",".join(x.strip() for x in str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "").split(",") if x.strip())
    try:
        _ts = int(row.get("published_at").timestamp())
    except Exception:
        _ts = 0
    _sent_lbl = {"bullish": f"利多 {sv:+.1f}", "bearish": f"利空 {sv:+.1f}"}.get(sent, "中性")
    _attrs = (f'data-tk="{esc(_tk, quote=True)}" data-sec="{esc(_sec, quote=True)}" '
              f'data-ts="{_ts}" data-sent="{esc(_sent_lbl, quote=True)}" '
              f'data-cat="{esc(str(row.get("category", "") or ""), quote=True)}" '
              f'data-src="{esc(str(row.get("source", "") or ""), quote=True)}" '
              f'data-age="{min(age_hours(row.get("published_at")), 999):.0f}" '
              f'data-imp="{_num(row.get("importance_score")):.1f}"')
    return _h(f"""
    <div class="card {cls}" {_attrs}>
      <a href="{href}" target="_blank" rel="noopener">{thumb_html(row) if has_img else strip_html(row)}</a>
      <div class="cb">
        <a class="ct" href="{href}" target="_blank" rel="noopener">{esc(main)}</a>
        {orig_h}{summ_h}{more_h}
        <div class="cm cm-a">{badge_html(sent, sv)}{geo}{tickers_chips(row)}</div>
        <div class="cm cm-b"><span>{esc(str(row.get("source", "")))}</span><span>· {relative_time(row.get("published_at"))}</span>{translate_link(row)}<span class="fav" role="button" title="收藏">☆</span></div>
      </div>
    </div>""")


def render_grid(df, max_items=60, merge=True, lazy=0, cls=""):
    """lazy>0：全部卡片一次輸出，但第 lazy 張之後先隱藏（data-lz），由瀏覽器端 JS 捲到底再分批顯示＝無限捲動。
    沒有 JS 時（html 沒有 data-fnjs）CSS 不會隱藏任何卡片，所以不會卡住。cls="wall" 會啟用「自上次造訪新增」橫幅。"""
    if df is None or df.empty:
        st.markdown('<div class="empty">📭 沒有符合條件的新聞</div>', unsafe_allow_html=True)
        return
    note = ""
    if merge and st.session_state.get("merge_events", True):
        groups = cluster_events(df.head(400))
        cards = [card_html(rep, oth) for rep, oth in groups[:max_items]]
        merged = len(df.head(400)) - len(groups)
        if len(groups) > max_items:
            note = f"顯示前 {max_items} 組事件，共 {len(groups)} 組（已把 {merged} 則重複報導合併）"
        elif merged:
            note = f"已把 {merged} 則重複報導合併進對應卡片"
    else:
        cards = [card_html(r) for _, r in df.head(max_items).iterrows()]
        if len(df) > max_items:
            note = f"顯示前 {max_items} 則，共 {len(df)} 則"
    if lazy:
        cards = [c if i < lazy else c.replace('<div class="card', '<div data-lz="1" class="card', 1)
                 for i, c in enumerate(cards)]
    sentinel = '<div class="fn-sentinel"></div>' if lazy and len(cards) > lazy else ""
    newbar = '<div class="fn-new"></div>' if cls == "wall" else ""
    st.markdown(f'{newbar}<div class="{("grid " + cls).strip()}">{"".join(cards)}</div>{sentinel}',
                unsafe_allow_html=True)
    if note:
        st.caption(note)


def hero_html(df) -> str:
    d = df.copy()
    d["_rank"] = (d["importance_score"].fillna(0)
                  + (d["image_url"].fillna("") != "").astype(float) * 4.0
                  + d["published_at"].apply(lambda x: max(0.0, 2 - age_hours(x) / 6)))
    d = d.sort_values("_rank", ascending=False)
    main = d.iloc[0]
    side = d.iloc[1:4]
    sent, sv = eff_sent(main)
    mt, mo = titles(main)
    href = esc(str(main.get("url", "") or "#"), quote=True)
    mo_h = f'<div class="hero-o">{esc(mo)}</div>' if mo else ""
    side_h = ""
    for _, r in side.iterrows():
        s2, v2 = eff_sent(r)
        t2, _o = titles(r)
        h2 = esc(str(r.get("url", "") or "#"), quote=True)
        side_h += (f'<a class="si" href="{h2}" target="_blank" rel="noopener">{thumb_html(r)}'
                   f'<div><div class="si-t">{esc(t2)}</div>'
                   f'<div class="si-m">{badge_html(s2, v2)} {esc(str(r.get("source", "")))} · '
                   f'{relative_time(r.get("published_at"))}</div></div></a>')
    return _h(f"""
    <div class="hero">
      <a class="hero-main" href="{href}" target="_blank" rel="noopener">
        {thumb_html(main)}
        <div class="hero-ov">
          <div class="hero-t">{esc(mt)}</div>{mo_h}
          <div class="hero-m">{badge_html(sent, sv)}<span>{esc(str(main.get("source", "")))}</span>
          <span>{relative_time(main.get("published_at"))}</span></div>
        </div>
      </a>
      <div class="hero-side">{side_h}</div>
    </div>""")


def hbars(items) -> str:
    """items: [(label, value, color, text)] → CSS 橫條圖"""
    if not items:
        return ""
    mx = max(abs(v) for _, v, _, _ in items) or 1
    rows = "".join(
        f'<div class="hb"><span class="hb-l">{esc(str(l))}</span>'
        f'<span class="hb-t"><i style="width:{abs(v) / mx * 100:.0f}%;background:{c}"></i></span>'
        f'<span class="hb-v">{esc(t)}</span></div>' for l, v, c, t in items)
    return rows


# ─────────────────────────────────────────────
# 提及量暴增偵測：某檔股票「最近 N 小時」被提及次數 ≥ 過去平均同長度時段的 K 倍
# 參數都在這裡，想更敏感就調低 SURGE_RATIO / SURGE_MIN_N
# ─────────────────────────────────────────────
SURGE_WIN_H = 3            # 觀察窗（小時）
SURGE_RATIO = 3.0          # 超過平均幾倍才算暴增
SURGE_MIN_N = 3            # 觀察窗內至少幾則（避免 1→3 這種小數字亂跳）
SURGE_MIN_SRC = 2          # 至少幾家媒體（避免單一來源洗版）
SURGE_MIN_HISTORY_H = 12   # 資料至少涵蓋幾小時才有「平均」可比


def detect_surge(df, top=6):
    """回傳 (暴增清單, 資料涵蓋小時數)。基準 = 觀察窗之前的所有文章，換算成每個觀察窗的平均數。"""
    if df is None or df.empty:
        return [], 0.0
    d = df[~df["category"].isin(LIFE_CATS + ("大陸生活",))]
    if d.empty:
        return [], 0.0
    recs = d.to_dict("records")
    ages = [age_hours(r.get("published_at")) for r in recs]
    span = min(72.0, max(ages))
    if span < SURGE_MIN_HISTORY_H:
        return [], span
    blocks = max((span - SURGE_WIN_H) / SURGE_WIN_H, 1.0)
    rec, base, names = {}, {}, {}
    for r, a in zip(recs, ages):
        if a > 72:
            continue
        try:
            for it in json.loads(r.get("ticker_details") or "[]"):
                nm = str(it.get("name") or "")
                if it.get("code") and nm and nm != it.get("code") and not nm.startswith("台股"):
                    names.setdefault(it["code"], nm)
        except Exception:
            pass
        for t in _row_tickers(r):
            if a <= SURGE_WIN_H:
                rec.setdefault(t, []).append(r)
            else:
                base[t] = base.get(t, 0) + 1
    out = []
    for t, rows in rec.items():
        n = len(rows)
        srcs = {str(x.get("source", "")) for x in rows}
        if n < SURGE_MIN_N or len(srcs) < SURGE_MIN_SRC:
            continue
        expected = base.get(t, 0) / blocks
        ratio = n / max(expected, 1.0)
        if ratio < SURGE_RATIO:
            continue
        sents = [eff_sent(x)[0] for x in rows]
        bull, bear = sents.count("bullish"), sents.count("bearish")
        if bull and bull >= 2 * bear:
            tone, tcls = "偏多", "up"
        elif bear and bear >= 2 * bull:
            tone, tcls = "偏空", "dn"
        elif bull or bear:
            tone, tcls = "多空分歧", "flat"
        else:
            tone, tcls = "中性", "flat"
        seen_t, heads = set(), []
        for x in sorted(rows, key=lambda z: age_hours(z.get("published_at"))):
            ttl, _o = titles(x)
            if ttl not in seen_t:
                seen_t.add(ttl)
                heads.append((ttl, str(x.get("url", "") or "#")))
            if len(heads) >= 2:
                break
        nm = names.get(t, "")
        out.append({"code": t, "name": nm, "n": n, "nsrc": len(srcs), "exp": expected, "ratio": ratio,
                    "tone": tone, "tcls": tcls, "heads": heads})
    out.sort(key=lambda z: (z["ratio"], z["n"]), reverse=True)
    return out[:top], span


def surge_html(items) -> str:
    cards = ""
    for it in items:
        li = "".join(f'<li><a href="{esc(u, quote=True)}" target="_blank" rel="noopener">{esc(t)}</a></li>'
                     for t, u in it["heads"])
        cards += (f'<div class="sg">'
                  f'<div class="sg-h"><span class="sg-tk">{esc(it["code"])}</span>'
                  f'<span class="sg-nm">{esc(it["name"])}</span>'
                  f'<span class="sg-x">×{it["ratio"]:.1f}</span></div>'
                  f'<div class="sg-m">近 {SURGE_WIN_H} 小時 {it["n"]} 則（平均 {it["exp"]:.1f} 則）· {it["nsrc"]} 家媒體 · '
                  f'<span class="{it["tcls"]}">{it["tone"]}</span></div>'
                  f'<ul class="sg-l">{li}</ul></div>')
    return f'<div class="sgrid">{cards}</div>'


# ─────────────────────────────────────────────
# AI 市場總結（Groq）
# ─────────────────────────────────────────────
def get_daily_ai_summary(ai_news_df):
    from groq_analyzer import _is_rate_limited, _set_rate_limited
    groq_key = _secret("GROQ_API_KEY")
    if not groq_key:
        return "", "找不到 GROQ_API_KEY"
    if _is_rate_limited():
        return "", "Groq 冷卻中，請稍後點「重新生成」"
    lines = []
    for _, r in ai_news_df.head(15).iterrows():
        label = {"bullish": "利多", "bearish": "利空"}.get(r.get("ai_sentiment", ""), "中性")
        lines.append(f"[{label}] {r.get('ai_summary') or r.get('title', '')}")
    news_text = "\n".join(lines)
    if not news_text.strip():
        return "", "沒有可用的 AI 新聞素材"
    prompt = f"""以下是近期財經新聞的 AI 分析摘要：

{news_text}

請用繁體中文輸出台股 AI 總結，嚴格按以下 JSON 格式回應，不要有任何其他文字：

{{
  "direction": "偏多/偏空/震盪（三擇一）",
  "direction_reason": "一句話說明整體方向原因（30字內）",
  "bull_themes": ["利多主題1（20字內）", "利多主題2（20字內）"],
  "bear_themes": ["利空主題1（20字內）", "利空主題2（20字內）"],
  "key_tickers": ["2330", "NVDA"],
  "summary": "一段客觀總結（60至100字，財經播報員語氣）"
}}"""
    from groq_analyzer import groq_chat, parse_json_loose
    for attempt in range(2):
        try:
            raw = groq_chat(prompt, groq_key, max_tokens=1500, temperature=0.2, timeout=45)
            data = parse_json_loose(raw)
            if isinstance(data, dict):
                return data, ""
            return "", "AI 回傳格式不是 JSON 物件，請按重試"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                _set_rate_limited()
                if attempt == 0:
                    time.sleep(5)
                    continue
                return "", "Groq 速率限制（429），請稍後點「重新生成」"
            body = ""
            try:
                body = e.response.text[:120]
            except Exception:
                pass
            return "", f"Groq HTTP {status}：{body}"
        except json.JSONDecodeError:
            if attempt == 0:
                continue
            return "", "AI 回傳內容無法解析，請按重試"
        except Exception as e:
            return "", f"生成失敗：{type(e).__name__}: {e}"
    return "", "多次重試後仍失敗"


# ─────────────────────────────────────────────
# 頂部
# ─────────────────────────────────────────────
def _gen_time_str(iso: str):
    try:
        t = datetime.fromisoformat(iso).astimezone(TZ_TW)
        return t.strftime("%m/%d %H:%M"), (datetime.now(TZ_TW) - t).total_seconds() / 3600
    except Exception:
        return "—", None


# ═══════════════════════════════════════════════
# 擴充資料：extras.json（籌碼 / 行事曆 / 社群）＋ health.json（來源健康度）
# 兩個檔案都由 GitHub Actions 產生，放在 news.json 旁邊（同一個 data 分支）
# ═══════════════════════════════════════════════
@st.cache_data(ttl=300, show_spinner=False)
def load_remote_json(name: str, data_url: str):
    if not data_url:
        return None
    try:
        r = requests.get(data_url.rsplit("/", 1)[0] + "/" + name, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


EXTRAS = load_remote_json("extras.json", DATA_URL) or {}
HEALTH = load_remote_json("health.json", DATA_URL) or {}
WEEK = "一二三四五六日"


def yahoo_tw(code) -> str:
    return f"https://tw.stock.yahoo.com/quote/{quote(str(code))}"


def rel_epoch(ts) -> str:
    try:
        s = datetime.now(TZ_TW).timestamp() - float(ts)
    except Exception:
        return ""
    if s < 90:
        return "剛剛"
    if s < 3600:
        return f"{int(s // 60)}分鐘前"
    if s < 86400:
        return f"{int(s // 3600)}小時前"
    return f"{int(s // 86400)}天前"


def _iso_age_h(iso) -> float:
    try:
        return (datetime.now(TZ_TW) - datetime.fromisoformat(str(iso))).total_seconds() / 3600
    except Exception:
        return 9999.0


def _vcls(v) -> str:
    return "up" if v > 0 else ("dn" if v < 0 else "flat")


def _parts_html(parts: dict) -> str:
    if not parts:
        return ""
    li = "".join(f'<span class="{"dn" if str(v).startswith("error") else "flat"}">{esc(k)}：{esc(str(v))}</span>'
                 for k, v in parts.items())
    return f'<div class="parts">{li}</div>'


# ── 來源健康度 ──────────────────────────────
def health_rows() -> list:
    rows = []
    for name, h in (HEALTH.get("sources") or {}).items():
        hist = h.get("hist") or []
        rec = [x for x in hist if _iso_age_h(x[0]) <= 24]
        rate = (sum(x[1] for x in rec) / len(rec) * 100) if rec else None
        fails = 0
        for x in reversed(hist):
            if x[1]:
                break
            fails += 1
        rows.append({"name": name, "rate": rate, "n": len(rec), "fails": fails,
                     "last_ok": h.get("last_ok", ""), "cnt": (hist[-1][2] if hist else 0)})
    rows.sort(key=lambda r: (-r["fails"], r["rate"] if r["rate"] is not None else 101))
    return rows


def health_html(rows) -> str:
    if not rows:
        return '<div class="empty">還沒有健康度資料（部署新版 scheduler.py 後，下一次自動抓取就會開始記錄）</div>'
    body = ""
    for r in rows:
        if r["fails"] >= 3:
            dot, cls = "🔴", "dn"
        elif r["rate"] is not None and r["rate"] < 80:
            dot, cls = "🟡", "flat"
        else:
            dot, cls = "🟢", "up"
        rate = f'{r["rate"]:.0f}%（{r["n"]} 次）' if r["rate"] is not None else "—"
        last = relative_time(datetime.fromisoformat(r["last_ok"])) if r["last_ok"] else "—"
        body += (f'<tr><td class="l">{dot} {esc(r["name"])}</td><td class="{cls}">{rate}</td>'
                 f'<td>{r["fails"] if r["fails"] else "—"}</td><td>{r["cnt"]}</td><td>{esc(last)}</td></tr>')
    return ('<div class="tblw"><table class="tbl"><tr><th>來源</th><th>近 24 小時成功率</th><th>連續失敗</th>'
            f'<th>最近篇數</th><th>最近成功</th></tr>{body}</table></div>')


# ── 籌碼分頁 ──────────────────────────────
def rank_html(title, items) -> str:
    if not items:
        return f'<div class="rk"><div class="rk-h">{esc(title)}</div><div class="rk-e">（無）</div></div>'
    mx = max(abs(x["v"]) for x in items) or 1
    rows = "".join(
        f'<div class="rk-r"><span class="rk-c"><a href="{esc(yahoo_tw(x["c"]), quote=True)}" target="_blank" '
        f'rel="noopener">{esc(x["c"])}</a></span><span class="rk-n">{esc(x.get("n", ""))}</span>'
        f'<span class="rk-b"><i style="width:{abs(x["v"]) / mx * 100:.0f}%;background:var(--{"up" if x["v"] > 0 else "dn"})"></i></span>'
        f'<span class="rk-v {_vcls(x["v"])}">{x["v"]:+,}</span></div>' for x in items)
    return f'<div class="rk"><div class="rk-h">{esc(title)}</div>{rows}</div>'


def _tbl_ann(items, cols) -> str:
    if not items:
        return '<div class="rk-e">（目前沒有資料）</div>'
    head = "".join(f"<th>{esc(h)}</th>" for _, h in cols)
    body = ""
    for it in items[:40]:
        tds = ""
        for k, _h2 in cols:
            v = it.get(k, "")
            if k == "c":
                v = f'<a href="{esc(yahoo_tw(v), quote=True)}" target="_blank" rel="noopener">{esc(v)}</a>'
            else:
                v = esc(str(v))
            tds += f'<td class="l">{v}</td>'
        body += f"<tr>{tds}</tr>"
    return f'<div class="tblw"><table class="tbl"><tr>{head}</tr>{body}</table></div>'


def chips_html(ch: dict) -> str:
    out = []
    mk = ch.get("market")
    out.append(f'<div class="sec">🏦 三大法人（大盤，億元）<span class="fn-reco-chips">資料日期 {esc(ch.get("asof", "—"))}</span></div>')
    if mk:
        cells = "".join(
            f'<div class="m"><div class="m-l">{lab}</div><div class="m-v {_vcls(mk.get(k, 0))}">{mk.get(k, 0):+,.1f}</div></div>'
            for k, lab in (("foreign", "外資"), ("trust", "投信"), ("dealer", "自營商"), ("total", "合計")))
        out.append(f'<div class="mrow">{cells}</div>')
    hist = ch.get("history") or {}
    if hist:
        rows = ""
        for k in sorted(hist.keys(), reverse=True)[:10]:
            h = hist[k]
            dd = f"{k[4:6]}/{k[6:]}"
            rows += (f'<tr><td class="l">{dd}</td>' + "".join(
                f'<td class="{_vcls(h.get(x, 0))}">{h.get(x, 0):+,.1f}</td>' for x in ("foreign", "trust", "dealer", "total"))
                + "</tr>")
        out.append('<div class="tblw"><table class="tbl"><tr><th>日期</th><th>外資</th><th>投信</th><th>自營商</th>'
                   f'<th>合計</th></tr>{rows}</table></div>')
    out.append('<div class="sec">📊 個股買賣超前 10（張）</div>')
    out.append('<div class="rkgrid">' + rank_html("外資買超", ch.get("foreign_buy")) + rank_html("外資賣超", ch.get("foreign_sell"))
               + rank_html("投信買超", ch.get("trust_buy")) + rank_html("投信賣超", ch.get("trust_sell")) + "</div>")
    mg = ch.get("margin")
    if mg:
        amt = mg.get("amt")
        extra = (f'<span class="fn-reco-chips">大盤融資餘額 {amt["bal"]:,.0f} 億（{amt["chg"]:+,.1f} 億）</span>' if amt else "")
        out.append(f'<div class="sec">💳 融資融券增減前 10（張）· {esc(mg.get("date", ""))}{extra}</div>')
        out.append('<div class="rkgrid">' + rank_html("融資增加", mg.get("fin_up")) + rank_html("融資減少", mg.get("fin_down"))
                   + rank_html("融券增加", mg.get("short_up")) + "</div>")
    return "".join(out)


# ── 行事曆分頁 ──────────────────────────────
CAL_KIND = {"macro": ("🏛", "總經"), "exdiv": ("💰", "除權息"), "earn": ("📊", "美股財報"),
            "holiday": ("🏖", "休市"), "other": ("📌", "其他")}


def cal_html(events, kind="全部") -> str:
    today = datetime.now(TZ_TW).date()
    keep = [e for e in events if kind == "全部" or e.get("kind") == kind]
    by = {}
    for e in keep:
        by.setdefault(e["date"], []).append(e)
    if not by:
        return '<div class="empty">📭 這個類別近期沒有事件</div>'
    out = []
    for ds in sorted(by.keys()):
        try:
            dt = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        delta = (dt - today).days
        rel = "今天" if delta == 0 else ("明天" if delta == 1 else (f"{delta} 天後" if delta > 1 else "已過"))
        out.append(f'<div class="cal-d{" today" if delta == 0 else ""}">{dt.month}/{dt.day}（{WEEK[dt.weekday()]}）<span>{rel}</span></div>')
        rows = []
        for e in by[ds]:
            ic = CAL_KIND.get(e.get("kind"), CAL_KIND["other"])[0]
            note = f'<span class="cal-x">{esc(e["note"])}</span>' if e.get("note") else ""
            rows.append(f'<div class="cal-r"><span>{ic}</span><span class="cal-t">{esc(e.get("time") or "")}</span>'
                        f'<span class="cal-n">{esc(e["title"])}</span>{note}</div>')
        if len(rows) > 8:
            out.append("".join(rows[:8]) + f'<details class="more"><summary>另外 {len(rows) - 8} 筆</summary>'
                       + "".join(rows[8:]) + "</details>")
        else:
            out.append("".join(rows))
    return "".join(out)


# ── 社群分頁 ──────────────────────────────
def _post_list(posts, n=40, show_k=True) -> str:
    if not posts:
        return '<div class="rk-e">（目前沒有資料）</div>'
    rows = ""
    for p in posts[:n]:
        k = f'<span class="pk">{esc(p["k"])}</span>' if (show_k and p.get("k")) else ""
        rows += (f'<div class="post">{k}<a href="{esc(p["u"], quote=True)}" target="_blank" rel="noopener">{esc(p["t"])}</a>'
                 f'<span class="pt">{rel_epoch(p.get("p"))}</span></div>')
    return rows


def _rank_list(rows, n=15) -> str:
    """rows: [(標題, 連結, 右側小字[, 原文])]"""
    if not rows:
        return '<div class="rk-e">（目前沒有資料）</div>'
    out = ""
    for r in rows[:n]:
        t, u, x = r[0], r[1], r[2]
        sub = f'<div style="font-size:11.5px;color:var(--tx3);font-weight:400">{esc(r[3])}</div>' if len(r) > 3 and r[3] else ""
        out += (f'<div class="post"><div style="flex:1;min-width:0"><a href="{esc(u, quote=True)}" target="_blank" '
                f'rel="noopener">{esc(t)}</a>{sub}</div><span class="pt">{esc(x)}</span></div>')
    return out


def social_html(sc: dict) -> tuple:
    """回傳 (左欄 html, 右欄 html)：左＝Reddit（美國），右＝微博（大陸）"""
    left = ""
    rd = ""
    for sub, lst in (sc.get("reddit") or {}).items():
        rd += f'<div class="subhead"><b>r/{esc(sub)}</b></div>' + _rank_list(
            [(p.get("z") or p["t"], p["u"], rel_epoch(p.get("p")), p["t"] if p.get("z") else "") for p in lst], 12)
    rd = rd or '<div class="rk-e">（Reddit 目前連不上）</div>'
    left += '<div class="sec">👽 Reddit（美國）</div>' + rd
    wb = sc.get("weibo") or []
    wb_h = _rank_list([(f'{i}. {p["t"]}', p["u"], (p.get("tag") or "") + (f' · {p["n"]:,}' if p.get("n") else ""))
                       for i, p in enumerate(wb, 1)], 20)
    right = '<div class="sec">🇨🇳 微博熱搜（大陸）</div>' + wb_h
    return left, right


gen_str, gen_age = _gen_time_str(sync.get("generated_at", ""))
if sync["ok"] and gen_age is not None and gen_age <= 3:
    status = f'<span class="pill pill-ok">● 自動更新正常</span>'
elif sync["ok"]:
    status = '<span class="pill pill-warn">⚠ 自動更新已超過 3 小時</span>'
else:
    status = '<span class="pill pill-warn">⚠ 未連到 GitHub 資料</span>'

_hbad = [r for r in health_rows() if r["fails"] >= 3]
if _hbad:
    status += (f'<span class="pill pill-warn" title="{esc("、".join(r["name"] for r in _hbad), quote=True)}">'
               f'⚠ {len(_hbad)} 個來源連續抓不到</span>')

tb1, tb3, tb2 = st.columns([5, 1.3, 1])
with tb1:
    st.markdown(_h(f"""
    <div class="topbar"><span class="logo">📈 FinNews AI</span>{status}
    <span class="tmono">資料產生 {gen_str} · 本頁載入 {datetime.now(TZ_TW).strftime("%H:%M")}</span></div>"""),
                unsafe_allow_html=True)
with tb3:
    _keys = list(THEMES.keys())
    st.selectbox("主題", _keys, key="theme_sel", format_func=lambda k: THEMES[k][0],
                 label_visibility="collapsed")
    if st.query_params.get("theme") != st.session_state["theme_sel"]:
        st.query_params["theme"] = st.session_state["theme_sel"]
with tb2:
    if st.button("🔄 立即抓取", type="primary", use_container_width=True):
        with st.spinner("抓取中，約 30～60 秒…"):
            res = crawl_and_save(enabled_names=st.session_state["enabled_srcs"],
                                 use_ai=GROQ_OK, max_ai_per_run=10)
            st.session_state["last_update"] = res["time"]
            st.cache_data.clear()
        st.success(f"新增 {res['saved']} 則｜AI {res['ai_count']} 則｜翻譯 {res['translated']} 則")
        st.rerun()

if not sync["ok"] and not DATA_URL:
    st.info("尚未設定 NEWS_DATA_URL（Streamlit Secrets），目前只顯示本機手動抓取的資料。設定方式見教學步驟。")
elif not sync["ok"]:
    st.warning(f"讀取 GitHub 資料失敗：{sync['error']}")

qs = fetch_quotes()
if qs:
    st.markdown(quotes_html(qs), unsafe_allow_html=True)

tab_home, tab_ai, tab_sports, tab_ent, tab_game, tab_cal, tab_search, tab_cfg = st.tabs(
    ["📰 今日新聞", "🤖 AI 專區", "⚽ 運動", "🎬 娛樂", "🎮 電玩", "📅 行事曆", "🔎 搜尋", "⚙️ 設定"])

# ═══════════════════════════════════════════════
# TAB 1：今日新聞
# ═══════════════════════════════════════════════
with tab_home:
    if df_all.empty:
        st.markdown('<div class="empty">📭 還沒有任何新聞，請按右上角「立即抓取」</div>', unsafe_allow_html=True)
    else:
        df_fin = df_all[~df_all["category"].isin(LIFE_CATS + ("AI", "大陸生活"))]
        df_r, used_h = recent(df_fin if not df_fin.empty else df_all)
        if used_h > 12:
            st.caption(f"ℹ️ 12 小時內新聞太少，已自動放寬到 {used_h} 小時")

        eff_all = df_r.apply(lambda r: eff_sent(r)[0], axis=1)
        bull_n = int((eff_all == "bullish").sum())
        bear_n = int((eff_all == "bearish").sum())
        neu_n = len(df_r) - bull_n - bear_n
        ai_df_all = df_r[df_r["ai_summary"].fillna("") != ""]
        geo_df = df_r[df_r["is_geo"] == True]
        cn_n = int((df_r["category"] == "大陸").sum())
        intl_n = int((df_r["language"] == "en").sum())
        tot = max(len(df_r), 1)
        qf = st.query_params.get("qf", "")
        _qbase = f"?theme={st.session_state['theme_sel']}"

        st.markdown(_h(f"""
        <div class="mrow">
          <a class="m" style="text-decoration:none" href="{_qbase}&qf=cn"><div class="m-l">🏮 大陸{' ✓' if qf == 'cn' else ''}</div><div class="m-v">{cn_n}</div></a>
          <a class="m" style="text-decoration:none" href="{_qbase}&qf=intl"><div class="m-l">🌐 國際英文{' ✓' if qf == 'intl' else ''}</div><div class="m-v">{intl_n}</div></a>
          <a class="m" style="text-decoration:none" href="{_qbase}"><div class="m-l">📰 新聞{' ✓' if not qf else ''}</div><div class="m-v">{len(df_r)}</div></a>
          <div class="m"><div class="m-l">📈 利多</div><div class="m-v up">{bull_n}</div></div>
          <div class="m"><div class="m-l">📉 利空</div><div class="m-v dn">{bear_n}</div></div>
          <div class="m"><div class="m-l">✦ AI 分析</div><div class="m-v">{len(ai_df_all)}</div></div>
          <div class="m"><div class="m-l">⚑ 地緣</div><div class="m-v">{len(geo_df)}</div></div>
        </div>
        <div class="sbar" title="利多 / 中性 / 利空">
          <i style="width:{bull_n / tot * 100:.1f}%;background:var(--up)"></i>
          <i style="width:{neu_n / tot * 100:.1f}%;background:#D9CDB3"></i>
          <i style="width:{bear_n / tot * 100:.1f}%;background:var(--dn)"></i>
        </div>"""), unsafe_allow_html=True)

        # ── 題材升溫（提及量暴增）──
        _sg, _span = detect_surge(df_all)
        if _sg:
            st.markdown(f'<div class="sec">📈 題材升溫 <span class="fn-reco-chips">近 {SURGE_WIN_H} 小時被提及次數 ≥ 平均的 '
                        f'{SURGE_RATIO:g} 倍</span></div>' + surge_html(_sg), unsafe_allow_html=True)
        elif _span < SURGE_MIN_HISTORY_H:
            st.caption(f"📈 題材升溫：資料只涵蓋 {_span:.0f} 小時，累積到 {SURGE_MIN_HISTORY_H} 小時以上才會開始偵測")
        else:
            st.caption(f"📈 題材升溫：近 {SURGE_WIN_H} 小時沒有個股的提及量超過平均 {SURGE_RATIO:g} 倍")

        # ── 頭條 ──
        st.markdown('<div class="sec">🔥 今日頭條</div>', unsafe_allow_html=True)
        st.markdown(hero_html(df_r), unsafe_allow_html=True)

        # ── ✨ 為你推薦（內容由瀏覽器端依你的點擊紀錄挑選，這裡只放容器與候選池）──
        st.markdown(_h("""
        <div class="fn-reco-wrap"><div class="sec">✨ 為你推薦 <span class="fn-reco-chips"></span>
        <a href="#" class="fn-reco-clear">清除偏好</a></div>
        <div class="fn-reco-note"></div><div class="grid fn-reco"></div></div>"""), unsafe_allow_html=True)
        _pool_src = df_all[df_all["published_at"].apply(age_hours) <= 48]
        _pool_src = _pool_src.sort_values("published_at", ascending=False).head(160)
        _pool_cards = "".join(card_html(rep_, oth_) for rep_, oth_ in cluster_events(_pool_src)[:80])
        st.markdown(f'<div class="fn-pool" style="display:none">{_pool_cards}</div>', unsafe_allow_html=True)

        # ── AI 市場總結 ──
        st.markdown('<div class="sec">✦ AI 市場總結</div>', unsafe_allow_html=True)
        if not GROQ_OK:
            st.info("需要設定 GROQ_API_KEY 才能顯示 AI 市場總結")
        elif ai_df_all.empty:
            st.info("近期還沒有 AI 分析資料（等下一次 GitHub Actions 執行，或按「立即抓取」）")
        else:
            cache_key = f"{len(ai_df_all)}_{ai_df_all.iloc[0].get('published_at')}"
            if st.session_state.get("_sum_key") != cache_key:
                with st.spinner("AI 正在生成市場總結…"):
                    sdata, serr = get_daily_ai_summary(
                        ai_df_all.sort_values("importance_score", ascending=False))
                st.session_state.update({"_sdata": sdata, "_serr": serr, "_sum_key": cache_key,
                                         "_stime": datetime.now(TZ_TW).strftime("%H:%M")})
            sdata, serr = st.session_state.get("_sdata", ""), st.session_state.get("_serr", "")
            if sdata and isinstance(sdata, dict):
                direction = sdata.get("direction", "震盪")
                dcls = "up" if direction == "偏多" else ("dn" if direction == "偏空" else "flat")
                icon = "↗" if direction == "偏多" else ("↘" if direction == "偏空" else "→")
                bt = "".join(f'<span class="tag tag-b">📈 {esc(t)}</span>' for t in sdata.get("bull_themes", []))
                dt = "".join(f'<span class="tag tag-d">📉 {esc(t)}</span>' for t in sdata.get("bear_themes", []))
                kt = "".join(f'<span class="chip-t">{esc(str(t))}</span>' for t in sdata.get("key_tickers", []))
                st.markdown(_h(f"""
                <div class="ai-card"><span class="ai-badge">Groq AI · 市場總結</span>
                <div class="ai-dir {dcls}">{icon} 整體{esc(direction)}</div>
                <div class="ai-rsn">{esc(sdata.get("direction_reason", ""))}</div>
                <div>{bt}{dt}</div><div>{kt}</div>
                <div class="ai-body">{esc(sdata.get("summary", ""))}</div>
                <div class="ai-ft">根據 {len(ai_df_all)} 則 AI 分析新聞 · {st.session_state.get("_stime", "")} 台灣時間</div></div>"""),
                            unsafe_allow_html=True)
                if st.button("🔄 重新生成總結", key="regen"):
                    st.session_state.pop("_sum_key", None)
                    st.rerun()
            elif serr:
                st.warning(f"⏳ {serr}")
                if st.button("🔄 重試", key="regen_err"):
                    st.session_state.pop("_sum_key", None)
                    st.rerun()

        # ── 地緣 ──
        if not geo_df.empty:
            st.markdown('<div class="sec">⚑ 地緣政治警示</div>', unsafe_allow_html=True)
            g = ""
            for _, r in geo_df.head(6).iterrows():
                s, _v = eff_sent(r)
                imp = "利多" if s == "bullish" else ("利空" if s == "bearish" else "中性")
                t, _o = titles(r)
                g += (f'<div class="geo"><a href="{esc(str(r.get("url", "#")), quote=True)}" target="_blank" '
                      f'rel="noopener">{esc(t)}</a> <span style="color:var(--tx3)">· {imp}</span></div>')
            st.markdown(f'<div class="geo-grid">{g}</div>', unsafe_allow_html=True)

        # ── 新聞牆 ──
        st.markdown('<div class="sec">📋 最新新聞</div>', unsafe_allow_html=True)
        chip = st.radio("快速篩選", ["全部", "📈 利多", "📉 利空", "✦ AI 精選", "⚑ 地緣"],
                        horizontal=True, label_visibility="collapsed", key="chip")
        f1, f2, f3, f4 = st.columns([1.2, 2.4, 1, 1])
        with f1:
            src_list = sorted(df_r["source"].unique().tolist())
            src_f = st.selectbox("來源", ["全部"] + src_list, key="d_src")
        with f2:
            kw = st.text_input("搜尋標題（中英文皆可）", placeholder="台積電 / Nvidia …", key="d_kw")
        with f3:
            sort_f = st.selectbox("排序", ["最新優先", "重要度"], key="d_sort")
        with f4:
            hide_neu = st.checkbox("隱藏中性", value=False, key="d_hide")

        ddf = df_r.copy()
        effs = ddf.apply(lambda r: eff_sent(r)[0], axis=1)
        if chip == "📈 利多":
            ddf = ddf[effs == "bullish"]
        elif chip == "📉 利空":
            ddf = ddf[effs == "bearish"]
        elif chip == "✦ AI 精選":
            ddf = ddf[(ddf["ai_summary"].fillna("") != "") & (ddf["ai_score"].abs() >= 5)]
        elif chip == "⚑ 地緣":
            ddf = ddf[ddf["is_geo"] == True]
        if qf == "cn":
            # 「大陸生活」（娛樂/電玩/旅遊）平常不進新聞牆，只在這個快篩底下才併進來顯示
            life_cn = df_all[df_all["category"] == "大陸生活"]
            ddf = pd.concat([ddf[ddf["category"] == "大陸"], life_cn]).drop_duplicates(subset=["url"])
            ddf = ddf.sort_values("published_at", ascending=False)
        elif qf == "intl":
            ddf = ddf[ddf["language"] == "en"]
        if hide_neu:
            ddf = ddf[ddf.apply(lambda r: eff_sent(r)[0] != "neutral", axis=1)]
        if src_f != "全部":
            ddf = ddf[ddf["source"] == src_f]
        if kw:
            m = (ddf["title"].str.contains(kw, case=False, na=False)
                 | ddf["title_zh"].fillna("").str.contains(kw, case=False, na=False))
            ddf = ddf[m]
        if sort_f == "重要度":
            ddf = ddf.sort_values(["importance_score", "published_at"], ascending=False)

        st.markdown('<div class="fn-dens"><span data-dens="card">▦ 卡片</span><span data-dens="list">☰ 列表</span></div>',
                    unsafe_allow_html=True)
        st.caption(f"共 {len(ddf)} 則（往下捲會自動載入更多）")
        render_grid(ddf, max_items=240, lazy=24, cls="wall")


# ═══════════════════════════════════════════════
# TAB：籌碼 / 行事曆 / 社群（資料來自 extras.json）
# ═══════════════════════════════════════════════
_NO_EXTRAS = ("這一塊的資料還沒產生。請確認 GitHub Actions 已部署新版（extras.py、calendar_manual.json、fetch_news.yml），"
              "下一次自動抓取後就會出現；也可以到 Actions 頁面手動按一次 Run workflow。")
with tab_cal:
    # ① 籌碼（最上）
    _ch = EXTRAS.get("chips")
    if not _ch or not (_ch.get("foreign_buy") or _ch.get("market")):
        st.markdown(f'<div class="empty">📊 {_NO_EXTRAS}</div>', unsafe_allow_html=True)
        if _ch:
            st.markdown(_parts_html(_ch.get("parts")), unsafe_allow_html=True)
    else:
        st.markdown(chips_html(_ch), unsafe_allow_html=True)
        st.caption("資料來源：臺灣證券交易所（三大法人買賣超日報、融資融券彙總）。"
                   "三大法人約 15:00～16:30 公布，融資融券通常更晚；紅色＝買超／增加，綠色＝賣超／減少。")

    # ② 行事曆（只留總經 / 休市 / NVDA 財報，未來約 2 個月，不分頁）
    st.markdown('<div class="sec">📅 行事曆（未來 2 個月）</div>', unsafe_allow_html=True)
    _cal = EXTRAS.get("calendar")
    _today = datetime.now(TZ_TW).date()
    _evs = []
    for _e in ((_cal or {}).get("events") or []):
        try:
            _d = datetime.strptime(_e["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if 0 <= (_d - _today).days <= 62 and (_e.get("kind") in ("macro", "holiday")
                                                  or (_e.get("kind") == "earn" and ("NVDA" in _e.get("title", "") or "輝達" in _e.get("title", "")))):
            _evs.append(_e)
    if _evs:
        st.markdown(cal_html(_evs), unsafe_allow_html=True)
    else:
        st.markdown('<div class="empty">📭 未來 2 個月沒有事件（請更新 calendar_manual.json）</div>', unsafe_allow_html=True)
    st.caption("自動更新：休市日用規則計算，FOMC 來自 Fed 官網，非農／CPI 來自 FRED／BLS，輝達財報來自 Nasdaq；時間已換成台灣時間，只顯示今天起 62 天內。")

    # ③ 社群（最下）：Reddit、微博
    st.markdown('<div class="sec">💬 社群熱門話題</div>', unsafe_allow_html=True)
    _sc = EXTRAS.get("social")
    if not _sc:
        st.markdown(f'<div class="empty">💬 {_NO_EXTRAS}</div>', unsafe_allow_html=True)
    else:
        _l, _r = social_html(_sc)
        _c1, _c2 = st.columns(2)
        with _c1:
            st.markdown(_l, unsafe_allow_html=True)
        with _c2:
            st.markdown(_r, unsafe_allow_html=True)
        st.markdown(_parts_html(_sc.get("parts")), unsafe_allow_html=True)
        st.caption("社群內容只代表網友討論熱度，不是消息來源，也不是買賣依據。")


# ═══════════════════════════════════════════════
# TAB：搜尋新聞（即時查 Google News）
# ═══════════════════════════════════════════════
with tab_search:
    st.caption("輸入任何關鍵字，現場去 Google News 查（不限於資料庫裡已有的新聞），結果不會存起來。")
    with st.form("search_form", clear_on_submit=False):
        sc1, sc2, sc3, sc4 = st.columns([3, 1.3, 1.3, 0.8])
        with sc1:
            sq = st.text_input("關鍵字", placeholder="例：台積電 2奈米 / Nvidia Rubin / 聯準會 降息", label_visibility="collapsed")
        with sc2:
            slang = st.selectbox("語言", ["中文", "英文（標題自動翻中文）", "中英文都找"], label_visibility="collapsed")
        with sc3:
            sdays = st.selectbox("時間", [1, 3, 7, 30], index=2, format_func=lambda d: f"近 {d} 天", label_visibility="collapsed")
        with sc4:
            go = st.form_submit_button("🔎 搜尋", type="primary", use_container_width=True)
    if go and sq.strip():
        st.session_state["last_search"] = (sq.strip(), slang, int(sdays))
    ls = st.session_state.get("last_search")
    if ls:
        with st.spinner(f"正在搜尋「{ls[0]}」…"):
            try:
                sdf = live_search(*ls)
            except Exception as e:
                sdf = None
                st.warning(f"搜尋失敗：{type(e).__name__}: {e}")
        if sdf is not None:
            if sdf.empty:
                st.markdown('<div class="empty">找不到結果，換個關鍵字或放寬時間試試</div>', unsafe_allow_html=True)
            else:
                st.caption(f"「{ls[0]}」共 {len(sdf)} 則")
                render_grid(sdf, max_items=40)
    else:
        st.markdown('<div class="empty">🔎 在上面輸入關鍵字開始搜尋</div>', unsafe_allow_html=True)


# ═══════════════════════════════════════════════
# TAB：AI 專區（美國 / 中國 / 台灣供應鏈）
# ═══════════════════════════════════════════════
AI_SEGMENTS = [
    ("🧠 AI 晶片／GPU",   ["nvidia", "輝達", "amd", "超微", "gpu", "blackwell", "rubin", "chip", "晶片", "broadcom", "博通"]),
    ("🏭 晶圓代工／封裝",  ["tsmc", "台積電", "cowos", "hbm", "先進封裝", "foundry", "samsung", "sk hynix", "micron", "美光", "asml"]),
    ("🖥️ 伺服器／ODM",    ["server", "伺服器", "廣達", "緯創", "鴻海", "foxconn", "supermicro", "dell", "wiwynn", "緯穎"]),
    ("☁️ 雲端大廠／資料中心", ["microsoft", "google", "amazon", "aws", "meta", "oracle", "data center", "資料中心", "capex", "azure"]),
    ("💬 模型／應用",      ["openai", "anthropic", "claude", "chatgpt", "gemini", "llama", "grok", "model", "模型", "agent"]),
    ("🇨🇳 中國 AI",        ["deepseek", "alibaba", "qwen", "huawei", "ascend", "baidu", "tencent", "moonshot", "kimi", "china", "中國", "大陸"]),
    ("🛡️ 管制／政策",      ["export control", "export", "tariff", "ban", "restriction", "sanction", "出口管制", "關稅", "禁令", "制裁"]),
    ("⚡ 電力／散熱",      ["power", "electricity", "cooling", "liquid", "散熱", "液冷", "電力", "nuclear", "核電"]),
]
CN_SRC = {"GN-中國AI", "SCMP Tech", "GN-大陸AI模型"}
CN_AI_RE = (r"\bai\b|人工智慧|人工智能|大模型|大語言|大语言|生成式|算力|晶片|芯片|半導體|半导体|gpu|deepseek|"
            r"輝達|英偉達|英伟达|華為|华为|昇騰|昇腾|機器人|机器人|智駕|自駕|openai|chatgpt|通義|文心|智譜|月之暗面")
STOCK_RE = r"stock|shares|\bsell|investor|etf|earnings|market cap|valuation|wall street|nasdaq|s&p|股|美股|財報|市值|估值|漲|跌|買進|賣出|投資人|營收"


def _blob(df):
    return (df["title"].fillna("") + " " + df["title_zh"].fillna("") + " " + df["summary"].fillna("")).str.lower()


with tab_ai:
    ai_zone = df_all[df_all["category"] == "AI"]
    tw_chain = df_all[(df_all["category"] != "AI") & df_all["sectors"].fillna("").str.contains("AI|伺服器|半導體", regex=True)]
    _cn_all = df_all[df_all["category"] == "大陸"]
    cn_ai = _cn_all[_blob(_cn_all).str.contains(CN_AI_RE, regex=True)] if not _cn_all.empty else _cn_all
    if ai_zone.empty and tw_chain.empty and cn_ai.empty:
        st.markdown('<div class="empty">🤖 AI 專區還沒有資料。等下一次 GitHub Actions 抓取（新來源會在下一輪出現），'
                    '或按右上角「立即抓取」。</div>', unsafe_allow_html=True)
    else:
        pool = pd.concat([ai_zone, tw_chain, cn_ai]).drop_duplicates(subset=["url"])
        pool, hrs = recent(pool, hours_steps=(24, 48, 72, 168), min_n=10)
        region = st.radio("地區", ["🧪 模型與技術", "🇺🇸 美國／全球", "🇨🇳 中國", "🇹🇼 台灣供應鏈", "🌐 全部"], horizontal=True,
                          label_visibility="collapsed", key="ai_region")
        if region == "🧪 模型與技術":
            # 只留「產品／模型／技術」：排除標題或摘要偏股價、財報、投資的文章
            view = pool[(pool["category"] == "AI") & ~_blob(pool).str.contains(STOCK_RE, regex=True)]
        elif region == "🇺🇸 美國／全球":
            view = pool[(pool["category"] == "AI") & ~pool["source"].isin(CN_SRC)]
        elif region == "🇨🇳 中國":
            blob = _blob(pool)
            cn_kw = "deepseek|alibaba|qwen|huawei|ascend|baidu|tencent|moonshot|kimi|china|chinese|中國|大陸"
            view = pool[pool["source"].isin(CN_SRC) | (pool["category"] == "大陸")
                        | ((pool["category"] == "AI") & blob.str.contains(cn_kw, regex=True))]
        elif region == "🇹🇼 台灣供應鏈":
            view = pool[~pool["category"].isin(["AI", "大陸"])]
        else:
            view = pool
        if view.empty:
            view = pool

        effs_ai = view.apply(lambda r: eff_sent(r)[0], axis=1)
        b_n, d_n = int((effs_ai == "bullish").sum()), int((effs_ai == "bearish").sum())
        st.markdown(_h(f"""
        <div class="mrow">
          <div class="m"><div class="m-l">🤖 AI 新聞</div><div class="m-v">{len(view)}</div></div>
          <div class="m"><div class="m-l">📈 利多</div><div class="m-v up">{b_n}</div></div>
          <div class="m"><div class="m-l">📉 利空</div><div class="m-v dn">{d_n}</div></div>
          <div class="m"><div class="m-l">🇺🇸 美國</div><div class="m-v">{int(((pool['category'] == 'AI') & ~pool['source'].isin(CN_SRC)).sum())}</div></div>
          <div class="m"><div class="m-l">🇨🇳 中國</div><div class="m-v">{int((pool['source'].isin(CN_SRC) | (pool['category'] == '大陸')).sum())}</div></div>
        </div>"""), unsafe_allow_html=True)
        if hrs > 24:
            st.caption(f"ℹ️ 近 24 小時 AI 新聞較少，已放寬到 {hrs} 小時")

        st.markdown('<div class="sec">🔥 AI 頭條</div>', unsafe_allow_html=True)
        st.markdown(hero_html(view), unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="sec">🧬 產業鏈熱度（提及篇數）</div>', unsafe_allow_html=True)
            blob = _blob(pool)
            segs = []
            for name, kws in AI_SEGMENTS:
                pat = "|".join(re.escape(k) for k in kws)
                m = blob.str.contains(pat, regex=True)
                n = int(m.sum())
                if n:
                    e = pool[m].apply(lambda r: eff_sent(r)[0], axis=1)
                    segs.append((name, float(n), "#B8742A",
                                 f"{n} 篇 ↑{int((e == 'bullish').sum())}↓{int((e == 'bearish').sum())}"))
            segs.sort(key=lambda x: -x[1])
            st.markdown(f'<div class="seg">{hbars(segs)}</div>' if segs else '<div class="empty">資料不足</div>',
                        unsafe_allow_html=True)
            st.caption("↑利多篇數 ↓利空篇數（關鍵字＋AI 判斷）；一篇文章可能同時屬於多個環節")
        with c2:
            st.markdown('<div class="sec">🎯 被點名最多的股票</div>', unsafe_allow_html=True)
            cnt = {}
            for _, r in pool.iterrows():
                for t in str(r.get("ai_affected_tickers", "") or r.get("tickers", "") or "").split(","):
                    t = t.strip()
                    if t:
                        cnt[t] = cnt.get(t, 0) + 1
            top = sorted(cnt.items(), key=lambda x: -x[1])[:8]
            st.markdown(f'<div class="seg">{hbars([(k, float(v), "#2E86DE", f"{v} 篇") for k, v in top])}</div>'
                        if top else '<div class="empty">還沒有個股被點名</div>', unsafe_allow_html=True)

        st.markdown('<div class="sec">📋 AI 新聞</div>', unsafe_allow_html=True)
        render_grid(view.sort_values("published_at", ascending=False), max_items=200, lazy=18)


# ═══════════════════════════════════════════════
# TAB：運動 / 娛樂 / 電玩（輕量，不做股市判斷）
# ═══════════════════════════════════════════════
def render_life_tab(cat: str, icon: str):
    d0 = df_all[df_all["category"] == cat]
    if d0.empty:
        st.markdown(f'<div class="empty">{icon} {cat}還沒有資料，等下一次自動抓取就會出現。</div>', unsafe_allow_html=True)
        return
    d0, _ = recent(d0, hours_steps=(24, 48, 72, 168), min_n=6)
    st.caption(f"共 {len(d0)} 則（只保留精華，每個來源 8～12 則）")
    render_grid(d0.sort_values("published_at", ascending=False), max_items=200, lazy=18)


with tab_sports:
    render_life_tab("運動", "⚽")
with tab_ent:
    render_life_tab("娛樂", "🎬")
with tab_game:
    render_life_tab("電玩", "🎮")


# ═══════════════════════════════════════════════
# TAB 3：設定
# ═══════════════════════════════════════════════
with tab_cfg:
    st.markdown("#### ⭐ 收藏")
    # 內容由瀏覽器端 JS 依 localStorage 畫出；收藏的是標題/連結快照，不受 72 小時限制
    st.markdown('<div class="fn-fav-wrap"><div class="fn-fav-note"></div><div class="grid fn-fav"></div></div>',
                unsafe_allow_html=True)
    st.markdown("#### 🧹 顯示內容")
    st.checkbox("把同一事件的多家報導合併成一張卡片", value=True, key="merge_events")
    st.checkbox("顯示例行「月營收公布」快訊（預設隱藏）", key="show_rev",
                help="像「聯詠10月營收100億元」這類每月大量出現的制式公告。勾選後下次重新整理生效。")
    st.markdown("#### 📦 資料狀態")
    st.caption(f"資料來源網址：{DATA_URL or '（未設定）'}")
    st.write(f"最近同步：{'成功' if sync['ok'] else '失敗 – ' + sync['error']}｜本次併入 {sync['added']} 則｜"
             f"JSON 產生時間 {gen_str}｜資料庫目前 {len(df_all)} 則")
    if not df_all.empty:
        cnt = df_all.groupby("source").size().sort_values(ascending=False).head(20)
        mx = float(cnt.max())
        st.markdown(hbars([(s, float(n), "#B8742A", f"{int(n)} 則") for s, n in cnt.items()]),
                    unsafe_allow_html=True)
        st.caption("各來源在資料庫裡的文章數（某來源是 0 或不見，代表該 RSS 目前抓不到，可去 crawler.py 換網址）")

    st.markdown("#### 🩺 來源健康度")
    st.markdown(health_html(health_rows()), unsafe_allow_html=True)
    st.caption("每次自動抓取記一筆，保留最近約 2 天。「連續失敗」≥ 3 會在頁面最上方出現警示；"
               "失敗＝該來源這次抓到 0 篇（網址壞了、被擋、或 RSS 真的沒新東西）。")
    if EXTRAS:
        st.markdown("**籌碼 / 行事曆 / 社群 資料狀態**")
        for _k, _lab in (("chips", "籌碼"), ("calendar", "行事曆"), ("social", "社群")):
            _e = EXTRAS.get(_k) or {}
            st.markdown(f'{_lab}：`{_e.get("status", "沒有資料")}`　更新於 {esc(str(_e.get("fetched_at", "—"))[:16])}'
                        + _parts_html(_e.get("parts")), unsafe_allow_html=True)
    st.divider()
    st.markdown("#### 📡「立即抓取」要用哪些來源")
    st.caption("GitHub Actions 的自動抓取固定用 crawler.py 裡 enabled=True 的來源；這裡只影響頁面上的手動按鈕。")
    enabled = []
    for src in SOURCES:
        if st.checkbox(f"**{src['name']}**　`{src['category']}`　`{src['language']}`",
                       value=(src["name"] in st.session_state["enabled_srcs"]), key=f"src_{src['name']}"):
            enabled.append(src["name"])
    if st.button("💾 儲存來源設定", type="primary", key="save_srcs"):
        st.session_state["enabled_srcs"] = enabled
        st.success(f"已儲存，啟用 {len(enabled)} 個來源")


# ─────────────────────────────────────────────
# 已讀變淡：點過的新聞記在「你這個瀏覽器」的 localStorage，之後自動變淡
# （腳本放在 iframe 裡，透過 parent 操作頁面；每次重跑會先清掉舊的監聽器避免重複）
# ─────────────────────────────────────────────
_READ_JS = """
<script>
(function(){
  var P = window.parent, D, K = 'finnews_read', PK = 'finnews_prof', CK = 'finnews_clicks', S;
  try { D = P.document; } catch(e) { return; }
  if (P.__fnCleanup) { try { P.__fnCleanup(); } catch(e) {} }
  function jget(k, d){ try { return JSON.parse(P.localStorage.getItem(k) || d); } catch(e) { return JSON.parse(d); } }
  function jset(k, v){ try { P.localStorage.setItem(k, JSON.stringify(v)); } catch(e) {} }
  try { P.localStorage.removeItem('finnews_wl'); } catch(e) {}
  S = new Set(jget(K, '[]'));
  function saveRead(){ jset(K, Array.from(S).slice(-2000)); }
  function mainLink(el){
    if (el.classList.contains('card')) return el.querySelector('a.ct');
    return el.tagName === 'A' ? el : el.querySelector('a');
  }
  function markRead(){
    D.querySelectorAll('.card, .si').forEach(function(el){
      var a = mainLink(el);
      if (a && S.has(a.href)) el.classList.add('read');
    });
  }
  // ── 偏好：每次點擊替該卡片的股票/類股/類別/來源加分，半衰期 14 天 ──
  var HALF = 14 * 864e5;
  function eff(p, k){ var e = p[k]; return e ? e[0] * Math.pow(0.5, (Date.now() - e[1]) / HALF) : 0; }
  function feats(el){
    var f = [], d = el.dataset || {};
    (d.tk || '').split(',').forEach(function(t){ if (t) f.push(['tk:' + t, 1]); });
    (d.sec || '').split(',').forEach(function(t){ if (t) f.push(['sec:' + t, 1]); });
    if (d.cat) f.push(['cat:' + d.cat, 0.6]);
    if (d.src) f.push(['src:' + d.src, 0.3]);
    return f;
  }
  function learn(card){
    var p = jget(PK, '{}'), now = Date.now();
    feats(card).forEach(function(x){
      p[x[0]] = [eff(p, x[0]) + x[1], now];
    });
    var keys = Object.keys(p);
    if (keys.length > 300) {   // 太多就丟掉最弱的
      keys.sort(function(a, b){ return eff(p, b) - eff(p, a); });
      keys.slice(300).forEach(function(k){ delete p[k]; });
    }
    jset(PK, p); jset(CK, (jget(CK, '0') | 0) + 1);
  }
  function score(el, p){
    var s = 0, d = el.dataset || {};
    feats(el).forEach(function(x){ s += x[1] * eff(p, x[0]); });
    var age = parseFloat(d.age || '99');
    return s + 0.8 * Math.max(0, 1 - age / 36) + 0.05 * parseFloat(d.imp || '0');
  }
  function label(k){ return k.replace(/^(tk|sec|cat):/, ''); }
  function buildReco(){
    var wrap = D.querySelector('.fn-reco-wrap'), box = D.querySelector('.fn-reco'), pool = D.querySelector('.fn-pool');
    if (!wrap || !box || !pool) return;
    var note = wrap.querySelector('.fn-reco-note'), chips = wrap.querySelector('.fn-reco-chips');
    var clicks = jget(CK, '0') | 0, p = jget(PK, '{}');
    function setText(el, txt){ if (el.textContent !== txt) el.textContent = txt; }
    if (clicks < 3) {
      if (box.children.length) box.innerHTML = '';
      setText(chips, '');
      setText(note, '多點幾則你有興趣的新聞（目前 ' + clicks + '/3），這裡就會開始學你的喜好。記錄只存在你的瀏覽器。');
      return;
    }
    var top = Object.keys(p).filter(function(k){ return !/^src:/.test(k); })
      .sort(function(a, b){ return eff(p, b) - eff(p, a); }).slice(0, 4).map(label);
    setText(chips, top.length ? '· 你常看：' + top.join('、') : '');
    var cards = Array.prototype.slice.call(pool.querySelectorAll('.card')).filter(function(c){
      var a = mainLink(c); return a && !S.has(a.href);
    });
    var scored = cards.map(function(c){ return {c: c, s: score(c, p)}; }).sort(function(a, b){ return b.s - a.s; });
    var pick = [], secCount = {};
    for (var i = 0; i < scored.length && pick.length < 5; i++) {
      var sec = (scored[i].c.dataset.sec || '').split(',')[0] || '-';
      if ((secCount[sec] || 0) >= 2) continue;     // 同類股最多 2 張，避免一面倒
      secCount[sec] = (secCount[sec] || 0) + 1; pick.push(scored[i].c);
    }
    var rest = scored.map(function(x){ return x.c; }).filter(function(c){ return pick.indexOf(c) < 0; });
    rest.sort(function(a, b){ return parseFloat(b.dataset.imp || 0) - parseFloat(a.dataset.imp || 0); });
    if (rest.length) pick.push(rest[0]);            // 第 6 張：不看偏好的「重要新聞」，保留探索
    var sig = pick.map(function(c){ var a = mainLink(c); return a ? a.href : ''; }).join('|');
    setText(note, '依你的點擊紀錄挑選（最近的點擊權重較高，14 天減半）；最後一張是不看偏好的重要新聞。');
    if (box.getAttribute('data-sig') === sig && box.children.length) return;
    box.setAttribute('data-sig', sig);
    box.innerHTML = '';
    pick.forEach(function(c){ var n = c.cloneNode(true); n.classList.add('reco'); box.appendChild(n); });
    markRead();
  }
  // ── 收藏：存標題/連結/來源/時間的快照到 localStorage（上限 300 則）──
  var FK = 'finnews_fav';
  function favs(){ return jget(FK, '[]'); }
  function saveFavs(a){ jset(FK, a.slice(0, 300)); }
  function markFav(){
    var set = new Set(favs().map(function(f){ return f.u; }));
    D.querySelectorAll('.card .fav').forEach(function(el){
      var c = el.closest('.card'), a = c && c.querySelector('a.ct');
      var on = !!(a && set.has(a.href));
      if (el.classList.contains('on') !== on) { el.classList.toggle('on', on); el.textContent = on ? '★' : '☆'; }
    });
  }
  function toggleFav(card){
    var a = card.querySelector('a.ct'); if (!a) return;
    var d = card.dataset || {}, list = favs(), i = -1;
    list.forEach(function(f, k){ if (f.u === a.href) i = k; });
    if (i >= 0) list.splice(i, 1);
    else list.unshift({u: a.href, t: a.textContent.trim(), s: d.src || '', p: parseInt(d.ts || '0', 10), b: d.sent || '', a: Date.now()});
    saveFavs(list); markFav(); buildFav();
  }
  function removeFav(u){ saveFavs(favs().filter(function(f){ return f.u !== u; })); markFav(); buildFav(); }
  function rel(ts){
    if (!ts) return '';
    var s = Date.now() / 1000 - ts;
    if (s < 3600) return Math.max(1, Math.floor(s / 60)) + '分鐘前';
    if (s < 86400) return Math.floor(s / 3600) + '小時前';
    return Math.floor(s / 86400) + '天前';
  }
  function mk(tag, cls, txt){ var n = D.createElement(tag); if (cls) n.className = cls; if (txt) n.textContent = txt; return n; }
  function buildFav(){
    var box = D.querySelector('.fn-fav'), note = D.querySelector('.fn-fav-note');
    if (!box || !note) return;
    var list = favs();
    var sig = list.map(function(f){ return f.u; }).join('|');
    if (box.getAttribute('data-sig') === sig && note.textContent) return;
    box.setAttribute('data-sig', sig);
    note.textContent = '';
    if (!list.length) {
      note.textContent = '還沒有收藏。在新聞卡片右下角按 ☆ 就會收藏到這裡（只存在你這個瀏覽器，不受 72 小時限制）。';
      box.innerHTML = ''; return;
    }
    note.appendChild(D.createTextNode('共收藏 ' + list.length + ' 則（只存在你這個瀏覽器）'));
    var clr = mk('span', 'fav-clear', '清除全部'); clr.setAttribute('data-clear', '1'); note.appendChild(clr);
    box.innerHTML = '';
    list.forEach(function(f){
      var c = mk('div', 'favc'), b = mk('div', 'cb');
      var a = mk('a', 'ct', f.t); a.href = f.u; a.target = '_blank'; a.rel = 'noopener';
      var m = mk('div', 'cm');
      if (f.b) m.appendChild(mk('span', 'bd ' + (f.b.indexOf('利多') === 0 ? 'bd-b' : (f.b.indexOf('利空') === 0 ? 'bd-d' : 'bd-n')), f.b));
      m.appendChild(mk('span', '', f.s));
      if (f.p) m.appendChild(mk('span', '', '· ' + rel(f.p)));
      var x = mk('span', 'fav on', '★'); x.setAttribute('data-rm', f.u); x.title = '取消收藏';
      m.appendChild(x); b.appendChild(a); b.appendChild(m); c.appendChild(b); box.appendChild(c);
    });
  }
  // ── 列表 / 卡片密度（存在 localStorage，不需要重新整理）──
  var DK = 'finnews_dens';
  function applyDens(){
    var list = false;
    try { list = P.localStorage.getItem(DK) === 'list'; } catch(e) {}
    if (D.body.classList.contains('fn-compact') !== list) D.body.classList.toggle('fn-compact', list);
    D.querySelectorAll('.fn-dens span').forEach(function(sp){
      var on = (sp.getAttribute('data-dens') === 'list') === list;
      if (sp.classList.contains('on') !== on) sp.classList.toggle('on', on);
    });
  }
  // ── 自上次造訪新增 N 則：基準＝上一次造訪最後活躍的時間（存在 localStorage）──
  var LV = 'finnews_last_seen', BASE = 0;
  function initBase(){
    try {
      var b = parseInt(P.sessionStorage.getItem('fn_base') || '', 10);
      if (isNaN(b)) { b = parseInt(P.localStorage.getItem(LV) || '0', 10) || 0; P.sessionStorage.setItem('fn_base', String(b)); }
      BASE = b;
    } catch(e) { BASE = 0; }
  }
  function touch(){
    if (D.visibilityState === 'visible') { try { P.localStorage.setItem(LV, String(Date.now())); } catch(e) {} }
  }
  function fmtT(ms){
    var d = new Date(ms), p2 = function(n){ return (n < 10 ? '0' : '') + n; };
    var t = p2(d.getHours()) + ':' + p2(d.getMinutes());
    return new Date().toDateString() === d.toDateString() ? '今天 ' + t : (d.getMonth() + 1) + '/' + d.getDate() + ' ' + t;
  }
  function markNew(force){
    var banner = D.querySelector('.fn-new'); if (!banner) return;
    var cards = D.querySelectorAll('.wall .card'), n = 0;
    cards.forEach(function(c){
      var isNew = BASE > 0 && parseInt((c.dataset || {}).ts || '0', 10) * 1000 > BASE;
      if (isNew) n++;
      if (c.classList.contains('new') !== isNew) c.classList.toggle('new', isNew);
    });
    var only = D.body.classList.contains('fn-onlynew');
    var sig = BASE + '|' + n + '|' + only + '|' + cards.length;
    if (!force && banner.getAttribute('data-sig') === sig) return;
    banner.setAttribute('data-sig', sig);
    banner.innerHTML = '';
    if (BASE <= 0 || !cards.length) return;
    banner.appendChild(D.createTextNode(n ? '🆕 自上次造訪（' + fmtT(BASE) + '）新增 ' + n + ' 則'
                                          : '✓ 自上次造訪（' + fmtT(BASE) + '）沒有新的新聞'));
    if (n) { var tg = mk('span', 'tg', only ? '看全部' : '只看新增'); tg.setAttribute('data-onlynew', '1'); banner.appendChild(tg); }
    else if (only) { D.body.classList.remove('fn-onlynew'); }
  }
  function onVis(){
    if (D.visibilityState !== 'visible') return;
    var last = 0;
    try { last = parseInt(P.localStorage.getItem(LV) || '0', 10) || 0; } catch(e) {}
    if (last && Date.now() - last > 30 * 60 * 1000) {          // 離開超過 30 分鐘再回來：基準更新成「離開前最後活躍的時間」
      BASE = last;
      try { P.sessionStorage.setItem('fn_base', String(BASE)); } catch(e) {}
      markNew(true);
    }
    touch();
  }
  // ── 無限捲動：卡片先全部輸出、第 N 張之後隱藏（data-lz），捲到底部哨兵時分批顯示 ──
  var io = null, sents = [];
  function reveal(grid){
    var hid = grid.querySelectorAll(':scope > .card[data-lz]');
    for (var i = 0; i < hid.length && i < 24; i++) hid[i].removeAttribute('data-lz');
    return hid.length > 24;
  }
  function ensureLazy(){
    var now = Array.prototype.slice.call(D.querySelectorAll('.fn-sentinel'));
    var same = now.length === sents.length && now.every(function(x, i){ return x === sents[i]; });
    if (same) return;
    if (io) io.disconnect();
    sents = now;
    io = new P.IntersectionObserver(function(es){
      es.forEach(function(e){
        if (!e.isIntersecting) return;
        var g = e.target.previousElementSibling;
        if (g && reveal(g)) { io.unobserve(e.target); io.observe(e.target); }   // 還有沒顯示的：重新觀察，哨兵仍在畫面內會再觸發
      });
    }, {rootMargin: '900px 0px'});
    sents.forEach(function(x){ io.observe(x); });
  }
  // ── 切換分頁：捲到「該分頁內容的最上面」（分頁列是 sticky，所以內容頂端剛好貼在分頁列下方）──
  function scrollerOf(el){
    for (var n = el.parentElement; n && n !== D.body; n = n.parentElement) {
      var oy = P.getComputedStyle(n).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && n.scrollHeight > n.clientHeight) return n;
    }
    return D.scrollingElement || D.documentElement;
  }
  function scrollToTabTop(tab){
    var list = tab.closest('[role="tablist"]');
    if (!list || list !== D.querySelector('[role="tablist"]')) return;     // 只處理最上面那排主分頁
    var panel = D.querySelector('[role="tabpanel"]:not([hidden])');
    var sc = scrollerOf(list);
    var isDoc = (sc === D.scrollingElement || sc === D.documentElement);
    var base = isDoc ? 0 : sc.getBoundingClientRect().top;
    var cur = isDoc ? (P.pageYOffset || sc.scrollTop) : sc.scrollTop;
    var target = 0;
    if (panel) target = Math.max(0, panel.getBoundingClientRect().top - base + cur - list.getBoundingClientRect().height);
    if (cur > target) { if (isDoc) P.scrollTo(0, target); else sc.scrollTop = target; }
  }
  function onClick(e){
    var tg = e.target, cl = function(sel){ return tg && tg.closest ? tg.closest(sel) : null; };
    var dn = cl('[data-dens]');
    if (dn) { e.preventDefault(); try { P.localStorage.setItem(DK, dn.getAttribute('data-dens')); } catch(x) {} applyDens(); return; }
    if (cl('[data-onlynew]')) { e.preventDefault(); D.body.classList.toggle('fn-onlynew'); markNew(true); return; }
    var tabEl = cl('[role="tab"]');
    if (tabEl) { setTimeout(function(){ scrollToTabTop(tabEl); }, 80); setTimeout(function(){ scrollToTabTop(tabEl); }, 300); }
    var fv = cl('.card .fav');
    if (fv) { e.preventDefault(); e.stopPropagation(); toggleFav(fv.closest('.card')); return; }
    var rm = cl('[data-rm]');
    if (rm) { e.preventDefault(); removeFav(rm.getAttribute('data-rm')); return; }
    if (cl('[data-clear]')) {
      e.preventDefault();
      if (P.confirm('確定清除全部收藏？')) { saveFavs([]); markFav(); buildFav(); }
      return;
    }
    var a = e.target.closest ? e.target.closest('a[href^="http"]') : null;
    var clr = e.target.closest ? e.target.closest('.fn-reco-clear') : null;
    if (clr) {
      e.preventDefault();
      try { P.localStorage.removeItem(PK); P.localStorage.removeItem(CK); } catch(x) {}
      var b = D.querySelector('.fn-reco'); if (b) b.removeAttribute('data-sig');
      buildReco(); return;
    }
    if (!a) return;
    var box = a.closest('.card, .si');
    if (!box) return;
    var m = mainLink(box);
    S.add(a.href); if (m) S.add(m.href);
    saveRead();
    if (box.classList.contains('card')) learn(box);
    markRead();
    setTimeout(buildReco, 400);
  }
  var t = null;
  var obs = new MutationObserver(function(){ clearTimeout(t); t = setTimeout(function(){ markRead(); buildReco(); markFav(); buildFav(); applyDens(); markNew(); ensureLazy(); }, 200); });
  obs.observe(D.body, {childList: true, subtree: true});
  D.addEventListener('click', onClick, true);
  P.__fnCleanup = function(){ obs.disconnect(); D.removeEventListener('click', onClick, true); D.removeEventListener('visibilitychange', onVis); clearInterval(tt); if (io) io.disconnect(); D.documentElement.removeAttribute('data-fnjs'); };
  D.documentElement.setAttribute('data-fnjs', '1');
  initBase(); touch();
  var tt = setInterval(touch, 60000);
  D.addEventListener('visibilitychange', onVis);
  markRead(); buildReco(); markFav(); buildFav(); applyDens(); markNew(); ensureLazy();
})();
</script>
"""
try:                                   # 新版 Streamlit 用 st.iframe；舊版退回 components.html
    st.iframe(_READ_JS, height=1)
except Exception:
    try:
        import streamlit.components.v1 as components
        components.html(_READ_JS, height=0)
    except Exception:
        pass                           # 已讀功能失敗不影響頁面其他功能
