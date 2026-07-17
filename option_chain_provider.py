"""news_agent.py — the News & Sentiment Engine.

Application-layer orchestrator, structured the same way as
market_agent.py and fii_dii_agent.py: one file, internally separated
into single-responsibility classes so the clean-architecture boundaries
(fetch -> parse -> analyze -> validate -> serialize) are enforced by
class design even though they live in one module. No order execution
of any kind happens here -- this is read-only news/sentiment data for a
paper trading system.

Pipeline:
    1. NewsApiProvider (primary) -- NewsAPI.org, requires NEWSAPI_KEY.
    2. GoogleNewsRssProvider (fallback) -- used automatically if NewsAPI
       is unconfigured or fails, no API key required.
    3. EconomicCalendarProvider -- forex/economic calendar events.
    4. PolicyNewsDetector -- flags RBI/Fed monetary policy articles.
    5. SentimentAnalyzer -- rule-based Bullish/Bearish/Neutral scoring.
    6. ImpactClassifier -- HIGH/MEDIUM/LOW market-impact grading.
    7. NewsAgent -- orchestrates the above, validates results, and
       serializes to the structured JSON contract ai_brain.py consumes.

Zero third-party dependencies: HTTP via urllib, RSS via xml.etree, same
as the Phase 2 NSE adapters. Network calls reuse the existing
retry_on_network_failure decorator rather than re-implementing backoff.

SOLID notes (same discipline as market_agent.py / fii_dii_agent.py):
  - SRP: fetching (NewsApiProvider / GoogleNewsRssProvider /
    EconomicCalendarProvider), policy detection (PolicyNewsDetector),
    sentiment scoring (SentimentAnalyzer), impact grading
    (ImpactClassifier), and serialization (NewsJsonSerializer) are all
    separate classes; NewsAgent only orchestrates them.
  - OCP: swapping the sentiment engine for an LLM-backed classifier, or
    adding a new news source, means adding/injecting a new class, not
    editing NewsAgent's orchestration methods.
  - LSP: NewsAgent depends only on the narrow `fetch(...)` contracts its
    providers expose; any object honoring those contracts (e.g. a test
    double) is a drop-in substitute.
  - ISP: each helper class exposes only the narrow method(s) its one
    job needs.
  - DIP: every dependency is constructor-injected, with sensible
    defaults built from environment configuration; NewsAgent never
    hardcodes a concrete provider inline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataUnavailableError,
)
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------- #
# Enums
# ---------------------------------------------------------------------- #
class SentimentLabel(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class NewsImpact(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# ---------------------------------------------------------------------- #
# Exceptions (a separate hierarchy from market data -- news failures
# should never be caught by a handler written for market-data errors,
# or vice versa)
# ---------------------------------------------------------------------- #
class NewsDataError(Exception):
    """Base class for all news/sentiment errors."""


class NewsValidationError(NewsDataError):
    """Raised when incoming news/calendar data fails a sanity check."""


class NewsUnavailableError(NewsDataError):
    """Raised when no configured news source could be reached."""


# ---------------------------------------------------------------------- #
# Domain-ish entities (validated at construction, same pattern as the
# Phase 1/2 entities)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class NewsArticle:
    """One news headline, enriched with sentiment/policy/impact analysis."""

    title: str
    source: str
    url: str
    published_at: datetime
    description: str = ""
    sentiment: SentimentLabel = SentimentLabel.NEUTRAL
    impact: NewsImpact = NewsImpact.LOW
    is_policy_news: bool = False
    matched_policy_keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise NewsValidationError("NewsArticle.title must not be empty.")
        if not self.source.strip():
            raise NewsValidationError("NewsArticle.source must not be empty.")
        if not self.url.strip():
            raise NewsValidationError("NewsArticle.url must not be empty.")


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    """One economic calendar event (e.g. RBI rate decision, US CPI)."""

    event_name: str
    country: str
    importance: NewsImpact
    event_time: datetime | None = None
    raw_date_text: str = ""
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None

    def __post_init__(self) -> None:
        if not self.event_name.strip():
            raise NewsValidationError("EconomicEvent.event_name must not be empty.")
        if not self.country.strip():
            raise NewsValidationError("EconomicEvent.country must not be empty.")


def _compile_keyword_pattern(keywords: frozenset[str] | tuple[str, ...]) -> re.Pattern[str]:
    """Compile a set/tuple of keywords/phrases into one case-sensitive-off
    regex with word boundaries around each, so matching never fires on a
    keyword that merely appears as a substring of an unrelated word (e.g.
    "war" inside "award", "gain" inside "bargain", "fall" inside
    "shortfall"). Multi-word phrases like "record high" still match
    correctly since \\b anchors sit at the start/end of the whole phrase.
    """
    escaped = sorted((re.escape(kw.strip()) for kw in keywords if kw.strip()), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


# ---------------------------------------------------------------------- #
# Sentiment analysis (SRP: isolated, deterministic, swappable)
# ---------------------------------------------------------------------- #
class SentimentAnalyzer:
    """Rule-based Bullish/Bearish/Neutral sentiment scorer.

    Deliberately dependency-free (no external ML/NLP library, no live
    LLM call) so this module adds zero new third-party requirements and
    stays fully deterministic and unit-testable. This is a first-pass
    signal; swapping in an LLM-backed or ML-based classifier later is a
    drop-in replacement -- callers only depend on `analyze(text) ->
    SentimentLabel`, never on how the score is computed.

    Keywords are matched on word boundaries (via regex \\b), not naive
    substrings -- a naive `"war" in text.lower()` check would wrongly
    fire on "award", "software", "warehouse", etc. Multi-word phrases
    (e.g. "record high") still match correctly since \\b anchors sit at
    the start/end of the whole phrase.
    """

    _BULLISH_KEYWORDS: frozenset[str] = frozenset(
        {
            "rally", "rallies", "surge", "surges", "record high", "beats estimates",
            "upgrade", "upgraded", "bullish", "gains", "gain", "rebound", "rebounds",
            "outperform", "strong growth", "buy rating", "positive", "rate cut",
            "stimulus", "boost", "boosts", "soar", "soars", "jumps", "jump",
            "all-time high", "recovery", "optimism", "outperforms",
        }
    )
    _BEARISH_KEYWORDS: frozenset[str] = frozenset(
        {
            "crash", "crashes", "plunge", "plunges", "selloff", "sell-off",
            "downgrade", "downgraded", "bearish", "recession", "slowdown",
            "rate hike", "inflation surge", "default", "bankruptcy", "layoffs",
            "miss estimates", "sell rating", "negative", "correction", "collapse",
            "tumble", "tumbles", "warns", "warning", "slump", "slumps", "losses",
            "falls", "fall", "plummet", "plummets",
        }
    )

    def __init__(self) -> None:
        self._bullish_pattern = _compile_keyword_pattern(self._BULLISH_KEYWORDS)
        self._bearish_pattern = _compile_keyword_pattern(self._BEARISH_KEYWORDS)

    def analyze(self, text: str) -> SentimentLabel:
        lowered = text.lower()
        bullish_hits = len(self._bullish_pattern.findall(lowered))
        bearish_hits = len(self._bearish_pattern.findall(lowered))
        if bullish_hits > bearish_hits:
            return SentimentLabel.BULLISH
        if bearish_hits > bullish_hits:
            return SentimentLabel.BEARISH
        return SentimentLabel.NEUTRAL


# ---------------------------------------------------------------------- #
# RBI/Fed policy detection (SRP: isolated, keyword-based)
# ---------------------------------------------------------------------- #
class PolicyNewsDetector:
    """Flags articles referencing RBI or US Federal Reserve monetary
    policy actions, and reports which keywords matched.

    Uses word-boundary regex matching (see _compile_keyword_pattern) so
    e.g. "fed" matches the standalone word "Fed" without also matching
    inside "federal" or "confederate".
    """

    _POLICY_KEYWORDS: tuple[str, ...] = (
        "rbi", "reserve bank of india", "repo rate", "monetary policy committee",
        "mpc", "federal reserve", "fed", "fomc", "interest rate", "rate hike",
        "rate cut", "jerome powell", "shaktikanta das", "quantitative easing",
        "tapering", "policy rate", "central bank",
    )

    def __init__(self) -> None:
        self._pattern = _compile_keyword_pattern(self._POLICY_KEYWORDS)

    def detect(self, text: str) -> tuple[bool, tuple[str, ...]]:
        lowered = text.lower()
        matched = tuple(dict.fromkeys(self._pattern.findall(lowered)))
        return (bool(matched), matched)


# ---------------------------------------------------------------------- #
# Market-impact classification (SRP: isolated, keyword-based)
# ---------------------------------------------------------------------- #
class ImpactClassifier:
    """Grades an article's likely market impact as HIGH/MEDIUM/LOW.

    Uses word-boundary regex matching (see _compile_keyword_pattern) so
    e.g. "war" matches the standalone word "war" without also matching
    inside "award", "software", "warehouse", "hardware", etc.
    """

    _HIGH_IMPACT_KEYWORDS: frozenset[str] = frozenset(
        {
            "rate hike", "rate cut", "war", "default", "bankruptcy", "crash",
            "recession", "fomc", "emergency", "sanctions", "credit rating",
            "downgrade", "black swan", "circuit breaker",
        }
    )
    _MEDIUM_IMPACT_KEYWORDS: frozenset[str] = frozenset(
        {
            "earnings", "gdp", "inflation", "unemployment", "tariff", "election",
            "budget", "cpi", "ppi", "jobs report", "trade deficit", "ipo",
        }
    )

    def __init__(self) -> None:
        self._high_pattern = _compile_keyword_pattern(self._HIGH_IMPACT_KEYWORDS)
        self._medium_pattern = _compile_keyword_pattern(self._MEDIUM_IMPACT_KEYWORDS)

    def classify(self, text: str, is_policy_news: bool) -> NewsImpact:
        lowered = text.lower()
        if is_policy_news or self._high_pattern.search(lowered):
            return NewsImpact.HIGH
        if self._medium_pattern.search(lowered):
            return NewsImpact.MEDIUM
        return NewsImpact.LOW

    @staticmethod
    def from_calendar_impact_text(raw_impact: str | None) -> NewsImpact:
        """Maps a calendar feed's own impact label (e.g. ForexFactory's
        'High'/'Medium'/'Low'/'Holiday') onto NewsImpact."""
        normalized = (raw_impact or "").strip().lower()
        if normalized in ("high", "red"):
            return NewsImpact.HIGH
        if normalized in ("medium", "med", "orange", "yellow"):
            return NewsImpact.MEDIUM
        return NewsImpact.LOW


# ---------------------------------------------------------------------- #
# NewsAPI.org provider (primary source)
# ---------------------------------------------------------------------- #
class NewsApiProvider:
    """Fetches headlines from NewsAPI.org's /v2/everything endpoint.

    Requires an API key (constructor arg or NEWSAPI_KEY env var). If no
    key is configured, `is_configured()` returns False so NewsAgent can
    skip straight to the Google News RSS fallback without wasting a
    network round trip on a call guaranteed to fail auth.
    """

    _BASE_URL = "https://newsapi.org/v2/everything"

    def __init__(self, api_key: str | None = None, timeout_seconds: float = 10.0) -> None:
        self._api_key = api_key or os.environ.get("NEWSAPI_KEY")
        self._timeout = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._api_key)

    @retry_on_network_failure(max_attempts=3)
    def _fetch_raw(self, query: str, page_size: int) -> dict:
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": str(page_size),
            "apiKey": self._api_key,
        }
        url = f"{self._BASE_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "TitanAITrader/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"NewsAPI returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"NewsAPI unreachable: {exc.reason}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise NewsValidationError("NewsAPI response was not valid JSON.") from exc

    def fetch(self, query: str, page_size: int = 20) -> list[dict]:
        if not self.is_configured():
            raise NewsUnavailableError("NEWSAPI_KEY is not configured.")
        try:
            payload = self._fetch_raw(query, page_size)
        except MarketDataUnavailableError as exc:
            raise NewsUnavailableError(str(exc)) from exc

        if payload.get("status") != "ok":
            raise NewsUnavailableError(
                f"NewsAPI error: {payload.get('message', 'unknown error')}"
            )
        return payload.get("articles", [])


# ---------------------------------------------------------------------- #
# Google News RSS provider (no-API-key fallback)
# ---------------------------------------------------------------------- #
class GoogleNewsRssProvider:
    """Fetches headlines from Google News' public RSS search feed.

    Used automatically when NewsApiProvider is unconfigured or fails.
    Requires no API key.
    """

    _BASE_URL = "https://news.google.com/rss/search"

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    @retry_on_network_failure(max_attempts=3)
    def _fetch_raw(self, query: str) -> bytes:
        params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
        url = f"{self._BASE_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 TitanAITrader/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"Google News RSS returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Google News RSS unreachable: {exc.reason}") from exc

    def fetch(self, query: str, max_items: int = 20) -> list[dict]:
        try:
            body = self._fetch_raw(query)
        except MarketDataUnavailableError as exc:
            raise NewsUnavailableError(str(exc)) from exc

        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise NewsValidationError("Google News RSS response was not valid XML.") from exc

        items: list[dict] = []
        for item in root.findall("./channel/item")[:max_items]:
            source_el = item.find("source")
            items.append(
                {
                    "title": (item.findtext("title") or "").strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "pubDate": (item.findtext("pubDate") or "").strip(),
                    "description": (item.findtext("description") or "").strip(),
                    "source": (
                        source_el.text.strip()
                        if source_el is not None and source_el.text
                        else "Google News"
                    ),
                }
            )
        return items


# ---------------------------------------------------------------------- #
# Economic calendar provider
# ---------------------------------------------------------------------- #
class EconomicCalendarProvider:
    """Fetches this week's economic calendar events (no API key required).

    Uses a public, unauthenticated JSON feed of economic calendar events
    (the same one many open-source trading tools use). Rows are filtered
    to the requested countries; malformed rows are skipped individually
    rather than failing the whole fetch.
    """

    _CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    @retry_on_network_failure(max_attempts=3)
    def _fetch_raw(self) -> Any:
        request = urllib.request.Request(
            self._CALENDAR_URL, headers={"User-Agent": "TitanAITrader/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"Economic calendar returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Economic calendar unreachable: {exc.reason}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise NewsValidationError("Economic calendar response was not valid JSON.") from exc

    def fetch(self, countries: tuple[str, ...] = ("IN", "US")) -> list[dict]:
        try:
            payload = self._fetch_raw()
        except MarketDataUnavailableError as exc:
            raise NewsUnavailableError(str(exc)) from exc

        if not isinstance(payload, list):
            raise NewsValidationError("Economic calendar response had an unexpected shape.")
        if not countries:
            return payload
        return [row for row in payload if row.get("country") in countries]


# ---------------------------------------------------------------------- #
# JSON serialization (SRP: the only class that knows the on-the-wire
# shape ai_brain.py will receive)
# ---------------------------------------------------------------------- #
class NewsJsonSerializer:
    """Converts news/calendar entities into plain, JSON-safe dicts."""

    @staticmethod
    def serialize_article(article: NewsArticle) -> dict[str, Any]:
        return {
            "title": article.title,
            "source": article.source,
            "url": article.url,
            "published_at": article.published_at.isoformat(),
            "description": article.description,
            "sentiment": article.sentiment.value,
            "impact": article.impact.value,
            "is_policy_news": article.is_policy_news,
            "matched_policy_keywords": list(article.matched_policy_keywords),
        }

    @classmethod
    def serialize_articles(cls, articles: list[NewsArticle]) -> list[dict[str, Any]]:
        return [cls.serialize_article(a) for a in articles]

    @staticmethod
    def serialize_event(event: EconomicEvent) -> dict[str, Any]:
        return {
            "event_name": event.event_name,
            "country": event.country,
            "importance": event.importance.value,
            "event_time": event.event_time.isoformat() if event.event_time else None,
            "raw_date_text": event.raw_date_text,
            "actual": event.actual,
            "forecast": event.forecast,
            "previous": event.previous,
        }

    @classmethod
    def serialize_events(cls, events: list[EconomicEvent]) -> list[dict[str, Any]]:
        return [cls.serialize_event(e) for e in events]

    @classmethod
    def serialize_market_news(
        cls,
        articles: list[NewsArticle],
        events: list[EconomicEvent],
        high_impact_alerts: list[dict[str, Any]],
        overall_sentiment: dict[str, Any],
        source_used: str,
        generated_at: datetime,
    ) -> dict[str, Any]:
        policy_news = [a for a in articles if a.is_policy_news]
        return {
            "source_used": source_used,
            "overall_sentiment": overall_sentiment,
            "articles": cls.serialize_articles(articles),
            "policy_news": cls.serialize_articles(policy_news),
            "economic_events": cls.serialize_events(events),
            "high_impact_alerts": high_impact_alerts,
            "generated_at": generated_at.isoformat(),
        }


# ---------------------------------------------------------------------- #
# Orchestrator
# ---------------------------------------------------------------------- #
DEFAULT_NEWS_QUERY = (
    'NIFTY OR "BANK NIFTY" OR Sensex OR RBI OR "Federal Reserve" '
    "OR \"Indian stock market\" OR NSE OR BSE"
)


class NewsAgent:
    """Orchestrates news + economic calendar fetching, RBI/Fed policy
    detection, sentiment analysis, impact grading, validation, and JSON
    serialization for ai_brain.py.

    Every dependency is injected (constructor defaults build the real
    NewsAPI/Google RSS/economic-calendar providers), so this class can
    be fully unit-tested with fakes and never needs network access to
    verify its own orchestration logic.
    """

    def __init__(
        self,
        newsapi_provider: NewsApiProvider | None = None,
        google_rss_provider: GoogleNewsRssProvider | None = None,
        economic_calendar_provider: EconomicCalendarProvider | None = None,
        sentiment_analyzer: SentimentAnalyzer | None = None,
        policy_detector: PolicyNewsDetector | None = None,
        impact_classifier: ImpactClassifier | None = None,
        json_serializer: type[NewsJsonSerializer] = NewsJsonSerializer,
        agent_logger: logging.Logger | None = None,
    ) -> None:
        self._newsapi = newsapi_provider or NewsApiProvider()
        self._google_rss = google_rss_provider or GoogleNewsRssProvider()
        self._calendar = economic_calendar_provider or EconomicCalendarProvider()
        self._sentiment_analyzer = sentiment_analyzer or SentimentAnalyzer()
        self._policy_detector = policy_detector or PolicyNewsDetector()
        self._impact_classifier = impact_classifier or ImpactClassifier()
        self._serializer = json_serializer
        self._logger = agent_logger or logger

    # -- Fetch + normalize -------------------------------------------- #
    def fetch_headlines(
        self, query: str = DEFAULT_NEWS_QUERY, page_size: int = 20
    ) -> tuple[list[NewsArticle], str]:
        """Fetch headlines, preferring NewsAPI and falling back to Google
        News RSS. Returns (articles, source_used) where source_used is
        'newsapi' or 'google_news_rss', so callers/logs know which path
        served the data.
        """
        raw_articles: list[dict] | None = None
        source_used = ""

        if self._newsapi.is_configured():
            try:
                self._logger.info("Fetching headlines from NewsAPI (query=%r)", query)
                raw_articles = self._newsapi.fetch(query, page_size)
                source_used = "newsapi"
            except NewsDataError as exc:
                self._logger.warning(
                    "NewsAPI fetch failed (%s); falling back to Google News RSS.", exc
                )
        else:
            self._logger.info("NEWSAPI_KEY not configured; using Google News RSS.")

        if raw_articles is None:
            self._logger.info("Fetching headlines from Google News RSS (query=%r)", query)
            raw_items = self._google_rss.fetch(query, max_items=page_size)
            raw_articles = raw_items
            source_used = "google_news_rss"

        parse_fn = (
            self._parse_newsapi_article if source_used == "newsapi" else self._parse_rss_item
        )
        articles: list[NewsArticle] = []
        for raw in raw_articles:
            article = self._build_article(raw, parse_fn)
            if article is not None:
                articles.append(article)

        articles = self._deduplicate(articles)
        self._logger.info(
            "Fetched %d validated article(s) via %s", len(articles), source_used
        )
        return articles, source_used

    def _build_article(self, raw: dict, parse_fn) -> NewsArticle | None:
        """Parse one raw record into a NewsArticle, running it through
        policy detection, sentiment analysis, and impact classification.
        Returns None (and logs) for a record too malformed to use --
        one bad headline must not take down the whole batch."""
        try:
            title, source, url, published_at, description = parse_fn(raw)
        except (KeyError, ValueError, TypeError) as exc:
            self._logger.debug("Skipping malformed news record (%s): %r", exc, raw)
            return None

        if not title or not url:
            return None

        combined_text = f"{title} {description}"
        is_policy, matched_keywords = self._policy_detector.detect(combined_text)
        sentiment = self._sentiment_analyzer.analyze(combined_text)
        impact = self._impact_classifier.classify(combined_text, is_policy)

        try:
            return NewsArticle(
                title=title,
                source=source,
                url=url,
                published_at=published_at,
                description=description,
                sentiment=sentiment,
                impact=impact,
                is_policy_news=is_policy,
                matched_policy_keywords=matched_keywords,
            )
        except NewsValidationError as exc:
            self._logger.debug("Rejected invalid article (%s): %r", exc, raw)
            return None

    @staticmethod
    def _parse_newsapi_article(raw: dict) -> tuple[str, str, str, datetime, str]:
        title = (raw.get("title") or "").strip()
        source = ((raw.get("source") or {}).get("name") or "Unknown").strip()
        url = (raw.get("url") or "").strip()
        description = (raw.get("description") or "").strip()
        published_raw = raw.get("publishedAt") or ""
        published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        return title, source, url, published_at, description

    @staticmethod
    def _parse_rss_item(raw: dict) -> tuple[str, str, str, datetime, str]:
        title = (raw.get("title") or "").strip()
        source = (raw.get("source") or "Google News").strip()
        url = (raw.get("link") or "").strip()
        description = (raw.get("description") or "").strip()
        pub_date_raw = raw.get("pubDate") or ""
        published_at = parsedate_to_datetime(pub_date_raw)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        return title, source, url, published_at, description

    @staticmethod
    def _deduplicate(articles: list[NewsArticle]) -> list[NewsArticle]:
        """Removes duplicate headlines (common when merging sources) by
        normalized title, keeping the first (most relevant) occurrence."""
        seen_titles: set[str] = set()
        deduplicated: list[NewsArticle] = []
        for article in articles:
            key = re.sub(r"\s+", " ", article.title.strip().lower())
            if key in seen_titles:
                continue
            seen_titles.add(key)
            deduplicated.append(article)
        return deduplicated

    # -- Economic calendar ---------------------------------------------#
    def fetch_economic_calendar(
        self, countries: tuple[str, ...] = ("IN", "US")
    ) -> list[EconomicEvent]:
        self._logger.info("Fetching economic calendar for countries=%s", countries)
        try:
            raw_rows = self._calendar.fetch(countries)
        except NewsDataError as exc:
            self._logger.error("Failed to fetch economic calendar: %s", exc)
            raise

        events: list[EconomicEvent] = []
        for row in raw_rows:
            event = self._build_event(row)
            if event is not None:
                events.append(event)

        self._logger.info("Fetched %d economic event(s)", len(events))
        return events

    def _build_event(self, row: dict) -> EconomicEvent | None:
        try:
            event_name = (row.get("title") or "").strip()
            country = (row.get("country") or "").strip()
            if not event_name or not country:
                return None
            importance = ImpactClassifier.from_calendar_impact_text(row.get("impact"))
            raw_date_text = f"{row.get('date', '')} {row.get('time', '')}".strip()
            event_time = self._try_parse_calendar_datetime(row.get("date"), row.get("time"))
            return EconomicEvent(
                event_name=event_name,
                country=country,
                importance=importance,
                event_time=event_time,
                raw_date_text=raw_date_text,
                actual=row.get("actual") or None,
                forecast=row.get("forecast") or None,
                previous=row.get("previous") or None,
            )
        except NewsValidationError as exc:
            self._logger.debug("Rejected invalid economic event (%s): %r", exc, row)
            return None

    @staticmethod
    def _try_parse_calendar_datetime(date_text: str | None, time_text: str | None) -> datetime | None:
        """Best-effort parse of the calendar feed's date/time strings.
        Returns None (rather than raising) on any format it doesn't
        recognize -- raw_date_text on the event always preserves the
        original values for a human or downstream system to interpret.
        """
        if not date_text:
            return None
        for fmt in ("%m-%d-%Y %I:%M%p", "%m-%d-%Y", "%Y-%m-%d %I:%M%p", "%Y-%m-%d"):
            candidate = f"{date_text} {time_text}".strip() if "%I:%M%p" in fmt else date_text
            try:
                parsed = datetime.strptime(candidate, fmt)
                return parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    # -- High-impact alerts ---------------------------------------------#
    @staticmethod
    def get_high_impact_alerts(
        articles: list[NewsArticle], events: list[EconomicEvent]
    ) -> list[dict[str, Any]]:
        """Unified alert list combining HIGH-impact news and economic
        events, sorted most-recent-first where a timestamp is available."""
        alerts: list[dict[str, Any]] = []
        for article in articles:
            if article.impact == NewsImpact.HIGH:
                alerts.append(
                    {
                        "type": "news",
                        "title": article.title,
                        "source": article.source,
                        "url": article.url,
                        "sentiment": article.sentiment.value,
                        "is_policy_news": article.is_policy_news,
                        "timestamp": article.published_at.isoformat(),
                    }
                )
        for event in events:
            if event.importance == NewsImpact.HIGH:
                alerts.append(
                    {
                        "type": "economic_event",
                        "title": event.event_name,
                        "country": event.country,
                        "forecast": event.forecast,
                        "previous": event.previous,
                        "timestamp": event.event_time.isoformat() if event.event_time else None,
                    }
                )
        alerts.sort(key=lambda a: a.get("timestamp") or "", reverse=True)
        return alerts

    # -- Aggregate sentiment ---------------------------------------------#
    @staticmethod
    def _aggregate_sentiment(articles: list[NewsArticle]) -> dict[str, Any]:
        bullish = sum(1 for a in articles if a.sentiment == SentimentLabel.BULLISH)
        bearish = sum(1 for a in articles if a.sentiment == SentimentLabel.BEARISH)
        neutral = sum(1 for a in articles if a.sentiment == SentimentLabel.NEUTRAL)

        if bullish > bearish:
            label = SentimentLabel.BULLISH
        elif bearish > bullish:
            label = SentimentLabel.BEARISH
        else:
            label = SentimentLabel.NEUTRAL

        return {
            "label": label.value,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "total_articles": len(articles),
        }

    # -- Top-level API (the contract ai_brain.py consumes) ---------------#
    def get_market_news_json(
        self,
        query: str = DEFAULT_NEWS_QUERY,
        countries: tuple[str, ...] = ("IN", "US"),
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Fetch, validate, analyze, and serialize everything this agent
        covers into one structured JSON payload for ai_brain.py."""
        articles, source_used = self.fetch_headlines(query, page_size)

        try:
            events = self.fetch_economic_calendar(countries)
        except NewsDataError as exc:
            # Economic calendar is a secondary signal -- if it's
            # unreachable, still return the news/sentiment payload rather
            # than failing the whole call, but log loudly so it's visible.
            self._logger.error(
                "Economic calendar unavailable, continuing without it: %s", exc
            )
            events = []

        alerts = self.get_high_impact_alerts(articles, events)
        overall_sentiment = self._aggregate_sentiment(articles)

        return self._serializer.serialize_market_news(
            articles=articles,
            events=events,
            high_impact_alerts=alerts,
            overall_sentiment=overall_sentiment,
            source_used=source_used,
            generated_at=_utcnow(),
        )

    def get_market_news_json_string(self, indent: int | None = 2, **kwargs: Any) -> str:
        """Convenience wrapper: the same payload as a JSON string, for
        callers that want to log, persist, or transmit it as text."""
        return json.dumps(self.get_market_news_json(**kwargs), indent=indent)
