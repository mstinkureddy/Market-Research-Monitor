"""Fail-closed synthetic batch. No GitHub, brokerage or notification client."""
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import re
from threading import Event, Lock
from time import monotonic
from typing import Protocol

from ..fixtures import DAY, HOURS, VENUE
from ..limits import FIXTURE_SYMBOLS, MAX_SESSION_MINUTES, valid_symbols
from ..synthetic import DataGrant, FixtureAdapter, RESEARCH_ONLY

MAX_CONTROL_AGE = timedelta(seconds=5)
FIXTURE_NOW = DAY.replace(hour=10, minute=5)
SCENARIO_LIMIT = {"success": MAX_SESSION_MINUTES * 60, "cancel": 180}
SIGNED_OFF = "signed terminal revocation observed"


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class Run:
    run_id: str
    attempt: int
    commit: str
    event: str
    job_deadline: datetime
    scenario: str = "success"


REPOSITORY = "mstinkureddy/Market-Research-Monitor"
WORKFLOW = ".github/workflows/top10-synthetic-research.yml"


@dataclass(frozen=True)
class Permit:
    run_id: str
    commit: str
    symbols: tuple[str, ...]
    opens: datetime
    expires: datetime
    scenario: str = "success"


@dataclass(frozen=True)
class Control:
    """Supplied by a trusted adapter. Workflow inputs alone cannot establish verification."""
    verified: bool
    observed: datetime
    enabled: bool
    permit: Permit | None


@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    symbols: tuple[str, ...] = ()
    rows: tuple[dict, ...] = ()


def valid_run(run: Run) -> bool:
    return (run.event == "workflow_dispatch" and type(run.attempt) is int and run.attempt == 1
            and run.scenario in SCENARIO_LIMIT
            and bool(re.fullmatch(r"[1-9][0-9]{0,19}", run.run_id))
            and bool(re.fullmatch(r"[0-9a-f]{40}", run.commit))
            and run.job_deadline.utcoffset() is not None)


def valid_permit(permit: Permit, run: Run) -> bool:
    return (permit.run_id == run.run_id and permit.commit == run.commit
            and permit.scenario == run.scenario and run.scenario in SCENARIO_LIMIT
            and isinstance(permit.opens, datetime) and isinstance(permit.expires, datetime)
            and permit.opens.utcoffset() is not None and permit.expires.utcoffset() is not None
            and timedelta(0) < permit.expires - permit.opens <= timedelta(seconds=SCENARIO_LIMIT[run.scenario])
            and valid_symbols(permit.symbols))


