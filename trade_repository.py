"""fii_dii_agent.py — the FII/DII & F&O Participant OI Engine.

Application-layer orchestrator, structured the same way as
market_agent.py: one file, internally separated into single-
responsibility classes so the clean-architecture boundaries (fetch ->
parse -> compare -> classify -> validate -> serialize) are enforced by
class design even though they live in one module. No order execution
of any kind happens here -- this is read-only institutional-activity
data for a paper trading system.

Two data sources are combined:
    1. Cash market FII/DII buy/sell figures -- already fully built in
       Phase 2 (MarketDataService.get_fii_dii / FiiDiiActivity /
       FiiDiiAdapter). This file WRAPS that, it does not refetch or
       reparse it.
    2. F&O Participant-wise Open Interest (FII/DII/Pro/Client position
       data in index futures & options) -- NEW in this file, since
       Phase 2 does not cover it. NSE publishes this as a daily CSV
       archive file (not a JSON API), one file per trading day, so the
       provider below fetches and parses CSV rather than JSON.

On top of both sources, this file adds:
    - Day-over-day comparison (cash net buying trend, F&O OI change).
    - Long Build-up / Short Build-up / Long Unwinding / Short Covering
      classification, using the standard price-vs-OI quadrant rule.
    - An aggregate institutional sentiment label (Bullish/Bearish/
      Neutral) combining cash-market and F&O signals.
    - Holiday/missing-data handling: NSE simply does not publish either
      feed on non-trading days, so both the cash-market previous-day
      lookup and the participant-OI fetch walk backward through
      calendar days (bounded by a configurable lookback window) until
      they find the most recent trading day with data, rather than
      failing on the first empty day.
    - Validation, retry, timeout, and logging, consistent with the rest
      of the codebase.

SOLID notes:
  - SRP: fetching (ParticipantOiProvider), day-over-day comparison
    (day-over-day methods on FiiDiiAgent), build-up classification
    (BuildUpAnalyzer), sentiment scoring (InstitutionalSentimentAnalyzer),
    and serialization (FiiDiiJsonSerializer) are all separate classes.
  - OCP: adding a new participant category or a new build-up rule means
    extending ParticipantCategory / BuildUpAnalyzer, not editing
    FiiDiiAgent's orchestration methods.
  - LSP: FiiDiiAgent depends only on MarketDataService's and
    ParticipantOiProvider's public contracts; test doubles honoring
    those contracts are drop-in substitutes.
  - ISP: each helper class exposes only the narrow methods its one job
    needs.
  - DIP: FiiDiiAgent depends on injected abstractions (MarketDataService,
    ParticipantOiProvider, BuildUpAnalyzer, InstitutionalSentimentAnalyzer,
    FreshnessValidator) -- never on concrete HTTP/CSV parsing details
    directly.

Reuses (does not duplicate) from earlier phases: MarketDataService,
FiiDiiActivity, MarketDataValidationError, MarketDataUnavailableError,
SymbolNotFoundError, StaleMarketDataError, retry_on_network_failure, and
market_agent.FreshnessValidator.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from titan_ai_trader.application.services.market_agent import FreshnessValidator
from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataError,
    MarketDataUnavailableError,
    MarketDataValidationError,
    StaleMarketDataError,
    SymbolNotFoundError,
)
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _today() -> date:
    return _utcnow().date()


# ---------------------------------------------------------------------- #
# Enums
# ---------------------------------------------------------------------- #
class ParticipantCategory(StrEnum):
    FII = "FII"
    DII = "DII"
    PRO = "PRO"
    CLIENT = "CLIENT"


class BuildUpPattern(StrEnum):
    LONG_BUILD_UP = "LONG_BUILD_UP"
    SHORT_BUILD_UP = "SHORT_BUILD_UP"
    LONG_UNWINDING = "LONG_UNWINDING"
    SHORT_COVERING = "SHORT_COVERING"
    NEUTRAL = "NEUTRAL"


class InstitutionalSentiment(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# Maps a raw NSE "Client Type" CSV value to our enum. Kept as its own
# table (rather than string-matching scattered through the parser) so
# adding a header variant NSE happens to use is a one-line change.
_CLIENT_TYPE_ALIASES: dict[str, ParticipantCategory] = {
    "FII": ParticipantCategory.FII,
    "FPI": ParticipantCategory.FII,
    "DII": ParticipantCategory.DII,
    "PRO": ParticipantCategory.PRO,
    "PROPRIETARY": ParticipantCategory.PRO,
    "CLIENT": ParticipantCategory.CLIENT,
    "CLIENTS": ParticipantCategory.CLIENT,
}


# ---------------------------------------------------------------------- #
# Domain-ish entities (validated at construction, same pattern as the
# Phase 1/2 entities)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ParticipantOiRecord:
    """One participant category's index F&O open interest for one day.

    Only index futures/options fields are modeled -- stock-level F&O OI
    is not needed for the build-up/sentiment analysis this agent
    performs, and omitting it keeps this entity focused (SRP).
    """

    category: ParticipantCategory
    report_date: date
    future_index_long: int
    future_index_short: int
    option_index_call_long: int
    option_index_put_long: int
    option_index_call_short: int
    option_index_put_short: int

    def __post_init__(self) -> None:
        for field_name in (
            "future_index_long",
            "future_index_short",
            "option_index_call_long",
            "option_index_put_long",
            "option_index_call_short",
            "option_index_put_short",
        ):
            if getattr(self, field_name) < 0:
                raise MarketDataValidationError(
                    f"ParticipantOiRecord.{field_name} cannot be negative."
                )

    @property
    def net_future_index_position(self) -> int:
        """Net long index-futures OI (positive = net long, negative = net short)."""
        return self.future_index_long - self.future_index_short

    @property
    def net_option_index_bias(self) -> int:
        """Approximate net bullish option positioning: (call long - call
        short) minus (put long - put short). Positive leans bullish."""
        call_net = self.option_index_call_long - self.option_index_call_short
        put_net = self.option_index_put_long - self.option_index_put_short
        return call_net - put_net


@dataclass(frozen=True, slots=True)
class ParticipantOiSnapshot:
    """All participant categories' index F&O OI for a single trading day."""

    report_date: date
    records: dict[ParticipantCategory, ParticipantOiRecord]

    def __post_init__(self) -> None:
        if not self.records:
            raise MarketDataValidationError(
                "ParticipantOiSnapshot must contain at least one participant record."
            )

    def get(self, category: ParticipantCategory) -> ParticipantOiRecord | None:
        return self.records.get(category)


