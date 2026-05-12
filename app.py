"""
app.py — FinNews AI 財經新聞智慧分析系統
全中文來源版 | 強化代碼抽取 | 純HTML表格（無pandas.style問題）

來源：鉅亨網 / MoneyDJ / Yahoo奇摩 / 經濟日報 / 工商時報 / 科技新報 / 聯合報
執行：streamlit run app.py
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from database import (init_db, SessionLocal, get_articles_df,
                      get_sentiment_counts, get_sector_counts,
                      get_ticker_counts, get_crawl_logs)
from scheduler import start_scheduler, crawl_and_save, next_run_time, update_interval
from crawler import SOURCES
from utils.ui import news_table, ticker_card, tickers_html, sectors_html, badge

TZ_TW = timezone(timedelta(hours=8))

def now_tw() -> str:
    return datetime.now(TZ_TW).strftime("%H:%M:%S")


# ── 頁面設定 ──────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FinNews AI — 財經新聞分析",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 全域 CSS ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .stTabs [data-baseweb="tab"] { font-size: 14px; padding: 8px 18px; }
  .stMetric { background: #F7F7F7; border-radius: 8px; padding: 12px; }
  div[data-testid="stSidebarContent"] { background: #FAFAFA; }
  .stButton > button { border-radius: 8px; }
  h3 { margin-bottom: 4px !important; }
</style>
""", unsafe_allow_html=True)


# ── 初始化（只跑一次）────────────────────────────────────────────────────────
if "initialized" not in st.session_state:
    init_db()
    start_scheduler(interval_minutes=30)
    st.session_state.update({
        "initialized":  True,
        "last_update":  "尚未更新",
        "custom_bull":  {},
        "custom_bear":  {},
        "enabled_srcs": [s["name"] for s in SOURCES if s["enabled"]],
        "interval":     30,
    })


