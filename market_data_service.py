"""FII/DII adapter — implements FiiDiiProvider.

Uses NSE's fiidiiTradeReact endpoint, which returns a small list of rows
(one for FII/FPI, one for DII) with gross buy/sell values in crore INR
for the most recently published trading session. NSE only republishes
one day at a time via this endpoint, so `get_by_date` filters the same
payload rather than hitting a separate historical endpoint.
"""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from titan_ai_trader.application.interfaces.fii_dii_provider import FiiDiiProvider
from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataUnavailableError,
    MarketDataValidationError,
    SymbolNotFoundError,
)
from titan_ai_trader.domain.value_objects.money import Money
from titan_ai_trader.infrastructure.market_data.http.nse_http_client import NseHttpClient
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

_NSE_DATE_FORMAT = "%d-%b-%Y"


class FiiDiiAdapter(FiiDiiProvider):
    def __init__(self, http_client: NseHttpClient | None = None) -> None:
        self._client = http_client or NseHttpClient()

    @retry_on_network_failure(max_attempts=3)
    def get_latest(self) -> FiiDiiActivity:
        rows = self._fetch_rows()
        if not rows:
            raise MarketDataUnavailableError("FII/DII response contained no rows.")
        activity_date = self._parse_date(rows[0])
        return self._build_activity(rows, activity_date)

    @retry_on_network_failure(max_attempts=3)
    def get_by_date(self, activity_date: Date) -> FiiDiiActivity:
        rows = self._fetch_rows()
        matching = [r for r in rows if self._parse_date(r) == activity_date]
        if not matching:
            raise SymbolNotFoundError(
                f"No FII/DII figures published for {activity_date.isoformat()}."
            )
        return self._build_activity(matching, activity_date)

    def _fetch_rows(self) -> list[dict]:
        try:
            payload = self._client.get_json("/api/fiidiiTradeReact")
        except json.JSONDecodeError as exc:
            raise MarketDataValidationError("NSE FII/DII response was not valid JSON.") from exc
        if not isinstance(payload, list):
            raise MarketDataValidationError("NSE FII/DII response had an unexpected shape.")
        return payload

    def _parse_date(self, row: dict) -> Date:
        try:
            return datetime.strptime(row["date"], _NSE_DATE_FORMAT).date()
        except (KeyError, ValueError) as exc:
            raise MarketDataValidationError(f"Unparseable FII/DII date in row: {row}") from exc

    def _build_activity(self, rows: list[dict], activity_date: Date) -> FiiDiiActivity:
        fii_row = next((r for r in rows if "FII" in r.get("category", "").upper()), None)
        dii_row = next((r for r in rows if "DII" in r.get("category", "").upper()), None)
        if fii_row is None or dii_row is None:
            raise MarketDataValidationError(
                "NSE FII/DII response missing an FII or DII row."
            )

        try:
            return FiiDiiActivity(
                activity_date=activity_date,
                fii_buy_value=Money.of(str(fii_row["buyValue"])),
                fii_sell_value=Money.of(str(fii_row["sellValue"])),
                dii_buy_value=Money.of(str(dii_row["buyValue"])),
                dii_sell_value=Money.of(str(dii_row["sellValue"])),
            )
        except (KeyError, InvalidOperation) as exc:
            raise MarketDataValidationError("Unexpected/missing FII/DII value fields.") from exc
