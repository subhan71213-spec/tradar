"""strategy_agent.py — the Strategy Generation Engine.

Application-layer orchestrator, same pattern as the other agents in
this package: fetch -> compute -> validate -> serialize, split into
single-responsibility classes in one file. This file generates trade
SETUPS (entry/stop-loss/targets/position-size/confidence) for paper
trading only -- it places no orders and connects to no broker.

Five strategy types, each with its own risk/target multiplier profile:
    - Option Buying   -- premium-based SL/targets on the nearest ATM
                          option contract (from MarketAgent's option
                          chain), since option P&L is driven by premium
                          movement, not underlying-point movement.
    - Option Selling   -- same premium basis, inverted risk profile
                          (defined-risk credit approach: small target,
                          tighter max-loss multiple).
    - Intraday         -- underlying-price-based, using the VIX-implied
                          expected daily move as the distance unit.
    - Swing            -- underlying-price-based, using the expected
                          move scaled to a multi-day holding period
                          (sqrt-of-time scaling).
    - Scalping         -- underlying-price-based, tightest multiples of
                          the expected daily move.

Expected move is derived from India VIX via the standard approximation
    expected_daily_move = spot * (VIX / 100) / sqrt(252)
This is a coarse, widely-used approximation (constant-volatility,
lognormal assumption) -- not a substitute for a real options-pricing or
historical-ATR model. It is used here because this codebase has no
historical OHLC/candle data source (candlestick pattern detection was
explicitly deferred to a later phase), so VIX is the only volatility
signal actually available.

Direction and confidence are driven by MarketSentimentAgent's output:
the strategy's bias follows the overall raw score's sign, and confidence
is adjusted up when the strategy's direction agrees with that bias and
down when it doesn't.

IMPORTANT: every StrategySignal is a paper-trading setup for further
review, not investment advice, and is only as good as the heuristics
documented above. This module contains no order-execution capability.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from titan_ai_trader.application.services.market_agent import MarketAgent
from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.application.services.market_sentiment_agent import MarketSentimentAgent
from titan_ai_trader.domain.entities.option_contract import OptionContract
from titan_ai_trader.domain.enums.option_type import OptionType
from titan_ai_trader.domain.enums.trade_side import TradeSide
from titan_ai_trader.domain.exceptions.market_data_exceptions import MarketDataError
from titan_ai_trader.domain.value_objects.money import Money

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class StrategyType(StrEnum):
    OPTION_BUYING = "OPTION_BUYING"
    OPTION_SELLING = "OPTION_SELLING"
    INTRADAY = "INTRADAY"
    SWING = "SWING"
    SCALPING = "SCALPING"


@dataclass(frozen=True, slots=True)
class RiskParameters:
    """Account-level risk configuration used to size every strategy."""

    capital: Decimal = Decimal("100000")
    risk_per_trade_pct: Decimal = Decimal("1.0")  # % of capital risked per trade
    nifty_lot_size: int = 75
    banknifty_lot_size: int = 30
    min_confidence_to_size: Decimal = Decimal("30")  # below this, position_size is forced to 0

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ValueError("capital must be positive.")
        if not (Decimal("0") < self.risk_per_trade_pct <= Decimal("100")):
            raise ValueError("risk_per_trade_pct must be in (0, 100].")
        if self.nifty_lot_size <= 0 or self.banknifty_lot_size <= 0:
            raise ValueError("Lot sizes must be positive.")

    def lot_size_for(self, symbol: str) -> int:
        return self.banknifty_lot_size if symbol.upper() == "BANKNIFTY" else self.nifty_lot_size

    @property
    def risk_amount(self) -> Decimal:
        return _quantize(self.capital * self.risk_per_trade_pct / Decimal("100"))


@dataclass(frozen=True, slots=True)
class _StrategyProfile:
    """SL/target multiples applied to the expected-move distance unit."""

    sl_multiple: Decimal
    target_multiples: tuple[Decimal, Decimal, Decimal]
    holding_days: int = 1  # used to scale expected move for multi-day strategies


# Multiples are expressed against the single-day expected move (or, for
# option strategies, against the option's own premium).
_STRATEGY_PROFILES: dict[StrategyType, _StrategyProfile] = {
    StrategyType.SCALPING: _StrategyProfile(
        sl_multiple=Decimal("0.15"),
        target_multiples=(Decimal("0.15"), Decimal("0.30"), Decimal("0.50")),
    ),
    StrategyType.INTRADAY: _StrategyProfile(
        sl_multiple=Decimal("0.35"),
        target_multiples=(Decimal("0.35"), Decimal("0.70"), Decimal("1.10")),
    ),
    StrategyType.SWING: _StrategyProfile(
        sl_multiple=Decimal("1.00"),
        target_multiples=(Decimal("1.00"), Decimal("2.00"), Decimal("3.50")),
        holding_days=5,
    ),
    # Option strategies use premium-based multiples (fraction of premium
    # at risk / targeted), not the underlying expected-move unit.
    StrategyType.OPTION_BUYING: _StrategyProfile(
        sl_multiple=Decimal("0.40"),
        target_multiples=(Decimal("0.50"), Decimal("1.00"), Decimal("1.50")),
    ),
    StrategyType.OPTION_SELLING: _StrategyProfile(
        sl_multiple=Decimal("1.00"),  # defined-risk: max loss = 1x credit received
        target_multiples=(Decimal("0.30"), Decimal("0.50"), Decimal("0.70")),
    ),
}

_UNDERLYING_BASED_STRATEGIES = frozenset(
    {StrategyType.INTRADAY, StrategyType.SWING, StrategyType.SCALPING}
)


@dataclass(frozen=True, slots=True)
class StrategySignal:
    """One fully-specified paper-trading setup."""

    strategy_type: StrategyType
    symbol: str
    direction: TradeSide
    entry: Decimal
    stop_loss: Decimal
    target_1: Decimal
    target_2: Decimal
    target_3: Decimal
    risk_reward_1: Decimal
    risk_reward_2: Decimal
    risk_reward_3: Decimal
    position_size: int
    confidence_score: Decimal
    basis: dict[str, str] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=_utcnow)


class ExpectedMoveCalculator:
    """Approximates the expected single-session price move from VIX."""

    _TRADING_DAYS_PER_YEAR = 252

    @classmethod
    def daily_move(cls, spot: Decimal, vix_value: Decimal) -> Decimal:
        move = spot * (vix_value / Decimal("100")) / Decimal(str(math.sqrt(cls._TRADING_DAYS_PER_YEAR)))
        return _quantize(move)

    @classmethod
    def scaled_move(cls, spot: Decimal, vix_value: Decimal, holding_days: int) -> Decimal:
        daily = cls.daily_move(spot, vix_value)
        return _quantize(daily * Decimal(str(math.sqrt(max(holding_days, 1)))))


class RiskRewardCalculator:
    @staticmethod
    def ratio(entry: Decimal, stop_loss: Decimal, target: Decimal) -> Decimal:
        risk = abs(entry - stop_loss)
        if risk == 0:
            return Decimal("0")
        reward = abs(target - entry)
        return _quantize(reward / risk)


class PositionSizer:
    @staticmethod
    def size_for_underlying(risk_amount: Decimal, sl_distance: Decimal) -> int:
        if sl_distance <= 0:
            return 0
        return int(risk_amount / sl_distance)

    @staticmethod
    def size_for_option(risk_amount: Decimal, sl_premium_distance: Decimal, lot_size: int) -> int:
        if sl_premium_distance <= 0 or lot_size <= 0:
            return 0
        lots = risk_amount / (sl_premium_distance * Decimal(lot_size))
        return int(lots) * lot_size  # whole lots, expressed in units


class ConfidenceScorer:
    """Blends MarketSentimentAgent's confidence with directional agreement
    between the strategy's bias and the overall market bias."""

    @staticmethod
    def score(sentiment_confidence: Decimal, raw_market_score: Decimal, direction: TradeSide) -> Decimal:
        agrees = (direction == TradeSide.LONG and raw_market_score >= 0) or (
            direction == TradeSide.SHORT and raw_market_score <= 0
        )
        agreement_strength = abs(raw_market_score) / Decimal("100")
        adjustment = (Decimal("15") if agrees else Decimal("-15")) * agreement_strength
        result = sentiment_confidence + adjustment
        return max(Decimal("0"), min(Decimal("100"), _quantize(result)))


