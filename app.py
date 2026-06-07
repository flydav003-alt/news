"""
app.py - FinNews AI v2.2
精簡頂部 · 12小時新聞過濾 · 更緊湊 UI
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from database import (init_db, SessionLocal, get_articles_df,
                      get_sentiment_counts, get_sector_counts,
                      get_ticker_counts, get_crawl_logs, TZ_TW)
from scheduler import start_scheduler, crawl_and_save, next_run_time, update_interval
from crawler import SOURCES


def now_tw_str():
    return datetime.now(TZ_TW).strftime("%H:%M:%S")


def filter_12h(df):
    """只保留 12 小時內新聞"""
    if df is None or df.empty:
        return df
    cutoff = datetime.now(TZ_TW) - timedelta(hours=12)
    mask = df["published_at"].apply(
        lambda x: x is not None and (
            x.astimezone(TZ_TW) >= cutoff if getattr(x, "tzinfo", None) else True
        )
    )
    filtered = df[mask]
    return filtered if not filtered.empty else df  # fallback 防空


# ─────────────────────────────────────────────
# AI 市場總結
# ─────────────────────────────────────────────
def get_daily_ai_summary(ai_news_df):
    import requests
    groq_key = ""
    try:
        groq_key = st.secrets.get("GROQ_API_KEY", "")
    except Exception:
        pass
    if not groq_key:
        groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return "", "找不到 GROQ_API_KEY"

    lines = []
    for _, r in ai_news_df.head(15).iterrows():
        label = {"bullish": "利多", "bearish": "利空"}.get(r.get("ai_sentiment", ""), "中性")
        text = r.get("ai_summary") or r.get("title", "")
        lines.append(f"[{label}] {text}")
    news_text = "\n".join(lines)
    if not news_text.strip():
        return "", "沒有可用 AI 新聞素材"

    prompt = f"""以下是今日台灣財經新聞的 AI 分析摘要：

{news_text}

請用繁體中文輸出今日台股 AI 總結，嚴格按以下 JSON 格式回應，不要有任何其他文字：

