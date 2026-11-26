"""Three isolated steps for the hosted synthetic cancellation probe."""
import argparse
import json
import os
from pathlib import Path
import signal
from threading import Event
from datetime import timedelta

from .batch import Batch, MAX_CONTROL_AGE, Permit, valid_permit
from .github import GitHubControl, GitHubRead, workflow_run
from .report import parse_stamp, utc
from .runner import SystemClock, wait_for_permit

ARMED = "Test B ARMED"
SIGNED_OFF = "Test B SIGNED OFF"
IDLE = "Test B CANCELLATION WINDOW"


def scope(run):
    return {"run_id": run.run_id, "attempt": run.attempt, "commit": run.commit,
            "scenario": run.scenario, "symbols": ["F00"], "deadline": utc(run.job_deadline)}


def store(directory, phase, run, permit, now):
    with (directory / phase).open("x", encoding="utf-8") as file:
        json.dump({**scope(run), "phase": phase, "opens": utc(permit.opens),
                   "expires": utc(permit.expires), "observed": utc(now)}, file)


def load(directory, phase, run, now):
    # Claim once before reading. A failed consumer cannot restart this phase.
    (directory / (phase + ".used")).touch(mode=0o600, exist_ok=False)
    with (directory / phase).open(encoding="utf-8") as file:
        state = json.loads(file.read(2048))
    expected = {**scope(run), "phase": phase}
    if (set(state) != set(expected) | {"opens", "expires", "observed"}
            or any(state[key] != value for key, value in expected.items())
            or type(state["attempt"]) is not int or run.scenario != "cancel"):
        raise ValueError("local scope invalid")
    permit = Permit(run.run_id, run.commit, ("F00",), parse_stamp(state["opens"]),
                    parse_stamp(state["expires"]), run.scenario)
    if (not valid_permit(permit, run)
            or not permit.opens <= parse_stamp(state["observed"]) <= now
            or now >= min(permit.expires, run.job_deadline)):
        raise ValueError("local state expired or invalid")
    return permit


def verify(snapshot, permit, run, now, stop):
    if (stop.is_set() or not permit.opens <= now < min(permit.expires, run.job_deadline)
            or snapshot.verified is not True or snapshot.permit != permit
            or type(snapshot.enabled) is not bool
            or not timedelta(0) <= now - snapshot.observed <= MAX_CONTROL_AGE):
        raise ValueError("control evidence unavailable or changed")
    return snapshot.enabled


def arm(directory, run, clock, control, stop, register):
    directory.mkdir(mode=0o700)
    approval = wait_for_permit(control, clock, run.job_deadline, stop, cancellation=True)
    permit = approval.permit
    if permit.symbols != ("F00",) or not verify(approval, permit, run, clock.now(), stop):
        raise ValueError("signed ON scope invalid")

    def read_bound():
        snapshot = control.read()
        verify(snapshot, permit, run, clock.now(), stop)
        return snapshot

    batch = Batch(run, clock, read_bound)
    register(batch)
    if batch.execute().status != "COMPLETED" or batch.verify_publication().status != "COMPLETED":
        raise ValueError("synthetic batch did not remain authorized")
    if stop.is_set() or clock.now() >= batch.deadline:
        raise ValueError("ARMED fence closed")
    store(directory, "armed", run, permit, clock.now())


def observe_off(directory, run, clock, control, stop):
    permit = load(directory, "armed", run, clock.now())
    for _ in range(8):
        if stop.is_set() or clock.now() >= min(permit.expires, run.job_deadline):
            break
        snapshot = control.read()
        if not verify(snapshot, permit, run, clock.now(), stop):
            store(directory, "off", run, permit, clock.now())
            return
        stop.wait(max(0, min(5, (min(permit.expires, run.job_deadline) - clock.now()).total_seconds())))
    raise ValueError("signed OFF not observed")


def idle(directory, run, clock, stop):
    permit = load(directory, "off", run, clock.now())
    # No batch, network, or report path exists in this step.
    stop.wait(max(0, min(30, (min(permit.expires, run.job_deadline) - clock.now()).total_seconds())))
    return 78  # GitHub must report terminal cancellation for acceptance.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("armed", "off", "idle"))
    args = parser.parse_args()
    clock = SystemClock()
    stop = Event()
    batch = None

    def register(active):
        nonlocal batch
        batch = active

    def interrupt(signum, frame):
        stop.set()
        if batch is not None:
            batch.stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupt)
    try:
        run = workflow_run(os.environ, clock.now())
        root = Path(os.environ["RUNNER_TEMP"])
        if run.scenario != "cancel" or not root.is_absolute() or not root.is_dir():
            raise ValueError("invalid cancellation environment")
        directory = root / f"top10-cancel-{run.run_id}-{run.attempt}"
        if args.phase == "idle":
            return idle(directory, run, clock, stop)
        reader = GitHubRead(os.environ.pop("MONITOR_GITHUB_TOKEN", ""))
        control = GitHubControl(run, clock, reader.get)
        if args.phase == "armed":
            arm(directory, run, clock, control, stop, register)
        else:
            observe_off(directory, run, clock, control, stop)
        if stop.is_set() or clock.now() >= run.job_deadline:
            raise ValueError("step stopped")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print(f"STOPPED · Test B {args.phase} evidence unavailable")
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
