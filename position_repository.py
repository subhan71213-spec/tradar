"""market_sentiment_agent.py — the Market Sentiment Aggregation Engine.

Application-layer orchestrator, same pattern as market_agent.py /
fii_dii_agent.py / news_agent.py: fetch -> normalize -> score -> validate
-> serialize, split into single-responsibility classes in one file. No
order execution of any kind happens here.

Combines, as inputs:
    - FII/DII institutional sentiment          (wraps FiiDiiAgent)
    - PCR + OI change                          (wraps MarketAgent)
    - India VIX                                (wraps MarketDataService)
    - News sentiment                           (wraps NewsAgent)
    - Global markets (US/Asia indices)         (new: GlobalMarketsProvider)
    - USDINR, Crude, 10Y bond yield             (new: GlobalMarketsProvider)
    - Market breadth (advances/declines)        (optional, caller-supplied --
      see MarketBreadthProvider docstring for why no built-in NSE adapter
      ships by default)

Every sub-signal is optional. Missing signals do not bias the result
toward "neutral" by silently averaging in a zero -- the scorer
renormalizes weights across whichever signals are actually available,
and reports data completeness as part of the confidence score.

Scoring conventions (heuristic, not universal market truth -- documented
explicitly so a reader can judge and retune them, same spirit as
BuildUpAnalyzer's documented price/OI quadrant rule elsewhere in this
codebase):
    - PCR: rising ratio (more put OI than call OI) is read here as a
      mild contrarian-bullish signal (oversold protective put buying).
    - OI change: net call-OI build vs put-OI build is read as
      resistance-building (bearish) vs support-building (bullish).
    - VIX: lower is read as more risk-on/bullish stability.
    - USDINR / crude / bond yields: rising is read as a headwind
      (bearish) for Indian equities; falling as a tailwind (bullish).

Output: Overall Market Score (0-100), Bullish/Bearish/Neutral %,
Confidence %, and a human-readable reasoning list -- all JSON-serializable
for ai_decision_engine.py.
"""

from __future__ import annotations

import json
import logging
import statistics
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from titan_ai_trader.application.services.fii_dii_agent import FiiDiiAgent
from titan_ai_trader.application.services.market_agent import MarketAgent, NIFTY
from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.application.services.news_agent import NewsAgent
from titan_ai_trader.domain.exceptions.market_data_exceptions import MarketDataError
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------- #
# Global markets / macro provider (new in this file)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GlobalQuote:
    symbol: str
    price: Decimal
    previous_close: Decimal
    change_percent: Decimal