{{
  "direction": "偏多/偏空/震盪（三擇一）",
  "direction_reason": "一句話說明整體方向原因（30字內）",
  "bull_themes": ["利多主題1（20字內）", "利多主題2（20字內）"],
  "bear_themes": ["利空主題1（20字內）", "利空主題2（20字內）"],
  "key_tickers": ["2330", "NVDA"],
  "summary": "一段客觀總結（60至100字，財經播報員語氣）"
}}"""

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 600, "temperature": 0.2},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip()), ""
    except requests.exceptions.Timeout:
        return "", "Groq API 逾時，請稍後重試"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        if status == 429:
            return "", "Groq 速率限制（429），請等 1 分鐘後重試"
        return "", f"Groq HTTP 錯誤 {status}"
    except Exception as e:
        return "", f"生成失敗：{e}"


# ─────────────────────────────────────────────
# 頁面設定
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="FinNews AI",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [class*="css"], .stApp {
  font-family: 'Noto Sans TC', sans-serif !important;
  background-color: #F6F8FA !important;
  color: #1A1A2E !important;
}

/* ── 完全移除 header ── */
header[data-testid="stHeader"] { display: none !important; }

/* ── 隱藏 sidebar ── */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div,
button[data-testid="collapsedControl"],
button[aria-label="Close sidebar"],
button[aria-label="Open sidebar"] { display: none !important; }

/* ── 消除所有上層容器的 padding — 多層保險 ── */
html, body { margin: 0 !important; padding: 0 !important; }
[data-testid="stAppViewContainer"] { padding-top: 0 !important; margin-top: 0 !important; }
[data-testid="stAppViewContainer"] > section.main { padding-top: 0 !important; }
[data-testid="stMain"] { padding-top: 0 !important; }
.main .block-container,
[data-testid="stMain"] .block-container,
section.main .block-container {
  padding-top: 6px !important;
  padding-bottom: 20px !important;
  max-width: 1400px !important;
  margin-top: 0 !important;
}
.appview-container { padding-top: 0 !important; }
.appview-container .main { padding-top: 0 !important; }

/* ── 緊湊 topbar ── */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #FFFFFF;
  border: 1px solid #E2E8F0;
  border-radius: 10px;
  padding: 7px 14px;
  margin-bottom: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.topbar-logo {
  font-size: 14px;
  font-weight: 700;
  color: #1A1A2E;
  white-space: nowrap;
  flex-shrink: 0;
}
.topbar-status-ok {
  font-size: 10px; font-weight: 600; color: #16A34A;
  background: #F0FDF4; border: 1px solid #BBF7D0;
  border-radius: 20px; padding: 2px 8px; white-space: nowrap; flex-shrink: 0;
}
.topbar-status-warn {
  font-size: 10px; font-weight: 600; color: #D97706;
  background: #FFFBEB; border: 1px solid #FDE68A;
  border-radius: 20px; padding: 2px 8px; white-space: nowrap; flex-shrink: 0;
}
.topbar-time {
  font-size: 10px; color: #94A3B8;
  font-family: 'JetBrains Mono', monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* ── 抓取按鈕列：固定高度，不讓按鈕撐高整行 ── */
.fetch-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

/* ── Tabs 更緊湊 ── */
.stTabs [data-baseweb="tab-list"] {
  background: #FFFFFF;
  border-radius: 8px;
  padding: 3px;
  gap: 2px;
  border: 1px solid #E2E8F0;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  margin-bottom: 8px !important;
}
.stTabs [data-baseweb="tab"] {
  font-size: 13px; font-weight: 600;
  padding: 6px 18px; border-radius: 6px;
  color: #64748B;
}
.stTabs [aria-selected="true"] {
  background: #1A1A2E !important;
  color: #FFFFFF !important;
}
.stTabs [data-baseweb="tab-panel"] {
  padding-top: 4px !important;
}

/* ── Metrics 更緊湊 ── */
[data-testid="metric-container"] {
  background: #FFFFFF;
  border: 1px solid #E2E8F0;
  border-radius: 10px;
  padding: 10px 14px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
[data-testid="stMetricLabel"] { color: #64748B !important; font-size: 11px !important; font-weight: 600 !important; }
[data-testid="stMetricValue"] { color: #1A1A2E !important; font-size: 20px !important; font-weight: 700 !important; }
[data-testid="stMetricDelta"] { font-size: 11px !important; }

/* ── Buttons ── */
.stButton > button {
  border-radius: 7px; font-weight: 600; font-size: 13px;
  border: 1px solid #E2E8F0; background: #FFFFFF; color: #374151;
  transition: all 0.15s; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  height: 36px !important; padding: 0 28px !important;
  white-space: nowrap !important;
}
.stButton > button:hover { background: #F1F5F9; border-color: #CBD5E1; }
.stButton > button[kind="primary"] {
  background: linear-gradient(135deg, #DC2626 0%, #B91C1C 100%);
  border-color: #DC2626; color: #FFFFFF;
  box-shadow: 0 2px 4px rgba(220,38,38,0.25);
}
.stButton > button[kind="primary"]:hover { opacity: 0.92; }

/* ── Checkbox 更緊湊 ── */
.stCheckbox { margin-bottom: 0 !important; }
.stCheckbox label { color: #374151 !important; font-size: 13px !important; }

/* ── Selectbox / Input ── */
.stSelectbox > div > div,
.stTextInput > div > div > input {
  background: #FFFFFF !important;
  border: 1px solid #E2E8F0 !important;
  border-radius: 7px !important;
  color: #1A1A2E !important;
  font-size: 13px !important;
}
.stSelectbox label, .stTextInput label { color: #64748B !important; font-size: 11px !important; font-weight: 600 !important; }

/* ── 篩選列：縮小 padding ── */
div[data-testid="column"] { padding-left: 4px !important; padding-right: 4px !important; }

/* ── Radio ── */
.stRadio label { color: #374151 !important; font-size: 13px !important; font-weight: 500 !important; }
.stRadio > div { gap: 6px !important; }

/* ── Divider ── */
hr { border-color: #E2E8F0 !important; margin: 10px 0 !important; }
.stCaption { color: #94A3B8 !important; font-size: 11px !important; }

/* ── Section Header ── */
.sec-hd {
  font-size: 10px; font-weight: 700; color: #94A3B8;
  letter-spacing: 1.2px; text-transform: uppercase;
  margin: 14px 0 7px;
  display: flex; align-items: center; gap: 7px;
}
.sec-hd::after { content: ''; flex: 1; height: 1px; background: #E2E8F0; }

/* ══════════════════════════════════════
   AI 總結卡片（更緊湊）
══════════════════════════════════════ */
.ai-card {
  background: #FFFFFF; border: 1px solid #E2E8F0;
  border-radius: 12px; padding: 16px 20px; margin-bottom: 6px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.05); border-top: 3px solid #1A1A2E;
}
.ai-badge {
  font-size: 9px; font-weight: 700; letter-spacing: 1.2px;
  color: #D97706; background: #FFFBEB; border: 1px solid #FDE68A;
  border-radius: 4px; padding: 2px 7px; text-transform: uppercase;
  display: inline-block; margin-bottom: 8px;
}
.ai-dir-bull { font-size: 17px; font-weight: 700; color: #DC2626; }
.ai-dir-bear { font-size: 17px; font-weight: 700; color: #16A34A; }
.ai-dir-neu  { font-size: 17px; font-weight: 700; color: #64748B; }
.ai-dir-reason { font-size: 12px; color: #64748B; margin: 2px 0 10px; }
.ai-themes { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.ai-tag-bull {
  background: #FEF2F2; border: 1px solid #FECACA; border-radius: 5px;
  padding: 3px 10px; font-size: 11px; color: #DC2626; font-weight: 600;
}
.ai-tag-bear {
  background: #F0FDF4; border: 1px solid #BBF7D0; border-radius: 5px;
  padding: 3px 10px; font-size: 11px; color: #16A34A; font-weight: 600;
}
.ai-tickers { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 10px; }
.ai-tick-chip {
  background: #F1F5F9; border: 1px solid #CBD5E1; border-radius: 4px;
  padding: 2px 8px; font-size: 11px; color: #1A1A2E;
  font-family: 'JetBrains Mono', monospace; font-weight: 600;
}
.ai-body {
  font-size: 13px; line-height: 1.8; color: #374151;
  border-top: 1px solid #E2E8F0; padding-top: 10px;
}
.ai-footer { font-size: 10px; color: #CBD5E1; margin-top: 8px; }

/* ══════════════════════════════════════
   GEO 警示
══════════════════════════════════════ */
.geo-card {
  background: #FFFBEB; border: 1px solid #FDE68A;
  border-left: 4px solid #F59E0B; border-radius: 8px;
  padding: 9px 13px; margin-bottom: 6px;
  display: flex; gap: 10px; align-items: flex-start;
}
.geo-icon { font-size: 14px; flex-shrink: 0; margin-top: 2px; }
.geo-title { font-size: 13px; font-weight: 700; color: #92400E; margin-bottom: 2px; }
.geo-title a { color: #92400E; text-decoration: none; }
.geo-title a:hover { text-decoration: underline; }
.geo-meta { font-size: 11px; color: #B45309; font-weight: 600; margin-bottom: 2px; }
.geo-body { font-size: 11px; color: #78350F; line-height: 1.5; }

/* ══════════════════════════════════════
   新聞卡片
══════════════════════════════════════ */
.nw {
  background: #FFFFFF; border: 1px solid #E2E8F0;
  border-radius: 8px; padding: 10px 13px; margin-bottom: 5px;
  border-left: 3px solid #E2E8F0;
  box-shadow: 0 1px 2px rgba(0,0,0,0.03);
  transition: box-shadow 0.15s, border-left-color 0.15s;
}
.nw:hover { box-shadow: 0 3px 8px rgba(0,0,0,0.07); }
.nw.bull  { border-left-color: #DC2626; }
.nw.bear  { border-left-color: #16A34A; }
.nw.geo   { border-left-color: #F59E0B; }
.nw-title {
  font-size: 13.5px; font-weight: 600; color: #1A1A2E;
  line-height: 1.5; margin-bottom: 5px;
}
.nw-title a { color: #1A1A2E; text-decoration: none; }
.nw-title a:hover { color: #DC2626; }
.nw-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.nw-score-bull {
  font-size: 10px; font-weight: 700; color: #DC2626; background: #FEF2F2;
  border-radius: 3px; padding: 1px 6px; font-family: 'JetBrains Mono', monospace;
}
.nw-score-bear {
  font-size: 10px; font-weight: 700; color: #16A34A; background: #F0FDF4;
  border-radius: 3px; padding: 1px 6px; font-family: 'JetBrains Mono', monospace;
}
.nw-score-neu {
  font-size: 10px; font-weight: 600; color: #94A3B8; background: #F1F5F9;
  border-radius: 3px; padding: 1px 6px; font-family: 'JetBrains Mono', monospace;
}
.nw-badge-ai {
  font-size: 9px; font-weight: 700; letter-spacing: 0.5px;
  color: #D97706; background: #FFFBEB; border: 1px solid #FDE68A;
  border-radius: 3px; padding: 1px 5px;
}
.nw-badge-geo {
  font-size: 9px; font-weight: 700; color: #92400E; background: #FFFBEB;
  border: 1px solid #FDE68A; border-radius: 3px; padding: 1px 5px;
}
.nw-tick {
  font-size: 10px; font-weight: 600; color: #2563EB; background: #EFF6FF;
  border-radius: 3px; padding: 1px 5px; font-family: 'JetBrains Mono', monospace;
}
.nw-src { font-size: 10px; color: #94A3B8; }
.nw-time { font-size: 10px; color: #CBD5E1; font-family: 'JetBrains Mono', monospace; }
.nw-ai-box {
  margin-top: 7px; padding: 8px 11px;
  background: #F8FAFC; border-radius: 6px;
  border-left: 3px solid #F59E0B;
  font-size: 12px; color: #374151; line-height: 1.7;
}
.nw-ai-reason { margin-top: 4px; font-size: 10px; color: #94A3B8; }

/* ══════════════════════════════════════
   熱門股票卡片
══════════════════════════════════════ */
.tk-card {
  background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px;
  padding: 10px; margin-bottom: 6px; text-align: center;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04); transition: box-shadow 0.15s;
}
.tk-card:hover { box-shadow: 0 3px 10px rgba(0,0,0,0.07); }
.tk-code { font-size: 15px; font-weight: 700; color: #1A1A2E; font-family: 'JetBrains Mono', monospace; }
.tk-name { font-size: 10px; color: #94A3B8; margin: 1px 0 4px; }
.tk-bull { color: #DC2626; font-size: 11px; font-weight: 700; }
.tk-bear { color: #16A34A; font-size: 11px; font-weight: 700; }
.tk-neu  { color: #94A3B8; font-size: 11px; font-weight: 600; }
.tk-cnt  { font-size: 10px; color: #CBD5E1; }

/* ══════════════════════════════════════
   空狀態
══════════════════════════════════════ */
.empty-box { text-align: center; padding: 36px 24px; color: #CBD5E1; }
.empty-box-icon { font-size: 30px; margin-bottom: 8px; }
.empty-box-txt { font-size: 13px; }

/* ══════════════════════════════════════
   日誌表格
══════════════════════════════════════ */
.log-table {
  width: 100%; border-collapse: collapse; font-size: 12px;
  background: #FFFFFF; border: 1px solid #E2E8F0;
  border-radius: 8px; overflow: hidden;
}
.log-table th {
  padding: 8px 12px; text-align: left; font-size: 10px; font-weight: 700;
  color: #64748B; letter-spacing: 0.6px; background: #F8FAFC;
  border-bottom: 1px solid #E2E8F0;
}
.log-table td { padding: 7px 12px; color: #374151; border-bottom: 1px solid #F1F5F9; }
.log-ok   { background: #F0FDF4; color: #16A34A; font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 4px; }
.log-err  { background: #FEF2F2; color: #DC2626; font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 4px; }
.log-warn { background: #FFFBEB; color: #D97706; font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 4px; }

/* ── 篩選列整體縮小間距 ── */
.filter-row .stSelectbox, .filter-row .stTextInput { margin-bottom: 0 !important; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 初始化
# ─────────────────────────────────────────────
if "initialized" not in st.session_state:
    init_db()
    start_scheduler(interval_minutes=30)
    try:
        groq_ok = bool(st.secrets.get("GROQ_API_KEY", ""))
    except Exception:
        groq_ok = False
    st.session_state.update({
        "initialized":  True,
        "last_update":  "尚未更新",
        "custom_bull":  {},
        "custom_bear":  {},
        "enabled_srcs": [s["name"] for s in SOURCES if s["enabled"]],
        "interval":     30,
        "groq_ok":      groq_ok,
        "use_ai":       groq_ok,
    })


# ─────────────────────────────────────────────
# 共用：渲染新聞清單
# ─────────────────────────────────────────────
def render_news(df, max_items=120):
    if df is None or df.empty:
        st.markdown("""
        <div class="empty-box">
          <div class="empty-box-icon">📭</div>
          <div class="empty-box-txt">沒有符合條件的新聞</div>
        </div>""", unsafe_allow_html=True)
        return

    chunks = []
    for _, row in df.head(max_items).iterrows():
        sent     = row.get("sentiment", "neutral")
        ai_sent  = row.get("ai_sentiment", "") or ""
        is_geo   = bool(row.get("is_geo", False))
        ai_score = float(row.get("ai_score", 0) or 0)
        kw_score = float(row.get("sentiment_score", 0) or 0)
        ai_sum   = str(row.get("ai_summary", "") or "")
        ai_rsn   = str(row.get("ai_reason", "") or "")
        title    = str(row.get("title", ""))
        url      = str(row.get("url", "") or "")
        source   = str(row.get("source", "") or "")
        traw     = str(row.get("ai_affected_tickers", "") or row.get("tickers", "") or "")
        tickers  = [t.strip() for t in traw.split(",") if t.strip()]

        pub_str = ""
        if row.get("published_at") is not None:
            try:
                pub_str = row["published_at"].astimezone(TZ_TW).strftime("%m/%d %H:%M")
            except Exception:
                try:
                    pub_str = row["published_at"].strftime("%m/%d %H:%M")
                except Exception:
                    pass

        eff = ai_sent if ai_sent in ("bullish", "bearish") else sent
        if is_geo:
            cls = "nw geo"
        elif eff == "bullish":
            cls = "nw bull"
        elif eff == "bearish":
            cls = "nw bear"
        else:
            cls = "nw"

        sv = ai_score if ai_score != 0 else kw_score * 10
        if sv > 0:
            score_h = f'<span class="nw-score-bull">+{sv:.1f}</span>'
        elif sv < 0:
            score_h = f'<span class="nw-score-bear">{sv:.1f}</span>'
        else:
            score_h = '<span class="nw-score-neu">—</span>'

        t_html = f'<a href="{url}" target="_blank">{title}</a>' if url else title

        badges = []
        if ai_sum:
            badges.append('<span class="nw-badge-ai">&#10022; AI</span>')
        if is_geo:
            badges.append('<span class="nw-badge-geo">&#9873; 地緣</span>')
        for t in tickers[:4]:
            badges.append(f'<span class="nw-tick">{t}</span>')
        bdg = " ".join(badges)

        ai_block = ""
        if ai_sum:
            rsn_part = f'<div class="nw-ai-reason">&#128204; {ai_rsn}</div>' if ai_rsn else ""
            ai_block = f'<div class="nw-ai-box">{ai_sum}{rsn_part}</div>'

        chunks.append(f"""
