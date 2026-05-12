"""
database.py — 資料庫模組
- SQLite + SQLAlchemy ORM
- 三層去重：Hash / 標題相似度 / 時間窗口
- 切換 PostgreSQL 只需改 DATABASE_URL
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

import pandas as pd
from sqlalchemy import (Boolean, Column, DateTime, Float,
                        Integer, String, Text, create_engine, text)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ── 資料庫路徑 ────────────────────────────────────────────────────────────────
DB_DIR = "/tmp" if os.path.exists("/tmp") else "data"
os.makedirs(DB_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_DIR}/finnews.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class NewsArticle(Base):
    __tablename__ = "news_articles"
    id              = Column(Integer, primary_key=True, index=True)
    hash_id         = Column(String(64), unique=True, index=True)
    title           = Column(Text, nullable=False)
    summary         = Column(Text, default="")
    url             = Column(Text, default="")
    source          = Column(String(100), default="")
    language        = Column(String(10), default="zh")
    category        = Column(String(50), default="財經")
    sentiment       = Column(String(10), default="neutral")
    sentiment_score = Column(Float, default=0.0)
    sentiment_label = Column(String(10), default="中性")
    tickers         = Column(Text, default="")      # 逗號分隔代碼
    ticker_details  = Column(Text, default="")      # JSON: [{code, name, market}]
    sectors         = Column(Text, default="")
    is_geo          = Column(Boolean, default=False)
    published_at    = Column(DateTime, default=datetime.utcnow)
    fetched_at      = Column(DateTime, default=datetime.utcnow)


class CrawlLog(Base):
    __tablename__ = "crawl_logs"
    id          = Column(Integer, primary_key=True)
    source      = Column(String(100))
    status      = Column(String(20))
    count       = Column(Integer, default=0)
    new_saved   = Column(Integer, default=0)
    skipped     = Column(Integer, default=0)
    executed_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # 如果舊資料庫缺少新欄位，自動補上
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE news_articles ADD COLUMN ticker_details TEXT DEFAULT ''"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE news_articles ADD COLUMN category TEXT DEFAULT '財經'"))
            conn.commit()
        except Exception:
            pass


# ── 去重工具 ──────────────────────────────────────────────────────────────────
def _make_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title.strip()}{url.strip()}".encode()).hexdigest()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _is_duplicate(db: Session, title: str, url: str) -> bool:
    hash_id = _make_hash(title, url)
    if db.query(NewsArticle).filter(NewsArticle.hash_id == hash_id).first():
        return True
    cutoff = datetime.utcnow() - timedelta(hours=6)
    recent = (db.query(NewsArticle.title)
               .filter(NewsArticle.fetched_at >= cutoff)
               .all())
    for (existing_title,) in recent:
        if _similar(title, existing_title) > 0.82:
            return True
    return False


def save_article(db: Session, data: dict) -> bool:
    if _is_duplicate(db, data.get("title", ""), data.get("url", "")):
        return False

    hash_id = _make_hash(data.get("title", ""), data.get("url", ""))

    # ticker_details 序列化
    td = data.get("ticker_details", [])
    if td and hasattr(td[0], "__dataclass_fields__"):
        td_json = json.dumps([
            {"code": t.code, "name": t.name, "market": t.market}
            for t in td
        ], ensure_ascii=False)
    elif isinstance(td, list) and td and isinstance(td[0], dict):
        td_json = json.dumps(td, ensure_ascii=False)
    else:
        td_json = "[]"

    art = NewsArticle(
        hash_id         = hash_id,
        title           = data.get("title", ""),
        summary         = data.get("summary", "")[:600],
        url             = data.get("url", ""),
        source          = data.get("source", ""),
        language        = data.get("language", "zh"),
        category        = data.get("category", "財經"),
        sentiment       = data.get("sentiment", "neutral"),
        sentiment_score = data.get("sentiment_score", 0.0),
        sentiment_label = data.get("sentiment_label", "中性"),
        tickers         = ",".join(data.get("tickers", [])),
        ticker_details  = td_json,
        sectors         = ",".join(data.get("sectors", [])),
        is_geo          = data.get("is_geo", False),
        published_at    = data.get("published_at", datetime.utcnow()),
        fetched_at      = datetime.utcnow(),
    )
    db.add(art)
    db.commit()
    return True


def log_crawl(db: Session, source: str, status: str,
              count=0, new_saved=0, skipped=0):
    db.add(CrawlLog(source=source, status=status,
                    count=count, new_saved=new_saved, skipped=skipped))
    db.commit()


# ── 查詢 ──────────────────────────────────────────────────────────────────────
def get_articles_df(db: Session, sentiment=None, ticker=None,
                    sector=None, geo_only=False,
                    category=None, keyword=None, limit=300) -> pd.DataFrame:
    q = db.query(NewsArticle)
    if geo_only:
        q = q.filter(NewsArticle.is_geo == True)
    if sentiment and sentiment != "all":
        q = q.filter(NewsArticle.sentiment == sentiment)
    if ticker:
        q = q.filter(NewsArticle.tickers.ilike(f"%{ticker}%") |
                     NewsArticle.ticker_details.ilike(f"%{ticker}%"))
    if sector:
        q = q.filter(NewsArticle.sectors.ilike(f"%{sector}%"))
    if category:
        q = q.filter(NewsArticle.category == category)
    if keyword:
        q = q.filter(NewsArticle.title.ilike(f"%{keyword}%") |
                     NewsArticle.summary.ilike(f"%{keyword}%"))
    rows = q.order_by(NewsArticle.published_at.desc()).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "id":             r.id,
        "title":          r.title,
        "summary":        r.summary,
        "url":            r.url,
        "source":         r.source,
        "language":       r.language,
        "category":       r.category or "財經",
        "sentiment":      r.sentiment,
        "sentiment_score": round(r.sentiment_score, 3),
        "sentiment_label": r.sentiment_label,
        "tickers":        r.tickers,
        "ticker_details": r.ticker_details or "[]",
        "sectors":        r.sectors,
        "is_geo":         r.is_geo,
        "published_at":   r.published_at,
    } for r in rows])


def get_sentiment_counts(db: Session) -> dict:
    result = {"bullish": 0, "bearish": 0, "neutral": 0}
    for row in db.execute(text(
        "SELECT sentiment, COUNT(*) FROM news_articles GROUP BY sentiment"
    )).fetchall():
        if row[0] in result:
            result[row[0]] = row[1]
    return result


def get_sector_counts(db: Session) -> pd.DataFrame:
    rows = db.query(NewsArticle.sectors).all()
    counter = {}
    for (s,) in rows:
        for sec in (s or "").split(","):
            sec = sec.strip()
            if sec:
                counter[sec] = counter.get(sec, 0) + 1
    if not counter:
        return pd.DataFrame(columns=["sector", "count"])
    df = pd.DataFrame(list(counter.items()), columns=["sector", "count"])
    return df.sort_values("count", ascending=False).reset_index(drop=True)


def get_ticker_counts(db: Session, limit=20) -> pd.DataFrame:
    """統計最常出現的股票代碼"""
    rows = db.query(NewsArticle.ticker_details, NewsArticle.sentiment_score).all()
    counter: dict[str, dict] = {}
    for (td_json, score) in rows:
        try:
            items = json.loads(td_json or "[]")
        except Exception:
            continue
        for item in items:
            code = item.get("code", "")
            name = item.get("name", code)
            market = item.get("market", "TW")
            if not code:
                continue
            if code not in counter:
                counter[code] = {"code": code, "name": name,
                                 "market": market, "count": 0, "score_sum": 0.0}
            counter[code]["count"] += 1
            counter[code]["score_sum"] += score or 0.0
    if not counter:
        return pd.DataFrame(columns=["code", "name", "market", "count", "avg_score"])
    rows_out = []
    for v in counter.values():
        rows_out.append({
            "代碼":   v["code"],
            "名稱":   v["name"],
            "市場":   v["market"],
            "出現次數": v["count"],
            "平均情緒": round(v["score_sum"] / v["count"], 3) if v["count"] else 0.0,
        })
    df = pd.DataFrame(rows_out).sort_values("出現次數", ascending=False)
    return df.head(limit).reset_index(drop=True)


def get_crawl_logs(db: Session, limit=30) -> pd.DataFrame:
    rows = db.query(CrawlLog).order_by(CrawlLog.executed_at.desc()).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "來源": r.source, "狀態": r.status, "抓取": r.count,
        "新增": r.new_saved, "跳過": r.skipped,
        "時間": r.executed_at,
    } for r in rows])
