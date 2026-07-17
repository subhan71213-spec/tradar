"""Journal entity.

A JournalEntry is a free-form, timestamped note a trader attaches to the
overall trading log. It may optionally reference a specific Trade (e.g.
"why I took this setup", "what I'd do differently") or stand alone as a
general market/session observation.

This is distinct from Trade.notes, which is a running log scoped to a
single trade. The Journal is the book-level diary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from titan_ai_trader.domain.exceptions.domain_exceptions import InvalidTradeError


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class JournalEntry:
    """A single journal note, optionally linked to a Trade."""

    content: str
    trade_id: str | None = None
    tags: list[str] = field(default_factory=list)

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.content or not self.content.strip():
            raise InvalidTradeError("Journal entry content must not be empty.")

    def edit(self, new_content: str) -> None:
        if not new_content or not new_content.strip():
            raise InvalidTradeError("Journal entry content must not be empty.")
        self.content = new_content
        self.updated_at = _utcnow()

    def add_tag(self, tag: str) -> None:
        if tag and tag not in self.tags:
            self.tags.append(tag)
            self.updated_at = _utcnow()
