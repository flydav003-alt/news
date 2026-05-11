"""
analyzer.py — 分析模組
- 中英文情緒分析（利多/利空/中性）
- 台股4碼 + 美股代碼提取
- 類股對照（含戰爭/地緣政治板塊）
- 地緣政治新聞自動標記
"""

import re
from dataclasses import dataclass, field
from typing import Optional

try:
    import jieba
    jieba.setLogLevel("ERROR")
    JIEBA_OK = True
except ImportError:
    JIEBA_OK = False


# ── 情緒詞典 ──────────────────────────────────────────────────────────────────
BULLISH: dict[str, float] = {
    # 中文利多
    "大漲": 1.0, "漲停": 1.0, "創高": 0.9, "突破": 0.8, "超預期": 0.9,
    "優於預期": 0.8, "獲利": 0.7, "亮眼": 0.7, "旺季": 0.6, "擴產": 0.7,
    "訂單暴增": 0.9, "受惠": 0.6, "轉機": 0.7, "業績成長": 0.8,
    "大單": 0.7, "需求強勁": 0.7, "上調目標價": 0.7, "買進": 0.6,
    "看好": 0.6, "利多": 0.8, "獲利創新高": 1.0, "業績大增": 0.9,
    "停火": 0.6, "和談": 0.6, "協議": 0.5, "降息": 0.6,
    # 英文利多
    "beat": 0.8, "surpass": 0.8, "record high": 1.0, "rally": 0.7,
    "upgrade": 0.7, "outperform": 0.8, "strong demand": 0.7,
    "bullish": 0.8, "profit": 0.6, "growth": 0.6, "surge": 0.8,
    "soar": 0.8, "jumped": 0.7, "ceasefire": 0.6, "peace": 0.5,
    "deal": 0.5, "agreement": 0.5, "rate cut": 0.6,
}

BEARISH: dict[str, float] = {
    # 中文利空
    "大跌": -1.0, "跌停": -1.0, "虧損": -0.9, "下修": -0.8,
    "低於預期": -0.8, "裁員": -0.7, "召回": -0.7, "訴訟": -0.6,
    "罰款": -0.7, "砍單": -0.9, "需求疲軟": -0.7, "庫存壓力": -0.7,
    "利空": -0.8, "虧損擴大": -0.9, "下調目標價": -0.7,
    "賣出": -0.6, "看壞": -0.6, "破產": -1.0, "下市": -1.0,
    # 戰爭/地緣政治利空
    "戰爭": -0.8, "開戰": -1.0, "轟炸": -0.9, "導彈": -0.8,
    "入侵": -0.9, "制裁": -0.7, "禁運": -0.7, "衝突": -0.6,
    "封鎖": -0.7, "核武": -0.9, "升息": -0.5, "通膨": -0.4,
    # 英文利空
    "miss": -0.8, "below expectations": -0.8, "layoff": -0.7,
    "recall": -0.7, "lawsuit": -0.6, "bearish": -0.8,
    "downgrade": -0.7, "loss": -0.7, "decline": -0.5,
    "drop": -0.6, "recession": -0.8, "tariff": -0.5,
    "war": -0.8, "attack": -0.9, "invasion": -0.9,
    "missile": -0.8, "sanction": -0.7, "airstrike": -0.9,
    "embargo": -0.7, "nuclear": -0.7, "conflict": -0.6,
}

NEGATION = {"不", "沒有", "未", "無", "非", "not", "no", "without", "never"}


# ── 地緣政治 / 戰爭關鍵字 ─────────────────────────────────────────────────────
GEO_KEYWORDS = {
    # 中文
    "戰爭", "開戰", "轟炸", "導彈", "飛彈", "入侵", "衝突", "制裁",
    "禁運", "停火", "和談", "台海", "南海", "中東", "俄烏", "以巴",
    "北韓", "核武", "地緣政治", "貿易戰", "封鎖", "軍事",
    # 英文
    "war", "warfare", "attack", "airstrike", "missile", "invasion",
    "ceasefire", "sanction", "embargo", "geopolitical", "Taiwan Strait",
    "Middle East", "Ukraine", "Russia", "NATO", "nuclear", "military",
    "conflict", "Gaza", "Israel", "Iran", "North Korea", "trade war",
}


