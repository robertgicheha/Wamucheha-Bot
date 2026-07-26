"""
News sentiment with multi-source support and caching.

Uses NewsAPI for headlines and VADER for sentiment scoring. Adds:
- In-memory cache to avoid re-fetching the same queries.
- Multi-query support (company name + ticker + sector keywords).
- Rate limit awareness (NewsAPI free tier: 100 req/day).
"""
import os
import time
import logging

import requests

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _analyzer = SentimentIntensityAnalyzer()
except ImportError:
    _analyzer = None

logger = logging.getLogger("news_sentiment")


class NewsSentiment:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("NEWSAPI_KEY")
        self._cache = {}
        self._cache_ttl = 1800  # 30 minutes
        self._request_count = 0
        self._daily_limit = 90  # stay under 100/day free tier

    def get_headlines(self, ticker_or_company: str, limit: int = 10) -> list:
        if not self.api_key:
            return []

        cache_key = f"{ticker_or_company}:{limit}"
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return data

        if self._request_count >= self._daily_limit:
            logger.warning("NewsAPI daily limit approaching — skipping request")
            return []

        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": ticker_or_company,
                    "sortBy": "publishedAt",
                    "language": "en",
                    "pageSize": limit,
                    "apiKey": self.api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            self._request_count += 1
            articles = resp.json().get("articles", [])
            headlines = [{"title": a["title"], "url": a["url"], "source": a["source"]["name"],
                          "published_at": a["publishedAt"]} for a in articles]
            self._cache[cache_key] = (time.time(), headlines)
            return headlines
        except Exception as e:
            logger.error(f"NewsAPI error for {ticker_or_company}: {e}")
            return []

    def score_headlines(self, headlines: list) -> dict:
        if not headlines:
            return {"avg_sentiment": None, "headlines": []}
        if _analyzer is None:
            return {"avg_sentiment": None, "headlines": headlines,
                     "note": "vaderSentiment not installed."}

        scored = []
        total = 0
        for h in headlines:
            compound = _analyzer.polarity_scores(h["title"])["compound"]
            scored.append({**h, "sentiment": round(compound, 3)})
            total += compound

        avg = total / len(scored)
        return {"avg_sentiment": round(avg, 3), "headlines": scored}

    def get_sentiment(self, ticker_or_company: str, limit: int = 10) -> dict:
        headlines = self.get_headlines(ticker_or_company, limit)
        result = self.score_headlines(headlines)
        result["ticker"] = ticker_or_company
        if result.get("avg_sentiment") is not None:
            if result["avg_sentiment"] > 0.2:
                result["label"] = "positive"
            elif result["avg_sentiment"] < -0.2:
                result["label"] = "negative"
            else:
                result["label"] = "neutral"
        else:
            result["label"] = "unknown"
        return result

    def get_multi_query_sentiment(self, queries: list, limit: int = 5) -> dict:
        """Aggregate sentiment across multiple queries (e.g. ticker + company name)."""
        all_headlines = []
        for q in queries:
            all_headlines.extend(self.get_headlines(q, limit))
        # Deduplicate by title
        seen = set()
        unique = []
        for h in all_headlines:
            if h["title"] not in seen:
                seen.add(h["title"])
                unique.append(h)
        result = self.score_headlines(unique[:limit * 2])
        result["queries"] = queries
        if result.get("avg_sentiment") is not None:
            if result["avg_sentiment"] > 0.2:
                result["label"] = "positive"
            elif result["avg_sentiment"] < -0.2:
                result["label"] = "negative"
            else:
                result["label"] = "neutral"
        else:
            result["label"] = "unknown"
        return result
