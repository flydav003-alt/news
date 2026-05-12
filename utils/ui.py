"""
utils/ui.py — 共用 UI 元件
- 情緒 badge、分數條
- 強化版代碼標籤（顯示代碼+公司名稱）
- 純 HTML 新聞表格（不依賴 pandas.style，永不報 applymap 錯誤）
"""

import json
import pandas as pd
import streamlit as st


# ── 情緒樣式 ──────────────────────────────────────────────────────────────────
SENT_STYLE = {
    "bullish": ("利多", "#EAF3DE", "#2D6A0F"),
    "bearish": ("利空", "#FCEBEB", "#9B2020"),
    "neutral": ("中性", "#F1EFE8", "#5F5E5A"),
}


def badge(sentiment: str) -> str:
    label, bg, color = SENT_STYLE.get(sentiment, ("中性", "#F1EFE8", "#5F5E5A"))
    return (
        f'<span style="background:{bg};color:{color};padding:3px 10px;'
        f'border-radius:12px;font-size:11px;font-weight:600;'
        f'letter-spacing:0.3px">{label}</span>'
    )


def score_bar(score: float) -> str:
    pct   = int(abs(score) * 100)
    color = "#1D9E75" if score > 0 else ("#D85A30" if score < 0 else "#B4B2A9")
    sign  = "+" if score > 0 else ""
    return (
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<div style="width:60px;background:#EBEBEB;border-radius:4px;'
        f'height:6px;overflow:hidden;flex-shrink:0">'
        f'<div style="width:{pct}%;height:100%;background:{color};'
        f'border-radius:4px;transition:width 0.3s"></div></div>'
        f'<span style="font-size:11px;color:{color};font-weight:500;'
        f'font-family:monospace">{sign}{score:.2f}</span>'
        f'</div>'
    )


def tickers_html(tickers_str: str, ticker_details_json: str = "") -> str:
    """
    優先使用 ticker_details（含公司名稱），fallback 到純代碼字串。
    台股代碼連結到 Yahoo 台灣，美股連結到 Yahoo Finance。
    """
    details = []
    try:
        details = json.loads(ticker_details_json or "[]")
    except Exception:
        pass

    if details:
        spans = []
        for item in details:
            code   = item.get("code", "")
            name   = item.get("name", code)
            market = item.get("market", "TW")
            if not code:
                continue
            if market == "TW":
                link = f"https://tw.stock.yahoo.com/quote/{code}"
                label = f"{code}｜{name}" if name != code else code
                bg, color = "#EEEDFE", "#3C3489"
            else:
                link = f"https://finance.yahoo.com/quote/{code}"
                label = f"{code}｜{name}" if name != code else code
                bg, color = "#E6F1FB", "#0C447C"
            spans.append(
                f'<a href="{link}" target="_blank" style="text-decoration:none">'
                f'<span style="background:{bg};color:{color};padding:2px 7px;'
                f'border-radius:4px;font-size:11px;font-family:monospace;'
                f'font-weight:500;white-space:nowrap;cursor:pointer;'
                f'border:1px solid {color}22">{label}</span></a>'
            )
        if spans:
            return " ".join(spans)

    # Fallback：純代碼字串
    if not tickers_str:
        return '<span style="color:#ccc;font-size:11px">—</span>'
    spans = []
    for t in tickers_str.split(","):
        t = t.strip()
        if not t:
            continue
        if t.isdigit():
            link  = f"https://tw.stock.yahoo.com/quote/{t}"
            bg, color = "#EEEDFE", "#3C3489"
        else:
            link  = f"https://finance.yahoo.com/quote/{t}"
            bg, color = "#E6F1FB", "#0C447C"
        spans.append(
            f'<a href="{link}" target="_blank" style="text-decoration:none">'
            f'<span style="background:{bg};color:{color};padding:2px 7px;'
            f'border-radius:4px;font-size:11px;font-family:monospace;'
            f'font-weight:500;border:1px solid {color}22">{t}</span></a>'
        )
    return " ".join(spans) if spans else '<span style="color:#ccc;font-size:11px">—</span>'


def sectors_html(s: str) -> str:
    if not s:
        return '<span style="color:#ccc;font-size:11px">—</span>'
    color_map = {
        "半導體":    ("#EEEDFE", "#3C3489"),
        "AI":        ("#E6F1FB", "#0C447C"),
        "伺服器":    ("#E6F1FB", "#1A5276"),
        "電動車":    ("#EAF3DE", "#2D6A0F"),
        "綠能":      ("#E9F7EF", "#1E8449"),
        "金融":      ("#FAEEDA", "#633806"),
        "傳產":      ("#F1EFE8", "#5F5E5A"),
        "航運":      ("#F1EFE8", "#4A4A4A"),
        "科技":      ("#EBF5FB", "#1A5276"),
        "戰爭/國防": ("#FCEBEB", "#9B2020"),
        "地緣政治":  ("#FCEBEB", "#9B2020"),
        "能源":      ("#FAEEDA", "#633806"),
        "國際財經":  ("#F8F9FA", "#495057"),
    }
    spans = []
    for sec in s.split(","):
        sec = sec.strip()
        if not sec:
            continue
        bg, color = color_map.get(sec, ("#F1EFE8", "#5F5E5A"))
        spans.append(
            f'<span style="background:{bg};color:{color};padding:2px 8px;'
            f'border-radius:10px;font-size:11px;font-weight:500;'
            f'white-space:nowrap">{sec}</span>'
        )
    return " ".join(spans) if spans else '<span style="color:#ccc;font-size:11px">—</span>'


