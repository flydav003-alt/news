"""
app.py — FinNews AI v3.0
重新設計版：深色主題 · 固定 sidebar · 緊湊高密度佈局
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


def relative_time(dt) -> str:
    if dt is None:
        return ""
    try:
        now = datetime.now(TZ_TW)
        if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
            diff = now - dt.astimezone(TZ_TW)
        else:
            diff = now - dt
        secs = diff.total_seconds()
        if secs < 0:
            secs = 0
        if secs < 60:
            return "剛剛"
        elif secs < 3600:
            return f"{int(secs // 60)}m"
        elif secs < 86400:
            return f"{int(secs // 3600)}h"
        elif secs < 86400 * 2:
            return "昨天"
        else:
            try:
                return dt.astimezone(TZ_TW).strftime("%m/%d")
            except Exception:
                return dt.strftime("%m/%d")
    except Exception:
        return ""


def filter_12h(df):
    if df is None or df.empty:
        return df
    cutoff = datetime.now(TZ_TW) - timedelta(hours=12)
    mask = df["published_at"].apply(
        lambda x: x is not None and (
            x.astimezone(TZ_TW) >= cutoff if getattr(x, "tzinfo", None) else True
        )
    )
    filtered = df[mask]
    return filtered if not filtered.empty else df


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
    initial_sidebar_state="expanded",
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+TC:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap');

/* ══ 全域重置 ══════════════════════════════════════════ */
html, body, [class*="css"], .stApp {
  font-family: 'IBM Plex Sans TC', sans-serif !important;
  background-color: #0d1117 !important;
  color: #e6edf3 !important;
}
header[data-testid="stHeader"] { display: none !important; }
html, body { margin: 0 !important; padding: 0 !important; }
[data-testid="stAppViewContainer"] { padding-top: 0 !important; }
[data-testid="stAppViewContainer"] > section.main { padding-top: 0 !important; }
[data-testid="stMain"] { padding-top: 0 !important; }
.main .block-container,
[data-testid="stMain"] .block-container {
  padding-top: 8px !important;
  padding-bottom: 24px !important;
  max-width: 100% !important;
}

/* ══ Sidebar ══════════════════════════════════════════ */
section[data-testid="stSidebar"] {
  background: #161b22 !important;
  border-right: 1px solid #21262d !important;
  min-width: 240px !important;
  max-width: 240px !important;
}
section[data-testid="stSidebar"] .block-container {
  padding: 14px 14px 20px !important;
}
.sb-logo {
  font-size: 15px; font-weight: 600; color: #e6edf3;
  display: flex; align-items: center; gap: 8px;
  padding-bottom: 14px; border-bottom: 1px solid #21262d;
  margin-bottom: 14px;
}
.sb-logo-dot { width: 8px; height: 8px; border-radius: 50%; background: #58a6ff; }
.sb-section { font-size: 10px; font-weight: 600; letter-spacing: 1.2px;
  text-transform: uppercase; color: #484f58; margin: 14px 0 6px; }
.sb-stat {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 8px; border-radius: 6px; margin-bottom: 3px;
  background: #0d1117; border: 1px solid #21262d;
}
.sb-stat-label { font-size: 12px; color: #8b949e; }
.sb-stat-val { font-size: 13px; font-weight: 600; font-family: 'IBM Plex Mono', monospace; }
.val-bull { color: #f85149; }
.val-bear { color: #3fb950; }
.val-neu  { color: #8b949e; }
.val-blue { color: #58a6ff; }

/* ── Mini donut in sidebar ── */
.sb-donut-wrap { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.sb-donut-legend { display: flex; flex-direction: column; gap: 3px; }
.sb-leg { font-size: 11px; color: #8b949e; display: flex; align-items: center; gap: 5px; }
.sb-leg-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }

/* ── Hot ticker rows ── */
.sb-ticker {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 8px; border-radius: 5px;
  margin-bottom: 3px; background: #0d1117; border: 1px solid #21262d;
  font-size: 12px;
}
.sb-ticker-sym { font-family: 'IBM Plex Mono', monospace; font-weight: 600;
  color: #e6edf3; min-width: 44px; }
.sb-ticker-bar-wrap { flex: 1; height: 3px; background: #21262d; border-radius: 2px; }
.sb-ticker-bar { height: 3px; border-radius: 2px; }
.sb-ticker-n { font-size: 10px; color: #484f58; font-family: 'IBM Plex Mono', monospace; }

/* ── Geo alert mini ── */
.sb-geo {
  padding: 6px 8px; border-radius: 5px; margin-bottom: 3px;
  background: #2d1a00; border: 1px solid #3d2400; border-left: 3px solid #d29922;
}
.sb-geo-title { font-size: 11px; color: #d29922; font-weight: 500; margin-bottom: 1px; }
.sb-geo-meta  { font-size: 10px; color: #7d6133; }

/* ── Sidebar status ── */
.sb-status {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 8px; border-radius: 5px; margin-bottom: 4px;
  font-size: 11px;
}
.sb-status-ok  { background: #0f2a1a; border: 1px solid #1a4a2a; color: #3fb950; }
.sb-status-warn{ background: #2a1f0a; border: 1px solid #4a3510; color: #d29922; }
.sb-status-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }

/* ══ Topbar ══════════════════════════════════════════ */
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  background: #161b22; border: 1px solid #21262d; border-radius: 8px;
  padding: 7px 14px; margin-bottom: 10px;
}
.tb-left { display: flex; align-items: center; gap: 10px; }
.tb-logo { font-size: 14px; font-weight: 600; color: #e6edf3; }
.tb-badge-ok   { font-size: 10px; font-weight: 600; color: #3fb950;
  background: #0f2a1a; border: 1px solid #1a4a2a;
  border-radius: 20px; padding: 2px 9px; }
.tb-badge-warn { font-size: 10px; font-weight: 600; color: #d29922;
  background: #2a1f0a; border: 1px solid #4a3510;
  border-radius: 20px; padding: 2px 9px; }
.tb-time { font-size: 10px; color: #484f58; font-family: 'IBM Plex Mono', monospace; }

/* ══ Tabs ══════════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {
  background: #161b22; border-radius: 7px; padding: 3px; gap: 2px;
  border: 1px solid #21262d; margin-bottom: 10px !important;
}
.stTabs [data-baseweb="tab"] {
  font-size: 12px; font-weight: 500; padding: 5px 16px; border-radius: 5px;
  color: #8b949e; font-family: 'IBM Plex Sans TC', sans-serif !important;
}
.stTabs [aria-selected="true"] {
  background: #21262d !important; color: #e6edf3 !important;
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 4px !important; }

/* ══ Metrics ══════════════════════════════════════════ */
[data-testid="metric-container"] {
  background: #161b22; border: 1px solid #21262d;
  border-radius: 8px; padding: 10px 14px;
}
[data-testid="stMetricLabel"] { color: #8b949e !important; font-size: 11px !important; font-weight: 500 !important; }
[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 20px !important; font-weight: 600 !important; }
[data-testid="stMetricDelta"] { font-size: 11px !important; }

/* ══ Buttons ══════════════════════════════════════════ */
.stButton > button {
  border-radius: 6px; font-weight: 500; font-size: 12px;
  border: 1px solid #30363d; background: #21262d; color: #c9d1d9;
  transition: all 0.15s; height: 34px !important; padding: 0 20px !important;
  font-family: 'IBM Plex Sans TC', sans-serif !important;
}
.stButton > button:hover { background: #30363d; border-color: #484f58; }
.stButton > button[kind="primary"] {
  background: #1f6feb; border-color: #1f6feb; color: #ffffff;
}
.stButton > button[kind="primary"]:hover { background: #388bfd; }

/* ══ Inputs / Selects ══════════════════════════════════ */
.stSelectbox > div > div,
.stTextInput > div > div > input {
  background: #0d1117 !important; border: 1px solid #30363d !important;
  border-radius: 6px !important; color: #e6edf3 !important; font-size: 12px !important;
}
.stSelectbox label, .stTextInput label { color: #8b949e !important; font-size: 11px !important; font-weight: 500 !important; }
.stCheckbox label { color: #c9d1d9 !important; font-size: 12px !important; }
.stCheckbox { margin-bottom: 0 !important; }
.stRadio label { color: #c9d1d9 !important; font-size: 12px !important; }
.stRadio > div { gap: 6px !important; }
hr { border-color: #21262d !important; margin: 10px 0 !important; }
.stCaption { color: #484f58 !important; font-size: 11px !important; }
div[data-testid="column"] { padding-left: 4px !important; padding-right: 4px !important; }

/* ══ Section Header ══════════════════════════════════════ */
.sec-hd {
  font-size: 10px; font-weight: 600; color: #484f58;
  letter-spacing: 1.2px; text-transform: uppercase;
  margin: 12px 0 7px; display: flex; align-items: center; gap: 7px;
}
.sec-hd::after { content: ''; flex: 1; height: 1px; background: #21262d; }

/* ══ AI 總結卡片 ══════════════════════════════════════════ */
.ai-card {
  background: #161b22; border: 1px solid #21262d;
  border-radius: 10px; padding: 14px 16px; margin-bottom: 8px;
  border-top: 2px solid #58a6ff;
}
.ai-badge {
  font-size: 9px; font-weight: 600; letter-spacing: 1px;
  color: #d29922; background: #2a1f0a; border: 1px solid #4a3510;
  border-radius: 4px; padding: 2px 7px; text-transform: uppercase;
  display: inline-block; margin-bottom: 8px;
}
.ai-dir-bull { font-size: 16px; font-weight: 600; color: #f85149; }
.ai-dir-bear { font-size: 16px; font-weight: 600; color: #3fb950; }
.ai-dir-neu  { font-size: 16px; font-weight: 600; color: #8b949e; }
.ai-dir-reason { font-size: 12px; color: #8b949e; margin: 2px 0 10px; }
.ai-themes { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 10px; }
.ai-tag-bull {
  background: #3d0f0f; border: 1px solid #6e1a1a; border-radius: 20px;
  padding: 2px 10px; font-size: 11px; color: #f85149; font-weight: 500;
}
.ai-tag-bear {
  background: #0f2a1a; border: 1px solid #1a4a2a; border-radius: 20px;
  padding: 2px 10px; font-size: 11px; color: #3fb950; font-weight: 500;
}
.ai-tickers { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 10px; }
.ai-tick-chip {
  background: #0d2b4a; border: 1px solid #1a4a7a; border-radius: 4px;
  padding: 2px 8px; font-size: 11px; color: #58a6ff;
  font-family: 'IBM Plex Mono', monospace; font-weight: 600;
}
.ai-body {
  font-size: 12px; line-height: 1.8; color: #8b949e;
  border-top: 1px solid #21262d; padding-top: 10px;
}
.ai-footer { font-size: 10px; color: #30363d; margin-top: 8px; }

/* ══ 地緣警示 ══════════════════════════════════════════ */
.geo-card {
  background: #2d1a00; border: 1px solid #3d2400; border-left: 3px solid #d29922;
  border-radius: 7px; padding: 9px 13px; margin-bottom: 5px;
  display: flex; gap: 10px; align-items: flex-start;
}
.geo-icon { font-size: 13px; flex-shrink: 0; margin-top: 1px; }
.geo-title { font-size: 12px; font-weight: 500; color: #e6edf3; margin-bottom: 2px; }
.geo-title a { color: #e6edf3; text-decoration: none; }
.geo-title a:hover { color: #d29922; }
.geo-meta { font-size: 11px; color: #7d6133; font-weight: 500; }
.geo-body { font-size: 11px; color: #8b949e; line-height: 1.5; margin-top: 3px; }

/* ══ 置頂高分卡片 ══════════════════════════════════════ */
.nw-pinned-bull {
  background: #1a0e0e; border: 1px solid #3d1a1a; border-left: 4px solid #f85149;
  border-radius: 8px; padding: 11px 14px; margin-bottom: 6px;
}
.nw-pinned-bear {
  background: #0b1f12; border: 1px solid #1a3d2a; border-left: 4px solid #3fb950;
  border-radius: 8px; padding: 11px 14px; margin-bottom: 6px;
}
.nw-pinned-label {
  font-size: 9px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  margin-bottom: 5px; display: inline-block; border-radius: 3px; padding: 2px 7px;
}
.nw-pinned-bull .nw-pinned-label { color: #f85149; background: #3d0f0f; }
.nw-pinned-bear .nw-pinned-label { color: #3fb950; background: #0f2a1a; }
.nw-pinned-title { font-size: 14px; font-weight: 500; line-height: 1.5; margin-bottom: 5px; }
.nw-pinned-bull .nw-pinned-title a { color: #ffa198; text-decoration: none; }
.nw-pinned-bear .nw-pinned-title a { color: #56d364; text-decoration: none; }
.nw-pinned-bull .nw-pinned-title a:hover { color: #f85149; }
.nw-pinned-bear .nw-pinned-title a:hover { color: #3fb950; }
.nw-pinned-score-bull { font-size: 13px; font-weight: 700; color: #f85149; font-family: 'IBM Plex Mono', monospace; }
.nw-pinned-score-bear { font-size: 13px; font-weight: 700; color: #3fb950; font-family: 'IBM Plex Mono', monospace; }

/* ══ 新聞卡片 ══════════════════════════════════════════ */
.nw {
  background: #161b22; border: 1px solid #21262d;
  border-radius: 7px; padding: 9px 12px; margin-bottom: 4px;
  border-left: 3px solid #21262d;
  transition: border-left-color 0.1s, background 0.1s;
}
.nw:hover { background: #1c2128; }
.nw.bull  { border-left-color: #f85149; }
.nw.bear  { border-left-color: #3fb950; }
.nw.geo   { border-left-color: #d29922; }
.nw-top   { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.nw-title { font-size: 13px; font-weight: 500; color: #e6edf3; line-height: 1.5; flex: 1; }
.nw-title a { color: #e6edf3; text-decoration: none; }
.nw-title a:hover { color: #58a6ff; }
.nw-score { font-size: 11px; font-family: 'IBM Plex Mono', monospace; font-weight: 600;
  white-space: nowrap; padding: 2px 7px; border-radius: 4px; flex-shrink: 0; }
.nw-score.s-bull { background: #3d0f0f; color: #f85149; }
.nw-score.s-bear { background: #0f2a1a; color: #3fb950; }
.nw-score.s-neu  { background: #21262d; color: #484f58; }
.nw-meta  { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: 5px; }
.nw-src   { font-size: 10px; color: #484f58; }
.nw-time  { font-size: 10px; color: #30363d; font-family: 'IBM Plex Mono', monospace; }
.nw-badge-ai  { font-size: 9px; font-weight: 600; color: #d29922; background: #2a1f0a;
  border: 1px solid #4a3510; border-radius: 3px; padding: 1px 5px; }
.nw-badge-geo { font-size: 9px; font-weight: 600; color: #d29922; background: #2a1f0a;
  border: 1px solid #4a3510; border-radius: 3px; padding: 1px 5px; }
.nw-tick  { font-size: 10px; font-weight: 600; color: #58a6ff; background: #0d2b4a;
  border-radius: 3px; padding: 1px 5px; font-family: 'IBM Plex Mono', monospace; }
.nw-ai-box {
  margin-top: 7px; padding: 8px 10px; background: #0d1117;
  border-radius: 5px; border-left: 2px solid #d29922;
  font-size: 11px; color: #8b949e; line-height: 1.7; display: none;
}
.nw-ai-box.open { display: block; }
.nw-ai-toggle {
  font-size: 10px; color: #d29922; cursor: pointer;
  display: inline-flex; align-items: center; gap: 3px;
  padding: 1px 6px; border: 1px solid #4a3510;
  background: #2a1f0a; border-radius: 3px;
  font-weight: 600; user-select: none; vertical-align: middle; margin-left: 4px;
}
.nw-ai-toggle:hover { background: #3d2d10; }
.nw-ai-reason { margin-top: 4px; font-size: 10px; color: #484f58; }

/* ══ 已讀灰化 ══════════════════════════════════════════ */
.nw-title a.nw-read { color: #484f58 !important; text-decoration: line-through; }
.nw.nw-read-card { opacity: 0.45; }

/* ══ Chip 篩選 ══════════════════════════════════════════ */
.chip-bar { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 9px; align-items: center; }
.chip {
  font-size: 11px; font-weight: 500; padding: 3px 12px; border-radius: 20px;
  border: 1px solid #30363d; background: #161b22; color: #8b949e;
  cursor: pointer; transition: all 0.12s; user-select: none; white-space: nowrap;
}
.chip:hover { background: #21262d; border-color: #484f58; }
.chip.chip-all.active  { background: #21262d; border-color: #58a6ff; color: #58a6ff; }
.chip.chip-bull.active { background: #3d0f0f; border-color: #f85149; color: #f85149; }
.chip.chip-bear.active { background: #0f2a1a; border-color: #3fb950; color: #3fb950; }
.chip.chip-ai.active   { background: #2a1f0a; border-color: #d29922; color: #d29922; }
.chip.chip-geo.active  { background: #2a1f0a; border-color: #d29922; color: #d29922; }

/* ══ 熱門股票卡片 ══════════════════════════════════════ */
.tk-card {
  background: #161b22; border: 1px solid #21262d; border-radius: 7px;
  padding: 10px; margin-bottom: 5px; text-align: center;
}
.tk-card:hover { border-color: #30363d; }
.tk-code { font-size: 14px; font-weight: 600; color: #e6edf3; font-family: 'IBM Plex Mono', monospace; }
.tk-name { font-size: 10px; color: #484f58; margin: 2px 0 5px; }
.tk-bull { color: #f85149; font-size: 11px; font-weight: 600; }
.tk-bear { color: #3fb950; font-size: 11px; font-weight: 600; }
.tk-neu  { color: #484f58; font-size: 11px; }
.tk-cnt  { font-size: 10px; color: #30363d; }

/* ══ 空狀態 ══════════════════════════════════════════ */
.empty-box { text-align: center; padding: 36px 24px; color: #30363d; }
.empty-box-icon { font-size: 28px; margin-bottom: 8px; }
.empty-box-txt { font-size: 13px; }

/* ══ 日誌表格 ══════════════════════════════════════════ */
.log-table {
  width: 100%; border-collapse: collapse; font-size: 12px;
  background: #161b22; border: 1px solid #21262d; border-radius: 7px; overflow: hidden;
}
.log-table th {
  padding: 8px 12px; text-align: left; font-size: 10px; font-weight: 600;
  color: #484f58; letter-spacing: 0.8px; background: #0d1117;
  border-bottom: 1px solid #21262d; text-transform: uppercase;
}
.log-table td { padding: 6px 12px; color: #8b949e; border-bottom: 1px solid #0d1117; }
.log-ok   { background: #0f2a1a; color: #3fb950; font-size: 10px; font-weight: 600; padding: 1px 7px; border-radius: 3px; }
.log-err  { background: #3d0f0f; color: #f85149; font-size: 10px; font-weight: 600; padding: 1px 7px; border-radius: 3px; }
.log-warn { background: #2a1f0a; color: #d29922; font-size: 10px; font-weight: 600; padding: 1px 7px; border-radius: 3px; }
</style>

<script>
(function(){
  function applyRead(){
    try {
      var read = JSON.parse(localStorage.getItem('fn_read') || '{}');
      document.querySelectorAll('.nw-title a').forEach(function(a){
        var key = a.href.split('?')[0];
        if(read[key]){
          a.classList.add('nw-read');
          var card = a.closest('.nw');
          if(card) card.classList.add('nw-read-card');
        }
      });
    } catch(e){}
  }
  applyRead();
  var obs = new MutationObserver(applyRead);
  obs.observe(document.body, {childList:true, subtree:true});
})();

document.addEventListener('click', function(e){
  var a = e.target.closest('.nw-title a');
  if(!a) return;
  try {
    var read = JSON.parse(localStorage.getItem('fn_read') || '{}');
    var key = a.href.split('?')[0];
    read[key] = Date.now();
    var keys = Object.keys(read);
    if(keys.length > 500){
      keys.sort(function(x,y){ return read[x]-read[y]; });
      keys.slice(0, keys.length-500).forEach(function(k){ delete read[k]; });
    }
    localStorage.setItem('fn_read', JSON.stringify(read));
    a.classList.add('nw-read');
    var card = a.closest('.nw');
    if(card) card.classList.add('nw-read-card');
  } catch(e){}
});

document.addEventListener('click', function(e){
  var btn = e.target.closest('.nw-ai-toggle');
  if(!btn) return;
  var box = btn.parentElement.querySelector('.nw-ai-box');
  if(!box) return;
  var open = box.classList.toggle('open');
  btn.innerHTML = open ? '&#9652; 收合' : '&#10022; AI 摘要';
});

function chipFilter(el, filter){
  document.querySelectorAll('.chip').forEach(function(c){ c.classList.remove('active'); });
  el.classList.add('active');
  var cards = document.querySelectorAll('.nw, .nw-pinned-bull, .nw-pinned-bear');
  cards.forEach(function(card){
    if(filter === 'all'){ card.style.display=''; return; }
    if(filter === 'bull'){
      card.style.display = card.classList.contains('bull') ? '' : 'none';
    } else if(filter === 'bear'){
      card.style.display = card.classList.contains('bear') ? '' : 'none';
    } else if(filter === 'ai'){
      card.style.display = card.querySelector('.nw-badge-ai') ? '' : 'none';
    } else if(filter === 'geo'){
      card.style.display = card.classList.contains('geo') ? '' : 'none';
    }
  });
}
</script>
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

    pinned_chunks = []
    if "ai_score" in df.columns:
        pinned_df = df[df["ai_score"].abs() >= 7].head(3)
        for _, row in pinned_df.iterrows():
            sc    = float(row.get("ai_score", 0) or 0)
            title = str(row.get("title", ""))
            url   = str(row.get("url", "") or "")
            ai_sum = str(row.get("ai_summary", "") or "")
            t_html = f'<a href="{url}" target="_blank">{title}</a>' if url else title
            cls    = "nw-pinned-bull" if sc > 0 else "nw-pinned-bear"
            lbl    = "🔥 強烈利多訊號" if sc > 0 else "⚠️ 強烈利空訊號"
            sc_cls = "nw-pinned-score-bull" if sc > 0 else "nw-pinned-score-bear"
            src    = str(row.get("source", "") or "")
            rtime  = relative_time(row.get("published_at"))
            ai_blk = f'<div style="font-size:11px;color:#8b949e;margin-top:6px;line-height:1.7">{ai_sum}</div>' if ai_sum else ""
            pinned_chunks.append(f"""
