"""bootstrap.py — the composition root.

Reads environment variables, validates the required ones, initializes
SQLite, and wires together every agent built across Phases 1-3 into one
AppContext. main.py is deliberately thin (validate -> build -> run) so
all the "how do these objects fit together" logic lives here, in one
auditable place, rather than scattered across the entry point.

This module constructs objects; it does not run anything itself (no
network calls happen at import time, and build_app_context() only
performs local setup -- opening the SQLite connection and constructing
provider/agent instances -- not any remote calls).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from titan_ai_trader.application.services.ai_decision_engine import (
    AiDecisionEngine,
    DecisionThresholds,
)
from titan_ai_trader.application.services.fii_dii_agent import FiiDiiAgent, ParticipantOiProvider
from titan_ai_trader.application.services.market_agent import MarketAgent
from titan_ai_trader.application.services.market_data_service import MarketDataService
from titan_ai_trader.application.services.market_sentiment_agent import (
    GlobalMarketsProvider,
    MarketSentimentAgent,
)
from titan_ai_trader.application.services.news_agent import (
    EconomicCalendarProvider,
    GoogleNewsRssProvider,
    NewsAgent,
    NewsApiProvider,
)
from titan_ai_trader.application.services.strategy_agent import RiskParameters, StrategyAgent
from titan_ai_trader.application.services.telegram_formatter import TelegramFormatter, TelegramSender
from titan_ai_trader.infrastructure.config.settings import Settings, get_settings
from titan_ai_trader.infrastructure.market_data.adapters.fii_dii_adapter import FiiDiiAdapter
from titan_ai_trader.infrastructure.market_data.adapters.india_vix_adapter import IndiaVixAdapter
from titan_ai_trader.infrastructure.market_data.adapters.nse_option_chain_adapter import (
    NseOptionChainAdapter,
)
from titan_ai_trader.infrastructure.market_data.adapters.nse_spot_adapter import NseSpotAdapter
from titan_ai_trader.infrastructure.market_data.cache.in_memory_cache import InMemoryMarketDataCache
from titan_ai_trader.infrastructure.market_data.http.nse_http_client import NseHttpClient
from titan_ai_trader.infrastructure.persistence.db.connection import get_engine

logger = logging.getLogger(__name__)

# Vars the application cannot meaningfully run without, given this
# deployment's stated purpose (sending signals to a Telegram channel).
REQUIRED_ENV_VARS: tuple[str, ...] = ("BOT_TOKEN", "CHANNEL_ID")

# Vars that are read if present but have a safe fallback or are reserved
# for future use -- documented here so validate_environment() can report
# their status without ever treating them as fatal.
_OPTIONAL_ENV_NOTES: dict[str, str] = {
    "NEWS_API_KEY": "optional; falls back to the Google News RSS feed (no key required) if unset",
    "NSE_API_KEY": "reserved; the NSE endpoints this project uses are public and require no key",
    "OPENAI_API_KEY": "reserved for future AI integration; not read by any code in this build",
    "GEMINI_API_KEY": "reserved for future AI integration; not read by any code in this build",
    "CLAUDE_API_KEY": "reserved for future AI integration; not read by any code in this build",
    "TZ": "optional; the scheduler uses a fixed IST offset internally regardless of this var",
}


@dataclass(frozen=True, slots=True)
class EnvironmentValidationResult:
    missing_required: tuple[str, ...]
    present_required: tuple[str, ...]
    optional_status: dict[str, bool]

    @property
    def is_valid(self) -> bool:
        return len(self.missing_required) == 0


def validate_environment() -> EnvironmentValidationResult:
    """Checks every documented environment variable and reports its
    status. Does not raise or exit -- main.py decides what to do with
    a failing result (this function is pure, and therefore trivially
    unit-testable)."""
    missing = tuple(name for name in REQUIRED_ENV_VARS if not os.environ.get(name))
    present = tuple(name for name in REQUIRED_ENV_VARS if os.environ.get(name))
    optional_status = {name: bool(os.environ.get(name)) for name in _OPTIONAL_ENV_NOTES}
    return EnvironmentValidationResult(
        missing_required=missing, present_required=present, optional_status=optional_status
    )


def log_environment_validation(result: EnvironmentValidationResult) -> None:
    """Prints a clear, human-readable startup validation report."""
    for name in result.present_required:
        logger.info("[env] %-16s ... OK (required, present)", name)
    for name in result.missing_required:
        logger.error("[env] %-16s ... MISSING (required)", name)
    for name, is_set in result.optional_status.items():
        status = "set" if is_set else "not set"
        logger.info("[env] %-16s ... %s (%s)", name, status, _OPTIONAL_ENV_NOTES[name])

    if result.is_valid:
        logger.info("Environment validation PASSED.")
    else:
        logger.error(
            "Environment validation FAILED. Missing required variable(s): %s",
            ", ".join(result.missing_required),
        )


def _decimal_from_env(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        return Decimal(raw)
    except InvalidOperation:
        logger.warning("Invalid value for %s=%r; using default %s", name, raw, default)
        return Decimal(default)


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using default %d", name, raw, default)
        return default


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Everything build_app_context() needs, resolved from the
    environment once at startup."""

    symbols: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    bot_token: str | None = None
    channel_id: str | None = None
    news_api_key: str | None = None
    risk_parameters: RiskParameters = field(default_factory=RiskParameters)
    decision_thresholds: DecisionThresholds = field(default_factory=DecisionThresholds)
    log_level: str = "INFO"
    port: int | None = None

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.bot_token and self.channel_id)


