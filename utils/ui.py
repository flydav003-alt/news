"""
utils/ui.py — 共用 UI 元件
- 情緒 badge、分數條
- 代碼標籤（含公司名稱，可點擊連結）
- AI 分析摘要卡片
- 純 HTML 新聞表格（不依賴 pandas.style）
- 所有時間顯示台灣時間
"""

import json
import pandas as pd
import streamlit as st


# ── 情緒樣式（台灣習慣：漲紅跌綠）─────────────────────────────────────────────
SENT_STYLE = {
    "bullish": ("利多", "#FDECEA", "#C0392B"),   # 紅色系
    "bearish": ("利空", "#E8F5E9", "#1B7A34"),   # 綠色系
    "neutral": ("中性", "#F1EFE8", "#5F5E5A"),   # 灰色系
}

CONF_STYLE = {
    "high":   ("高", "#FDECEA", "#C0392B"),
    "medium": ("中", "#FEF9E7", "#7D6608"),
    "low":    ("低", "#F1EFE8", "#888"),
}


def badge(sentiment: str) -> str:
    label, bg, color = SENT_STYLE.get(sentiment, ("中性", "#F1EFE8", "#5F5E5A"))
    return (
        f'<span style="background:{bg};color:{color};padding:3px 10px;'
        f'border-radius:12px;font-size:11px;font-weight:600">{label}</span>'
    )


def ai_badge(sentiment: str) -> str:
    """AI 判定的情緒 badge，加上 ✦ 標記"""
    label, bg, color = SENT_STYLE.get(sentiment, ("中性", "#F1EFE8", "#5F5E5A"))
    return (
        f'<span style="background:{bg};color:{color};padding:3px 10px;'
        f'border-radius:12px;font-size:11px;font-weight:600;'
        f'border:1px solid {color}44">✦ {label}</span>'
    )


def conf_badge(confidence: str) -> str:
    label, bg, color = CONF_STYLE.get(confidence, ("", "#F1EFE8", "#888"))
    if not label:
        return ""
    return (
        f'<span style="background:{bg};color:{color};padding:2px 7px;'
        f'border-radius:8px;font-size:10px">信心:{label}</span>'
    )


def score_bar(score: float, scale10: bool = False) -> str:
    """
    scale10=False：輸入 -1~+1（關鍵字分數）
    scale10=True ：輸入 -10~+10（AI 分數）
    """
    if scale10:
        pct   = int(abs(score) / 10 * 100)
        disp  = f"{score:+.1f}"
    else:
        pct   = int(abs(score) * 100)
        disp  = f"{score:+.2f}"
    color = "#C0392B" if score > 0 else ("#1B7A34" if score < 0 else "#B4B2A9")  # 台灣：漲紅跌綠
    return (
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<div style="width:60px;background:#EBEBEB;border-radius:4px;'
        f'height:6px;overflow:hidden;flex-shrink:0">'
        f'<div style="width:{pct}%;height:100%;background:{color};'
        f'border-radius:4px"></div></div>'
        f'<span style="font-size:11px;color:{color};font-weight:500;'
        f'font-family:monospace">{disp}</span>'
        f'</div>'
    )


def tickers_html(tickers_str: str, ticker_details_json: str = "",
                 ai_tickers_str: str = "") -> str:
    """
    顯示代碼標籤，優先用 ticker_details（含名稱），
    再補入 AI 偵測的額外代碼。
    台股 → Yahoo 台灣，美股 → Yahoo Finance
    """
    shown_codes = set()
    spans = []

    # ── 主要代碼（有完整資訊）────────────────────────────────
    try:
        details = json.loads(ticker_details_json or "[]")
    except Exception:
        details = []

    for item in details:
        code   = item.get("code", "")
        name   = item.get("name", code)
        market = item.get("market", "TW")
        if not code or code in shown_codes:
            continue
        shown_codes.add(code)
        spans.append(_ticker_span(code, name, market))

    # ── AI 補充代碼 ───────────────────────────────────────────
    for code in (ai_tickers_str or "").split(","):
        code = code.strip()
        if not code or code in shown_codes:
            continue
        shown_codes.add(code)
        market = "TW" if code.isdigit() else "US"
        spans.append(_ticker_span(code, code, market, is_ai=True))

    if spans:
        return " ".join(spans)

    # Fallback：純字串
    for t in (tickers_str or "").split(","):
        t = t.strip()
        if not t or t in shown_codes:
            continue
        shown_codes.add(t)
        market = "TW" if t.isdigit() else "US"
        spans.append(_ticker_span(t, t, market))

    return " ".join(spans) if spans else '<span style="color:#ccc;font-size:11px">—</span>'


