from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from titan_ai_trader.application.services.news_agent import (
    EconomicCalendarProvider,
    GoogleNewsRssProvider,
    ImpactClassifier,
    NewsAgent,
    NewsApiProvider,
    NewsArticle,
    NewsImpact,
    NewsUnavailableError,
    NewsValidationError,
    PolicyNewsDetector,
    SentimentAnalyzer,
    SentimentLabel,
)

# ---------------------------------------------------------------------- #
# Keyword-classifier collision regression tests -- these specifically
# guard against naive substring matching (e.g. "war" matching inside
# "award"), which was a real bug caught during verification.
# ---------------------------------------------------------------------- #
COLLISION_WORDS = (
    "award", "bargain", "shortfall", "footfall", "waterfall", "software",
    "warehouse", "warranty", "gainful", "regain", "warm", "wardrobe",
    "reward", "forewarn", "hardware", "warfare", "wartime", "confederate",
)


class TestKeywordClassifierCollisions:
    def test_no_false_positive_matches(self):
        sa, ic, pd = SentimentAnalyzer(), ImpactClassifier(), PolicyNewsDetector()
        for word in COLLISION_WORDS:
            assert sa.analyze(word) == SentimentLabel.NEUTRAL, word
            assert ic.classify(word, False) == NewsImpact.LOW, word
            is_policy, _ = pd.detect(word)
            assert is_policy is False, word


class TestSentimentAnalyzer:
    def test_bullish_text(self):
        assert (
            SentimentAnalyzer().analyze("Sensex rallies to record high on strong earnings")
            == SentimentLabel.BULLISH
        )

    def test_bearish_text(self):
        assert (
            SentimentAnalyzer().analyze("Markets crash as recession fears grow, selloff deepens")
            == SentimentLabel.BEARISH
        )

    def test_neutral_text(self):
        assert (
            SentimentAnalyzer().analyze("Company announces quarterly board meeting schedule")
            == SentimentLabel.NEUTRAL
        )


class TestPolicyNewsDetector:
    def test_detects_rbi_policy_language(self):
        detector = PolicyNewsDetector()
        is_policy, matched = detector.detect("RBI hikes repo rate in latest MPC meeting")
        assert is_policy is True
        assert "rbi" in matched
        assert "repo rate" in matched

    def test_fed_matches_standalone_word_only(self):
        detector = PolicyNewsDetector()
        assert detector.detect("Fed hikes rates")[0] is True
        assert detector.detect("Federal Express delivers a package")[0] is False

    def test_non_policy_text_not_flagged(self):
        assert PolicyNewsDetector().detect("Local team wins cricket match")[0] is False


class TestImpactClassifier:
    def test_policy_news_is_always_high(self):
        assert ImpactClassifier().classify("routine announcement", is_policy_news=True) == NewsImpact.HIGH

    def test_high_impact_keyword(self):
        assert (
            ImpactClassifier().classify("War breaks out, markets crash", is_policy_news=False)
            == NewsImpact.HIGH
        )

    def test_medium_impact_keyword(self):
        assert (
            ImpactClassifier().classify("Q2 earnings season begins for major banks", is_policy_news=False)
            == NewsImpact.MEDIUM
        )

    def test_low_impact_default(self):
        assert (
            ImpactClassifier().classify("Local bakery wins award", is_policy_news=False)
            == NewsImpact.LOW
        )

    def test_calendar_impact_text_mapping(self):
        assert ImpactClassifier.from_calendar_impact_text("High") == NewsImpact.HIGH
        assert ImpactClassifier.from_calendar_impact_text("Medium") == NewsImpact.MEDIUM
        assert ImpactClassifier.from_calendar_impact_text("Low") == NewsImpact.LOW
        assert ImpactClassifier.from_calendar_impact_text(None) == NewsImpact.LOW


class TestNewsArticleValidation:
    def test_empty_title_rejected(self):
        with pytest.raises(NewsValidationError):
            NewsArticle(title="", source="X", url="https://x.com", published_at=datetime.now(timezone.utc))

    def test_empty_url_rejected(self):
        with pytest.raises(NewsValidationError):
            NewsArticle(title="Title", source="X", url="", published_at=datetime.now(timezone.utc))


class _FakeNewsApi(NewsApiProvider):
    def __init__(self, articles=None, fail: bool = False) -> None:
        self._articles = articles or []
        self._fail = fail

    def is_configured(self) -> bool:
        return True

    def fetch(self, query, page_size=20):
        if self._fail:
            raise NewsUnavailableError("simulated NewsAPI outage")
        return self._articles


class _FakeRss(GoogleNewsRssProvider):
    def __init__(self, items=None) -> None:
        self._items = items or []

    def fetch(self, query, max_items=20):
        return self._items


