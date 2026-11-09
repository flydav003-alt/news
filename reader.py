"""
reader.py — 閱讀模式：抓原文網頁 → 只留標題 / 內文 / 圖片 → 輸出乾淨 HTML（廣告、側欄、彈窗全部丟掉）

fetch_article(url) 失敗會丟 RuntimeError（訊息可直接顯示給使用者），成功回傳 dict：
  title, author, date, site, image, body_html, chars, final_url
所有文字都經過 html.escape，圖片只接受 http(s)，所以 body_html 可以安全地放進 st.markdown(unsafe_allow_html=True)。
"""
import base64
import html
import json
import re
import time
from urllib.parse import quote, urljoin, urlparse

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_BAD_IMG = ("pixel", "1x1", "spacer", "blank.gif", "/ads/", "adserver", "logo", "favicon",
            "doubleclick", "analytics", "beacon", "emoji")
_MAX_BYTES = 12_000_000
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


def _gn_batchexecute(gid: str, why: list) -> str:
    """Google News 新格式：先從文章頁拿簽章與時間戳，再呼叫 batchexecute 換回真實網址（同 googlenewsdecoder 的做法）。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    for k, v in (("CONSENT", "YES+cb"), ("SOCS", "CAI")):       # 略過歐盟同意頁
        sess.cookies.set(k, v, domain=".google.com")
    sg = ts = None
    for base in ("https://news.google.com/articles/", "https://news.google.com/rss/articles/"):
        try:
            r = sess.get(base + gid, timeout=12)
        except requests.RequestException as e:
            why.append(f"取文章頁失敗（{type(e).__name__}）")
            continue
        if "consent." in r.url:
            why.append("被導向 Google 同意頁")
            continue
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if sg and ts:
            break
        why.append(f"文章頁沒有簽章（HTTP {r.status_code}）")
    if not (sg and ts):
        return ""
    inner = json.dumps(["garturlreq",
                        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        gid, int(float(ts.group(1))), sg.group(1)], separators=(",", ":"))
    payload = json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))
    try:
        r = sess.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                      data="f.req=" + quote(payload),
                      headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=12)
        if r.status_code != 200:
            why.append(f"解析 API 回應 HTTP {r.status_code}")
            return ""
        arr = json.loads(r.text.split("\n\n")[1])[:-2]
        real = json.loads(arr[0][2])[1]
        return real if isinstance(real, str) else ""
    except Exception as e:
        why.append(f"解析 API 回傳格式不符（{type(e).__name__}）")
        return ""


def _resolve_google(url: str) -> str:
    """Google News 的 RSS 連結是轉址。依序嘗試：舊格式直接解碼 → batchexecute → googlenewsdecoder 套件 → 直接跟轉址。"""
    if "news.google.com" not in urlparse(url).netloc:
        return url
    m = re.search(r"/articles/([^/?#]+)", url)
    gid = m.group(1) if m else ""
    why: list = []
    ok = lambda u: isinstance(u, str) and u.startswith("http") and "google.com" not in urlparse(u).netloc

    if gid:
        try:
            raw = base64.urlsafe_b64decode(gid + "===")
            mm = re.search(rb"https?://[\x21-\x7e]+", raw)
            if mm and ok(mm.group(0).decode("ascii", "ignore")):
                return mm.group(0).decode("ascii", "ignore")
        except Exception:
            pass
        for _ in range(2):                                     # 偶爾會暫時失敗，重試一次
            real = _gn_batchexecute(gid, why)
            if ok(real):
                return real
            time.sleep(0.8)
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, interval=1) or {}
        if res.get("status") and ok(res.get("decoded_url", "")):
            return res["decoded_url"]
        why.append(f"套件回報：{str(res.get('message', ''))[:60]}")
    except ImportError:
        why.append("未安裝 googlenewsdecoder")
    except Exception as e:
        why.append(f"套件錯誤（{type(e).__name__}）")
    try:                                                       # 最後手段：像瀏覽器一樣跟轉址
        r = requests.get(url, timeout=12, allow_redirects=True, headers={"User-Agent": _UA},
                         cookies={"CONSENT": "YES+cb", "SOCS": "CAI"})
        if ok(r.url):
            return r.url
    except requests.RequestException:
        pass
    detail = "；".join(dict.fromkeys(why))[:160]
    raise RuntimeError("Google News 轉址連結解析失敗" + (f"（{detail}）" if detail else "") + "。")


def _ld_body(text: str) -> str:
    """很多新聞站會把全文放在 <script type=application/ld+json> 的 articleBody（給搜尋引擎用），頁面本來就有。"""
    best = ""

    def walk(o):
        nonlocal best
        if isinstance(o, dict):
            v = o.get("articleBody")
            if isinstance(v, str) and len(v) > len(best):
                best = v
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            walk(json.loads(m.group(1).strip()))
        except Exception:
            continue
    best = html.unescape(re.sub(r"<[^>]+>", "", best)).strip()
    return best


def _paras(t: str) -> list:
    parts = [p.strip() for p in re.split(r"[\r\n]+", t) if p.strip()]
    if len(parts) <= 1:                        # 沒換行：每約 3 句切一段
        sents = re.findall(r"[^。！？!?]+[。！？!?]?", t)
        parts = ["".join(sents[i:i + 3]).strip() for i in range(0, len(sents), 3)]
    return [p for p in parts if p]


def fetch_article(url: str) -> dict:
    try:
        import trafilatura
        from lxml import etree
    except ImportError:
        raise RuntimeError("伺服器還沒安裝閱讀模式需要的套件，請在 requirements.txt 加上 trafilatura。")

    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError("網址格式不正確。")
    url = _resolve_google(url)
    try:
        r = requests.get(url, timeout=15, allow_redirects=True, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        })
    except requests.exceptions.Timeout:
        raise RuntimeError("連線逾時（網站太慢，或刻意不回應雲端主機）。")
    except requests.exceptions.SSLError:
        raise RuntimeError("加密連線（SSL）失敗。")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("連不上該網站（對方可能直接切斷了雲端主機的連線，或暫時無回應）。")
    except requests.RequestException as e:
        raise RuntimeError(f"連線失敗：{type(e).__name__}")
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"網站拒絕這次抓取（HTTP {r.status_code}），多半是防機器人機制擋掉了雲端主機。")
    if r.status_code >= 400:
        raise RuntimeError(f"網站回應錯誤（HTTP {r.status_code}）。")
    if len(r.content) > _MAX_BYTES:
        raise RuntimeError("頁面超過 12MB，略過（多半是靠 JavaScript 載入內容的網頁）。")
    final = r.url
    if "news.google.com" in urlparse(final).netloc:
        raise RuntimeError("仍停在 Google News 轉址頁，無法讀取原文。")

    xml = trafilatura.extract(r.content, url=final, output_format="xml", include_images=True,
                              include_comments=False, include_tables=True, include_links=False)
    root, out, seen = None, [], set()
    if xml:
        root = etree.fromstring(xml.encode("utf-8"))
        main = root.find("main")
        for ch in (main if main is not None else root):
            _walk(ch, out, final, seen)

    # 內文太短（付費牆 / 需登入）時，改看頁面 JSON-LD 裡的 articleBody（網站本來就放在 HTML 裡的全文）
    try:
        page_text = r.content.decode("utf-8")
    except UnicodeDecodeError:
        page_text = r.text
    chars = sum(len(re.sub(r"<[^>]+>", "", b)) for b in out)
    ld = _ld_body(page_text)
    if len(ld) >= 300 and len(ld) > chars * 1.3:
        imgs = [b for b in out if b.startswith("<figure")]
        out = [f"<p>{esc(p)}</p>" for p in _paras(ld)] + imgs
    if not out:
        raise RuntimeError("抓不到內文（可能是付費牆、需要登入，或網頁靠 JavaScript 才顯示）。")

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
    ttl = (g("title") or (root.get("title", "") if root is not None else "") or "").strip()
    if out and ttl and out[0].startswith("<h") and re.sub(r"<[^>]+>", "", out[0]).strip() in ttl + " ":
        out.pop(0)

    chars = sum(len(re.sub(r"<[^>]+>", "", b)) for b in out)
    return {
        "title": g("title") or (root.get("title", "") if root is not None else "") or "",
        "author": g("author"), "date": g("date"), "site": g("sitename") or urlparse(final).netloc,
        "image": lead, "body_html": "".join(out), "chars": chars, "final_url": final,
    }