def _ticker_span(code: str, name: str, market: str,
                 is_ai: bool = False) -> str:
    if market == "TW":
        link = f"https://tw.stock.yahoo.com/quote/{code}"
        bg, color = "#EEEDFE", "#3C3489"
    else:
        link = f"https://finance.yahoo.com/quote/{code}"
        bg, color = "#E6F1FB", "#0C447C"

    label  = f"{code}｜{name}" if name and name != code else code
    border = f"border:1px dashed {color}66;" if is_ai else f"border:1px solid {color}22;"
    ai_dot = '<span style="color:#F39C12;font-size:9px;margin-right:2px">✦</span>' if is_ai else ""

    return (
        f'<a href="{link}" target="_blank" style="text-decoration:none">'
        f'<span style="background:{bg};color:{color};padding:2px 7px;'
        f'border-radius:4px;font-size:11px;font-family:monospace;'
        f'font-weight:500;white-space:nowrap;{border}">'
        f'{ai_dot}{label}</span></a>'
    )


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


def ai_summary_block(ai_summary: str, ai_reason: str,
                     ai_sentiment: str, ai_score: float,
                     ai_confidence: str) -> str:
    """AI 分析摘要區塊，嵌入新聞列表行內"""
    if not ai_summary:
        return ""
    _, bg, color = SENT_STYLE.get(ai_sentiment, ("", "#F1EFE8", "#5F5E5A"))
    score_color  = "#C0392B" if ai_score > 0 else ("#1B7A34" if ai_score < 0 else "#888")  # 台灣：漲紅跌綠
    sign         = "+" if ai_score > 0 else ""
    conf_label, conf_bg, conf_c = CONF_STYLE.get(ai_confidence, ("", "#F1EFE8", "#888"))

    return f"""
    <div style="margin-top:6px;background:{bg}22;border-left:3px solid {color};
                border-radius:0 6px 6px 0;padding:6px 10px">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
        <span style="font-size:10px;color:{color};font-weight:700">✦ AI 分析</span>
        <span style="font-size:10px;color:{score_color};font-family:monospace;
                     font-weight:600">{sign}{ai_score:.1f}/10</span>
        {f'<span style="font-size:10px;background:{conf_bg};color:{conf_c};'
          f'padding:0 5px;border-radius:6px">信心:{conf_label}</span>'
          if conf_label else ""}
      </div>
      <div style="font-size:12px;color:#333;line-height:1.5">{ai_summary}</div>
      {f'<div style="font-size:10px;color:#888;margin-top:3px">📌 {ai_reason}</div>'
        if ai_reason else ""}
    </div>"""


def _format_tw_time(dt) -> str:
    """將 datetime 格式化為台灣時間字串"""
    try:
        t = pd.to_datetime(dt)
        # 如果已有時區資訊就直接格式化，否則假設已是台灣時間
        return t.strftime("%m/%d %H:%M")
    except Exception:
        return str(dt)


