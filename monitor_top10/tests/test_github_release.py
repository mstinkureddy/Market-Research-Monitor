"""Artifact and GitHub-runner acceptance with injected GitHub REST responses."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
import signal
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from zipfile import ZipFile

from monitor_top10.actions.archive import Archive, MEMBER, NAME, review_archive
from monitor_top10.actions.batch import Batch, Control, Permit, REPOSITORY, Run, WORKFLOW
from monitor_top10.actions.github import APPROVED_REF, CONTROL_BRANCH, OPERATOR, GitHubControl, GitHubRead, PermitPending, workflow_run
from monitor_top10.actions.report import Provenance, decode_object, utc
from monitor_top10.actions.runner import main, rehearse, run_batch, run_cancellation_probe, wait_for_permit
from monitor_top10.fixtures import FakeClock


class GitHubRelease(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.now = self.clock.now()
        self.run_record = Run("137", 1, "a" * 40, "workflow_dispatch", self.now + timedelta(minutes=20))
        self.scope = Permit("137", self.run_record.commit, ("F02", "F00", "F01"), self.now, self.run_record.job_deadline)
        self.revision = "b" * 40
        self.approved_branch = APPROVED_REF.removeprefix("refs/heads/")
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "research",
                    "GITHUB_REF": APPROVED_REF, "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{WORKFLOW}@{APPROVED_REF}",
                    "GITHUB_SHA": self.run_record.commit, "GITHUB_WORKFLOW_SHA": self.run_record.commit,
                    "GITHUB_RUN_ID": "137", "MONITOR_DEADLINE_UTC": utc(self.run_record.job_deadline),
                    "MONITOR_GITHUB_TOKEN": "fixture_" + "x" * 30}
        self.meta = {"id": 137, "run_attempt": 1, "repository": {"full_name": REPOSITORY},
                     "head_repository": {"full_name": REPOSITORY}, "path": WORKFLOW,
                     "event": "workflow_dispatch", "head_sha": self.run_record.commit,
                     "head_branch": self.approved_branch, "actor": {"login": OPERATOR},
                     "status": "in_progress", "conclusion": None}
        self.body = {"schema": 1, "repository": REPOSITORY, "workflow": WORKFLOW, "run_id": "137",
                     "attempt": 1, "commit": self.run_record.commit, "enabled": True, "synthetic": True, "scenario": "success",
                     "symbols": list(self.scope.symbols), "opens": utc(self.now), "expires": self.env["MONITOR_DEADLINE_UTC"]}
        self.signed = True
        self.protected = True
        self.head = self.revision
        self.history_count = 1
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "output"

    def get(self, route, *, revision=None):
        if route == "actions/runs/137":
            return self.meta
        if route == f"branches/{CONTROL_BRANCH}":
            return {"name": CONTROL_BRANCH, "protected": self.protected, "commit": {"sha": self.head}}
        if route == f"branches/{self.approved_branch}":
            return {"name": self.approved_branch, "protected": self.protected, "commit": {"sha": self.run_record.commit}}
        if route == f"commits/{self.revision}":
            return {"sha": self.revision, "author": {"login": OPERATOR},
                    "commit": {"verification": {"verified": self.signed}}}
        if route == "permit-history/137" and revision == self.revision:
            return [{"author": {"login": OPERATOR}, "commit": {"verification": {"verified": self.signed}}}] * self.history_count
        if route == "contents/monitor-control/137.json" and revision == self.revision:
            return {"type": "file", "path": "monitor-control/137.json", "encoding": "base64",
                    "content": base64.b64encode(json.dumps(self.body).encode()).decode()}
        self.fail("unexpected GitHub request")

    def local_control(self):
        return Control(True, self.clock.now(), True, self.scope)

    def batch(self, read=None):
        return Batch(self.run_record, self.clock, read or self.local_control, elapsed=lambda: 0)

    def proof(self):
        return Provenance(self.run_record, True, False, "success", self.now, False, self.scope)

    def archive(self, content, filename=MEMBER):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as zip_file:
            zip_file.writestr(filename, content)
        data = buffer.getvalue()
        return data, Archive(REPOSITORY, NAME, "137", self.run_record.commit, "sha256:" + sha256(data).hexdigest(), False)

    def test_default_cli_off_without_output_or_github_access(self):
        with patch.object(sys, "argv", ["runner", "--output", str(self.output)]), patch(
                "monitor_top10.actions.runner.GitHubRead") as client, redirect_stdout(StringIO()) as stdout:
            self.assertEqual(main(), 78)
        client.assert_not_called()
        self.assertIn("OFF", stdout.getvalue())
        self.assertFalse(self.output.exists())

    def test_runner_env_rejects_unapproved_ref_workflow_commit_rerun_and_deadline(self):
        self.assertEqual(workflow_run(self.env, self.now), self.run_record)
        for key, value in (("GITHUB_REF", "refs/heads/main"), ("GITHUB_WORKFLOW_REF", "wrong"),
                           ("GITHUB_WORKFLOW_SHA", "b" * 40), ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "push"),
                           ("MONITOR_DEADLINE_UTC", utc(self.run_record.job_deadline + timedelta(seconds=1)))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                workflow_run(self.env | {key: value}, self.now)

    def test_mock_github_entrypoint_produces_bound_ranked_artifact(self):
        with patch.object(sys, "argv", ["runner", "--github", "--output", str(self.output)]), patch.dict(
                "os.environ", self.env, clear=True), patch(
                "monitor_top10.actions.runner.SystemClock", return_value=self.clock), patch(
                "monitor_top10.actions.runner.GitHubRead", return_value=Mock(get=self.get)), patch.object(
                signal, "signal"), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(main(), 0)
        self.assertNotIn(self.env["MONITOR_GITHUB_TOKEN"], stdout.getvalue())
        files = list(self.output.iterdir())
        self.assertEqual([path.name for path in files], ["report.json"])
        raw = files[0].read_bytes()
        artifact = decode_object(raw)
        self.assertEqual(artifact["repository"], REPOSITORY)
        self.assertEqual(artifact["state"], "COMPLETED")
        self.assertEqual(artifact["shutdown_reason"], "synthetic batch complete")
        self.assertEqual(artifact["peak_validations"], 2)
        self.assertEqual([row["rank"] for row in artifact["rows"]], [1, 2, 3])
        data, metadata = self.archive(raw)
        checked = review_archive(data, metadata, self.proof(), self.now)
        self.assertEqual(checked.status, "HISTORICAL")
        for text in ("🟢 Level 1", "Peak concurrent validations 2/2", "Fresh in fixture",
                     "Synthetic contract", "Quote / terms", "NO TRADE", "NOT A BUY SIGNAL"):
            self.assertIn(text, checked.dashboard)

    def test_unprotected_unsigned_wrong_actor_cancelled_and_overbroad_permits_fail_closed(self):
        ctrl = GitHubControl(self.run_record, self.clock, self.get)
        self.assertEqual(ctrl.read().permit, self.scope)
        for field, invalid in (("actor", {"login": "someone-else"}), ("status", "completed"),
                               ("head_repository", {"full_name": "unapproved/fork"}), ("event", "schedule")):
            with patch.dict(self.meta, {field: invalid}), self.assertRaises(OSError):
                ctrl.read()
        for field, invalid in (("symbols", ["F00", "F01", "F02", "SPY"]), ("commit", "b" * 40),
                               ("enabled", "true"), ("synthetic", False)):
            with patch.dict(self.body, {field: invalid}), self.assertRaises(OSError):
                ctrl.read()
        self.signed = False
        with self.assertRaises(OSError):
            ctrl.read()
        self.signed, self.protected = True, False
        with self.assertRaises(OSError):
            ctrl.read()

    def test_signed_permit_rejects_unexpected_metadata_and_ambiguous_values(self):
        for change in ({"extra": "unused"}, {"schema": True}, {"attempt": True}, {"scenario": "cancel"}):
            with self.subTest(change=change), patch.object(self, "body", self.body | change):
                with self.assertRaises(OSError):
                    GitHubControl(self.run_record, self.clock, self.get).read()

    def test_control_revision_change_between_reads_withholds_session(self):
        def moved(route, **kw):
            record = self.get(route, **kw)
            if route == f"commits/{self.revision}":
                self.head = "c" * 40
            return record

        with self.assertRaises(OSError):
            GitHubControl(self.run_record, self.clock, moved).read()

    def test_cancellation_probe_enforces_its_short_bound_and_signed_off_without_artifact(self):
        cutoff = self.now + timedelta(minutes=3)
        run = replace(self.run_record, job_deadline=cutoff, scenario="cancel")
        env = self.env | {"MONITOR_SCENARIO": "cancel", "MONITOR_DEADLINE_UTC": utc(cutoff)}
        self.assertEqual(workflow_run(env, self.now), run)
        with self.assertRaises(ValueError):
            workflow_run(self.env | {"MONITOR_SCENARIO": "cancel"}, self.now)
        self.body["expires"] = utc(cutoff)
        control = GitHubControl(run, self.clock, self.get)
        with self.assertRaises(OSError):
            control.read()  # A success permit cannot authorize the cancellation scenario.
        self.body["scenario"] = "cancel"
        batch = Batch(run, self.clock, control.read, elapsed=lambda: 0)
        stop = Mock(wraps=Event())

        def wait(seconds):
            self.clock.advance(seconds=seconds)
            self.body["enabled"] = False

        stop.wait.side_effect = wait
        with redirect_stdout(StringIO()) as stdout:
            result = run_cancellation_probe(batch, self.output, stop)
        self.assertEqual(result.status, "STOPPED")
        self.assertIn("ARMED", stdout.getvalue())
        self.assertIn("signed terminal revocation observed", stdout.getvalue())
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [8, 20])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cancellation_probe_never_releases_data_even_without_off(self):
        cutoff = self.now + timedelta(minutes=3)
        run = replace(self.run_record, job_deadline=cutoff, scenario="cancel")
        scope = replace(self.scope, expires=cutoff, scenario="cancel")
        batch = Batch(run, self.clock, lambda: Control(True, self.clock.now(), True, scope), elapsed=lambda: 0)
        stop = Mock(wraps=Event())
        stop.wait.side_effect = lambda seconds: self.clock.advance(seconds=seconds)
        with redirect_stdout(StringIO()):
            result = run_cancellation_probe(batch, self.output, stop)
        self.assertIn("without observed signed OFF", result.reason)
        self.assertEqual(stop.wait.call_count, 6)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_manual_archive_review_needs_independent_terminal_job_and_signed_control(self):
        self.assertEqual(run_batch(self.batch(), self.output).status, "COMPLETED")
        data, metadata = self.archive((self.output / "report.json").read_bytes())
        job = {"run_id": 137, "name": "research", "status": "completed", "conclusion": "success",
               "completed_at": utc(self.now)}

        def records(route, **kwargs):
            if route == "actions/runs/137/attempts/1/jobs":
                return {"total_count": 1, "jobs": [job]}
            return self.get(route, **kwargs)

        control = GitHubControl(self.run_record, self.clock, records)
        self.meta.update(status="completed", conclusion="success")
        checked = review_archive(data, metadata, control.completed_proof(), self.now)
        self.assertEqual(checked.status, "HISTORICAL")
        self.assertIn("NO TRADE", checked.dashboard)
        self.body["enabled"] = False
        self.assertEqual(review_archive(data, metadata, control.completed_proof(), self.now).status, "INCOMPLETE")
        self.body["enabled"] = True
        self.history_count = 2
        with self.assertRaises(OSError):
            control.completed_proof()  # Restoring enabled after a path change is not a fresh permit.
        self.history_count = 1
        self.meta["conclusion"] = "cancelled"
        with self.assertRaises(OSError):
            control.completed_proof()
        self.meta["conclusion"] = "success"
        job["completed_at"] = utc(self.run_record.job_deadline + timedelta(seconds=1))
        with self.assertRaises(OSError):
            control.completed_proof()

    def test_missing_permit_wait_is_bounded_and_off_is_terminal(self):
        control = Mock(read=Mock(side_effect=[PermitPending(), replace(self.local_control(), enabled=False)]))
        stop = Mock(wraps=Event())
        stop.wait.side_effect = lambda seconds: self.clock.advance(seconds=seconds)
        with self.assertRaisesRegex(OSError, "not armed"):
            wait_for_permit(control, self.clock, self.run_record.job_deadline, stop)
        self.assertEqual(control.read.call_count, 2)
        expired = self.clock.now()
        with self.assertRaises(OSError):
            wait_for_permit(control, self.clock, expired, Event())
        self.assertEqual(control.read.call_count, 2)

    def test_stateless_installation_token_reaches_read_only_rest_checks(self):
        from urllib.parse import parse_qs, urlsplit

        # Synthetic bearer with the provider's dotted, base64url and >300-character shape.
        credential = "ghs_1234_" + "A" * 64 + "." + "b_" * 120 + "." + "c-" * 80

        def transport(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + credential)
            url = urlsplit(request.full_url)
            self.assertEqual(url.netloc, "api.github.com")
            route = url.path.removeprefix(f"/repos/{REPOSITORY}/")
            query = parse_qs(url.query)
            if route == "commits":
                self.assertEqual(query["path"], ["monitor-control/137.json"])
                value = self.get("permit-history/137", revision=query["sha"][0])
            else:
                value = self.get(route, revision=query.get("ref", [None])[0])
            return BytesIO(json.dumps(value).encode())

        run = workflow_run(self.env, self.now)
        client = GitHubRead(credential, open_request=transport)
        approve = wait_for_permit(GitHubControl(run, self.clock, client.get), self.clock,
                                  run.job_deadline, Event())
        self.assertEqual(approve.permit, self.scope)

    def test_token_header_injection_is_rejected_without_transport(self):
        transport = Mock()
        for delimiter in (" ", "\r", "\n"):
            with self.assertRaises(ValueError):
                GitHubRead("a" * 24 + delimiter + "b" * 24, open_request=transport)
        transport.assert_not_called()

    def test_http_allowlist_refuses_writes_arbitrary_urls_redirects_and_leaks(self):
        transport = Mock(return_value=BytesIO(b"{}"))
        client = GitHubRead(self.env["MONITOR_GITHUB_TOKEN"], open_request=transport)
        for route in ("/actions/runs/137/cancel", "https://invalid.example/", "actions/runs/137/cancel", "../repos/x"):
            with self.assertRaises(ValueError):
                client.get(route)
        transport.assert_not_called()
        client.get("contents/monitor-control/137.json", revision=self.revision)
        request = transport.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, f"https://api.github.com/repos/{REPOSITORY}/contents/monitor-control/137.json?ref={self.revision}")
        self.assertEqual(transport.call_args.kwargs["timeout"], 2)
        transport.return_value = BytesIO(b"[]")
        self.assertEqual(client.get("permit-history/137", revision=self.revision), [])
        self.assertEqual(transport.call_args.args[0].full_url,
                         f"https://api.github.com/repos/{REPOSITORY}/commits?sha={self.revision}&path=monitor-control%2F137.json&per_page=2")
        transport.side_effect = HTTPError(request.full_url, 302, self.env["MONITOR_GITHUB_TOKEN"], {}, None)
        with self.assertRaisesRegex(OSError, "^GitHub control unavailable$"):
            client.get("actions/runs/137")

    def test_concurrent_runner_attempts_can_stage_only_one_artifact_in_one_job_directory(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(run_batch, self.batch(), self.output) for _ in range(2)]
            outcomes = [future.result(timeout=5).status for future in pending]
        self.assertCountEqual(outcomes, ["COMPLETED", "STOPPED"])
        self.assertEqual([path.name for path in self.output.iterdir()], ["report.json"])

    def test_cancel_or_termination_during_dispatch_produces_no_artifact(self):
        for outcome in ("cancel", "exit"):
            with self.subTest(outcome=outcome):
                batch = self.batch()
                target = self.output.with_name(outcome)
                analyze = batch.adapter.analyze

                def interrupt(*args):
                    if outcome == "exit":
                        raise SystemExit(143)
                    batch.stop()
                    return analyze(*args)

                with patch.object(batch.adapter, "analyze", side_effect=interrupt):
                    if outcome == "exit":
                        with self.assertRaises(SystemExit):
                            run_batch(batch, target)
                    else:
                        self.assertEqual(run_batch(batch, target).status, "STOPPED")
                self.assertEqual(list(target.iterdir()), [])

    def test_report_stage_cancellation_discards_written_temporary_file(self):
        batch = self.batch()
        real = batch.verify_publication
        calls = 0

        def before_release():
            nonlocal calls
            calls += 1
            if calls == 3:
                batch.stop()
            return real()

        with patch.object(batch, "verify_publication", side_effect=before_release):
            self.assertEqual(run_batch(batch, self.output).status, "STOPPED")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_time_expiry_during_analysis_and_partial_failure_create_no_artifact(self):
        for outcome in ("expiry", "failure"):
            with self.subTest(outcome=outcome):
                batch = self.batch()
                output = self.output.with_name(outcome)
                analyze = batch.adapter.analyze

                def delayed(*args):
                    coverage = analyze(*args)
                    if outcome == "expiry":
                        self.clock.advance(seconds=1200)
                    else:
                        coverage["results"].pop()
                    return coverage

                with patch.object(batch.adapter, "analyze", side_effect=delayed):
                    self.assertEqual(run_batch(batch, output).status, "EXPIRED" if outcome == "expiry" else "FAILED")
                self.assertEqual(list(output.iterdir()), [])
                self.clock = FakeClock(self.now)

    def test_github_signal_handler_stops_before_artifact_release(self):
        handlers = {}

        def interrupted(*args):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return {}, []

        with patch.object(sys, "argv", ["runner", "--github", "--output", str(self.output)]), patch.dict(
                "os.environ", self.env, clear=True), patch(
                "monitor_top10.actions.runner.SystemClock", return_value=self.clock), patch(
                "monitor_top10.actions.runner.GitHubRead", return_value=Mock(get=self.get)), patch.object(
                signal, "signal", side_effect=handlers.__setitem__), patch(
                "monitor_top10.actions.batch.FixtureAdapter.analyze", side_effect=interrupted), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(main(), 78)
        self.assertIn("STOPPED", stdout.getvalue())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_report_failures_missing_archive_corruption_and_revocation(self):
        self.assertEqual(run_batch(self.batch(), self.output).status, "COMPLETED")
        data, meta = self.archive((self.output / "report.json").read_bytes())
        self.assertEqual(review_archive(None, None, self.proof(), self.now).status, "MISSING_ARTIFACT")
        self.assertEqual(review_archive(data + b"x", meta, self.proof(), self.now).status, "UNVERIFIED")
        self.assertEqual(review_archive(data, replace(meta, expired=True), self.proof(), self.now).status, "UNVERIFIED")
        self.assertEqual(review_archive(data, meta, replace(self.proof(), revoked=True), self.now).status, "INCOMPLETE")
        self.assertEqual(review_archive(data, meta, replace(self.proof(), conclusion="cancelled"), self.now).status, "INCOMPLETE")
        self.assertEqual(review_archive(data, meta, self.proof(), self.run_record.job_deadline).status, "STALE")

    def test_zip_traversal_or_fake_report_with_valid_archive_hash_is_unverified(self):
        for filename, raw in (("../report.json", b"{}"), ("report.json", b"not-json"), ("report.json", b"x" * 65537)):
            with self.subTest(filename=filename, size=len(raw)):
                data, meta = self.archive(raw, filename)
                self.assertEqual(review_archive(data, meta, self.proof(), self.now).status, "UNVERIFIED")

    def test_dashboard_preserves_unverified_gaps_and_marks_only_budget_excess_red(self):
        self.assertEqual(run_batch(self.batch(), self.output).status, "COMPLETED")
        report = decode_object((self.output / "report.json").read_bytes())
        row = report["rows"][0]
        row.pop("contract")
        row.pop("evidence_level")
        row.update(quote_observed=None, dollar_risk=None, budget_excess=None)
        for index, status in ((3, "Failed"), (4, "Blocked"), (7, "Not run")):
            row["checks"][index].update(status=status, detail="test evidence unavailable")
        report["rows"][1].update(dollar_risk="52.00", budget_excess="2.00")
        data, meta = self.archive(json.dumps(report).encode())
        checked = review_archive(data, meta, self.proof(), self.now)
        self.assertEqual(checked.status, "HISTORICAL")
        for text in ("7/10", "🔴 Freshness and pricing", "🟠 Instrument or contract terms", "⚪ Exit economics",
                     "🔴 OVER BUDGET by $2.00", "NO TRADE"):
            self.assertIn(text, checked.dashboard)
        self.assertNotIn("🔴 NO TRADE", checked.dashboard)
        self.assertNotIn("BUY NOW", checked.dashboard)

    def test_offline_rehearsal_writes_json_and_markdown_and_cannot_replace_existing_directory(self):
        self.assertEqual(rehearse(self.output).status, "COMPLETED")
        report = (self.output / "dashboard.md").read_text()
        self.assertIn("RESEARCH CANDIDATE", report)
        self.assertNotIn("BUY NOW", report)
        self.assertEqual(rehearse(self.output).status, "STOPPED")
        self.assertEqual((self.output / "dashboard.md").read_text(), report)

    def test_template_full_reviewed_action_pins_and_no_start_condition(self):
        template = Path("monitor_top10/actions/synthetic-monitor.workflow.disabled.yml").read_text()
        self.assertEqual(re.findall(r"uses: (\S+)", template), [
            "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
            "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
        ])
        self.assertFalse(Path(WORKFLOW).exists())
        self.assertIn("if: ${{ false }}", template)
        self.assertNotIn("always()", template)
