"""Synthetic Stage 2 validation and research decisions."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING

from .evidence import (D, HARD, MIN_RR, STANDARD, ZERO, Calendar, Contract, Fees, Greeks,
                       OfficialEvidence, Quote, FixtureReadBlocked, SessionHours, FixtureFeed, Terms)
from .market import Signal

CHECKS = ("Feed and access", "Scope and screening", "Market-data retrieval",
          "Freshness and pricing", "Instrument or contract terms", "Official sources and costs",
          "Setup analysis", "Exit economics", "Classification and risk rules", "Dashboard and coverage")
PASS, FAIL, BLOCKED, NOT_RUN = "Passed", "Failed", "Blocked", "Not run"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class Economics:
    loss: Decimal
    net: Decimal
    reward_risk: Decimal
    downside: Decimal
    required_move: Decimal | None
    early_profit_bid: Decimal
    expiry_break_even: Decimal
    upper_entry_debit: Decimal
    modeled_exit_bid: Decimal
    quote_friction: Decimal
    extra_slippage: Decimal
    iv_shock: Decimal
    hours_to_exit: Decimal
    hours_remaining_at_exit: Decimal


@dataclass(frozen=True)
class Result:
    signal: Signal
    at: datetime
    deadline: datetime
    level: int | None
    decision: str
    reason: str
    modeled_quantity: int
    checks: tuple[Check, ...]
    steps: tuple[str, ...] = ()
    contract: Contract | None = None
    economics: Economics | None = None
    budget_excess: Decimal | None = None
    quote_times: tuple[datetime, ...] = ()
    terms: Terms | None = None
    fees: Fees | None = None
    greeks: Greeks | None = None
    calendar: Calendar | None = None
    pricing: tuple[Quote, ...] = ()

    @property
    def display_eligible(self) -> bool:
        return self.level in (1, 2) and self.decision != "REJECTED"


@dataclass(frozen=True)
class PlanningRisk:
    """Voluntary synthetic ledger. Unknown actual exposure stays unknown."""
    open_risk: Decimal | None = None
    daily_loss: Decimal | None = None
    weekly_loss: Decimal | None = None

    def __post_init__(self) -> None:
        if any(x is not None and (not x.is_finite() or x < 0)
               for x in (self.open_risk, self.daily_loss, self.weekly_loss)):
            raise ValueError("planning inputs must be finite and nonnegative")

    def budget(self, reserved: Decimal) -> Decimal:
        return max(ZERO, min(STANDARD, HARD,
                             D(100) - (self.open_risk or ZERO) - reserved,
                             D(100) - (self.daily_loss or ZERO) - reserved,
                             D(200) - (self.weekly_loss or ZERO) - reserved))


def model(signal: Signal, now: datetime, deadline: datetime, under: Quote, option: Quote,
          contract: Contract, terms: Terms, fees: Fees, greek: Greeks) -> Economics:
    """Local bid-side Greek approximation with adverse IV and modeled downside."""
    slippage = D("0.01")
    multiplier = terms.multiplier
    entry = option.ask + slippage
    open_outlay = entry * multiplier + fees.open_fee
    loss = open_outlay + max(fees.close_fee, fees.tail_fee)
    direction = D(1) if signal.side == "C" else D(-1)
    # Use the underlying side that reduces the modeled favorable move.
    anchor = under.ask if signal.side == "C" else under.bid
    dt = D(str((deadline - now).total_seconds())) / D(86400)
    shock = min(D("0.02"), greek.iv / 5)
    def exit_bid(move: Decimal) -> Decimal:
        return max(ZERO, option.bid + greek.delta * move + greek.gamma * move * move / 2
                   + greek.theta * dt - greek.vega * shock)
    def net(move: Decimal) -> Decimal:
        return max(ZERO, exit_bid(move) - slippage) * multiplier - open_outlay - fees.close_fee
    target_move = D(str(signal.target)) - anchor
    downside_move = D(str(signal.invalidation)) - anchor
    # Do not let the local convex approximation predict a downside improvement.
    downside = min(net(ZERO), net(downside_move))
    profit = net(target_move)
    required = None
    if profit >= MIN_RR * loss and direction * target_move > 0:
        low, high = ZERO, abs(target_move)
        for _ in range(32):
            middle = (low + high) / 2
            if net(direction * middle) >= MIN_RR * loss:
                high = middle
            else:
                low = middle
        required = high
    early_bid = ((open_outlay + fees.close_fee + MIN_RR * loss) / multiplier + slippage).quantize(
        D(".01"), rounding=ROUND_CEILING)
    expiry_break_even = contract.strike + direction * (open_outlay + fees.tail_fee) / multiplier
    return Economics(loss, profit, profit / loss, downside, required, early_bid, expiry_break_even,
                     entry, exit_bid(target_move), option.ask - option.bid, slippage, shock, dt * 24,
                     D(str((contract.expiration - deadline).total_seconds())) / 3600)


class Validator:
    def __init__(self, feed: FixtureFeed, official: OfficialEvidence) -> None:
        self.feed = feed
        self.official = official

    def evaluate(self, signal: Signal, now: datetime, deadline: datetime, hours: SessionHours,
                 venue: str | None, budget: Decimal, invalidated: bool = False) -> Result:
        report = {name: (NOT_RUN, "Not attempted") for name in CHECKS}
        steps: list[str] = []
        contract = economics = verified_terms = verified_fees = verified_event = marks = None
        quotes: list[Quote] = []
        times: tuple[datetime, ...] = ()
        report[CHECKS[1]] = PASS, "Bounded synthetic intraday shortlist with a completed-bar signal"
        report[CHECKS[6]] = PASS, "Volume-backed break, held retest and separate confirmation"

        def finish(level: int | None, decision: str, reason: str, modeled_quantity: int = 0,
                   excess: Decimal | None = None) -> Result:
            report[CHECKS[9]] = PASS, "Observed scope, decision and verification gaps recorded"
            checks = tuple(Check(name, *report[name]) for name in CHECKS)
            return Result(signal, now, deadline, level, decision, reason, modeled_quantity, checks, tuple(steps),
                          contract, economics, excess, times, verified_terms, verified_fees, marks, verified_event, tuple(quotes))

        if invalidated or not hours.opens <= now < deadline <= hours.closes:
            report[CHECKS[6]] = FAIL, "Signal invalidated or exit window ended"
            report[CHECKS[8]] = PASS, "Invalid setup withheld before pricing"
            return finish(None, "REJECTED", report[CHECKS[6]][1])

        symbol = signal.symbol
        try:
            expirations = self.feed.expirations(symbol)
            steps.append("expirations")
            report[CHECKS[0]] = PASS, "Authorized synthetic feed route used"
            # Allow time beyond the planned exit and use exactly one chain.
            expiry = next((e for e in expirations if e.utcoffset() is not None and e > deadline), None)
            if expiry is None:
                report[CHECKS[2]] = BLOCKED, "No expiry after planned exit"
                report[CHECKS[4]] = BLOCKED, "Exact series not selected"
                return finish(4, "NO TRADE", "eligible expiration unavailable")
            chain = self.feed.chain(symbol, expiry)
            steps.append("chain")
            price = D(str(signal.confirmation_price))
            candidates = [c for c in chain if c.is_exact() and c.side == signal.side
                          and c.underlying == symbol and c.last_trade > deadline
                          and abs(c.strike - price) <= max(D(2), price * D(".04"))
                          and c.screening_spread.is_finite() and c.screening_spread >= 0
                          and c.screening_depth > 0]
            if not candidates:
                report[CHECKS[2]] = BLOCKED, "No eligible exact chain entry"
                report[CHECKS[4]] = BLOCKED, "Exact series not selected"
                return finish(4, "NO TRADE", "exact contract selection incomplete")
            contract = min(candidates, key=lambda c: (c.screening_spread, -c.screening_depth,
                                                       abs(c.strike - price), c.osi))
            terms = self.official.terms.get(contract.osi)
            fees = self.official.fees.get((venue or "").casefold())
            event = self.official.calendar.get(symbol)
            terms_ok = terms is not None and terms.supported(contract, now)
            fee_ok = fees is not None and fees.supported(venue, now, deadline)
            event_ok = event is not None and event.supports(symbol, now, deadline)
            verified_terms = terms if terms_ok else None
            verified_fees = fees if fee_ok else None
            verified_event = event if event_ok else None
            report[CHECKS[4]] = ((PASS, "Exact identity, standard exposure and adjustment status")
                                 if terms_ok else (BLOCKED, "Exact OCC terms absent or unsupported"))
            cost_gaps = []
            if not venue:
                cost_gaps.append("manual venue unnamed")
            elif not fee_ok:
                cost_gaps.append("applicable fees unverified")
            if not event_ok:
                cost_gaps.append("material catalyst timing unverified")
            report[CHECKS[5]] = ((BLOCKED, ". ".join(cost_gaps)) if cost_gaps
                                 else (PASS, "Dated official catalyst and manual-venue fee fixtures"))
            underlying = self.feed.underlying(symbol)
            steps.append("underlying quote")
            if underlying:
                quotes.append(underlying)
            option, marks, entitlement, supplemental = self.feed.exact_quote(symbol, contract.osi)
            steps.append("exact quote batch")
            if option:
                quotes.append(option)
            if marks is None or not marks.usable(contract, now, hours):
                marks = self.feed.greeks(symbol, contract.osi)
                steps.append("Greeks batch")
        except FixtureReadBlocked as error:
            if not steps:
                report[CHECKS[0]] = BLOCKED, str(error)
            report[CHECKS[2]] = BLOCKED, str(error)
            return finish(3 if contract else 4, "NO TRADE", str(error))

        if not entitlement:
            report[CHECKS[0]] = BLOCKED, "Synthetic market-data entitlement not established"
        evidence = (("underlying", underlying, symbol), ("option", option, contract.osi))
        issues = []
        for label, quote, identity in evidence:
            issue = quote.issue(identity, now, hours) if quote else "quote not returned"
            if issue:
                issues.append(f"{label}: {issue}")
        greeks_ok = marks is not None and marks.usable(contract, now, hours)
        times = tuple(t for quote in (underlying, option) if quote
                      for t in (quote.bid_at, quote.ask_at) if t is not None)
        if underlying is not None and option is not None and greeks_ok:
            report[CHECKS[2]] = PASS, "Chain, both quote types and required Greeks returned"
        else:
            report[CHECKS[2]] = BLOCKED, "Required quote or model Greeks unavailable"
        report[CHECKS[3]] = ((FAIL, ". ".join(issues)) if issues
                             else (PASS, "Both sides for option and underlying pass the 15-second gate"))
        blockers = (issues + ([] if terms_ok else ["exact OCC terms unverified"]) + cost_gaps
                    + ([] if entitlement else ["entitlement unverified"])
                    + ([] if greeks_ok else ["early-exit Greeks or IV unverified"]))
        if blockers:
            report[CHECKS[7]] = BLOCKED, "Missing mandatory inputs"
            report[CHECKS[8]] = BLOCKED, "Maximum loss or qualifying evidence incomplete"
            return finish(3, "CONDITIONAL · NO TRADE" if not venue else "NO TRADE", ". ".join(blockers))
        if (option.ask - option.bid) / option.ask > D(".25"):
            report[CHECKS[3]] = FAIL, "Option spread exceeds synthetic liquidity gate"
            report[CHECKS[8]] = PASS, "Verified liquidity rejection"
            return finish(None, "REJECTED", "option spread too wide")
        direction = 1 if signal.side == "C" else -1
        market_entry = underlying.ask if signal.side == "C" else underlying.bid
        max_chase = abs(D(str(signal.target)) - D(str(signal.confirmation_price))) / 4
        if (signal.invalidated(float(market_entry)) or
                direction * (market_entry - D(str(signal.trigger))) <= 0 or
                direction * (market_entry - D(str(signal.confirmation_price))) > max_chase):
            report[CHECKS[6]] = FAIL, "Final underlying quote invalidated or outran the confirmed setup"
            report[CHECKS[8]] = PASS, "Verified setup rejection"
            return finish(None, "REJECTED", report[CHECKS[6]][1])
        economics = model(signal, now, deadline, underlying, option, contract, terms, fees, marks)
        report[CHECKS[7]] = PASS, "After-cost target, adverse IV/time, downside and required move modeled"
        if economics.reward_risk < MIN_RR or economics.required_move is None:
            report[CHECKS[8]] = PASS, "Verified net reward/risk rejection"
            return finish(1, "REJECTED", "net reward/risk below 1.5:1 at supported target")
        if not supplemental:
            report[CHECKS[8]] = PASS, "Only supplemental research context is incomplete; no authorization"
            return finish(2, "NO TRADE", "supplemental sector comparison unavailable")
        size = min(int(budget // economics.loss), option.bid_size, option.ask_size)
        report[CHECKS[8]] = PASS, "Whole-contract sizing at capped maximum loss"
        if not size:
            return finish(1, "NO TRADE", "verified risk exceeds remaining budget", excess=economics.loss - budget)
        return finish(1, "NO TRADE", "fully verified fixture setup", modeled_quantity=size)
