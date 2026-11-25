"""Read-only, run-scoped GitHub control. The workflow template is disabled."""
import base64
import binascii
from collections.abc import Callable
from datetime import timedelta
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .batch import Clock, Control, MAX_CONTROL_AGE, Permit, REPOSITORY, Run, SCENARIO_LIMIT, WORKFLOW, valid_permit, valid_run
from .report import Provenance, decode_object, parse_stamp

APPROVED_REF = "refs/heads/top10-synthetic-approved"
CONTROL_BRANCH = "top10-monitor-control"
OPERATOR = REPOSITORY.split("/")[0]
_API_ROOT = f"https://api.github.com/repos/{REPOSITORY}/"
_API_LIMIT = 256 * 1024
_PERMIT_LIMIT = 4096
_SHA = r"[0-9a-f]{40}"
_RUN = r"[1-9][0-9]{0,19}"


class PermitPending(OSError):
    """No permit file exists yet on the verified branch."""


class ControlUnavailable(OSError):
    """Fixed diagnostic category. Never includes a request, response body or credential."""

    def __init__(self, category: str):
        super().__init__("GitHub control unavailable")
        self.category = category


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubRead:
    """Small allowlist. No write method, arbitrary URL, retry or redirect."""
    def __init__(self, token: str, *, open_request: Callable | None = None):
        # Treat the credential as opaque. Allow GitHub's stateless base64url
        # segments and legacy tokens, but never whitespace or header delimiters.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{20,}", token):
            raise ValueError("GitHub token unavailable")
        self._token = token
        self._open = open_request or build_opener(ProxyHandler({}), NoRedirect()).open
        self.requests = 0

    def get(self, path: str, *, revision: str | None = None) -> dict | list | None:
        permit_path = bool(re.fullmatch(rf"contents/monitor-control/{_RUN}\.json", path))
        history = re.fullmatch(rf"permit-history/({_RUN})", path)
        branches = {f"branches/{CONTROL_BRANCH}", "branches/" + APPROVED_REF.removeprefix("refs/heads/")}
        if not (permit_path or history or path in branches or
                re.fullmatch(rf"(?:actions/runs/{_RUN}(?:/attempts/1/jobs)?|commits/{_SHA})", path)):
            raise ValueError("GitHub route outside the allowlist")
        if (permit_path or bool(history)) != (revision is not None) or (revision is not None and not re.fullmatch(_SHA, revision)):
            raise ValueError("GitHub revision invalid")
        if self.requests >= 96:
            raise OSError("GitHub control call cap exceeded")
        self.requests += 1
        if history:
            url = _API_ROOT + "commits?" + urlencode({"sha": revision, "path": f"monitor-control/{history[1]}.json", "per_page": 2})
        else:
            url = _API_ROOT + path + ("?" + urlencode({"ref": revision}) if revision else "")
        request = Request(url, method="GET", headers={"Authorization": f"Bearer {self._token}",
                          "Accept": "application/vnd.github+json", "Cache-Control": "no-cache",
                          "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self._open(request, timeout=2) as response:
                body = response.read(_API_LIMIT + 1)
            if len(body) > _API_LIMIT:
                raise ValueError
            if history:
                records = json.loads(body)
                if not isinstance(records, list):
                    raise ValueError
                return records
            return decode_object(body)
        except HTTPError as error:
            if permit_path and error.code == 404:
                return None
            category = ("API access denied" if error.code in (401, 403) else
                        "API object not found" if error.code == 404 else "API HTTP failure")
            raise ControlUnavailable(category) from None
        except OSError:
            raise ControlUnavailable("API transport failure") from None
        except (ValueError, RecursionError):
            raise ControlUnavailable("API response invalid") from None


class GitHubControl:
    def __init__(self, run: Run, clock: Clock, get: Callable):
        if not valid_run(run):
            raise ValueError("unsupported workflow execution")
        self.run, self.clock, self.get = run, clock, get

    def _branch(self, name: str) -> str:
        record = self.get(f"branches/{name}")
        if not isinstance(record, dict) or record.get("name") != name or record.get("protected") is not True:
            raise ValueError("control branch unverified")
        sha = record["commit"]["sha"]
        if not re.fullmatch(_SHA, sha):
            raise ValueError("control revision invalid")
        return sha

    def read(self) -> Control:
        return self._read(completed=False)

    def _read(self, *, completed: bool) -> Control:
        observed = self.clock.now()
        category = "run metadata invalid"
        try:
            run = self.run
            meta = self.get(f"actions/runs/{run.run_id}")
            if (str(meta["id"]) != run.run_id or type(meta["run_attempt"]) is not int or meta["run_attempt"] != 1
                    or meta["repository"]["full_name"] != REPOSITORY or meta["head_repository"]["full_name"] != REPOSITORY
                    or meta["path"] != WORKFLOW or meta["event"] != "workflow_dispatch" or meta["head_sha"] != run.commit
                    or meta["head_branch"] != APPROVED_REF.removeprefix("refs/heads/") or meta["actor"]["login"] != OPERATOR
                    or meta["status"] != ("completed" if completed else "in_progress")
                    or meta["conclusion"] != ("success" if completed else None)):
                raise ValueError
            category = "protected control ref invalid"
            sha = self._branch(CONTROL_BRANCH)
            category = "permit file invalid"
            path = f"monitor-control/{run.run_id}.json"
            file = self.get(f"contents/{path}", revision=sha)
            if file is None:
                raise PermitPending("waiting for exact-run approval")
            if file["type"] != "file" or file["path"] != path or file["encoding"] != "base64":
                raise ValueError
            encoded = "".join(file["content"].split())
            if len(encoded) > (_PERMIT_LIMIT + 2) // 3 * 4:
                raise ValueError
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > _PERMIT_LIMIT:
                raise ValueError
            body = decode_object(raw)
            category = "owner signature invalid"
            signature = self.get(f"commits/{sha}")
            if (signature["sha"] != sha or signature["author"]["login"] != OPERATOR
                    or signature["commit"]["verification"]["verified"] is not True):
                raise ValueError
            category = "permit scope invalid"
            expected = {"schema": 1, "repository": REPOSITORY, "workflow": WORKFLOW, "run_id": run.run_id,
                        "attempt": 1, "commit": run.commit}
            if (set(body) != set(expected) | {"synthetic", "enabled", "symbols", "opens", "expires", "scenario"}
                    or any(body[key] != value for key, value in expected.items())
                    or body["synthetic"] is not True or type(body["schema"]) is not int
                    or type(body["attempt"]) is not int):
                raise ValueError
            if type(body["enabled"]) is not bool or not isinstance(body["symbols"], list):
                raise ValueError
            permit = Permit(run.run_id, run.commit, tuple(body["symbols"]),
                            parse_stamp(body["opens"]), parse_stamp(body["expires"]), body["scenario"])
            if not valid_permit(permit, run) or permit.opens.microsecond or permit.expires.microsecond:
                raise ValueError
            category = "permit history invalid"
            if body["enabled"]:
                # On the required linear, non-rewritable control ref, an ON permit may only be created once.
                created, = self.get(f"permit-history/{run.run_id}", revision=sha)
                if (created["author"]["login"] != OPERATOR
                        or created["commit"]["verification"]["verified"] is not True):
                    raise ValueError
            category = "protected ref binding invalid"
            if self._branch(CONTROL_BRANCH) != sha or self._branch(APPROVED_REF.removeprefix("refs/heads/")) != run.commit:
                raise ValueError
            return Control(True, observed, body["enabled"], permit)
        except (AttributeError, binascii.Error, KeyError, TypeError, ValueError, RecursionError, OverflowError):
            raise ControlUnavailable(category) from None

    def completed_proof(self) -> Provenance:
        """Read the known run, its current signed permit and its single terminal job."""
        if self.run.scenario != "success":
            raise OSError("only the success scenario can supply report provenance")
        try:
            jobs = self.get(f"actions/runs/{self.run.run_id}/attempts/1/jobs")
            job, = jobs["jobs"]
            if (jobs["total_count"] != 1 or str(job["run_id"]) != self.run.run_id or job["name"] != "research"
                    or job["status"] != "completed" or job["conclusion"] != "success"):
                raise ValueError
            finished = parse_stamp(job["completed_at"])
            control = self._read(completed=True)
            now = self.clock.now()
            if (control.permit is None or not control.permit.opens <= finished <= now
                    or not finished <= min(control.permit.expires, self.run.job_deadline)
                    or not timedelta(0) <= now - control.observed <= MAX_CONTROL_AGE):
                raise ValueError
            return Provenance(self.run, True, False, "success", finished, not control.enabled, control.permit)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise OSError("terminal run provenance unverified") from None


def workflow_run(env: dict[str, str], now) -> Run:
    """Environment is supplied by the approved runner, never from the permit file."""
    required = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "research",
                "GITHUB_REF": APPROVED_REF, "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{WORKFLOW}@{APPROVED_REF}"}
    if any(env.get(key) != value for key, value in required.items()) or env.get("GITHUB_SHA") != env.get("GITHUB_WORKFLOW_SHA"):
        raise ValueError("workflow execution unverified")
    try:
        run = Run(env["GITHUB_RUN_ID"], 1, env["GITHUB_SHA"], "workflow_dispatch",
                  parse_stamp(env["MONITOR_DEADLINE_UTC"]), env.get("MONITOR_SCENARIO", "success"))
        if (not valid_run(run) or run.job_deadline.microsecond
                or not 0 < (run.job_deadline - now).total_seconds() <= SCENARIO_LIMIT[run.scenario]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("workflow scope unverified") from None
    return run
