"""market_agent.py — the Market Data Engine.

This module is an ORCHESTRATION layer only. It does not fetch or parse
any raw data itself -- all of that already exists in Phase 2
(MarketDataService + the four NSE adapters). MarketAgent's job is to:

    1. Know which real-world index maps to which NSE symbol codes
       (NSE uses different symbol strings for spot vs. option-chain
       lookups of the same index -- see IndexDefinition below).
    2. Fan out to MarketDataService for spot / option chain / PCR /
       Max Pain / OI change, and bundle the results into one cohesive
       report per index.
    3. VALIDATE what comes back before it leaves this layer -- on top
       of the data-shape validation domain entities already enforce at
       construction time, this layer adds a FRESHNESS check (data too
       old to safely act on), because "how old is too old" is an
       orchestration/business-timing concern the domain entities have
       no way to know (they don't know what time "now" is when they
       are read back later; the agent does).
    4. Serialize the validated result into plain, JSON-serializable
       dicts for downstream consumption by ai_brain.py (a later,
       separately-approved file). This module does not import or
       depend on ai_brain.py in any way -- the dependency points one
       direction only, from ai_brain.py down into this module, which
       is why the JSON boundary lives here rather than the other way
       around.
    5. Log each fetch at the orchestration boundary, so failures are
       traceable to "which index, which data type" without needing to
       step through MarketDataService internals.

Explicitly OUT OF SCOPE for this file (by design, matching the phased
approach used so far): Smart Money Concepts, Supply & Demand zones,
Support & Resistance, candlestick pattern detection, news/sentiment
analysis, entry/stop-loss/target generation, risk management, and
backtesting. Those are strategy/analysis/execution concerns, not market
data, and will be their own separately-approved modules. This file
performs no order execution of any kind -- it is read-only market data
orchestration for a paper trading system.

SOLID notes for this file:
  - SRP: three distinct responsibilities (orchestration, freshness
    validation, JSON serialization) live in three distinct classes
    (MarketAgent, FreshnessValidator, MarketDataJsonSerializer) rather
    than one god-class. Each can be understood, tested, and changed
    independently.
  - OCP: adding a new tracked index is a one-line addition to
    SUPPORTED_INDICES; no existing method needs to change. Swapping the
    staleness policy or the output shape means injecting/subclassing
    FreshnessValidator or MarketDataJsonSerializer, not editing
    MarketAgent.
  - LSP: MarketAgent depends on MarketDataService's public contract
    only; any object honoring that contract (e.g. a test double) works
    as a drop-in substitute.
  - ISP: FreshnessValidator and MarketDataJsonSerializer each expose
    only the narrow methods their one job needs -- MarketAgent isn't
    forced to depend on serialization details to validate, or on
    validation details to serialize.
  - DIP: MarketAgent depends on the MarketDataService abstraction
    (itself a facade over injected ports from Phase 2) and on
    injectable FreshnessValidator/MarketDataJsonSerializer instances,
    never on concrete NSE adapters, HTTP clients, or cache
    implementations directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
from titan_ai_trader.domain.entities.market_quote import MarketQuote
from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.entities.option_contract import OptionContract
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataError,
    StaleMarketDataError,
    SymbolNotFoundError,
)
from titan_ai_trader.domain.value_objects.max_pain import MaxPain
from titan_ai_trader.domain.value_objects.money import Money
from titan_ai_trader.domain.value_objects.oi_change_summary import OiChangeSummary
from titan_ai_trader.domain.value_objects.pcr import Pcr

# Module-level logger only -- this module deliberately does NOT call
# logging.basicConfig() or attach handlers. Configuring log output
# (handlers, formatters, log file targets) is an application-entrypoint
# concern and belongs in a dedicated logging module in a later phase.
# Left unconfigured, this logger is a harmless no-op until the host
# application wires up logging.
logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------- #
# Tracked index registry
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IndexDefinition:
    """Maps one tracked index to the symbol codes NSE expects.

    NSE is inconsistent about symbol naming across its own endpoints:
    the spot/index-quote endpoint expects e.g. 'NIFTY 50', while the
    option-chain endpoint expects the shorter 'NIFTY'. This mapping is
    the single place that inconsistency is resolved, so the rest of the
    codebase never has to think about it.
    """

    key: str                    # internal lookup key, e.g. "NIFTY"
    display_name: str           # human-readable name for logs/reports
    spot_symbol: str            # symbol string for the spot/index quote endpoint
    option_chain_symbol: str    # symbol string for the option-chain endpoint


# The two indices this build tracks. Adding a new tracked index later is
# a one-line addition here -- nothing else in this file needs to change
# (Open/Closed Principle).
NIFTY = IndexDefinition(
    key="NIFTY",
    display_name="NIFTY 50",
    spot_symbol="NIFTY 50",
    option_chain_symbol="NIFTY",
)
BANKNIFTY = IndexDefinition(
    key="BANKNIFTY",
    display_name="NIFTY BANK",
    spot_symbol="NIFTY BANK",
    option_chain_symbol="BANKNIFTY",
)

SUPPORTED_INDICES: dict[str, IndexDefinition] = {
    NIFTY.key: NIFTY,
    BANKNIFTY.key: BANKNIFTY,
}


# ---------------------------------------------------------------------- #
# Orchestration-level result objects (internal to this layer; ai_brain.py
# consumes the JSON produced from these, not these objects directly)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IndexAnalysisReport:
    """One index's bundled live market data: spot + option-chain-derived
    analytics, all as of the same orchestration call."""

    index: IndexDefinition
    spot: MarketQuote
    option_chain: OptionChainSnapshot
    pcr: Pcr
    max_pain: MaxPain
    oi_change: OiChangeSummary
    generated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """The full "everything we track" snapshot: both indices plus the
    latest FII/DII cash market activity, in one call."""

    nifty: IndexAnalysisReport
    banknifty: IndexAnalysisReport
    fii_dii: FiiDiiActivity
    generated_at: datetime = field(default_factory=_utcnow)


# ---------------------------------------------------------------------- #
# Freshness validation (SRP: separated from orchestration and from
# serialization; injectable so the staleness policy can change without
# touching MarketAgent -- DIP/OCP)
# ---------------------------------------------------------------------- #
class FreshnessValidator:
    """Rejects market data that is older than an allowed threshold.

    This is deliberately distinct from the data-shape validation domain
    entities already perform in their own __post_init__ (e.g. "is this
    price positive"). That validation happens once, at construction time,
    and has no notion of the current wall-clock time. Freshness is a
    different kind of check -- "was this still true recently enough to
    act on" -- and can only be answered here, at the moment the data is
    about to be handed off.
    """

    def __init__(
        self,
        max_live_data_age_seconds: float = 300.0,
        max_fii_dii_age_days: int = 5,
    ) -> None:
        """
        max_live_data_age_seconds: how old a spot/option-chain timestamp
            may be (default 5 minutes) before it's considered stale.
        max_fii_dii_age_days: how many calendar days old the latest
            published FII/DII activity_date may be (default 5, to
            tolerate weekends/holidays without false positives).
        """
        if max_live_data_age_seconds <= 0:
            raise ValueError("max_live_data_age_seconds must be positive.")
        if max_fii_dii_age_days <= 0:
            raise ValueError("max_fii_dii_age_days must be positive.")
        self._max_live_data_age_seconds = max_live_data_age_seconds
        self._max_fii_dii_age_days = max_fii_dii_age_days

    def validate_live_timestamp(self, timestamp: datetime, label: str) -> None:
        """Raise StaleMarketDataError if timestamp is older than allowed
        for live (intraday) data such as spot quotes or option chains."""
        age_seconds = (_utcnow() - timestamp).total_seconds()
        if age_seconds > self._max_live_data_age_seconds:
            raise StaleMarketDataError(
                f"{label} is stale: {age_seconds:.0f}s old "
                f"(max allowed {self._max_live_data_age_seconds:.0f}s)."
            )

    def validate_fii_dii_date(self, activity_date: date, label: str) -> None:
        """Raise StaleMarketDataError if activity_date is older than
        allowed for end-of-day published data such as FII/DII figures."""
        age_days = (datetime.now(UTC).date() - activity_date).days
        if age_days > self._max_fii_dii_age_days:
            raise StaleMarketDataError(
                f"{label} is stale: published {age_days} day(s) ago "
                f"(max allowed {self._max_fii_dii_age_days} day(s))."
            )


# ---------------------------------------------------------------------- #
# JSON serialization (SRP: separated from orchestration and validation;
# this is the only class that knows the on-the-wire shape ai_brain.py
# will receive)
# ---------------------------------------------------------------------- #
class MarketDataJsonSerializer:
    """Converts domain entities/value objects into plain, JSON-safe dicts.

    All monetary and decimal values are serialized as strings (never
    float) to avoid floating point rounding surprises for any downstream
    consumer -- ai_brain.py is expected to parse these back into Decimal
    if it needs to do further arithmetic.
    """

    @staticmethod
    def _money(value: Money | None) -> str | None:
        return str(value.amount) if value is not None else None

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    @classmethod
    def serialize_market_quote(cls, quote: MarketQuote) -> dict[str, Any]:
        return {
            "symbol": quote.symbol,
            "last_price": cls._money(quote.last_price),
            "change": cls._decimal(quote.change),
            "change_percent": cls._decimal(quote.change_percent),
            "volume": quote.volume,
            "open_price": cls._money(quote.open_price),
            "high_price": cls._money(quote.high_price),
            "low_price": cls._money(quote.low_price),
            "previous_close": cls._money(quote.previous_close),
            "timestamp": quote.timestamp.isoformat(),
        }

    @classmethod
    def serialize_option_contract(cls, contract: OptionContract) -> dict[str, Any]:
        return {
            "strike_price": cls._decimal(contract.strike_price),
            "option_type": contract.option_type.value,
            "expiry_date": contract.expiry_date.isoformat(),
            "open_interest": contract.open_interest,
            "change_in_open_interest": contract.change_in_open_interest,
            "volume": contract.volume,
            "implied_volatility": cls._decimal(contract.implied_volatility),
            "last_price": cls._money(contract.last_price),
            "bid_price": cls._money(contract.bid_price),
            "ask_price": cls._money(contract.ask_price),
        }

    @classmethod
    def serialize_option_chain(cls, snapshot: OptionChainSnapshot) -> dict[str, Any]:
        return {
            "symbol": snapshot.symbol,
            "expiry_date": snapshot.expiry_date.isoformat(),
            "underlying_value": cls._money(snapshot.underlying_value),
            "timestamp": snapshot.timestamp.isoformat(),
            "contracts": [cls.serialize_option_contract(c) for c in snapshot.contracts],
        }

    @classmethod
    def serialize_pcr(cls, pcr: Pcr) -> dict[str, Any]:
        return {
            "symbol": pcr.symbol,
            "total_put_open_interest": pcr.total_put_open_interest,
            "total_call_open_interest": pcr.total_call_open_interest,
            "ratio": cls._decimal(pcr.ratio),
        }

    @classmethod
    def serialize_max_pain(cls, max_pain: MaxPain) -> dict[str, Any]:
        return {
            "symbol": max_pain.symbol,
            "expiry_date": max_pain.expiry_date.isoformat(),
            "max_pain_strike": cls._decimal(max_pain.max_pain_strike),
            "pain_by_strike": {
                str(strike): str(pain) for strike, pain in max_pain.pain_by_strike.items()
            },
        }

    @classmethod
    def serialize_oi_change(cls, oi_change: OiChangeSummary) -> dict[str, Any]:
        return {
            "symbol": oi_change.symbol,
            "total_call_oi_change": oi_change.total_call_oi_change,
            "total_put_oi_change": oi_change.total_put_oi_change,
            "net_oi_change": oi_change.net_oi_change,
            "by_strike": [
                {
                    "strike_price": cls._decimal(s.strike_price),
                    "call_oi_change": s.call_oi_change,
                    "put_oi_change": s.put_oi_change,
                }
                for s in oi_change.by_strike
            ],
        }

    @classmethod
    def serialize_fii_dii(cls, activity: FiiDiiActivity) -> dict[str, Any]:
        return {
            "activity_date": activity.activity_date.isoformat(),
            "fii_buy_value": cls._money(activity.fii_buy_value),
            "fii_sell_value": cls._money(activity.fii_sell_value),
            "fii_net_value": cls._money(activity.fii_net_value),
            "is_fii_net_buyer": activity.is_fii_net_buyer,
            "dii_buy_value": cls._money(activity.dii_buy_value),
            "dii_sell_value": cls._money(activity.dii_sell_value),
            "dii_net_value": cls._money(activity.dii_net_value),
            "is_dii_net_buyer": activity.is_dii_net_buyer,
        }

    @classmethod
    def serialize_index_report(cls, report: IndexAnalysisReport) -> dict[str, Any]:
        return {
            "index_key": report.index.key,
            "display_name": report.index.display_name,
            "spot": cls.serialize_market_quote(report.spot),
            "option_chain": cls.serialize_option_chain(report.option_chain),
            "pcr": cls.serialize_pcr(report.pcr),
            "max_pain": cls.serialize_max_pain(report.max_pain),
            "oi_change": cls.serialize_oi_change(report.oi_change),
            "generated_at": report.generated_at.isoformat(),
        }

    @classmethod
    def serialize_market_snapshot(cls, snapshot: MarketSnapshot) -> dict[str, Any]:
        return {
            "nifty": cls.serialize_index_report(snapshot.nifty),
            "banknifty": cls.serialize_index_report(snapshot.banknifty),
            "fii_dii": cls.serialize_fii_dii(snapshot.fii_dii),
            "generated_at": snapshot.generated_at.isoformat(),
        }


# ---------------------------------------------------------------------- #
# Orchestrator
# ---------------------------------------------------------------------- #
class MarketAgent:
    """Orchestrates MarketDataService into validated, JSON-ready reports
    for ai_brain.py.

    Holds no state of its own beyond the injected MarketDataService,
    FreshnessValidator, and JsonSerializer -- every call re-fetches
    through the service, which is itself responsible for caching (see
    Phase 2). This class adds no caching or retry logic of its own;
    those are already handled one layer down.
    """

    def __init__(
        self,
        market_data_service: MarketDataService,
        freshness_validator: FreshnessValidator | None = None,
        json_serializer: type[MarketDataJsonSerializer] = MarketDataJsonSerializer,
        agent_logger: logging.Logger | None = None,
    ) -> None:
        self._service = market_data_service
        self._freshness_validator = freshness_validator or FreshnessValidator()
        self._serializer = json_serializer
        self._logger = agent_logger or logger

    # -- Domain-object API (used internally and available to any caller
    #    that wants typed objects instead of dicts) ---------------------
    def analyze_index(self, index_key: str) -> IndexAnalysisReport:
        """Fetch, bundle, and freshness-validate a full live analysis
        report for one tracked index ('NIFTY' or 'BANKNIFTY').

        Raises SymbolNotFoundError for an unrecognized index key,
        StaleMarketDataError if any fetched piece is too old to trust,
        and propagates other MarketDataError subclasses from the
        underlying service untouched -- this method never silently
        swallows a failure or returns a partial report.
        """
        definition = SUPPORTED_INDICES.get(index_key.strip().upper())
        if definition is None:
            supported = ", ".join(sorted(SUPPORTED_INDICES))
            raise SymbolNotFoundError(
                f"'{index_key}' is not a supported tracked index. Supported: {supported}."
            )

        self._logger.info("Fetching live analysis for %s", definition.display_name)
        try:
            spot = self._service.get_spot(definition.spot_symbol, is_index=True)
            option_chain = self._service.get_option_chain(definition.option_chain_symbol)
            pcr = self._service.get_pcr(definition.option_chain_symbol)
            max_pain = self._service.get_max_pain(definition.option_chain_symbol)
            oi_change = self._service.get_oi_change(definition.option_chain_symbol)
        except MarketDataError:
            self._logger.error(
                "Failed to build analysis for %s", definition.display_name, exc_info=True
            )
            raise

        # Validate freshness before this data is trusted by anything
        # downstream. A cache hit that's technically present but old
        # (e.g. the service was last able to reach NSE 20 minutes ago)
        # must not be silently handed off as if it were current.
        self._freshness_validator.validate_live_timestamp(
            spot.timestamp, f"{definition.display_name} spot quote"
        )
        self._freshness_validator.validate_live_timestamp(
            option_chain.timestamp, f"{definition.display_name} option chain"
        )

        self._logger.info(
            "Completed analysis for %s: spot=%s pcr=%s max_pain=%s",
            definition.display_name,
            spot.last_price,
            pcr.ratio,
            max_pain.max_pain_strike,
        )
        return IndexAnalysisReport(
            index=definition,
            spot=spot,
            option_chain=option_chain,
            pcr=pcr,
            max_pain=max_pain,
            oi_change=oi_change,
        )

    def analyze_fii_dii(self) -> FiiDiiActivity:
        """Fetch and freshness-validate the latest published FII/DII
        cash market activity."""
        self._logger.info("Fetching latest FII/DII activity")
        try:
            activity = self._service.get_fii_dii()
        except MarketDataError:
            self._logger.error("Failed to fetch FII/DII activity", exc_info=True)
            raise

        self._freshness_validator.validate_fii_dii_date(
            activity.activity_date, "FII/DII activity"
        )

        self._logger.info(
            "FII/DII activity for %s: FII net=%s (%s), DII net=%s (%s)",
            activity.activity_date,
            activity.fii_net_value,
            "buyer" if activity.is_fii_net_buyer else "seller",
            activity.dii_net_value,
            "buyer" if activity.is_dii_net_buyer else "seller",
        )
        return activity

    def get_market_snapshot(self) -> MarketSnapshot:
        """Build the full NIFTY + BANKNIFTY + FII/DII snapshot in one call."""
        self._logger.info("Building full market snapshot (NIFTY + BANKNIFTY + FII/DII)")
        nifty_report = self.analyze_index(NIFTY.key)
        banknifty_report = self.analyze_index(BANKNIFTY.key)
        fii_dii_activity = self.analyze_fii_dii()

        return MarketSnapshot(
            nifty=nifty_report,
            banknifty=banknifty_report,
            fii_dii=fii_dii_activity,
        )

    # -- JSON API (the contract ai_brain.py is expected to consume) ----
    def get_index_analysis_json(self, index_key: str) -> dict[str, Any]:
        """Validated, JSON-serializable analysis for one tracked index."""
        report = self.analyze_index(index_key)
        return self._serializer.serialize_index_report(report)

    def get_market_snapshot_json(self) -> dict[str, Any]:
        """Validated, JSON-serializable full market snapshot -- this is
        the primary hand-off point to ai_brain.py."""
        snapshot = self.get_market_snapshot()
        return self._serializer.serialize_market_snapshot(snapshot)

    def get_market_snapshot_json_string(self, indent: int | None = 2) -> str:
        """Convenience wrapper: the same snapshot as a JSON string, for
        callers that want to log, persist, or transmit it as text."""
        return json.dumps(self.get_market_snapshot_json(), indent=indent)
