"""
reader.py — 閱讀模式：抓原文網頁 → 只留標題 / 內文 / 圖片 → 輸出乾淨 HTML（廣告、側欄、彈窗全部丟掉）

fetch_article(url) 失敗會丟 RuntimeError（訊息可直接顯示給使用者），成功回傳 dict：
  title, author, date, site, image, body_html, chars, final_url
所有文字都經過 html.escape，圖片只接受 http(s)，所以 body_html 可以安全地放進 st.markdown(unsafe_allow_html=True)。
"""
import html
import re
from urllib.parse import urljoin, urlparse

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_BAD_IMG = ("pixel", "1x1", "spacer", "blank.gif", "/ads/", "adserver", "logo", "favicon",
            "doubleclick", "analytics", "beacon", "emoji")
_MAX_BYTES = 5_000_000
esc = html.escape


def _txt(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _img(src: str, base: str, alt: str = "") -> str:
    if not src:
        return ""
    src = urljoin(base, src.strip())
    if urlparse(src).scheme not in ("http", "https"):
        return ""
    low = src.lower()
    if any(b in low for b in _BAD_IMG):
        return ""
    return (f'<figure><img src="{esc(src, quote=True)}" alt="{esc(alt or "", quote=True)}" '
            f'loading="lazy" referrerpolicy="no-referrer"></figure>'), src


def _walk(el, out: list, base: str, seen: set) -> None:
    tag = el.tag
    if tag == "graphic":
        r = _img(el.get("src", ""), base, el.get("alt", "") or el.get("title", ""))
        if r and r[1] not in seen:
            seen.add(r[1])
            out.append(r[0])
    elif tag == "head":
        t = _txt(el)
        if t:
            lv = 3 if str(el.get("rend", "h2")).lower() in ("h1", "h2") else 4
            out.append(f"<h{lv}>{esc(t)}</h{lv}>")
    elif tag == "p":
        t = _txt(el)
        if t:
            out.append(f"<p>{esc(t)}</p>")
        for g in el.iter("graphic"):
            _walk(g, out, base, seen)
    elif tag == "quote":
        t = _txt(el)
        if t:
            out.append(f"<blockquote>{esc(t)}</blockquote>")
    elif tag == "list":
        items = [_txt(i) for i in el.iter("item")]
        items = [i for i in items if i]
        if items:
            k = "ol" if str(el.get("rend", "")).lower() == "ol" else "ul"
            out.append(f"<{k}>" + "".join(f"<li>{esc(i)}</li>" for i in items) + f"</{k}>")
    elif tag == "table":
        rows = []
        for tr in el.iter("row"):
            cells = "".join(f"<td>{esc(_txt(c))}</td>" for c in tr.iter("cell"))
            if cells:
                rows.append(f"<tr>{cells}</tr>")
        if rows:
            out.append('<div class="rdr-tbl"><table>' + "".join(rows) + "</table></div>")
    elif tag in ("code",):
        t = el.text_content() if hasattr(el, "text_content") else "".join(el.itertext())
        if t.strip():
            out.append(f"<pre>{esc(t.strip())}</pre>")
    elif len(el):
        for ch in el:
            _walk(ch, out, base, seen)
    else:
        t = _txt(el)
        if t:
            out.append(f"<p>{esc(t)}</p>")


def fetch_article(url: str) -> dict:
    try:
        import trafilatura
        from lxml import etree
    except ImportError:
        raise RuntimeError("伺服器還沒安裝閱讀模式需要的套件，請在 requirements.txt 加上 trafilatura。")

    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError("網址格式不正確。")
    try:
        r = requests.get(url, timeout=15, allow_redirects=True, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        })
    except requests.RequestException as e:
        raise RuntimeError(f"連線失敗：{type(e).__name__}")
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"網站拒絕這次抓取（HTTP {r.status_code}），可能擋掉了雲端主機。")
    if r.status_code >= 400:
        raise RuntimeError(f"網站回應錯誤（HTTP {r.status_code}）。")
    if len(r.content) > _MAX_BYTES:
        raise RuntimeError("頁面太大，略過。")
    final = r.url
    if "news.google.com" in urlparse(final).netloc:
        raise RuntimeError("這是 Google News 的轉址連結，無法直接讀取原文。")

    xml = trafilatura.extract(r.content, url=final, output_format="xml", include_images=True,
                              include_comments=False, include_tables=True, include_links=False,
                              favor_precision=True)
    if not xml:
        raise RuntimeError("抓不到內文（可能是付費牆、需要登入，或網頁靠 JavaScript 才顯示）。")
    root = etree.fromstring(xml.encode("utf-8"))
    main = root.find("main")
    out, seen = [], set()
    for ch in (main if main is not None else root):
        _walk(ch, out, final, seen)

    meta = None
    try:
        meta = trafilatura.extract_metadata(r.content, default_url=final)
    except Exception:
        pass
    g = lambda k: (getattr(meta, k, "") or "") if meta else ""

    lead = ""
    li = _img(g("image"), final)
    if li and li[1] not in seen:
        lead = li[0]

    # 內文開頭若只是重複標題就拿掉（標題已另外顯示）
    ttl = (g("title") or root.get("title", "") or "").strip()
    if out and ttl and out[0].startswith("<h") and re.sub(r"<[^>]+>", "", out[0]).strip() in ttl + " ":
        out.pop(0)

    chars = sum(len(re.sub(r"<[^>]+>", "", b)) for b in out)
    return {
        "title": g("title") or root.get("title", "") or "",
        "author": g("author"), "date": g("date"), "site": g("sitename") or urlparse(final).netloc,
        "image": lead, "body_html": "".join(out), "chars": chars, "final_url": final,
    }
