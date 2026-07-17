"""Shared fake providers for application-layer tests.

These are simple test doubles (not mocks) that implement the same ports
the real infrastructure adapters implement, so MarketDataService and the
agents built on top of it can be exercised fully offline -- no network
access is required to prove the orchestration, validation, and
serialization logic is correct.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from titan_ai_trader.application.interfaces.fii_dii_provider import FiiDiiProvider
from titan_ai_trader.application.interfaces.india_vix_provider import IndiaVixProvider
from titan_ai_trader.application.interfaces.nse_spot_provider import NseSpotProvider
from titan_ai_trader.application.interfaces.option_chain_provider import OptionChainProvider
from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
from titan_ai_trader.domain.entities.india_vix import IndiaVix
from titan_ai_trader.domain.entities.market_quote import MarketQuote
from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.entities.option_contract import OptionContract
from titan_ai_trader.domain.enums.option_type import OptionType
from titan_ai_trader.domain.exceptions.market_data_exceptions import SymbolNotFoundError
from titan_ai_trader.domain.value_objects.money import Money

EXPIRY = date(2026, 7, 30)


def make_option_chain(symbol: str, timestamp: datetime | None = None) -> OptionChainSnapshot:
    contracts = (
        OptionContract(Decimal("24000"), OptionType.CE, EXPIRY, 1000, 50, 200, Decimal("14"), Money.of("120")),
        OptionContract(Decimal("24000"), OptionType.PE, EXPIRY, 1500, 30, 150, Decimal("15"), Money.of("80")),
        OptionContract(Decimal("24500"), OptionType.CE, EXPIRY, 2000, 100, 300, Decimal("13"), Money.of("60")),
        OptionContract(Decimal("24500"), OptionType.PE, EXPIRY, 1200, -20, 100, Decimal("16"), Money.of("100")),
    )
    kwargs = dict(
        symbol=symbol, expiry_date=EXPIRY, underlying_value=Money.of("24300"), contracts=contracts
    )
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return OptionChainSnapshot(**kwargs)


class FakeSpotProvider(NseSpotProvider):
    def __init__(self, timestamp: datetime | None = None) -> None:
        self.timestamp = timestamp

    def get_spot(self, symbol: str, is_index: bool = False) -> MarketQuote:
        kwargs = dict(
            symbol=symbol, last_price=Money.of("24300"), change=Decimal("50"), change_percent=Decimal("0.2")
        )
        if self.timestamp is not None:
            kwargs["timestamp"] = self.timestamp
        return MarketQuote(**kwargs)


class FakeOptionChainProvider(OptionChainProvider):
    def __init__(self, timestamp: datetime | None = None) -> None:
        self.timestamp = timestamp

    def get_option_chain(self, symbol: str, expiry_date=None) -> OptionChainSnapshot:
        return make_option_chain(symbol, self.timestamp)


class FakeVixProvider(IndiaVixProvider):
    def get_vix(self) -> IndiaVix:
        return IndiaVix(value=Decimal("13.5"), change=Decimal("0.1"), change_percent=Decimal("0.7"))


class FakeFiiDiiProvider(FiiDiiProvider):
    """Backed by a small in-memory {date: FiiDiiActivity} table so tests
    can exercise previous-day lookback and holiday-skipping behavior.

    Dates default to "today"/"yesterday" (computed at construction time,
    not hardcoded) so these fixtures never go stale relative to
    FreshnessValidator's real wall-clock check just because real time
    has moved past whatever date was hardcoded when the test was written.
    """

    def __init__(self, activity_by_date: dict[date, FiiDiiActivity] | None = None) -> None:
        if activity_by_date is not None:
            self.activity_by_date = activity_by_date
            return
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        self.activity_by_date = {
            today: FiiDiiActivity(
                today, Money.of("6000"), Money.of("4000"), Money.of("3000"), Money.of("3500")
            ),
            yesterday: FiiDiiActivity(
                yesterday, Money.of("5000"), Money.of("4500"), Money.of("3000"), Money.of("2800")
            ),
        }

    def get_latest(self) -> FiiDiiActivity:
        latest_date = max(self.activity_by_date)
        return self.activity_by_date[latest_date]

    def get_by_date(self, activity_date: date) -> FiiDiiActivity:
        if activity_date not in self.activity_by_date:
            raise SymbolNotFoundError(f"no FII/DII data for {activity_date}")
        return self.activity_by_date[activity_date]
