"""Synthetic underlying discovery, completed-bar ingestion and setup ranking."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from statistics import mean
from typing import Literal

Side = Literal["C", "P"]
SIGNAL_TTL = timedelta(minutes=3)


@dataclass(frozen=True)
class Asset:
    symbol: str
    sector: str
    liquidity: float
    volatility_fit: float
    event_risk: float
    optionable: bool = True


@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    complete: bool = True


class BarBook:
    def __init__(self) -> None:
        self.series: dict[str, list[Bar]] = {}

    def ingest(self, bar: Bar, now: datetime, market_open: datetime) -> bool:
        if bar.start.utcoffset() is None or bar.end.utcoffset() is None:
            return False
        if (not bar.complete or not market_open <= bar.start < bar.end <= now
                or bar.end - bar.start != timedelta(minutes=1)
                or now - bar.end > SIGNAL_TTL or bar.volume <= 0):
            return False
        if (not all(isfinite(x) and x > 0 for x in (bar.open, bar.high, bar.low, bar.close))
                or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close)):
            return False
        series = self.series.setdefault(bar.symbol, [])
        if series and (bar.start < series[-1].end or bar.end <= series[-1].end):
            return False
        series.append(bar)
        del series[:-20]
        return True


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    confirmed_at: datetime
    trigger: float
    invalidation: float
    target: float
    confirmation_price: float
    relative_volume: float

    @property
    def key(self) -> tuple[str, Side, datetime]:
        return self.symbol, self.side, self.confirmed_at

    def invalidated(self, price: float) -> bool:
        return price <= self.invalidation if self.side == "C" else price >= self.invalidation


def detect(bars: list[Bar]) -> Signal | None:
    """Require a volume-backed breach, a held retest and a separate confirming bar."""
    if len(bars) < 6:
        return None
    sample = bars[-6:]
    if any(left.end != right.start for left, right in zip(sample, sample[1:])):
        return None
    normal = sample[:3]
    breach, retest, confirm = sample[3:]
    floor = min(bar.low for bar in normal)
    ceiling = max(bar.high for bar in normal)
    span = ceiling - floor
    volume = mean(bar.volume for bar in normal)
    if span <= 0 or breach.volume < 1.5 * volume or confirm.volume < 1.2 * volume:
        return None
    for side, direction, level in (("C", 1, ceiling), ("P", -1, floor)):
        tangent = retest.low if side == "C" else retest.high
        extreme = breach.high if side == "C" else breach.low
        if (direction * (breach.close - level) < .15 * span
                or abs(tangent - level) > .25 * span
                or direction * (retest.close - level) <= 0
                or direction * (confirm.close - extreme) <= 0
                or direction * (confirm.close - breach.open) < .4 * span):
            continue
        target = level + direction * 1.75 * span
        if target <= 0 or direction * (target - confirm.close) <= 0:
            continue
        return Signal(confirm.symbol, side, confirm.end, level, level - direction * .3 * span,
                      target, confirm.close, breach.volume / volume)
    return None


class Universe:
    """Refresh a diversified sample, keeping incumbents through small score changes."""
    def __init__(self, assets: tuple[Asset, ...]) -> None:
        if len({a.symbol for a in assets}) != len(assets):
            raise ValueError("duplicate symbol")
        if len(assets) < 25 or len({a.sector for a in assets}) < 5:
            raise ValueError("need at least 25 assets and five sectors")
        if any(any(not 0 <= v <= 1 for v in (a.liquidity, a.volatility_fit, a.event_risk)) for a in assets):
            raise ValueError("scores must be between zero and one")
        self.assets = {a.symbol: a for a in assets}
        self.watching: tuple[str, ...] = ()
        self.scores: dict[str, float] = {}
        self.last_refresh: datetime | None = None
        self.screened = 0

    def refresh(self, now: datetime, book: BarBook, only: str | None = None) -> bool:
        if self.last_refresh is not None and now - self.last_refresh < timedelta(minutes=1):
            return False
        self.last_refresh = now
        sample = {only: self.assets[only]} if only else self.assets
        self.screened = len(sample)
        scores = {}
        for symbol, asset in sample.items():
            if not asset.optionable or asset.liquidity < .25:
                continue
            bars = book.series.get(symbol, [])
            timely = bars and now - bars[-1].end <= SIGNAL_TTL
            trend = min(abs(bars[-1].close / bars[0].close - 1) * 100, 1) if timely else 0
            signal = detect(bars) if timely else None
            quality = min(signal.relative_volume / 3, 1) if signal else 0
            scores[symbol] = (.5 * asset.liquidity + .15 * asset.volatility_fit
                              + .2 * trend + .15 * quality - .2 * asset.event_risk)
        # An incumbent advantage is used only for membership. Display order uses actual scores.
        previous = set(self.watching)
        membership = sorted(scores, key=lambda s: (-(scores[s] + .04 * (s in previous)), s))[:10]
        self.watching = tuple(sorted(membership, key=lambda s: (-scores[s], s)))
        self.scores = scores
        return True
