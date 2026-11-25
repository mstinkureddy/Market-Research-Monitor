"""Synthetic GitHub runner and reporting acceptance with no platform calls."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from threading import Event, Thread
import unittest
from unittest.mock import patch

from monitor_top10.actions.batch import Batch, Control, Permit, Run
from monitor_top10.actions.report import Provenance, review, stage
from monitor_top10.fixtures import FakeClock


class ActionsMonitor(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.now = self.clock.now()
        self.limit = self.now + timedelta(minutes=20)
        self.run_record = Run("137", 1, "a" * 40, "workflow_dispatch", self.limit)
        self.permit = Permit("137", "a" * 40, ("F00", "F01", "F02"), self.now, self.limit)
        self.enabled, self.verified = True, True
        self.elapsed = 0
        self.batch = self.new_batch()

    def control(self):
        return Control(self.verified, self.clock.now(), self.enabled, self.permit)

    def new_batch(self, run=None, control=None):
        return Batch(run or self.run_record, self.clock, control or self.control, elapsed=lambda: self.elapsed)

    def proof(self, **changes):
        return replace(Provenance(self.run_record, True, True, "success", self.now, False, self.permit), **changes)

    def complete(self):
        self.assertEqual(self.batch.execute().status, "COMPLETED")
        payload = stage(self.batch)
        self.assertIsNotNone(payload)
        return payload

    def test_off_by_default_and_complete_three_symbol_report(self):
        self.assertEqual(self.batch.result.status, "OFF")
        self.assertIsNone(stage(self.batch))
        report = review(self.complete(), self.proof(), self.now)
        self.assertEqual(report.status, "HISTORICAL")
        for marker in ("F00", "F01", "F02", "NO TRADE", "NOT A BUY SIGNAL", "Quality checks", "Dollar risk"):
            self.assertIn(marker, report.dashboard)
        self.assertNotIn("BUY NOW", report.dashboard)

    def test_unverified_and_unarmed_control_do_not_enter_analysis(self):
        for snapshot in (Control(False, self.now, True, self.permit),
                         Control(True, self.now, False, self.permit),
                         Control(True, self.now, True, None),
                         Control(True, self.now - timedelta(seconds=6), True, self.permit)):
            with self.subTest(snapshot=snapshot):
                batch = self.new_batch(control=lambda: snapshot)
                with patch.object(batch.adapter, "analyze") as analyze:
                    self.assertEqual(batch.execute().status, "STOPPED")
                    analyze.assert_not_called()

    def test_wrong_trigger_rerun_and_duplicate_run_id_rejected(self):
        for run in (replace(self.run_record, event="schedule"), replace(self.run_record, attempt=2),
                    replace(self.run_record, run_id="138"), replace(self.run_record, commit="b" * 40)):
            with self.subTest(run=run):
                self.assertEqual(self.new_batch(run).execute().status, "STOPPED")

    def test_concurrent_local_retry_cannot_start_second_analysis(self):
        entered, finish = Event(), Event()
        analyze = self.batch.adapter.analyze
        outcomes = []

        def delayed(*args):
            entered.set()
            if not finish.wait(3):
                raise AssertionError("test dispatch was not released")
            return analyze(*args)

        with patch.object(self.batch.adapter, "analyze", side_effect=delayed) as delegate:
            worker = Thread(target=lambda: outcomes.append(self.batch.execute()))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.batch.execute().status, "RUNNING")
            finally:
                finish.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, "COMPLETED")
            self.batch.execute()
            self.assertEqual(delegate.call_count, 1)

    def test_workflow_timeout_during_analysis_discards_results(self):
        job_cutoff = self.now + timedelta(seconds=4)
        self.batch = self.new_batch(replace(self.run_record, job_deadline=job_cutoff))
        analyze = self.batch.adapter.analyze

        def late(*args):
            result = analyze(*args)
            self.clock.advance(seconds=4)
            return result

        with patch.object(self.batch.adapter, "analyze", side_effect=late):
            self.assertEqual(self.batch.execute().status, "EXPIRED")
        self.assertIsNone(stage(self.batch))

    def test_delayed_control_read_cannot_start_past_job_timeout(self):
        def delayed():
            self.clock.advance(seconds=20 * 60)
            return self.control()

        batch = self.new_batch(control=delayed)
        with patch.object(batch.adapter, "analyze") as analyze:
            self.assertEqual(batch.execute().status, "EXPIRED")
            analyze.assert_not_called()

    def test_cancel_after_analysis_before_publication_suppresses_artifact(self):
        self.assertEqual(self.batch.execute().status, "COMPLETED")
        self.batch.stop()
        self.assertIsNone(stage(self.batch))
        self.assertEqual(self.batch.execute().status, "STOPPED")

    def test_off_during_delayed_analysis_is_sticky(self):
        analyze = self.batch.adapter.analyze

        def delayed(*args):
            self.batch.stop()
            return analyze(*args)

        with patch.object(self.batch.adapter, "analyze", side_effect=delayed):
            self.assertEqual(self.batch.execute().status, "STOPPED")
        self.assertIsNone(stage(self.batch))
        self.assertEqual(self.batch.execute().rows, ())

    def test_off_or_network_failure_during_final_control_reads_cannot_publish(self):
        for failure in (Control(True, self.now, False, self.permit), OSError("synthetic control outage"),
                        Control(True, self.now, True, replace(self.permit, symbols=("F00",)))):
            with self.subTest(failure=type(failure)):
                batch = self.new_batch()
                self.assertEqual(batch.execute().status, "COMPLETED")
                with patch.object(batch, "read_control", side_effect=[self.control(), failure]):
                    self.assertIsNone(stage(batch))
                self.assertEqual(batch.result.status, "STOPPED")

    def test_expired_queued_or_oversized_permit_rejected_without_analysis(self):
        self.clock.advance(seconds=20 * 60)
        longer_job = replace(self.run_record, job_deadline=self.clock.now() + timedelta(seconds=5))
        self.assertEqual(self.new_batch(longer_job).execute().status, "EXPIRED")
        self.permit = replace(self.permit, expires=self.limit + timedelta(seconds=1))
        self.assertEqual(self.new_batch(longer_job).execute().status, "STOPPED")
        self.permit = replace(self.permit, opens=self.clock.now(), symbols=("F00", "SPY"))
        self.assertEqual(self.new_batch(longer_job).execute().status, "STOPPED")

    def test_monotonic_deadline_fails_even_if_wall_clock_does_not_advance(self):
        self.assertEqual(self.batch.execute().status, "COMPLETED")
        self.elapsed = 1200
        self.assertIsNone(stage(self.batch))
        self.assertEqual(self.batch.result.status, "EXPIRED")

    def test_runner_termination_leaves_no_report(self):
        with patch.object(self.batch.adapter, "analyze", side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                self.batch.execute()
        self.assertIsNone(stage(self.batch))
        self.assertEqual(review(None, self.proof(conclusion="cancelled"), self.now).status, "INCOMPLETE")

    def test_partial_analysis_or_platform_failure_cannot_promote(self):
        good = self.complete()
        for conclusion in ("timed_out", "failure", "cancelled", "in_progress"):
            self.assertEqual(review(good, self.proof(conclusion=conclusion), self.now).status, "INCOMPLETE")
        self.assertEqual(review(good, self.proof(revoked=True), self.now).status, "INCOMPLETE")
        batch = self.new_batch()
        analyze = batch.adapter.analyze

        def partial(*args):
            coverage = analyze(*args)
            return coverage | {"results": coverage["results"][:-1]}

        with patch.object(batch.adapter, "analyze", side_effect=partial):
            self.assertEqual(batch.execute().status, "FAILED")
        self.assertIsNone(stage(batch))

    def test_artifact_missing_unverified_and_stale_are_separate(self):
        payload = self.complete()
        self.assertEqual(review(None, self.proof(), self.now).status, "MISSING_ARTIFACT")
        self.assertEqual(review(payload, self.proof(artifact_verified=False), self.now).status, "UNVERIFIED")
        report = review(payload, self.proof(), self.limit)
        self.assertEqual(report.status, "STALE")
        self.assertIn("Archive only", report.dashboard)

    def test_archive_tampering_cannot_change_run_or_research_label(self):
        record = json.loads(self.complete())
        for tampered in (record | {"run_id": "999"}, record | {"decision": "BUY NOW"},
                         record | {"symbols": ["SPY"]}, record | {"rows": record["rows"][:-1]}):
            self.assertEqual(review(json.dumps(tampered).encode(), self.proof(), self.now).status, "UNVERIFIED")
        narrow = replace(self.permit, symbols=("F00",))
        self.assertEqual(review(json.dumps(record).encode(), self.proof(permit=narrow), self.now).status, "UNVERIFIED")
        record["rows"][0]["reason"] = "BUY NOW"
        self.assertEqual(review(json.dumps(record).encode(), self.proof(), self.now).status, "UNVERIFIED")

    def test_workflow_is_outside_active_path_and_has_default_deny(self):
        path = Path("monitor_top10/actions/synthetic-monitor.workflow.disabled.yml")
        config = path.read_text()
        self.assertFalse(Path(".github/workflows/synthetic-monitor.yml").exists())
        for setting in ("workflow_dispatch:", "if: ${{ false }}", "cancel-in-progress: false",
                        "timeout-minutes: ${{ inputs.scenario == 'cancel' && 3 || 20 }}", "permissions: {}", "contents: read", "actions: read",
                        "persist-credentials: false", "retention-days: 1", "if-no-files-found: error",
                        "exec python3 -B -m monitor_top10.actions.runner --github"):
            self.assertIn(setting, config)
        for forbidden in ("schedule:", "push:", "pull_request_target", "actions: write", "contents: write",
                          "id-token: write", "secrets."):
            self.assertNotIn(forbidden, config)