def load_runtime_config() -> RuntimeConfig:
    symbols_raw = os.environ.get("TITAN_SYMBOLS", "NIFTY,BANKNIFTY")
    symbols = tuple(s.strip().upper() for s in symbols_raw.split(",") if s.strip())

    risk_parameters = RiskParameters(
        capital=_decimal_from_env("TITAN_CAPITAL", "100000"),
        risk_per_trade_pct=_decimal_from_env("TITAN_RISK_PER_TRADE_PCT", "1.0"),
        nifty_lot_size=_int_from_env("TITAN_NIFTY_LOT_SIZE", 75),
        banknifty_lot_size=_int_from_env("TITAN_BANKNIFTY_LOT_SIZE", 30),
    )

    port_raw = os.environ.get("PORT")
    port = int(port_raw) if port_raw and port_raw.isdigit() else None

    # NEWS_API_KEY is this deployment's documented name; NEWSAPI_KEY (no
    # underscore) is what news_agent.py's NewsApiProvider itself falls
    # back to if no key is passed explicitly. Reading both here and
    # passing the result explicitly means either name works without
    # modifying the existing, already-tested news_agent.py.
    news_api_key = os.environ.get("NEWS_API_KEY") or os.environ.get("NEWSAPI_KEY")

    return RuntimeConfig(
        symbols=symbols or ("NIFTY", "BANKNIFTY"),
        bot_token=os.environ.get("BOT_TOKEN"),
        channel_id=os.environ.get("CHANNEL_ID"),
        news_api_key=news_api_key,
        risk_parameters=risk_parameters,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        port=port,
    )


@dataclass(slots=True)
class AppContext:
    """Every constructed object main.py needs to run the service."""

    settings: Settings
    runtime_config: RuntimeConfig
    db_connection: sqlite3.Connection
    market_data_service: MarketDataService
    market_agent: MarketAgent
    fii_dii_agent: FiiDiiAgent
    news_agent: NewsAgent
    market_sentiment_agent: MarketSentimentAgent
    strategy_agent: StrategyAgent
    decision_engine: AiDecisionEngine
    telegram_formatter: TelegramFormatter
    telegram_sender: TelegramSender | None


def build_app_context(runtime_config: RuntimeConfig | None = None) -> AppContext:
    """Constructs the full agent graph. Performs no network I/O itself
    (SQLite schema initialization is local disk I/O only) -- agents make
    their first real network call only when actually invoked."""
    config = runtime_config or load_runtime_config()
    settings = get_settings()

    logger.info("Initializing SQLite at %s", settings.database_path)
    db_connection = get_engine(settings.database_path)

    http_client = NseHttpClient(timeout_seconds=settings.market_data_http_timeout_seconds)
    cache = InMemoryMarketDataCache()
    market_data_service = MarketDataService(
        spot_provider=NseSpotAdapter(http_client),
        option_chain_provider=NseOptionChainAdapter(http_client),
        vix_provider=IndiaVixAdapter(http_client),
        fii_dii_provider=FiiDiiAdapter(http_client),
        cache=cache,
    )

    market_agent = MarketAgent(market_data_service)
    fii_dii_agent = FiiDiiAgent(
        market_data_service, participant_oi_provider=ParticipantOiProvider()
    )
    news_agent = NewsAgent(
        newsapi_provider=NewsApiProvider(api_key=config.news_api_key),
        google_rss_provider=GoogleNewsRssProvider(),
        economic_calendar_provider=EconomicCalendarProvider(),
    )
    market_sentiment_agent = MarketSentimentAgent(
        market_agent,
        fii_dii_agent,
        news_agent,
        market_data_service,
        global_markets_provider=GlobalMarketsProvider(),
    )
    strategy_agent = StrategyAgent(
        market_agent,
        market_sentiment_agent,
        market_data_service,
        risk_parameters=config.risk_parameters,
    )
    decision_engine = AiDecisionEngine(
        market_agent,
        fii_dii_agent,
        news_agent,
        market_sentiment_agent,
        strategy_agent,
        market_data_service,
        thresholds=config.decision_thresholds,
    )

    telegram_sender: TelegramSender | None = None
    if config.telegram_enabled:
        telegram_sender = TelegramSender(bot_token=config.bot_token, chat_id=config.channel_id)
    else:
        logger.warning(
            "Telegram not configured (BOT_TOKEN/CHANNEL_ID missing) -- "
            "decisions will be logged only, not sent."
        )

    return AppContext(
        settings=settings,
        runtime_config=config,
        db_connection=db_connection,
        market_data_service=market_data_service,
        market_agent=market_agent,
        fii_dii_agent=fii_dii_agent,
        news_agent=news_agent,
        market_sentiment_agent=market_sentiment_agent,
        strategy_agent=strategy_agent,
        decision_engine=decision_engine,
        telegram_formatter=TelegramFormatter(),
        telegram_sender=telegram_sender,
    )