<div class="{cls}">
  <div class="nw-title">{t_html}</div>
  <div class="nw-meta">
    {score_h} {bdg}
    <span class="nw-src">{source}</span>
    <span class="nw-time">{pub_str}</span>
  </div>
  {ai_block}
</div>""")

    st.markdown("\n".join(chunks), unsafe_allow_html=True)
    if len(df) > max_items:
        st.caption(f"顯示前 {max_items} 則，共 {len(df)} 則")


# ─────────────────────────────────────────────
# ① Topbar：單行，Logo + 狀態 + 時間
# ─────────────────────────────────────────────
_groq_ok = st.session_state["groq_ok"]
_status_html = (
    '<span class="topbar-status-ok">&#9679; Groq AI</span>'
    if _groq_ok else
    '<span class="topbar-status-warn">&#9888; 關鍵字模式</span>'
)
_next_run = next_run_time()
_last_upd = st.session_state["last_update"]

st.markdown(f"""
<div class="topbar">
  <div class="topbar-left">
    <span class="topbar-logo">📈 FinNews AI</span>
    {_status_html}
    <span class="topbar-time">下次 {_next_run} · 最後 {_last_upd}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ② 控制列：AI checkbox + 立即抓取，固定在同一行，按鈕有邊框不撐高
_c1, _c2, _c3 = st.columns([2, 1, 5])
with _c1:
    st.session_state["use_ai"] = st.checkbox(
        "啟用 AI 深度分析",
        value=st.session_state["use_ai"],
        disabled=not _groq_ok,
        key="use_ai_cb",
    )