<div class="{cls}">
  <span class="nw-pinned-label">{lbl}</span>
  <div class="nw-pinned-title">{t_html}</div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <span class="{sc_cls}">{sc:+.1f}</span>
    <span style="font-size:10px;color:#484f58">{src}</span>
    <span style="font-size:10px;color:#30363d;font-family:'IBM Plex Mono',monospace">{rtime}</span>
  </div>
  {ai_blk}
</div>""")
    if pinned_chunks:
        st.markdown("\n".join(pinned_chunks), unsafe_allow_html=True)

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
        rtime    = relative_time(row.get("published_at")) if row.get("published_at") is not None else ""

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
            score_h = f'<span class="nw-score s-bull">+{sv:.1f}</span>'
        elif sv < 0:
            score_h = f'<span class="nw-score s-bear">{sv:.1f}</span>'
        else:
            score_h = '<span class="nw-score s-neu">—</span>'

        t_html = f'<a href="{url}" target="_blank">{title}</a>' if url else title

        badges = []
        if ai_sum:
            badges.append('<span class="nw-badge-ai">✦ AI</span>')
        if is_geo:
            badges.append('<span class="nw-badge-geo">⚑ 地緣</span>')
        for t in tickers[:4]:
            badges.append(f'<span class="nw-tick">{t}</span>')
        bdg = " ".join(badges)

        ai_block = ""
        if ai_sum:
            rsn_part = f'<div class="nw-ai-reason">📌 {ai_rsn}</div>' if ai_rsn else ""
            ai_toggle = '<span class="nw-ai-toggle">✦ AI 摘要</span>'
            ai_block = f'<div style="margin-top:5px">{ai_toggle}<div class="nw-ai-box">{ai_sum}{rsn_part}</div></div>'

        chunks.append(f"""
