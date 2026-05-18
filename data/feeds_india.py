"""Indian-market data feeds.

Two free sources, both injectable behind a Protocol so the research agent
is testable without network:
  * Google News RSS — per-ticker headlines, refreshed every 15 min.
  * yfinance — daily / intraday close prices (NSE tickers use the `.NS` suffix).

Production feed: `GoogleNewsAndYFinanceFeed`. Test feed: `StaticIndiaFeed`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote_plus

from models.indicators import OHLCBar


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    ticker: str
    title: str
    link: str
    published: datetime
    source: str


@dataclass(frozen=True)
class PricePoint:
    ticker: str
    ts: datetime
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class DatedBar:
    """OHLC bar with a timestamp. The indicator code uses plain OHLCBar;
    this wrapper preserves the bar timestamp for the agent's bookkeeping.
    """
    ts: datetime
    bar: OHLCBar


class IndiaFeed(Protocol):
    def fetch_news(self, ticker: str, *, limit: int = 20) -> list[NewsItem]: ...
    def fetch_latest_close(self, ticker: str) -> PricePoint | None: ...
    def fetch_ohlc(self, ticker: str, *, days: int = 60) -> list[DatedBar]: ...


class GoogleNewsAndYFinanceFeed:
    """Production India feed. Lazy-imports feedparser and yfinance."""

    NEWS_URL = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

    def fetch_news(self, ticker: str, *, limit: int = 20) -> list[NewsItem]:
        try:
            import feedparser
        except ImportError as exc:
            raise RuntimeError("feedparser is required for Google News fetch") from exc
        query = quote_plus(f"{ticker} stock NSE")
        url = self.NEWS_URL.format(q=query)
        feed = feedparser.parse(url)
        out: list[NewsItem] = []
        for entry in (feed.entries or [])[:limit]:
            published = _parse_rss_time(entry.get("published_parsed"))
            out.append(NewsItem(
                ticker=ticker,
                title=entry.get("title", ""),
                link=entry.get("link", ""),
                published=published,
                source=entry.get("source", {}).get("title", "google_news"),
            ))
        return out

    def fetch_latest_close(self, ticker: str) -> PricePoint | None:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance is required for price fetch") from exc
        sym = ticker if ticker.endswith(".NS") else f"{ticker}.NS"
        hist = yf.Ticker(sym).history(period="2d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        last = hist.iloc[-1]
        return PricePoint(
            ticker=ticker,
            ts=last.name.to_pydatetime().astimezone(timezone.utc),
            close=float(last["Close"]),
            volume=float(last["Volume"]) if "Volume" in last.index else None,
        )

    def fetch_ohlc(self, ticker: str, *, days: int = 60) -> list[DatedBar]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance is required for OHLC fetch") from exc
        sym = ticker if ticker.endswith(".NS") else f"{ticker}.NS"
        # Pad the period slightly so we always have `days` usable bars after
        # weekend/holiday gaps.
        hist = yf.Ticker(sym).history(period=f"{int(days * 1.5)}d", interval="1d", auto_adjust=False)
        if hist.empty:
            return []
        out: list[DatedBar] = []
        for ts_idx, row in hist.iterrows():
            ts = ts_idx.to_pydatetime().astimezone(timezone.utc)
            out.append(DatedBar(
                ts=ts,
                bar=OHLCBar(
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0)),
                ),
            ))
        return out[-days:]


class StaticIndiaFeed:
    """Test feed."""

    def __init__(self) -> None:
        self._news: dict[str, list[NewsItem]] = {}
        self._prices: dict[str, PricePoint] = {}

    def set_news(self, ticker: str, items: list[NewsItem]) -> None:
        self._news[ticker] = items

    def set_price(self, ticker: str, close: float, *, volume: float | None = None) -> None:
        self._prices[ticker] = PricePoint(
            ticker=ticker, ts=datetime.now(timezone.utc), close=close, volume=volume
        )

    def fetch_news(self, ticker: str, *, limit: int = 20) -> list[NewsItem]:
        return list(self._news.get(ticker, []))[:limit]

    def fetch_latest_close(self, ticker: str) -> PricePoint | None:
        return self._prices.get(ticker)


def _parse_rss_time(parsed) -> datetime:
    if not parsed:
        return datetime.now(timezone.utc)
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
