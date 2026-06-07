"""
app.py — FinNews AI 財經新聞智慧分析系統 v2.0
全中文來源 + Groq AI 深度分析版

來源：鉅亨網 / MoneyDJ / Yahoo奇摩 / 經濟日報 / 工商時報 / 科技新報
AI：Groq Llama 3.3 70B（選擇性觸發，節省 quota）
時間：全部台灣時間（UTC+8）

[v2.0 重大改版]
- Tab 從 8 個合併為 3 個：今日速覽 / 深度篩選 / 設定
- 全新暗色主題設計語言（高對比、清晰層次）
- 新聞卡片改為清單式 + 展開摘要
- AI 總結加入結構化顯示（整體方向 / 主題 / 關注個股）
- 台股紅漲綠跌色彩系統統一
- 重點新聞強度排序清單，移除 meta 噪音
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
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
from utils.ui import (news_table, ticker_card, tickers_html,
                      sectors_html, badge, ai_badge, score_bar)


def now_tw_str() -> str:
    return datetime.now(TZ_TW).strftime("%H:%M:%S")


# ════════════════════════════════════════════════════════════════════════════
# 今日 AI 市場總結
# ════════════════════════════════════════════════════════════════════════════
def get_daily_ai_summary(ai_news_df: pd.DataFrame) -> tuple[str, str]:
    import requests

    groq_key = ""
    try:
        groq_key = st.secrets.get("GROQ_API_KEY", "")
    except Exception:
        pass
    if not groq_key:
        groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return "", "找不到 GROQ_API_KEY，請確認 Secrets 設定"

    rows = ai_news_df.head(15)
    lines = []
    for _, r in rows.iterrows():
        sent_label = {"bullish": "利多", "bearish": "利空"}.get(
            r.get("ai_sentiment", ""), "中性")
        summary = r.get("ai_summary") or r.get("title", "")
        lines.append(f"[{sent_label}] {summary}")
    news_text = "\n".join(lines)

    if not news_text.strip():
        return "", "沒有可用的 AI 分析新聞素材"

    prompt = f"""以下是今日台灣財經新聞的 AI 分析摘要列表：

{news_text}

請根據以上資訊，用繁體中文輸出「今日台灣股市 AI 總結」，嚴格按以下 JSON 格式回應，不要有任何其他文字：

