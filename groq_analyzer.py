"""
groq_analyzer.py — Groq AI 深度分析模組
- 使用 Llama 3.3 70B（Groq 免費額度）
- [修改] 收窄觸發條件：只送真正有分析價值的新聞，避免每則都送導致超慢
- 輸出結構化 JSON：情緒、影響摘要、受影響個股、信心度
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL        = "llama-3.3-70b-versatile"
MAX_TOKENS   = 400
TEMPERATURE  = 0.1

# ── 全域熔斷器：429 後封鎖整批，冷卻 90 秒後才恢復 ──────────────────────────
# Groq 免費版 RPM=30（每 2 秒一次），一旦觸發 429 需等完整冷卻週期
_rate_limit_until: float = 0.0   # timestamp；在這時間之前不送任何請求
COOLDOWN_SECONDS = 90            # 冷卻時間（秒），比 60 秒再多一點保險


def _is_rate_limited() -> bool:
    return time.time() < _rate_limit_until


def _set_rate_limited() -> None:
    global _rate_limit_until
    _rate_limit_until = time.time() + COOLDOWN_SECONDS
    logger.warning(f"Groq 429：啟動熔斷，冷卻 {COOLDOWN_SECONDS} 秒後恢復")


# ── 觸發條件判斷 ──────────────────────────────────────────────────────────────
def should_use_ai(
    keyword_score: float,
    title: str,
    is_geo: bool,
    has_tickers: bool = False,
) -> bool:
    """
    判斷這則新聞是否需要送 Groq 做 AI 分析。

    設計原則：寧可少送，不要每則都送。
    送 AI 的目的是「補關鍵字判斷不足」，不是替每則新聞都加摘要。

    觸發條件（任一符合即送）：
      1. 情緒模糊帶（-0.15 ~ +0.15）：關鍵字沒把握，需要 AI
      2. 含否定詞且有強烈情緒（|score|>0.2）：否定詞可能反轉方向
      3. 地緣政治新聞：影響複雜，需要深度解讀
      4. 有個股代碼 + 強烈訊號（|score|>=0.4）：個股影響才送，避免雜訊
         （純中性個股新聞不送，節省 quota）

    不送的情況：
      - 分數明確（|score|>0.15）且無否定詞、無地緣政治、無個股 → 關鍵字夠用
      - 分數很強（|score|>0.4）但沒有個股代碼 → 影響範圍不精確，不值得送
    """
    # 條件 1：情緒模糊帶（縮小至 ±0.15，原本 ±0.20 太寬）
    if -0.15 <= keyword_score <= 0.15:
        return True

    # 條件 2：含否定詞 且 情緒有一定強度（否定詞+中性不送，節省 quota）
    negation_words = ["不", "未", "沒有", "擬", "傳", "疑", "恐"]
    if any(w in title for w in negation_words) and abs(keyword_score) > 0.20:
        return True

    # 條件 3：地緣政治（一律送，影響範圍需要 AI 解讀）
    if is_geo:
        return True

    # 條件 4：有個股代碼 且 訊號夠強（避免每個有代碼的新聞都送）
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
        resp = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  MAX_TOKENS,
                "temperature": TEMPERATURE,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        raw_text = data["choices"][0]["message"]["content"].strip()

        # 清理可能的 markdown 包裝
        if "```" in raw_text:
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        result = json.loads(raw_text)

        required = {"sentiment", "score", "summary", "affected_tickers",
                    "reason", "confidence"}
        if not required.issubset(result.keys()):
            logger.warning(f"Groq 回傳欄位不完整：{result}")
            return None

        result["sentiment"] = result["sentiment"].lower()
        if result["sentiment"] not in ("bullish", "bearish", "neutral"):
            result["sentiment"] = "neutral"
        result["score"]      = max(-10, min(10, float(result["score"])))
        result["confidence"] = result["confidence"].lower()

        logger.info(
            f"Groq 分析完成：{title[:30]}… → {result['sentiment']} ({result['score']:+.1f})"
        )

        # ── 成功後依 RPM=30 限制，每次請求間隔至少 2 秒 ──────
        time.sleep(2)
        return result

    except requests.exceptions.Timeout:
        logger.error("Groq API 超時")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else 0
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

        if should_use_ai(kw_score, title, is_geo, has_tickers):
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
