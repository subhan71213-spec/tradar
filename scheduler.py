"""India VIX adapter — implements IndiaVixProvider.

NSE reports India VIX as one row within the /api/allIndices payload
(index name "INDIA VIX"), the same endpoint used for index spot quotes.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from titan_ai_trader.application.interfaces.india_vix_provider import IndiaVixProvider
from titan_ai_trader.domain.entities.india_vix import IndiaVix
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataUnavailableError,
    MarketDataValidationError,
)
from titan_ai_trader.infrastructure.market_data.http.nse_http_client import NseHttpClient
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

_VIX_INDEX_NAME = "INDIA VIX"


class IndiaVixAdapter(IndiaVixProvider):
    def __init__(self, http_client: NseHttpClient | None = None) -> None:
        self._client = http_client or NseHttpClient()

    @retry_on_network_failure(max_attempts=3)
    def get_vix(self) -> IndiaVix:
        try:
            payload = self._client.get_json("/api/allIndices")
        except json.JSONDecodeError as exc:
            raise MarketDataValidationError("NSE VIX response was not valid JSON.") from exc

        rows = payload.get("data", [])
        match = next((row for row in rows if row.get("index") == _VIX_INDEX_NAME), None)
        if match is None:
            raise MarketDataUnavailableError("India VIX row missing from NSE allIndices response.")

        try:
            value = Decimal(str(match["last"]))
            change = Decimal(str(match["variation"]))
            change_percent = Decimal(str(match["percentChange"]))
        except (KeyError, InvalidOperation) as exc:
            raise MarketDataValidationError("Unexpected/missing India VIX fields.") from exc

        return IndiaVix(value=value, change=change, change_percent=change_percent)
