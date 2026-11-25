"""One-batch CLI for offline rehearsal and the separately gated workflow template."""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
from threading import Event
from time import monotonic
from uuid import uuid4

from .batch import Batch, Control, Permit, Result, Run, SIGNED_OFF
from .github import GitHubControl, GitHubRead, PermitPending, workflow_run
from .report import Provenance, review, stage, utc
from ..fixtures import FakeClock


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def wait_for_permit(source, clock, deadline, stop: Event, *, elapsed=monotonic, cancellation=False):
    """Allow a brief phone handoff. Only a missing permit can be polled."""
    polls = 10 if cancellation else 15
    until = elapsed() + 5 * polls
    for _ in range(polls):
        if stop.is_set() or clock.now() >= deadline or elapsed() >= until:
            break
        try:
            approval = source.read()
        except PermitPending:
            remaining = min(until - elapsed(), (deadline - clock.now()).total_seconds())
            if remaining > 0:
                stop.wait(min(5, remaining))
            continue
        if stop.is_set() or clock.now() >= deadline or elapsed() >= until:
            break
        if approval.enabled is not True or approval.verified is not True or approval.permit is None:
            raise OSError("run was not armed")
        return approval
    raise OSError("GitHub approval was not available within the approved window")


def run_batch(batch: Batch, output: Path) -> Result:
    """Fresh directory per job. Only the final JSON file can reach the upload step."""
    if batch.result.status == "STOPPED":
        return batch.result
    try:
        output.mkdir(mode=0o700)
    except OSError:
        return Result("STOPPED", "output directory unavailable or already used")
    temporary = output / ("pending-" + uuid4().hex)
    final = output / "report.json"
    published = False
    try:
        result = batch.execute()
        if result.status != "COMPLETED":
            return result
        payload = stage(batch)
        if payload is None:
            result = batch.result
            return result if result.status != "COMPLETED" else Result("FAILED", "invalid report evidence")
        with temporary.open("xb") as stream:
            stream.write(payload)
        if batch.verify_publication().status != "COMPLETED":
            return batch.result
        temporary.replace(final)
        if batch.result.status != "COMPLETED":
            return batch.result
        published = True
        return batch.result
    except (OSError, ValueError):
        return Result("FAILED", "report staging failed")
    finally:
        temporary.unlink(missing_ok=True)
        if not published or batch.result.status != "COMPLETED":
            final.unlink(missing_ok=True)


def run_cancellation_probe(batch: Batch, output: Path, stop: Event) -> Result:
    """One synthetic batch, then a brief cancellation window without publication."""
    if batch.run.scenario != "cancel" or batch.result.status == "STOPPED":
        return Result("STOPPED", "cancellation probe is not authorized")
    try:
        output.mkdir(mode=0o700)
    except OSError:
        return Result("STOPPED", "output directory unavailable or already used")
    result = batch.execute()
    if result.status != "COMPLETED" or batch.deadline is None:
        return result
    print(f"ARMED · {utc(batch.clock.now())} · fixture work complete; awaiting signed OFF", flush=True)

    for _ in range(6):
        remaining = (batch.deadline - batch.clock.now()).total_seconds()
        stop.wait(max(0, min(8, remaining)))
        if stop.is_set():
            batch.stop()
        result = batch.verify_publication()
        if result.reason == SIGNED_OFF:
            print(f"STOPPED · {utc(batch.clock.now())} · {SIGNED_OFF}; no further work", flush=True)
            # Keep the process briefly visible for phone cancellation after the work fence.
            stop.wait(max(0, min(20, (batch.deadline - batch.clock.now()).total_seconds())))
            return result
        if result.status != "COMPLETED":
            return result
    batch.stop()
    return Result("STOPPED", "cancellation probe ended without observed signed OFF")


def rehearse(output: Path) -> Result:
    clock = FakeClock(datetime(2026, 11, 21, 16, tzinfo=timezone.utc))
    run = Run("1", 1, "a" * 40, "workflow_dispatch", clock.now() + timedelta(minutes=20))
    permit = Permit(run.run_id, run.commit, ("F00", "F01", "F02"), clock.now(), run.job_deadline)
    batch = Batch(run, clock, lambda: Control(True, clock.now(), True, permit))
    result = run_batch(batch, output)
    if result.status == "COMPLETED":
        checked = review((output / "report.json").read_bytes(),
                         Provenance(run, True, True, "success", clock.now(), False, permit), clock.now())
        if checked.status != "HISTORICAL":
            (output / "report.json").unlink(missing_ok=True)
            return Result("FAILED", "local review failed")
        (output / "dashboard.md").write_text(checked.dashboard, encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--rehearse", action="store_true", help="explicit local synthetic simulation")
    modes.add_argument("--github", action="store_true", help="future approved Actions run")
    parser.add_argument("--output", type=Path, default=Path("_monitor"))
    args = parser.parse_args()
    if not (args.rehearse or args.github):
        print("OFF · explicit activation required")
        return 78
    if args.rehearse:
        result = rehearse(args.output)
    else:
        clock = SystemClock()
        stop = Event()
        batch = None

        def interrupt(signum, frame):
            stop.set()
            if batch is not None:
                batch.stop()

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, interrupt)
        try:
            run = workflow_run(os.environ, clock.now())
            reader = GitHubRead(os.environ.pop("MONITOR_GITHUB_TOKEN", ""))
            control = GitHubControl(run, clock, reader.get)
            approval = wait_for_permit(control, clock, run.job_deadline, stop, cancellation=run.scenario == "cancel")

            def read_bound():
                snapshot = control.read()
                if snapshot.permit != approval.permit:
                    raise OSError("run approval changed")
                return snapshot

            batch = Batch(run, clock, read_bound)
            if stop.is_set():
                batch.stop()
            result = (run_cancellation_probe(batch, args.output, stop) if run.scenario == "cancel"
                      else run_batch(batch, args.output))
            if stop.is_set():
                if result.status == "COMPLETED":
                    (args.output / "report.json").unlink(missing_ok=True)
                result = Result("STOPPED", "local cancellation observed")
        except (OSError, ValueError):
            result = Result("STOPPED", "workflow or run approval unverified")
    print(f"{result.status} · {result.reason}")
    return 0 if result.status == "COMPLETED" else 78


if __name__ == "__main__":
    raise SystemExit(main())
