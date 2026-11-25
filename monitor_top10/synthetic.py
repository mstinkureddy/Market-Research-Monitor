"""Synthetic-only fixture grant and analysis adapter."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from .fixtures import HOURS, feed_patterns, install_evidence, make_monitor
from .limits import valid_symbols

RESEARCH_ONLY = "RESEARCH CANDIDATE — NOT A BUY SIGNAL"


@dataclass(frozen=True)
class DataGrant:
    source: str
    entitled: bool
    license_confirmed: bool
    reviewed_for: str
    expires: datetime

    def blocker(self, now: datetime) -> str | None:
        if self.source != "SYNTHETIC_FIXTURE":
            return "external market-data connections are disabled"
        if self.entitled is not True:
            return "fixture entitlement unavailable"
        if self.license_confirmed is not True or self.reviewed_for != "offline synthetic monitor":
            return "fixture licensing evidence unavailable"
        if self.expires.utcoffset() is None or self.expires <= now:
            return "fixture evidence grant expired"
        return None


class FixtureAdapter:
    def __init__(self, grant: DataGrant, *, quote_age: int = 0):
        self.grant, self.quote_age = grant, quote_age

    def analyze(self, symbols: tuple[str, ...], now: datetime, deadline: datetime, venue: str | None):
        if not valid_symbols(symbols):
            return {"status": "BLOCKED", "reason": "fixture scope is invalid", "results": []}
        gap = self.grant.blocker(now)
        if gap:
            return {"status": "BLOCKED", "reason": gap, "results": []}
        start = now - timedelta(minutes=6, seconds=4)
        if not HOURS.opens <= start < now < deadline:
            return {"status": "BLOCKED", "reason": "fixture warm-up or exit window unavailable", "results": []}
        monitor = make_monitor(connected_route=True)
        monitor.clock.instant = start
        if not monitor.on(HOURS, deadline.astimezone(HOURS.opens.tzinfo), symbols=symbols, authorized=True, venue=venue):
            return {"status": "BLOCKED", "reason": "fixture session unavailable", "results": []}
        patterns = tuple((symbol, "P" if symbol == "F01" else "C") for symbol in symbols)
        try:
            feed_patterns(monitor, patterns)
            for symbol, side in patterns:
                install_evidence(monitor, symbol, side, age=self.quote_age)
            for _ in range(2):
                monitor.clock.advance(seconds=2)
                monitor.tick()
            rows = []
            ranked = sorted(symbols, key=lambda symbol: (-monitor.universe.scores.get(symbol, 0), symbol))
            for result in monitor.results:
                quotes = result.pricing
                fresh = (len(quotes) == 2 and result.contract is not None and
                         all(q.issue(symbol, now, HOURS) is None for q, symbol in
                             zip(quotes, (result.signal.symbol, result.contract.osi))))
                checks = [{"name": c.name, "status": c.status, "detail": c.detail} for c in result.checks]
                if not fresh and checks[3]["status"] == "Passed":
                    checks[3]["status"] = "Failed"
                    checks[3]["detail"] = "Quote aged past the common final snapshot"
                row = {"symbol": result.signal.symbol,
                       "reason": result.reason if fresh else "fresh two-sided pricing unverified",
                       "checks": checks}
                if fresh and result.display_eligible:
                    row["evidence_level"] = result.level
                    row["contract"] = {"expiration": result.contract.expiration.date().isoformat(),
                                       "strike": str(result.contract.strike),
                                       "side": "call" if result.contract.side == "C" else "put"}
                row.update(rank=ranked.index(result.signal.symbol) + 1,
                           quote_observed=min(result.quote_times).isoformat() if fresh else None,
                           dollar_risk=str(result.economics.loss) if fresh and result.economics else None,
                           budget_excess=str(result.budget_excess) if fresh and result.budget_excess is not None else None)
                rows.append(row)
            return {"status": "SYNTHETIC_ONLY", "label": RESEARCH_ONLY, "results": rows,
                    "peak_validations": monitor.peak_in_flight}
        finally:
            monitor.off()
