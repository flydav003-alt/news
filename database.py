"""
database.py — 資料庫模組
- SQLite + SQLAlchemy ORM
- 三層去重：Hash / 標題相似度 / 時間窗口
- 切換 PostgreSQL 只需改 DATABASE_URL
"""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

import pandas as pd
from sqlalchemy import (Boolean, Column, DateTime, Float,
                        Integer, String, Text, create_engine, text)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ── 資料庫路徑（Streamlit Cloud 寫入 /tmp 最穩定）────────────────────────────
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
    language        = Column(String(10), default="en")
    sentiment       = Column(String(10), default="neutral")
    sentiment_score = Column(Float, default=0.0)
    sentiment_label = Column(String(10), default="中性")
    tickers         = Column(Text, default="")
    sectors         = Column(Text, default="")
    is_geo          = Column(Boolean, default=False)   # 是否為地緣政治/戰爭新聞
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


# ── 去重工具 ──────────────────────────────────────────────────────────────────
def _make_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title.strip()}{url.strip()}".encode()).hexdigest()


def _similar(a: str, b: str) -> float:
    """計算兩個標題的相似度（0~1）"""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _is_duplicate(db: Session, title: str, url: str) -> bool:
    """
    三層去重：
    1. Hash 完全比對
    2. 標題相似度 > 0.80（6小時內）
    3. 都通過才是真正新文章
    """
    hash_id = _make_hash(title, url)

    # 第一層：Hash
    if db.query(NewsArticle).filter(NewsArticle.hash_id == hash_id).first():
        return True

    # 第二層：標題相似度（只查最近6小時）
    cutoff = datetime.utcnow() - timedelta(hours=6)
    recent = (db.query(NewsArticle.title)
               .filter(NewsArticle.fetched_at >= cutoff)
               .all())
    for (existing_title,) in recent:
        if _similar(title, existing_title) > 0.80:
            return True

    return False


def save_article(db: Session, data: dict) -> bool:
    """儲存文章，回傳 True 表示成功新增，False 表示重複跳過"""
    if _is_duplicate(db, data.get("title", ""), data.get("url", "")):
        return False

    hash_id = _make_hash(data.get("title", ""), data.get("url", ""))
    art = NewsArticle(
        hash_id         = hash_id,
        title           = data.get("title", ""),
        summary         = data.get("summary", "")[:600],
        url             = data.get("url", ""),
        source          = data.get("source", ""),
        language        = data.get("language", "en"),
        sentiment       = data.get("sentiment", "neutral"),
        sentiment_score = data.get("sentiment_score", 0.0),
        sentiment_label = data.get("sentiment_label", "中性"),
        tickers         = ",".join(data.get("tickers", [])),
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
                    sector=None, geo_only=False, limit=300) -> pd.DataFrame:
    q = db.query(NewsArticle)
    if geo_only:
        q = q.filter(NewsArticle.is_geo == True)
    if sentiment and sentiment != "all":
        q = q.filter(NewsArticle.sentiment == sentiment)
    if ticker:
        q = q.filter(NewsArticle.tickers.ilike(f"%{ticker}%"))
    if sector:
        q = q.filter(NewsArticle.sectors.ilike(f"%{sector}%"))
    rows = q.order_by(NewsArticle.published_at.desc()).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "id": r.id, "title": r.title, "summary": r.summary,
        "url": r.url, "source": r.source, "language": r.language,
        "sentiment": r.sentiment, "sentiment_score": round(r.sentiment_score, 3),
        "sentiment_label": r.sentiment_label,
        "tickers": r.tickers, "sectors": r.sectors,
        "is_geo": r.is_geo, "published_at": r.published_at,
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


def get_crawl_logs(db: Session, limit=30) -> pd.DataFrame:
    rows = db.query(CrawlLog).order_by(CrawlLog.executed_at.desc()).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "來源": r.source, "狀態": r.status, "抓取": r.count,
        "新增": r.new_saved, "跳過": r.skipped,
        "時間": r.executed_at,
    } for r in rows])
