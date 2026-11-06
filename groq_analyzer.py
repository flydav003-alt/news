"""
groq_analyzer.py — Groq AI 深度分析模組
- 使用 Llama 3.3 70B（Groq 免費額度）
- [修改] 收窄觸發條件：只送真正有分析價值的新聞，避免每則都送導致超慢
- [修改] should_use_ai 新增 category 參數：非財經類別 + 無財經信號詞直接排除
- 輸出結構化 JSON：情緒、影響摘要、受影響個股、信心度
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# [修改] llama-3.3-70b-versatile 已於 2026/8/16 被 Groq 下架（呼叫會 404）。
# 改用 openai/gpt-oss-120b；之後再換模型只要設環境變數 GROQ_MODEL，不用改程式。
MODEL        = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_TOKENS   = 1200          # gpt-oss 是推理模型，要留空間給思考，不然 content 會是空的
TEMPERATURE  = 0.1

# ── 全域熔斷器：429 後封鎖整批，冷卻 90 秒後才恢復 ──────────────────────────
# Groq 免費版 RPM=30（每 2 秒一次），一旦觸發 429 需等完整冷卻週期
_rate_limit_until: float = 0.0   # timestamp；在這時間之前不送任何請求
COOLDOWN_SECONDS = 90            # 冷卻時間（秒），比 60 秒再多一點保險


_cooldowns: dict = {}              # 金鑰 -> 冷卻到的時間；兩把金鑰各自計算，一把被限速就換另一把
_rr_counter = 0


def _get_secret(name: str) -> str:
    try:
        import streamlit as st
        v = st.secrets.get(name, "")
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "")


def _all_keys(primary: str = "") -> list:
    """GROQ_API_KEY、GROQ_API_KEY_2（兩個不同帳號）依序收集，去重。"""
    keys = []
    for k in (primary, _get_secret("GROQ_API_KEY"), _get_secret("GROQ_API_KEY_2")):
        if k and k not in keys:
            keys.append(k)
    return keys


def _is_rate_limited() -> bool:
    """全部金鑰都在冷卻才算被限速（有一把能用就繼續）"""
    keys = _all_keys()
    now = time.time()
    if not keys:
        return now < _rate_limit_until
    return all(_cooldowns.get(k, 0) > now for k in keys) or (now < _rate_limit_until and len(keys) == 1)


def _set_rate_limited() -> None:
    """所有金鑰都被 429 時由呼叫端觸發：全部進入冷卻"""
    global _rate_limit_until
    _rate_limit_until = time.time() + COOLDOWN_SECONDS
    for k in _all_keys():
        _cooldowns[k] = _rate_limit_until
    logger.warning(f"Groq 429：所有金鑰都被限速，冷卻 {COOLDOWN_SECONDS} 秒後恢復")


def _chat_once(prompt: str, key: str, max_tokens: int, temperature: float, timeout: int) -> str:
    payload = {
        "model":       MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    if "gpt-oss" in MODEL:
        payload["reasoning_effort"] = "low"     # 新聞分類不需要長思考，省 token 又快
    resp = requests.post(
        GROQ_API_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    if not resp.ok:
        logger.error(f"Groq HTTP {resp.status_code}（model={MODEL}，key=…{key[-4:]}）：{resp.text[:300]}")
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    return (msg.get("content") or "").strip()


def groq_chat(prompt: str, api_key: str, max_tokens: int = MAX_TOKENS,
              temperature: float = TEMPERATURE, timeout: int = 40) -> str:
    """
    呼叫 Groq，回傳模型文字。有多把金鑰時輪流使用（分攤額度），
    某把遇到 429 就標記冷卻並改用下一把；全部都被限速才把 429 丟給呼叫端。
    其他 HTTP 錯誤（例如 404 模型下架）直接拋出 requests.HTTPError。
    """
    global _rr_counter
    keys = _all_keys(api_key)
    if not keys:
        raise requests.exceptions.HTTPError("沒有可用的 Groq 金鑰")
    start = _rr_counter % len(keys)
    _rr_counter += 1
    order = keys[start:] + keys[:start]
    last = None
    for k in order:
        if _cooldowns.get(k, 0) > time.time():
            continue
        try:
            return _chat_once(prompt, k, max_tokens, temperature, timeout)
        except requests.exceptions.HTTPError as e:
            sc = e.response.status_code if e.response is not None else 0
            if sc == 429:
                _cooldowns[k] = time.time() + COOLDOWN_SECONDS
                logger.warning(f"Groq 金鑰 …{k[-4:]} 被限速 429，改用下一把")
                last = e
                continue
            if sc in (401, 403) and len(keys) > 1:
                _cooldowns[k] = time.time() + 3600
                logger.error(f"Groq 金鑰 …{k[-4:]} 無效（{sc}），本輪改用其他金鑰")
                last = e
                continue
            raise
    if last is not None:
        raise last
    raise requests.exceptions.HTTPError("所有 Groq 金鑰都在冷卻中")


def parse_json_loose(raw: str):
    """從模型輸出裡撈出第一個 JSON 物件或陣列（容忍 ```json 包裝與前後廢話）"""
    raw = (raw or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty", raw, 0)
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = raw.find(opener), raw.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(raw[i:j + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(raw)


def groq_translate_titles(titles: list[str], api_key: str) -> list[str]:
    """
    一次翻譯一批英文財經標題成繁體中文（台灣財經用語）。
    成功回傳與輸入等長的 list；任何失敗回傳空 list（呼叫端改走備援）。
    """
    if not titles or _is_rate_limited():
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "把下列英文財經新聞標題翻成繁體中文（台灣用語），公司名用台灣通用譯名"
        "（如 Nvidia→輝達、Tesla→特斯拉），股票代號、數字、單位保持原樣，語氣簡潔像新聞標題。\n"
        "只輸出一個 JSON 陣列，長度必須恰好是 "
        f"{len(titles)}，依序對應，不要有任何其他文字。\n\n{numbered}"
    )
    try:
        out = parse_json_loose(groq_chat(prompt, api_key, max_tokens=2500, temperature=0.0, timeout=60))
        if isinstance(out, list) and len(out) == len(titles) and all(isinstance(x, str) for x in out):
            time.sleep(2)
            return [x.strip() for x in out]
        logger.warning(f"Groq 翻譯回傳格式不符（期望 {len(titles)} 筆）")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            _set_rate_limited()
    except Exception as e:
        logger.warning(f"Groq 翻譯失敗：{type(e).__name__}: {e}")
    return []


# ── 觸發條件判斷 ──────────────────────────────────────────────────────────────

# 財經相關信號詞：標題含任一詞才視為有分析價值
_FINANCE_SIGNALS = [
    "股", "元", "億", "萬", "獲利", "營收", "法人", "財報", "毛利", "EPS",
    "台積", "聯發", "廣達", "緯創", "鴻海", "輝達", "漲", "跌", "漲停", "跌停",
    "供應鏈", "訂單", "出貨", "庫存", "產能", "AI", "半導體", "晶片", "封裝",
    "ETF", "指數", "大盤", "外資", "投信", "自營", "融資", "融券",
    "關稅", "貿易", "出口", "進口", "景氣", "Fed", "升息", "降息",
    "TSMC", "NVDA", "AMD", "MSFT", "AAPL", "INTC", "QCOM",
    # [新增] 英文新聞財經信號詞（title 比對時大小寫不敏感，見 should_use_ai）
    "stock", "shares", "earnings", "revenue", "profit", "chip", "chips", "nasdaq",
    "s&p", "dow", "fed", "rate", "inflation", "tariff", "ai ", "nvidia", "apple",
    "tesla", "microsoft", "amazon", "oil", "yields", "bank", "ipo", "market",
]

# 非財經類別：這些 category 的新聞直接不送（科技/科學/天文等）
_NON_FINANCE_CATEGORIES = {"科學", "太空", "天文", "健康", "醫療", "體育", "運動", "娛樂", "電玩", "生活"}


def should_use_ai(
    keyword_score: float,
    title: str,
    is_geo: bool,
    has_tickers: bool = False,
    category: str = "",
) -> bool:
    """
    判斷這則新聞是否需要送 Groq 做 AI 分析。

    設計原則：寧可少送，不要每則都送。
    送 AI 的目的是「補關鍵字判斷不足」，不是替每則新聞都加摘要。

    前置過濾（命中即直接返回 False，不進後續條件）：
      - 非財經類別（科學/太空/天文/娛樂…）
      - 情緒模糊帶且標題無任何財經信號詞（純科技/學術新聞幾乎都中了這條）

    觸發條件（任一符合即送）：
      1. 情緒模糊帶（-0.15 ~ +0.15）且含財經信號詞：關鍵字沒把握，需要 AI
      2. 含否定詞且有強烈情緒（|score|>0.2）：否定詞可能反轉方向
      3. 地緣政治新聞：影響複雜，需要深度解讀
      4. 有個股代碼 + 強烈訊號（|score|>=0.4）：個股影響才送，避免雜訊

    不送的情況：
      - 分數明確（|score|>0.15）且無否定詞、無地緣政治、無個股 → 關鍵字夠用
      - 分數很強（|score|>0.4）但沒有個股代碼 → 影響範圍不精確，不值得送
    """
    # ── 前置過濾 1：非財經類別直接排除 ─────────────────────────
    if category in _NON_FINANCE_CATEGORIES:
        logger.debug(f"非財經類別（{category}），跳過 AI：{title[:30]}")
        return False

    # ── 前置過濾 2：地緣政治無視財經信號詞，直接通過（條件3提前處理）
    if is_geo:
        return True

    # ── 前置過濾 3：情緒模糊帶但無任何財經信號詞 → 純科技/學術/雜訊，排除 ──
    _tl = title.lower()
    has_finance_signal = any((w in title) or (w.lower() in _tl) for w in _FINANCE_SIGNALS)
    if -0.15 <= keyword_score <= 0.15 and not has_finance_signal:
        logger.debug(f"模糊帶但無財經信號詞，跳過 AI：{title[:30]}")
        return False

    # ── 條件 1：情緒模糊帶 + 有財經信號詞（關鍵字判斷不足，需 AI）─────────
    if -0.15 <= keyword_score <= 0.15:
        return True

    # ── 條件 2：含否定詞 且 情緒有一定強度 ──────────────────────────────────
    negation_words = ["不", "未", "沒有", "擬", "傳", "疑", "恐"]
    if any(w in title for w in negation_words) and abs(keyword_score) > 0.20:
        return True

    # ── 條件 4：有個股代碼 且 訊號夠強 ──────────────────────────────────────
    if has_tickers and abs(keyword_score) >= 0.40:
        return True

    return False


# ── Prompt 設計 ───────────────────────────────────────────────────────────────
def _build_prompt(title: str, summary: str, category: str) -> str:
    return f"""你是一位專業的台灣股市財經分析師。請分析以下新聞對股市的影響。

新聞標題：{title}
新聞摘要：{summary[:300] if summary else "（無摘要）"}
新聞類別：{category}

請用繁體中文回答，只輸出以下 JSON 格式，不要有任何其他文字：

{{
  "sentiment": "bullish/bearish/neutral（三擇一）",
  "score": 數字（-10到+10，+10極度利多，-10極度利空，0中性）,
  "summary": "請用40至60字說明這則新聞對台灣股市或相關個股的具體影響，包含影響方向和原因",
  "affected_tickers": ["受影響的股票代碼，台股用4位數字如2330，美股用英文如NVDA，最多3個，若不確定給空陣列"],
  "reason": "一句話說明判斷依據（20字內）",
  "confidence": "high/medium/low（三擇一）"
}}"""


# ── 主分析函式 ────────────────────────────────────────────────────────────────
def groq_analyze(
    title: str,
    summary: str = "",
    category: str = "財經",
    api_key: str = "",
) -> Optional[dict]:
    """
    呼叫 Groq API 分析單則新聞。
    成功回傳 dict；429 熔斷中或失敗回傳 None。
    """
    # ── 熔斷器檢查：429 冷卻中直接跳過 ───────────────────────
    if _is_rate_limited():
        remaining = int(_rate_limit_until - time.time())
        logger.info(f"Groq 熔斷中，跳過（剩餘冷卻 {remaining}s）")
        return None

    if not api_key:
        try:
            import streamlit as st
            api_key = st.secrets.get("GROQ_API_KEY", "")
        except Exception:
            pass
    if not api_key:
        import os
        api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.warning("Groq API Key 未設定，跳過 AI 分析")
        return None

    prompt = _build_prompt(title, summary, category)

    try:
        raw_text = groq_chat(prompt, api_key, timeout=40)
        result = parse_json_loose(raw_text)
        if not isinstance(result, dict):
            logger.warning("Groq 回傳不是 JSON 物件")
            return None

        required = {"sentiment", "score", "summary", "affected_tickers",
                    "reason", "confidence"}
        if not required.issubset(result.keys()):
            logger.warning(f"Groq 回傳欄位不完整：{result}")
            return None

        result["sentiment"] = result["sentiment"].lower()
        if result["sentiment"] not in ("bullish", "bearish", "neutral"):
            result["sentiment"] = "neutral"
        result["score"]      = max(-10, min(10, float(result["score"])))
        result["confidence"] = str(result["confidence"]).lower()

        logger.info(
            f"Groq 分析完成：{title[:30]}… → {result['sentiment']} ({result['score']:+.1f})"
        )

        # ── 成功後依 RPM=30 限制，每次請求間隔至少 2 秒 ──────
        time.sleep(1 if len(_all_keys(api_key)) > 1 else 2)
        return result

    except requests.exceptions.Timeout:
        logger.error("Groq API 超時")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 429:
            _set_rate_limited()   # 啟動熔斷，這批剩餘新聞全部跳過
        else:
            logger.error(f"Groq HTTP 錯誤 {status}：{e}")
    except json.JSONDecodeError as e:
        logger.error(f"Groq 回傳 JSON 解析失敗：{e}")
    except Exception as e:
        logger.error(f"Groq 分析失敗：{e}")

    return None


# ── 批次分析（帶速率控制）────────────────────────────────────────────────────
def batch_groq_analyze(
    articles: list[dict],
    api_key: str = "",
    delay: float = 0.5,
) -> list[dict]:
    """
    批次分析文章列表。
    每篇之間 delay 秒，避免觸發速率限制。
    回傳新增了 ai_* 欄位的文章列表。
    """
    results = []
    total   = len(articles)

    for i, article in enumerate(articles):
        title      = article.get("title", "")
        summary    = article.get("summary", "")
        category   = article.get("category", "財經")
        kw_score   = article.get("sentiment_score", 0.0)
        is_geo     = article.get("is_geo", False)
        has_tickers = bool(article.get("tickers"))   # [新增]

        if should_use_ai(kw_score, title, is_geo, has_tickers, category):
            ai = groq_analyze(title, summary, category, api_key)
            if ai:
                article["ai_sentiment"]        = ai["sentiment"]
                article["ai_score"]            = ai["score"]
                article["ai_summary"]          = ai["summary"]
                article["ai_affected_tickers"] = ",".join(ai.get("affected_tickers", []))
                article["ai_reason"]           = ai["reason"]
                article["ai_confidence"]       = ai["confidence"]
                logger.info(f"[{i+1}/{total}] AI 分析：{title[:25]}…")
            else:
                _clear_ai_fields(article)
            time.sleep(delay)
        else:
            _clear_ai_fields(article)

        results.append(article)

    return results


def _clear_ai_fields(article: dict) -> None:
    article["ai_sentiment"]        = ""
    article["ai_score"]            = 0.0
    article["ai_summary"]          = ""
    article["ai_affected_tickers"] = ""
    article["ai_reason"]           = ""
    article["ai_confidence"]       = ""
