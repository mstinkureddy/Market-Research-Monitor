"""Verify owner CLI snapshots for one cancellation run without making requests."""
from base64 import b64decode
from datetime import timedelta
import re
from urllib.parse import urlencode

from .batch import REPOSITORY, WORKFLOW
from .github import APPROVED_REF, CONTROL_BRANCH, OPERATOR
from .cancel_probe import ARMED, IDLE, SIGNED_OFF
from .report import decode_object, parse_stamp


def live_step(run, metadata, jobs, step_name, opens, observed, *, previous=None):
    """Return (job ID, completion, observation, delay) only for live evidence."""
    job, = jobs["jobs"]
    if (type(metadata["run_attempt"]) is not int or metadata["run_attempt"] != 1
            or str(metadata["id"]) != run.run_id or str(job["run_id"]) != run.run_id
            or metadata["head_sha"] != run.commit or job["head_sha"] != run.commit
            or metadata["event"] != "workflow_dispatch" or metadata["path"] != WORKFLOW
            or metadata["head_branch"] != APPROVED_REF.removeprefix("refs/heads/")
            or metadata["actor"]["login"] != OPERATOR
            or metadata["repository"]["full_name"] != REPOSITORY
            or metadata["head_repository"]["full_name"] != REPOSITORY
            or metadata["status"] != "in_progress" or metadata["conclusion"] is not None
            or jobs["total_count"] != 1 or job["name"] != "research" or job["status"] != "in_progress"
            or job["conclusion"] is not None or run.scenario != "cancel"):
        raise ValueError("live job provenance invalid")
    step, = (s for s in job["steps"] if s["name"] == step_name)
    completed = parse_stamp(step["completed_at"])
    if (step["status"] != "completed" or step["conclusion"] != "success"
            or not opens <= parse_stamp(job["started_at"]) <= parse_stamp(step["started_at"])
            <= completed <= observed < run.job_deadline
            or observed - completed > timedelta(seconds=15)):
        raise ValueError("step evidence absent, stale or out of order")
    if step_name == SIGNED_OFF:
        idle_step, = (s for s in job["steps"] if s["name"] == IDLE)
        armed_step, = (s for s in job["steps"] if s["name"] == ARMED)
        if (previous is None or previous[0] != job["id"]
                or armed_step["status"] != "completed" or armed_step["conclusion"] != "success"
                or parse_stamp(armed_step["completed_at"]) != previous[1]
                or not previous[1] <= previous[2] <= completed
                or armed_step["number"] >= step["number"] or idle_step["number"] <= step["number"]
                or idle_step["status"] not in {"pending", "queued", "in_progress"}):
            raise ValueError("OFF lacks prior live ARMED or idle window")
    elif step_name != ARMED or previous is not None:
        raise ValueError("unexpected transition")
    return job["id"], completed, observed, (observed - completed).total_seconds()


def verify_permit_history(run, opens, armed, off, get):
    """Read exact permit history from a stable protected tip using owner CLI GETs."""
    path = f"monitor-control/{run.run_id}.json"
    branch = f"branches/{CONTROL_BRANCH}"

    def control_tip():
        record = get(branch)
        sha = record["commit"]["sha"]
        if (record["name"] != CONTROL_BRANCH or record["protected"] is not True
                or not re.fullmatch(r"[0-9a-f]{40}", sha)):
            raise ValueError("control tip unverified")
        return sha

    tip = control_tip()
    # A third result detects extra history without relying on pagination.
    history = get("commits?" + urlencode({"sha": tip, "path": path, "per_page": 3}))
    if not isinstance(history, list) or len(history) != 2:
        raise ValueError("expected exactly two permit commits")
    off_sha, on_sha = (entry["sha"] for entry in history)
    if off_sha == on_sha or not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (on_sha, off_sha)):
        raise ValueError("invalid permit history identity")
    for sha, status, enabled, earliest, latest in (
        (on_sha, "added", True, opens, armed[1]),
        (off_sha, "modified", False, armed[2], off[1]),
    ):
        record = get(f"commits/{sha}")
        commit = record["commit"]
        changed, = record["files"]
        if (record["sha"] != sha or record["author"]["login"] != OPERATOR
                or commit["verification"]["verified"] is not True
                or not earliest <= parse_stamp(commit["committer"]["date"]) <= latest
                or changed["filename"] != path or changed["status"] != status):
            raise ValueError("permit commit evidence invalid")
        file = get(f"contents/{path}?{urlencode({'ref': sha})}")
        expected = {"schema": 1, "repository": REPOSITORY, "workflow": WORKFLOW,
                    "run_id": run.run_id, "attempt": 1, "commit": run.commit, "synthetic": True,
                    "enabled": enabled, "symbols": ["F00"], "scenario": "cancel"}
        if file["path"] != path or file["type"] != "file" or file["encoding"] != "base64":
            raise ValueError("permit file evidence invalid")
        permit = decode_object(b64decode("".join(file["content"].split()), validate=True))
        if (set(permit) != set(expected) | {"opens", "expires"}
                or any(type(permit[key]) is not type(value) or permit[key] != value
                       for key, value in expected.items())
                or parse_stamp(permit["opens"]) != opens
                or parse_stamp(permit["expires"]) != run.job_deadline):
            raise ValueError("permit scope or transition unverified")
        if enabled:
            original = permit
        elif permit != {**original, "enabled": False}:
            raise ValueError("OFF changed additional permit fields")
    if control_tip() != tip:
        raise ValueError("control tip changed during verification")
    return on_sha, off_sha


def verify_terminal(run, job_id, metadata, jobs, artifacts, off, requested, *, opens, armed, get):
    job, = jobs["jobs"]
    if (str(metadata["id"]) != run.run_id or metadata["run_attempt"] != 1
            or metadata["head_sha"] != run.commit or metadata["status"] != "completed"
            or metadata["conclusion"] != "cancelled" or jobs["total_count"] != 1
            or job["id"] != job_id or job_id != off[0] or str(job["run_id"]) != run.run_id
            or job["head_sha"] != run.commit or job["status"] != "completed"
            or job["conclusion"] != "cancelled" or artifacts["total_count"] != 0
            or artifacts["artifacts"] != [] or not off[2] <= requested < run.job_deadline):
        raise ValueError("terminal cancellation, ordering or zero artifacts unverified")
    completed = parse_stamp(job["completed_at"])
    if completed < requested:
        raise ValueError("job ended before cancellation request")
    verify_permit_history(run, opens, armed, off, get)
    return (completed - requested).total_seconds()