{{
  "direction": "偏多/偏空/震盪（三擇一）",
  "direction_reason": "一句話說明整體方向原因（30字內）",
  "bull_themes": ["利多主題1（20字內）", "利多主題2（20字內）"],
  "bear_themes": ["利空主題1（20字內）", "利空主題2（20字內）"],
  "key_tickers": ["2330", "NVDA"],
  "summary": "一段客觀總結（60~100字，財經播報員語氣）"
}}"""

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {groq_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.2,
            },
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        return result, ""
    except requests.exceptions.Timeout:
        return "", "Groq API 逾時（15秒），請稍後重試"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        if status == 429:
            return "", "Groq 速率限制（429），請等 1 分鐘後點『重新生成』"
        return "", f"Groq HTTP 錯誤 {status}，請稍後重試"
    except Exception as e:
        return "", f"生成失敗：{e}"


# ════════════════════════════════════════════════════════════════════════════
# 頁面設定
# ════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="FinNews AI — 財經新聞分析",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  /* ── 全域字型與背景 ── */
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

  html, body, [class*="css"] {
    font-family: 'Noto Sans TC', sans-serif;
  }

  /* ── Streamlit 背景壓暗 ── */
  .stApp { background: #0E1117; }
  section[data-testid="stSidebar"] { background: #161B22 !important; }
  section[data-testid="stSidebar"] > div { background: #161B22 !important; }

  /* ── Tab 樣式 ── */
  .stTabs [data-baseweb="tab-list"] {
    background: #161B22;
    border-radius: 10px;
    padding: 4px;
    gap: 2px;
    border: 1px solid #2D333B;
  }
  .stTabs [data-baseweb="tab"] {
    font-size: 14px;
    font-weight: 600;
    padding: 8px 22px;
    border-radius: 8px;
    color: #8B949E;
    letter-spacing: 0.3px;
  }
  .stTabs [aria-selected="true"] {
    background: #21262D !important;
    color: #F0F6FC !important;
  }

  /* ── Metrics ── */
  [data-testid="metric-container"] {
    background: #161B22;
    border: 1px solid #2D333B;
    border-radius: 10px;
    padding: 14px 18px;
  }
  [data-testid="stMetricLabel"] { color: #8B949E !important; font-size: 12px !important; }
  [data-testid="stMetricValue"] { color: #F0F6FC !important; font-size: 22px !important; font-weight: 700 !important; }
  [data-testid="stMetricDelta"] { font-size: 12px !important; }

  /* ── 台股慣例：漲紅跌綠 ── */
  [data-testid="stMetricDelta"][data-direction="up"]   { color: #E85454 !important; }
  [data-testid="stMetricDelta"][data-direction="down"] { color: #3FB950 !important; }

  /* ── Buttons ── */
  .stButton > button {
    border-radius: 8px;
    font-weight: 600;
    font-size: 13px;
    border: 1px solid #2D333B;
    background: #21262D;
    color: #F0F6FC;
    transition: all 0.15s;
  }
  .stButton > button:hover { background: #2D333B; border-color: #444C56; }
  .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #E85454 0%, #C0392B 100%);
    border-color: #E85454;
    color: #fff;
  }
  .stButton > button[kind="primary"]:hover { opacity: 0.9; }

  /* ── Selectbox / Input ── */
  .stSelectbox > div > div,
  .stTextInput > div > div > input {
    background: #21262D !important;
    border: 1px solid #2D333B !important;
    border-radius: 8px !important;
    color: #F0F6FC !important;
    font-size: 13px !important;
  }

  /* ── Checkbox ── */
  .stCheckbox label { color: #8B949E; font-size: 13px; }

  /* ── Divider ── */
  hr { border-color: #2D333B !important; margin: 16px 0; }

  /* ══════════════════════════════════════════
     AI 總結橫幅
  ══════════════════════════════════════════ */
  .ai-summary-wrap {
    background: #161B22;
    border: 1px solid #2D333B;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 8px;
  }
  .ai-summary-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
  }
  .ai-summary-badge {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    color: #F0A500;
    background: rgba(240,165,0,0.12);
    border: 1px solid rgba(240,165,0,0.3);
    border-radius: 4px;
    padding: 2px 8px;
    text-transform: uppercase;
  }
  .ai-direction-bull {
    font-size: 18px;
    font-weight: 700;
    color: #E85454;
  }
  .ai-direction-bear {
    font-size: 18px;
    font-weight: 700;
    color: #3FB950;
  }
  .ai-direction-neutral {
    font-size: 18px;
    font-weight: 700;
    color: #8B949E;
  }
  .ai-direction-reason {
    font-size: 13px;
    color: #8B949E;
    margin-top: 2px;
    margin-bottom: 14px;
  }
  .ai-themes-row {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .ai-theme-bull {
    background: rgba(232,84,84,0.1);
    border: 1px solid rgba(232,84,84,0.3);
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 12px;
    color: #E85454;
    font-weight: 600;
  }
  .ai-theme-bear {
    background: rgba(63,185,80,0.1);
    border: 1px solid rgba(63,185,80,0.3);
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 12px;
    color: #3FB950;
    font-weight: 600;
  }
  .ai-summary-text {
    font-size: 14px;
    line-height: 1.9;
    color: #C9D1D9;
    border-top: 1px solid #2D333B;
    padding-top: 12px;
    margin-top: 4px;
  }
  .ai-key-tickers {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .ai-ticker-chip {
    background: #21262D;
    border: 1px solid #444C56;
    border-radius: 5px;
    padding: 2px 10px;
    font-size: 12px;
    color: #F0F6FC;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
  }
  .ai-summary-footer {
    font-size: 11px;
    color: #444C56;
    margin-top: 10px;
  }

  /* ══════════════════════════════════════════
     新聞卡片清單
  ══════════════════════════════════════════ */
  .news-item {
    background: #161B22;
    border: 1px solid #2D333B;
    border-radius: 10px;
    padding: 13px 16px;
    margin-bottom: 7px;
    border-left: 3px solid #2D333B;
    transition: border-color 0.15s, background 0.15s;
  }
  .news-item:hover { background: #1C2128; border-color: #444C56; }
  .news-item.bull  { border-left-color: #E85454; }
  .news-item.bear  { border-left-color: #3FB950; }
  .news-item.geo   { border-left-color: #F0A500; }
  .news-title {
    font-size: 14px;
    font-weight: 600;
    color: #F0F6FC;
    line-height: 1.5;
    margin-bottom: 5px;
  }
  .news-title a {
    color: inherit;
    text-decoration: none;
  }
  .news-title a:hover { color: #79C0FF; }
  .news-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 5px;
  }
  .news-score-bull {
    font-size: 11px;
    font-weight: 700;
    color: #E85454;
    font-family: 'JetBrains Mono', monospace;
    background: rgba(232,84,84,0.1);
    border-radius: 4px;
    padding: 1px 7px;
  }
  .news-score-bear {
    font-size: 11px;
    font-weight: 700;
    color: #3FB950;
    font-family: 'JetBrains Mono', monospace;
    background: rgba(63,185,80,0.1);
    border-radius: 4px;
    padding: 1px 7px;
  }
  .news-score-neutral {
    font-size: 11px;
    font-weight: 600;
    color: #8B949E;
    font-family: 'JetBrains Mono', monospace;
    background: rgba(139,148,158,0.1);
    border-radius: 4px;
    padding: 1px 7px;
  }
  .news-source {
    font-size: 11px;
    color: #8B949E;
  }
  .news-time {
    font-size: 11px;
    color: #444C56;
    font-family: 'JetBrains Mono', monospace;
  }
  .news-ticker {
    font-size: 11px;
    font-weight: 600;
    color: #79C0FF;
    font-family: 'JetBrains Mono', monospace;
    background: rgba(121,192,255,0.1);
    border-radius: 4px;
    padding: 1px 6px;
  }
  .news-ai-badge {
    font-size: 10px;
    font-weight: 700;
    color: #F0A500;
    background: rgba(240,165,0,0.1);
    border: 1px solid rgba(240,165,0,0.2);
    border-radius: 4px;
    padding: 1px 6px;
    letter-spacing: 0.5px;
  }
  .news-geo-badge {
    font-size: 10px;
    font-weight: 700;
    color: #F0A500;
    background: rgba(240,165,0,0.1);
    border: 1px solid rgba(240,165,0,0.2);
    border-radius: 4px;
    padding: 1px 6px;
  }
  .news-ai-summary {
    margin-top: 9px;
    padding: 9px 12px;
    background: #0D1117;
    border-radius: 7px;
    border-left: 2px solid #F0A500;
    font-size: 13px;
    color: #C9D1D9;
    line-height: 1.75;
  }
  .news-ai-reason {
    margin-top: 5px;
    font-size: 11px;
    color: #8B949E;
  }

  /* ══════════════════════════════════════════
     熱門股票卡片
  ══════════════════════════════════════════ */
  .hot-ticker-card {
    background: #161B22;
    border: 1px solid #2D333B;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 10px;
    text-align: center;
    transition: border-color 0.15s;
  }
  .hot-ticker-card:hover { border-color: #444C56; }
  .hot-ticker-code {
    font-size: 18px;
    font-weight: 700;
    color: #F0F6FC;
    font-family: 'JetBrains Mono', monospace;
  }
  .hot-ticker-name {
    font-size: 12px;
    color: #8B949E;
    margin: 2px 0 6px;
  }
  .hot-ticker-count {
    font-size: 11px;
    color: #444C56;
  }
  .hot-ticker-score-bull { color: #E85454; font-size: 12px; font-weight: 700; }
  .hot-ticker-score-bear { color: #3FB950; font-size: 12px; font-weight: 700; }
  .hot-ticker-score-neu  { color: #8B949E; font-size: 12px; font-weight: 600; }

  /* ══════════════════════════════════════════
     側邊欄
  ══════════════════════════════════════════ */
  .sidebar-title {
    font-size: 18px;
    font-weight: 700;
    color: #F0F6FC;
    letter-spacing: -0.3px;
    margin-bottom: 2px;
  }
  .sidebar-sub {
    font-size: 11px;
    color: #8B949E;
    margin-bottom: 12px;
  }
  .status-pill-ok {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: rgba(63,185,80,0.1);
    border: 1px solid rgba(63,185,80,0.25);
    border-radius: 20px;
    padding: 4px 10px;
    font-size: 11px;
    color: #3FB950;
    font-weight: 600;
    margin-bottom: 6px;
  }
  .status-pill-warn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: rgba(240,165,0,0.1);
    border: 1px solid rgba(240,165,0,0.25);
    border-radius: 20px;
    padding: 4px 10px;
    font-size: 11px;
    color: #F0A500;
    font-weight: 600;
    margin-bottom: 6px;
  }
  .sidebar-stat {
    background: #0D1117;
    border: 1px solid #2D333B;
    border-radius: 8px;
    padding: 10px 14px;
    margin: 8px 0;
    font-size: 12px;
    color: #C9D1D9;
  }
  .sidebar-stat-num {
    font-size: 22px;
    font-weight: 700;
    color: #F0F6FC;
    font-family: 'JetBrains Mono', monospace;
    display: block;
    line-height: 1.2;
  }
  .sidebar-bar-wrap {
    background: #0D1117;
    border-radius: 4px;
    height: 6px;
    margin: 4px 0 2px;
    overflow: hidden;
  }
  .sidebar-bar-inner-bull { height: 6px; border-radius: 4px; background: #E85454; }
  .sidebar-bar-inner-bear { height: 6px; border-radius: 4px; background: #3FB950; }

  /* ══════════════════════════════════════════
     區塊標題
  ══════════════════════════════════════════ */
  .section-title {
    font-size: 13px;
    font-weight: 700;
    color: #8B949E;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    margin: 20px 0 10px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .section-title::after {
    content: '';
    flex: 1;
    height: 1px;
    background: #2D333B;
    margin-left: 6px;
  }

  /* Geo 警示橫幅 */
  .geo-banner {
    background: rgba(240,165,0,0.08);
    border: 1px solid rgba(240,165,0,0.25);
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 10px;
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }
  .geo-banner-icon { font-size: 18px; flex-shrink: 0; margin-top: 1px; }
  .geo-banner-content { flex: 1; }
  .geo-banner-title {
    font-size: 13px;
    font-weight: 700;
    color: #F0A500;
    margin-bottom: 2px;
  }
  .geo-banner-text { font-size: 12px; color: #C9D1D9; line-height: 1.6; }

  /* 濾鏡列 */
  .filter-row {
    background: #161B22;
    border: 1px solid #2D333B;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 14px;
  }

  /* 空狀態 */
  .empty-state {
    text-align: center;
    padding: 48px 24px;
    color: #444C56;
  }
  .empty-state-icon { font-size: 40px; margin-bottom: 10px; }
  .empty-state-text { font-size: 14px; }

  /* Plotly 圖表背景 */
  .js-plotly-plot { border-radius: 10px; }

  /* Caption */
  .stCaption { color: #444C56 !important; font-size: 11px !important; }

  /* Progress bar */
  .stProgress > div > div > div > div { background: #E85454; }
</style>
""", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════════════════════════════════════
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


