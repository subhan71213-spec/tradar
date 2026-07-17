"""MarketDataService — the single entry point the rest of the app uses
for all market data.

Orchestrates the individual providers (spot, option chain, VIX, FII/DII),
the cache, and the domain calculators (PCR, Max Pain, OI Change) behind
one facade. Callers never talk to an adapter or the cache directly.

This service performs NO order execution of any kind -- it is read-only
market data plumbing for a paper trading system.
"""

from __future__ import annotations

from datetime import date as Date

from titan_ai_trader.application.interfaces.fii_dii_provider import FiiDiiProvider
from titan_ai_trader.application.interfaces.india_vix_provider import IndiaVixProvider
from titan_ai_trader.application.interfaces.market_data_cache import MarketDataCache
from titan_ai_trader.application.interfaces.nse_spot_provider import NseSpotProvider
from titan_ai_trader.application.interfaces.option_chain_provider import OptionChainProvider
from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
from titan_ai_trader.domain.entities.india_vix import IndiaVix
from titan_ai_trader.domain.entities.market_quote import MarketQuote
from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.services.max_pain_calculator import MaxPainCalculator
from titan_ai_trader.domain.services.oi_change_analyzer import OiChangeAnalyzer
from titan_ai_trader.domain.services.pcr_calculator import PcrCalculator
from titan_ai_trader.domain.value_objects.max_pain import MaxPain
from titan_ai_trader.domain.value_objects.oi_change_summary import OiChangeSummary
from titan_ai_trader.domain.value_objects.pcr import Pcr


class MarketDataCacheTtls:
    """Default cache lifetimes, in seconds, per data type.

    Spot and option chain move quickly during market hours; VIX and
    FII/DII are published far less often, so they get longer TTLs.
    """

    SPOT_SECONDS: float = 5.0
    OPTION_CHAIN_SECONDS: float = 10.0
    VIX_SECONDS: float = 5.0
    FII_DII_SECONDS: float = 3600.0  # published once per day


class MarketDataService:
    """Unified facade over all market data sources, with caching."""

    def __init__(
        self,
        spot_provider: NseSpotProvider,
        option_chain_provider: OptionChainProvider,
        vix_provider: IndiaVixProvider,
        fii_dii_provider: FiiDiiProvider,
        cache: MarketDataCache,
        ttls: MarketDataCacheTtls | None = None,
    ) -> None:
        self._spot_provider = spot_provider
        self._option_chain_provider = option_chain_provider
        self._vix_provider = vix_provider
        self._fii_dii_provider = fii_dii_provider
        self._cache = cache
        self._ttls = ttls or MarketDataCacheTtls()

    # ------------------------------------------------------------------ #
    # Raw data access
    # ------------------------------------------------------------------ #
    def get_spot(self, symbol: str, is_index: bool = False) -> MarketQuote:
        cache_key = f"spot:{symbol}:{is_index}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        quote = self._spot_provider.get_spot(symbol, is_index)
        self._cache.set(cache_key, quote, self._ttls.SPOT_SECONDS)
        return quote

    def get_option_chain(
        self, symbol: str, expiry_date: Date | None = None
    ) -> OptionChainSnapshot:
        cache_key = f"option_chain:{symbol}:{expiry_date or 'nearest'}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        snapshot = self._option_chain_provider.get_option_chain(symbol, expiry_date)
        self._cache.set(cache_key, snapshot, self._ttls.OPTION_CHAIN_SECONDS)
        return snapshot

    def get_india_vix(self) -> IndiaVix:
        cache_key = "india_vix"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        vix = self._vix_provider.get_vix()
        self._cache.set(cache_key, vix, self._ttls.VIX_SECONDS)
        return vix

    def get_fii_dii(self, activity_date: Date | None = None) -> FiiDiiActivity:
        cache_key = f"fii_dii:{activity_date or 'latest'}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        activity = (
            self._fii_dii_provider.get_by_date(activity_date)
            if activity_date is not None
            else self._fii_dii_provider.get_latest()
        )
        self._cache.set(cache_key, activity, self._ttls.FII_DII_SECONDS)
        return activity

    # ------------------------------------------------------------------ #
    # Derived analytics (computed from the option chain)
    # ------------------------------------------------------------------ #
    def get_pcr(self, symbol: str, expiry_date: Date | None = None) -> Pcr:
        snapshot = self.get_option_chain(symbol, expiry_date)
        return PcrCalculator.calculate(snapshot)

    def get_max_pain(self, symbol: str, expiry_date: Date | None = None) -> MaxPain:
        snapshot = self.get_option_chain(symbol, expiry_date)
        return MaxPainCalculator.calculate(snapshot)

    def get_oi_change(self, symbol: str, expiry_date: Date | None = None) -> OiChangeSummary:
        snapshot = self.get_option_chain(symbol, expiry_date)
        return OiChangeAnalyzer.summarize(snapshot)

    # ------------------------------------------------------------------ #
    # Cache control
    # ------------------------------------------------------------------ #
    def invalidate_all(self) -> None:
        self._cache.clear()
