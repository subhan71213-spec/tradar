"""Domain-level exceptions. Framework-free."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain errors."""


class InvalidTradeError(DomainError):
    """Raised when a Trade is constructed or mutated into an invalid state."""


class InvalidTargetLevelsError(DomainError):
    """Raised when target levels are inconsistent with trade direction."""


class TradeAlreadyClosedError(DomainError):
    """Raised when attempting to mutate a trade that is already terminal."""


class InsufficientFundsError(DomainError):
    """Raised when a portfolio does not have enough cash to open a position."""


class PositionNotFoundError(DomainError):
    """Raised when an operation references a position that does not exist."""