with _c2:
    if st.button("🔄 立即抓取新聞", type="primary"):
        with st.spinner("抓取＋分析中，約 30～60 秒…"):
            result = crawl_and_save(
                enabled_names=st.session_state["enabled_srcs"],
                custom_bull=st.session_state["custom_bull"],
                custom_bear=st.session_state["custom_bear"],
                use_ai=st.session_state["use_ai"],
            )
            st.session_state["last_update"] = now_tw_str()
            st.cache_data.clear()
        ai_info = f"｜AI {result.get('ai_count', 0)} 則" if st.session_state["use_ai"] else ""
        st.success(f"✅ 新增 **{result['saved']}** 則｜去重 {result['skipped']} 則{ai_info}｜{result['elapsed']}s")
        st.rerun()


# ─────────────────────────────────────────────
# 主體 Tabs
# ─────────────────────────────────────────────
tab_dash, tab_deep, tab_cfg = st.tabs([
    "📊 今日速覽", "🔍 深度篩選", "⚙️ 設定"
])


# ═══════════════════════════════════════════════
# TAB 1：今日速覽
# ═══════════════════════════════════════════════
with tab_dash:

    @st.cache_data(ttl=60, show_spinner=False)
    def load_dash():
        db = SessionLocal()
        try:
            df      = get_articles_df(db, limit=500)
            counts  = get_sentiment_counts(db)
            secs    = get_sector_counts(db)
            tickers = get_ticker_counts(db, limit=30)
            geo_df  = get_articles_df(db, geo_only=True, limit=10)
        finally:
            db.close()
        return df, counts, secs, tickers, geo_df

    @st.cache_data(ttl=60, show_spinner=False)
    def load_ai_12h():
        db = SessionLocal()
        try:
            all_ai = get_articles_df(db, ai_only=True, limit=2000)
        finally:
            db.close()
        return filter_12h(all_ai)

    df, counts, secs, hot_tickers, geo_df = load_dash()
    ai_12h = load_ai_12h()

    # 12h 篩選後的 df 用於最新新聞區
    df_12h = filter_12h(df)

    total  = sum(counts.values())
    bull_n = counts.get("bullish", 0)
    bear_n = counts.get("bearish", 0)
    mid_n  = counts.get("neutral", 0)

    # ── 頂部指標（5欄，更緊湊）──
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📰 新聞總數", total)
    if total:
        c2.metric("📈 利多", bull_n, delta=f"{bull_n/total*100:.1f}%")
        c3.metric("📉 利空", bear_n, delta=f"-{bear_n/total*100:.1f}%", delta_color="inverse")
    else:
        c2.metric("📈 利多", 0)
        c3.metric("📉 利空", 0)
    c4.metric("✦ AI 分析", len(ai_12h))
    c5.metric("⚑ 地緣政治", len(geo_df))

    # ── AI 市場總結 ──
    st.markdown('<div class="sec-hd">✦ AI 市場總結</div>', unsafe_allow_html=True)

    if not st.session_state["groq_ok"]:
        st.info("需要設定 Groq API Key 才能顯示 AI 市場總結")
    elif ai_12h.empty:
        st.info("尚無 AI 分析資料，請先抓取新聞並啟用 AI 深度分析")
    else:
        ts_key = str(ai_12h.iloc[0].get("published_at", ""))
        cache_key = f"ds_v22_{ts_key}"

        if st.session_state.get("_sum_key") != cache_key:
            with st.spinner("AI 正在生成今日市場總結…"):
                sort_col = "importance_score" if "importance_score" in ai_12h.columns else "ai_score"
                sdata, serr = get_daily_ai_summary(ai_12h.sort_values(sort_col, ascending=False))
            st.session_state["_sdata"]   = sdata
            st.session_state["_serr"]    = serr
            st.session_state["_sum_key"] = cache_key
            st.session_state["_stime"]   = datetime.now(TZ_TW).strftime("%H:%M")
        else:
            sdata = st.session_state.get("_sdata", "")
            serr  = st.session_state.get("_serr", "")

        if sdata and isinstance(sdata, dict):
            direction = sdata.get("direction", "震盪")
            dir_r     = sdata.get("direction_reason", "")
            bulls     = sdata.get("bull_themes", [])
            bears     = sdata.get("bear_themes", [])
            keys      = sdata.get("key_tickers", [])
            sumtxt    = sdata.get("summary", "")
            gen_t     = st.session_state.get("_stime", "")

            dir_cls  = "ai-dir-bull" if direction == "偏多" else ("ai-dir-bear" if direction == "偏空" else "ai-dir-neu")
            dir_icon = "&#8599;" if direction == "偏多" else ("&#8600;" if direction == "偏空" else "&#8594;")

            bull_tags = "".join(f'<span class="ai-tag-bull">&#128200; {t}</span>' for t in bulls)
            bear_tags = "".join(f'<span class="ai-tag-bear">&#128201; {t}</span>' for t in bears)
            tick_tags = "".join(f'<span class="ai-tick-chip">{t}</span>' for t in keys)
            tick_html = f'<div style="font-size:10px;color:#94A3B8;margin-bottom:3px">關注個股</div><div class="ai-tickers">{tick_tags}</div>' if tick_tags else ""

            st.markdown(f"""
<div class="ai-card">
  <span class="ai-badge">Groq AI · 今日總結</span>
  <div class="{dir_cls}">{dir_icon} 整體{direction}</div>
  <div class="ai-dir-reason">{dir_r}</div>
  <div class="ai-themes">{bull_tags}{bear_tags}</div>
  {tick_html}
  <div class="ai-body">{sumtxt}</div>
  <div class="ai-footer">根據 {len(ai_12h)} 則 AI 分析新聞生成 · {gen_t} 台灣時間</div>
</div>""", unsafe_allow_html=True)

            if st.button("🔄 重新生成總結", key="regen"):
                st.session_state.pop("_sum_key", None)
                st.rerun()

        elif serr:
            st.error(f"AI 總結生成失敗：{serr}")
            if st.button("🔄 重試", key="regen_err"):
                st.session_state.pop("_sum_key", None)
                st.rerun()

    # ── 地緣政治警示（折疊，預設展開）──
    if not geo_df.empty:
        geo_12h = filter_12h(geo_df)
        if not geo_12h.empty:
            st.markdown('<div class="sec-hd">⚑ 地緣政治警示</div>', unsafe_allow_html=True)
            geo_chunks = []
            for _, row in geo_12h.head(3).iterrows():
                eff    = row.get("ai_sentiment", "") or row.get("sentiment", "neutral")
                impact = "利多" if eff == "bullish" else ("利空" if eff == "bearish" else "中性")
                url_g  = row.get("url", "")
                ttl_g  = row.get("title", "")
                sum_g  = str(row.get("ai_summary", "") or "")
                link_h = f'<a href="{url_g}" target="_blank">{ttl_g}</a>' if url_g else ttl_g
                body_h = f'<div class="geo-body">{sum_g}</div>' if sum_g else ""
                geo_chunks.append(f"""
<div class="geo-card">
  <div class="geo-icon">&#9873;</div>
  <div>
    <div class="geo-title">{link_h}</div>
    <div class="geo-meta">{impact}</div>
    {body_h}
  </div>
</div>""")
            st.markdown("\n".join(geo_chunks), unsafe_allow_html=True)

    # ── 今日重點新聞 + 圖表（兩欄）──
    st.markdown('<div class="sec-hd">🔑 今日重點新聞（12h）</div>', unsafe_allow_html=True)
    col_news, col_chart = st.columns([3, 2])

    with col_news:
        if ai_12h.empty:
            st.markdown('<div class="empty-box"><div class="empty-box-icon">📭</div><div class="empty-box-txt">請先抓取並啟用 AI 分析</div></div>', unsafe_allow_html=True)
        else:
            key_df = ai_12h[ai_12h["ai_sentiment"].isin(["bullish", "bearish"])]
            if key_df.empty:
                key_df = ai_12h.copy()
            if "importance_score" in key_df.columns:
                key_df = key_df.sort_values("importance_score", ascending=False)
            else:
                key_df = key_df.reindex(key_df["ai_score"].abs().sort_values(ascending=False).index)
            render_news(key_df, max_items=25)

    with col_chart:
        # 情緒圓餅（更小）
        if total > 0:
            fig_pie = go.Figure(go.Pie(
                labels=["利多", "利空", "中性"],
                values=[bull_n, bear_n, mid_n],
                hole=0.60,
                marker=dict(colors=["#DC2626", "#16A34A", "#E2E8F0"],
                            line=dict(color="#FFFFFF", width=2)),
                textinfo="percent+label",
                textfont=dict(size=11, color="#374151"),
                showlegend=False,
            ))
            fig_pie.update_layout(
                margin=dict(t=4, b=4, l=4, r=4), height=170,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        # 類股橫條（更小）
        if not secs.empty:
            top_s = secs.head(7)
            fig_sec = go.Figure(go.Bar(
                x=top_s["count"], y=top_s["sector"], orientation="h",
                marker=dict(color="#1A1A2E"),
                text=top_s["count"], textposition="outside",
                textfont=dict(size=10, color="#64748B"),
            ))
            fig_sec.update_layout(
                yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=10)),
                xaxis=dict(showgrid=False, visible=False),
                margin=dict(t=4, b=4, l=4, r=35), height=200,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_sec, use_container_width=True)

        # 熱門股 Top5
        if not hot_tickers.empty:
            st.markdown('<div class="sec-hd" style="margin-top:4px">🔥 熱門個股</div>', unsafe_allow_html=True)
            tk_chunks = []
            for _, row in hot_tickers.head(5).iterrows():
                sc = row["平均情緒"]
                if sc >= 0.15:
                    sc_h = f'<span class="tk-bull">+{sc:.2f}</span>'
                elif sc <= -0.15:
                    sc_h = f'<span class="tk-bear">{sc:.2f}</span>'
                else:
                    sc_h = f'<span class="tk-neu">{sc:.2f}</span>'
                market = row.get("市場", "TW")
                lk = f"https://tw.stock.yahoo.com/quote/{row['代碼']}" if market == "TW" else f"https://finance.yahoo.com/quote/{row['代碼']}"
                tk_chunks.append(f"""
<div class="tk-card">
  <a href="{lk}" target="_blank" style="text-decoration:none">
    <div class="tk-code">{row['代碼']}</div>
  </a>
  <div class="tk-name">{row['名稱']}</div>
  <div>{sc_h} <span class="tk-cnt">· {row['出現次數']} 則</span></div>
</div>""")
            st.markdown("\n".join(tk_chunks), unsafe_allow_html=True)

    # ── 最新新聞（12h 快速篩選）──
    st.markdown('<div class="sec-hd">📋 最新新聞（12h）</div>', unsafe_allow_html=True)

    f1, f2, f3, f4 = st.columns([1, 1, 2, 1])
    with f1:
        sent_f = st.selectbox("情緒", ["全部", "利多", "利空", "中性"], key="d_sent")
    with f2:
        src_list = sorted(df_12h["source"].unique().tolist()) if not df_12h.empty else []
        src_f = st.selectbox("來源", ["全部"] + src_list, key="d_src")
    with f3:
        kw = st.text_input("🔍 搜尋標題", placeholder="關鍵字…", key="d_kw")
    with f4:
        sort_f = st.selectbox("排序", ["最新優先", "強度↓"], key="d_sort")

    hide_neu = st.checkbox("隱藏中性新聞", value=True, key="d_hide_neu")

    ddf = df_12h.copy() if not df_12h.empty else pd.DataFrame()
    if not ddf.empty:
        if hide_neu:
            ddf = ddf[ddf["sentiment"] != "neutral"]
        sm = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
        if sent_f != "全部":
            ddf = ddf[ddf["sentiment"] == sm[sent_f]]
        if src_f != "全部":
            ddf = ddf[ddf["source"] == src_f]
        if kw:
            ddf = ddf[ddf["title"].str.contains(kw, case=False, na=False)]
        if sort_f == "強度↓":
            ddf = ddf.reindex(ddf["sentiment_score"].abs().sort_values(ascending=False).index)

    st.caption(f"顯示 {len(ddf)} 則（12小時內）")
    render_news(ddf)


