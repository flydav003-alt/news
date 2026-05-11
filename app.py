"""
app.py — 主入口
財經新聞智慧抓取與分析系統（最終版）

來源：Yahoo Finance / Reuters / CNBC / 鉅亨網 / BBC World
功能：情緒分析、類股標記、戰爭地緣政治板塊、三層去重、CSV匯出
部署：Streamlit Cloud（免費，固定網址）

執行：streamlit run app.py
"""

import os, sys, logging
import streamlit as st
import plotly.express as px
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from database import (init_db, SessionLocal, get_articles_df,
                      get_sentiment_counts, get_sector_counts, get_crawl_logs)
from scheduler import start_scheduler, crawl_and_save, next_run_time, update_interval
from crawler import SOURCES
from utils.ui import news_table, sectors_html

# ── 頁面設定 ──────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title = "FinNews AI — 財經新聞分析",
    page_icon  = "📈",
    layout     = "wide",
    initial_sidebar_state = "expanded",
)

# ── 初始化（只跑一次）────────────────────────────────────────────────────────
if "initialized" not in st.session_state:
    init_db()
    start_scheduler(interval_minutes=st.session_state.get("interval", 30))
    st.session_state["initialized"]  = True
    st.session_state["last_update"]  = "尚未更新"
    st.session_state["custom_bull"]  = {}
    st.session_state["custom_bear"]  = {}
    st.session_state["enabled_srcs"] = [s["name"] for s in SOURCES if s["enabled"]]
    st.session_state["interval"]     = 30


# ── 側邊欄 ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📈 FinNews AI")
    st.caption("財經新聞智慧抓取與分析系統")
    st.divider()

    # 排程狀態
    col_dot, col_txt = st.columns([1, 6])
    with col_dot:
        st.markdown('<div style="width:8px;height:8px;background:#1D9E75;'
                    'border-radius:50%;margin-top:6px"></div>', unsafe_allow_html=True)
    with col_txt:
        st.caption(f"排程運行中｜下次：{next_run_time()}")
    st.caption(f"最後更新：{st.session_state['last_update']}")
    st.divider()

    # 手動更新
    if st.button("🔄 立即抓取新聞", use_container_width=True, type="primary"):
        with st.spinner("抓取中，約需 15～30 秒…"):
            result = crawl_and_save(st.session_state["enabled_srcs"])
            st.session_state["last_update"] = result["time"]
            st.cache_data.clear()
        st.success(
            f"完成！新增 **{result['saved']}** 則｜"
            f"去重跳過 {result['skipped']} 則｜"
            f"耗時 {result['elapsed']}s"
        )
        st.rerun()

    st.divider()

    # 快速統計
    db = SessionLocal()
    counts = get_sentiment_counts(db)
    db.close()
    total = sum(counts.values())
    st.markdown(f"**今日累計：{total} 則新聞**")
    if total:
        bull_pct = counts.get("bullish", 0) / total
        bear_pct = counts.get("bearish", 0) / total
        st.progress(bull_pct, text=f"📈 利多 {bull_pct*100:.0f}%")
        st.progress(bear_pct, text=f"📉 利空 {bear_pct*100:.0f}%")

    st.divider()
    st.caption("Powered by Streamlit Cloud")
    st.caption("來源：Yahoo Finance / Reuters / CNBC / 鉅亨網 / BBC")


