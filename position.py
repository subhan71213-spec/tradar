"""Put-Call Ratio calculator.

Pure domain logic: takes an OptionChainSnapshot, returns a Pcr value
object. No I/O, no framework dependencies, fully unit-testable.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot
from titan_ai_trader.domain.value_objects.pcr import Pcr


class PcrCalculator:
    """Computes OI-based Put-Call Ratio from an option chain snapshot."""

    @staticmethod
    def calculate(snapshot: OptionChainSnapshot) -> Pcr:
        total_put_oi = sum(c.open_interest for c in snapshot.puts)
        total_call_oi = sum(c.open_interest for c in snapshot.calls)

        if total_call_oi == 0:
            # Avoid a ZeroDivisionError; an undefined ratio is reported as 0
            # rather than raising, since "no call OI yet" is valid early in
            # a contract's life, not an error condition.
            ratio = Decimal("0")
        else:
            ratio = (Decimal(total_put_oi) / Decimal(total_call_oi)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )

        return Pcr(
            symbol=snapshot.symbol,
            total_put_open_interest=total_put_oi,
            total_call_open_interest=total_call_oi,
            ratio=ratio,
        )