# ═══════════════════════════════════════════════
# TAB 2：深度篩選
# ═══════════════════════════════════════════════
with tab_deep:

    mode = st.radio(
        "分析模式",
        ["✦ AI 深度分析", "🔥 熱門股票", "⚑ 地緣政治", "🏭 類股排行", "🔍 個股聚焦"],
        horizontal=True, key="deep_mode",
    )

    # ── AI 深度分析 ──
    if mode == "✦ AI 深度分析":
        if not st.session_state["groq_ok"]:
            st.warning("請先設定 Groq API Key（Streamlit Cloud → App Settings → Secrets → GROQ_API_KEY）")
        else:
            @st.cache_data(ttl=60, show_spinner=False)
            def load_ai_all():
                db = SessionLocal()
                try:
                    return get_articles_df(db, ai_only=True, limit=300)
                finally:
                    db.close()

            ai_df = load_ai_all()
            if ai_df.empty:
                st.markdown('<div class="empty-box"><div class="empty-box-icon">🤖</div><div class="empty-box-txt">尚無 AI 分析結果</div></div>', unsafe_allow_html=True)
            else:
                ai_df["_agree"] = ai_df.apply(lambda r: r["ai_sentiment"] == r["sentiment"], axis=1)
                a1, a2, a3, a4 = st.columns(4)
                a1.metric("✦ AI 分析總數", len(ai_df))
                a2.metric("📈 AI 判多", len(ai_df[ai_df["ai_sentiment"] == "bullish"]))
                a3.metric("📉 AI 判空", len(ai_df[ai_df["ai_sentiment"] == "bearish"]))
                a4.metric("⚡ 與KW不一致", len(ai_df[~ai_df["_agree"]]), help="最有參考價值")

                st.divider()
                af1, af2, af3 = st.columns(3)
                with af1:
                    asf = st.selectbox("AI 情緒", ["全部", "利多", "利空", "中性"], key="asf")
                with af2:
                    acf = st.selectbox("信心程度", ["全部", "high（高）", "medium（中）", "low（低）"], key="acf")
                with af3:
                    asort = st.selectbox("排序", ["AI分數↓", "最新優先"], key="asort")

                diff_only = st.checkbox("只看 AI 與關鍵字不一致（最有參考價值）", key="adiff")

                fai = ai_df.copy()
                sm = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
                if asf != "全部":
                    fai = fai[fai["ai_sentiment"] == sm[asf]]
                if acf != "全部":
                    fai = fai[fai["ai_confidence"] == acf.split("（")[0]]
                if diff_only:
                    fai = fai[~fai["_agree"]]
                if asort == "AI分數↓":
                    fai = fai.reindex(fai["ai_score"].abs().sort_values(ascending=False).index)

                st.caption(f"顯示 {len(fai)} 則")
                render_news(fai)

    # ── 熱門股票 ──
    elif mode == "🔥 熱門股票":
        @st.cache_data(ttl=60, show_spinner=False)
        def load_hot():
            db = SessionLocal()
            try:
                return get_ticker_counts(db, limit=30)
            finally:
                db.close()

        hdf = load_hot()
        if hdf.empty:
            st.info("請先抓取新聞")
        else:
            top12 = hdf.head(12)
            cols = st.columns(4)
            for i, (_, row) in enumerate(top12.iterrows()):
                sc = row["平均情緒"]
                sc_h = (f'<span class="tk-bull">+{sc:.2f}</span>' if sc >= 0.15
                        else f'<span class="tk-bear">{sc:.2f}</span>' if sc <= -0.15
                        else f'<span class="tk-neu">{sc:.2f}</span>')
                mkt = row.get("市場", "TW")
                lk = f"https://tw.stock.yahoo.com/quote/{row['代碼']}" if mkt == "TW" else f"https://finance.yahoo.com/quote/{row['代碼']}"
                with cols[i % 4]:
                    st.markdown(f"""
<div class="tk-card">
  <a href="{lk}" target="_blank" style="text-decoration:none">
    <div class="tk-code">{row['代碼']}</div>
  </a>
  <div class="tk-name">{row['名稱']}</div>
  <div>{sc_h} <span class="tk-cnt">· {row['出現次數']} 則</span></div>
</div>""", unsafe_allow_html=True)

            st.divider()
            ch1, ch2 = st.columns(2)
            with ch1:
                fig_cnt = go.Figure(go.Bar(
                    x=hdf.head(15)["出現次數"], y=hdf.head(15)["代碼"], orientation="h",
                    marker=dict(color=hdf.head(15)["出現次數"],
                                colorscale=[[0, "#E2E8F0"], [1, "#1A1A2E"]], showscale=False),
                    text=hdf.head(15)["出現次數"], textposition="outside",
                    textfont=dict(size=10, color="#64748B"),
                ))
                fig_cnt.update_layout(
                    title=dict(text="出現次數", font=dict(color="#374151", size=12)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=10)),
                    xaxis=dict(showgrid=False, visible=False),
                    margin=dict(t=24, b=8, l=8, r=40), height=360,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_cnt, use_container_width=True)

            with ch2:
                cdf15 = hdf.head(15)
                colors15 = ["#DC2626" if s >= 0.15 else ("#16A34A" if s <= -0.15 else "#CBD5E1")
                            for s in cdf15["平均情緒"]]
                fig_sc = go.Figure(go.Bar(
                    x=cdf15["平均情緒"], y=cdf15["代碼"], orientation="h",
                    marker=dict(color=colors15),
                    text=cdf15["平均情緒"].round(2), textposition="outside",
                    textfont=dict(size=10, color="#64748B"),
                ))
                fig_sc.update_layout(
                    title=dict(text="平均情緒分數", font=dict(color="#374151", size=12)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=10)),
                    xaxis=dict(showgrid=False, visible=False),
                    margin=dict(t=24, b=8, l=8, r=40), height=360,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_sc, use_container_width=True)

    # ── 地緣政治 ──
    elif mode == "⚑ 地緣政治":
        @st.cache_data(ttl=60, show_spinner=False)
        def load_geo():
            db = SessionLocal()
            try:
                return get_articles_df(db, geo_only=True, limit=100)
            finally:
                db.close()

        gdf = load_geo()
        if gdf.empty:
            st.info("目前沒有地緣政治相關新聞")
        else:
            g1, g2, g3 = st.columns(3)
            g1.metric("⚑ 地緣政治新聞", len(gdf))
            g2.metric("📉 利空", len(gdf[gdf["sentiment"] == "bearish"]))
            g3.metric("📈 利多", len(gdf[gdf["sentiment"] == "bullish"]))
            st.divider()
            render_news(gdf)

    # ── 類股排行 ──
    elif mode == "🏭 類股排行":
        @st.cache_data(ttl=60, show_spinner=False)
        def load_sec():
            db = SessionLocal()
            try:
                df_ = get_articles_df(db, limit=500)
                sc_ = get_sector_counts(db)
            finally:
                db.close()
            return df_, sc_

        full_df2, secs2 = load_sec()
        if secs2.empty:
            st.info("請先抓取新聞")
        else:
            rows2 = []
            for _, r2 in secs2.iterrows():
                sec2  = r2["sector"]
                cnt2  = r2["count"]
                msk2  = full_df2["sectors"].str.contains(sec2, na=False)
                avg2  = float(full_df2[msk2]["sentiment_score"].mean()) if msk2.any() else 0.0
                bull2 = int(full_df2[msk2 & (full_df2["sentiment"] == "bullish")].shape[0])
                bear2 = int(full_df2[msk2 & (full_df2["sentiment"] == "bearish")].shape[0])
                rows2.append({"類股": sec2, "新聞數": cnt2, "平均情緒": round(avg2, 3), "利多": bull2, "利空": bear2})
            rank_df = pd.DataFrame(rows2)

            clrs_r = ["#DC2626" if s >= 0.05 else ("#16A34A" if s <= -0.05 else "#CBD5E1")
                      for s in rank_df.head(10)["平均情緒"]]
            fig_r = go.Figure(go.Bar(
                x=rank_df.head(10)["新聞數"], y=rank_df.head(10)["類股"], orientation="h",
                marker=dict(color=clrs_r),
                text=rank_df.head(10)["新聞數"], textposition="outside",
                textfont=dict(size=11, color="#64748B"),
            ))
            fig_r.update_layout(
                title=dict(text="類股新聞數（紅=偏多 綠=偏空）", font=dict(color="#374151", size=12)),
                yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=11)),
                xaxis=dict(showgrid=False, visible=False),
                margin=dict(t=24, b=8, l=8, r=40), height=340,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_r, use_container_width=True)
            st.divider()
            sel_sec = st.selectbox("點選類股查看相關新聞", rank_df["類股"].tolist(), key="sec_sel")
            if sel_sec:
                db2 = SessionLocal()
                try:
                    sec_news = get_articles_df(db2, sector=sel_sec, limit=50)
                finally:
                    db2.close()
                st.caption(f"{sel_sec}：共 {len(sec_news)} 則")
                render_news(sec_news)

    # ── 個股聚焦 ──
    elif mode == "🔍 個股聚焦":
        ticker_q = st.text_input(
            "輸入股票代碼或公司名稱",
            placeholder="台積電 / 2330 / 聯發科 / NVDA / 輝達…",
            key="ticker_q",
        )
        if not ticker_q:
            st.markdown("""
<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;
            padding:14px 18px;font-size:13px;color:#64748B;line-height:2.2">
  <strong style="color:#374151">支援格式</strong><br>
  台股代碼：<code style="background:#F1F5F9;padding:1px 6px;border-radius:4px;color:#1A1A2E">2330</code>
  <code style="background:#F1F5F9;padding:1px 6px;border-radius:4px;color:#1A1A2E">2454</code><br>
  台股名稱：<code style="background:#F1F5F9;padding:1px 6px;border-radius:4px;color:#1A1A2E">台積電</code>
  <code style="background:#F1F5F9;padding:1px 6px;border-radius:4px;color:#1A1A2E">廣達</code><br>
  美股代碼：<code style="background:#F1F5F9;padding:1px 6px;border-radius:4px;color:#1A1A2E">NVDA</code>
  <code style="background:#F1F5F9;padding:1px 6px;border-radius:4px;color:#1A1A2E">TSLA</code>
</div>""", unsafe_allow_html=True)
        else:
            q = ticker_q.strip()
            try:
                from analyzer import TW_COMPANY_TO_CODE, US_NAME_TO_CODE
                code_q = TW_COMPANY_TO_CODE.get(q) or US_NAME_TO_CODE.get(q) or q.upper()
            except ImportError:
                code_q = q.upper()
            db3 = SessionLocal()
            try:
                sdf = get_articles_df(db3, ticker=code_q, limit=150)
                if sdf.empty:
                    sdf = get_articles_df(db3, keyword=q, limit=150)
            finally:
                db3.close()

            if sdf.empty:
                st.warning(f"找不到 **{q}** 的相關新聞")
            else:
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("總計", len(sdf))
                s2.metric("📈 利多", len(sdf[sdf["sentiment"] == "bullish"]))
                s3.metric("📉 利空", len(sdf[sdf["sentiment"] == "bearish"]))
                s4.metric("✦ AI 分析", len(sdf[sdf.get("ai_summary", pd.Series(dtype=str)).ne("") if "ai_summary" in sdf.columns else pd.Series([False]*len(sdf))]))
                csv_s = sdf[["title","sentiment_label","sentiment_score","ai_sentiment","ai_score","ai_summary","tickers","sectors","source","published_at","url"]].to_csv(index=False, encoding="utf-8-sig")
                st.download_button("⬇ 匯出 CSV", csv_s, file_name=f"finnews_{code_q}.csv", mime="text/csv", key="stock_csv")
                st.divider()
                render_news(sdf)

    # ── 全部新聞篩選（深度篩選 tab 底部）──
    st.divider()
    st.markdown('<div class="sec-hd">📋 全部新聞篩選</div>', unsafe_allow_html=True)

    @st.cache_data(ttl=60, show_spinner=False)
    def load_all_news():
        db = SessionLocal()
        try:
            return get_articles_df(db, limit=500)
        finally:
            db.close()

    ndf = load_all_news()
    nf1, nf2, nf3, nf4, nf5 = st.columns([1, 1, 1, 2, 1])
    with nf1:
        nsent = st.selectbox("情緒", ["全部", "利多", "利空", "中性"], key="n_sent")
    with nf2:
        cats_l = sorted(ndf["category"].unique().tolist()) if not ndf.empty else []
        ncat = st.selectbox("分類", ["全部"] + cats_l, key="n_cat")
    with nf3:
        src_l = sorted(ndf["source"].unique().tolist()) if not ndf.empty else []
        nsrc = st.selectbox("來源", ["全部"] + src_l, key="n_src")
    with nf4:
        nkw = st.text_input("🔍 搜尋", placeholder="標題或摘要…", key="n_kw")
    with nf5:
        nsort = st.selectbox("排序", ["最新優先", "強度↓"], key="n_sort")

    hide_n = st.checkbox("隱藏中性新聞", value=True, key="n_hide")

    fdf = ndf.copy() if not ndf.empty else pd.DataFrame()
    if not fdf.empty:
        if hide_n:
            fdf = fdf[fdf["sentiment"] != "neutral"]
        sm2 = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
        if nsent != "全部":
            fdf = fdf[fdf["sentiment"] == sm2[nsent]]
        if ncat != "全部":
            fdf = fdf[fdf["category"] == ncat]
        if nsrc != "全部":
            fdf = fdf[fdf["source"] == nsrc]
        if nkw:
            fdf = fdf[
                fdf["title"].str.contains(nkw, case=False, na=False) |
                fdf["summary"].str.contains(nkw, case=False, na=False)
            ]
        if nsort == "強度↓":
            fdf = fdf.reindex(fdf["sentiment_score"].abs().sort_values(ascending=False).index)

    cc1, cc2 = st.columns([3, 1])
    with cc1:
        st.caption(f"顯示 {len(fdf)} / {len(ndf)} 則")
    with cc2:
        if not fdf.empty:
            csv_all = fdf[["title","sentiment_label","sentiment_score","ai_sentiment","ai_score","ai_summary","tickers","sectors","source","category","published_at","url"]].to_csv(index=False, encoding="utf-8-sig")
            st.download_button("⬇ 匯出 CSV", csv_all, file_name="finnews_export.csv", mime="text/csv", key="csv_all")
    render_news(fdf)


