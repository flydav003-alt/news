"""
app.py — FinNews AI 財經新聞智慧分析系統
全中文來源 + Groq AI 深度分析版

來源：鉅亨網 / MoneyDJ / Yahoo奇摩 / 經濟日報 / 工商時報 / 科技新報
AI：Groq Llama 3.3 70B（選擇性觸發，節省 quota）
時間：全部台灣時間（UTC+8）

[修改]
- 總覽 Tab：新增「今日重點」區塊，僅顯示高重要性非中性新聞
- 新聞列表 Tab：新增「隱藏中性」預設勾選，減少雜訊
- AI 分析 Tab：新增按重要性排序選項
- scheduler.py 呼叫 should_use_ai 時傳入 has_tickers 參數
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
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


# ── 頁面設定 ──────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FinNews AI — 財經新聞分析",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  .stTabs [data-baseweb="tab"] { font-size:14px; padding:8px 18px; }
  .stMetric { background:#F7F7F7; border-radius:8px; padding:12px; }
  div[data-testid="stSidebarContent"] { background:#FAFAFA; }
  .stButton > button { border-radius:8px; }
  /* 台灣習慣：漲紅跌綠 */
  [data-testid="stMetricDelta"] svg { color: #C0392B !important; }
  [data-testid="stMetricDelta"][data-direction="up"] {
      color: #C0392B !important;
  }
  [data-testid="stMetricDelta"][data-direction="down"] {
      color: #1B7A34 !important;
  }
  .stProgress > div > div > div > div { background-color: #C0392B; }
  /* 今日重點卡片 */
  .key-news-card {
      background: #fff;
      border-radius: 10px;
      padding: 12px 16px;
      margin-bottom: 8px;
      border-left: 4px solid #ccc;
      box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }
  .key-news-card.bullish { border-left-color: #C0392B; }
  .key-news-card.bearish { border-left-color: #1D9E75; }
  .key-news-card-title {
      font-size: 14px;
      font-weight: 600;
      color: #1a1a1a;
      margin-bottom: 4px;
  }
  .key-news-card-summary {
      font-size: 12px;
      color: #555;
      line-height: 1.6;
  }
  .key-news-card-meta {
      font-size: 11px;
      color: #999;
      margin-top: 5px;
  }
  .importance-dot {
      display:inline-block;
      width:8px; height:8px;
      border-radius:50%;
      margin-right:4px;
      vertical-align:middle;
  }
</style>
""", unsafe_allow_html=True)


# ── 初始化 ────────────────────────────────────────────────────────────────────
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


