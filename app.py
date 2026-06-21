"""
app.py - FinNews AI v2.3
🎨 UI 優化版 - 護眼配色 · 增強微交互 · 視覺舒適度提升
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


def relative_time(dt) -> str:
    """將 datetime 轉成「3分鐘前」、「2小時前」等相對時間字串（台灣時區）"""
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
            return f"{int(secs // 60)}分鐘前"
        elif secs < 86400:
            return f"{int(secs // 3600)}小時前"
        elif secs < 86400 * 2:
            return "昨天"
        else:
            try:
                return dt.astimezone(TZ_TW).strftime("%m/%d %H:%M")
            except Exception:
                return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ""


def filter_12h(df):
    """只保留 12 小時內新聞（無時區資訊視為舊資料排除，不 fallback 舊新聞）"""
    if df is None or df.empty:
        return df
    cutoff = datetime.now(TZ_TW) - timedelta(hours=12)
    mask = df["published_at"].apply(
        lambda x: x is not None and (
            x.astimezone(TZ_TW) >= cutoff if getattr(x, "tzinfo", None) else False
        )
    )
    return df[mask]  # 空就回空，不 fallback 舊新聞


# ─────────────────────────────────────────────
# AI 市場總結
# ─────────────────────────────────────────────
def get_daily_ai_summary(ai_news_df):
    """
    生成今日 AI 市場總結。
    - 接入 groq_analyzer 共用熔斷器，避免與文章分析搶 quota
    - 最多 retry 2 次（429 冷卻後再試）
    - 詳細錯誤訊息供 debug
    """
    import time
    import requests
    from groq_analyzer import _is_rate_limited, _set_rate_limited, COOLDOWN_SECONDS

    groq_key = ""
    try:
        groq_key = st.secrets.get("GROQ_API_KEY", "")
    except Exception:
        pass
    if not groq_key:
        groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return "", "找不到 GROQ_API_KEY"

    # ── 熔斷器：文章分析若已觸發 429，總結直接等冷卻後才試 ──────────────
    if _is_rate_limited():
        remaining = int(max(0, _rate_limit_until_seconds() - time.time()))
        return "", f"Groq 熔斷冷卻中（約 {remaining} 秒後恢復），請稍後點「重新生成」"

    lines = []
    for _, r in ai_news_df.head(15).iterrows():
        label = {"bullish": "利多", "bearish": "利空"}.get(r.get("ai_sentiment", ""), "中性")
        text = r.get("ai_summary") or r.get("title", "")
        lines.append(f"[{label}] {text}")
    news_text = "\n".join(lines)
    if not news_text.strip():
        return "", "沒有可用 AI 新聞素材（請先抓取並啟用 AI 分析）"

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

    # ── 最多 retry 2 次（第一次 429 → 等 5 秒 → retry；第二次再失敗才報錯）──
    for attempt in range(2):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}",
                         "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 600, "temperature": 0.2},
                timeout=20,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip()), ""

        except requests.exceptions.Timeout:
            return "", "Groq API 逾時（20s），請稍後重試"

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                _set_rate_limited()   # 同步熔斷器，讓文章分析也知道
                if attempt == 0:
                    # 第一次 429 → 等 5 秒再 retry 一次
                    time.sleep(5)
                    continue
                remaining = COOLDOWN_SECONDS
                return "", f"Groq 速率限制（429）｜已觸發熔斷冷卻 {remaining}s，請等候後點「重新生成」"
            return "", f"Groq HTTP {status} 錯誤：{e}"

        except json.JSONDecodeError as e:
            return "", f"AI 回傳 JSON 解析失敗：{e}"

        except Exception as e:
            return "", f"生成失敗：{type(e).__name__}: {e}"

    return "", "多次重試後仍失敗，請稍後再試"


def _rate_limit_until_seconds():
    """讀取 groq_analyzer 的熔斷截止時間戳（秒）"""
    try:
        from groq_analyzer import _rate_limit_until
        return _rate_limit_until
    except Exception:
        return 0


# ─────────────────────────────────────────────
# 頁面設定
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="FinNews AI",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ════════════════════════════════════════════════════════════════
# 🎨 v2.3 UI 優化版 CSS - 護眼配色 · 微交互增強 · 視覺舒適度提升
# ════════════════════════════════════════════════════════════════
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

/* ══════════════════════════════════════
   🎨 CSS 變數系統（v2.3 護眼優化版）
   - 背景色調整為暖白色，減少眩光
   - 增加柔和陰影系統
   - 統一動畫時間常數
══════════════════════════════════════ */
:root {
  /* 🌟 核心改進：護眼背景色 */
  --color-background-primary: #FDFCF8;      /* ← 從 #FFFFFF 改為暖白 */
  --color-background-secondary: #F5F3EE;
  --color-background-tertiary: #FAF9F6;     /* ← 更柔和的底色 */
  --color-text-primary: #2D3436;             /* ← 柔和黑（非純黑）*/
  --color-text-secondary: #636E72;
  --color-text-tertiary: #95A5A6;
  --color-border-primary: #DFE6E9;
  --color-border-secondary: #E8ECF0;
  --color-border-tertiary: #F0F0F0;
  
  /* 功能色 */
  --color-text-info: #378ADD;
  --color-background-info: #EBF4FF;
  --color-text-warning: #92400E;
  --color-background-warning: #FFFBEB;
  --color-border-warning: #FDE68A;
  
  /* 語意色：利多／利空 */
  --color-bull-text: #C0392B;               /* ← 提高對比度 */
  --color-bull-bg: #FDEDEC;
  --color-bear-text: #16A085;               /* ← 提高對比度 */
  --color-bear-bg: #E8F8F5;
  --color-accent: #6C5CE7;                  /* ← 改為紫藍色（更現代）*/
  
  /* ✨ 新增：統一陰影系統 */
  --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.08);
  --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.12);
  --shadow-hover: 0 12px 40px rgba(108, 92, 231, 0.15);
  
  /* ⚡ 新增：統一動畫 */
  --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-base: 300ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-slow: 500ms cubic-bezier(0.4, 0, 0.2, 1);
  
  /* 🔵 新增：焦點環樣式（無障礙）*/
  --focus-ring: 0 0 0 3px rgba(108, 92, 231, 0.3);
}

@media (prefers-color-scheme: dark) {
  :root {
    --color-background-primary: #1A1D26;
    --color-background-secondary: #232733;
    --color-background-tertiary: #15171F;
    --color-text-primary: #EDEFF3;
    --color-text-secondary: #9AA3B2;
    --color-text-tertiary: #6B7280;
    --color-border-primary: #3A3F4B;
    --color-border-secondary: #2D313D;
    --color-border-tertiary: #2A2E38;
    --color-text-info: #6FB3F0;
    --color-background-info: #1C2A3A;
    --color-text-warning: #FBBF24;
    --color-background-warning: #2A2310;
    --color-border-warning: #4A3B14;
    --color-bull-text: #E57373;              /* ← 深色模式提高亮度 */
    --color-bull-bg: #2E1E1E;
    --color-bear-text: #81C784;              /* ← 深色模式提高亮度 */
    --color-bear-bg: #163029;
    --color-accent: #A29BFE;
    
    /* 深色模式陰影更明顯 */
    --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.3);
    --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.4);
    --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.5);
    --shadow-hover: 0 12px 40px rgba(162, 155, 254, 0.25);
    
    --focus-ring: 0 0 0 3px rgba(162, 155, 254, 0.4);
  }
}

html, body, [class*="css"], .stApp {
  font-family: 'Noto Sans TC', sans-serif !important;
  background-color: var(--color-background-tertiary) !important;
  color: var(--color-text-primary) !important;
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
  padding-top: 10px !important;               /* ← 增加 top padding */
  padding-bottom: 24px !important;            /* ← 增加 bottom padding */
  max-width: 1400px !important;
  margin-top: 0 !important;
}
.appview-container { padding-top: 0 !important; }
.appview-container .main { padding-top: 0 !important; }

/* ── 緊湊 topbar：去框，改底部分隔線 + 增強視覺效果 ── */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: transparent;
  border-bottom: 1px solid var(--color-border-tertiary);  /* ← 加粗邊框 */
  padding: 12px 4px;                           /* ← 增加 padding */
  margin-bottom: 8px;                          /* ← 增加 margin */
  transition: all var(--transition-base);     /* ← 新增過渡動畫 */
}
.topbar:hover {
  background: var(--color-background-secondary);  /* ← hover 效果 */
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;                                    /* ← 增加間距 */
  min-width: 0;
}
.topbar-logo {
  font-size: 15px;                             /* ← 稍微加大 */
  font-weight: 600;                            /* ← 加粗 */
  color: var(--color-text-primary);
  white-space: nowrap;
  flex-shrink: 0;
  transition: color var(--transition-fast);    /* ← 新增 */
}
.topbar-logo:hover {
  color: var(--color-accent);                  /* ← hover 變色 */
}
.topbar-status-ok {
  font-size: 12px; 
  font-weight: 500; 
  color: var(--color-bear-text);
  background: var(--color-bear-bg);
  border-radius: 20px; 
  padding: 3px 10px;                          /* ← 增加 padding */
  white-space: nowrap; 
  flex-shrink: 0;
  transition: all var(--transition-fast);     /* ← 新增 */
  box-shadow: var(--shadow-sm);                /* ← 新增陰影 */
}
.topbar-status-ok:hover {
  transform: translateY(-1px);                 /* ← hover 上浮 */
  box-shadow: var(--shadow-md);
}
.topbar-status-warn {
  font-size: 12px; 
  font-weight: 500; 
  color: var(--color-text-warning);
  background: var(--color-background-warning);
  border-radius: 20px; 
  padding: 3px 10px;
  white-space: nowrap; 
  flex-shrink: 0;
  transition: all var(--transition-fast);
  box-shadow: var(--shadow-sm);
}
.topbar-status-warn:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}
.topbar-time {
  font-size: 12px; 
  color: var(--color-text-secondary);
  font-family: 'JetBrains Mono', monospace;
  white-space: nowrap; 
  overflow: hidden; 
  text-overflow: ellipsis;
  opacity: 0.8;                                /* ← 降低透明度 */
  transition: opacity var(--transition-fast);
}
.topbar-time:hover {
  opacity: 1;                                  /* ← hover 完全顯示 */
}

/* ── 抓取按鈕列：固定高度，不讓按鈕撐高整行 ── */
.fetch-row {
  display: flex;
  align-items: center;
  gap: 14px;                                   /* ← 增加間距 */
  margin-bottom: 10px;                         /* ← 增加 margin */
  padding: 8px 0;                              /* ← 新增 padding */
}
/* topbar 同列的 checkbox/button 垂直對齊 */
div[data-testid="column"]:has(.stCheckbox) { padding-top: 16px !important; }
div[data-testid="column"]:has(.stButton) { padding-top: 6px !important; }

/* ── Tabs：下底線式，去掉膠囊背景 + 增強互動 ── */
.stTabs [data-baseweb="tab-list"] {
  background: transparent;
  border-radius: 0;
  padding: 0;
  gap: 6px;                                    /* ← 增加間距 */
  border-bottom: 1px solid var(--color-border-tertiary);
  margin-bottom: 16px !important;               /* ← 增加 margin */
}
.stTabs [data-baseweb="tab"] {
  font-size: 14px; 
  font-weight: 500;
  padding: 10px 18px;                          /* ← 增加 padding */
  border-radius: 0;
  color: var(--color-text-secondary);
  border-bottom: 2px solid transparent;
  transition: all var(--transition-base);      /* ← 新增過渡 */
  position: relative;                           /* ← 新增 */
}
.stTabs [data-baseweb="tab"]:hover {
  color: var(--color-text-primary);            /* ← hover 變色 */
  background: rgba(108, 92, 231, 0.04);       /* ← hover 背景 */
}
.stTabs [aria-selected="true"] {
  background: transparent !important;
  color: var(--color-accent) !important;
  border-bottom: 2px solid var(--color-accent) !important;
  font-weight: 600;                            /* ← 選中加粗 */
}
.stTabs [data-baseweb="tab-panel"] {
  padding-top: 8px !important;                 /* ← 增加 padding */
}
.stTabs [data-baseweb="tab-highlight"] { 
  display: none !important; 
  transition: all var(--transition-base);      /* ← 新增 */
}

/* ── Metrics：去框，填色背景 + 增強卡片效果 ── */
[data-testid="metric-container"] {
  background: var(--color-background-secondary);
  border: none;
  border-radius: 12px;                         /* ← 增加圓角 */
  padding: 14px 18px;                          /* ← 增加 padding */
  box-shadow: var(--shadow-sm);                /* ← 新增陰影 */
  transition: all var(--transition-base);      /* ← 新增過渡 */
}
[data-testid="metric-container"]:hover {
  box-shadow: var(--shadow-md);                /* ← hover 陰影增強 */
  transform: translateY(-2px);                 /* ← hover 上浮 */
}
[data-testid="stMetricLabel"] { 
  color: var(--color-text-secondary) !important; 
  font-size: 11px !important; 
  font-weight: 500 !important; 
  margin-bottom: 4px !important; 
}
[data-testid="stMetricValue"] { 
  color: var(--color-text-primary) !important; 
  font-size: 20px !important;                   /* ← 稍微加大 */
  font-weight: 600 !important;                  /* ← 加粗 */
  margin-bottom: 0 !important; 
  line-height: 1 !important; 
}
[data-testid="stMetricDelta"] { 
  font-size: 11px !important; 
  color: var(--color-text-secondary) !important; 
  display: inline !important; 
  margin: 0 !important; 
  margin-left: 4px !important; 
}
[data-testid="metric-container"] { 
  padding: 12px 16px !important; 
  gap: 4px !important; 
}
[data-testid="stMetric"] { 
  gap: 0px !important; 
  padding: 10px 16px !important; 
}

/* ── Buttons：增強微交互 ── */
.stButton > button {
  border-radius: 8px;                          /* ← 增加圓角 */
  font-weight: 500; 
  font-size: 14px;
  border: 1px solid var(--color-border-secondary);  /* ← 加粗邊框 */
  background: var(--color-background-primary); 
  color: var(--color-text-primary);
  transition: all var(--transition-base);      /* ← 延長過渡時間 */
  box-shadow: var(--shadow-sm);                /* ← 新增陰影 */
  height: 38px !important;                     /* ← 增加高度 */
  padding: 0 28px !important;
  white-space: nowrap !important;
  position: relative;                           /* ← 新增 */
  overflow: hidden;                            /* ← 新增 */
}
.stButton > button:hover { 
  background: var(--color-background-secondary); 
  border-color: var(--color-accent);           /* ← hover 邊框變色 */
  box-shadow: var(--shadow-md);                /* ← hover 陰影增強 */
  transform: translateY(-2px);                 /* ← hover 上浮效果 */
}
.stButton > button:active {
  transform: translateY(0);                    /* ← 點擊回彈 */
  box-shadow: var(--shadow-sm);
}
/* 🔵 無障礙：鍵盤焦點可見 */
.stButton > button:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.stButton > button[kind="primary"] {
  background: linear-gradient(135deg, var(--color-accent), #5641D4);  /* ← 漸變背景 */
  border-color: transparent; 
  color: #FFFFFF;
  box-shadow: var(--shadow-md), 0 4px 12px rgba(108, 92, 231, 0.25);  /* ← 發光效果 */
}
.stButton > button[kind="primary"]:hover { 
  opacity: 0.92; 
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg), 0 8px 20px rgba(108, 92, 231, 0.35);  /* ← 增強發光 */
}

/* ── Checkbox 更緊湊 + 增強交互 ── */
.stCheckbox { margin-bottom: 0 !important; }
.stCheckbox label { 
  color: var(--color-text-primary) !important; 
  font-size: 14px !important;
  transition: color var(--transition-fast);
}
.stCheckbox label:hover {
  color: var(--color-accent) !important;
}

/* ── Selectbox / Input：增強焦點狀態 ── */
.stSelectbox > div > div,
.stTextInput > div > div > input {
  background: var(--color-background-primary) !important;
  border: 1.5px solid var(--color-border-secondary) !important;  /* ← 加粗邊框 */
  border-radius: 8px !important;               /* ← 增加圓角 */
  color: var(--color-text-primary) !important;
  font-size: 14px !important;
  transition: all var(--transition-fast) !important;  /* ← 新增過渡 */
  box-shadow: none !important;
}
.stSelectbox > div > div:focus-within,
.stTextInput > div > div > input:focus {
  border-color: var(--color-accent) !important;  /* ← 焦點變色 */
  box-shadow: var(--focus-ring) !important;     /* ← 焦點環 */
}
.stSelectbox label, .stTextInput label { 
  color: var(--color-text-secondary) !important; 
  font-size: 12px !important; 
  font-weight: 500 !important; 
}

/* ── 篩選列：縮小 padding ── */
div[data-testid="column"] { 
  padding-left: 6px !important; 
  padding-right: 6px !important; 
}

/* ── Radio ── */
.stRadio label { 
  color: var(--color-text-primary) !important; 
  font-size: 14px !important; 
  font-weight: 500 !important;
  transition: color var(--transition-fast);
}
.stRadio label:hover {
  color: var(--color-accent) !important;
}
.stRadio > div { gap: 8px !important; }          /* ← 增加間距 */

/* ── Divider ── */
hr { 
  border: none;                                 /* ← 移除預設邊框 */
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--color-border-tertiary), transparent);  /* ← 漸變線 */
  margin: 14px 0 !important; 
}
.stCaption { 
  color: var(--color-text-tertiary) !important; 
  font-size: 12px !important; 
}

/* ── Section Header：增強視覺效果 ── */
.sec-hd {
  font-size: 13px;                             /* ← 稍微加大 */
  font-weight: 600;                            /* ← 加粗 */
  color: var(--color-text-secondary);
  letter-spacing: 1.5px;                       /* ← 增加字距 */
  text-transform: uppercase;
  margin: 18px 0 10px;                         /* ← 增加 margin */
  display: flex; 
  align-items: center; 
  gap: 10px;                                   /* ← 增加間距 */
  position: relative;                           /* ← 新增 */
}
.sec-hd::before {                               /* ← 新增：裝飾圓點 */
  content: '';
  width: 6px;
  height: 6px;
  background: var(--color-accent);
  border-radius: 50%;
  display: inline-block;
}
.sec-hd::after { 
  content: ''; 
  flex: 1; 
  height: 1px;
  background: linear-gradient(90deg, var(--color-border-tertiary), transparent);  /* ← 漸變尾線 */
}


/* ══════════════════════════════════════
   🤖 AI 總結卡片（左側 accent，更輕量 + 增強效果）
══════════════════════════════════════ */
.ai-card {
  background: var(--color-background-primary);
  border: 1px solid var(--color-border-tertiary);  /* ← 加粗邊框 */
  border-left: 4px solid var(--color-accent);      /* ← 加粗 accent */
  border-radius: 0 12px 12px 0;                    /* ← 增加圓角 */
  padding: 18px 22px;                              /* ← 增加 padding */
  margin-bottom: 10px;                             /* ← 增加 margin */
  box-shadow: var(--shadow-sm);                    /* ← 新增陰影 */
  transition: all var(--transition-base);          /* ← 新增過渡 */
  position: relative;                              /* ← 新增 */
  overflow: hidden;                                /* ← 新增 */
}
.ai-card:hover {
  box-shadow: var(--shadow-md);                   /* ← hover 陰影增強 */
  transform: translateX(4px);                     /* ← hover 右移 */
  border-left-width: 5px;                         /* ← accent 加寬 */
}
.ai-badge {
  font-size: 11px; 
  font-weight: 600;                               /* ← 加粗 */
  letter-spacing: 0.8px;                          /* ← 增加字距 */
  color: var(--color-text-warning); 
  background: var(--color-background-warning);
  border-radius: 6px;                             /* ← 增加圓角 */
  padding: 3px 10px;                              /* ← 增加 padding */
  text-transform: uppercase;
  display: inline-block; 
  margin-bottom: 10px;
  transition: all var(--transition-fast);
}
.ai-badge:hover {
  transform: scale(1.05);                         /* ← hover 放大 */
}
.ai-dir-bull { 
  font-size: 15px;                               /* ← 加大 */
  font-weight: 600; 
  color: var(--color-bull-text); 
  transition: color var(--transition-fast);
}
.ai-dir-bear { 
  font-size: 15px; 
  font-weight: 600; 
  color: var(--color-bear-text); 
  transition: color var(--transition-fast);
}
.ai-dir-neu  { 
  font-size: 15px; 
  font-weight: 600; 
  color: var(--color-text-secondary); 
}
.ai-dir-reason { 
  font-size: 13px;                               /* ← 加大 */
  color: var(--color-text-secondary); 
  margin: 4px 0 12px;                            /* ← 增加 spacing */
  line-height: 1.6;
}
.ai-themes { 
  display: flex; 
  gap: 8px;                                      /* ← 增加間距 */
  flex-wrap: wrap; 
  margin-bottom: 12px;                           /* ← 增加 margin */
}
.ai-tag-bull {
  background: var(--color-bull-bg); 
  border-radius: 6px;                            /* ← 增加圓角 */
  padding: 4px 12px;                             /* ← 增加 padding */
  font-size: 12px; 
  color: var(--color-bull-text); 
  font-weight: 500;
  transition: all var(--transition-fast);         /* ← 新增 */
  cursor: default;
}
.ai-tag-bull:hover {
  transform: translateY(-2px);                   /* ← hover 上浮 */
  box-shadow: var(--shadow-sm);
}
.ai-tag-bear {
  background: var(--color-bear-bg); 
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 12px; 
  color: var(--color-bear-text); 
  font-weight: 500;
  transition: all var(--transition-fast);
  cursor: default;
}
.ai-tag-bear:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-sm);
}
.ai-tickers { 
  display: flex; 
  gap: 6px; 
  flex-wrap: wrap; 
  margin-bottom: 12px; 
}
.ai-tick-chip {
  background: var(--color-background-secondary); 
  border-radius: 6px;                            /* ← 增加圓角 */
  padding: 3px 10px;                             /* ← 增加 padding */
  font-size: 12px; 
  color: var(--color-text-primary);
  font-family: 'JetBrains Mono', monospace; 
  font-weight: 500;
  transition: all var(--transition-fast);
}
.ai-tick-chip:hover {
  background: var(--color-accent);
  color: white;
  transform: scale(1.05);
}
.ai-body {
  font-size: 14px; 
  line-height: 1.9;                              /* ← 增加行高（更易讀）*/
  color: var(--color-text-primary);
  border-top: 1px solid var(--color-border-tertiary);  /* ← 加粗分隔線 */
  padding-top: 12px;                             /* ← 增加 padding */
}
.ai-footer { 
  font-size: 12px; 
  color: var(--color-text-tertiary); 
  margin-top: 10px; 
  opacity: 0.7;                                  /* ← 降低透明度 */
  transition: opacity var(--transition-fast);
}
.ai-card:hover .ai-footer {
  opacity: 1;                                    /* ← hover 顯示 */
}


/* ══════════════════════════════════════
   ⚠️ GEO 警示（雙欄 grid，省空間多顯示 + 增強）
══════════════════════════════════════ */
.geo-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;                                /* ← 增加間距 */
  margin-bottom: 10px;
}
@media (max-width: 640px) {
  .geo-grid { grid-template-columns: 1fr; }
}
.geo-card {
  background: var(--color-background-warning);
  border: 1px solid var(--color-border-warning);
  border-left: 3px solid var(--color-text-warning); 
  border-radius: 10px;                           /* ← 增加圓角 */
  padding: 12px 16px;                            /* ← 增加 padding */
  display: flex; 
  gap: 10px;                                     /* ← 增加間距 */
  align-items: flex-start;
  min-width: 0;
  transition: all var(--transition-base);         /* ← 新增過渡 */
  box-shadow: var(--shadow-sm);                  /* ← 新增陰影 */
}
.geo-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);                   /* ← hover 上浮 */
  border-left-width: 4px;                        /* ← accent 加寬 */
}
.geo-icon { 
  font-size: 16px;                               /* ← 加大 */
  flex-shrink: 0; 
  margin-top: 2px; 
}
.geo-title {
  font-size: 14px;                               /* ← 加大 */
  font-weight: 600;                              /* ← 加粗 */
  color: var(--color-text-warning); 
  margin-bottom: 4px;
  line-height: 1.5;
  display: -webkit-box; 
  -webkit-line-clamp: 2; 
  -webkit-box-orient: vertical; 
  overflow: hidden;
  transition: color var(--transition-fast);
}
.geo-title a { 
  color: var(--color-text-warning); 
  text-decoration: none;
  transition: opacity var(--transition-fast);
}
.geo-title a:hover { 
  opacity: 0.8;
  text-decoration: underline;
}
.geo-meta { 
  font-size: 12px; 
  color: var(--color-text-warning); 
  font-weight: 500; 
}
.geo-body { 
  font-size: 13px;                               /* ← 加大 */
  color: var(--color-text-warning); 
  line-height: 1.6;                              /* ← 增加行高 */
}


/* ══════════════════════════════════════
   📰 新聞列表（去框，分隔線取代卡片 + 增強交互）
══════════════════════════════════════ */
.nw {
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--color-border-tertiary);  /* ← 加粗分隔線 */
  border-radius: 0; 
  padding: 14px 4px;                             /* ← 增加 padding */
  margin-bottom: 0;
  box-shadow: none;
  transition: all var(--transition-base);         /* ← 新增過渡 */
  position: relative;                             /* ← 新增 */
}
.nw:hover { 
  background: var(--color-background-secondary); 
  padding-left: 12px;                            /* ← hover 左縮進 */
  border-left: 3px solid var(--color-accent);    /* ← hover 左側 accent */
}
.nw:last-child { border-bottom: none; }
.nw.bull, .nw.bear, .nw.geo { border-left: none; }
.nw.bull:hover { border-left-color: var(--color-bull-text); }
.nw.bear:hover { border-left-color: var(--color-bear-text); }
.nw.geo:hover { border-left-color: var(--color-text-warning); }
.nw-title {
  font-size: 15px;                               /* ← 加大 */
  font-weight: 500; 
  color: var(--color-text-primary);
  line-height: 1.6;                              /* ← 增加行高 */
  margin-bottom: 6px;
  transition: all var(--transition-fast);
}
.nw-title a { 
  color: var(--color-text-primary); 
  text-decoration: none;
  transition: color var(--transition-fast);
  position: relative;                             /* ← 新增 */
}
.nw-title a::after {                              /* ← 新增：下劃線動畫 */
  content: '';
  position: absolute;
  bottom: -2px;
  left: 0;
  width: 0;
  height: 1.5px;
  background: var(--color-accent);
  transition: width var(--transition-base);
}
.nw-title a:hover { 
  color: var(--color-accent);
}
.nw-title a:hover::after {
  width: 100%;
}
.nw-meta { 
  display: flex; 
  align-items: center; 
  gap: 8px;                                      /* ← 增加間距 */
  flex-wrap: wrap; 
}
.nw-score-bull {
  font-size: 12px; 
  font-weight: 600;                              /* ← 加粗 */
  color: var(--color-bull-text); 
  background: var(--color-bull-bg);
  border-radius: 5px;                            /* ← 增加圓角 */
  padding: 2px 8px;                              /* ← 增加 padding */
  font-family: 'JetBrains Mono', monospace;
  transition: all var(--transition-fast);
}
.nw-score-bull:hover {
  transform: scale(1.05);
  box-shadow: var(--shadow-sm);
}
.nw-score-bear {
  font-size: 12px; 
  font-weight: 600;
  color: var(--color-bear-text); 
  background: var(--color-bear-bg);
  border-radius: 5px;
  padding: 2px 8px;
  font-family: 'JetBrains Mono', monospace;
  transition: all var(--transition-fast);
}
.nw-score-bear:hover {
  transform: scale(1.05);
  box-shadow: var(--shadow-sm);
}
.nw-score-neu {
  font-size: 12px; 
  font-weight: 500;
  color: var(--color-text-tertiary); 
  background: var(--color-background-secondary);
  border-radius: 5px;
  padding: 2px 8px;
  font-family: 'JetBrains Mono', monospace;
}
.nw-badge-geo {
  font-size: 12px; 
  font-weight: 600;
  color: var(--color-text-warning); 
  background: var(--color-background-warning);
  border-radius: 5px;
  padding: 2px 8px;
  animation: pulse 2s infinite;                  /* ← 新增：脈衝動畫 */
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.nw-tick {
  font-size: 12px; 
  font-weight: 500; 
  color: var(--color-text-info); 
  background: var(--color-background-info);
  border-radius: 5px;
  padding: 2px 8px;
  font-family: 'JetBrains Mono', monospace;
  transition: all var(--transition-fast);
}
.nw-tick:hover {
  background: var(--color-accent);
  color: white;
  transform: scale(1.05);
}
.nw-src { 
  font-size: 12px; 
  color: var(--color-text-secondary);
  transition: color var(--transition-fast);
}
.nw-src:hover {
  color: var(--color-text-primary);
}
.nw-time { 
  font-size: 12px; 
  color: var(--color-text-tertiary); 
  font-family: 'JetBrains Mono', monospace;
}
.nw-ai-reason { 
  margin-top: 6px; 
  font-size: 13px;                               /* ← 加大 */
  color: var(--color-text-tertiary);
  line-height: 1.6;
  padding-left: 12px;
  border-left: 2px solid var(--color-border-secondary);
}


/* ══════════════════════════════════════
   📈 熱門股票卡片（增強懸停效果）
══════════════════════════════════════ */
.tk-card {
  background: var(--color-background-secondary); 
  border: none; 
  border-radius: 10px;                           /* ← 增加圓角 */
  padding: 12px;                                 /* ← 增加 padding */
  margin-bottom: 8px;                            /* ← 增加 margin */
  text-align: center;
  box-shadow: var(--shadow-sm);                 /* ← 新增陰影 */
  transition: all var(--transition-base);       /* ← 新增過渡 */
  cursor: pointer;
}
.tk-card:hover { 
  background: var(--color-background-primary);
  box-shadow: var(--shadow-md);
  transform: translateY(-4px) scale(1.02);      /* ← hover 上浮 + 放大 */
}
.tk-code { 
  font-size: 16px;                               /* ← 加大 */
  font-weight: 600;                              /* ← 加粗 */
  color: var(--color-text-primary); 
  font-family: 'JetBrains Mono', monospace;
  transition: color var(--transition-fast);
}
.tk-card:hover .tk-code {
  color: var(--color-accent);
}
.tk-name { 
  font-size: 12px; 
  color: var(--color-text-tertiary); 
  margin: 2px 0 6px; 
}
.tk-bull { 
  color: var(--color-bull-text); 
  font-size: 13px;                               /* ← 加大 */
  font-weight: 600; 
}
.tk-bear { 
  color: var(--color-bear-text); 
  font-size: 13px; 
  font-weight: 600; 
}
.tk-neu  { 
  color: var(--color-text-tertiary); 
  font-size: 13px; 
  font-weight: 500; 
}
.tk-cnt  { 
  font-size: 12px; 
  color: var(--color-text-tertiary); 
}


/* ══════════════════════════════════════
   📭 空狀態（增強視覺效果）
══════════════════════════════════════ */
.empty-box { 
  text-align: center; 
  padding: 48px 24px;                            /* ← 增加 padding */
  color: var(--color-text-tertiary); 
  animation: fadeInUp 0.5s ease-out;             /* ← 新增：淡入動畫 */
}
@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.empty-box-icon { 
  font-size: 36px;                               /* ← 加大 */
  margin-bottom: 12px;                           /* ← 增加 margin */
  animation: bounce 2s infinite;                 /* ← 新增：彈跳動畫 */
}
@keyframes bounce {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-10px); }
}
.empty-box-txt { 
  font-size: 15px;                               /* ← 加大 */
  line-height: 1.6;
}


/* ══════════════════════════════════════
   📜 日誌表格（增強可讀性）
══════════════════════════════════════ */
.log-table {
  width: 100%; 
  border-collapse: separate;                      /* ← 改為 separate 以支援圓角 */
  border-spacing: 0;                             /* ← 移除間隙 */
  font-size: 12px;
  background: var(--color-background-primary); 
  border: 1px solid var(--color-border-tertiary);
  border-radius: 10px;                           /* ← 增加圓角 */
  overflow: hidden;
  box-shadow: var(--shadow-sm);                  /* ← 新增陰影 */
}
.log-table th {
  padding: 10px 14px;                            /* ← 增加 padding */
  text-align: left; 
  font-size: 12px; 
  font-weight: 600;                              /* ← 加粗 */
  color: var(--color-text-secondary); 
  letter-spacing: 0.5px;
  background: var(--color-background-secondary);
  border-bottom: 2px solid var(--color-border-tertiary);  /* ← 加粗底部邊框 */
}
.log-table td { 
  padding: 9px 14px;                            /* ← 增加 padding */
  color: var(--color-text-primary); 
  border-bottom: 1px solid var(--color-border-tertiary);
  transition: background var(--transition-fast);
}
.log-table tr:hover td {
  background: var(--color-background-secondary);  /* ← hover 行背景 */
}
.log-ok   { 
  background: var(--color-bear-bg); 
  color: var(--color-bear-text); 
  font-size: 11px; 
  font-weight: 600; 
  padding: 3px 10px; 
  border-radius: 5px;
  transition: all var(--transition-fast);
}
.log-ok:hover {
  transform: scale(1.05);
}
.log-err  { 
  background: var(--color-bull-bg); 
  color: var(--color-bull-text); 
  font-size: 11px; 
  font-weight: 600; 
  padding: 3px 10px; 
  border-radius: 5px;
  transition: all var(--transition-fast);
}
.log-err:hover {
  transform: scale(1.05);
}
.log-warn { 
  background: var(--color-background-warning); 
  color: var(--color-text-warning); 
  font-size: 11px; 
  font-weight: 600; 
  padding: 3px 10px; 
  border-radius: 5px;
  transition: all var(--transition-fast);
}
.log-warn:hover {
  transform: scale(1.05);
}

/* ── 篩選列整體縮小間距 ── */
.filter-row .stSelectbox, .filter-row .stTextInput { margin-bottom: 0 !important; }


/* ══════════════════════════════════════
   🔥 置頂高分新聞橫幅（增強視覺效果）
══════════════════════════════════════ */
.nw-pinned-bull {
  background: linear-gradient(135deg, var(--color-bull-bg), #fff5f5);  /* ← 漸變背景 */
  border: 1px solid var(--color-bull-text);      /* ← 可見邊框 */
  border-left: 4px solid var(--color-bull-text);
  border-radius: 10px;                           /* ← 增加圓角 */
  padding: 14px 18px;                            /* ← 增加 padding */
  margin-bottom: 10px;
  box-shadow: var(--shadow-sm), 0 4px 12px rgba(192, 57, 43, 0.08);  /* ← 彩色陰影 */
  transition: all var(--transition-base);
  position: relative;
  overflow: hidden;
}
.nw-pinned-bull::before {                        /* ← 新增：裝飾光暈 */
  content: '';
  position: absolute;
  top: -50%;
  right: -50%;
  width: 200px;
  height: 200px;
  background: radial-gradient(circle, rgba(192, 57, 43, 0.06), transparent 70%);
  border-radius: 50%;
  pointer-events: none;
}
.nw-pinned-bull:hover {
  box-shadow: var(--shadow-md), 0 8px 20px rgba(192, 57, 43, 0.15);
  transform: translateX(4px);
}
.nw-pinned-bear {
  background: linear-gradient(135deg, var(--color-bear-bg), #f0fdf9);
  border: 1px solid var(--color-bear-text);
  border-left: 4px solid var(--color-bear-text);
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 10px;
  box-shadow: var(--shadow-sm), 0 4px 12px rgba(22, 160, 133, 0.08);
  transition: all var(--transition-base);
  position: relative;
  overflow: hidden;
}
.nw-pinned-bear::before {
  content: '';
  position: absolute;
  top: -50%;
  right: -50%;
  width: 200px;
  height: 200px;
  background: radial-gradient(circle, rgba(22, 160, 133, 0.06), transparent 70%);
  border-radius: 50%;
  pointer-events: none;
}
.nw-pinned-bear:hover {
  box-shadow: var(--shadow-md), 0 8px 20px rgba(22, 160, 133, 0.15);
  transform: translateX(4px);
}
.nw-pinned-label {
  font-size: 12px; 
  font-weight: 600;                              /* ← 加粗 */
  letter-spacing: 0.5px;
  margin-bottom: 8px; 
  display: inline-block;
  transition: all var(--transition-fast);
}
.nw-pinned-bull .nw-pinned-label { 
  color: var(--color-bull-text); 
  background: var(--color-background-primary); 
  padding: 3px 10px; 
  border-radius: 5px;
  box-shadow: var(--shadow-sm);
}
.nw-pinned-bear .nw-pinned-label { 
  color: var(--color-bear-text); 
  background: var(--color-background-primary); 
  padding: 3px 10px; 
  border-radius: 5px;
  box-shadow: var(--shadow-sm);
}
.nw-pinned-title {
  font-size: 15px;                               /* ← 加大 */
  font-weight: 600;                              /* ← 加粗 */
  line-height: 1.6;                              /* ← 增加行高 */
  margin-bottom: 8px;
  transition: all var(--transition-fast);
}
.nw-pinned-bull .nw-pinned-title a { 
  color: var(--color-bull-text); 
  text-decoration: none;
  transition: opacity var(--transition-fast);
}
.nw-pinned-bull .nw-pinned-title a:hover { 
  opacity: 0.8;
  text-decoration: underline;
}
.nw-pinned-bear .nw-pinned-title a { 
  color: var(--color-bear-text); 
  text-decoration: none;
  transition: opacity var(--transition-fast);
}
.nw-pinned-bear .nw-pinned-title a:hover { 
  opacity: 0.8;
  text-decoration: underline;
}
.nw-pinned-score-bull {
  font-size: 13px;                               /* ← 加大 */
  font-weight: 600;
  color: var(--color-bull-text);
  font-family: 'JetBrains Mono', monospace;
}
.nw-pinned-score-bear {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-bear-text);
  font-family: 'JetBrains Mono', monospace;
}


/* ══════════════════════════════════════
   👁️ 已讀灰化（增強過渡效果）
══════════════════════════════════════ */
.nw-title a.nw-read {
  color: var(--color-text-tertiary) !important;
  text-decoration: line-through;
  opacity: 0.6;
  transition: all var(--transition-base);
}
.nw.nw-read-card {
  opacity: 0.45;                                 /* ← 降低不透明度 */
  filter: grayscale(50%);                        /* ← 新增：灰階濾鏡 */
  transition: all var(--transition-base);
}


/* ══════════════════════════════════════
   🏷️ A 篩選快捷 Chip（增強互動）
══════════════════════════════════════ */
.chip-bar {
  display: flex; 
  gap: 8px;                                      /* ← 增加間距 */
  flex-wrap: wrap;
  margin-bottom: 12px;                           /* ← 增加 margin */
  align-items: center;
}
.chip {
  font-size: 13px; 
  font-weight: 500;
  padding: 6px 16px;                             /* ← 增加 padding */
  border-radius: 20px;
  border: 1.5px solid var(--color-border-secondary);  /* ← 加粗邊框 */
  background: var(--color-background-primary); 
  color: var(--color-text-secondary);
  cursor: pointer; 
  transition: all var(--transition-base);        /* ← 延長過渡時間 */
  user-select: none; 
  white-space: nowrap;
  position: relative;                             /* ← 新增 */
  overflow: hidden;                               /* ← 新增 */
}
.chip::before {                                   /* ← 新增：hover 光波效果 */
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 0;
  height: 0;
  border-radius: 50%;
  background: rgba(108, 92, 231, 0.1);
  transform: translate(-50%, -50%);
  transition: width 0.6s, height 0.6s;
}
.chip:hover::before {
  width: 300px;
  height: 300px;
}
.chip:hover { 
  background: var(--color-background-secondary); 
  border-color: var(--color-accent);
  color: var(--color-accent);
  transform: translateY(-2px);                   /* ← hover 上浮 */
  box-shadow: var(--shadow-sm);
}
.chip.chip-bull.active { 
  background: var(--color-bull-bg); 
  border-color: var(--color-bull-text); 
  color: var(--color-bull-text);
  font-weight: 600;
  box-shadow: var(--shadow-sm), 0 2px 8px rgba(192, 57, 43, 0.15);
  transform: scale(1.02);
}
.chip.chip-bear.active { 
  background: var(--color-bear-bg); 
  border-color: var(--color-bear-text); 
  color: var(--color-bear-text);
  font-weight: 600;
  box-shadow: var(--shadow-sm), 0 2px 8px rgba(22, 160, 133, 0.15);
  transform: scale(1.02);
}
.chip.chip-ai.active { 
  background: var(--color-background-warning); 
  border-color: var(--color-border-warning); 
  color: var(--color-text-warning);
  font-weight: 600;
  box-shadow: var(--shadow-sm);
  transform: scale(1.02);
}
.chip.chip-geo.active { 
  background: var(--color-background-warning); 
  border-color: var(--color-border-warning); 
  color: var(--color-text-warning);
  font-weight: 600;
  box-shadow: var(--shadow-sm);
  transform: scale(1.02);
}
.chip.chip-all.active { 
  background: var(--color-text-primary); 
  border-color: var(--color-text-primary); 
  color: var(--color-background-primary);
  font-weight: 600;
  box-shadow: var(--shadow-md);
  transform: scale(1.02);
}


/* ══════════════════════════════════════
   📝 B AI摘要：預設顯示前段，點擊展開全文
══════════════════════════════════════ */
.nw-ai-preview {
  margin-top: 6px; 
  font-size: 13px; 
  color: var(--color-text-secondary);
  line-height: 1.7;                              /* ← 增加行高 */
  cursor: pointer;
  padding: 8px 10px;                             /* ← 新增 padding */
  border-radius: 6px;
  border-left: 2px solid var(--color-border-secondary);
  transition: all var(--transition-fast);
}
.nw-ai-preview:hover { 
  color: var(--color-text-primary);
  background: var(--color-background-secondary);
  border-left-color: var(--color-accent);
  padding-left: 14px;                            /* ← hover 左縮進 */
}
.nw-ai-more { 
  color: var(--color-accent);                    /* ← 改為主色 */
  font-size: 12px; 
  margin-left: 4px;
  font-weight: 500;
  text-decoration: underline;
}
.nw-ai-full {
  display: none;
  margin-top: 6px; 
  padding: 12px 14px;                            /* ← 增加 padding */
  background: var(--color-background-secondary); 
  border-radius: 8px;                            /* ← 增加圓角 */
  border-left: 3px solid var(--color-accent);
  font-size: 13px; 
  color: var(--color-text-primary); 
  line-height: 1.8;                              /* ← 增加行高 */
  cursor: pointer;
  box-shadow: var(--shadow-sm);                  /* ← 新增陰影 */
  animation: slideDown 0.3s ease-out;            /* ← 新增：展開動畫 */
}
@keyframes slideDown {
  from {
    opacity: 0;
    transform: translateY(-10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.nw-ai-full.open { 
  display: block; 
}
.nw-ai-preview.hidden { 
  display: none; 
}


/* ══════════════════════════════════════
   🔄 全局載入動畫（新增功能）
══════════════════════════════════════ */
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes slideInLeft {
  from {
    opacity: 0;
    transform: translateX(-20px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

/* 平滑滾動（新增）*/
html {
  scroll-behavior: smooth;
}

/* 選取文字樣式（新增）*/
::selection {
  background: rgba(108, 92, 231, 0.2);
  color: var(--color-text-primary);
}

/* 滾動條美化（新增）*/
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-track {
  background: var(--color-background-secondary);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb {
  background: var(--color-border-primary);
  border-radius: 4px;
  transition: background var(--transition-fast);
}
::-webkit-scrollbar-thumb:hover {
  background: var(--color-text-tertiary);
}
</style>

<script>
/* ── 已讀標記：頁面載入時套用 ── */
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

/* ── 點擊新聞連結時標記已讀 ── */
document.addEventListener('click', function(e){
  var a = e.target.closest('.nw-title a');
  if(!a) return;
  try {
    var read = JSON.parse(localStorage.getItem('fn_read') || '{}');
    var key = a.href.split('?')[0];
    read[key] = Date.now();
    /* 只保留最近 500 筆 */
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

/* ── AI 摘要展開/收合：點擊預覽文字展開全文，點全文收合 ── */
document.addEventListener('click', function(e){
  var prev = e.target.closest('.nw-ai-preview');
  if(prev){
    var full = prev.nextElementSibling;
    if(full && full.classList.contains('nw-ai-full')){
      full.classList.add('open');
      prev.classList.add('hidden');
    }
    return;
  }
  var full2 = e.target.closest('.nw-ai-full');
  if(full2){
    full2.classList.remove('open');
    var prev2 = full2.previousElementSibling;
    if(prev2 && prev2.classList.contains('nw-ai-preview')) prev2.classList.remove('hidden');
  }
});

/* ── Chip 篩選（增強動畫效果）── */
function chipFilter(el, filter){
  document.querySelectorAll('.chip').forEach(function(c){ c.classList.remove('active'); });
  el.classList.add('active');
  var cards = document.querySelectorAll('.nw, .nw-pinned-bull, .nw-pinned-bear');
  cards.forEach(function(card, index){
    if(filter === 'all'){ 
      card.style.display=''; 
      card.style.animation = 'fadeIn 0.3s ease-out ' + (index * 0.03) + 's both';
      return; 
    }
    if(filter === 'bull'){
      var show = card.classList.contains('bull');
      card.style.display = show ? '' : 'none';
      if(show) card.style.animation = 'slideInLeft 0.3s ease-out ' + (index * 0.03) + 's both';
    } else if(filter === 'bear'){
      var show = card.classList.contains('bear');
      card.style.display = show ? '' : 'none';
      if(show) card.style.animation = 'slideInLeft 0.3s ease-out ' + (index * 0.03) + 's both';
    } else if(filter === 'ai'){
      var show = card.querySelector('.nw-ai-preview');
      card.style.display = show ? '' : 'none';
      if(show) card.style.animation = 'slideInLeft 0.3s ease-out ' + (index * 0.03) + 's both';
    } else if(filter === 'geo'){
      var show = card.classList.contains('geo');
      card.style.display = show ? '' : 'none';
      if(show) card.style.animation = 'slideInLeft 0.3s ease-out ' + (index * 0.03) + 's both';
    }
  });
}

/* ── 新增：鍵盤快捷鍵支援 ── */
document.addEventListener('keydown', function(e){
  // Ctrl+F 或 Cmd+F 聚焦搜尋框
  if((e.ctrlKey || e.metaKey) && e.key === 'f'){
    var searchInput = document.querySelector('input[placeholder*="搜尋"]');
    if(searchInput){
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
  }
});
</script>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 初始化
# ─────────────────────────────────────────────
if "initialized" not in st.session_state:
    init_db()
    start_scheduler(interval_minutes=60)
    # 多一層保護：secrets 讀取失敗時退而用環境變數
    groq_ok = False
    try:
        groq_ok = bool(st.secrets.get("GROQ_API_KEY", ""))
    except Exception:
        pass
    if not groq_ok:
        import os
        groq_ok = bool(os.environ.get("GROQ_API_KEY", ""))
    st.session_state.update({
        "initialized":  True,
        "last_update":  "尚未更新",
        "custom_bull":  {},
        "custom_bear":  {},
        "enabled_srcs": [s["name"] for s in SOURCES if s["enabled"]],
        "interval":     60,
        "groq_ok":      groq_ok,
        "use_ai":       groq_ok,   # groq_ok=True 時預設開啟
    })
# ── 每次 rerun 都重新確認 groq_ok（防 secrets 讀取延遲問題）──
if not st.session_state.get("groq_ok"):
    try:
        _recheck = bool(st.secrets.get("GROQ_API_KEY", ""))
    except Exception:
        import os
        _recheck = bool(os.environ.get("GROQ_API_KEY", ""))
    if _recheck:
        st.session_state["groq_ok"] = True
        st.session_state["use_ai"]  = True


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

    # ② 置頂高分新聞（AI分數 ≥7 或 ≤-7）
    pinned_chunks = []
    if "ai_score" in df.columns:
        pinned_df = df[df["ai_score"].abs() >= 7].head(3)
        for _, row in pinned_df.iterrows():
            sc = float(row.get("ai_score", 0) or 0)
            title = str(row.get("title", ""))
            url   = str(row.get("url", "") or "")
            ai_sum = str(row.get("ai_summary", "") or "")
            sent   = row.get("ai_sentiment", "") or row.get("sentiment", "neutral")
            t_html = f'<a href="{url}" target="_blank">{title}</a>' if url else title
            cls    = "nw-pinned-bull" if sc > 0 else "nw-pinned-bear"
            lbl    = "🔥 強烈利多訊號" if sc > 0 else "⚠️ 強烈利空訊號"
            sc_cls = "nw-pinned-score-bull" if sc > 0 else "nw-pinned-score-bear"
            src    = str(row.get("source", "") or "")
            rtime  = relative_time(row.get("published_at"))
            ai_blk = f'<div style="font-size:14px;color:var(--color-text-primary);margin-top:8px;line-height:1.8">{ai_sum}</div>' if ai_sum else ""
            pinned_chunks.append(f"""
