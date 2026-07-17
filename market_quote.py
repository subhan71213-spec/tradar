"""OI Change analyzer.

Summarizes open-interest change across an option chain snapshot.

NSE's option chain feed already reports change_in_open_interest per
contract (change since previous session close), so the primary path
just aggregates that field. `compare_snapshots` is offered for the case
where a caller wants the OI delta between two snapshots taken at
arbitrary points in time (e.g. two intraday cache reads) instead of the
exchange-reported since-close figure.
"""

from __future__ import annotations

from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)
from titan_ai_trader.domain.value_objects.oi_change_summary import (
    OiChangeSummary,
    StrikeOiChange,
)


class OiChangeAnalyzer:
    """Aggregates OI change data from an option chain snapshot."""

    @staticmethod
    def summarize(snapshot: OptionChainSnapshot) -> OiChangeSummary:
        """Aggregate the exchange-reported change_in_open_interest field."""
        call_by_strike = {c.strike_price: c.change_in_open_interest for c in snapshot.calls}
        put_by_strike = {c.strike_price: c.change_in_open_interest for c in snapshot.puts}

        by_strike = tuple(
            StrikeOiChange(
                strike_price=strike,
                call_oi_change=call_by_strike.get(strike, 0),
                put_oi_change=put_by_strike.get(strike, 0),
            )
            for strike in snapshot.strikes
        )

        return OiChangeSummary(
            symbol=snapshot.symbol,
            total_call_oi_change=sum(s.call_oi_change for s in by_strike),
            total_put_oi_change=sum(s.put_oi_change for s in by_strike),
            by_strike=by_strike,
        )

    @staticmethod
    def compare_snapshots(
        previous: OptionChainSnapshot, current: OptionChainSnapshot
    ) -> OiChangeSummary:
        """Compute OI change between two snapshots of the same symbol/expiry."""
        if previous.symbol != current.symbol or previous.expiry_date != current.expiry_date:
            raise MarketDataValidationError(
                "Cannot compare OI change across different symbols/expiries."
            )

        def _oi_map(snap: OptionChainSnapshot, option_type_contracts) -> dict:
            return {c.strike_price: c.open_interest for c in option_type_contracts}

        prev_calls = _oi_map(previous, previous.calls)
        curr_calls = _oi_map(current, current.calls)
        prev_puts = _oi_map(previous, previous.puts)
        curr_puts = _oi_map(current, current.puts)

        strikes = current.strikes
        by_strike = tuple(
            StrikeOiChange(
                strike_price=strike,
                call_oi_change=curr_calls.get(strike, 0) - prev_calls.get(strike, 0),
                put_oi_change=curr_puts.get(strike, 0) - prev_puts.get(strike, 0),
            )
            for strike in strikes
        )

        return OiChangeSummary(
            symbol=current.symbol,
            total_call_oi_change=sum(s.call_oi_change for s in by_strike),
            total_put_oi_change=sum(s.put_oi_change for s in by_strike),
            by_strike=by_strike,
        )