# ---------------------------------------------------------------------- #
# Participant OI provider (new in this file -- NSE publishes this as a
# daily CSV archive, not JSON, so parsing differs from the Phase 2 NSE
# adapters)
# ---------------------------------------------------------------------- #
class ParticipantOiProvider:
    """Fetches and parses NSE's daily Participant-wise Open Interest CSV.

    NSE only publishes this file for actual trading days -- there is no
    file for weekends/holidays. `fetch_snapshot` walks backward from the
    requested date (bounded by max_lookback_days) until it finds a day
    with a published file, which is how holiday/missing-data handling is
    satisfied for this feed.
    """

    _ARCHIVE_URL_TEMPLATE = (
        "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date_str}.csv"
    )

    def __init__(self, timeout_seconds: float = 10.0, max_lookback_days: int = 7) -> None:
        if max_lookback_days < 0:
            raise ValueError("max_lookback_days cannot be negative.")
        self._timeout = timeout_seconds
        self._max_lookback_days = max_lookback_days

    @retry_on_network_failure(max_attempts=3)
    def _download(self, url: str) -> str | None:
        """Return the response body as text, or None if the file simply
        does not exist for that date (HTTP 404 -- a holiday/weekend,
        not a failure). Any other network problem is raised as OSError
        so the retry decorator wrapping this method retries it; a 404
        is a legitimate, expected outcome and is never retried."""
        request = urllib.request.Request(url, headers={"User-Agent": "TitanAITrader/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise OSError(f"Participant OI archive returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Participant OI archive unreachable: {exc.reason}") from exc

    def fetch_snapshot(self, as_of: date | None = None) -> ParticipantOiSnapshot:
        """Fetch the most recent available snapshot at or before `as_of`
        (defaults to today), walking backward through calendar days to
        skip weekends/holidays. Raises SymbolNotFoundError if nothing is
        found within max_lookback_days, and MarketDataUnavailableError
        if a genuine network failure (not a 404) exhausts its retries.
        """
        anchor = as_of or _today()
        for offset in range(self._max_lookback_days + 1):
            probe_date = anchor - timedelta(days=offset)
            url = self._ARCHIVE_URL_TEMPLATE.format(date_str=probe_date.strftime("%d%m%Y"))
            text = self._download(url)
            if text is None:
                logger.info("No participant OI file for %s (holiday/weekend); trying earlier day.", probe_date)
                continue
            snapshot = self._parse_csv(text, probe_date)
            if snapshot is not None:
                return snapshot
            logger.warning("Participant OI file for %s did not parse into any records.", probe_date)

        raise SymbolNotFoundError(
            f"No participant OI data found within {self._max_lookback_days} day(s) "
            f"before {anchor.isoformat()}."
        )

    @staticmethod
    def _find_column(
        headers: list[str], include: tuple[str, ...], exclude: tuple[str, ...] = ()
    ) -> str | None:
        for header in headers:
            lowered = header.lower()
            if all(token in lowered for token in include) and not any(
                token in lowered for token in exclude
            ):
                return header
        return None

    @staticmethod
    def _parse_int(raw_value: str | None) -> int:
        if raw_value is None:
            return 0
        cleaned = raw_value.strip().replace(",", "")
        if cleaned in ("", "-"):
            return 0
        try:
            return int(float(cleaned))
        except ValueError as exc:
            raise MarketDataValidationError(
                f"Could not parse participant OI numeric value: {raw_value!r}"
            ) from exc

    def _parse_csv(self, text: str, report_date: date) -> ParticipantOiSnapshot | None:
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        if not headers:
            return None

        client_type_col = self._find_column(headers, ("client",))
        future_index_long_col = self._find_column(headers, ("future", "index", "long"), ("notional",))
        future_index_short_col = self._find_column(headers, ("future", "index", "short"), ("notional",))
        option_index_call_long_col = self._find_column(
            headers, ("option", "index", "call", "long"), ("notional",)
        )
        option_index_put_long_col = self._find_column(
            headers, ("option", "index", "put", "long"), ("notional",)
        )
        option_index_call_short_col = self._find_column(
            headers, ("option", "index", "call", "short"), ("notional",)
        )
        option_index_put_short_col = self._find_column(
            headers, ("option", "index", "put", "short"), ("notional",)
        )

        required = {
            "client type": client_type_col,
            "future index long": future_index_long_col,
            "future index short": future_index_short_col,
        }
        missing = [name for name, col in required.items() if col is None]
        if missing:
            raise MarketDataValidationError(
                f"Participant OI CSV for {report_date} is missing required column(s): {missing}."
            )

        records: dict[ParticipantCategory, ParticipantOiRecord] = {}
        for row in reader:
            raw_client_type = (row.get(client_type_col) or "").strip().upper()
            category = _CLIENT_TYPE_ALIASES.get(raw_client_type)
            if category is None:
                continue  # e.g. a "TOTAL" row, or a header/footer artifact

            try:
                record = ParticipantOiRecord(
                    category=category,
                    report_date=report_date,
                    future_index_long=self._parse_int(row.get(future_index_long_col)),
                    future_index_short=self._parse_int(row.get(future_index_short_col)),
                    option_index_call_long=self._parse_int(
                        row.get(option_index_call_long_col) if option_index_call_long_col else None
                    ),
                    option_index_put_long=self._parse_int(
                        row.get(option_index_put_long_col) if option_index_put_long_col else None
                    ),
                    option_index_call_short=self._parse_int(
                        row.get(option_index_call_short_col) if option_index_call_short_col else None
                    ),
                    option_index_put_short=self._parse_int(
                        row.get(option_index_put_short_col) if option_index_put_short_col else None
                    ),
                )
            except MarketDataValidationError as exc:
                logger.debug("Skipping malformed participant OI row (%s): %r", exc, row)
                continue

            records[category] = record

        if not records:
            return None
        return ParticipantOiSnapshot(report_date=report_date, records=records)


# ---------------------------------------------------------------------- #
# Build-up classification (SRP: isolated, deterministic)
# ---------------------------------------------------------------------- #
class BuildUpAnalyzer:
    """Classifies a day-over-day net OI change into Long Build-up / Short
    Build-up / Long Unwinding / Short Covering, using the standard rule:

        Price up,   OI up    -> Long Build-up    (new longs entering)
        Price down, OI up    -> Short Build-up   (new shorts entering)
        Price up,   OI down  -> Short Covering   (shorts exiting)
        Price down, OI down  -> Long Unwinding   (longs exiting)

    This agent has no price feed of its own -- `price_change` must be
    supplied by the caller (e.g. from market_agent's NIFTY/BANKNIFTY
    spot `change` field). Without it, the direction is genuinely
    ambiguous (rising OI alone cannot distinguish new longs from new
    shorts), so classification correctly falls back to NEUTRAL rather
    than guessing.
    """

    @staticmethod
    def classify(net_oi_change: int, price_change: Decimal | None) -> BuildUpPattern:
        if price_change is None or net_oi_change == 0 or price_change == 0:
            return BuildUpPattern.NEUTRAL
        price_up = price_change > 0
        oi_up = net_oi_change > 0
        if price_up and oi_up:
            return BuildUpPattern.LONG_BUILD_UP
        if not price_up and oi_up:
            return BuildUpPattern.SHORT_BUILD_UP
        if price_up and not oi_up:
            return BuildUpPattern.SHORT_COVERING
        return BuildUpPattern.LONG_UNWINDING


# ---------------------------------------------------------------------- #
# Institutional sentiment scoring (SRP: isolated, deterministic)
# ---------------------------------------------------------------------- #
class InstitutionalSentimentAnalyzer:
    """Combines cash-market FII/DII positioning with F&O build-up
    signals into one Bullish/Bearish/Neutral institutional sentiment
    label, plus a transparent score breakdown so ai_brain.py can see
    exactly how the label was derived rather than trusting it blindly.
    """

    _BUILD_UP_SCORES: dict[BuildUpPattern, int] = {
        BuildUpPattern.LONG_BUILD_UP: 2,
        BuildUpPattern.SHORT_COVERING: 1,
        BuildUpPattern.NEUTRAL: 0,
        BuildUpPattern.LONG_UNWINDING: -1,
        BuildUpPattern.SHORT_BUILD_UP: -2,
    }

    def analyze(
        self, cash: FiiDiiActivity, fii_build_up: BuildUpPattern
    ) -> dict[str, Any]:
        fii_cash_score = 2 if cash.is_fii_net_buyer else -2
        dii_cash_score = 1 if cash.is_dii_net_buyer else -1
        build_up_score = self._BUILD_UP_SCORES[fii_build_up]
        total_score = fii_cash_score + dii_cash_score + build_up_score

        if total_score > 0:
            label = InstitutionalSentiment.BULLISH
        elif total_score < 0:
            label = InstitutionalSentiment.BEARISH
        else:
            label = InstitutionalSentiment.NEUTRAL

        return {
            "label": label.value,
            "total_score": total_score,
            "breakdown": {
                "fii_cash_score": fii_cash_score,
                "dii_cash_score": dii_cash_score,
                "fii_fno_build_up_score": build_up_score,
            },
        }


# ---------------------------------------------------------------------- #
# JSON serialization (SRP: the only class that knows the on-the-wire
# shape ai_brain.py will receive)
# ---------------------------------------------------------------------- #
class FiiDiiJsonSerializer:
    """Converts this agent's entities/results into plain, JSON-safe dicts.

    Monetary and decimal values are serialized as strings (never float)
    to avoid floating point rounding surprises for any downstream
    consumer.
    """

    @staticmethod
    def serialize_cash_activity(activity: FiiDiiActivity) -> dict[str, Any]:
        return {
            "activity_date": activity.activity_date.isoformat(),
            "fii_buy_value": str(activity.fii_buy_value.amount),
            "fii_sell_value": str(activity.fii_sell_value.amount),
            "fii_net_value": str(activity.fii_net_value.amount),
            "is_fii_net_buyer": activity.is_fii_net_buyer,
            "dii_buy_value": str(activity.dii_buy_value.amount),
            "dii_sell_value": str(activity.dii_sell_value.amount),
            "dii_net_value": str(activity.dii_net_value.amount),
            "is_dii_net_buyer": activity.is_dii_net_buyer,
        }

    @staticmethod
    def serialize_participant_record(record: ParticipantOiRecord) -> dict[str, Any]:
        return {
            "category": record.category.value,
            "report_date": record.report_date.isoformat(),
            "future_index_long": record.future_index_long,
            "future_index_short": record.future_index_short,
            "net_future_index_position": record.net_future_index_position,
            "option_index_call_long": record.option_index_call_long,
            "option_index_put_long": record.option_index_put_long,
            "option_index_call_short": record.option_index_call_short,
            "option_index_put_short": record.option_index_put_short,
            "net_option_index_bias": record.net_option_index_bias,
        }

    @classmethod
    def serialize_participant_snapshot(
        cls, snapshot: ParticipantOiSnapshot | None
    ) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        return {
            "report_date": snapshot.report_date.isoformat(),
            "records": {
                category.value: cls.serialize_participant_record(record)
                for category, record in snapshot.records.items()
            },
        }

    @staticmethod
    def serialize_cash_comparison(comparison: dict[str, Any]) -> dict[str, Any]:
        return comparison

    @staticmethod
    def serialize_build_up(build_up: dict[str, Any]) -> dict[str, Any]:
        return build_up

    @staticmethod
    def serialize_sentiment(sentiment: dict[str, Any]) -> dict[str, Any]:
        return sentiment

    @classmethod
    def serialize_report(
        cls,
        cash_current: FiiDiiActivity,
        cash_previous: FiiDiiActivity | None,
        cash_comparison: dict[str, Any],
        participant_current: ParticipantOiSnapshot | None,
        participant_previous: ParticipantOiSnapshot | None,
        build_ups: dict[str, dict[str, Any]],
        institutional_sentiment: dict[str, Any],
        generated_at: datetime,
    ) -> dict[str, Any]:
        return {
            "cash_market": {
                "current": cls.serialize_cash_activity(cash_current),
                "previous": (
                    cls.serialize_cash_activity(cash_previous)
                    if cash_previous is not None
                    else None
                ),
                "comparison": cls.serialize_cash_comparison(cash_comparison),
            },
            "fno_participant_oi": {
                "current": cls.serialize_participant_snapshot(participant_current),
                "previous": cls.serialize_participant_snapshot(participant_previous),
            },
            "build_up_analysis": {
                category: cls.serialize_build_up(result)
                for category, result in build_ups.items()
            },
            "institutional_sentiment": cls.serialize_sentiment(institutional_sentiment),
            "generated_at": generated_at.isoformat(),
        }


# ---------------------------------------------------------------------- #
# Orchestrator
# ---------------------------------------------------------------------- #
class FiiDiiAgent:
    """Orchestrates cash-market FII/DII data (via MarketDataService) and
    F&O participant OI data (via ParticipantOiProvider) into a validated,
    JSON-ready institutional-activity report for ai_brain.py.
    """

    def __init__(
        self,
        market_data_service: MarketDataService,
        participant_oi_provider: ParticipantOiProvider | None = None,
        build_up_analyzer: BuildUpAnalyzer | None = None,
        sentiment_analyzer: InstitutionalSentimentAnalyzer | None = None,
        freshness_validator: FreshnessValidator | None = None,
        json_serializer: type[FiiDiiJsonSerializer] = FiiDiiJsonSerializer,
        agent_logger: logging.Logger | None = None,
        max_previous_day_lookback: int = 7,
    ) -> None:
        if max_previous_day_lookback < 1:
            raise ValueError("max_previous_day_lookback must be at least 1.")
        self._service = market_data_service
        self._participant_provider = participant_oi_provider or ParticipantOiProvider()
        self._build_up_analyzer = build_up_analyzer or BuildUpAnalyzer()
        self._sentiment_analyzer = sentiment_analyzer or InstitutionalSentimentAnalyzer()
        self._freshness_validator = freshness_validator or FreshnessValidator()
        self._serializer = json_serializer
        self._logger = agent_logger or logger
        self._max_previous_day_lookback = max_previous_day_lookback

    # -- Cash market (wraps Phase 2, does not duplicate it) ------------- #
    def get_cash_activity(self, activity_date: date | None = None) -> FiiDiiActivity:
        """Fetch and freshness-validate the cash-market FII/DII figures
        for activity_date (defaults to the latest published day)."""
        self._logger.info("Fetching cash-market FII/DII activity (date=%s)", activity_date)
        try:
            activity = self._service.get_fii_dii(activity_date)
        except MarketDataError:
            self._logger.error("Failed to fetch cash-market FII/DII activity", exc_info=True)
            raise

        self._freshness_validator.validate_fii_dii_date(
            activity.activity_date, "Cash-market FII/DII activity"
        )
        return activity

    def get_previous_cash_activity(self, current: FiiDiiActivity) -> FiiDiiActivity | None:
        """Walk backward from the day before `current` to find the
        previous trading day's cash-market figures, skipping
        holidays/weekends (SymbolNotFoundError -- no data published for
        that date). Returns None if nothing is found within the
        configured lookback window, rather than raising, since "no prior
        comparison available yet" is a valid, expected state (e.g. very
        early in a data set's history)."""
        for offset in range(1, self._max_previous_day_lookback + 1):
            candidate = current.activity_date - timedelta(days=offset)
            try:
                return self._service.get_fii_dii(candidate)
            except SymbolNotFoundError:
                continue
            except MarketDataError:
                self._logger.warning(
                    "Error fetching previous-day FII/DII for %s; continuing lookback.",
                    candidate,
                    exc_info=True,
                )
                continue
        self._logger.info(
            "No previous-day cash FII/DII activity found within %d day(s) before %s.",
            self._max_previous_day_lookback,
            current.activity_date,
        )
        return None

    @staticmethod
    def compare_cash_activity(
        current: FiiDiiActivity, previous: FiiDiiActivity | None
    ) -> dict[str, Any]:
        """Day-over-day comparison of net FII/DII buying."""
        if previous is None:
            return {"available": False}

        fii_change = current.fii_net_value - previous.fii_net_value
        dii_change = current.dii_net_value - previous.dii_net_value

        def _trend(change: Decimal) -> str:
            if change > 0:
                return "increasing_net_buying"
            if change < 0:
                return "decreasing_net_buying"
            return "unchanged"

        return {
            "available": True,
            "previous_activity_date": previous.activity_date.isoformat(),
            "fii_net_value_change": str(fii_change.amount),
            "fii_trend": _trend(fii_change.amount),
            "dii_net_value_change": str(dii_change.amount),
            "dii_trend": _trend(dii_change.amount),
        }

    # -- F&O participant OI (new in this file) --------------------------#
    def get_participant_snapshot(self, as_of: date | None = None) -> ParticipantOiSnapshot | None:
        """Fetch the latest available participant OI snapshot. Returns
        None (rather than raising) if none could be found within the
        provider's lookback window -- F&O participant data is a
        secondary signal and its absence should not fail the whole
        report."""
        self._logger.info("Fetching F&O participant OI snapshot (as_of=%s)", as_of)
        try:
            snapshot = self._participant_provider.fetch_snapshot(as_of)
        except SymbolNotFoundError:
            self._logger.warning("No participant OI data available for as_of=%s", as_of)
            return None
        except MarketDataError:
            self._logger.error("Failed to fetch participant OI snapshot", exc_info=True)
            return None

        self._freshness_validator.validate_fii_dii_date(
            snapshot.report_date, "Participant OI snapshot"
        )
        return snapshot

    def get_previous_participant_snapshot(
        self, current: ParticipantOiSnapshot
    ) -> ParticipantOiSnapshot | None:
        """Fetch the participant OI snapshot for the trading day before
        `current`, for build-up comparison."""
        probe_date = current.report_date - timedelta(days=1)
        try:
            return self._participant_provider.fetch_snapshot(probe_date)
        except SymbolNotFoundError:
            self._logger.info(
                "No previous participant OI snapshot found before %s.", current.report_date
            )
            return None
        except MarketDataError:
            self._logger.warning(
                "Error fetching previous participant OI snapshot before %s.",
                current.report_date,
                exc_info=True,
            )
            return None

    def analyze_build_up(
        self,
        category: ParticipantCategory,
        current: ParticipantOiSnapshot | None,
        previous: ParticipantOiSnapshot | None,
        price_change: Decimal | None,
    ) -> dict[str, Any]:
        """Classify one participant category's index-futures build-up
        pattern between `previous` and `current`."""
        if current is None or previous is None:
            return {
                "category": category.value,
                "pattern": BuildUpPattern.NEUTRAL.value,
                "reason": "insufficient data: current or previous participant OI snapshot missing",
            }

        current_record = current.get(category)
        previous_record = previous.get(category)
        if current_record is None or previous_record is None:
            return {
                "category": category.value,
                "pattern": BuildUpPattern.NEUTRAL.value,
                "reason": f"no {category.value} record in one of the two snapshots",
            }

        net_oi_change = (
            current_record.net_future_index_position - previous_record.net_future_index_position
        )
        pattern = self._build_up_analyzer.classify(net_oi_change, price_change)

        return {
            "category": category.value,
            "net_future_index_oi_change": net_oi_change,
            "price_change_used": str(price_change) if price_change is not None else None,
            "pattern": pattern.value,
        }

    # -- Top-level API (the contract ai_brain.py consumes) ---------------#
    def get_fii_dii_report_json(
        self,
        activity_date: date | None = None,
        nifty_price_change: Decimal | None = None,
    ) -> dict[str, Any]:
        """Fetch, compare, classify, validate, and serialize the full
        FII/DII + F&O participant institutional-activity report.

        `nifty_price_change` should be the NIFTY spot change for the same
        session (e.g. from market_agent's IndexAnalysisReport.spot.change)
        so build-up classification can distinguish Long Build-up from
        Short Build-up. Passing None still produces a report, with
        build-up patterns reported as NEUTRAL due to missing price data.
        """
        self._logger.info("Building FII/DII institutional activity report")

        cash_current = self.get_cash_activity(activity_date)
        cash_previous = self.get_previous_cash_activity(cash_current)
        cash_comparison = self.compare_cash_activity(cash_current, cash_previous)

        participant_current = self.get_participant_snapshot(cash_current.activity_date)
        participant_previous = (
            self.get_previous_participant_snapshot(participant_current)
            if participant_current is not None
            else None
        )

        build_ups: dict[str, dict[str, Any]] = {}
        for category in (ParticipantCategory.FII, ParticipantCategory.DII):
            build_ups[category.value] = self.analyze_build_up(
                category, participant_current, participant_previous, nifty_price_change
            )

        fii_pattern = BuildUpPattern(build_ups[ParticipantCategory.FII.value]["pattern"])
        institutional_sentiment = self._sentiment_analyzer.analyze(cash_current, fii_pattern)

        self._logger.info(
            "FII/DII report complete: FII net=%s DII net=%s sentiment=%s",
            cash_current.fii_net_value,
            cash_current.dii_net_value,
            institutional_sentiment["label"],
        )

        return self._serializer.serialize_report(
            cash_current=cash_current,
            cash_previous=cash_previous,
            cash_comparison=cash_comparison,
            participant_current=participant_current,
            participant_previous=participant_previous,
            build_ups=build_ups,
            institutional_sentiment=institutional_sentiment,
            generated_at=_utcnow(),
        )

    def get_fii_dii_report_json_string(self, indent: int | None = 2, **kwargs: Any) -> str:
        """Convenience wrapper: the same report as a JSON string."""
        return json.dumps(self.get_fii_dii_report_json(**kwargs), indent=indent)