<div class="{cls}">
  <span class="nw-pinned-label">{lbl}</span>
  <div class="nw-pinned-title">{t_html}</div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span class="{sc_cls}">{sc:+.1f}</span>
    <span style="font-size:12px;color:var(--color-text-secondary)">{src}</span>
    <span style="font-size:12px;color:var(--color-text-tertiary);font-family:'JetBrains Mono',monospace">{rtime}</span>
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

        # ① 相對時間顯示
        rtime = ""
        if row.get("published_at") is not None:
            rtime = relative_time(row["published_at"])

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
        if is_geo:
            badges.append('<span class="nw-badge-geo">&#9873; 地緣</span>')
        for t in tickers[:4]:
            badges.append(f'<span class="nw-tick">{t}</span>')
        bdg = " ".join(badges)

        # B AI摘要：預設顯示前30字預覽，點擊展開全文
        ai_block = ""
        if ai_sum:
            rsn_part = f'<div class="nw-ai-reason">&#128204; {ai_rsn}</div>' if ai_rsn else ""
            preview = ai_sum[:35] + ("…" if len(ai_sum) > 35 else "")
            ai_block = (
                f'<div class="nw-ai-preview">{preview}<span class="nw-ai-more">展開</span></div>'
                f'<div class="nw-ai-full">{ai_sum}{rsn_part}</div>'
            )

        chunks.append(f"""
<div class="{cls}">
  <div class="nw-title">{t_html}</div>
  <div class="nw-meta">
    {score_h} {bdg}
    <span class="nw-src">{source}</span>
    <span class="nw-time" title="{rtime}">{rtime}</span>
  </div>
  {ai_block}
</div>""")

    st.markdown("\n".join(chunks), unsafe_allow_html=True)
    if len(df) > max_items:
        st.caption(f"顯示前 {max_items} 則，共 {len(df)} 則")


