"""
News sentiment for a ticker. Uses NewsAPI for headline retrieval and a lexicon-based
sentiment score (VADER) rather than a heavyweight LLM call per headline — cheap,
fast, deterministic, and good enough to flag "something significant happened,"
which is the actual job here: flagging events for you to read, not replacing your
judgment on them.

This deliberately surfaces headlines + links, not a synthesized "the news says X"
paragraph — you should read the actual articles for anything that will influence
a real trade or investment decision, not trust a one-line auto-summary of it.
"""
import os
import requests

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _analyzer = SentimentIntensityAnalyzer()
except ImportError:
    _analyzer = None


class NewsSentiment:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("NEWSAPI_KEY")

    def get_headlines(self, ticker_or_company: str, limit: int = 10) -> list:
        if not self.api_key:
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
            articles = resp.json().get("articles", [])
            return [{"title": a["title"], "url": a["url"], "source": a["source"]["name"],
                      "published_at": a["publishedAt"]} for a in articles]
        except Exception:
            return []

    def score_headlines(self, headlines: list) -> dict:
        """Returns aggregate sentiment: -1 (very negative) to +1 (very positive),
        plus the individual scored headlines so you can see what drove the number."""
        if not headlines:
            return {"avg_sentiment": None, "headlines": []}
        if _analyzer is None:
            return {"avg_sentiment": None, "headlines": headlines,
                     "note": "vaderSentiment not installed — showing headlines without scores. "
                             "pip install vaderSentiment to enable."}

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