# ════════════════════════════════════════════════════════════════════════════
# Helper：渲染新聞清單（統一版）
# ════════════════════════════════════════════════════════════════════════════
def render_news_list(df: pd.DataFrame, key: str, max_items: int = 100,
                     show_expand: bool = True):
    """
    統一的新聞清單渲染。
    每則新聞：左側色條 + 標題 + meta行 + 可展開摘要
    """
    if df is None or df.empty:
        st.markdown("""
        <div class="empty-state">
          <div class="empty-state-icon">📭</div>
          <div class="empty-state-text">沒有符合條件的新聞</div>
        </div>""", unsafe_allow_html=True)
        return

    display_df = df.head(max_items)

    for idx, (_, row) in enumerate(display_df.iterrows()):
        sent        = row.get("sentiment", "neutral")
        ai_sent     = row.get("ai_sentiment", "")
        is_geo      = row.get("is_geo", False)
        ai_score    = row.get("ai_score", 0.0) or 0.0
        kw_score    = row.get("sentiment_score", 0.0) or 0.0
        ai_summary  = row.get("ai_summary", "") or ""
        ai_reason   = row.get("ai_reason", "") or ""
        title       = row.get("title", "")
        url         = row.get("url", "")
        source      = row.get("source", "")
        tickers_raw = row.get("ai_affected_tickers", "") or row.get("tickers", "") or ""
        tickers     = [t.strip() for t in tickers_raw.split(",") if t.strip()]

        # 時間
        pub_str = ""
        if row.get("published_at") is not None:
            try:
                pub_str = row["published_at"].strftime("%m/%d %H:%M")
            except Exception:
                pass

        # 用 AI 情緒優先；無 AI 則用關鍵字情緒
        eff_sent = ai_sent if ai_sent in ("bullish", "bearish") else sent

        if is_geo:
            card_cls = "news-item geo"
        elif eff_sent == "bullish":
            card_cls = "news-item bull"
        elif eff_sent == "bearish":
            card_cls = "news-item bear"
        else:
            card_cls = "news-item"

        # 分數顯示（AI優先）
        if ai_score and ai_score != 0.0:
            score_val = ai_score
        else:
            score_val = kw_score * 10  # kw_score 是 -1~1，換算成 -10~10

        if score_val > 0:
            score_html = f'<span class="news-score-bull">+{score_val:.1f}</span>'
        elif score_val < 0:
            score_html = f'<span class="news-score-bear">{score_val:.1f}</span>'
        else:
            score_html = '<span class="news-score-neutral">—</span>'

        # 標題（有連結就包）
        if url:
            title_html = f'<a href="{url}" target="_blank">{title}</a>'
        else:
            title_html = title

        # Badges
        badges = []
        if ai_summary:
            badges.append('<span class="news-ai-badge">✦ AI</span>')
        if is_geo:
            badges.append('<span class="news-geo-badge">⚑ 地緣</span>')
        for t in tickers[:4]:
            badges.append(f'<span class="news-ticker">{t}</span>')
        badges_html = " ".join(badges)

        card_html = f"""
        <div class="{card_cls}">
          <div class="news-title">{title_html}</div>
          <div class="news-meta">
            {score_html}
            {badges_html}
            <span class="news-source">{source}</span>
            <span class="news-time">{pub_str}</span>
          </div>
          {f'<div class="news-ai-summary">{ai_summary}{"<div class=news-ai-reason>📌 " + ai_reason + "</div>" if ai_reason else ""}</div>' if ai_summary else ""}
        </div>"""

        st.markdown(card_html, unsafe_allow_html=True)

    if len(df) > max_items:
        st.caption(f"顯示前 {max_items} 則，共 {len(df)} 則")


# ════════════════════════════════════════════════════════════════════════════
# 側邊欄
# ════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<div class="sidebar-title">📈 FinNews AI</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-sub">台灣財經新聞智慧分析系統</div>', unsafe_allow_html=True)

    if st.session_state["groq_ok"]:
        st.markdown(
            '<div class="status-pill-ok">● Groq AI 已啟用</div>',
            unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="status-pill-warn">⚠ 純關鍵字模式</div>',
            unsafe_allow_html=True)

    st.caption(f"下次更新：{next_run_time()} ｜ 最後：{st.session_state['last_update']}")

    st.divider()

    use_ai_cb = st.checkbox(
        "啟用 AI 深度分析",
        value=st.session_state["use_ai"],
        disabled=not st.session_state["groq_ok"],
        key="use_ai_cb",
    )
    st.session_state["use_ai"] = use_ai_cb

    if st.button("🔄 立即抓取新聞", use_container_width=True, type="primary"):
        with st.spinner("抓取 + 分析中，約需 30～60 秒…"):
            result = crawl_and_save(
                enabled_names=st.session_state["enabled_srcs"],
                custom_bull=st.session_state["custom_bull"],
                custom_bear=st.session_state["custom_bear"],
                use_ai=st.session_state["use_ai"],
            )
            st.session_state["last_update"] = now_tw_str()
            st.cache_data.clear()

        ai_info = f"｜AI分析 {result.get('ai_count',0)} 則" if st.session_state["use_ai"] else ""
        st.success(
            f"✅ 新增 **{result['saved']}** 則"
            f"｜去重 {result['skipped']} 則"
            f"{ai_info}"
            f"｜耗時 {result['elapsed']}s"
        )
        st.rerun()

    st.divider()

    # 資料庫統計
    _db = SessionLocal()
    _counts = get_sentiment_counts(_db)
    _db.close()
    _total = sum(_counts.values())
    _bull_n = _counts.get("bullish", 0)
    _bear_n = _counts.get("bearish", 0)

    st.markdown(f"""
    <div class="sidebar-stat">
      <span class="sidebar-stat-num">{_total}</span>
      資料庫新聞總數
      <div class="sidebar-bar-wrap">
        <div class="sidebar-bar-inner-bull" style="width:{(_bull_n/_total*100) if _total else 0:.0f}%"></div>
      </div>
      <span style="font-size:11px;color:#E85454">📈 利多 {_bull_n}</span>
      <span style="font-size:11px;color:#8B949E;margin:0 6px">/</span>
      <span style="font-size:11px;color:#3FB950">📉 利空 {_bear_n}</span>
    </div>
    """, unsafe_allow_html=True)

    st.divider()
    st.caption("📡 鉅亨網 · MoneyDJ · Yahoo奇摩")
    st.caption("　　經濟日報 · 工商時報 · 科技新報")
    st.caption("✦ AI：Groq Llama 3.3 70B")