# ─────────────────────────────────────────────
# ① Topbar：單行，Logo + 狀態 + 時間 + AI開關 + 立即抓取（同一視覺列）
# ─────────────────────────────────────────────
_groq_ok = st.session_state["groq_ok"]
_status_html = (
    '<span class="topbar-status-ok">&#9679; Groq AI</span>'
    if _groq_ok else
    '<span class="topbar-status-warn">&#9888; 關鍵字模式</span>'
)
_next_run = next_run_time()
_last_upd = st.session_state["last_update"]

_tb1, _tb2, _tb3 = st.columns([4, 2, 2])
with _tb1:
    st.markdown(f"""
<div class="topbar">
  <div class="topbar-left">
    <span class="topbar-logo">📈 FinNews AI</span>
    {_status_html}
    <span class="topbar-time">下次 {_next_run} · 最後 {_last_upd}</span>
  </div>
</div>
""", unsafe_allow_html=True)
with _tb2:
    st.session_state["use_ai"] = st.checkbox(
        "啟用 AI 深度分析",
        value=st.session_state["use_ai"],
        disabled=not _groq_ok,
        key="use_ai_cb",
    )
with _tb3:
    if st.button("🔄 立即抓取新聞", type="primary", use_container_width=True):
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

    # 利多/利空指標只算 12h 內（從 df_12h 統計，不用全庫 counts）
    if df_12h is not None and not df_12h.empty:
        bull_n = int((df_12h["sentiment"] == "bullish").sum())
        bear_n = int((df_12h["sentiment"] == "bearish").sum())
        mid_n  = int((df_12h["sentiment"] == "neutral").sum())
    else:
        bull_n = counts.get("bullish", 0)
        bear_n = counts.get("bearish", 0)
        mid_n  = counts.get("neutral", 0)
    total = bull_n + bear_n + mid_n

    # ── 頂部指標（5欄，自訂 HTML，台灣紅漲綠跌，百分比同行）──
    def _metric_html(icon, label, value, pct=None, bull=False):
        if pct is not None:
            if bull:
                delta_html = (f'<span style="color:var(--color-bull-text);font-size:11px;'
                              f'margin-left:4px;font-weight:400">↑ {pct:.1f}%</span>')
            else:
                delta_html = (f'<span style="color:var(--color-bear-text);font-size:11px;'
                              f'margin-left:4px;font-weight:400">↓ {pct:.1f}%</span>')
        else:
            delta_html = ""
        return (
            f'<div style="padding:10px 14px">'
            f'  <div style="font-size:11px;color:var(--color-text-secondary);font-weight:500;margin-bottom:4px">'
            f'    {icon} {label}</div>'
            f'  <div style="font-size:20px;font-weight:600;line-height:1;color:var(--color-text-primary)">'
            f'    {value}{delta_html}</div>'
            f'</div>'
        )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(_metric_html("📰", "新聞總數", total), unsafe_allow_html=True)
    if total:
        c2.markdown(_metric_html("📈", "利多", bull_n, pct=bull_n/total*100, bull=True),  unsafe_allow_html=True)
        c3.markdown(_metric_html("📉", "利空", bear_n, pct=bear_n/total*100, bull=False), unsafe_allow_html=True)
    else:
        c2.markdown(_metric_html("📈", "利多", 0),  unsafe_allow_html=True)
        c3.markdown(_metric_html("📉", "利空", 0), unsafe_allow_html=True)
    c4.markdown(_metric_html("✦", "AI 分析", len(ai_12h)),    unsafe_allow_html=True)
    c5.markdown(_metric_html("⚑", "地緣政治", len(geo_df)),   unsafe_allow_html=True)

    # ── AI 市場總結 ──
    st.markdown('<div class="sec-hd">✦ AI 市場總結</div>', unsafe_allow_html=True)

    if not st.session_state["groq_ok"]:
        st.info("需要設定 Groq API Key 才能顯示 AI 市場總結")
    elif ai_12h.empty:
        st.info("尚無 AI 分析資料，請先抓取新聞並啟用 AI 深度分析")
    else:
        ts_key = str(ai_12h.iloc[0].get("published_at", ""))
        cache_key = f"ds_v23_{ts_key}"

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
            tick_html = f'<div style="font-size:12px;color:var(--color-text-tertiary);margin-bottom:4px">關注個股</div><div class="ai-tickers">{tick_tags}</div>' if tick_tags else ""

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
            # 熔斷中顯示 warning（非 error），其他錯誤才用 error
            if "熔斷" in serr or "429" in serr or "速率限制" in serr:
                st.warning(f"⏳ {serr}")
            else:
                st.error(f"AI 總結生成失敗：{serr}")
            if st.button("🔄 重試", key="regen_err"):
                st.session_state.pop("_sum_key", None)
                st.rerun()

    # ── 地緣政治警示（雙欄 grid，左三右三）──
    if not geo_df.empty:
        geo_12h = filter_12h(geo_df)
        if not geo_12h.empty:
            st.markdown('<div class="sec-hd">⚑ 地緣政治警示</div>', unsafe_allow_html=True)
            geo_chunks = []
            for _, row in geo_12h.head(6).iterrows():
                eff    = row.get("ai_sentiment", "") or row.get("sentiment", "neutral")
                impact = "利多" if eff == "bullish" else ("利空" if eff == "bearish" else "中性")
                url_g  = row.get("url", "")
                ttl_g  = row.get("title", "")
                link_h = f'<a href="{url_g}" target="_blank">{ttl_g}</a>' if url_g else ttl_g
                geo_chunks.append(f"""
<div class="geo-card">
  <div class="geo-icon">&#9873;</div>
  <div>
    <div class="geo-title">{link_h}</div>
    <div class="geo-meta">{impact}</div>
  </div>
</div>""")
            st.markdown(f'<div class="geo-grid">{"".join(geo_chunks)}</div>', unsafe_allow_html=True)

    # ── 今日重點新聞 + 圖表（兩欄）──
    st.markdown('<div class="sec-hd">🔑 今日重點新聞（12h）</div>', unsafe_allow_html=True)
    col_news, col_chart = st.columns([4, 1])

    with col_news:
        if ai_12h.empty:
            _groq_status = "✅ Groq Key 已設定" if st.session_state["groq_ok"] else "❌ Groq Key 未找到"
            _use_ai_status = "✅ AI 開關已開啟" if st.session_state["use_ai"] else "❌ AI 開關未開啟（頂部 checkbox 未勾）"
            st.markdown(f"""
<div class="empty-box">
  <div class="empty-box-icon">📭</div>
  <div class="empty-box-txt">12h 內無 AI 分析資料</div>
  <div style="font-size:13px;color:var(--color-text-tertiary);margin-top:10px;line-height:2.2">
    {_groq_status}<br>{_use_ai_status}<br>
    👉 請手動按「立即抓取」並確認 AI 開關開啟
  </div>
</div>""", unsafe_allow_html=True)
        else:
            key_df = ai_12h[ai_12h["ai_sentiment"].isin(["bullish", "bearish"])]
            if key_df.empty:
                st.markdown("""
<div class="empty-box">
  <div class="empty-box-icon">📭</div>
  <div class="empty-box-txt">12h 內無 AI 判定有明確多空方向的財經新聞</div>
</div>""", unsafe_allow_html=True)
            else:
                if "importance_score" in key_df.columns:
                    key_df = key_df.sort_values("importance_score", ascending=False)
                else:
                    key_df = key_df.reindex(key_df["ai_score"].abs().sort_values(ascending=False).index)
                render_news(key_df, max_items=25)

    with col_chart:
        # 情緒圓餅
        if total > 0:
            fig_pie = go.Figure(go.Pie(
                labels=["利多", "利空", "中性"],
                values=[bull_n, bear_n, mid_n],
                hole=0.60,
                marker=dict(colors=["#C0392B", "#16A085", "#DFE6E9"],  # 使用新配色
                            line=dict(color="#FFFFFF", width=2)),
                textinfo="percent+label",
                textfont=dict(size=12, color="#374151"),
                showlegend=False,
            ))
            fig_pie.update_layout(
                margin=dict(t=4, b=4, l=4, r=4), height=200,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_pie, use_container_width=True)

    # ── 最新新聞（12h 快速篩選）──
    st.markdown('<div class="sec-hd">📋 最新新聞（12h）</div>', unsafe_allow_html=True)

    # A Chip 快捷篩選列
    st.markdown("""
<div class="chip-bar">
  <span class="chip chip-all active" onclick="chipFilter(this,'all')">全部</span>
  <span class="chip chip-bull" onclick="chipFilter(this,'bull')">📈 利多</span>
  <span class="chip chip-bear" onclick="chipFilter(this,'bear')">📉 利空</span>
  <span class="chip chip-ai"  onclick="chipFilter(this,'ai')">✦ AI高分</span>
  <span class="chip chip-geo" onclick="chipFilter(this,'geo')">⚑ 地緣政治</span>
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
                                colorscale=[[0, "#E2E8F0"], [1, "#6C5CE7"]], showscale=False),  /* ← 使用主色 */
                    text=hdf.head(15)["出現次數"], textposition="outside",
                    textfont=dict(size=12, color="#64748B"),
                ))
                fig_cnt.update_layout(
                    title=dict(text="出現次數", font=dict(color="#374151", size=12)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=12)),
                    xaxis=dict(showgrid=False, visible=False),
                    margin=dict(t=24, b=8, l=8, r=40), height=360,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_cnt, use_container_width=True)

            with ch2:
                cdf15 = hdf.head(15)
                colors15 = ["#C0392B" if s >= 0.15 else ("#16A085" if s <= -0.15 else "#DFE6E9")  /* ← 使用新配色 */
                            for s in cdf15["平均情緒"]]
                fig_sc = go.Figure(go.Bar(
                    x=cdf15["平均情緒"], y=cdf15["代碼"], orientation="h",
                    marker=dict(color=colors15),
                    text=cdf15["平均情緒"].round(2), textposition="outside",
                    textfont=dict(size=12, color="#64748B"),
                ))
                fig_sc.update_layout(
                    title=dict(text="平均情緒分數", font=dict(color="#374151", size=12)),
                    yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=12)),
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

            clrs_r = ["#C0392B" if s >= 0.05 else ("#16A085" if s <= -0.05 else "#DFE6E9")  /* ← 使用新配色 */
                      for s in rank_df.head(10)["平均情緒"]]
            fig_r = go.Figure(go.Bar(
                x=rank_df.head(10)["新聞數"], y=rank_df.head(10)["類股"], orientation="h",
                marker=dict(color=clrs_r),
                text=rank_df.head(10)["新聞數"], textposition="outside",
                textfont=dict(size=12, color="#64748B"),
            ))
            fig_r.update_layout(
                title=dict(text="類股新聞數（紅=偏多 綠=偏空）", font=dict(color="#374151", size=12)),
                yaxis=dict(autorange="reversed", tickfont=dict(color="#374151", size=12)),
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
<div style="background:var(--color-background-secondary);border-radius:10px;
            padding:16px 20px;font-size:14px;color:var(--color-text-secondary);line-height:2.3">
  <strong style="color:var(--color-text-primary)">支援格式</strong><br>
  台股代碼：<code style="background:var(--color-background-tertiary);padding:2px 8px;border-radius:5px;color:var(--color-text-primary);font-family:'JetBrains Mono',monospace">2330</code>
  <code style="background:var(--color-background-tertiary);padding:2px 8px;border-radius:5px;color:var(--color-text-primary);font-family:'JetBrains Mono',monospace">2454</code><br>
  台股名稱：<code style="background:var(--color-background-tertiary);padding:2px 8px;border-radius:5px;color:var(--color-text-primary);font-family:'JetBrains Mono',monospace">台積電</code>
  <code style="background:var(--color-background-tertiary);padding:2px 8px;border-radius:5px;color:var(--color-text-primary);font-family:'JetBrains Mono',monospace">廣達</code><br>
  美股代碼：<code style="background:var(--color-background-tertiary);padding:2px 8px;border-radius:5px;color:var(--color-text-primary);font-family:'JetBrains Mono',monospace">NVDA</code>
  <code style="background:var(--color-background-tertiary);padding:2px 8px;border-radius:5px;color:var(--color-text-primary);font-family:'JetBrains Mono',monospace">TSLA</code>
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
        st.markdown("#### ⏱ 背景抓取頻率")
        st.caption("⚠️ 背景排程**不做 AI 分析**，僅存文章到資料庫。想要 AI 分析與重點新聞，請手動點「立即抓取」。")
        new_iv = st.select_slider("每隔幾分鐘自動抓取（僅存文章，不消耗 Groq quota）",
                                   options=[30, 60, 90, 120],
                                   value=st.session_state["interval"])
        if st.button("套用", key="iv_apply"):
            st.session_state["interval"] = new_iv
            update_interval(new_iv)
            st.success(f"已更新：每 {new_iv} 分鐘（背景抓取，不做 AI）")

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
                f'<span style="background:{"var(--color-bull-bg)" if lb=="利多" else "var(--color-bear-bg)"};'
                f'padding:4px 12px;border-radius:12px;font-size:12px;'
                f'color:{"var(--color-bull-text)" if lb=="利多" else "var(--color-bear-text)"};'
                f'font-weight:500;transition:all 0.2s;cursor:pointer;display:inline-block;margin:2px'
                f'onmouseover="this.style.transform=\'translateY(-2px)\'"'
                f'onmouseout="this.style.transform=\'translateY(0)\'"'
                f'>{wd} {lb}</span>'
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
  <td style="color:var(--color-text-secondary)">{lr["抓取"]}</td>
  <td style="color:var(--color-bear-text);font-weight:500">{lr["新增"]}</td>
  <td style="color:var(--color-text-tertiary)">{lr["跳過"]}</td>
  <td style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--color-text-tertiary)">{lr["時間(台灣)"]}</td>
</tr>""")
            st.markdown(f"""
<div style="overflow-x:auto">
<table class="log-table">
  <thead>
    <tr>
      <th>來源</th><th>狀態</th><th>抓取</th>
      <th style="color:var(--color-bear-text)">新增</th><th style="color:var(--color-text-tertiary)">跳過</th><th>時間(台灣)</th>
    </tr>
  </thead>
  <tbody>{"".join(log_rows_html)}</tbody>
</table>
</div>""", unsafe_allow_html=True)