# ── 主頁面 Tabs ───────────────────────────────────────────────────────────────
tab_dash, tab_geo, tab_news, tab_stock, tab_sector, tab_settings = st.tabs([
    "📊 總覽", "⚑ 地緣政治", "📋 新聞列表", "🔍 個股聚焦", "🏭 類股排行", "⚙️ 設定"
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
                color_discrete_map = {
                    "利多": "#1D9E75", "利空": "#D85A30", "中性": "#B4B2A9"},
                hole = 0.45,
            )
            fig.update_traces(textposition="inside", textinfo="percent+label")
            fig.update_layout(showlegend=True,
                              margin=dict(t=10,b=10,l=10,r=10), height=270)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("請先點選「立即抓取新聞」")

    with col_bar:
        st.markdown("#### 熱門類股排行")
        if not secs.empty:
            fig2 = px.bar(
                secs.head(8), x="count", y="sector", orientation="h",
                color="count", color_continuous_scale=["#EEEDFE", "#534AB7"],
                labels={"count": "則數", "sector": "類股"},
            )
            fig2.update_layout(showlegend=False, coloraxis_showscale=False,
                               yaxis=dict(autorange="reversed"),
                               margin=dict(t=10,b=10,l=10,r=10), height=270)
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    st.markdown("#### 最新新聞")

    # 篩選列
    f1, f2, f3, f4 = st.columns([1,1,2,1])
    with f1:
        sent_f = st.selectbox("情緒", ["全部","利多","利空","中性"], key="d_sent")
    with f2:
        srcs = sorted(df["source"].unique().tolist()) if not df.empty else []
        src_f = st.selectbox("來源", ["全部"] + srcs, key="d_src")
    with f3:
        kw = st.text_input("🔍 搜尋標題", placeholder="輸入關鍵字…", key="d_kw")
    with f4:
        sort_f = st.selectbox("排序", ["最新優先","強度↓","強度↑"], key="d_sort")

    ddf = df.copy() if not df.empty else pd.DataFrame()
    if not ddf.empty:
        sm = {"利多":"bullish","利空":"bearish","中性":"neutral"}
        if sent_f != "全部": ddf = ddf[ddf["sentiment"] == sm[sent_f]]
        if src_f  != "全部": ddf = ddf[ddf["source"] == src_f]
        if kw: ddf = ddf[ddf["title"].str.contains(kw, case=False, na=False)]
        if sort_f == "強度↓":
            ddf = ddf.reindex(ddf["sentiment_score"].abs().sort_values(ascending=False).index)
        elif sort_f == "強度↑":
            ddf = ddf.reindex(ddf["sentiment_score"].abs().sort_values(ascending=True).index)

    st.caption(f"顯示 {len(ddf)} 則")
    news_table(ddf, key="dash")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2：地緣政治 / 戰爭警示
# ════════════════════════════════════════════════════════════════════════════
with tab_geo:
    st.markdown("### ⚑ 地緣政治 / 戰爭即時警示")
    st.caption("自動偵測涉及戰爭、衝突、制裁、台海、中東等關鍵字的新聞，獨立置頂顯示")

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
        # 地緣政治統計
        g1, g2, g3 = st.columns(3)
        g1.metric("地緣政治新聞", len(geo_df))
        g2.metric("利空（風險）",
                  len(geo_df[geo_df["sentiment"]=="bearish"]),
                  delta=None)
        g3.metric("利多（緩和）",
                  len(geo_df[geo_df["sentiment"]=="bullish"]))

        # 衝突熱點標籤
        hotspots = []
        keywords = {
            "俄烏": ["Ukraine","Russia","俄烏","烏克蘭"],
            "以巴": ["Israel","Gaza","Palestine","以色列","加薩"],
            "台海": ["Taiwan","台灣","台海"],
            "中東": ["Middle East","Iran","中東","伊朗"],
            "北韓": ["North Korea","北韓","朝鮮"],
            "貿易戰": ["tariff","trade war","關稅","貿易戰"],
        }
        all_text = " ".join(geo_df["title"].tolist())
        for name, kws in keywords.items():
            if any(k.lower() in all_text.lower() for k in kws):
                hotspots.append(name)

        if hotspots:
            st.markdown("**偵測到衝突熱點：** " + "  ".join(
                f'<span style="background:#FCEBEB;color:#A32D2D;'
                f'padding:3px 10px;border-radius:12px;font-size:12px;'
                f'font-weight:500;margin-right:4px">{h}</span>'
                for h in hotspots
            ), unsafe_allow_html=True)

        st.divider()

        # 地緣政治新聞表格，依情緒篩選
        geo_sent = st.radio("篩選", ["全部","利空","中性","利多"],
                            horizontal=True, key="geo_sent")
        sm = {"利多":"bullish","利空":"bearish","中性":"neutral"}
        gdf = geo_df if geo_sent == "全部" else geo_df[geo_df["sentiment"] == sm[geo_sent]]
        news_table(gdf, key="geo")


# ════════════════════════════════════════════════════════════════════════════
# TAB 3：全部新聞列表
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

    if ndf.empty:
        st.info("尚無資料，請先抓取新聞。")
        st.stop()

    n1, n2, n3, n4 = st.columns([1,1,2,1])
    with n1:
        nsent = st.selectbox("情緒", ["全部","利多","利空","中性"], key="n_sent")
    with n2:
        nsrc  = st.selectbox("來源",
                             ["全部"] + sorted(ndf["source"].unique().tolist()),
                             key="n_src")
    with n3:
        nkw   = st.text_input("🔍 搜尋", placeholder="關鍵字…", key="n_kw")
    with n4:
        nsort = st.selectbox("排序", ["最新","強度↓","強度↑"], key="n_sort")

    fdf = ndf.copy()
    sm  = {"利多":"bullish","利空":"bearish","中性":"neutral"}
    if nsent != "全部": fdf = fdf[fdf["sentiment"] == sm[nsent]]
    if nsrc  != "全部": fdf = fdf[fdf["source"] == nsrc]
    if nkw:             fdf = fdf[fdf["title"].str.contains(nkw, case=False, na=False)]
    if nsort == "強度↓":
        fdf = fdf.reindex(fdf["sentiment_score"].abs().sort_values(ascending=False).index)
    elif nsort == "強度↑":
        fdf = fdf.reindex(fdf["sentiment_score"].abs().sort_values(ascending=True).index)

    col_cap, col_btn = st.columns([3,1])
    with col_cap:
        st.caption(f"顯示 {len(fdf)} / {len(ndf)} 則新聞")
    with col_btn:
        csv = fdf[["title","sentiment_label","sentiment_score",
                   "tickers","sectors","source","published_at","url"]
               ].to_csv(index=False, encoding="utf-8-sig")
        st.download_button("⬇ 匯出 CSV", csv,
                           file_name="finnews_export.csv", mime="text/csv")

    news_table(fdf, key="news")


# ════════════════════════════════════════════════════════════════════════════
# TAB 4：個股聚焦
# ════════════════════════════════════════════════════════════════════════════
with tab_stock:
    st.markdown("### 🔍 個股新聞聚焦")

    col_in, col_btn = st.columns([3,1])
    with col_in:
        ticker_q = st.text_input(
            "輸入股票代碼",
            placeholder="台股：2330　美股：NVDA、TSLA、AMD…",
            key="ticker_q",
        )
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        search_btn = st.button("搜尋", type="primary", key="ticker_btn")

    if not ticker_q:
        st.markdown("""
        **支援格式**
        - 台股：輸入 4 位數字，如 `2330`（台積電）
        - 美股：輸入英文代碼，如 `NVDA`、`TSLA`、`AMD`
        """)
    else:
        t = ticker_q.strip().upper()
        db = SessionLocal()
        try:
            sdf = get_articles_df(db, ticker=t, limit=100)
        finally:
            db.close()

        if sdf.empty:
            st.warning(f"找不到 **{t}** 的相關新聞，請先抓取資料或確認代碼正確。")
        else:
            st.success(f"找到 **{len(sdf)}** 則 {t} 相關新聞")
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("總計", len(sdf))
            s2.metric("利多", len(sdf[sdf["sentiment"]=="bullish"]))
            s3.metric("利空", len(sdf[sdf["sentiment"]=="bearish"]))
            s4.metric("中性", len(sdf[sdf["sentiment"]=="neutral"]))

            fig_s = px.pie(
                names=sdf["sentiment_label"].value_counts().index,
                values=sdf["sentiment_label"].value_counts().values,
                color=sdf["sentiment_label"].value_counts().index,
                color_discrete_map={"利多":"#1D9E75","利空":"#D85A30","中性":"#B4B2A9"},
                hole=0.4, title=f"{t} 情緒分佈",
            )
            fig_s.update_layout(height=250, margin=dict(t=30,b=10))
            st.plotly_chart(fig_s, use_container_width=True)
            news_table(sdf, key="stock")


# ════════════════════════════════════════════════════════════════════════════
# TAB 5：類股排行
# ════════════════════════════════════════════════════════════════════════════
with tab_sector:
    st.markdown("### 🏭 類股影響排行")

    @st.cache_data(ttl=60, show_spinner=False)
    def load_sector():
        db = SessionLocal()
        try:
            df   = get_articles_df(db, limit=500)
            secs = get_sector_counts(db)
        finally:
            db.close()
        return df, secs

    full_df, secs_df = load_sector()

    if secs_df.empty:
        st.info("請先抓取新聞。")
    else:
        # 計算各類股平均情緒
        rows = []
        for _, row in secs_df.iterrows():
            sec   = row["sector"]
            cnt   = row["count"]
            mask  = full_df["sectors"].str.contains(sec, na=False)
            avg   = full_df[mask]["sentiment_score"].mean() if mask.any() else 0.0
            bull  = full_df[mask & (full_df["sentiment"]=="bullish")].shape[0]
            bear  = full_df[mask & (full_df["sentiment"]=="bearish")].shape[0]
            rows.append({"類股": sec, "新聞數": cnt,
                         "平均情緒": round(avg,3),
                         "利多": bull, "利空": bear})
        rank_df = pd.DataFrame(rows)

        fig_r = px.bar(
            rank_df.head(10), x="新聞數", y="類股", orientation="h",
            color="平均情緒",
            color_continuous_scale=["#D85A30","#B4B2A9","#1D9E75"],
            range_color=[-1, 1],
            title="類股新聞數（顏色=平均情緒：綠=偏多 紅=偏空）",
        )
        fig_r.update_layout(
            yaxis=dict(autorange="reversed"),
            coloraxis_colorbar=dict(
                title="情緒",
                tickvals=[-1,0,1], ticktext=["利空","中性","利多"]),
            height=420, margin=dict(t=40,b=20),
        )
        st.plotly_chart(fig_r, use_container_width=True)

        st.markdown("#### 詳細統計")
        st.dataframe(
            rank_df.style.background_gradient(
                subset=["平均情緒"], cmap="RdYlGn", vmin=-1, vmax=1),
            use_container_width=True, hide_index=True,
        )


# ════════════════════════════════════════════════════════════════════════════
# TAB 6：設定
# ════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.markdown("### ⚙️ 系統設定")

    set1, set2 = st.tabs(["抓取設定", "執行日誌"])

    with set1:
        st.markdown("#### ⏱ 抓取頻率")
        new_interval = st.select_slider(
            "每隔幾分鐘自動抓取一次",
            options=[15, 30, 60],
            value=st.session_state["interval"],
        )
        if st.button("套用頻率"):
            st.session_state["interval"] = new_interval
            update_interval(new_interval)
            st.success(f"已更新為每 {new_interval} 分鐘")

        st.divider()
        st.markdown("#### 📡 啟用新聞來源")
        enabled = []
        for src in SOURCES:
            checked = st.checkbox(
                f"{src['name']}　`{src['category']}` `{src['language']}`",
                value=(src["name"] in st.session_state["enabled_srcs"]),
                key=f"src_{src['name']}",
            )
            if checked:
                enabled.append(src["name"])
        if st.button("💾 儲存來源設定", type="primary"):
            st.session_state["enabled_srcs"] = enabled
            st.success(f"已儲存，啟用 {len(enabled)} 個來源")

        st.divider()
        st.markdown("#### 📝 自訂情緒詞彙")
        st.caption("新增自訂利多/利空詞彙，下次抓取時生效")
        wc1, wc2, wc3 = st.columns([2,1,1])
        with wc1:
            new_word = st.text_input("詞彙", placeholder="如：大客戶加單", key="nw")
        with wc2:
            new_score = st.number_input("分數", -1.0, 1.0, 0.7, 0.1, key="ns")
        with wc3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("新增"):
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
            for word, label in all_custom.items():
                color = "#3B6D11" if label=="利多" else "#A32D2D"
                st.markdown(
                    f'<span style="background:#f5f5f5;padding:2px 10px;'
                    f'border-radius:10px;font-size:12px;color:{color}">'
                    f'{word} {label}</span>',
                    unsafe_allow_html=True,
                )

    with set2:
        st.markdown("#### 📜 最近抓取日誌")
        db = SessionLocal()
        log_df = get_crawl_logs(db)
        db.close()
        if log_df.empty:
            st.info("尚無日誌")
        else:
            def _style(val):
                m = {"success":"background:#EAF3DE",
                     "error":"background:#FCEBEB",
                     "empty":"background:#FAEEDA"}
                return m.get(val, "")
            st.dataframe(
                log_df.style.applymap(_style, subset=["狀態"]),
                use_container_width=True, hide_index=True,
            )