class _FakeCalendar(EconomicCalendarProvider):
    def __init__(self, rows=None) -> None:
        self._rows = rows or []

    def fetch(self, countries=("IN", "US")):
        return self._rows


_SAMPLE_NEWSAPI_ARTICLES = [
    {
        "title": "RBI hikes repo rate, markets tumble on rate hike fears",
        "source": {"name": "Economic Times"},
        "url": "https://example.com/a1",
        "publishedAt": "2026-07-10T09:30:00Z",
        "description": "The Reserve Bank of India raised rates today.",
    },
    {
        "title": "Sensex rallies to record high on strong earnings",
        "source": {"name": "Mint"},
        "url": "https://example.com/a2",
        "publishedAt": "2026-07-10T10:00:00Z",
        "description": "Bulls take charge as markets soar.",
    },
    {
        "title": "",  # malformed: empty title, must be skipped, not crash the batch
        "source": {"name": "Nowhere"},
        "url": "https://example.com/bad",
        "publishedAt": "2026-07-10T10:00:00Z",
        "description": "",
    },
]

_SAMPLE_CALENDAR_ROWS = [
    {
        "title": "RBI Interest Rate Decision", "country": "IN", "impact": "High",
        "date": "07-10-2026", "time": "11:00am", "forecast": "6.50%", "previous": "6.25%", "actual": "6.50%",
    },
    {
        "title": "US Non-Farm Payrolls", "country": "US", "impact": "High",
        "date": "07-10-2026", "time": "6:00pm", "forecast": "180K", "previous": "175K",
    },
    {"title": "", "country": "IN", "impact": "Low"},  # malformed, must be skipped
]


def test_full_orchestration_prefers_newsapi_and_produces_valid_json():
    agent = NewsAgent(
        newsapi_provider=_FakeNewsApi(articles=_SAMPLE_NEWSAPI_ARTICLES),
        economic_calendar_provider=_FakeCalendar(rows=_SAMPLE_CALENDAR_ROWS),
    )
    report = agent.get_market_news_json()

    assert report["source_used"] == "newsapi"
    assert len(report["articles"]) == 2  # malformed article filtered out
    assert len(report["policy_news"]) == 1
    assert len(report["economic_events"]) == 2  # malformed row filtered out
    assert len(report["high_impact_alerts"]) >= 2
    assert report["overall_sentiment"]["label"] in {s.value for s in SentimentLabel}

    # Must be genuinely JSON-serializable end to end (the ai_brain.py contract).
    reparsed = json.loads(json.dumps(report))
    assert reparsed == report


def test_newsapi_failure_falls_back_to_google_rss():
    rss_items = [
        {
            "title": "BANKNIFTY sees short covering rally",
            "link": "https://example.com/rss1",
            "pubDate": "Fri, 10 Jul 2026 12:00:00 GMT",
            "description": "",
            "source": "Google News",
        }
    ]
    agent = NewsAgent(
        newsapi_provider=_FakeNewsApi(fail=True),
        google_rss_provider=_FakeRss(items=rss_items),
        economic_calendar_provider=_FakeCalendar(rows=[]),
    )
    report = agent.get_market_news_json()
    assert report["source_used"] == "google_news_rss"
    assert len(report["articles"]) == 1


def test_unconfigured_newsapi_skips_straight_to_rss():
    agent = NewsAgent(
        newsapi_provider=NewsApiProvider(api_key=None),
        google_rss_provider=_FakeRss(items=[]),
        economic_calendar_provider=_FakeCalendar(rows=[]),
    )
    report = agent.get_market_news_json()
    assert report["source_used"] == "google_news_rss"


def test_duplicate_titles_are_deduplicated():
    duplicated = _SAMPLE_NEWSAPI_ARTICLES[:2] + [_SAMPLE_NEWSAPI_ARTICLES[1]]
    agent = NewsAgent(
        newsapi_provider=_FakeNewsApi(articles=duplicated),
        economic_calendar_provider=_FakeCalendar(rows=[]),
    )
    report = agent.get_market_news_json()
    titles = [a["title"] for a in report["articles"]]
    assert len(titles) == len(set(titles))


def test_economic_calendar_failure_does_not_fail_whole_report():
    class _FailingCalendar(EconomicCalendarProvider):
        def __init__(self) -> None:
            pass

        def fetch(self, countries=("IN", "US")):
            raise NewsUnavailableError("calendar down")

    agent = NewsAgent(
        newsapi_provider=_FakeNewsApi(articles=_SAMPLE_NEWSAPI_ARTICLES),
        economic_calendar_provider=_FailingCalendar(),
    )
    report = agent.get_market_news_json()
    assert report["economic_events"] == []
    json.dumps(report)  # still valid JSON
