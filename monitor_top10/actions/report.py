"""Stage a bounded synthetic report and render only independently verified evidence."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import json
import re

from ..limits import FIXTURE_SYMBOLS
from ..synthetic import RESEARCH_ONLY
from ..validation import BLOCKED, CHECKS, FAIL, NOT_RUN, PASS
from .batch import Batch, FIXTURE_NOW, Permit, REPOSITORY, Run, WORKFLOW, valid_permit, valid_run

ARTIFACT_LIMIT = 64 * 1024
LEVELS = {1: "🟢 Level 1", 2: "🟠 Level 2", 3: "🔴 Level 3", 4: "🔵 Level 4", 5: "⚪ Level 5"}
COLORS = {PASS: "🟢", FAIL: "🔴", BLOCKED: "🟠", NOT_RUN: "⚪"}
REPORT_MAX_AGE = timedelta(minutes=20)
REPORT_KEYS = {"schema", "synthetic", "decision", "label", "permitted_contracts", "repository",
               "workflow", "run_id", "attempt", "commit", "symbols", "rows", "state", "shutdown_reason",
               "peak_validations", "started", "finished", "observed", "deadline", "fixture_as_of"}
ROW_KEYS = {"symbol", "reason", "checks", "rank", "quote_observed", "dollar_risk", "budget_excess"}
CONTRACT_KEYS = {"side", "strike", "expiration"}


@dataclass(frozen=True)
class Provenance:
    """Obtain these platform facts independently of the artifact."""
    run: Run
    verified: bool
    artifact_verified: bool
    conclusion: str
    finished_at: datetime
    revoked: bool
    permit: Permit | None


@dataclass(frozen=True)
class Review:
    status: str
    dashboard: str


def withheld(status: str, reason: str) -> Review:
    return Review(status, f"NO TRADE · {reason}\n\n{RESEARCH_ONLY}\n\nPermitted contracts: 0")


def utc(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat()


def decode_object(payload: bytes) -> dict:
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("ambiguous JSON keys")
        return result

    data = json.loads(payload, object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def parse_stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("timezone required")
    return stamp


def _research_text(value: object) -> bool:
    return isinstance(value, str) and not re.search(r"\b(?:BUY NOW|WAIT FOR TRIGGER)\b", " ".join(value.upper().split()))


def _money(value: object) -> bool:
    return value is None or (isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8})?", value)))


def _valid_rows(rows: object, symbols: tuple[str, ...]) -> bool:
    if (not isinstance(rows, (tuple, list)) or not 1 <= len(rows) == len(symbols) <= 3
            or not all(_valid_row(row) for row in rows)):
        return False
    return ({row["symbol"] for row in rows} == set(symbols)
            and {row["rank"] for row in rows} == set(range(1, len(rows) + 1)))


def _valid_row(row: object) -> bool:
    if (not isinstance(row, dict) or row.get("symbol") not in FIXTURE_SYMBOLS
            or set(row) not in (ROW_KEYS, ROW_KEYS | {"contract", "evidence_level"})
            or type(row.get("rank")) is not int or not _research_text(row.get("reason"))):
        return False
    checks = row.get("checks")
    if not isinstance(checks, list) or len(checks) != len(CHECKS) or any(
        not isinstance(check, dict) or set(check) != {"name", "status", "detail"} or check.get("name") != name
        or check.get("status") not in COLORS or not _research_text(check.get("detail"))
        for check, name in zip(checks, CHECKS)
    ):
        return False
    observed = row.get("quote_observed")
    if observed is not None:
        if checks[3]["status"] != PASS or not timedelta(0) <= FIXTURE_NOW - parse_stamp(observed) <= timedelta(seconds=15):
            return False
    elif checks[3]["status"] == PASS:
        return False
    risk, excess = row.get("dollar_risk"), row.get("budget_excess")
    if (not _money(risk) or not _money(excess) or (excess is not None and risk is None)
            or (risk is not None and (observed is None or checks[4]["status"] != PASS or checks[7]["status"] != PASS))):
        return False
    if "contract" not in row:
        return "evidence_level" not in row
    contract = row["contract"]
    return (observed is not None and checks[4]["status"] == PASS
            and type(row.get("evidence_level")) is int and row["evidence_level"] in (1, 2)
            and isinstance(contract, dict) and set(contract) == CONTRACT_KEYS and contract.get("side") in ("call", "put")
            and isinstance(contract.get("strike"), str) and bool(re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,4})?", contract["strike"]))
            and isinstance(contract.get("expiration"), str) and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", contract["expiration"])))


def stage(batch: Batch) -> bytes | None:
    if batch.run.scenario != "success":
        return None
    result = batch.verify_publication()
    if (result.status != "COMPLETED" or not batch.deadline or not batch.started_at or not batch.finished_at
            or not _valid_rows(result.rows, result.symbols)):
        return None
    payload = json.dumps({
        "schema": 3, "synthetic": True, "decision": "NO TRADE", "label": RESEARCH_ONLY, "permitted_contracts": 0,
        "repository": REPOSITORY, "workflow": WORKFLOW, "run_id": batch.run.run_id, "attempt": batch.run.attempt,
        "commit": batch.run.commit, "symbols": result.symbols, "rows": sorted(result.rows, key=lambda row: row["rank"]),
        "state": result.status, "shutdown_reason": result.reason, "peak_validations": batch.peak_validations,
        "started": utc(batch.started_at.replace(microsecond=0)), "finished": utc(batch.finished_at.replace(microsecond=0)),
        "observed": utc(batch.clock.now().replace(microsecond=0)), "deadline": utc(batch.deadline), "fixture_as_of": utc(FIXTURE_NOW),
    }, ensure_ascii=False, sort_keys=True).encode()
    return payload if len(payload) <= ARTIFACT_LIMIT and batch.verify_publication().status == "COMPLETED" else None


def _text(value: str) -> str:
    return escape(" ".join(value.split())).translate(str.maketrans({"|": "&#124;", "[": "&#91;", "]": "&#93;", "`": "&#96;"}))


def _render(record: dict, note: str) -> str:
    lines = ["# Top 10 Monitor · Synthetic research dashboard", "",
             "**NO TRADE · HISTORICAL FIXTURE EVIDENCE**", "", RESEARCH_ONLY, "", "Permitted contracts: 0", "",
             f"{REPOSITORY} · GitHub run {record['run_id']} · {record['commit']}",
             f"State {record['state']} · {_text(record['shutdown_reason'])}",
             f"UTC start {record['started']} · finished {record['finished']} · deadline {record['deadline']}",
             f"Artifact staged {record['observed']} · fixture as of {record['fixture_as_of']}",
             f"Peak concurrent validations {record['peak_validations']}/2 · {note}", "",
             "| Rank | Symbol | Evidence level | Synthetic contract | Quote / terms | Quality checks | Dollar risk | Permitted quantity |",
             "| ---: | --- | --- | --- | --- | --- | --- | ---: |"]
    for row in sorted(record["rows"], key=lambda item: item["rank"]):
        checks = row["checks"]
        passed = sum(item["status"] == PASS for item in checks)
        level = LEVELS.get(row.get("evidence_level"), "UNVERIFIED")
        contract = row.get("contract")
        leg = (_text(f"{contract['side']} {contract['strike']} · expires {contract['expiration']}")
               if contract else "UNVERIFIED")
        quote = "UNVERIFIED"
        if row.get("quote_observed"):
            stamp = parse_stamp(row["quote_observed"])
            age = (FIXTURE_NOW - stamp).total_seconds()
            quote = f"Fresh in fixture · {utc(stamp)} · {age:g}s old"
        terms = checks[4]["status"]
        risk = f"${row['dollar_risk']} modeled" if row.get("dollar_risk") is not None else "UNVERIFIED"
        if row.get("budget_excess") is not None:
            risk += f" · 🔴 OVER BUDGET by ${row['budget_excess']}"
        lines.append(f"| {row['rank']} | {row['symbol']} | {level} | {leg} | {quote} / {COLORS[terms]} {terms} | {passed}/{len(CHECKS)} | {risk} | 0 |")
    for row in record["rows"]:
        lines.extend(("", f"### {row['symbol']}", f"- {_text(row['reason'])}"))
        gaps = [c for c in row["checks"] if c["status"] != PASS]
        for check in gaps:
            lines.append(f"- {COLORS[check['status']]} {check['name']} · {check['status']} · {_text(check['detail'])}")
        if not gaps:
            lines.append("- 🟢 All ten fixture checks passed.")
    return "\n".join(lines) + "\n"


def review(payload: bytes | None, proof: Provenance, now: datetime) -> Review:
    if (proof.verified is not True or not valid_run(proof.run) or proof.permit is None
            or not valid_permit(proof.permit, proof.run)):
        return withheld("UNVERIFIED", "Run provenance unverified.")
    if proof.run.scenario != "success":
        return withheld("INCOMPLETE", "Cancellation probe artifacts are never accepted.")
    if proof.revoked is not False or proof.conclusion != "success":
        return withheld("INCOMPLETE", "Run did not complete with a verified success and clear control.")
    if payload is None:
        return withheld("MISSING_ARTIFACT", "Expected research artifact unavailable.")
    if proof.artifact_verified is not True or len(payload) > ARTIFACT_LIMIT:
        return withheld("UNVERIFIED", "Artifact integrity or size unverified.")
    try:
        record = decode_object(payload)
        expected = {"schema": 3, "synthetic": True, "decision": "NO TRADE", "label": RESEARCH_ONLY, "permitted_contracts": 0,
                    "repository": REPOSITORY, "workflow": WORKFLOW, "state": "COMPLETED",
                    "shutdown_reason": "synthetic batch complete", "run_id": proof.run.run_id,
                    "attempt": proof.run.attempt, "commit": proof.run.commit}
        if (set(record) != REPORT_KEYS or any(record[key] != value for key, value in expected.items())
                or record["synthetic"] is not True or type(record["permitted_contracts"]) is not int
                or type(record["attempt"]) is not int):
            raise ValueError
        observed, deadline, started, finished, fixture = (
            parse_stamp(record[key]) for key in ("observed", "deadline", "started", "finished", "fixture_as_of"))
        if (not proof.permit.opens <= started <= finished <= observed <= proof.finished_at <= min(now, proof.run.job_deadline, proof.permit.expires)
                or not observed < deadline == min(proof.permit.expires, proof.run.job_deadline) or fixture != FIXTURE_NOW
                or type(record["peak_validations"]) is not int or not 0 <= record["peak_validations"] <= 2):
            raise ValueError
        record.update(observed=utc(observed), deadline=utc(deadline), started=utc(started),
                      finished=utc(finished), fixture_as_of=utc(fixture))
        if (not isinstance(record["symbols"], list) or tuple(record["symbols"]) != proof.permit.symbols
                or not _valid_rows(record["rows"], proof.permit.symbols)):
            raise ValueError
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
        return withheld("UNVERIFIED", "Artifact schema or run binding failed.")
    stale = now - observed >= REPORT_MAX_AGE or now >= deadline
    return Review("STALE" if stale else "HISTORICAL",
                  _render(record, "Stale report. Archive only." if stale else "Completed synthetic run. Archive only."))