# ══════════════════════════════════════════════════════════════════════════════
# 側邊欄
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 📈 FinNews AI")
    st.caption("台灣財經新聞智慧分析")

    if st.session_state["groq_ok"]:
        st.markdown(
            '<div style="background:#EAF3DE;border-radius:8px;padding:6px 12px;'
            'font-size:12px;color:#2D6A0F;margin:4px 0">'
            '✦ Groq AI 已啟用</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:#FEF9E7;border-radius:8px;padding:6px 12px;'
            'font-size:12px;color:#7D6608;margin:4px 0">'
            '⚠ Groq 未設定（純關鍵字模式）</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    col_dot, col_info = st.columns([1, 7])
    with col_dot:
        st.markdown(
            '<div style="width:9px;height:9px;background:#1D9E75;'
            'border-radius:50%;margin-top:7px"></div>',
            unsafe_allow_html=True,
        )
    with col_info:
        st.caption(f"排程中｜下次：{next_run_time()}")
    st.caption(f"最後更新：{st.session_state['last_update']}（台灣時間）")
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

    _db = SessionLocal()
    _counts = get_sentiment_counts(_db)
    _db.close()
    _total = sum(_counts.values())
    st.markdown(f"**資料庫：{_total} 則**")
    if _total:
        b_pct = _counts.get("bullish", 0) / _total
        r_pct = _counts.get("bearish", 0) / _total
        st.progress(b_pct, text=f"📈 利多 {b_pct*100:.0f}%")
        st.progress(r_pct, text=f"📉 利空 {r_pct*100:.0f}%")

    st.divider()
    st.caption("📡 鉅亨網 · MoneyDJ · Yahoo奇摩")
    st.caption("　　經濟日報 · 工商時報 · 科技新報")
    st.caption("✦ AI：Groq Llama 3.3 70B")


# ══════════════════════════════════════════════════════════════════════════════
# Tabs
# ══════════════════════════════════════════════════════════════════════════════
tab_dash, tab_ai, tab_hot, tab_geo, tab_news, tab_stock, tab_sector, tab_settings = st.tabs([
    "📊 總覽", "✦ AI 分析", "🔥 熱門股票", "⚑ 地緣政治",
    "📋 新聞列表", "🔍 個股聚焦", "🏭 類股排行", "⚙️ 設定"
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1：總覽
# ════════════════════════════════════════════════════════════════════════════
with tab_dash:
    @st.cache_data(ttl=60, show_spinner=False)
    def load_dash():
        db = SessionLocal()
        try:
            df     = get_articles_df(db, limit=300)
            counts = get_sentiment_counts(db)
            secs   = get_sector_counts(db)
        finally:
            db.close()
        return df, counts, secs

    df, counts, secs = load_dash()
    total  = sum(counts.values())
    bull_n = counts.get("bullish", 0)
    bear_n = counts.get("bearish", 0)
    mid_n  = counts.get("neutral", 0)

    st.markdown("### 今日市場概覽")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📰 新聞總數", total)
    c2.metric("📈 利多", bull_n,
              delta=f"{bull_n/total*100:.1f}%" if total else None)
    c3.metric("📉 利空", bear_n,
              delta=f"-{bear_n/total*100:.1f}%" if total else None,
              delta_color="inverse")
    c4.metric("🏭 涵蓋類股", len(secs) if not secs.empty else 0)

    # ── [新增] 今日重點區塊 ────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🔑 今日重點")
    st.caption("高影響力利多／利空新聞，依重要性排序（中性與低分雜訊已過濾）")

    if df.empty:
        st.info("請先點選「立即抓取新聞」")
    else:
        # 撈出非中性 + importance_score >= 2.0，最多顯示 12 則
        if "importance_score" in df.columns:
            key_df = (
                df[
                    (df["sentiment"] != "neutral") &
                    (df["importance_score"] >= 2.0)
                ]
                .sort_values("importance_score", ascending=False)
                .head(12)
            )
        else:
            # 相容舊資料（沒有 importance_score 欄位時，用 |sentiment_score| 代替）
            key_df = (
                df[df["sentiment"] != "neutral"]
                .assign(_abs=df["sentiment_score"].abs())
                .sort_values("_abs", ascending=False)
                .head(12)
            )

        if key_df.empty:
            st.info("目前沒有高影響力新聞，或資料還不夠多——可嘗試調低重要性門檻或先抓取新聞。")
        else:
            # 分左右兩欄顯示
            left_col, right_col = st.columns(2)
            for i, (_, row) in enumerate(key_df.iterrows()):
                is_bull    = row["sentiment"] == "bullish"
                color_cls  = "bullish" if is_bull else "bearish"
                icon       = "🔴" if is_bull else "🟢"
                label      = row.get("sentiment_label", "利多" if is_bull else "利空")
                imp        = row.get("importance_score", 0)
                # 優先顯示 AI 摘要，否則顯示原始摘要前 80 字
                summary    = (row.get("ai_summary") or
                              row.get("summary", "")[:80])
                ticker_str = row.get("tickers", "") or row.get("ai_affected_tickers", "")
                tickers    = [t.strip() for t in ticker_str.split(",") if t.strip()]
                ticker_tag = (
                    " ".join(
                        f'<span style="background:#F0F0F0;border-radius:4px;'
                        f'padding:1px 6px;font-size:11px;color:#333">{t}</span>'
                        for t in tickers[:3]
                    )
                    if tickers else ""
                )
                # 重要性顏色點
                imp_color  = "#C0392B" if imp >= 4 else "#E67E22" if imp >= 3 else "#3498DB"
                pub_str    = ""
                if row.get("published_at") is not None:
                    try:
                        pub_str = row["published_at"].strftime("%m/%d %H:%M")
                    except Exception:
                        pass

                card_html = f"""
                <div class="key-news-card {color_cls}">
                  <div class="key-news-card-title">
                    {icon} {row['title']}
                  </div>
                  <div class="key-news-card-summary">{summary}</div>
                  <div class="key-news-card-meta">
                    <span class="importance-dot" style="background:{imp_color}"></span>
                    重要性 {imp:.1f}　{label}　{row.get('source','')}
                    {'　' + ticker_tag if ticker_tag else ''}
                    {'　' + pub_str if pub_str else ''}
                  </div>
                </div>"""

                target_col = left_col if i % 2 == 0 else right_col
                with target_col:
                    st.markdown(card_html, unsafe_allow_html=True)
                    if row.get("url"):
                        st.markdown(
                            f'<a href="{row["url"]}" target="_blank" '
                            f'style="font-size:11px;color:#888;text-decoration:none">'
                            f'→ 查看原文</a>',
                            unsafe_allow_html=True,
                        )

    # ── 圖表區 ────────────────────────────────────────────────────────────────
    st.divider()
    col_pie, col_bar = st.columns(2)

    with col_pie:
        st.markdown("#### 情緒分佈")
        if total > 0:
            fig = px.pie(
                names  = ["利多", "利空", "中性"],
                values = [bull_n, bear_n, mid_n],
                color  = ["利多", "利空", "中性"],
                color_discrete_map={
                    "利多": "#1D9E75", "利空": "#D85A30", "中性": "#B4B2A9"},
                hole=0.45,
            )
            fig.update_traces(textposition="inside", textinfo="percent+label",
                              textfont_size=13)
            fig.update_layout(showlegend=True,
                              margin=dict(t=10, b=10, l=10, r=10), height=270)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("請先點選「立即抓取新聞」")

    with col_bar:
        st.markdown("#### 熱門類股排行")
        if not secs.empty:
            fig2 = px.bar(
                secs.head(8), x="count", y="sector", orientation="h",
                color="count",
                color_continuous_scale=["#EEEDFE", "#534AB7"],
                labels={"count": "則數", "sector": "類股"},
            )
            fig2.update_layout(
                showlegend=False, coloraxis_showscale=False,
                yaxis=dict(autorange="reversed"),
                margin=dict(t=10, b=10, l=10, r=10), height=270,
            )
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    st.markdown("#### 最新新聞")

    f1, f2, f3, f4 = st.columns([1, 1, 2, 1])
    with f1:
        sent_f = st.selectbox("情緒", ["全部", "利多", "利空", "中性"], key="d_sent")
    with f2:
        srcs_list = sorted(df["source"].unique().tolist()) if not df.empty else []
        src_f = st.selectbox("來源", ["全部"] + srcs_list, key="d_src")
    with f3:
        kw = st.text_input("🔍 搜尋標題", placeholder="輸入關鍵字…", key="d_kw")
    with f4:
        sort_f = st.selectbox("排序", ["最新優先", "強度↓", "強度↑"], key="d_sort")

    ddf = df.copy() if not df.empty else pd.DataFrame()
    if not ddf.empty:
        sm = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
        if sent_f != "全部":
            ddf = ddf[ddf["sentiment"] == sm[sent_f]]
        if src_f != "全部":
            ddf = ddf[ddf["source"] == src_f]
        if kw:
            ddf = ddf[ddf["title"].str.contains(kw, case=False, na=False)]
        if sort_f == "強度↓":
            ddf = ddf.reindex(ddf["sentiment_score"].abs().sort_values(ascending=False).index)
        elif sort_f == "強度↑":
            ddf = ddf.reindex(ddf["sentiment_score"].abs().sort_values(ascending=True).index)

    st.caption(f"顯示 {len(ddf)} 則")
    news_table(ddf, key="dash", show_ai=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 2：AI 分析專頁
# ════════════════════════════════════════════════════════════════════════════
with tab_ai:
    st.markdown("### ✦ Groq AI 深度分析")
    st.caption("只顯示已通過 AI 分析的新聞（情緒模糊、含否定詞、地緣政治、強烈訊號、有個股代碼）")

    if not st.session_state["groq_ok"]:
        st.warning(
            "Groq API Key 尚未設定。\n\n"
            "請到 Streamlit Cloud → App Settings → Secrets，"
            "新增 `GROQ_API_KEY = \"gsk_你的key\"`，然後重啟 App。"
        )
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
            st.info("尚無 AI 分析結果，請先點選「立即抓取新聞」並確認已勾選「啟用 AI 深度分析」。")
        else:
            a1, a2, a3, a4 = st.columns(4)
            a1.metric("✦ AI 分析總數", len(ai_df))
            ai_bull = len(ai_df[ai_df["ai_sentiment"] == "bullish"])
            ai_bear = len(ai_df[ai_df["ai_sentiment"] == "bearish"])
            ai_mid  = len(ai_df[ai_df["ai_sentiment"] == "neutral"])
            a2.metric("📈 AI 判多", ai_bull)
            a3.metric("📉 AI 判空", ai_bear)
            a4.metric("中性", ai_mid)

            # AI 情緒 vs 關鍵字情緒 差異分析
            st.divider()
            st.markdown("#### AI vs 關鍵字 情緒差異")
            st.caption("✦ 標記 = AI 與關鍵字判斷不一致（最有參考價值）")

            ai_df["情緒一致"] = ai_df.apply(
                lambda r: r["ai_sentiment"] == r["sentiment"], axis=1)
            diff_df  = ai_df[~ai_df["情緒一致"]]
            same_df  = ai_df[ai_df["情緒一致"]]

            d1, d2 = st.columns(2)
            d1.metric("⚡ 判斷不一致", len(diff_df),
                      help="AI 與關鍵字分析結果不同，值得特別關注")
            d2.metric("✅ 判斷一致", len(same_df))

            if not diff_df.empty:
                st.markdown("##### 🔍 不一致新聞（優先關注）")
                news_table(diff_df.head(30), key="ai_diff",
                           show_summary=True, show_ai=True)

            st.divider()
            st.markdown("##### 全部 AI 分析新聞")

            af1, af2, af3 = st.columns([1, 2, 1])   # [修改] 多一個排序欄
            with af1:
                ai_sent_f = st.selectbox(
                    "AI 情緒篩選",
                    ["全部", "利多", "利空", "中性"],
                    key="ai_sent_f",
                )
            with af2:
                ai_conf_f = st.selectbox(
                    "信心程度",
                    ["全部", "high（高）", "medium（中）", "low（低）"],
                    key="ai_conf_f",
                )
            with af3:
                # [新增] 重要性排序
                ai_sort_f = st.selectbox(
                    "排序",
                    ["重要性↓", "最新優先", "AI分數↓"],
                    key="ai_sort_f",
                )

            fai = ai_df.copy()
            sm  = {"利多": "bullish", "利空": "bearish", "中性": "neutral"}
            if ai_sent_f != "全部":
                fai = fai[fai["ai_sentiment"] == sm[ai_sent_f]]
            if ai_conf_f != "全部":
                conf_key = ai_conf_f.split("（")[0]
                fai = fai[fai["ai_confidence"] == conf_key]

            # [新增] 套用排序
            if ai_sort_f == "重要性↓" and "importance_score" in fai.columns:
                fai = fai.sort_values("importance_score", ascending=False)
            elif ai_sort_f == "AI分數↓":
                fai = fai.reindex(fai["ai_score"].abs().sort_values(ascending=False).index)
            # "最新優先" 維持原本順序（published_at desc）

            st.caption(f"顯示 {len(fai)} 則")
            news_table(fai, key="ai_all", show_summary=True, show_ai=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 3：熱門股票
# ════════════════════════════════════════════════════════════════════════════
with tab_hot:
    st.markdown("### 🔥 熱門股票")
    st.caption("根據新聞提及次數 + AI 補充代碼排行，代碼可點擊連結至報價頁")

    @st.cache_data(ttl=60, show_spinner=False)
    def load_hot():
        db = SessionLocal()
        try:
            return get_ticker_counts(db, limit=30)
        finally:
            db.close()

    hot_df = load_hot()

    if hot_df.empty:
        st.info("請先抓取新聞資料。")
    else:
        st.markdown("#### Top 12 熱門股票")
        top12 = hot_df.head(12)
        cols  = st.columns(4)
        for i, (_, row) in enumerate(top12.iterrows()):
            with cols[i % 4]:
                st.markdown(
                    ticker_card(
                        code=row["代碼"], name=row["名稱"],
                        market=row["市場"],
                        count=row["出現次數"],
                        avg_score=row["平均情緒"],
                    ),
                    unsafe_allow_html=True,
                )
                st.markdown("<div style='margin-bottom:10px'></div>",
                            unsafe_allow_html=True)

        st.divider()
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.markdown("#### 出現次數")
            fig_h = px.bar(
                hot_df.head(15), x="出現次數", y="代碼",
                orientation="h", text="名稱",
                color="出現次數",
                color_continuous_scale=["#EEEDFE", "#534AB7"],
            )
            fig_h.update_traces(textposition="outside", textfont_size=10)
            fig_h.update_layout(yaxis=dict(autorange="reversed"),
                                coloraxis_showscale=False,
                                margin=dict(t=10, b=10, l=10, r=10), height=420)
            st.plotly_chart(fig_h, use_container_width=True)

        with col_c2:
            st.markdown("#### 平均情緒")
            cdf = hot_df.head(15).copy()
            cdf["顏色"] = cdf["平均情緒"].apply(
                lambda x: "利多" if x >= 0.15 else ("利空" if x <= -0.15 else "中性"))
            fig_s = px.bar(
                cdf, x="平均情緒", y="代碼", orientation="h",
                color="顏色",
                color_discrete_map={
                    "利多": "#1D9E75", "利空": "#D85A30", "中性": "#B4B2A9"},
                text="代碼",
            )
            fig_s.update_layout(yaxis=dict(autorange="reversed"),
                                margin=dict(t=10, b=10, l=10, r=10), height=420)
            st.plotly_chart(fig_s, use_container_width=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 4：地緣政治
# ════════════════════════════════════════════════════════════════════════════
with tab_geo:
    st.markdown("### ⚑ 地緣政治 / 戰爭即時警示")

    @st.cache_data(ttl=60, show_spinner=False)
    def load_geo():
        db = SessionLocal()
        try:
            return get_articles_df(db, geo_only=True, limit=100)
        finally:
            db.close()

    geo_df = load_geo()

    if geo_df.empty:
        st.info("目前沒有地緣政治相關新聞，或尚未抓取資料。")
    else:
        g1, g2, g3 = st.columns(3)
        g1.metric("⚑ 地緣政治新聞", len(geo_df))
        g2.metric("📉 利空比例",
                  f"{len(geo_df[geo_df['sentiment']=='bearish'])/len(geo_df)*100:.0f}%")
        g3.metric("📈 利多比例",
                  f"{len(geo_df[geo_df['sentiment']=='bullish'])/len(geo_df)*100:.0f}%")
        st.divider()
        news_table(geo_df, key="geo", show_summary=True, show_ai=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 5：新聞列表
# ════════════════════════════════════════════════════════════════════════════
with tab_news:
    st.markdown("### 📋 全部新聞列表")

    @st.cache_data(ttl=60, show_spinner=False)
    def load_news():
        db = SessionLocal()
        try:
            return get_articles_df(db, limit=500)
        finally:
            db.close()

    ndf = load_news()

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
        nsort = st.selectbox("排序", ["最新優先", "強度↓", "強度↑", "重要性↓"], key="n_sort")

    # [新增] 隱藏中性開關，預設勾選（讓使用者預設看不到雜訊）
    hide_neutral = st.checkbox("🙈 隱藏中性新聞（預設開啟，只看利多/利空）",
                               value=True, key="n_hide_neutral")

    fdf = ndf.copy() if not ndf.empty else pd.DataFrame()
    if not fdf.empty:
        # [新增] 優先套用隱藏中性
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
        elif nsort == "強度↑":
            fdf = fdf.reindex(fdf["sentiment_score"].abs().sort_values(ascending=True).index)
        elif nsort == "重要性↓" and "importance_score" in fdf.columns:
            fdf = fdf.sort_values("importance_score", ascending=False)

    col_cap, col_btn = st.columns([3, 1])
    with col_cap:
        st.caption(f"顯示 {len(fdf)} / {len(ndf)} 則")
    with col_btn:
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

    news_table(fdf, key="news", show_summary=True, show_ai=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 6：個股聚焦
# ════════════════════════════════════════════════════════════════════════════
with tab_stock:
    st.markdown("### 🔍 個股新聞聚焦")

    ticker_q = st.text_input(
        "輸入股票代碼或公司名稱",
        placeholder="台積電 / 2330 / 聯發科 / NVDA / 輝達…",
        key="ticker_q",
    )

    if not ticker_q:
        st.markdown("""
        **支援格式**
        - 台股代碼：`2330`（台積電）、`2454`（聯發科）
        - 台股公司名稱：`台積電`、`廣達`、`鴻海`
        - 美股代碼：`NVDA`、`TSLA`
        - 美股中文：`輝達`、`特斯拉`
        """)
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
            st.success(f"找到 **{len(sdf)}** 則 **{q}** 相關新聞")
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
            news_table(sdf, key="stock", show_summary=True, show_ai=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 7：類股排行
# ════════════════════════════════════════════════════════════════════════════
with tab_sector:
    st.markdown("### 🏭 類股影響排行")

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

        fig_r = px.bar(
            rank_df.head(10), x="新聞數", y="類股", orientation="h",
            color="平均情緒",
            color_continuous_scale=["#D85A30", "#B4B2A9", "#1D9E75"],
            range_color=[-1, 1],
            title="類股新聞數（顏色：綠=偏多 紅=偏空）",
        )
        fig_r.update_layout(
            yaxis=dict(autorange="reversed"),
            height=420, margin=dict(t=40, b=20),
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
            news_table(sec_news, key="sec_news", show_ai=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 8：設定
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
                f'<span style="background:#f5f5f5;padding:3px 12px;'
                f'border-radius:12px;font-size:12px;'
                f'color:{"#2D6A0F" if lbl=="利多" else "#9B2020"}">'
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
                bg = {"success": "#EAF3DE", "error": "#FCEBEB",
                      "empty": "#FAEEDA"}.get(status, "#F7F7F7")
                log_rows.append(f"""
                <tr style="border-bottom:1px solid #F0F0F0">
                  <td style="padding:8px 14px">{row["來源"]}</td>
                  <td style="padding:8px 14px">
                    <span style="background:{bg};padding:2px 8px;
                      border-radius:8px;font-size:11px">{status}</span>
                  </td>
                  <td style="padding:8px 14px">{row["抓取"]}</td>
                  <td style="padding:8px 14px;color:#1D9E75">{row["新增"]}</td>
                  <td style="padding:8px 14px;color:#888">{row["跳過"]}</td>
                  <td style="padding:8px 14px;font-size:11px;color:#666">
                    {row["時間(台灣)"]}</td>
                </tr>""")
            st.markdown(f"""
            <div style="overflow-x:auto;border:1px solid #EBEBEB;
                        border-radius:8px;background:#fff">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              <thead>
                <tr style="background:#F7F7F7;border-bottom:2px solid #E8E8E8">
                  <th style="padding:9px 14px;text-align:left;font-size:11px;
                             font-weight:600;color:#666">來源</th>
                  <th style="padding:9px 14px;text-align:left;font-size:11px;
                             font-weight:600;color:#666">狀態</th>
                  <th style="padding:9px 14px;text-align:left;font-size:11px;
                             font-weight:600;color:#666">抓取</th>
                  <th style="padding:9px 14px;text-align:left;font-size:11px;
                             font-weight:600;color:#1D9E75">新增</th>
                  <th style="padding:9px 14px;text-align:left;font-size:11px;
                             font-weight:600;color:#888">跳過</th>
                  <th style="padding:9px 14px;text-align:left;font-size:11px;
                             font-weight:600;color:#666">時間(台灣)</th>
                </tr>
              </thead>
              <tbody>{"".join(log_rows)}</tbody>
            </table></div>
            """, unsafe_allow_html=True)