class Batch:
    """One synchronous batch per runner. The platform must fence separate runners."""
    def __init__(self, run: Run, clock: Clock, read_control: Callable[[], Control],
                 *, elapsed: Callable[[], float] = monotonic):
        self.run, self.clock, self.read_control, self.elapsed = run, clock, read_control, elapsed
        self._result = Result("OFF", "awaiting a verified run-specific permit")
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.peak_validations = 0
        self.permit: Permit | None = None
        self.deadline: datetime | None = None
        self.elapsed_deadline: float | None = None
        self._lock = Lock()
        self._stopped = Event()
        self.adapter = FixtureAdapter(DataGrant("SYNTHETIC_FIXTURE", True, True,
                                               "offline synthetic monitor", HOURS.closes))

    @property
    def result(self) -> Result:
        if self._stopped.is_set() and self._result.status in {"OFF", "RUNNING", "COMPLETED"}:
            return Result("STOPPED", "local cancellation observed")
        return self._result

    def stop(self) -> None:
        self._stopped.set()

    def _expired(self, now: datetime) -> bool:
        return (now >= self.run.job_deadline or
                (self.deadline is not None and now >= self.deadline) or
                (self.elapsed_deadline is not None and self.elapsed() >= self.elapsed_deadline))

    def _gate(self) -> bool:
        if self.result.status not in {"OFF", "RUNNING"}:
            return False
        if self._expired(self.clock.now()):
            self._result = Result("EXPIRED", "permit or job deadline reached")
            return False
        try:
            snapshot = self.read_control()
        except (OSError, ValueError):
            self._result = Result("STOPPED", "control unavailable")
            return False
        # A signal can arrive while the control read is pending.
        if self.result.status not in {"OFF", "RUNNING"}:
            return False
        now = self.clock.now()
        if self._expired(now):
            self._result = Result("EXPIRED", "permit or job deadline reached")
            return False
        if (not isinstance(snapshot, Control) or snapshot.verified is not True
                or not isinstance(snapshot.observed, datetime) or snapshot.observed.utcoffset() is None
                or not timedelta(0) <= now - snapshot.observed <= MAX_CONTROL_AGE
                or type(snapshot.enabled) is not bool or not isinstance(snapshot.permit, Permit)):
            self._result = Result("STOPPED", "activation or continued authorization unverified")
            return False
        permit = snapshot.permit
        if not valid_permit(permit, self.run) or (self.permit is not None and self.permit != permit):
            self._result = Result("STOPPED", "permit scope mismatched or changed")
            return False
        if not snapshot.enabled:
            self._result = Result("STOPPED", SIGNED_OFF)
            return False
        if now >= permit.expires:
            self._result = Result("EXPIRED", "permit or job deadline reached")
            return False
        if now < permit.opens:
            self._result = Result("STOPPED", "permit window has not opened")
            return False
        if self.permit is None:
            self.permit = permit
            self.deadline = min(permit.expires, self.run.job_deadline)
            self.elapsed_deadline = self.elapsed() + (self.deadline - now).total_seconds()
        return True

    def execute(self) -> Result:
        if not self._lock.acquire(blocking=False):
            return self.result
        try:
            return self._execute()
        finally:
            self._lock.release()

    def _execute(self) -> Result:
        if self.result.status != "OFF":
            return self.result
        if not valid_run(self.run):
            self._result = Result("STOPPED", "unsupported trigger or run attempt")
            return self.result
        if not self._gate():
            return self.result
        assert self.permit is not None
        self.started_at = self.clock.now()
        self._result = Result("RUNNING", "synthetic fixture analysis", self.permit.symbols)
        if self._stopped.is_set():
            self.stop()
            return self.result
        try:
            coverage = self.adapter.analyze(self.permit.symbols, FIXTURE_NOW, HOURS.closes, VENUE)
        except Exception:
            if self.result.status == "RUNNING":
                self._result = Result("FAILED", "fixture analysis failed")
            return self.result
        if not self._gate():
            return self.result
        raw_rows = coverage.get("results") if isinstance(coverage, dict) else None
        if (not isinstance(raw_rows, list) or len(raw_rows) != len(self.permit.symbols)
                or coverage.get("status") != "SYNTHETIC_ONLY" or coverage.get("label") != RESEARCH_ONLY
                or type(coverage.get("peak_validations")) is not int or not 0 <= coverage["peak_validations"] <= 2
                or not all(isinstance(row, dict) and row.get("symbol") in FIXTURE_SYMBOLS for row in raw_rows)
                or {row.get("symbol") for row in raw_rows} != set(self.permit.symbols)):
            self._result = Result("FAILED", "fixture coverage incomplete")
            return self.result
        rows = tuple({key: row[key] for key in ("symbol", "reason", "checks", "evidence_level", "contract",
                                               "rank", "quote_observed", "dollar_risk", "budget_excess") if key in row}
                     for row in raw_rows)
        self.peak_validations = coverage["peak_validations"]
        self.finished_at = self.clock.now()
        self._result = Result("COMPLETED", "synthetic batch complete", self.permit.symbols, rows)
        return self.result

    def verify_publication(self) -> Result:
        """Recheck before staging an artifact. GitHub status must still gate its use."""
        with self._lock:
            if self.result.status != "COMPLETED":
                return self.result
            completed = self.result
            self._result = replace(completed, status="RUNNING")
            if self._gate():
                self._result = completed
            return self.result