class StrategyJsonSerializer:
    @staticmethod
    def serialize_signal(signal: StrategySignal) -> dict[str, Any]:
        return {
            "strategy_type": signal.strategy_type.value,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "entry": str(signal.entry),
            "stop_loss": str(signal.stop_loss),
            "target_1": str(signal.target_1),
            "target_2": str(signal.target_2),
            "target_3": str(signal.target_3),
            "risk_reward_1": str(signal.risk_reward_1),
            "risk_reward_2": str(signal.risk_reward_2),
            "risk_reward_3": str(signal.risk_reward_3),
            "position_size": signal.position_size,
            "confidence_score": str(signal.confidence_score),
            "basis": signal.basis,
            "generated_at": signal.generated_at.isoformat(),
        }

    @classmethod
    def serialize_all(cls, signals: list[StrategySignal]) -> dict[str, Any]:
        return {
            "strategies": [cls.serialize_signal(s) for s in signals],
            "generated_at": _utcnow().isoformat(),
        }


class StrategyAgent:
    """Orchestrates MarketAgent + MarketSentimentAgent + MarketDataService
    into concrete StrategySignals for all five strategy types.
    """

    def __init__(
        self,
        market_agent: MarketAgent,
        market_sentiment_agent: MarketSentimentAgent,
        market_data_service: MarketDataService,
        risk_parameters: RiskParameters | None = None,
        json_serializer: type[StrategyJsonSerializer] = StrategyJsonSerializer,
        agent_logger: logging.Logger | None = None,
    ) -> None:
        self._market_agent = market_agent
        self._sentiment_agent = market_sentiment_agent
        self._market_data_service = market_data_service
        self._risk_params = risk_parameters or RiskParameters()
        self._serializer = json_serializer
        self._logger = agent_logger or logger

    def _pick_atm_contract(
        self, contracts: tuple[OptionContract, ...], spot: Decimal, option_type: OptionType
    ) -> OptionContract | None:
        candidates = [c for c in contracts if c.option_type == option_type]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c.strike_price - spot))

    def generate_strategy(self, strategy_type: StrategyType, symbol: str = "NIFTY") -> StrategySignal:
        self._logger.info("Generating %s strategy for %s", strategy_type.value, symbol)

        index_report = self._market_agent.analyze_index(symbol)
        sentiment = self._sentiment_agent.get_market_sentiment_json()
        raw_score = Decimal(sentiment["raw_score_neg100_100"])
        sentiment_confidence = Decimal(sentiment["confidence_pct"])
        direction = TradeSide.LONG if raw_score >= 0 else TradeSide.SHORT

        vix = self._market_data_service.get_india_vix()
        spot = index_report.spot.last_price.amount
        profile = _STRATEGY_PROFILES[strategy_type]
        risk_amount = self._risk_params.risk_amount

        if strategy_type in _UNDERLYING_BASED_STRATEGIES:
            move = ExpectedMoveCalculator.scaled_move(spot, vix.value, profile.holding_days)
            sl_distance = _quantize(move * profile.sl_multiple)
            targets = tuple(_quantize(move * m) for m in profile.target_multiples)

            if direction == TradeSide.LONG:
                stop_loss = _quantize(spot - sl_distance)
                target_1, target_2, target_3 = (_quantize(spot + t) for t in targets)
            else:
                stop_loss = _quantize(spot + sl_distance)
                target_1, target_2, target_3 = (_quantize(spot - t) for t in targets)
            entry = _quantize(spot)
            position_size = PositionSizer.size_for_underlying(risk_amount, sl_distance)
            basis = {
                "expected_move": str(move),
                "vix": str(vix.value),
                "spot": str(spot),
                "holding_days": str(profile.holding_days),
            }
        else:
            option_type = OptionType.CE if direction == TradeSide.LONG else OptionType.PE
            contract = self._pick_atm_contract(index_report.option_chain.contracts, spot, option_type)
            if contract is None:
                raise MarketDataError(
                    f"No {option_type.value} contracts available near spot for {symbol}."
                )
            premium = contract.last_price.amount
            sl_distance = _quantize(premium * profile.sl_multiple)
            targets = tuple(_quantize(premium * m) for m in profile.target_multiples)

            if strategy_type == StrategyType.OPTION_BUYING:
                entry = premium
                stop_loss = _quantize(max(Decimal("0.05"), premium - sl_distance))
                target_1, target_2, target_3 = (_quantize(premium + t) for t in targets)
            else:  # OPTION_SELLING: short premium, profit as premium decays toward zero
                entry = premium
                stop_loss = _quantize(premium + sl_distance)
                target_1, target_2, target_3 = (
                    _quantize(max(Decimal("0.05"), premium - t)) for t in targets
                )

            lot_size = self._risk_params.lot_size_for(symbol)
            position_size = PositionSizer.size_for_option(risk_amount, sl_distance, lot_size)
            basis = {
                "option_type": option_type.value,
                "strike_price": str(contract.strike_price),
                "premium": str(premium),
                "expiry_date": contract.expiry_date.isoformat(),
                "lot_size": str(lot_size),
            }

        if sentiment_confidence < self._risk_params.min_confidence_to_size:
            self._logger.info(
                "Confidence %.1f%% below min_confidence_to_size %.1f%%; zeroing position size.",
                sentiment_confidence,
                self._risk_params.min_confidence_to_size,
            )
            position_size = 0

        confidence_score = ConfidenceScorer.score(sentiment_confidence, raw_score, direction)

        return StrategySignal(
            strategy_type=strategy_type,
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            target_3=target_3,
            risk_reward_1=RiskRewardCalculator.ratio(entry, stop_loss, target_1),
            risk_reward_2=RiskRewardCalculator.ratio(entry, stop_loss, target_2),
            risk_reward_3=RiskRewardCalculator.ratio(entry, stop_loss, target_3),
            position_size=position_size,
            confidence_score=confidence_score,
            basis=basis,
        )

    def generate_all_strategies(self, symbol: str = "NIFTY") -> list[StrategySignal]:
        signals = []
        for strategy_type in StrategyType:
            try:
                signals.append(self.generate_strategy(strategy_type, symbol))
            except MarketDataError as exc:
                self._logger.error(
                    "Skipping %s for %s: %s", strategy_type.value, symbol, exc
                )
        return signals

    def get_strategies_json(self, symbol: str = "NIFTY") -> dict[str, Any]:
        signals = self.generate_all_strategies(symbol)
        return self._serializer.serialize_all(signals)

    def get_strategies_json_string(self, indent: int | None = 2, **kwargs: Any) -> str:
        return json.dumps(self.get_strategies_json(**kwargs), indent=indent)
