"""Typed synthetic market and official evidence. This module has no I/O clients."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import re

from .market import Side

D = Decimal
ZERO = D(0)
STANDARD = D(50)
HARD = D(100)
MIN_RR = D("1.5")


@dataclass(frozen=True)
class SessionHours:
    opens: datetime
    closes: datetime

    def __post_init__(self) -> None:
        if (self.opens.utcoffset() is None or self.closes.utcoffset() is None
                or self.opens >= self.closes or self.opens.date() != self.closes.date()):
            raise ValueError("regular session must have ordered, aware, same-day hours")


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: int
    ask_size: int
    bid_at: datetime | None
    ask_at: datetime | None
    delayed: bool = False

    def issue(self, symbol: str, now: datetime, hours: SessionHours) -> str | None:
        if self.symbol != symbol:
            return "quote identity mismatch"
        if any(x is None or not x.is_finite() or x <= 0 for x in (self.bid, self.ask)):
            return "missing or invalid quote side"
        if self.bid > self.ask:
            return "crossed quote"
        if self.bid_size <= 0 or self.ask_size <= 0:
            return "missing positive displayed size"
        if self.delayed:
            return "delayed quote"
        for when in (self.bid_at, self.ask_at):
            if when is None or when.utcoffset() is None:
                return "missing quote-side timestamp"
            if not hours.opens <= when < hours.closes or when > now:
                return "quote-side time outside the current session or in the future"
            if (now - when).total_seconds() > 15:
                return "stale quote side"
        return None


@dataclass(frozen=True)
class Contract:
    osi: str
    underlying: str
    side: Side
    strike: Decimal
    expiration: datetime
    last_trade: datetime
    screening_spread: Decimal
    screening_depth: int

    def is_exact(self) -> bool:
        if self.expiration.utcoffset() is None or self.last_trade.utcoffset() is None:
            return False
        parts = re.fullmatch(r"([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})", self.osi.replace(" ", ""))
        return bool(parts and parts[1] == self.underlying
                    and parts[2] == self.expiration.strftime("%y%m%d") and parts[3] == self.side
                    and D(parts[4]) / 1000 == self.strike > 0 and self.last_trade <= self.expiration)


@dataclass(frozen=True)
class Greeks:
    osi: str
    observed: datetime
    iv: Decimal
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal

    def usable(self, contract: Contract, now: datetime, hours: SessionHours) -> bool:
        if not all(v.is_finite() for v in (self.iv, self.delta, self.gamma, self.theta, self.vega)):
            return False
        return (self.osi == contract.osi and self.observed.utcoffset() is not None
                and hours.opens <= self.observed <= now < hours.closes
                and (now - self.observed).total_seconds() <= 60
                and self.iv > 0 and 0 <= self.gamma and self.theta <= 0 and self.vega >= 0
                and (0 < self.delta <= 1 if contract.side == "C" else -1 <= self.delta < 0))


@dataclass(frozen=True)
class FixtureEvidence:
    chain: tuple[Contract, ...]
    underlying: Quote | None
    quotes: dict[str, Quote]
    greeks: dict[str, Greeks]
    entitlement: bool = True
    recovered_greeks: dict[str, Greeks] = field(default_factory=dict)
    supplemental_context: bool = True


@dataclass(frozen=True)
class Terms:
    osi: str
    multiplier: Decimal
    components: tuple[tuple[str, Decimal], ...]
    adjusted: bool | None
    source: str
    effective: datetime
    retrieved: datetime
    style: str = "American"
    settlement: str = "physical"
    memo: str | None = None

    def supported(self, contract: Contract, now: datetime) -> bool:
        # This estimator only models a verified standard single-share-component payoff.
        return (self.osi == contract.osi and self.source.startswith("fixture:occ/")
                and self.effective <= self.retrieved <= now and self.adjusted is False
                and self.multiplier.is_finite() and self.multiplier > 0 and self.components == ((contract.underlying, self.multiplier),)
                and self.style == "American" and self.settlement == "physical")


@dataclass(frozen=True)
class Fees:
    venue: str
    open_fee: Decimal
    close_fee: Decimal
    tail_fee: Decimal
    source: str
    effective: datetime
    retrieved: datetime
    through: datetime
    complete: bool = True
    conflict: bool = False
    zero_documented: bool = False

    def supported(self, venue: str | None, now: datetime, deadline: datetime) -> bool:
        amounts = (self.open_fee, self.close_fee, self.tail_fee)
        return (bool(venue) and venue.casefold() == self.venue.casefold()
                and self.source.startswith("fixture:manual-fee/")
                and self.effective <= self.retrieved <= now <= deadline <= self.through
                and self.complete and not self.conflict
                and all(value.is_finite() and value >= 0 for value in amounts)
                and (any(amounts) or self.zero_documented))


@dataclass(frozen=True)
class Calendar:
    symbol: str
    starts: datetime
    through: datetime
    retrieved: datetime
    source: str
    unresolved_material: bool = False

    def supports(self, symbol: str, now: datetime, deadline: datetime) -> bool:
        return (self.symbol == symbol and self.starts <= now < deadline <= self.through
                and self.starts <= self.retrieved <= now and self.source.startswith("fixture:official/")
                and not self.unresolved_material)


class OfficialEvidence:
    """Separate fixture store for non-price facts, scoped to the contract and exit."""
    def __init__(self) -> None:
        self.terms: dict[str, Terms] = {}
        self.fees: dict[str, Fees] = {}
        self.calendar: dict[str, Calendar] = {}


class FixtureReadBlocked(Exception):
    """A safe fixed message for the failed stage. It never contains connection state."""


class FixtureFeed:
    """In-memory source. A boolean sentinel models fixture availability."""
    def __init__(self, *, max_calls: int = 100, connected_route: bool = False,
                 lose_after: str | None = None) -> None:
        if max_calls < 0:
            raise ValueError("negative call limit")
        self.fixtures: dict[str, FixtureEvidence] = {}
        self.calls: list[tuple[str, str]] = []
        self.route_resolutions = 0
        self._opened = False
        self._approved = False
        self._available = False
        self._halt: str | None = None
        self._max_calls = max_calls
        self._connected_route = connected_route
        self._lose_after = lose_after
        self._call_start = 0

    def begin(self, approved: bool) -> None:
        if self._opened:
            raise RuntimeError("simulation already open")
        self._opened = True
        self._approved = approved
        self._available = approved and self._connected_route
        self._halt = None
        self._call_start = len(self.calls)

    def end(self) -> None:
        self._available = self._approved = self._opened = False
        self._halt = None

    def _take(self, stage: str, symbol: str) -> FixtureEvidence:
        if not self._opened or not self._approved:
            raise FixtureReadBlocked("explicit synthetic feed authorization missing")
        if self._halt is not None:
            raise FixtureReadBlocked(self._halt)
        if len(self.calls) - self._call_start >= self._max_calls:
            self._halt = "simulated tool-call limit"
            raise FixtureReadBlocked(self._halt)
        if not self._available:
            self._available = True
            self.route_resolutions += 1
        self.calls.append((stage, symbol))
        if stage == self._lose_after:
            self._available = False
            self._halt = "simulated route lost"
        fixture = self.fixtures.get(symbol)
        if fixture is None:
            raise FixtureReadBlocked("synthetic market fixture unavailable")
        return fixture

    def expirations(self, symbol: str) -> tuple[datetime, ...]:
        fixture = self._take("expirations", symbol)
        return tuple(sorted({c.expiration for c in fixture.chain if c.expiration.utcoffset() is not None}))

    def chain(self, symbol: str, expiry: datetime) -> tuple[Contract, ...]:
        return tuple(c for c in self._take("chain", symbol).chain if c.expiration == expiry)

    def underlying(self, symbol: str) -> Quote | None:
        return self._take("underlying quote", symbol).underlying

    def exact_quote(self, symbol: str, osi: str) -> tuple[Quote | None, Greeks | None, bool, bool]:
        f = self._take("exact quote batch", symbol)
        return f.quotes.get(osi), f.greeks.get(osi), f.entitlement, f.supplemental_context

    def greeks(self, symbol: str, osi: str) -> Greeks | None:
        return self._take("Greeks batch", symbol).recovered_greeks.get(osi)
