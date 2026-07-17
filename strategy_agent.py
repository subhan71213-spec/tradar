"""NSE Option Chain adapter — implements OptionChainProvider.

Hits /api/option-chain-indices (for index underlyings like NIFTY,
BANKNIFTY) and parses NSE's nested records payload into an
OptionChainSnapshot of flat OptionContract rows.
"""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from titan_ai_trader.application.interfaces.option_chain_provider import OptionChainProvider
from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.entities.option_contract import OptionContract
from titan_ai_trader.domain.enums.option_type import OptionType
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
    SymbolNotFoundError,
)
from titan_ai_trader.domain.value_objects.money import Money
from titan_ai_trader.infrastructure.market_data.http.nse_http_client import NseHttpClient
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

_NSE_EXPIRY_FORMAT = "%d-%b-%Y"


class NseOptionChainAdapter(OptionChainProvider):
    def __init__(self, http_client: NseHttpClient | None = None) -> None:
        self._client = http_client or NseHttpClient()

    @retry_on_network_failure(max_attempts=3)
    def get_option_chain(
        self, symbol: str, expiry_date: Date | None = None
    ) -> OptionChainSnapshot:
        try:
            payload = self._client.get_json(
                "/api/option-chain-indices", params={"symbol": symbol}
            )
        except json.JSONDecodeError as exc:
            raise MarketDataValidationError(
                f"NSE option chain response for {symbol} was not valid JSON."
            ) from exc

        records = payload.get("records")
        if not records:
            raise SymbolNotFoundError(f"No option chain records for symbol '{symbol}'.")

        underlying_value = records.get("underlyingValue")
        if underlying_value is None:
            raise MarketDataValidationError(
                f"NSE option chain payload for {symbol} missing underlyingValue."
            )

        target_expiry = expiry_date or self._resolve_nearest_expiry(records)
        expiry_str = target_expiry.strftime(_NSE_EXPIRY_FORMAT)

        contracts = self._parse_contracts(records.get("data", []), expiry_str, target_expiry)
        if not contracts:
            raise MarketDataValidationError(
                f"No option contracts found for {symbol} expiry {target_expiry}."
            )

        return OptionChainSnapshot(
            symbol=symbol,
            expiry_date=target_expiry,
            underlying_value=Money.of(str(underlying_value)),
            contracts=tuple(contracts),
        )

    def _resolve_nearest_expiry(self, records: dict) -> Date:
        expiry_dates = records.get("expiryDates", [])
        if not expiry_dates:
            raise MarketDataValidationError("NSE option chain payload has no expiryDates.")
        nearest = expiry_dates[0]
        return datetime.strptime(nearest, _NSE_EXPIRY_FORMAT).date()

    def _parse_contracts(
        self, rows: list[dict], expiry_str: str, expiry_date: Date
    ) -> list[OptionContract]:
        contracts: list[OptionContract] = []
        for row in rows:
            if row.get("expiryDate") != expiry_str:
                continue
            strike = row.get("strikePrice")
            if strike is None:
                continue
            for option_type, key in ((OptionType.CE, "CE"), (OptionType.PE, "PE")):
                leg = row.get(key)
                if leg is None:
                    continue
                try:
                    contracts.append(
                        OptionContract(
                            strike_price=Decimal(str(strike)),
                            option_type=option_type,
                            expiry_date=expiry_date,
                            open_interest=int(leg.get("openInterest", 0)),
                            change_in_open_interest=int(leg.get("changeinOpenInterest", 0)),
                            volume=int(leg.get("totalTradedVolume", 0)),
                            implied_volatility=(
                                Decimal(str(leg["impliedVolatility"]))
                                if leg.get("impliedVolatility") not in (None, 0)
                                else None
                            ),
                            last_price=Money.of(str(leg.get("lastPrice", 0))),
                            bid_price=(
                                Money.of(str(leg["bidprice"]))
                                if leg.get("bidprice")
                                else None
                            ),
                            ask_price=(
                                Money.of(str(leg["askPrice"]))
                                if leg.get("askPrice")
                                else None
                            ),
                        )
                    )
                except (InvalidOperation, ValueError, TypeError):
                    # Skip a single malformed leg rather than failing the
                    # whole chain -- one bad row upstream shouldn't take
                    # down every other strike.
                    continue
        return contracts
