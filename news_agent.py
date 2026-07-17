"""NSE Spot adapter — implements NseSpotProvider against nseindia.com.

Handles both equities (/api/quote-equity) and indices (/api/allIndices),
since NSE exposes them via different endpoints. `is_index` selects which
endpoint to hit -- pass True for symbols like 'NIFTY 50', 'NIFTY BANK'.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from titan_ai_trader.application.interfaces.nse_spot_provider import NseSpotProvider
from titan_ai_trader.domain.entities.market_quote import MarketQuote
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
    SymbolNotFoundError,
)
from titan_ai_trader.domain.value_objects.money import Money
from titan_ai_trader.infrastructure.market_data.http.nse_http_client import NseHttpClient
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure


class NseSpotAdapter(NseSpotProvider):
    def __init__(self, http_client: NseHttpClient | None = None) -> None:
        self._client = http_client or NseHttpClient()

    @retry_on_network_failure(max_attempts=3)
    def get_spot(self, symbol: str, is_index: bool = False) -> MarketQuote:
        try:
            if is_index:
                payload = self._client.get_json("/api/allIndices")
                return self._parse_index_quote(symbol, payload)
            payload = self._client.get_json(
                "/api/quote-equity", params={"symbol": symbol}
            )
            return self._parse_equity_quote(symbol, payload)
        except json.JSONDecodeError as exc:
            raise MarketDataValidationError(
                f"NSE spot response for {symbol} was not valid JSON."
            ) from exc

    def _parse_equity_quote(self, symbol: str, payload: dict) -> MarketQuote:
        try:
            price_info = payload["priceInfo"]
            last_price = Decimal(str(price_info["lastPrice"]))
            change = Decimal(str(price_info["change"]))
            change_percent = Decimal(str(price_info["pChange"]))
            open_price = price_info.get("open")
            day_high = price_info.get("intraDayHighLow", {}).get("max")
            day_low = price_info.get("intraDayHighLow", {}).get("min")
            prev_close = price_info.get("previousClose")
        except (KeyError, InvalidOperation) as exc:
            raise SymbolNotFoundError(f"Unexpected/missing NSE quote data for {symbol}.") from exc

        return MarketQuote(
            symbol=symbol,
            last_price=Money.of(last_price),
            change=change,
            change_percent=change_percent,
            open_price=Money.of(open_price) if open_price is not None else None,
            high_price=Money.of(day_high) if day_high is not None else None,
            low_price=Money.of(day_low) if day_low is not None else None,
            previous_close=Money.of(prev_close) if prev_close is not None else None,
        )

    def _parse_index_quote(self, symbol: str, payload: dict) -> MarketQuote:
        indices = payload.get("data", [])
        match = next((row for row in indices if row.get("index") == symbol), None)
        if match is None:
            raise SymbolNotFoundError(f"Index '{symbol}' not found in NSE allIndices response.")

        try:
            last_price = Decimal(str(match["last"]))
            change = Decimal(str(match["variation"]))
            change_percent = Decimal(str(match["percentChange"]))
        except (KeyError, InvalidOperation) as exc:
            raise SymbolNotFoundError(f"Unexpected/missing NSE index data for {symbol}.") from exc

        return MarketQuote(
            symbol=symbol,
            last_price=Money.of(last_price),
            change=change,
            change_percent=change_percent,
        )
