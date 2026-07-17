"""scheduler.py — the trading-day scheduler.

Drives *when* the rest of the system runs (e.g. calling
AiDecisionEngine.get_decision_json and pushing the result via
TelegramFormatter/TelegramSender). This module contains no analysis or
trading logic of its own -- it only knows how to compute "when is the
next run" and invoke an injected async callback at that time.

Schedule (IST, India market hours):
    - 09:00  -- pre-open
    - 09:08  -- pre-open close / opening auction settling
    - 09:15  -- market open
    - every 30 minutes from 09:15 to market close
    - market close (default 15:30)

Weekends are always skipped. A `holiday_dates` set can be supplied to
skip known market holidays too (NSE's holiday calendar changes yearly
and isn't fetched automatically by this module -- pass in the dates you
want skipped for the year you're running).

Designed for testability: `now_fn` and `sleep_fn` are injectable, so the
schedule-computation logic can be exercised with a virtual clock in unit
tests, without ever actually sleeping in real time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

SchedulerCallback = Callable[[str], Awaitable[None]]


def _default_now() -> datetime:
    return datetime.now(IST)


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    fixed_times: tuple[time, ...] = (time(9, 0), time(9, 8), time(9, 15))
    interval_minutes: int = 30
    interval_start: time = time(9, 15)
    interval_end: time = time(15, 30)
    market_close: time = time(15, 30)
    holiday_dates: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive.")
        if self.interval_start >= self.interval_end:
            raise ValueError("interval_start must be before interval_end.")


class TradingScheduler:
    """Computes the next scheduled run time and invokes `callback(label)`
    at each one, forever (until `stop()` is called)."""

    def __init__(
        self,
        callback: SchedulerCallback,
        config: ScheduleConfig | None = None,
        now_fn: Callable[[], datetime] = _default_now,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        scheduler_logger: logging.Logger | None = None,
    ) -> None:
        self._callback = callback
        self._config = config or ScheduleConfig()
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._logger = scheduler_logger or logger
        self._stop_event = asyncio.Event()

    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        if day in self._config.holiday_dates:
            return False
        return True

    def _trigger_points_for_day(self, day: date) -> list[tuple[datetime, str]]:
        """All (datetime, label) trigger points for a single calendar day,
        sorted chronologically, deduplicated by exact timestamp."""
        points: dict[datetime, str] = {}

        for t in self._config.fixed_times:
            dt = datetime.combine(day, t, tzinfo=IST)
            points.setdefault(dt, f"fixed:{t.strftime('%H:%M')}")

        cursor = datetime.combine(day, self._config.interval_start, tzinfo=IST)
        end = datetime.combine(day, self._config.interval_end, tzinfo=IST)
        step = timedelta(minutes=self._config.interval_minutes)
        while cursor <= end:
            points.setdefault(cursor, f"interval:{cursor.strftime('%H:%M')}")
            cursor += step

        close_dt = datetime.combine(day, self._config.market_close, tzinfo=IST)
        points.setdefault(close_dt, "market_close")

        return sorted(points.items(), key=lambda item: item[0])

    def next_trigger(self, after: datetime, max_days_ahead: int = 14) -> tuple[datetime, str]:
        """The next (datetime, label) strictly after `after`, skipping
        non-trading days. Raises RuntimeError if none is found within
        max_days_ahead (a sanity bound against a misconfigured holiday
        calendar accidentally blocking every day)."""
        candidate_day = after.date()
        for _ in range(max_days_ahead + 1):
            if self.is_trading_day(candidate_day):
                for trigger_dt, label in self._trigger_points_for_day(candidate_day):
                    if trigger_dt > after:
                        return trigger_dt, label
            candidate_day = candidate_day + timedelta(days=1)

        raise RuntimeError(
            f"No trading-day trigger found within {max_days_ahead} day(s) after {after.isoformat()}. "
            "Check ScheduleConfig.holiday_dates for a misconfiguration."
        )

    def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Runs until `stop()` is called. Each iteration: compute the next
        trigger, wait until it via the injected sleep_fn, invoke the
        callback, repeat. A callback exception is logged and does NOT
        stop the scheduler -- one failed run should not take down all
        future scheduled runs.

        The wait is a race between `sleep_fn(wait_seconds)` and the stop
        event, so `stop()` can interrupt a wait in progress. Using
        `sleep_fn` here (rather than asyncio.wait_for's own real-time
        timeout) is what makes this method actually testable with a
        virtual clock -- a wait_for timeout would use real wall-clock
        time regardless of what sleep_fn is, defeating the injection.
        """
        self._stop_event.clear()
        while not self._stop_event.is_set():
            now = self._now_fn()
            trigger_dt, label = self.next_trigger(now)
            wait_seconds = max(0.0, (trigger_dt - now).total_seconds())
            self._logger.info(
                "Next scheduled run: %s (%s) in %.0fs", trigger_dt.isoformat(), label, wait_seconds
            )

            sleep_task = asyncio.ensure_future(self._sleep_fn(wait_seconds))
            stop_task = asyncio.ensure_future(self._stop_event.wait())
            done, pending = await asyncio.wait(
                {sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stop_task in done:
                break

            if self._stop_event.is_set():
                break

            self._logger.info("Triggering scheduled run: %s", label)
            try:
                await self._callback(label)
            except Exception:
                self._logger.error("Scheduled callback failed for trigger %s", label, exc_info=True)

    async def run_n_triggers(self, count: int) -> list[str]:
        """Runs exactly `count` triggers then stops -- primarily for
        tests and manual smoke-runs, not for production use (which
        should use run_forever)."""
        fired: list[str] = []
        original_callback = self._callback

        async def _wrapped(label: str) -> None:
            fired.append(label)
            await original_callback(label)
            if len(fired) >= count:
                self.stop()

        self._callback = _wrapped
        try:
            await self.run_forever()
        finally:
            self._callback = original_callback
        return fired