# ═══════════════════════════════════════════════
# TAB 3：設定
# ═══════════════════════════════════════════════
with tab_cfg:
    st.markdown("### ⚙️ 系統設定")
    st1, st2, st3 = st.tabs(["📡 來源 / 頻率", "📝 情緒詞典", "📜 執行日誌"])

    with st1:
        st.markdown("#### ⏱ 抓取頻率")
        new_iv = st.select_slider("每隔幾分鐘自動抓取", options=[15, 30, 60],
                                   value=st.session_state["interval"])
        if st.button("套用", key="iv_apply"):
            st.session_state["interval"] = new_iv
            update_interval(new_iv)
            st.success(f"已更新：每 {new_iv} 分鐘")

        st.divider()
        st.markdown("#### 📡 新聞來源開關")
        enabled_srcs = []
        for src in SOURCES:
            chk = st.checkbox(
                f"**{src['name']}**　`{src['category']}`",
                value=(src["name"] in st.session_state["enabled_srcs"]),
                key=f"src_{src['name']}",
            )
            if chk:
                enabled_srcs.append(src["name"])
        if st.button("💾 儲存來源設定", type="primary", key="save_srcs"):
            st.session_state["enabled_srcs"] = enabled_srcs
            st.success(f"已儲存，啟用 {len(enabled_srcs)} 個來源")

    with st2:
        st.markdown("#### 📝 自訂情緒詞彙")
        wc1, wc2, wc3 = st.columns([2, 1, 1])
        with wc1:
            nw_word = st.text_input("詞彙", placeholder="如：大客戶加單", key="nw")
        with wc2:
            nw_score = st.number_input("分數（正=利多 負=利空）", -1.0, 1.0, 0.7, 0.1, key="ns")
        with wc3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("新增", key="add_w"):
                if nw_word:
                    if nw_score > 0:
                        st.session_state["custom_bull"][nw_word] = nw_score
                    else:
                        st.session_state["custom_bear"][nw_word] = nw_score
                    st.success(f"已新增：{nw_word} ({nw_score:+.1f})")

        all_cw = {
            **{f"{w} (+{s:.1f})": "利多" for w, s in st.session_state["custom_bull"].items()},
            **{f"{w} ({s:.1f})": "利空" for w, s in st.session_state["custom_bear"].items()},
        }
        if all_cw:
            chips = " ".join(
                f'<span style="background:{"#FEF2F2" if lb=="利多" else "#F0FDF4"};'
                f'padding:3px 10px;border-radius:10px;font-size:11px;'
                f'border:1px solid {"#FECACA" if lb=="利多" else "#BBF7D0"};'
                f'color:{"#DC2626" if lb=="利多" else "#16A34A"}">'
                f'{wd} {lb}</span>'
                for wd, lb in all_cw.items()
            )
            st.markdown(chips, unsafe_allow_html=True)
        else:
            st.caption("尚無自訂詞彙")

    with st3:
        st.markdown("#### 📜 最近抓取日誌（台灣時間）")
        db_l = SessionLocal()
        log_df = get_crawl_logs(db_l)
        db_l.close()
        if log_df.empty:
            st.info("尚無日誌")
        else:
            log_rows_html = []
            for _, lr in log_df.iterrows():
                s = lr["狀態"]
                sbadge = (f'<span class="log-ok">{s}</span>' if s == "success"
                          else f'<span class="log-err">{s}</span>' if s == "error"
                          else f'<span class="log-warn">{s}</span>')
                log_rows_html.append(f"""
<tr>
  <td>{lr["來源"]}</td>
  <td>{sbadge}</td>
  <td style="color:#64748B">{lr["抓取"]}</td>
  <td style="color:#16A34A;font-weight:700">{lr["新增"]}</td>
  <td style="color:#94A3B8">{lr["跳過"]}</td>
  <td style="font-family:'JetBrains Mono',monospace;font-size:10px;color:#94A3B8">{lr["時間(台灣)"]}</td>
</tr>""")
            st.markdown(f"""
<div style="overflow-x:auto">
<table class="log-table">
  <thead>
    <tr>
      <th>來源</th><th>狀態</th><th>抓取</th>
      <th style="color:#16A34A">新增</th><th style="color:#94A3B8">跳過</th><th>時間(台灣)</th>
    </tr>
  </thead>
  <tbody>{"".join(log_rows_html)}</tbody>
</table>
</div>""", unsafe_allow_html=True)
