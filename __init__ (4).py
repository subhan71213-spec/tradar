from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import json

import pytest

from titan_ai_trader.application.services.market_agent import (
    FreshnessValidator,
    MarketAgent,
)
from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    StaleMarketDataError,
    SymbolNotFoundError,
)
from titan_ai_trader.infrastructure.market_data.cache.in_memory_cache import (
    InMemoryMarketDataCache,
)

from .fakes import (
    FakeFiiDiiProvider,
    FakeOptionChainProvider,
    FakeSpotProvider,
    FakeVixProvider,
)


def _build_agent(**overrides) -> MarketAgent:
    service = MarketDataService(
        overrides.get("spot", FakeSpotProvider()),
        overrides.get("chain", FakeOptionChainProvider()),
        FakeVixProvider(),
        overrides.get("fii_dii", FakeFiiDiiProvider()),
        InMemoryMarketDataCache(),
    )
    return MarketAgent(service, freshness_validator=overrides.get("freshness_validator"))


def test_analyze_index_is_case_insensitive_and_bundles_all_data():
    agent = _build_agent()
    report = agent.analyze_index("nifty")
    assert report.index.key == "NIFTY"
    assert report.spot.last_price.amount == Decimal("24300")
    assert report.pcr.total_call_open_interest == 3000
    assert report.max_pain.max_pain_strike is not None
    assert report.oi_change.total_call_oi_change == 150


def test_unsupported_index_raises_symbol_not_found():
    agent = _build_agent()
    with pytest.raises(SymbolNotFoundError):
        agent.analyze_index("SENSEX")


def test_stale_spot_or_option_chain_data_is_rejected():
    stale_time = datetime.now(UTC) - timedelta(minutes=30)
    agent = _build_agent(
        spot=FakeSpotProvider(timestamp=stale_time),
        chain=FakeOptionChainProvider(timestamp=stale_time),
        freshness_validator=FreshnessValidator(max_live_data_age_seconds=300),
    )
    with pytest.raises(StaleMarketDataError):
        agent.analyze_index("NIFTY")


def test_stale_fii_dii_activity_is_rejected():
    from datetime import date

    from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
    from titan_ai_trader.domain.value_objects.money import Money

    old_activity = {
        date(2026, 6, 1): FiiDiiActivity(
            date(2026, 6, 1), Money.of("5000"), Money.of("3000"), Money.of("2000"), Money.of("2500")
        )
    }
    agent = _build_agent(fii_dii=FakeFiiDiiProvider(activity_by_date=old_activity))
    with pytest.raises(StaleMarketDataError):
        agent.analyze_fii_dii()


def test_market_snapshot_json_round_trips_through_json_dumps():
    agent = _build_agent()
    snapshot_json = agent.get_market_snapshot_json()

    assert "nifty" in snapshot_json
    assert "banknifty" in snapshot_json
    assert "fii_dii" in snapshot_json
    assert isinstance(snapshot_json["nifty"]["option_chain"]["contracts"], list)

    # The real contract test for ai_brain.py: this must actually be JSON.
    reparsed = json.loads(json.dumps(snapshot_json))
    assert reparsed == snapshot_json


def test_market_snapshot_json_string_matches_dict_shape():
    agent = _build_agent()
    as_string = agent.get_market_snapshot_json_string()
    reparsed = json.loads(as_string)
    assert set(reparsed.keys()) == {"nifty", "banknifty", "fii_dii", "generated_at"}
