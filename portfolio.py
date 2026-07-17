"""Max Pain calculator.

Max Pain is the strike at which option WRITERS (sellers) as a whole lose
the least money if the underlying settles there at expiry -- equivalently
where option HOLDERS collectively lose the most. For each candidate
strike S, total writer loss is:

    sum over calls with strike K <= S of (S - K) * call_OI(K)
  + sum over puts  with strike K >= S of (K - S) * put_OI(K)

The strike minimizing this sum is the max pain point.

Pure domain logic: no I/O, no framework dependencies.
"""

from __future__ import annotations

from decimal import Decimal

from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.value_objects.max_pain import MaxPain


class MaxPainCalculator:
    """Computes the Max Pain strike from an option chain snapshot."""

    @staticmethod
    def calculate(snapshot: OptionChainSnapshot) -> MaxPain:
        strikes = snapshot.strikes
        calls = snapshot.calls
        puts = snapshot.puts

        pain_by_strike: dict[Decimal, Decimal] = {}
        for candidate in strikes:
            call_loss = sum(
                (candidate - c.strike_price) * c.open_interest
                for c in calls
                if c.strike_price <= candidate
            )
            put_loss = sum(
                (c.strike_price - candidate) * c.open_interest
                for c in puts
                if c.strike_price >= candidate
            )
            pain_by_strike[candidate] = Decimal(call_loss) + Decimal(put_loss)

        max_pain_strike = min(pain_by_strike, key=lambda s: pain_by_strike[s])

        return MaxPain(
            symbol=snapshot.symbol,
            expiry_date=snapshot.expiry_date,
            max_pain_strike=max_pain_strike,
            pain_by_strike=pain_by_strike,
        )
