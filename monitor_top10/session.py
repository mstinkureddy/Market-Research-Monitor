"""User-controlled, deterministic event-loop state machine for the prototype."""
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from .evidence import ZERO, OfficialEvidence, SessionHours, FixtureFeed
from .limits import MAX_SESSION_MINUTES, valid_symbols
from .market import Asset, Bar, BarBook, SIGNAL_TTL, Signal, Universe, detect
from .validation import PlanningRisk, Result, Validator


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class AuditEvent:
    at: datetime
    event: str
    detail: str


@dataclass
class Job:
    signal: Signal
    ready: datetime
    invalidated: bool = False


class Monitor:
    """Feed serialized events into tick/ingest. No scheduler, threads, or real adapters."""
    def __init__(self, clock: Clock, assets: tuple[Asset, ...], feed: FixtureFeed,
                 official: OfficialEvidence,
                 *, validation_latency: timedelta = timedelta(seconds=2),
                 portfolio: PlanningRisk | None = None) -> None:
        if not timedelta(0) <= validation_latency < SIGNAL_TTL:
            raise ValueError("validation latency outside the signal lifetime")
        if type(feed) is not FixtureFeed or type(official) is not OfficialEvidence:
            raise TypeError("only the bundled synthetic adapters are supported")
        self.clock, self.feed = clock, feed
        self.validator = Validator(feed, official)
        self.assets = assets
        self.universe, self.book = Universe(assets), BarBook()
        self.portfolio = portfolio or PlanningRisk()
        self.results: list[Result] = []
        self.audit: list[AuditEvent] = []
        self.pending: deque[Signal] = deque()
        self.in_flight: dict[str, Job] = {}
        self.peak_in_flight = 0
        self._latency = validation_latency
        self._seen: set[tuple] = set()
        self._hours: SessionHours | None = None
        self._deadline: datetime | None = None
        self._shutdown_reason = "operator"
        self._venue: str | None = None
        self._symbols: tuple[str, ...] = ()
        self._reserved = ZERO

    @property
    def active(self) -> bool:
        if self._deadline is not None and self.clock.now() >= self._deadline:
            self.off(self._shutdown_reason)
        return self._deadline is not None

    @property
    def deadline(self) -> datetime | None:
        return self._deadline if self.active else None

    def _record(self, event: str, detail: str = "") -> None:
        self.audit.append(AuditEvent(self.clock.now(), event, detail))

    def on(self, hours: SessionHours, cutoff: datetime, *, symbols: tuple[str, ...], authorized: bool = False,
           venue: str | None = None, lease: timedelta = timedelta(minutes=MAX_SESSION_MINUTES)) -> bool:
        """Open a local fixture session. Hosted authorization is verified by the batch."""
        now = self.clock.now()
        if (self.active or authorized is not True or not valid_symbols(symbols)
                or any(symbol not in self.universe.assets for symbol in symbols)
                or cutoff.utcoffset() is None or cutoff.date() != hours.opens.date()
                or not timedelta(0) < lease <= timedelta(minutes=MAX_SESSION_MINUTES)):
            return False
        deadline, reason = min(((hours.closes, "market close"), (cutoff, "cutoff"),
                                (now + lease, "lease expired")), key=lambda item: item[0])
        if not hours.opens <= now < deadline:
            return False
        self.feed.begin(True)
        self._hours, self._deadline, self._shutdown_reason = hours, deadline, reason
        self._venue = venue
        self._symbols = symbols
        self._seen.clear()
        self._reserved = ZERO
        self.results.clear()
        self.book = BarBook()
        self.universe = Universe(tuple(replace(asset, optionable=asset.optionable and asset.symbol in symbols)
                                       for asset in self.assets))
        self.peak_in_flight = 0
        self._record("on", "synthetic intraday session")
        self.tick()
        return True

    def off(self, reason: str = "operator") -> None:
        if self._deadline is None:
            return
        pending = len(self.pending) + len(self.in_flight)
        self._deadline = None
        self.pending.clear()
        self.in_flight.clear()
        self.feed.end()
        self._record("off", f"{reason}; canceled {pending}")

    def ingest(self, bar: Bar) -> bool:
        if not self.active or bar.symbol not in self._symbols:
            return False
        if not self.book.ingest(bar, self.clock.now(), self._hours.opens):
            self._record("bar ignored", bar.symbol)
            return False
        job = self.in_flight.get(bar.symbol)
        if job:
            extreme = bar.low if job.signal.side == "C" else bar.high
            job.invalidated |= job.signal.invalidated(extreme)
        self.tick()
        return True

    def _shortlist(self, now: datetime) -> None:
        if self.universe.refresh(now, self.book):
            self._record("rank", f"{self.universe.screened} screened; {len(self.universe.watching)} monitored")
        for symbol in self.universe.watching:
            if symbol not in self.universe.scores or symbol in self.in_flight:
                continue
            signal = detect(self.book.series.get(symbol, []))
            if signal and now - signal.confirmed_at < SIGNAL_TTL and signal.key not in self._seen:
                self._seen.add(signal.key)
                self.pending.append(signal)
                self._record("signal", symbol)

    def _finish(self, job: Job, now: datetime) -> None:
        signal = job.signal
        result = self.validator.evaluate(signal, now, self._deadline, self._hours, self._venue,
                                         self.portfolio.budget(self._reserved), job.invalidated)
        self.results.append(result)
        self._record("validation complete", f"{signal.symbol}: {result.decision}")

        if result.modeled_quantity and result.economics:
            self._reserved += result.modeled_quantity * result.economics.loss

    def tick(self) -> None:
        if not self.active:
            return
        now = self.clock.now()
        self._shortlist(now)
        done = [symbol for symbol, job in self.in_flight.items() if job.ready <= now]
        for symbol in done:
            job = self.in_flight.pop(symbol)
            if now - job.signal.confirmed_at < SIGNAL_TTL:
                self._finish(job, now)
            else:
                self._record("signal expired", symbol)
        symbols = set(self.universe.watching)
        while self.pending and len(self.in_flight) < 2:
            signal = self.pending.popleft()
            if signal.symbol not in symbols or signal.symbol in self.in_flight:
                continue
            later = [bar for bar in self.book.series[signal.symbol] if bar.end > signal.confirmed_at]
            invalidated = any(signal.invalidated(bar.low if signal.side == "C" else bar.high) for bar in later)
            if invalidated or now - signal.confirmed_at >= SIGNAL_TTL:
                self._record("signal dropped", signal.symbol)
                continue
            self.in_flight[signal.symbol] = Job(signal, now + self._latency)
            self.peak_in_flight = max(self.peak_in_flight, len(self.in_flight))
            self._record("validation queued", signal.symbol)