# ── 主新聞表格 ────────────────────────────────────────────────────────────────
def news_table(df: pd.DataFrame, key: str = "",
               show_summary: bool = False,
               show_ai: bool = True) -> None:
    if df is None or df.empty:
        st.info("目前沒有符合條件的新聞")
        return

    rows_html = []
    for _, r in df.iterrows():
        # 時間（台灣時間）
        t = _format_tw_time(r.get("published_at"))

        # 標題
        url        = r.get("url", "")
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

        source     = r.get("source", "")
        cat        = r.get("category", "")
        source_html = (
            f'<div style="font-size:10px;color:#aaa;margin-top:3px">'
            f'{source}　{cat}</div>'
        )
        geo_mark   = geo_badge() if r.get("is_geo") else ""

        # 摘要
        summary_html = ""
        if show_summary and r.get("summary"):
            summary_html = (
                f'<div style="font-size:11px;color:#888;margin-top:4px;'
                f'line-height:1.5;max-width:420px;overflow:hidden;'
                f'display:-webkit-box;-webkit-line-clamp:2;'
                f'-webkit-box-orient:vertical">{str(r["summary"])[:120]}…</div>'
            )

        # AI 分析區塊
        ai_block = ""
        if show_ai:
            ai_block = ai_summary_block(
                ai_summary   = str(r.get("ai_summary", "")),
                ai_reason    = str(r.get("ai_reason", "")),
                ai_sentiment = str(r.get("ai_sentiment", "")),
                ai_score     = float(r.get("ai_score", 0)),
                ai_confidence= str(r.get("ai_confidence", "")),
            )

        # 決定顯示的情緒：有 AI 結果優先用 AI
        has_ai     = bool(r.get("ai_sentiment"))
        sent_key   = r.get("ai_sentiment") if has_ai else r.get("sentiment", "neutral")
        badge_html = ai_badge(sent_key) if has_ai else badge(r.get("sentiment", "neutral"))
        kw_score   = float(r.get("sentiment_score", 0))
        ai_score_v = float(r.get("ai_score", 0))
        score_html = (score_bar(ai_score_v, scale10=True) if has_ai
                      else score_bar(kw_score))

        # 代碼
        tickers_cell = tickers_html(
            str(r.get("tickers", "")),
            str(r.get("ticker_details", "[]")),
            str(r.get("ai_affected_tickers", "")),
        )

        rows_html.append(f"""
        <tr style="border-bottom:1px solid #F0F0F0"
            onmouseover="this.style.background='#FAFAFA'"
            onmouseout="this.style.background='transparent'">
          <td style="padding:10px 14px;max-width:400px;vertical-align:top">
            {title_html}{geo_mark}{source_html}{summary_html}{ai_block}
          </td>
          <td style="padding:10px 14px;vertical-align:top;white-space:nowrap">
            {badge_html}
          </td>
          <td style="padding:10px 14px;vertical-align:top;min-width:90px">
            {score_html}
          </td>
          <td style="padding:10px 14px;vertical-align:top;max-width:220px">
            <div style="display:flex;flex-wrap:wrap;gap:3px">{tickers_cell}</div>
          </td>
          <td style="padding:10px 14px;vertical-align:top;max-width:180px">
            <div style="display:flex;flex-wrap:wrap;gap:3px">
              {sectors_html(str(r.get("sectors","")))}
            </div>
          </td>
          <td style="padding:10px 14px;font-size:11px;color:#aaa;
                     white-space:nowrap;vertical-align:top">{t}<br>
            <span style="font-size:9px;color:#ddd">台灣時間</span>
          </td>
        </tr>""")

    table_html = f"""
    <div style="overflow-x:auto;border:1px solid #EBEBEB;border-radius:8px;
                margin-top:8px;background:#fff">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#F7F7F7;border-bottom:2px solid #E8E8E8">
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666">標題 / 來源</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666">情緒</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666">強度</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666">相關代碼</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666">類股</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;
                     font-weight:600;color:#666">時間</th>
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
        bg, color = "#EEEDFE", "#3C3489"
    else:
        link  = f"https://finance.yahoo.com/quote/{code}"
        bg, color = "#E6F1FB", "#0C447C"

    bar_color = "#1D9E75" if avg_score > 0 else ("#D85A30" if avg_score < 0 else "#B4B2A9")
    sign      = "+" if avg_score > 0 else ""
    pct       = int(abs(avg_score) * 100)

    return f"""
    <a href="{link}" target="_blank" style="text-decoration:none">
    <div style="background:{bg};border-radius:10px;padding:12px 16px;
                cursor:pointer;border:1px solid {color}22"
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
