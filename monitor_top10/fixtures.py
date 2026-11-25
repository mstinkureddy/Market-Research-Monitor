"""Reproducible synthetic assets, option chains, OHLCV and fake time."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from .evidence import (D, Calendar, Contract, Fees, Greeks, OfficialEvidence, FixtureEvidence,
                       Quote, SessionHours, FixtureFeed, Terms)
from .market import Asset, Bar, Side
from .session import Monitor
from .validation import PlanningRisk


ET = timezone(timedelta(hours=-5), "EST")
DAY = datetime(2026, 11, 20, 9, 54, tzinfo=ET)
HOURS = SessionHours(DAY.replace(hour=9, minute=30), DAY.replace(hour=16, minute=0))
CUTOFF = DAY.replace(hour=15, minute=45)
SECTORS = ("technology", "health", "energy", "financials", "utilities", "industrial",
           "consumer staples", "materials", "communications", "real estate")
ASSETS = tuple(Asset(f"F{i:02d}", SECTORS[i % len(SECTORS)], .99 - .02 * i,
                     .90 - .01 * (i % 3), .03 * (i % 4)) for i in range(30))
VENUE = "Synthetic Manual"


@dataclass
class FakeClock:
    instant: datetime = DAY

    def now(self) -> datetime:
        return self.instant

    def advance(self, **delta: float) -> datetime:
        return self.set(self.instant + timedelta(**delta))

    def set(self, instant: datetime) -> datetime:
        if instant < self.instant:
            raise ValueError("fixture clock cannot move backward")
        self.instant = instant
        return instant


def make_monitor(*, max_calls: int = 100, connected_route: bool = False,
                 lose_after: str | None = None, latency: float = 2,
                 portfolio: PlanningRisk | None = None) -> Monitor:
    return Monitor(FakeClock(), ASSETS, FixtureFeed(max_calls=max_calls,
                   connected_route=connected_route, lose_after=lose_after),
                   OfficialEvidence(), validation_latency=timedelta(seconds=latency),
                   portfolio=portfolio)


def bars(symbol: str, end: datetime, side: Side = "C", *, fake: bool = False) -> tuple[Bar, ...]:
    rows = ((99.8, 100.6, 99.0, 100.1, 100), (100.1, 101.0, 99.5, 100.5, 120),
            (100.5, 100.9, 99.8, 100.4, 110), (100.4, 101.65, 100.35, 101.55, 205),
            (101.55, 101.60, 101.00, 100.8 if fake else 101.25, 105),
            (101.25, 102.1, 101.20, 101.9, 165))
    output = []
    for index, (op, hi, lo, close, volume) in enumerate(rows):
        if fake and index == 4:
            lo = 100.30
        if side == "P":
            op, hi, lo, close = 200 - op, 200 - lo, 200 - hi, 200 - close
        start = end - timedelta(minutes=len(rows) - index)
        output.append(Bar(symbol, start, start + timedelta(minutes=1), op, hi, lo, close, volume))
    return tuple(output)


def feed_patterns(monitor: Monitor, symbols: tuple[tuple[str, Side], ...], *, fake: bool = False) -> None:
    end = monitor.clock.now() + timedelta(minutes=6)
    for group in zip(*(bars(symbol, end, side, fake=fake) for symbol, side in symbols)):
        monitor.clock.set(group[0].end)
        for bar in group:
            assert monitor.ingest(bar), f"fixture bar rejected: {bar.symbol} {bar.end}"


def install_evidence(monitor: Monitor, symbol: str = "F00", side: Side = "C", *,
                     ask: str = ".30", delta: str = ".55", age: int = 0,
                     bad_quote: str | None = None, terms_issue: str | None = None,
                     fee_issue: str | None = None, catalyst: bool = False,
                     supplemental: bool = True, greek_fallback: bool = False) -> str:
    now = monitor.clock.now()
    expiry = now.replace(hour=16, minute=0, second=0) + timedelta(days=3)
    strike = D(102 if side == "C" else 98)
    opposite = "P" if side == "C" else "C"
    ident = f"{symbol}{expiry:%y%m%d}{side}{int(strike * 1000):08d}"
    another = f"{symbol}{expiry:%y%m%d}{opposite}{int(strike * 1000):08d}"
    contract = Contract(ident, symbol, side, strike, expiry, expiry, D(".02"), 8)
    chain = (contract, replace(contract, osi=another, side=opposite))
    observed = now - timedelta(seconds=age)
    center = D("101.90" if side == "C" else "98.10")
    underlying = Quote(symbol, center-D(".02"), center+D(".02"), 20, 20, now, now)
    quote = Quote(ident, D(ask)-D(".02"), D(ask), 8, 8, observed, observed)
    if bad_quote == "crossed":
        quote = replace(quote, bid=quote.ask + D(".01"))
    if bad_quote == "incomplete":
        quote = replace(quote, ask_at=None, ask_size=0)
    if bad_quote == "underlying":
        underlying = replace(underlying, bid_at=None)
    if bad_quote == "identity":
        quote = replace(quote, symbol=another)
    if bad_quote == "future":
        quote = replace(quote, bid_at=now + timedelta(seconds=20))
    marks = Greeks(ident, now, D(".28"), D(delta) * (1 if side == "C" else -1),
                   D(".01"), D("-.04"), D(".8"))
    monitor.feed.fixtures[symbol] = FixtureEvidence(
        chain, underlying, {ident: quote}, {} if greek_fallback else {ident: marks},
        recovered_greeks={ident: marks} if greek_fallback else {}, supplemental_context=supplemental)
    official = monitor.validator.official
    terms = Terms(ident, D(100), ((symbol, D(100)),), False, "fixture:occ/exact-series", now, now)
    if terms_issue == "adjusted":
        terms = replace(terms, adjusted=None)
    if terms_issue == "mismatch":
        terms = replace(terms, osi=another)
    if terms_issue != "missing":
        official.terms[ident] = terms
    fee = Fees(VENUE, D(".05"), D(".06"), D(".10"), "fixture:manual-fee/synthetic-schedule",
               now, now, HOURS.closes)
    if fee_issue == "conflict":
        fee = replace(fee, conflict=True)
    if fee_issue == "zero":
        fee = replace(fee, open_fee=D(0), close_fee=D(0), tail_fee=D(0))
    if fee_issue != "missing":
        official.fees[VENUE.casefold()] = fee
    official.calendar[symbol] = Calendar(symbol, HOURS.opens, HOURS.closes, now,
                                         "fixture:official/calendar", catalyst)
    return ident
