"""
groq_analyzer.py — Groq AI 深度分析模組
- 使用 Llama 3.3 70B（Groq 免費額度）
- 只在關鍵字分析不確定時觸發（省 token）
- 輸出結構化 JSON：情緒、影響摘要、受影響個股、信心度
"""

import json
import logging
import time
from typing import Optional

import requests
import streamlit as st

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL        = "llama-3.3-70b-versatile"
MAX_TOKENS   = 400   # 夠用且省 quota
TEMPERATURE  = 0.1   # 盡量穩定輸出


# ── 觸發條件判斷 ──────────────────────────────────────────────────────────────
def should_use_ai(keyword_score: float, title: str, is_geo: bool) -> bool:
    """
    判斷這則新聞是否需要送 Groq 做 AI 分析。
    觸發條件（任一符合即送）：
      1. 關鍵字分數在模糊帶（-0.2 ~ +0.2）
      2. 標題含否定詞（容易誤判）
      3. 地緣政治新聞（需要深度解讀）
    """
    # 條件 1：模糊帶
    if -0.20 <= keyword_score <= 0.20:
        return True

    # 條件 2：否定詞
    negation_words = ["不", "未", "沒有", "擬", "傳", "疑", "恐", "或", "待"]
    if any(w in title for w in negation_words):
        return True

    # 條件 3：地緣政治
    if is_geo:
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
    成功回傳 dict，失敗回傳 None。
    """
    if not api_key:
        try:
            api_key = st.secrets.get("GROQ_API_KEY", "")
        except Exception:
            pass

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

        # 驗證必要欄位
        required = {"sentiment", "score", "summary", "affected_tickers",
                    "reason", "confidence"}
        if not required.issubset(result.keys()):
            logger.warning(f"Groq 回傳欄位不完整：{result}")
            return None

        # 正規化
        result["sentiment"] = result["sentiment"].lower()
        if result["sentiment"] not in ("bullish", "bearish", "neutral"):
            result["sentiment"] = "neutral"
        result["score"] = max(-10, min(10, float(result["score"])))
        result["confidence"] = result["confidence"].lower()

        logger.info(f"Groq 分析完成：{title[:30]}… → {result['sentiment']} ({result['score']:+.1f})")
        return result

    except requests.exceptions.Timeout:
        logger.error("Groq API 超時")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            logger.warning("Groq 達到速率限制，等待後重試")
            time.sleep(5)
        else:
            logger.error(f"Groq HTTP 錯誤：{e}")
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
        title    = article.get("title", "")
        summary  = article.get("summary", "")
        category = article.get("category", "財經")
        kw_score = article.get("sentiment_score", 0.0)
        is_geo   = article.get("is_geo", False)

        if should_use_ai(kw_score, title, is_geo):
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
