from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from titan_ai_trader.application.services.fii_dii_agent import (
    BuildUpAnalyzer,
    BuildUpPattern,
    FiiDiiAgent,
    InstitutionalSentiment,
    InstitutionalSentimentAnalyzer,
    ParticipantCategory,
    ParticipantOiProvider,
    ParticipantOiRecord,
    ParticipantOiSnapshot,
)
from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
    SymbolNotFoundError,
)
from titan_ai_trader.domain.value_objects.money import Money
from titan_ai_trader.infrastructure.market_data.cache.in_memory_cache import (
    InMemoryMarketDataCache,
)

from .fakes import FakeFiiDiiProvider, FakeOptionChainProvider, FakeSpotProvider, FakeVixProvider


class TestBuildUpAnalyzer:
    def test_price_up_oi_up_is_long_build_up(self):
        assert BuildUpAnalyzer.classify(1000, Decimal("50")) == BuildUpPattern.LONG_BUILD_UP

    def test_price_down_oi_up_is_short_build_up(self):
        assert BuildUpAnalyzer.classify(1000, Decimal("-50")) == BuildUpPattern.SHORT_BUILD_UP

    def test_price_up_oi_down_is_short_covering(self):
        assert BuildUpAnalyzer.classify(-1000, Decimal("50")) == BuildUpPattern.SHORT_COVERING

    def test_price_down_oi_down_is_long_unwinding(self):
        assert BuildUpAnalyzer.classify(-1000, Decimal("-50")) == BuildUpPattern.LONG_UNWINDING

    def test_missing_price_change_is_neutral(self):
        assert BuildUpAnalyzer.classify(1000, None) == BuildUpPattern.NEUTRAL

    def test_zero_oi_change_is_neutral(self):
        assert BuildUpAnalyzer.classify(0, Decimal("50")) == BuildUpPattern.NEUTRAL


class TestParticipantOiRecord:
    def test_valid_record_constructs(self):
        record = ParticipantOiRecord(
            ParticipantCategory.FII, date(2026, 7, 10), 100000, 60000, 20000, 15000, 8000, 9000
        )
        assert record.net_future_index_position == 40000

    def test_negative_field_rejected(self):
        with pytest.raises(MarketDataValidationError):
            ParticipantOiRecord(ParticipantCategory.FII, date(2026, 7, 10), -1, 0, 0, 0, 0, 0)


class TestParticipantOiProviderCsvParsing:
    def test_parses_known_categories_and_skips_total_row(self):
        csv_text = (
            "Client Type,Future Index Long,Future Index Short,Future Index Long Notional Value,"
            "Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short\n"
            "FII,150000,90000,999999,40000,30000,20000,25000\n"
            "DII,60000,50000,999999,10000,8000,7000,6000\n"
            "Pro,30000,32000,999999,5000,4000,4500,4200\n"
            "Client,20000,18000,999999,3000,2500,2200,2100\n"
            "TOTAL,260000,190000,999999,58000,44500,33700,37300\n"
        )
        provider = ParticipantOiProvider()
        snapshot = provider._parse_csv(csv_text, date(2026, 7, 10))
        assert snapshot is not None
        assert len(snapshot.records) == 4
        assert snapshot.get(ParticipantCategory.FII).future_index_long == 150000
        assert snapshot.get(ParticipantCategory.FII).net_future_index_position == 60000

    def test_missing_required_columns_raises(self):
        provider = ParticipantOiProvider()
        with pytest.raises(MarketDataValidationError):
            provider._parse_csv("Foo,Bar\n1,2\n", date(2026, 7, 10))


