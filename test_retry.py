"""Immutable Money value object.

Uses Decimal internally to avoid floating point drift in P&L math.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def _to_decimal(value: "Decimal | float | int | str") -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Money:
    """Represents a currency amount, quantized to 2 decimal places."""

    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        quantized = _to_decimal(self.amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        object.__setattr__(self, "amount", quantized)

    @classmethod
    def of(cls, value: "Decimal | float | int | str", currency: str = "USD") -> "Money":
        return cls(_to_decimal(value), currency)

    def _check_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot operate on Money with different currencies: "
                f"{self.currency} vs {other.currency}"
            )

    def __add__(self, other: "Money") -> "Money":
        self._check_currency(other)
        return Money.of(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check_currency(other)
        return Money.of(self.amount - other.amount, self.currency)

    def __mul__(self, factor: "Decimal | float | int") -> "Money":
        return Money.of(self.amount * _to_decimal(factor), self.currency)

    def __neg__(self) -> "Money":
        return Money.of(-self.amount, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._check_currency(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._check_currency(other)
        return self.amount <= other.amount

    def is_negative(self) -> bool:
        return self.amount < 0

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