# ── 類股對照 ──────────────────────────────────────────────────────────────────
SECTOR_KW: dict[str, list[str]] = {
    # 半導體
    "台積電": ["半導體"], "聯發科": ["半導體"], "晶片": ["半導體"],
    "半導體": ["半導體"], "CoWoS": ["半導體"], "HBM": ["半導體"],
    "封裝": ["半導體"], "晶圓": ["半導體"],
    "TSMC": ["半導體"], "NVDA": ["半導體", "AI"],
    "AMD": ["半導體", "AI"], "INTC": ["半導體"],
    "QCOM": ["半導體"], "AVGO": ["半導體"],
    # AI / 雲端
    "AI": ["AI"], "人工智慧": ["AI"], "ChatGPT": ["AI"],
    "資料中心": ["AI"], "雲端": ["AI"], "大語言模型": ["AI"],
    "MSFT": ["AI"], "Microsoft": ["AI"], "Google": ["AI"],
    "AMZN": ["AI"], "META": ["AI"],
    # 電動車
    "特斯拉": ["電動車"], "Tesla": ["電動車"], "TSLA": ["電動車"],
    "電動車": ["電動車"], "EV": ["電動車"], "充電樁": ["電動車"],
    "NIO": ["電動車"], "RIVN": ["電動車"],
    # 綠能
    "太陽能": ["綠能"], "風電": ["綠能"], "儲能": ["綠能"],
    "綠能": ["綠能"], "renewable": ["綠能"], "ENPH": ["綠能"],
    # 金融
    "聯準會": ["金融"], "央行": ["金融"], "Fed": ["金融"],
    "升息": ["金融"], "降息": ["金融"], "利率": ["金融"],
    "通膨": ["金融"], "inflation": ["金融"], "FOMC": ["金融"],
    "JPM": ["金融"], "GS": ["金融"], "BAC": ["金融"],
    # 傳產
    "航運": ["傳產"], "鋼鐵": ["傳產"], "石化": ["傳產"],
    "AAPL": ["傳產"], "Apple": ["傳產"],
    # 戰爭 / 國防
    "國防": ["戰爭/國防"], "軍事": ["戰爭/國防"], "武器": ["戰爭/國防"],
    "defense": ["戰爭/國防"], "military": ["戰爭/國防"],
    "LMT": ["戰爭/國防"], "RTX": ["戰爭/國防"],
    "NOC": ["戰爭/國防"], "GD": ["戰爭/國防"],
    # 地緣政治
    "地緣政治": ["地緣政治"], "制裁": ["地緣政治"],
    "貿易戰": ["地緣政治"], "台海": ["地緣政治"],
    "geopolitical": ["地緣政治"], "sanction": ["地緣政治"],
    "tariff": ["地緣政治"],
    # 能源
    "油價": ["能源"], "原油": ["能源"], "天然氣": ["能源"],
    "OPEC": ["能源"], "XOM": ["能源"], "CVX": ["能源"],
}

# 美股白名單
US_WHITELIST = {
    "NVDA", "AMD", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "INTC", "TSMC", "QCOM", "AVGO", "ARM", "MU", "ASML",
    "NFLX", "DIS", "JPM", "GS", "BAC", "WFC",
    "NIO", "RIVN", "ENPH", "FSLR", "NEE",
    "LMT", "RTX", "NOC", "GD",
    "XOM", "CVX", "QQQ", "SPY", "GLD", "TLT",
    "OPEC", "NATO", "FED",
}


@dataclass
class Result:
    sentiment:       str   = "neutral"
    sentiment_score: float = 0.0
    sentiment_label: str   = "中性"
    tickers:         list  = field(default_factory=list)
    sectors:         list  = field(default_factory=list)
    is_geo:          bool  = False


class Analyzer:
    def __init__(self, extra_bullish: dict = None, extra_bearish: dict = None):
        self.bull = {**BULLISH,  **(extra_bullish or {})}
        self.bear = {**BEARISH,  **(extra_bearish or {})}

    def analyze(self, title: str, summary: str = "", language: str = "en") -> Result:
        text = f"{title} {summary}"
        tickers = self._tickers(text)
        sectors = self._sectors(text, tickers)
        is_geo  = self._is_geo(text)
        score   = self._score(text, language)
        sent, label = self._classify(score)
        return Result(
            sentiment       = sent,
            sentiment_score = round(score, 3),
            sentiment_label = label,
            tickers         = tickers,
            sectors         = list(dict.fromkeys(sectors)),
            is_geo          = is_geo,
        )

    def _tickers(self, text: str) -> list:
        out = []
        for m in re.finditer(r"\b(\d{4})\b", text):
            if 1000 <= int(m.group(1)) <= 9999:
                out.append(m.group(1))
        for m in re.finditer(r"\b([A-Z]{1,5})\b", text):
            if m.group(1) in US_WHITELIST and m.group(1) not in out:
                out.append(m.group(1))
        return list(dict.fromkeys(out))

    def _sectors(self, text: str, tickers: list) -> list:
        sectors = []
        for kw, secs in SECTOR_KW.items():
            if kw in text:
                sectors.extend(secs)
        return sectors

    def _is_geo(self, text: str) -> bool:
        tl = text.lower()
        return any(kw.lower() in tl for kw in GEO_KEYWORDS)

    def _score(self, text: str, language: str) -> float:
        tokens = list(jieba.cut(text)) if (language == "zh" and JIEBA_OK) else text.split()
        score, negate = 0.0, False
        for i, tok in enumerate(tokens):
            tl = tok.lower()
            if tl in NEGATION:
                negate = True
                continue
            w = self.bull.get(tok, self.bull.get(tl,
                self.bear.get(tok, self.bear.get(tl, 0.0))))
            if i + 1 < len(tokens):
                bigram = tok + tokens[i + 1]
                w2 = self.bull.get(bigram, self.bear.get(bigram, 0.0))
                if w2:
                    w = w2
            if w:
                score += (-w if negate else w)
            negate = False
        return max(-1.0, min(1.0, score))

    @staticmethod
    def _classify(score: float):
        if score >= 0.15:
            return "bullish", "利多"
        if score <= -0.15:
            return "bearish", "利空"
        return "neutral", "中性"