def geo_badge() -> str:
    return (
        '<span style="background:#FCEBEB;color:#9B2020;font-size:10px;'
        'padding:1px 6px;border-radius:8px;margin-left:5px;'
        'font-weight:500">⚑ 地緣</span>'
    )


# ── 主新聞表格（純 HTML，無 pandas.style 依賴）──────────────────────────────────
def news_table(df: pd.DataFrame, key: str = "", show_summary: bool = False) -> None:
    if df is None or df.empty:
        st.info("目前沒有符合條件的新聞")
        return

    rows_html = []
    for _, r in df.iterrows():
        # 時間
        try:
            t = pd.to_datetime(r["published_at"]).strftime("%m/%d %H:%M")
        except Exception:
            t = str(r.get("published_at", ""))

        # 標題 + 連結
        url = r.get("url", "")
        title_text = r.get("title", "")
        if url:
            title_html = (
                f'<a href="{url}" target="_blank" rel="noopener" '
                f'style="color:#1A1A1A;text-decoration:none;font-weight:500;'
                f'line-height:1.5;font-size:13px">{title_text}'
                f'<span style="color:#534AB7;margin-left:4px;font-size:10px">↗</span></a>'
            )
        else:
            title_html = f'<span style="font-weight:500;font-size:13px">{title_text}</span>'

        # 來源 + 分類標籤
        source = r.get("source", "")
        cat    = r.get("category", "")
        source_html = f'<div style="font-size:10px;color:#aaa;margin-top:3px">{source}　{cat}</div>'

        # 地緣政治標記
        geo_mark = geo_badge() if r.get("is_geo") else ""

        # 摘要（可選）
        summary_html = ""
        if show_summary and r.get("summary"):
            summary_html = (
                f'<div style="font-size:11px;color:#888;margin-top:4px;'
                f'line-height:1.5;max-width:420px;'
                f'overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;'
                f'-webkit-box-orient:vertical">{r["summary"][:120]}…</div>'
            )

        # 代碼欄位
        td_json = r.get("ticker_details", "[]")
        tk_str  = r.get("tickers", "")
        tickers_cell = tickers_html(tk_str, td_json)

        rows_html.append(f"""
        <tr style="border-bottom:1px solid #F0F0F0;transition:background 0.15s"
            onmouseover="this.style.background='#FAFAFA'"
            onmouseout="this.style.background='transparent'">
          <td style="padding:10px 14px;max-width:400px;vertical-align:top">
            {title_html}{geo_mark}{source_html}{summary_html}
          </td>
          <td style="padding:10px 14px;vertical-align:top;white-space:nowrap">
            {badge(r.get("sentiment","neutral"))}
          </td>
          <td style="padding:10px 14px;vertical-align:top;min-width:90px">
            {score_bar(float(r.get("sentiment_score",0)))}
          </td>
          <td style="padding:10px 14px;vertical-align:top;max-width:220px">
            <div style="display:flex;flex-wrap:wrap;gap:3px">{tickers_cell}</div>
          </td>
          <td style="padding:10px 14px;vertical-align:top;max-width:180px">
            <div style="display:flex;flex-wrap:wrap;gap:3px">{sectors_html(str(r.get("sectors","")))}
            </div>
          </td>
          <td style="padding:10px 14px;font-size:11px;color:#aaa;
                     white-space:nowrap;vertical-align:top">{t}</td>
        </tr>""")

    table_html = f"""
    <div style="overflow-x:auto;border:1px solid #EBEBEB;border-radius:8px;
                margin-top:8px;background:#fff">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#F7F7F7;border-bottom:2px solid #E8E8E8">
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666;letter-spacing:0.5px">標題 / 來源</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666;letter-spacing:0.5px">情緒</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666;letter-spacing:0.5px">強度</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666;letter-spacing:0.5px">相關代碼</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666;letter-spacing:0.5px">類股</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666;letter-spacing:0.5px">時間</th>
        </tr>
      </thead>
      <tbody>{"".join(rows_html)}</tbody>
    </table>
    </div>"""

    st.markdown(table_html, unsafe_allow_html=True)


# ── 熱門股票卡片 ──────────────────────────────────────────────────────────────
def ticker_card(code: str, name: str, market: str,
                count: int, avg_score: float) -> str:
    if market == "TW":
        link  = f"https://tw.stock.yahoo.com/quote/{code}"
        bg    = "#EEEDFE"
        color = "#3C3489"
    else:
        link  = f"https://finance.yahoo.com/quote/{code}"
        bg    = "#E6F1FB"
        color = "#0C447C"

    bar_color = "#1D9E75" if avg_score > 0 else ("#D85A30" if avg_score < 0 else "#B4B2A9")
    sign      = "+" if avg_score > 0 else ""
    pct       = int(abs(avg_score) * 100)

    return f"""
    <a href="{link}" target="_blank" style="text-decoration:none">
    <div style="background:{bg};border-radius:10px;padding:12px 16px;
                cursor:pointer;transition:opacity 0.2s;border:1px solid {color}22"
         onmouseover="this.style.opacity='0.85'"
         onmouseout="this.style.opacity='1'">
      <div style="font-size:15px;font-weight:700;color:{color};
                  font-family:monospace">{code}</div>
      <div style="font-size:11px;color:{color}99;margin-top:2px">{name}</div>
      <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
        <span style="font-size:11px;color:#666">{count} 則</span>
        <span style="font-size:11px;color:{bar_color};font-weight:600">
          {sign}{avg_score:.2f}</span>
      </div>
      <div style="margin-top:5px;background:#fff;border-radius:3px;
                  height:4px;overflow:hidden">
        <div style="width:{pct}%;height:100%;background:{bar_color};
                    border-radius:3px"></div>
      </div>
    </div></a>"""