class TestInstitutionalSentimentAnalyzer:
    def test_fii_buyer_dii_seller_long_build_up_is_bullish(self):
        cash = FiiDiiActivity(
            date(2026, 7, 10), Money.of("6000"), Money.of("4000"), Money.of("3000"), Money.of("3500")
        )
        result = InstitutionalSentimentAnalyzer().analyze(cash, BuildUpPattern.LONG_BUILD_UP)
        assert result["label"] == InstitutionalSentiment.BULLISH.value
        assert result["total_score"] == 3

    def test_fii_seller_dii_seller_short_build_up_is_bearish(self):
        cash = FiiDiiActivity(
            date(2026, 7, 10), Money.of("3000"), Money.of("5000"), Money.of("2000"), Money.of("3000")
        )
        result = InstitutionalSentimentAnalyzer().analyze(cash, BuildUpPattern.SHORT_BUILD_UP)
        assert result["label"] == InstitutionalSentiment.BEARISH.value


class _FakeParticipantProvider(ParticipantOiProvider):
    """Backed by an in-memory table instead of hitting the NSE archive."""

    def __init__(self, snapshots: dict[date, ParticipantOiSnapshot]) -> None:
        self.snapshots = snapshots

    def fetch_snapshot(self, as_of: date | None = None) -> ParticipantOiSnapshot:
        if as_of not in self.snapshots:
            raise SymbolNotFoundError(f"no snapshot for {as_of}")
        return self.snapshots[as_of]


def _participant_snapshot(day: date, fii_long: int, fii_short: int) -> ParticipantOiSnapshot:
    return ParticipantOiSnapshot(
        day,
        {
            ParticipantCategory.FII: ParticipantOiRecord(
                ParticipantCategory.FII, day, fii_long, fii_short, 40000, 30000, 20000, 25000
            ),
            ParticipantCategory.DII: ParticipantOiRecord(
                ParticipantCategory.DII, day, 60000, 50000, 10000, 8000, 7000, 6000
            ),
        },
    )


def test_full_report_orchestration_produces_valid_json():
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)

    service = MarketDataService(
        FakeSpotProvider(),
        FakeOptionChainProvider(),
        FakeVixProvider(),
        FakeFiiDiiProvider(),
        InMemoryMarketDataCache(),
    )
    participant_provider = _FakeParticipantProvider(
        {
            today: _participant_snapshot(today, 160000, 90000),
            yesterday: _participant_snapshot(yesterday, 150000, 90000),
        }
    )
    agent = FiiDiiAgent(service, participant_oi_provider=participant_provider)

    report = agent.get_fii_dii_report_json(activity_date=today, nifty_price_change=Decimal("50"))

    assert report["cash_market"]["current"]["activity_date"] == today.isoformat()
    assert report["cash_market"]["comparison"]["available"] is True
    assert report["cash_market"]["comparison"]["fii_trend"] == "increasing_net_buying"
    assert report["fno_participant_oi"]["current"]["records"]["FII"]["net_future_index_position"] == 70000
    assert report["build_up_analysis"]["FII"]["pattern"] == BuildUpPattern.LONG_BUILD_UP.value
    assert report["institutional_sentiment"]["label"] in {s.value for s in InstitutionalSentiment}

    # Must be genuinely JSON-serializable end to end (the ai_brain.py contract).
    reparsed = json.loads(json.dumps(report))
    assert reparsed == report


def test_missing_participant_data_degrades_to_neutral_without_failing_report():
    today = datetime.now(UTC).date()
    service = MarketDataService(
        FakeSpotProvider(),
        FakeOptionChainProvider(),
        FakeVixProvider(),
        FakeFiiDiiProvider(),
        InMemoryMarketDataCache(),
    )
    empty_participant_provider = _FakeParticipantProvider({})
    agent = FiiDiiAgent(service, participant_oi_provider=empty_participant_provider)

    report = agent.get_fii_dii_report_json(activity_date=today)

    assert report["fno_participant_oi"]["current"] is None
    assert report["build_up_analysis"]["FII"]["pattern"] == BuildUpPattern.NEUTRAL.value
    # The report as a whole must still be produced and still be valid JSON.
    json.dumps(report)