<div class="{cls}">
  <div class="nw-top">
    <div class="nw-title">{t_html}</div>
    {score_h}
  </div>
  <div class="nw-meta">
    {bdg}
    <span class="nw-src">{source}</span>
    <span class="nw-time">{rtime}</span>
  </div>
  {ai_block}
</div>""")

    st.markdown("\n".join(chunks), unsafe_allow_html=True)
    if len(df) > max_items:
        st.caption(f"顯示前 {max_items} 則，共 {len(df)} 則")


# ─────────────────────────────────────────────
# SIDEBAR：統計資訊面板
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
<div class="sb-logo">
  <div class="sb-logo-dot"></div>
  FinNews AI
</div>""", unsafe_allow_html=True)

    # AI / 模式狀態
    _groq_ok = st.session_state["groq_ok"]
    if _groq_ok:
        st.markdown('<div class="sb-status sb-status-ok"><span class="sb-status-dot"></span>Groq AI 已連線</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="sb-status sb-status-warn"><span class="sb-status-dot"></span>關鍵字模式</div>', unsafe_allow_html=True)

    _next_run = next_run_time()
    _last_upd = st.session_state["last_update"]
    st.markdown(f'<div style="font-size:10px;color:#484f58;font-family:\'IBM Plex Mono\',monospace;margin-bottom:10px">下次 {_next_run} · 最後 {_last_upd}</div>', unsafe_allow_html=True)

    # AI checkbox
    st.session_state["use_ai"] = st.checkbox(
        "啟用 AI 深度分析",
        value=st.session_state["use_ai"],
        disabled=not _groq_ok,
        key="use_ai_cb",
    )

    # 立即抓取按鈕
    if st.button("🔄 立即抓取", type="primary", use_container_width=True):
        with st.spinner("抓取＋分析中…"):
            result = crawl_and_save(
                enabled_names=st.session_state["enabled_srcs"],
                custom_bull=st.session_state["custom_bull"],
                custom_bear=st.session_state["custom_bear"],
                use_ai=st.session_state["use_ai"],
            )
            st.session_state["last_update"] = now_tw_str()
            st.cache_data.clear()
        ai_info = f"｜AI {result.get('ai_count', 0)} 則" if st.session_state["use_ai"] else ""
        st.success(f"新增 {result['saved']} 則{ai_info}")
        st.rerun()

    # ── 統計數字（從 DB 拉）──
    @st.cache_data(ttl=60, show_spinner=False)
    def load_sidebar_stats():
        db = SessionLocal()
        try:
            counts  = get_sentiment_counts(db)
            tickers = get_ticker_counts(db, limit=8)
            geo_df  = get_articles_df(db, geo_only=True, limit=5)
            ai_df   = get_articles_df(db, ai_only=True, limit=1)
        finally:
            db.close()
        return counts, tickers, geo_df, len(ai_df) if not ai_df.empty else 0

    sb_counts, sb_tickers, sb_geo, sb_ai_cnt = load_sidebar_stats()
    total_n  = sum(sb_counts.values())
    bull_n   = sb_counts.get("bullish", 0)
    bear_n   = sb_counts.get("bearish", 0)
    mid_n    = sb_counts.get("neutral", 0)

    st.markdown('<div class="sb-section">情緒統計</div>', unsafe_allow_html=True)

    # Mini donut
    bull_pct = bull_n / total_n * 100 if total_n else 0
    bear_pct = bear_n / total_n * 100 if total_n else 0
    circ = 2 * 3.14159 * 20
    bull_arc = circ * bull_pct / 100
    bear_arc = circ * bear_pct / 100
    gap = circ - bull_arc - bear_arc
    st.markdown(f"""
<div class="sb-donut-wrap">
  <svg width="54" height="54" viewBox="0 0 54 54">
    <circle cx="27" cy="27" r="20" fill="none" stroke="#21262d" stroke-width="7"/>
    <circle cx="27" cy="27" r="20" fill="none" stroke="#f85149" stroke-width="7"
      stroke-dasharray="{bull_arc:.1f} {circ-bull_arc:.1f}" stroke-dashoffset="{circ/4:.1f}"
      transform="rotate(0 27 27)"/>
    <circle cx="27" cy="27" r="20" fill="none" stroke="#3fb950" stroke-width="7"
      stroke-dasharray="{bear_arc:.1f} {circ-bear_arc:.1f}" stroke-dashoffset="{circ/4 - bull_arc:.1f}"
      transform="rotate(0 27 27)"/>
    <text x="27" y="31" text-anchor="middle" font-size="11" font-weight="600" fill="#e6edf3" font-family="IBM Plex Mono">{total_n}</text>
  </svg>
  <div class="sb-donut-legend">
    <div class="sb-leg"><span class="sb-leg-dot" style="background:#f85149"></span>利多 {bull_pct:.0f}%</div>
    <div class="sb-leg"><span class="sb-leg-dot" style="background:#3fb950"></span>利空 {bear_pct:.0f}%</div>
    <div class="sb-leg"><span class="sb-leg-dot" style="background:#21262d"></span>中性 {(100-bull_pct-bear_pct):.0f}%</div>
  </div>
</div>""", unsafe_allow_html=True)

    st.markdown(f"""
<div class="sb-stat"><span class="sb-stat-label">✦ AI 分析</span><span class="sb-stat-val val-blue">{sb_ai_cnt}</span></div>
<div class="sb-stat"><span class="sb-stat-label">⚑ 地緣警示</span><span class="sb-stat-val val-neu">{len(sb_geo)}</span></div>
""", unsafe_allow_html=True)

    # Hot tickers
    if not sb_tickers.empty:
        st.markdown('<div class="sb-section">熱門個股</div>', unsafe_allow_html=True)
        max_cnt = sb_tickers["出現次數"].max() if len(sb_tickers) else 1
        tk_html = []
        for _, row in sb_tickers.head(7).iterrows():
            sc  = row["平均情緒"]
            pct = int(row["出現次數"] / max_cnt * 100)
            bar_color = "#f85149" if sc >= 0.15 else ("#3fb950" if sc <= -0.15 else "#484f58")
            tk_html.append(f"""
<div class="sb-ticker">
  <span class="sb-ticker-sym">{row['代碼']}</span>
  <div class="sb-ticker-bar-wrap"><div class="sb-ticker-bar" style="width:{pct}%;background:{bar_color}"></div></div>
  <span class="sb-ticker-n">{row['出現次數']}</span>
</div>""")
        st.markdown("\n".join(tk_html), unsafe_allow_html=True)

    # Geo alerts
    if not sb_geo.empty:
        geo_12h = filter_12h(sb_geo)
        if not geo_12h.empty:
            st.markdown('<div class="sb-section">地緣警示</div>', unsafe_allow_html=True)
            g_html = []
            for _, row in geo_12h.head(3).iterrows():
                eff    = row.get("ai_sentiment", "") or row.get("sentiment", "neutral")
                impact = "利空" if eff == "bearish" else ("利多" if eff == "bullish" else "中性")
                t      = str(row.get("title", ""))[:28] + ("…" if len(str(row.get("title", ""))) > 28 else "")
                rtime  = relative_time(row.get("published_at"))
                g_html.append(f"""
<div class="sb-geo">
  <div class="sb-geo-title">{t}</div>
  <div class="sb-geo-meta">{impact} · {rtime}</div>
</div>""")
            st.markdown("\n".join(g_html), unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 主 Topbar（極簡，sidebar 已有控制）
# ─────────────────────────────────────────────
st.markdown(f"""
<div class="topbar">
  <div class="tb-left">
    <span class="tb-logo">📈 FinNews AI</span>
    {'<span class="tb-badge-ok">● Groq AI</span>' if _groq_ok else '<span class="tb-badge-warn">⚠ 關鍵字模式</span>'}
    <span class="tb-time">下次 {_next_run} · 最後 {_last_upd}</span>
  </div>
</div>
""", unsafe_allow_html=True)


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
    df_12h = filter_12h(df)

    total  = sum(counts.values())
    bull_n = counts.get("bullish", 0)
    bear_n = counts.get("bearish", 0)
    mid_n  = counts.get("neutral", 0)

    # ── 5 個指標 ──
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📰 新聞總數", total)
    if total:
        c2.metric("利多", bull_n, delta=f"{bull_n/total*100:.1f}%")
        c3.metric("利空", bear_n, delta=f"-{bear_n/total*100:.1f}%", delta_color="inverse")
    else:
        c2.metric("利多", 0)
        c3.metric("利空", 0)
    c4.metric("✦ AI 分析", len(ai_12h))
    c5.metric("⚑ 地緣政治", len(geo_df))

    # ── AI 市場總結 ──
    st.markdown('<div class="sec-hd">✦ AI 市場總結</div>', unsafe_allow_html=True)

    if not st.session_state["groq_ok"]:
        st.info("需要設定 Groq API Key 才能顯示 AI 市場總結")
    elif ai_12h.empty:
        st.info("尚無 AI 分析資料，請先抓取新聞並啟用 AI 深度分析")
    else:
        ts_key    = str(ai_12h.iloc[0].get("published_at", ""))
        cache_key = f"ds_v30_{ts_key}"

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
            dir_icon = "↗" if direction == "偏多" else ("↘" if direction == "偏空" else "→")

            bull_tags = "".join(f'<span class="ai-tag-bull">▲ {t}</span>' for t in bulls)
            bear_tags = "".join(f'<span class="ai-tag-bear">▼ {t}</span>' for t in bears)
            tick_tags = "".join(f'<span class="ai-tick-chip">{t}</span>' for t in keys)
            tick_html = f'<div style="font-size:10px;color:#484f58;margin-bottom:3px">關注個股</div><div class="ai-tickers">{tick_tags}</div>' if tick_tags else ""

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

    # ── 地緣政治警示 ──
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
  <div class="geo-icon">⚑</div>
  <div>
    <div class="geo-title">{link_h}</div>
    <div class="geo-meta">{impact}</div>
    {body_h}
  </div>
</div>""")
            st.markdown("\n".join(geo_chunks), unsafe_allow_html=True)

    # ── 今日重點新聞 ──
    st.markdown('<div class="sec-hd">🔑 今日重點新聞（12h）</div>', unsafe_allow_html=True)

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

    # ── 最新新聞（12h 快速篩選）──
    st.markdown('<div class="sec-hd">📋 最新新聞（12h）</div>', unsafe_allow_html=True)

    st.markdown("""
<div class="chip-bar">
  <span class="chip chip-all active" onclick="chipFilter(this,'all')">全部</span>
  <span class="chip chip-bull" onclick="chipFilter(this,'bull')">利多</span>
  <span class="chip chip-bear" onclick="chipFilter(this,'bear')">利空</span>
  <span class="chip chip-ai"  onclick="chipFilter(this,'ai')">✦ AI高分</span>
  <span class="chip chip-geo" onclick="chipFilter(this,'geo')">⚑ 地緣</span>
</div>""", unsafe_allow_html=True)

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
            st.warning("請先設定 Groq API Key")
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
                a2.metric("利多", len(ai_df[ai_df["ai_sentiment"] == "bullish"]))
                a3.metric("利空", len(ai_df[ai_df["ai_sentiment"] == "bearish"]))
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
                sm2 = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
                if asf != "全部":
                    fai = fai[fai["ai_sentiment"] == sm2[asf]]
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
                    marker=dict(color="#1f6feb"),
                    text=hdf.head(15)["出現次數"], textposition="outside",
                    textfont=dict(size=10, color="#8b949e"),
                ))
                fig_cnt.update_layout(
                    title=dict(text="出現次數", font=dict(color="#8b949e", size=12)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#8b949e", size=10)),
                    xaxis=dict(showgrid=False, visible=False),
                    margin=dict(t=24, b=8, l=8, r=40), height=360,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_cnt, use_container_width=True)

            with ch2:
                cdf15 = hdf.head(15)
                colors15 = ["#f85149" if s >= 0.15 else ("#3fb950" if s <= -0.15 else "#484f58")
                            for s in cdf15["平均情緒"]]
                fig_sc = go.Figure(go.Bar(
                    x=cdf15["平均情緒"], y=cdf15["代碼"], orientation="h",
                    marker=dict(color=colors15),
                    text=cdf15["平均情緒"].round(2), textposition="outside",
                    textfont=dict(size=10, color="#8b949e"),
                ))
                fig_sc.update_layout(
                    title=dict(text="平均情緒分數", font=dict(color="#8b949e", size=12)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#8b949e", size=10)),
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
            g2.metric("利空", len(gdf[gdf["sentiment"] == "bearish"]))
            g3.metric("利多", len(gdf[gdf["sentiment"] == "bullish"]))
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

            clrs_r = ["#f85149" if s >= 0.05 else ("#3fb950" if s <= -0.05 else "#484f58")
                      for s in rank_df.head(10)["平均情緒"]]
            fig_r = go.Figure(go.Bar(
                x=rank_df.head(10)["新聞數"], y=rank_df.head(10)["類股"], orientation="h",
                marker=dict(color=clrs_r),
                text=rank_df.head(10)["新聞數"], textposition="outside",
                textfont=dict(size=11, color="#8b949e"),
            ))
            fig_r.update_layout(
                title=dict(text="類股新聞數（紅=偏多 綠=偏空）", font=dict(color="#8b949e", size=12)),
                yaxis=dict(autorange="reversed", tickfont=dict(color="#8b949e", size=11)),
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
            placeholder="台積電 / 2330 / 聯發科 / NVDA…",
            key="ticker_q",
        )
        if not ticker_q:
            st.markdown("""
<div style="background:#161b22;border:1px solid #21262d;border-radius:8px;
            padding:14px 18px;font-size:12px;color:#8b949e;line-height:2.2">
  <strong style="color:#e6edf3">支援格式</strong><br>
  台股代碼：<code style="background:#0d1117;padding:1px 6px;border-radius:4px;color:#58a6ff">2330</code>
  <code style="background:#0d1117;padding:1px 6px;border-radius:4px;color:#58a6ff">2454</code><br>
  台股名稱：<code style="background:#0d1117;padding:1px 6px;border-radius:4px;color:#58a6ff">台積電</code>
  <code style="background:#0d1117;padding:1px 6px;border-radius:4px;color:#58a6ff">廣達</code><br>
  美股代碼：<code style="background:#0d1117;padding:1px 6px;border-radius:4px;color:#58a6ff">NVDA</code>
  <code style="background:#0d1117;padding:1px 6px;border-radius:4px;color:#58a6ff">TSLA</code>
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
                s2.metric("利多", len(sdf[sdf["sentiment"] == "bullish"]))
                s3.metric("利空", len(sdf[sdf["sentiment"] == "bearish"]))
                s4.metric("✦ AI 分析", len(sdf[sdf.get("ai_summary", pd.Series(dtype=str)).ne("") if "ai_summary" in sdf.columns else pd.Series([False]*len(sdf))]))
                csv_s = sdf[["title","sentiment_label","sentiment_score","ai_sentiment","ai_score","ai_summary","tickers","sectors","source","published_at","url"]].to_csv(index=False, encoding="utf-8-sig")
                st.download_button("⬇ 匯出 CSV", csv_s, file_name=f"finnews_{code_q}.csv", mime="text/csv", key="stock_csv")
                st.divider()
                render_news(sdf)

    # ── 全部新聞篩選 ──
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
                f'<span style="background:{"#3d0f0f" if lb=="利多" else "#0f2a1a"};'
                f'padding:3px 10px;border-radius:10px;font-size:11px;'
                f'border:1px solid {"#6e1a1a" if lb=="利多" else "#1a4a2a"};'
                f'color:{"#f85149" if lb=="利多" else "#3fb950"}">'
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
                row_html = (
                    "<tr>"
                    f'<td style="color:#e6edf3">{lr["來源"]}</td>'
                    f"<td>{sbadge}</td>"
                    f'<td style="color:#8b949e">{lr["抓取"]}</td>'
                    f'<td style="color:#3fb950;font-weight:600">{lr["新增"]}</td>'
                    f'<td style="color:#484f58">{lr["跳過"]}</td>'
                    f'<td style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#484f58">{lr["時間(台灣)"]}</td>'
                    "</tr>"
                )
                log_rows_html.append(row_html)
            st.markdown(
                '<div style="overflow-x:auto">'
                '<table class="log-table"><thead><tr>'
                '<th>來源</th><th>狀態</th><th>抓取</th>'
                '<th style="color:#3fb950">新增</th>'
                '<th style="color:#484f58">跳過</th>'
                '<th>時間(台灣)</th>'
                '</tr></thead><tbody>'
                + "".join(log_rows_html)
                + '</tbody></table></div>',
                unsafe_allow_html=True,
            )