# ══════════════════════════════════════════════════════════════════════════════
# 側邊欄
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 📈 FinNews AI")
    st.caption("台灣財經新聞智慧分析")
    st.divider()

    # 排程狀態
    c1, c2 = st.columns([1, 7])
    with c1:
        st.markdown(
            '<div style="width:9px;height:9px;background:#1D9E75;'
            'border-radius:50%;margin-top:7px"></div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.caption(f"排程運行｜下次：{next_run_time()}")
    st.caption(f"最後更新：{st.session_state['last_update']}")
    st.divider()

    # 手動抓取
    if st.button("🔄 立即抓取新聞", use_container_width=True, type="primary"):
        with st.spinner("抓取中，約需 20～40 秒…"):
            result = crawl_and_save(
                enabled_names=st.session_state["enabled_srcs"],
                custom_bull=st.session_state["custom_bull"],
                custom_bear=st.session_state["custom_bear"],
            )
            st.session_state["last_update"] = now_tw()
            st.cache_data.clear()
        st.success(
            f"✅ 新增 **{result['saved']}** 則｜"
            f"去重 {result['skipped']} 則｜"
            f"耗時 {result['elapsed']}s"
        )
        st.rerun()

    st.divider()

    # 快速情緒統計
    db = SessionLocal()
    _counts = get_sentiment_counts(db)
    db.close()
    _total = sum(_counts.values())
    st.markdown(f"**資料庫：{_total} 則新聞**")
    if _total:
        b_pct = _counts.get("bullish", 0) / _total
        r_pct = _counts.get("bearish", 0) / _total
        st.progress(b_pct, text=f"📈 利多 {b_pct*100:.0f}%")
        st.progress(r_pct, text=f"📉 利空 {r_pct*100:.0f}%")

    st.divider()
    st.caption("📡 來源：鉅亨網 · MoneyDJ")
    st.caption("　　　Yahoo奇摩 · 經濟日報")
    st.caption("　　　工商時報 · 科技新報")


# ══════════════════════════════════════════════════════════════════════════════
# 主頁面 Tabs
# ══════════════════════════════════════════════════════════════════════════════
tab_dash, tab_hot, tab_geo, tab_news, tab_stock, tab_sector, tab_settings = st.tabs([
    "📊 總覽", "🔥 熱門股票", "⚑ 地緣政治",
    "📋 新聞列表", "🔍 個股聚焦", "🏭 類股排行", "⚙️ 設定"
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1：總覽 Dashboard
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

    # 篩選列
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
            ddf = ddf.reindex(
                ddf["sentiment_score"].abs().sort_values(ascending=False).index)
        elif sort_f == "強度↑":
            ddf = ddf.reindex(
                ddf["sentiment_score"].abs().sort_values(ascending=True).index)

    st.caption(f"顯示 {len(ddf)} 則")
    news_table(ddf, key="dash")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2：熱門股票
# ════════════════════════════════════════════════════════════════════════════
with tab_hot:
    st.markdown("### 🔥 熱門股票")
    st.caption("根據新聞提及次數 + 情緒分數排行，代碼可點擊連結至報價頁")

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
        # 上方卡片：Top 12
        st.markdown("#### Top 12 熱門股票")
        top12 = hot_df.head(12)
        cols = st.columns(4)
        for i, (_, row) in enumerate(top12.iterrows()):
            with cols[i % 4]:
                st.markdown(
                    ticker_card(
                        code=row["代碼"],
                        name=row["名稱"],
                        market=row["市場"],
                        count=row["出現次數"],
                        avg_score=row["平均情緒"],
                    ),
                    unsafe_allow_html=True,
                )
                st.markdown("<div style='margin-bottom:10px'></div>",
                            unsafe_allow_html=True)

        st.divider()

        # 圖表
        col_chart1, col_chart2 = st.columns(2)
        with col_chart1:
            st.markdown("#### 出現次數排行")
            fig_h = px.bar(
                hot_df.head(15), x="出現次數", y="代碼",
                orientation="h", text="名稱",
                color="出現次數",
                color_continuous_scale=["#EEEDFE", "#534AB7"],
            )
            fig_h.update_traces(textposition="outside", textfont_size=10)
            fig_h.update_layout(
                yaxis=dict(autorange="reversed"),
                coloraxis_showscale=False,
                margin=dict(t=10, b=10, l=10, r=10), height=420,
            )
            st.plotly_chart(fig_h, use_container_width=True)

        with col_chart2:
            st.markdown("#### 平均情緒熱度")
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
            fig_s.update_layout(
                yaxis=dict(autorange="reversed"),
                margin=dict(t=10, b=10, l=10, r=10), height=420,
                showlegend=True,
            )
            st.plotly_chart(fig_s, use_container_width=True)

        st.divider()
        st.markdown("#### 完整清單")
        # 純 HTML 表格，避免 pandas.style 問題
        rows_h = []
        for _, row in hot_df.iterrows():
            market = row["市場"]
            code   = row["代碼"]
            name   = row["名稱"]
            count  = row["出現次數"]
            score  = row["平均情緒"]
            if market == "TW":
                link = f"https://tw.stock.yahoo.com/quote/{code}"
                bg, color = "#EEEDFE", "#3C3489"
            else:
                link = f"https://finance.yahoo.com/quote/{code}"
                bg, color = "#E6F1FB", "#0C447C"
            sc_color = "#1D9E75" if score > 0 else ("#D85A30" if score < 0 else "#888")
            sign = "+" if score > 0 else ""
            rows_h.append(f"""
            <tr style="border-bottom:1px solid #F0F0F0">
              <td style="padding:9px 14px">
                <a href="{link}" target="_blank" style="text-decoration:none">
                <span style="background:{bg};color:{color};padding:2px 8px;
                  border-radius:4px;font-family:monospace;font-weight:600;
                  font-size:12px">{code}</span></a>
              </td>
              <td style="padding:9px 14px;font-size:13px">{name}</td>
              <td style="padding:9px 14px">
                <span style="font-size:11px;background:#F1EFE8;padding:1px 7px;
                  border-radius:8px">{market}</span>
              </td>
              <td style="padding:9px 14px;font-size:13px;font-weight:600">{count}</td>
              <td style="padding:9px 14px;font-size:13px;font-weight:600;
                         color:{sc_color}">{sign}{score:.3f}</td>
            </tr>""")
        table_h = f"""
        <div style="overflow-x:auto;border:1px solid #EBEBEB;border-radius:8px;background:#fff">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="background:#F7F7F7;border-bottom:2px solid #E8E8E8">
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">代碼</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">公司名稱</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">市場</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">出現次數</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">平均情緒</th>
            </tr>
          </thead>
          <tbody>{"".join(rows_h)}</tbody>
        </table></div>"""
        st.markdown(table_h, unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 3：地緣政治
# ════════════════════════════════════════════════════════════════════════════
with tab_geo:
    st.markdown("### ⚑ 地緣政治 / 戰爭即時警示")
    st.caption("自動偵測涉及戰爭、衝突、制裁、台海、中東等關鍵字的新聞")

    @st.cache_data(ttl=60, show_spinner=False)
    def load_geo():
        db = SessionLocal()
        try:
            return get_articles_df(db, geo_only=True, limit=100)
        finally:
            db.close()

    geo_df = load_geo()

    if geo_df.empty:
        st.info("目前沒有偵測到地緣政治相關新聞，或尚未抓取資料。")
    else:
        g1, g2, g3 = st.columns(3)
        g1.metric("⚑ 地緣政治新聞", len(geo_df))
        g2.metric("📉 利空比例",
                  f"{len(geo_df[geo_df['sentiment']=='bearish'])/len(geo_df)*100:.0f}%")
        g3.metric("📈 利多比例",
                  f"{len(geo_df[geo_df['sentiment']=='bullish'])/len(geo_df)*100:.0f}%")
        st.divider()
        news_table(geo_df, key="geo", show_summary=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 4：新聞列表
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

    # 篩選列
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
        nkw = st.text_input("🔍 搜尋關鍵字", placeholder="標題或摘要…", key="n_kw")
    with nf5:
        nsort = st.selectbox("排序", ["最新優先", "強度↓", "強度↑"], key="n_sort")

    fdf = ndf.copy() if not ndf.empty else pd.DataFrame()
    if not fdf.empty:
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
            fdf = fdf.reindex(
                fdf["sentiment_score"].abs().sort_values(ascending=False).index)
        elif nsort == "強度↑":
            fdf = fdf.reindex(
                fdf["sentiment_score"].abs().sort_values(ascending=True).index)

    col_cap, col_btn = st.columns([3, 1])
    with col_cap:
        st.caption(f"顯示 {len(fdf)} / {len(ndf)} 則")
    with col_btn:
        if not fdf.empty:
            csv = fdf[[
                "title", "sentiment_label", "sentiment_score",
                "tickers", "sectors", "source", "category",
                "published_at", "url",
            ]].to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                "⬇ 匯出 CSV", csv,
                file_name="finnews_export.csv", mime="text/csv",
                key="csv_btn",
            )

    news_table(fdf, key="news", show_summary=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 5：個股聚焦
# ════════════════════════════════════════════════════════════════════════════
with tab_stock:
    st.markdown("### 🔍 個股新聞聚焦")

    col_in, col_btn2 = st.columns([3, 1])
    with col_in:
        ticker_q = st.text_input(
            "輸入股票代碼或公司名稱",
            placeholder="台積電 / 2330 / 聯發科 / NVDA / 輝達…",
            key="ticker_q",
        )
    with col_btn2:
        st.markdown("<br>", unsafe_allow_html=True)
        st.button("搜尋", type="primary", key="ticker_btn")

    if not ticker_q:
        st.markdown("""
        **支援格式**
        - 台股代碼：`2330`（台積電）、`2454`（聯發科）
        - 台股公司名稱：`台積電`、`廣達`、`聯發科`
        - 美股代碼：`NVDA`、`TSLA`、`AMD`
        - 美股中文名稱：`輝達`、`特斯拉`、`超微`
        """)
    else:
        q = ticker_q.strip()
        db = SessionLocal()
        try:
            # 支援中文公司名稱搜尋
            from analyzer import TW_COMPANY_TO_CODE, US_NAME_TO_CODE
            code_q = TW_COMPANY_TO_CODE.get(q) or US_NAME_TO_CODE.get(q) or q.upper()
            sdf = get_articles_df(db, ticker=code_q, limit=150)
            # 若找不到，也試試直接關鍵字搜尋標題
            if sdf.empty:
                sdf = get_articles_df(db, keyword=q, limit=150)
        finally:
            db.close()

        if sdf.empty:
            st.warning(f"找不到 **{q}** 的相關新聞，請先抓取資料或確認輸入正確。")
        else:
            display_name = q if len(q) <= 6 else q[:6] + "…"
            st.success(f"找到 **{len(sdf)}** 則 **{display_name}** 相關新聞")

            s1, s2, s3, s4 = st.columns(4)
            s1.metric("總計", len(sdf))
            s2.metric("📈 利多", len(sdf[sdf["sentiment"] == "bullish"]))
            s3.metric("📉 利空", len(sdf[sdf["sentiment"] == "bearish"]))
            s4.metric("中性", len(sdf[sdf["sentiment"] == "neutral"]))

            vcounts = sdf["sentiment_label"].value_counts()
            fig_s = px.pie(
                names=vcounts.index, values=vcounts.values,
                color=vcounts.index,
                color_discrete_map={
                    "利多": "#1D9E75", "利空": "#D85A30", "中性": "#B4B2A9"},
                hole=0.4, title=f"{display_name} 情緒分佈",
            )
            fig_s.update_layout(height=250, margin=dict(t=30, b=10))
            st.plotly_chart(fig_s, use_container_width=True)

            # CSV 匯出
            csv_s = sdf[["title", "sentiment_label", "sentiment_score",
                          "tickers", "sectors", "source", "published_at", "url"]
                        ].to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                "⬇ 匯出 CSV", csv_s,
                file_name=f"finnews_{code_q}.csv", mime="text/csv",
                key="stock_csv",
            )
            news_table(sdf, key="stock", show_summary=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 6：類股排行
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
            rows.append({
                "類股": sec, "新聞數": cnt,
                "平均情緒": round(avg, 3),
                "利多": bull, "利空": bear,
            })
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
            coloraxis_colorbar=dict(
                title="情緒",
                tickvals=[-1, 0, 1], ticktext=["利空", "中性", "利多"],
            ),
            height=420, margin=dict(t=40, b=20),
        )
        st.plotly_chart(fig_r, use_container_width=True)

        st.markdown("#### 詳細統計")
        # 純 HTML 表格（避免 applymap/map 版本問題）
        sec_rows = []
        for _, row in rank_df.iterrows():
            score = row["平均情緒"]
            sc_color = ("#1D9E75" if score >= 0.15
                        else "#D85A30" if score <= -0.15 else "#888")
            sign = "+" if score > 0 else ""
            sec_rows.append(f"""
            <tr style="border-bottom:1px solid #F0F0F0">
              <td style="padding:9px 14px">{sectors_html(row["類股"])}</td>
              <td style="padding:9px 14px;font-weight:600">{row["新聞數"]}</td>
              <td style="padding:9px 14px;color:{sc_color};font-weight:600">
                {sign}{score:.3f}</td>
              <td style="padding:9px 14px;color:#1D9E75">{row["利多"]}</td>
              <td style="padding:9px 14px;color:#D85A30">{row["利空"]}</td>
            </tr>""")
        sec_table = f"""
        <div style="overflow-x:auto;border:1px solid #EBEBEB;border-radius:8px;background:#fff">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="background:#F7F7F7;border-bottom:2px solid #E8E8E8">
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">類股</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">新聞數</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#666">平均情緒</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#1D9E75">利多</th>
              <th style="padding:10px 14px;text-align:left;font-size:11px;
                         font-weight:600;color:#D85A30">利空</th>
            </tr>
          </thead>
          <tbody>{"".join(sec_rows)}</tbody>
        </table></div>"""
        st.markdown(sec_table, unsafe_allow_html=True)

        # 點擊類股看相關新聞
        st.divider()
        st.markdown("#### 查看類股相關新聞")
        sec_options = rank_df["類股"].tolist()
        selected_sec = st.selectbox("選擇類股", sec_options, key="sec_select")
        if selected_sec:
            db = SessionLocal()
            try:
                sec_news = get_articles_df(db, sector=selected_sec, limit=50)
            finally:
                db.close()
            st.caption(f"{selected_sec}：共 {len(sec_news)} 則新聞")
            news_table(sec_news, key="sec_news")


# ════════════════════════════════════════════════════════════════════════════
# TAB 7：設定
# ════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.markdown("### ⚙️ 系統設定")

    set1, set2, set3 = st.tabs(["📡 來源設定", "📝 情緒詞典", "📜 執行日誌"])

    with set1:
        st.markdown("#### ⏱ 抓取頻率")
        new_interval = st.select_slider(
            "每隔幾分鐘自動抓取一次",
            options=[15, 30, 60],
            value=st.session_state["interval"],
        )
        if st.button("套用頻率", key="apply_interval"):
            st.session_state["interval"] = new_interval
            update_interval(new_interval)
            st.success(f"已更新為每 {new_interval} 分鐘")

        st.divider()
        st.markdown("#### 📡 啟用新聞來源")
        enabled = []
        for src in SOURCES:
            checked = st.checkbox(
                f"**{src['name']}**　`{src['category']}` `{src['language']}`",
                value=(src["name"] in st.session_state["enabled_srcs"]),
                key=f"src_{src['name']}",
            )
            if checked:
                enabled.append(src["name"])
        if st.button("💾 儲存來源設定", type="primary", key="save_srcs"):
            st.session_state["enabled_srcs"] = enabled
            st.success(f"已儲存，啟用 {len(enabled)} 個來源")

    with set2:
        st.markdown("#### 📝 自訂情緒詞彙")
        st.caption("新增自訂利多/利空詞彙，下次抓取時生效")
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
            st.markdown("**目前自訂詞彙：**")
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
        st.markdown("#### 📜 最近抓取日誌")
        db = SessionLocal()
        log_df = get_crawl_logs(db)
        db.close()
        if log_df.empty:
            st.info("尚無日誌")
        else:
            # 純 HTML 表格
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
                  <td style="padding:8px 14px;font-size:11px;color:#aaa">{row["時間"]}</td>
                </tr>""")
            log_table = f"""
            <div style="overflow-x:auto;border:1px solid #EBEBEB;border-radius:8px;background:#fff">
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
                             font-weight:600;color:#666">時間</th>
                </tr>
              </thead>
              <tbody>{"".join(log_rows)}</tbody>
            </table></div>"""
            st.markdown(log_table, unsafe_allow_html=True)