# ════════════════════════════════════════════════════════════════════════════
# 主體 Tabs（3個）
# ════════════════════════════════════════════════════════════════════════════
tab_dash, tab_deep, tab_settings = st.tabs([
    "📊 今日速覽",
    "🔍 深度篩選",
    "⚙️ 設定",
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1：今日速覽
# ════════════════════════════════════════════════════════════════════════════
with tab_dash:

    @st.cache_data(ttl=60, show_spinner=False)
    def load_dash():
        db = SessionLocal()
        try:
            df     = get_articles_df(db, limit=300)
            counts = get_sentiment_counts(db)
            secs   = get_sector_counts(db)
            tickers= get_ticker_counts(db, limit=30)
            geo_df = get_articles_df(db, geo_only=True, limit=10)
        finally:
            db.close()
        return df, counts, secs, tickers, geo_df

    @st.cache_data(ttl=60, show_spinner=False)
    def load_ai_24h():
        db = SessionLocal()
        try:
            all_ai = get_articles_df(db, ai_only=True, limit=2000)
        finally:
            db.close()
        if all_ai.empty:
            return all_ai
        cutoff = datetime.now(TZ_TW) - timedelta(hours=24)
        mask = all_ai["published_at"].apply(
            lambda x: x is not None and (
                x >= cutoff if getattr(x, "tzinfo", None) else True
            )
        )
        result = all_ai[mask]
        return result if not result.empty else all_ai

    df, counts, secs, hot_tickers, geo_df = load_dash()
    ai_24h_df = load_ai_24h()

    total  = sum(counts.values())
    bull_n = counts.get("bullish", 0)
    bear_n = counts.get("bearish", 0)
    mid_n  = counts.get("neutral", 0)

    # ── 頂部指標 ─────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📰 新聞總數", total)
    if total:
        c2.metric("📈 利多", bull_n,
                  delta=f"{bull_n/total*100:.1f}%")
        c3.metric("📉 利空", bear_n,
                  delta=f"-{bear_n/total*100:.1f}%",
                  delta_color="inverse")
    else:
        c2.metric("📈 利多", 0)
        c3.metric("📉 利空", 0)
    c4.metric("✦ AI 分析", len(ai_24h_df))
    c5.metric("⚑ 地緣政治", len(geo_df))

    # ── AI 市場總結 ──────────────────────────────────────────────────────
    st.markdown('<div class="section-title">✦ AI 市場總結</div>', unsafe_allow_html=True)

    if not st.session_state["groq_ok"]:
        st.markdown("""
        <div style="background:rgba(240,165,0,0.08);border:1px solid rgba(240,165,0,0.2);
                    border-radius:10px;padding:14px 18px;color:#F0A500;font-size:13px">
          ⚠ 需要設定 Groq API Key 才能顯示 AI 市場總結
        </div>""", unsafe_allow_html=True)
    elif ai_24h_df.empty:
        st.markdown("""
        <div style="background:#161B22;border:1px solid #2D333B;border-radius:10px;
                    padding:14px 18px;color:#8B949E;font-size:13px">
          尚無 AI 分析資料，請先抓取新聞並啟用 AI 深度分析
        </div>""", unsafe_allow_html=True)
    else:
        latest_ts = str(ai_24h_df.iloc[0].get("published_at", ""))
        cache_key = f"daily_summary_v2_{latest_ts}"

        if st.session_state.get("_summary_cache_key") != cache_key:
            with st.spinner("AI 正在生成今日市場總結…"):
                ai_for_summary = ai_24h_df.sort_values(
                    "importance_score" if "importance_score" in ai_24h_df.columns
                    else "ai_score", ascending=False
                )
                summary_data, summary_err = get_daily_ai_summary(ai_for_summary)
            st.session_state["_daily_summary"]     = summary_data
            st.session_state["_daily_summary_err"] = summary_err
            st.session_state["_summary_cache_key"] = cache_key
            st.session_state["_summary_time"]      = datetime.now(TZ_TW).strftime("%H:%M")
        else:
            summary_data = st.session_state.get("_daily_summary", "")
            summary_err  = st.session_state.get("_daily_summary_err", "")

        if summary_data and isinstance(summary_data, dict):
            direction    = summary_data.get("direction", "震盪")
            dir_reason   = summary_data.get("direction_reason", "")
            bull_themes  = summary_data.get("bull_themes", [])
            bear_themes  = summary_data.get("bear_themes", [])
            key_tickers  = summary_data.get("key_tickers", [])
            summary_text = summary_data.get("summary", "")
            gen_time     = st.session_state.get("_summary_time", "")

            if direction == "偏多":
                dir_cls = "ai-direction-bull"
                dir_icon = "↗"
            elif direction == "偏空":
                dir_cls = "ai-direction-bear"
                dir_icon = "↘"
            else:
                dir_cls = "ai-direction-neutral"
                dir_icon = "→"

            bull_tags = "".join(
                f'<span class="ai-theme-bull">📈 {t}</span>'
                for t in bull_themes
            )
            bear_tags = "".join(
                f'<span class="ai-theme-bear">📉 {t}</span>'
                for t in bear_themes
            )
            ticker_tags = "".join(
                f'<span class="ai-ticker-chip">{t}</span>'
                for t in key_tickers
            )

            st.markdown(f"""
            <div class="ai-summary-wrap">
              <div class="ai-summary-header">
                <span class="ai-summary-badge">Groq AI · 今日總結</span>
              </div>
              <div class="{dir_cls}">{dir_icon} 整體{direction}</div>
              <div class="ai-direction-reason">{dir_reason}</div>
              <div class="ai-themes-row">{bull_tags}{bear_tags}</div>
              {"<div style='font-size:11px;color:#8B949E;margin-bottom:4px'>關注個股</div><div class='ai-key-tickers'>" + ticker_tags + "</div>" if ticker_tags else ""}
              <div class="ai-summary-text">{summary_text}</div>
              <div class="ai-summary-footer">
                根據過去 24 小時 {len(ai_24h_df)} 則 AI 分析新聞生成 · {gen_time} 台灣時間
              </div>
            </div>""", unsafe_allow_html=True)

            if st.button("🔄 重新生成總結", key="regen_summary"):
                st.session_state.pop("_summary_cache_key", None)
                st.rerun()

        elif summary_err:
            st.markdown(f"""
            <div style="background:rgba(232,84,84,0.08);border:1px solid rgba(232,84,84,0.25);
                        border-radius:10px;padding:14px 18px;color:#E85454;font-size:13px">
              ⚠ AI 總結生成失敗：{summary_err}
            </div>""", unsafe_allow_html=True)
            if st.button("🔄 重試", key="regen_summary"):
                st.session_state.pop("_summary_cache_key", None)
                st.rerun()

    # ── 地緣政治警示（如有） ────────────────────────────────────────────
    if not geo_df.empty:
        st.markdown('<div class="section-title">⚑ 地緣政治警示</div>', unsafe_allow_html=True)
        for _, row in geo_df.head(3).iterrows():
            ai_sent    = row.get("ai_sentiment", "")
            kw_sent    = row.get("sentiment", "neutral")
            eff_sent   = ai_sent if ai_sent in ("bullish","bearish") else kw_sent
            impact_cls = "color:#E85454" if eff_sent == "bullish" else ("color:#3FB950" if eff_sent == "bearish" else "color:#8B949E")
            impact_txt = "利多" if eff_sent == "bullish" else ("利空" if eff_sent == "bearish" else "中性")
            url        = row.get("url", "")
            title      = row.get("title", "")
            ai_sum     = row.get("ai_summary", "") or ""
            link_html  = f'<a href="{url}" target="_blank" style="color:#F0A500;font-size:13px;font-weight:700;text-decoration:none">{title}</a>' if url else f'<span style="color:#F0A500;font-size:13px;font-weight:700">{title}</span>'
            st.markdown(f"""
            <div class="geo-banner">
              <div class="geo-banner-icon">⚑</div>
              <div class="geo-banner-content">
                <div class="geo-banner-title">{link_html}</div>
                <div class="geo-banner-text" style="{impact_cls};font-weight:600;font-size:11px;margin-bottom:3px">{impact_txt}</div>
                {"<div class='geo-banner-text'>" + ai_sum + "</div>" if ai_sum else ""}
              </div>
            </div>""", unsafe_allow_html=True)

    # ── 今日 AI 重點新聞 ─────────────────────────────────────────────────
    st.markdown('<div class="section-title">🔑 今日重點新聞</div>', unsafe_allow_html=True)

    if ai_24h_df.empty:
        st.markdown("""
        <div class="empty-state">
          <div class="empty-state-icon">📭</div>
          <div class="empty-state-text">請先抓取新聞並啟用 AI 深度分析</div>
        </div>""", unsafe_allow_html=True)
    else:
        # 只取非中性、依重要性排序
        key_df = ai_24h_df[ai_24h_df["ai_sentiment"].isin(["bullish", "bearish"])]
        if key_df.empty:
            key_df = ai_24h_df.copy()
        if "importance_score" in key_df.columns:
            key_df = key_df.sort_values("importance_score", ascending=False)
        else:
            key_df = key_df.reindex(
                key_df["ai_score"].abs().sort_values(ascending=False).index)
        render_news_list(key_df, key="dash_key")

    # ── 圖表區 ──────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">📊 市場概覽</div>', unsafe_allow_html=True)
    col_pie, col_bar, col_hot = st.columns(3)

    with col_pie:
        if total > 0:
            fig = go.Figure(go.Pie(
                labels=["利多", "利空", "中性"],
                values=[bull_n, bear_n, mid_n],
                hole=0.55,
                marker=dict(
                    colors=["#E85454", "#3FB950", "#2D333B"],
                    line=dict(color="#0E1117", width=2)
                ),
                textinfo="percent+label",
                textfont=dict(size=12, color="#C9D1D9"),
            ))
            fig.update_layout(
                showlegend=False,
                margin=dict(t=10, b=10, l=10, r=10),
                height=220,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown("""<div class="empty-state" style="padding:20px">
              <div class="empty-state-icon" style="font-size:24px">📭</div>
              <div class="empty-state-text" style="font-size:12px">請先抓取新聞</div>
            </div>""", unsafe_allow_html=True)

    with col_bar:
        if not secs.empty:
            top_secs = secs.head(8)
            fig2 = go.Figure(go.Bar(
                x=top_secs["count"],
                y=top_secs["sector"],
                orientation="h",
                marker=dict(
                    color=top_secs["count"],
                    colorscale=[[0, "#21262D"], [1, "#79C0FF"]],
                    showscale=False,
                ),
                text=top_secs["count"],
                textposition="outside",
                textfont=dict(size=11, color="#8B949E"),
            ))
            fig2.update_layout(
                yaxis=dict(autorange="reversed", tickfont=dict(color="#C9D1D9", size=11)),
                xaxis=dict(showgrid=False, visible=False),
                margin=dict(t=4, b=4, l=4, r=40),
                height=220,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig2, use_container_width=True)

    with col_hot:
        if not hot_tickers.empty:
            top5 = hot_tickers.head(5)
            for _, row in top5.iterrows():
                score = row["平均情緒"]
                if score >= 0.15:
                    score_cls = "hot-ticker-score-bull"
                    score_str = f"+{score:.2f}"
                elif score <= -0.15:
                    score_cls = "hot-ticker-score-bear"
                    score_str = f"{score:.2f}"
                else:
                    score_cls = "hot-ticker-score-neu"
                    score_str = f"{score:.2f}"
                st.markdown(f"""
                <div class="hot-ticker-card">
                  <div class="hot-ticker-code">{row['代碼']}</div>
                  <div class="hot-ticker-name">{row['名稱']}</div>
                  <div><span class="{score_cls}">{score_str}</span>
                    <span class="hot-ticker-count"> · {row['出現次數']}則</span></div>
                </div>""", unsafe_allow_html=True)

    # ── 最新新聞（快速過濾版）───────────────────────────────────────────
    st.markdown('<div class="section-title">📋 最新新聞</div>', unsafe_allow_html=True)

    with st.container():
        f1, f2, f3, f4 = st.columns([1, 1, 2, 1])
        with f1:
            sent_f = st.selectbox("情緒", ["全部", "利多", "利空", "中性"], key="d_sent")
        with f2:
            srcs_list = sorted(df["source"].unique().tolist()) if not df.empty else []
            src_f = st.selectbox("來源", ["全部"] + srcs_list, key="d_src")
        with f3:
            kw = st.text_input("🔍 搜尋標題", placeholder="關鍵字…", key="d_kw")
        with f4:
            sort_f = st.selectbox("排序", ["最新優先", "強度↓"], key="d_sort")

    hide_neu = st.checkbox("隱藏中性新聞", value=True, key="d_hide_neutral")

    ddf = df.copy() if not df.empty else pd.DataFrame()
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

    st.caption(f"顯示 {len(ddf)} 則")
    render_news_list(ddf, key="dash_news")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2：深度篩選
# ════════════════════════════════════════════════════════════════════════════
with tab_deep:

    mode = st.radio(
        "分析模式",
        ["✦ AI 深度分析", "🔥 熱門股票", "⚑ 地緣政治", "🏭 類股排行", "🔍 個股聚焦"],
        horizontal=True,
        key="deep_mode",
    )

    # ────────────────────────────────────────────────────────────────────
    # 模式：AI 深度分析
    # ────────────────────────────────────────────────────────────────────
    if mode == "✦ AI 深度分析":

        if not st.session_state["groq_ok"]:
            st.warning("Groq API Key 尚未設定。請到 Streamlit Cloud → App Settings → Secrets，新增 `GROQ_API_KEY`。")
        else:
            @st.cache_data(ttl=60, show_spinner=False)
            def load_ai():
                db = SessionLocal()
                try:
                    return get_articles_df(db, ai_only=True, limit=200)
                finally:
                    db.close()

            ai_df = load_ai()

            if ai_df.empty:
                st.markdown("""<div class="empty-state">
                  <div class="empty-state-icon">🤖</div>
                  <div class="empty-state-text">尚無 AI 分析結果，請先抓取新聞並啟用 AI 深度分析</div>
                </div>""", unsafe_allow_html=True)
            else:
                a1, a2, a3, a4 = st.columns(4)
                a1.metric("✦ AI 分析總數", len(ai_df))
                ai_bull = len(ai_df[ai_df["ai_sentiment"] == "bullish"])
                ai_bear = len(ai_df[ai_df["ai_sentiment"] == "bearish"])
                ai_df["情緒一致"] = ai_df.apply(
                    lambda r: r["ai_sentiment"] == r["sentiment"], axis=1)
                diff_cnt = len(ai_df[~ai_df["情緒一致"]])
                a2.metric("📈 AI 判多", ai_bull)
                a3.metric("📉 AI 判空", ai_bear)
                a4.metric("⚡ AI vs KW 不一致", diff_cnt,
                          help="AI 與關鍵字判斷不同，最有參考價值")

                st.divider()

                af1, af2, af3 = st.columns([1, 1, 1])
                with af1:
                    ai_sent_f = st.selectbox("AI 情緒", ["全部", "利多", "利空", "中性"], key="ai_sent_f")
                with af2:
                    ai_conf_f = st.selectbox("信心程度", ["全部", "high（高）", "medium（中）", "low（低）"], key="ai_conf_f")
                with af3:
                    ai_sort_f = st.selectbox("排序", ["重要性↓", "最新優先", "AI分數↓"], key="ai_sort_f")

                diff_only = st.checkbox("🔍 只看 AI 與關鍵字不一致（最有參考價值）", key="ai_diff_only")

                fai = ai_df.copy()
                sm  = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
                if ai_sent_f != "全部":
                    fai = fai[fai["ai_sentiment"] == sm[ai_sent_f]]
                if ai_conf_f != "全部":
                    conf_key = ai_conf_f.split("（")[0]
                    fai = fai[fai["ai_confidence"] == conf_key]
                if diff_only:
                    fai = fai[~fai["情緒一致"]]
                if ai_sort_f == "重要性↓" and "importance_score" in fai.columns:
                    fai = fai.sort_values("importance_score", ascending=False)
                elif ai_sort_f == "AI分數↓":
                    fai = fai.reindex(fai["ai_score"].abs().sort_values(ascending=False).index)

                st.caption(f"顯示 {len(fai)} 則")
                render_news_list(fai, key="ai_all")

    # ────────────────────────────────────────────────────────────────────
    # 模式：熱門股票
    # ────────────────────────────────────────────────────────────────────
    elif mode == "🔥 熱門股票":

        @st.cache_data(ttl=60, show_spinner=False)
        def load_hot_full():
            db = SessionLocal()
            try:
                return get_ticker_counts(db, limit=30)
            finally:
                db.close()

        hot_df = load_hot_full()

        if hot_df.empty:
            st.markdown("""<div class="empty-state">
              <div class="empty-state-icon">📊</div>
              <div class="empty-state-text">請先抓取新聞資料</div>
            </div>""", unsafe_allow_html=True)
        else:
            st.markdown("#### Top 12 熱門股票")
            top12 = hot_df.head(12)
            cols  = st.columns(4)
            for i, (_, row) in enumerate(top12.iterrows()):
                with cols[i % 4]:
                    score = row["平均情緒"]
                    if score >= 0.15:
                        score_cls = "hot-ticker-score-bull"
                        score_str = f"+{score:.2f}"
                    elif score <= -0.15:
                        score_cls = "hot-ticker-score-bear"
                        score_str = f"{score:.2f}"
                    else:
                        score_cls = "hot-ticker-score-neu"
                        score_str = f"{score:.2f}"
                    market = row.get("市場", "TW")
                    if market == "TW":
                        link = f"https://tw.stock.yahoo.com/quote/{row['代碼']}"
                    else:
                        link = f"https://finance.yahoo.com/quote/{row['代碼']}"
                    st.markdown(f"""
                    <div class="hot-ticker-card">
                      <a href="{link}" target="_blank" style="text-decoration:none">
                        <div class="hot-ticker-code">{row['代碼']}</div>
                      </a>
                      <div class="hot-ticker-name">{row['名稱']}</div>
                      <div>
                        <span class="{score_cls}">{score_str}</span>
                        <span class="hot-ticker-count"> · {row['出現次數']} 則</span>
                      </div>
                    </div>""", unsafe_allow_html=True)

            st.divider()
            col_c1, col_c2 = st.columns(2)
            with col_c1:
                fig_h = go.Figure(go.Bar(
                    x=hot_df.head(15)["出現次數"],
                    y=hot_df.head(15)["代碼"],
                    orientation="h",
                    text=hot_df.head(15)["名稱"],
                    textposition="outside",
                    textfont=dict(size=10, color="#8B949E"),
                    marker=dict(
                        color=hot_df.head(15)["出現次數"],
                        colorscale=[[0,"#21262D"],[1,"#79C0FF"]],
                        showscale=False,
                    ),
                ))
                fig_h.update_layout(
                    title=dict(text="出現次數", font=dict(color="#C9D1D9", size=13)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#C9D1D9", size=11)),
                    xaxis=dict(showgrid=False, visible=False),
                    margin=dict(t=30, b=10, l=10, r=60),
                    height=420,
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_h, use_container_width=True)

            with col_c2:
                cdf = hot_df.head(15).copy()
                colors = ["#E85454" if s >= 0.15 else ("#3FB950" if s <= -0.15 else "#444C56")
                          for s in cdf["平均情緒"]]
                fig_s = go.Figure(go.Bar(
                    x=cdf["平均情緒"],
                    y=cdf["代碼"],
                    orientation="h",
                    marker=dict(color=colors),
                    text=cdf["平均情緒"].round(2),
                    textposition="outside",
                    textfont=dict(size=10, color="#8B949E"),
                ))
                fig_s.update_layout(
                    title=dict(text="平均情緒分數", font=dict(color="#C9D1D9", size=13)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#C9D1D9", size=11)),
                    xaxis=dict(showgrid=False, visible=False),
                    margin=dict(t=30, b=10, l=10, r=60),
                    height=420,
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_s, use_container_width=True)

    # ────────────────────────────────────────────────────────────────────
    # 模式：地緣政治
    # ────────────────────────────────────────────────────────────────────
    elif mode == "⚑ 地緣政治":

        @st.cache_data(ttl=60, show_spinner=False)
        def load_geo_full():
            db = SessionLocal()
            try:
                return get_articles_df(db, geo_only=True, limit=100)
            finally:
                db.close()

        geo_full_df = load_geo_full()

        if geo_full_df.empty:
            st.markdown("""<div class="empty-state">
              <div class="empty-state-icon">🌏</div>
              <div class="empty-state-text">目前沒有地緣政治相關新聞</div>
            </div>""", unsafe_allow_html=True)
        else:
            g1, g2, g3 = st.columns(3)
            g1.metric("⚑ 地緣政治新聞", len(geo_full_df))
            geo_bear = len(geo_full_df[geo_full_df["sentiment"] == "bearish"])
            geo_bull = len(geo_full_df[geo_full_df["sentiment"] == "bullish"])
            g2.metric("📉 利空", geo_bear,
                      delta=f"-{geo_bear/len(geo_full_df)*100:.0f}%",
                      delta_color="inverse")
            g3.metric("📈 利多", geo_bull,
                      delta=f"{geo_bull/len(geo_full_df)*100:.0f}%")
            st.divider()
            render_news_list(geo_full_df, key="geo")

    # ────────────────────────────────────────────────────────────────────
    # 模式：類股排行
    # ────────────────────────────────────────────────────────────────────
    elif mode == "🏭 類股排行":

        @st.cache_data(ttl=60, show_spinner=False)
        def load_sector():
            db = SessionLocal()
            try:
                df_   = get_articles_df(db, limit=500)
                secs_ = get_sector_counts(db)
            finally:
                db.close()
            return df_, secs_

        full_df, secs_df = load_sector()

        if secs_df.empty:
            st.info("請先抓取新聞。")
        else:
            rows = []
            for _, row in secs_df.iterrows():
                sec  = row["sector"]
                cnt  = row["count"]
                mask = full_df["sectors"].str.contains(sec, na=False)
                avg  = float(full_df[mask]["sentiment_score"].mean()) if mask.any() else 0.0
                bull = int(full_df[mask & (full_df["sentiment"] == "bullish")].shape[0])
                bear = int(full_df[mask & (full_df["sentiment"] == "bearish")].shape[0])
                rows.append({"類股": sec, "新聞數": cnt,
                             "平均情緒": round(avg, 3), "利多": bull, "利空": bear})
            rank_df = pd.DataFrame(rows)

            colors_sec = ["#E85454" if s >= 0.05 else ("#3FB950" if s <= -0.05 else "#444C56")
                          for s in rank_df.head(10)["平均情緒"]]
            fig_r = go.Figure(go.Bar(
                x=rank_df.head(10)["新聞數"],
                y=rank_df.head(10)["類股"],
                orientation="h",
                marker=dict(color=colors_sec),
                text=rank_df.head(10)["新聞數"],
                textposition="outside",
                textfont=dict(size=11, color="#8B949E"),
            ))
            fig_r.update_layout(
                title=dict(text="類股新聞數（紅=偏多 綠=偏空）", font=dict(color="#C9D1D9", size=13)),
                yaxis=dict(autorange="reversed", tickfont=dict(color="#C9D1D9", size=12)),
                xaxis=dict(showgrid=False, visible=False),
                margin=dict(t=30, b=10, l=10, r=50),
                height=400,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_r, use_container_width=True)

            st.divider()
            selected_sec = st.selectbox(
                "點選類股查看相關新聞",
                rank_df["類股"].tolist(), key="sec_select",
            )
            if selected_sec:
                db = SessionLocal()
                try:
                    sec_news = get_articles_df(db, sector=selected_sec, limit=50)
                finally:
                    db.close()
                st.caption(f"{selected_sec}：共 {len(sec_news)} 則新聞")
                render_news_list(sec_news, key="sec_news")

    # ────────────────────────────────────────────────────────────────────
    # 模式：個股聚焦
    # ────────────────────────────────────────────────────────────────────
    elif mode == "🔍 個股聚焦":

        ticker_q = st.text_input(
            "輸入股票代碼或公司名稱",
            placeholder="台積電 / 2330 / 聯發科 / NVDA / 輝達…",
            key="ticker_q",
        )

        if not ticker_q:
            st.markdown("""
            <div style="background:#161B22;border:1px solid #2D333B;border-radius:10px;padding:16px 20px;color:#8B949E;font-size:13px;line-height:2">
              <strong style="color:#C9D1D9">支援格式</strong><br>
              台股代碼：<code style="background:#21262D;padding:1px 6px;border-radius:4px">2330</code>（台積電）、<code style="background:#21262D;padding:1px 6px;border-radius:4px">2454</code>（聯發科）<br>
              台股公司名稱：<code style="background:#21262D;padding:1px 6px;border-radius:4px">台積電</code>、<code style="background:#21262D;padding:1px 6px;border-radius:4px">廣達</code>、<code style="background:#21262D;padding:1px 6px;border-radius:4px">鴻海</code><br>
              美股代碼：<code style="background:#21262D;padding:1px 6px;border-radius:4px">NVDA</code>、<code style="background:#21262D;padding:1px 6px;border-radius:4px">TSLA</code><br>
              美股中文：<code style="background:#21262D;padding:1px 6px;border-radius:4px">輝達</code>、<code style="background:#21262D;padding:1px 6px;border-radius:4px">特斯拉</code>
            </div>
            """, unsafe_allow_html=True)
        else:
            q = ticker_q.strip()
            from analyzer import TW_COMPANY_TO_CODE, US_NAME_TO_CODE
            code_q = TW_COMPANY_TO_CODE.get(q) or US_NAME_TO_CODE.get(q) or q.upper()
            db = SessionLocal()
            try:
                sdf = get_articles_df(db, ticker=code_q, limit=150)
                if sdf.empty:
                    sdf = get_articles_df(db, keyword=q, limit=150)
            finally:
                db.close()

            if sdf.empty:
                st.warning(f"找不到 **{q}** 的相關新聞，請先抓取或確認輸入正確。")
            else:
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("總計", len(sdf))
                s2.metric("📈 利多", len(sdf[sdf["sentiment"] == "bullish"]))
                s3.metric("📉 利空", len(sdf[sdf["sentiment"] == "bearish"]))
                ai_cnt = len(sdf[sdf["ai_summary"] != ""])
                s4.metric("✦ AI 分析", ai_cnt)

                csv_s = sdf[[
                    "title", "sentiment_label", "sentiment_score",
                    "ai_sentiment", "ai_score", "ai_summary",
                    "tickers", "sectors", "source", "published_at", "url",
                ]].to_csv(index=False, encoding="utf-8-sig")
                st.download_button(
                    "⬇ 匯出 CSV", csv_s,
                    file_name=f"finnews_{code_q}.csv", mime="text/csv",
                    key="stock_csv",
                )
                st.divider()
                render_news_list(sdf, key="stock")

    # ────────────────────────────────────────────────────────────────────
    # 全部新聞列表（在深度篩選最下方加一個區塊）
    # ────────────────────────────────────────────────────────────────────
    if mode == "✦ AI 深度分析":
        pass  # 已在上面處理
    elif mode not in ("🔍 個股聚焦",):
        # 其他模式底部不重複，個股聚焦自己有列表
        pass

    # 全部新聞列表作為獨立 expander
    with st.expander("📋 全部新聞列表（展開）", expanded=False):
        @st.cache_data(ttl=60, show_spinner=False)
        def load_news_all():
            db = SessionLocal()
            try:
                return get_articles_df(db, limit=500)
            finally:
                db.close()

        ndf = load_news_all()

        nf1, nf2, nf3, nf4, nf5 = st.columns([1, 1, 1, 2, 1])
        with nf1:
            nsent = st.selectbox("情緒", ["全部", "利多", "利空", "中性"], key="n_sent")
        with nf2:
            cats_list = sorted(ndf["category"].unique().tolist()) if not ndf.empty else []
            ncat = st.selectbox("分類", ["全部"] + cats_list, key="n_cat")
        with nf3:
            nsrc_list = sorted(ndf["source"].unique().tolist()) if not ndf.empty else []
            nsrc = st.selectbox("來源", ["全部"] + nsrc_list, key="n_src")
        with nf4:
            nkw = st.text_input("🔍 搜尋", placeholder="標題或摘要…", key="n_kw")
        with nf5:
            nsort = st.selectbox("排序", ["最新優先", "強度↓", "重要性↓"], key="n_sort")

        hide_neutral = st.checkbox("隱藏中性新聞", value=True, key="n_hide_neutral")

        fdf = ndf.copy() if not ndf.empty else pd.DataFrame()
        if not fdf.empty:
            if hide_neutral:
                fdf = fdf[fdf["sentiment"] != "neutral"]
            sm = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
            if nsent != "全部":
                fdf = fdf[fdf["sentiment"] == sm[nsent]]
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
            elif nsort == "重要性↓" and "importance_score" in fdf.columns:
                fdf = fdf.sort_values("importance_score", ascending=False)

        col_cap2, col_btn2 = st.columns([3, 1])
        with col_cap2:
            st.caption(f"顯示 {len(fdf)} / {len(ndf)} 則")
        with col_btn2:
            if not fdf.empty:
                csv = fdf[[
                    "title", "sentiment_label", "sentiment_score",
                    "ai_sentiment", "ai_score", "ai_summary",
                    "tickers", "sectors", "source", "category",
                    "published_at", "url",
                ]].to_csv(index=False, encoding="utf-8-sig")
                st.download_button(
                    "⬇ 匯出 CSV", csv,
                    file_name="finnews_export.csv", mime="text/csv",
                    key="csv_btn",
                )
        render_news_list(fdf, key="news_all")


# ════════════════════════════════════════════════════════════════════════════
# TAB 3：設定
# ════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.markdown("### ⚙️ 系統設定")

    set1, set2, set3 = st.tabs(["📡 來源 / 頻率", "📝 情緒詞典", "📜 執行日誌"])

    with set1:
        st.markdown("#### ⏱ 抓取頻率")
        new_interval = st.select_slider(
            "每隔幾分鐘自動抓取",
            options=[15, 30, 60],
            value=st.session_state["interval"],
        )
        if st.button("套用", key="apply_interval"):
            st.session_state["interval"] = new_interval
            update_interval(new_interval)
            st.success(f"已更新：每 {new_interval} 分鐘")

        st.divider()
        st.markdown("#### 📡 新聞來源開關")
        enabled = []
        for src in SOURCES:
            checked = st.checkbox(
                f"**{src['name']}**　`{src['category']}`",
                value=(src["name"] in st.session_state["enabled_srcs"]),
                key=f"src_{src['name']}",
            )
            if checked:
                enabled.append(src["name"])
        if st.button("💾 儲存", type="primary", key="save_srcs"):
            st.session_state["enabled_srcs"] = enabled
            st.success(f"已儲存，啟用 {len(enabled)} 個來源")

    with set2:
        st.markdown("#### 📝 自訂情緒詞彙")
        wc1, wc2, wc3 = st.columns([2, 1, 1])
        with wc1:
            new_word = st.text_input("詞彙", placeholder="如：大客戶加單", key="nw")
        with wc2:
            new_score = st.number_input("分數（正=利多 負=利空）",
                                        -1.0, 1.0, 0.7, 0.1, key="ns")
        with wc3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("新增", key="add_word"):
                if new_word:
                    if new_score > 0:
                        st.session_state["custom_bull"][new_word] = new_score
                    else:
                        st.session_state["custom_bear"][new_word] = new_score
                    st.success(f"已新增：{new_word} ({new_score:+.1f})")

        all_custom = {
            **{f"{w} (+{s:.1f})": "利多"
               for w, s in st.session_state["custom_bull"].items()},
            **{f"{w} ({s:.1f})": "利空"
               for w, s in st.session_state["custom_bear"].items()},
        }
        if all_custom:
            html_words = " ".join(
                f'<span style="background:#21262D;padding:3px 12px;'
                f'border-radius:12px;font-size:12px;border:1px solid #2D333B;'
                f'color:{"#E85454" if lbl=="利多" else "#3FB950"}">'
                f'{word} {lbl}</span>'
                for word, lbl in all_custom.items()
            )
            st.markdown(html_words, unsafe_allow_html=True)
        else:
            st.caption("尚無自訂詞彙")

    with set3:
        st.markdown("#### 📜 最近抓取日誌（台灣時間）")
        db = SessionLocal()
        log_df = get_crawl_logs(db)
        db.close()
        if log_df.empty:
            st.info("尚無日誌")
        else:
            log_rows = []
            for _, row in log_df.iterrows():
                status = row["狀態"]
                bg_map = {
                    "success": "rgba(63,185,80,0.1)",
                    "error":   "rgba(232,84,84,0.1)",
                    "empty":   "rgba(240,165,0,0.1)",
                }
                color_map = {
                    "success": "#3FB950",
                    "error":   "#E85454",
                    "empty":   "#F0A500",
                }
                bg    = bg_map.get(status, "#21262D")
                color = color_map.get(status, "#8B949E")
                log_rows.append(f"""
                <tr style="border-bottom:1px solid #21262D">
                  <td style="padding:8px 14px;color:#C9D1D9">{row["來源"]}</td>
                  <td style="padding:8px 14px">
                    <span style="background:{bg};padding:2px 8px;border-radius:6px;
                      font-size:11px;color:{color};font-weight:600">{status}</span>
                  </td>
                  <td style="padding:8px 14px;color:#8B949E">{row["抓取"]}</td>
                  <td style="padding:8px 14px;color:#3FB950;font-weight:600">{row["新增"]}</td>
                  <td style="padding:8px 14px;color:#444C56">{row["跳過"]}</td>
                  <td style="padding:8px 14px;font-size:11px;color:#8B949E;
                    font-family:'JetBrains Mono',monospace">{row["時間(台灣)"]}</td>
                </tr>""")
            st.markdown(f"""
            <div style="overflow-x:auto;border:1px solid #2D333B;border-radius:10px;background:#161B22">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              <thead>
                <tr style="background:#0D1117;border-bottom:1px solid #2D333B">
                  <th style="padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:#8B949E;letter-spacing:0.8px">來源</th>
                  <th style="padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:#8B949E;letter-spacing:0.8px">狀態</th>
                  <th style="padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:#8B949E;letter-spacing:0.8px">抓取</th>
                  <th style="padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:#3FB950;letter-spacing:0.8px">新增</th>
                  <th style="padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:#444C56;letter-spacing:0.8px">跳過</th>
                  <th style="padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:#8B949E;letter-spacing:0.8px">時間(台灣)</th>
                </tr>
              </thead>
              <tbody>{"".join(log_rows)}</tbody>
            </table></div>
            """, unsafe_allow_html=True)