class GlobalMarketsProvider:
    """Fetches global index / forex / commodity / bond-yield quotes from
    Yahoo Finance's public, no-API-key chart endpoint.

    A single row per symbol; a bad/unreachable symbol is skipped rather
    than failing the whole batch, since these are all secondary/
    tailwind signals for the sentiment score, not primary data.
    """

    _CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    # Best-effort default symbol map. GIFT Nifty (SGX Nifty's successor
    # since 2023) is intentionally left for the caller to confirm/override
    # via `symbols=` since ticker availability on public feeds varies.
    DEFAULT_SYMBOLS: dict[str, str] = {
        "DOW_JONES": "^DJI",
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
        "NIKKEI": "^N225",
        "HANG_SENG": "^HSI",
        "USDINR": "INR=X",
        "CRUDE_OIL": "CL=F",
        "US10Y_YIELD": "^TNX",
    }

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    @retry_on_network_failure(max_attempts=2)
    def _fetch_raw(self, symbol: str) -> dict:
        url = self._CHART_URL_TEMPLATE.format(symbol=urllib.request.quote(symbol, safe=""))
        request = urllib.request.Request(url, headers={"User-Agent": "TitanAITrader/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"Global quote endpoint returned HTTP {exc.code} for {symbol}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Global quote endpoint unreachable for {symbol}: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Global quote response for {symbol} was not valid JSON") from exc

    def fetch_quote(self, symbol: str) -> GlobalQuote | None:
        """Returns None (rather than raising) if the symbol could not be
        fetched or parsed -- callers treat a missing quote as simply an
        unavailable optional signal."""
        try:
            payload = self._fetch_raw(symbol)
            meta = payload["chart"]["result"][0]["meta"]
            price = Decimal(str(meta["regularMarketPrice"]))
            previous_close = Decimal(
                str(meta.get("previousClose") or meta["chartPreviousClose"])
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any
            # parsing/network failure degrades this optional signal to
            # "unavailable" rather than propagating, per this provider's
            # documented contract.
            logger.warning("Could not fetch/parse global quote for %s: %s", symbol, exc)
            return None

        if previous_close == 0:
            return None
        change_percent = ((price - previous_close) / previous_close) * Decimal("100")
        return GlobalQuote(symbol=symbol, price=price, previous_close=previous_close, change_percent=change_percent)

    def fetch_all(self, symbols: dict[str, str] | None = None) -> dict[str, GlobalQuote]:
        symbol_map = symbols or self.DEFAULT_SYMBOLS
        results: dict[str, GlobalQuote] = {}
        for label, ticker in symbol_map.items():
            quote = self.fetch_quote(ticker)
            if quote is not None:
                results[label] = quote
        return results


# ---------------------------------------------------------------------- #
# Market breadth (optional, caller-supplied)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MarketBreadth:
    """Advances/declines for the session. No default NSE adapter ships
    for this in this file -- NSE's breadth endpoint was not something
    this codebase could verify against a live response in its build
    environment, and shipping an unverified scraper as if it were
    production-ready would be worse than not shipping one. Supply this
    from whatever source you've validated (e.g. your own NSE breadth
    call, a data vendor, or a manual override) via
    MarketSentimentAgent.score(..., market_breadth=...).
    """

    advances: int
    declines: int
    unchanged: int = 0

    @property
    def breadth_score(self) -> Decimal:
        total = self.advances + self.declines
        if total == 0:
            return Decimal("0")
        return Decimal(self.advances - self.declines) / Decimal(total) * Decimal("100")


# ---------------------------------------------------------------------- #
# Pure scoring engine (SRP: no I/O, fully unit-testable)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SentimentInputs:
    """Every sub-score is on a -100 (max bearish) .. +100 (max bullish)
    scale, or None if that signal was unavailable."""

    fii_dii_score: Decimal | None = None
    pcr_score: Decimal | None = None
    vix_score: Decimal | None = None
    oi_score: Decimal | None = None
    news_score: Decimal | None = None
    market_breadth_score: Decimal | None = None
    global_markets_score: Decimal | None = None
    usdinr_score: Decimal | None = None
    crude_score: Decimal | None = None
    bond_yield_score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MarketSentimentResult:
    overall_score_0_100: Decimal
    raw_score_neg100_100: Decimal
    bullish_pct: Decimal
    bearish_pct: Decimal
    neutral_pct: Decimal
    confidence_pct: Decimal
    reasoning: tuple[str, ...]
    components: dict[str, str | None]


class MarketSentimentScorer:
    """Combines SentimentInputs into one MarketSentimentResult.

    Pure function class: no network, no logging side effects beyond what
    the caller does with the result -- fully deterministic and testable
    with hand-built SentimentInputs.
    """

    WEIGHTS: dict[str, float] = {
        "fii_dii_score": 20.0,
        "pcr_score": 12.0,
        "vix_score": 12.0,
        "oi_score": 12.0,
        "news_score": 15.0,
        "market_breadth_score": 8.0,
        "global_markets_score": 10.0,
        "usdinr_score": 6.0,
        "crude_score": 3.0,
        "bond_yield_score": 2.0,
    }

    _LABELS: dict[str, str] = {
        "fii_dii_score": "FII/DII institutional activity",
        "pcr_score": "Put-Call Ratio",
        "vix_score": "India VIX",
        "oi_score": "Open Interest change",
        "news_score": "News sentiment",
        "market_breadth_score": "Market breadth (advances/declines)",
        "global_markets_score": "Global markets",
        "usdinr_score": "USD/INR",
        "crude_score": "Crude oil",
        "bond_yield_score": "Bond yield",
    }

    def score(self, inputs: SentimentInputs) -> MarketSentimentResult:
        available: dict[str, float] = {}
        components: dict[str, str | None] = {}
        for field_name in self.WEIGHTS:
            value = getattr(inputs, field_name)
            components[field_name] = str(value) if value is not None else None
            if value is not None:
                available[field_name] = float(value)

        if not available:
            return MarketSentimentResult(
                overall_score_0_100=Decimal("50"),
                raw_score_neg100_100=Decimal("0"),
                bullish_pct=Decimal("0"),
                bearish_pct=Decimal("0"),
                neutral_pct=Decimal("100"),
                confidence_pct=Decimal("0"),
                reasoning=("No sentiment signals were available.",),
                components=components,
            )

        total_weight = sum(self.WEIGHTS[k] for k in available)
        raw_score = sum(available[k] * self.WEIGHTS[k] for k in available) / total_weight
        raw_score = _clamp(raw_score)

        completeness = (total_weight / sum(self.WEIGHTS.values())) * 100.0
        if len(available) >= 2:
            agreement = _clamp(100.0 - statistics.pstdev(available.values()) * 0.6, 0.0, 100.0)
        else:
            agreement = 50.0  # a single signal carries no agreement information
        confidence = _clamp((completeness * 0.5) + (agreement * 0.5), 0.0, 100.0)

        neutral_pct = _clamp(40.0 - abs(raw_score) * 0.4, 5.0, 40.0)
        remaining = 100.0 - neutral_pct
        if raw_score >= 0:
            bullish_pct = remaining * (0.5 + raw_score / 200.0)
            bearish_pct = remaining - bullish_pct
        else:
            bearish_pct = remaining * (0.5 - raw_score / 200.0)
            bullish_pct = remaining - bearish_pct

        reasoning = [
            f"{self._LABELS[k]}: {available[k]:+.1f} (weight {self.WEIGHTS[k]:.0f})"
            for k in available
        ]
        reasoning.append(
            f"{len(available)}/{len(self.WEIGHTS)} signal(s) available "
            f"({completeness:.0f}% data completeness)."
        )

        return MarketSentimentResult(
            overall_score_0_100=Decimal(str(round((raw_score + 100.0) / 2.0, 1))),
            raw_score_neg100_100=Decimal(str(round(raw_score, 1))),
            bullish_pct=Decimal(str(round(bullish_pct, 1))),
            bearish_pct=Decimal(str(round(bearish_pct, 1))),
            neutral_pct=Decimal(str(round(neutral_pct, 1))),
            confidence_pct=Decimal(str(round(confidence, 1))),
            reasoning=tuple(reasoning),
            components=components,
        )


# ---------------------------------------------------------------------- #
# JSON serialization
# ---------------------------------------------------------------------- #
class MarketSentimentJsonSerializer:
    @staticmethod
    def serialize(result: MarketSentimentResult, generated_at: datetime) -> dict[str, Any]:
        return {
            "overall_score_0_100": str(result.overall_score_0_100),
            "raw_score_neg100_100": str(result.raw_score_neg100_100),
            "bullish_pct": str(result.bullish_pct),
            "bearish_pct": str(result.bearish_pct),
            "neutral_pct": str(result.neutral_pct),
            "confidence_pct": str(result.confidence_pct),
            "reasoning": list(result.reasoning),
            "components": result.components,
            "generated_at": generated_at.isoformat(),
        }


# ---------------------------------------------------------------------- #
# Orchestrator
# ---------------------------------------------------------------------- #
class MarketSentimentAgent:
    """Orchestrates MarketAgent + FiiDiiAgent + NewsAgent +
    GlobalMarketsProvider into one MarketSentimentResult, JSON-ready for
    ai_decision_engine.py.
    """

    def __init__(
        self,
        market_agent: MarketAgent,
        fii_dii_agent: FiiDiiAgent,
        news_agent: NewsAgent,
        market_data_service: MarketDataService,
        global_markets_provider: GlobalMarketsProvider | None = None,
        scorer: MarketSentimentScorer | None = None,
        json_serializer: type[MarketSentimentJsonSerializer] = MarketSentimentJsonSerializer,
        agent_logger: logging.Logger | None = None,
    ) -> None:
        self._market_agent = market_agent
        self._fii_dii_agent = fii_dii_agent
        self._news_agent = news_agent
        self._market_data_service = market_data_service
        self._global_markets = global_markets_provider or GlobalMarketsProvider()
        self._scorer = scorer or MarketSentimentScorer()
        self._serializer = json_serializer
        self._logger = agent_logger or logger

    def _score_fii_dii(self) -> Decimal | None:
        try:
            report = self._fii_dii_agent.get_fii_dii_report_json()
            total_score = float(report["institutional_sentiment"]["total_score"])
            return Decimal(str(round(_clamp(total_score * 20.0), 1)))
        except (MarketDataError, KeyError, ValueError) as exc:
            self._logger.warning("FII/DII signal unavailable: %s", exc)
            return None

    def _score_pcr_and_oi(self) -> tuple[Decimal | None, Decimal | None]:
        try:
            report = self._market_agent.analyze_index(NIFTY.key)
            pcr_ratio = float(report.pcr.ratio)
            pcr_score = Decimal(str(round(_clamp((pcr_ratio - 1.0) * 100.0), 1)))

            net_change = report.oi_change.net_oi_change
            total_change = (
                abs(report.oi_change.total_call_oi_change)
                + abs(report.oi_change.total_put_oi_change)
                + 1
            )
            oi_score = Decimal(
                str(round(_clamp(-(net_change) / total_change * 100.0), 1))
            )
            return pcr_score, oi_score
        except (MarketDataError, ZeroDivisionError) as exc:
            self._logger.warning("PCR/OI signal unavailable: %s", exc)
            return None, None

    def _score_vix(self) -> Decimal | None:
        try:
            vix = self._market_data_service.get_india_vix()
            return Decimal(str(round(_clamp((15.0 - float(vix.value)) * 8.0), 1)))
        except MarketDataError as exc:
            self._logger.warning("VIX signal unavailable: %s", exc)
            return None

    def _score_news(self) -> Decimal | None:
        try:
            report = self._news_agent.get_market_news_json()
            sentiment = report["overall_sentiment"]
            total = sentiment["total_articles"]
            if total == 0:
                return None
            score = (sentiment["bullish_count"] - sentiment["bearish_count"]) / total * 100.0
            return Decimal(str(round(_clamp(score), 1)))
        except Exception as exc:  # noqa: BLE001 - news is a secondary signal
            self._logger.warning("News sentiment signal unavailable: %s", exc)
            return None

    def _score_global_markets(
        self, quotes: dict[str, GlobalQuote]
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        index_labels = ("DOW_JONES", "NASDAQ", "SP500", "NIKKEI", "HANG_SENG")
        index_changes = [float(quotes[k].change_percent) for k in index_labels if k in quotes]
        global_score = (
            Decimal(str(round(_clamp(statistics.fmean(index_changes) * 10.0), 1)))
            if index_changes
            else None
        )

        usdinr_score = (
            Decimal(str(round(_clamp(-float(quotes["USDINR"].change_percent) * 15.0), 1)))
            if "USDINR" in quotes
            else None
        )
        crude_score = (
            Decimal(str(round(_clamp(-float(quotes["CRUDE_OIL"].change_percent) * 8.0), 1)))
            if "CRUDE_OIL" in quotes
            else None
        )
        bond_yield_score = (
            Decimal(str(round(_clamp(-float(quotes["US10Y_YIELD"].change_percent) * 10.0), 1)))
            if "US10Y_YIELD" in quotes
            else None
        )
        return global_score, usdinr_score, crude_score, bond_yield_score

    def get_market_sentiment_json(
        self, market_breadth: MarketBreadth | None = None
    ) -> dict[str, Any]:
        """Fetch every available signal, score them, and return a JSON-
        ready result for ai_decision_engine.py. `market_breadth` is an
        optional caller-supplied override (see MarketBreadth docstring)."""
        self._logger.info("Computing overall market sentiment")

        fii_dii_score = self._score_fii_dii()
        pcr_score, oi_score = self._score_pcr_and_oi()
        vix_score = self._score_vix()
        news_score = self._score_news()

        quotes = self._global_markets.fetch_all()
        global_score, usdinr_score, crude_score, bond_yield_score = self._score_global_markets(
            quotes
        )

        breadth_score = (
            Decimal(str(round(float(market_breadth.breadth_score), 1)))
            if market_breadth is not None
            else None
        )

        inputs = SentimentInputs(
            fii_dii_score=fii_dii_score,
            pcr_score=pcr_score,
            vix_score=vix_score,
            oi_score=oi_score,
            news_score=news_score,
            market_breadth_score=breadth_score,
            global_markets_score=global_score,
            usdinr_score=usdinr_score,
            crude_score=crude_score,
            bond_yield_score=bond_yield_score,
        )
        result = self._scorer.score(inputs)

        self._logger.info(
            "Market sentiment: score=%s bullish=%s%% bearish=%s%% confidence=%s%%",
            result.overall_score_0_100,
            result.bullish_pct,
            result.bearish_pct,
            result.confidence_pct,
        )
        return self._serializer.serialize(result, _utcnow())

    def get_market_sentiment_json_string(self, indent: int | None = 2, **kwargs: Any) -> str:
        return json.dumps(self.get_market_sentiment_json(**kwargs), indent=indent)
