"""
utils/ui.py — 共用 UI 元件
供所有 pages/*.py 引用，統一樣式
"""

import pandas as pd
import streamlit as st


SENT_STYLE = {
    "bullish": ("利多", "#EAF3DE", "#3B6D11"),
    "bearish": ("利空", "#FCEBEB", "#A32D2D"),
    "neutral": ("中性", "#F1EFE8", "#5F5E5A"),
}


def badge(sentiment: str) -> str:
    label, bg, color = SENT_STYLE.get(sentiment, ("中性", "#F1EFE8", "#5F5E5A"))
    return (f'<span style="background:{bg};color:{color};padding:2px 9px;'
            f'border-radius:12px;font-size:11px;font-weight:500">{label}</span>')


def score_bar(score: float) -> str:
    pct = int(abs(score) * 100)
    color = "#1D9E75" if score > 0 else ("#D85A30" if score < 0 else "#B4B2A9")
    return (f'<div style="display:flex;align-items:center;gap:5px">'
            f'<div style="width:56px;background:#F1EFE8;border-radius:3px;height:6px;overflow:hidden">'
            f'<div style="width:{pct}%;height:100%;background:{color};border-radius:3px"></div></div>'
            f'<span style="font-size:11px;color:#888">{pct}</span></div>')


def tickers_html(s: str) -> str:
    if not s:
        return "<span style='color:#bbb'>—</span>"
    return " ".join(
        f'<span style="font-family:monospace;font-size:11px;background:#F1EFE8;'
        f'padding:1px 5px;border-radius:3px">{t.strip()}</span>'
        for t in s.split(",") if t.strip()
    )


def sectors_html(s: str) -> str:
    if not s:
        return "<span style='color:#bbb'>—</span>"
    color_map = {
        "半導體":   ("#EEEDFE", "#3C3489"),
        "AI":       ("#E6F1FB", "#0C447C"),
        "電動車":   ("#EAF3DE", "#3B6D11"),
        "綠能":     ("#EAF3DE", "#3B6D11"),
        "金融":     ("#FAEEDA", "#633806"),
        "傳產":     ("#F1EFE8", "#5F5E5A"),
        "戰爭/國防":("#FCEBEB", "#A32D2D"),
        "地緣政治": ("#FCEBEB", "#A32D2D"),
        "能源":     ("#FAEEDA", "#633806"),
    }
    spans = []
    for sec in s.split(","):
        sec = sec.strip()
        if sec:
            bg, color = color_map.get(sec, ("#F1EFE8", "#5F5E5A"))
            spans.append(
                f'<span style="background:{bg};color:{color};padding:2px 7px;'
                f'border-radius:10px;font-size:11px">{sec}</span>'
            )
    return " ".join(spans)


def news_table(df: pd.DataFrame, key: str = "") -> None:
    """渲染新聞表格，標題可點擊開啟原文連結"""
    if df is None or df.empty:
        st.info("目前沒有符合條件的新聞")
        return

    rows = []
    for _, r in df.iterrows():
        # 時間格式化
        try:
            t = pd.to_datetime(r["published_at"]).strftime("%m/%d %H:%M")
        except Exception:
            t = str(r.get("published_at", ""))

        # 標題（可點擊）
        url = r.get("url", "")
        if url:
            title_html = (
                f'<a href="{url}" target="_blank" style="color:inherit;'
                f'text-decoration:none;font-weight:500;line-height:1.5">'
                f'{r["title"]}'
                f'<span style="color:#534AB7;margin-left:4px;font-size:11px">↗</span></a>'
            )
        else:
            title_html = f'<span style="font-weight:500">{r["title"]}</span>'

        source_html = f'<div style="font-size:10px;color:#aaa;margin-top:2px">{r.get("source","")}</div>'

        # 地緣政治標記
        geo_mark = ""
        if r.get("is_geo"):
            geo_mark = '<span style="background:#FCEBEB;color:#A32D2D;font-size:10px;padding:1px 5px;border-radius:8px;margin-left:6px">⚑ 地緣</span>'

        rows.append(f"""
        <tr style="border-bottom:0.5px solid #e8e8e8">
          <td style="padding:10px 12px;max-width:380px;vertical-align:top">
            {title_html}{geo_mark}{source_html}
          </td>
          <td style="padding:10px 12px;vertical-align:top;white-space:nowrap">
            {badge(r.get("sentiment","neutral"))}
          </td>
          <td style="padding:10px 12px;vertical-align:top">
            {score_bar(float(r.get("sentiment_score",0)))}
          </td>
          <td style="padding:10px 12px;vertical-align:top">
            {tickers_html(str(r.get("tickers","")))}
          </td>
          <td style="padding:10px 12px;vertical-align:top">
            {sectors_html(str(r.get("sectors","")))}
          </td>
          <td style="padding:10px 12px;font-size:11px;color:#aaa;white-space:nowrap;vertical-align:top">
            {t}
          </td>
        </tr>""")

    html = f"""
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f7f7f7;border-bottom:1px solid #ddd">
          <th style="padding:9px 12px;text-align:left;font-size:11px;font-weight:500;color:#888">標題</th>
          <th style="padding:9px 12px;text-align:left;font-size:11px;font-weight:500;color:#888">情緒</th>
          <th style="padding:9px 12px;text-align:left;font-size:11px;font-weight:500;color:#888">強度</th>
          <th style="padding:9px 12px;text-align:left;font-size:11px;font-weight:500;color:#888">代碼</th>
          <th style="padding:9px 12px;text-align:left;font-size:11px;font-weight:500;color:#888">類股</th>
          <th style="padding:9px 12px;text-align:left;font-size:11px;font-weight:500;color:#888">時間</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    </div>"""
    st.markdown(html, unsafe_allow_html=True)
