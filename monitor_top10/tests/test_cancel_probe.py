import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest

from monitor_top10.actions.github import APPROVED_REF
from monitor_top10.actions.batch import REPOSITORY, WORKFLOW, Control, Permit, Run
from monitor_top10.actions.cancel_probe import ARMED, IDLE, SIGNED_OFF, arm, idle, load, observe_off, store
from monitor_top10.actions.cancel_evidence import live_step, verify_permit_history, verify_terminal
from monitor_top10.fixtures import FakeClock


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "private"
        self.now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        self.clock = FakeClock(self.now)
        self.run_info = Run("42", 1, "a"*40, "workflow_dispatch", self.now + timedelta(minutes=3), "cancel")
        self.permit = Permit("42", "a"*40, ("F00",), self.now, self.run_info.job_deadline, "cancel")
        self.stop = Event()

    def snapshot(self, enabled=True, **changes):
        return replace(Control(True, self.clock.now(), enabled, self.permit), **changes)

    def arm(self, enabled=True):
        arm(self.directory, self.run_info, self.clock,
            SimpleNamespace(read=lambda: self.snapshot(enabled)), self.stop, lambda batch: None)

    def off(self, snapshot):
        observe_off(self.directory, self.run_info, self.clock, SimpleNamespace(read=lambda: snapshot), self.stop)

    def test_complete_one_batch_signed_off_idle_no_report(self):
        self.arm()
        self.off(self.snapshot(False))
        class Cancel:
            def wait(self, timeout):
                assert 0 < timeout <= 30
        self.assertEqual(idle(self.directory, self.run_info, self.clock, Cancel()), 78)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"armed", "armed.used", "off", "off.used"})
        with self.assertRaises(OSError):
            idle(self.directory, self.run_info, self.clock, Cancel())

    def test_missing_and_out_of_order(self):
        with self.assertRaises(OSError):
            self.off(self.snapshot(False))
        self.arm()
        with self.assertRaises(OSError):
            idle(self.directory, self.run_info, self.clock, self.stop)

    def test_wrong_scope_for_on(self):
        self.permit = replace(self.permit, symbols=("F01",))
        with self.assertRaises(ValueError):
            self.arm()

    def test_revoked_before_arming(self):
        with self.assertRaises(OSError):
            self.arm(False)

    def test_stale_off(self):
        self.arm()
        with self.assertRaises(ValueError):
            self.off(self.snapshot(False, observed=self.now-timedelta(seconds=6)))
        self.assertFalse((self.directory/"off").exists())

    def test_unsigned_off(self):
        self.arm()
        with self.assertRaises(ValueError):
            self.off(self.snapshot(False, verified=False))

    def test_mismatched_off(self):
        self.arm()
        with self.assertRaises(ValueError):
            self.off(self.snapshot(False, permit=replace(self.permit, symbols=("F01",))))

    def test_expired_state_and_workflow_timeout(self):
        self.arm()
        with self.assertRaises(ValueError):
            load(self.directory, "armed", self.run_info, self.run_info.job_deadline)

    def test_future_state(self):
        self.directory.mkdir()
        store(self.directory, "armed", self.run_info, self.permit, self.now + timedelta(seconds=1))
        with self.assertRaises(ValueError):
            load(self.directory, "armed", self.run_info, self.now)

    def test_state_binds_release(self):
        self.arm()
        with self.assertRaises(ValueError):
            load(self.directory, "armed", replace(self.run_info, commit="b"*40), self.now)

    def test_cancel_race_during_off_read_fails_closed(self):
        self.arm()
        def read():
            self.stop.set()
            return self.snapshot(False)
        with self.assertRaises(ValueError):
            observe_off(self.directory, self.run_info, self.clock, SimpleNamespace(read=read), self.stop)
        self.assertFalse((self.directory/"off").exists())


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        self.release = "a"*40
        self.run_info = Run("42", 1, self.release, "workflow_dispatch", self.now+timedelta(minutes=3), "cancel")
        self.meta = dict(id=42, run_attempt=1, head_sha=self.release, event="workflow_dispatch", path=WORKFLOW,
                         head_branch=APPROVED_REF.removeprefix("refs/heads/"), actor={"login":"mstinkureddy"},
                         status="in_progress", conclusion=None, repository={"full_name":REPOSITORY},
                         head_repository={"full_name":REPOSITORY})
        self.job = dict(id=9, run_id=42, head_sha=self.release, name="research", status="in_progress",
                        conclusion=None, started_at=self.stamp(0), steps=[
                            self.step(ARMED, 2, 2), self.step(SIGNED_OFF, 3, 10),
                            dict(name=IDLE, number=4, status="in_progress")])
        self.jobs = {"total_count":1,"jobs":[self.job]}

    def history(self):
        path = "monitor-control/42.json"
        on, off = "b"*40, "c"*40
        branch = "top10-monitor-control"
        records = {
            f"branches/{branch}": {"name":branch,"protected":True,"commit":{"sha":off}},
            f"commits?sha={off}&path=monitor-control%2F42.json&per_page=3": [{"sha":off},{"sha":on}],
        }
        for sha, enabled, seconds in ((on, True, 1), (off, False, 8)):
            records[f"commits/{sha}"] = {
                "sha":sha,"author":{"login":"mstinkureddy"},
                "commit":{"verification":{"verified":True},"committer":{"date":self.stamp(seconds)}},
                "files":[{"filename":path,"status":"added" if enabled else "modified"}],
            }
            body = dict(schema=1, repository=REPOSITORY, workflow=WORKFLOW, run_id="42", attempt=1,
                        commit=self.release, synthetic=True, enabled=enabled, symbols=["F00"],
                        scenario="cancel", opens=self.stamp(0), expires=self.stamp(180))
            records[f"contents/{path}?ref={sha}"] = {
                "path":path,"type":"file","encoding":"base64",
                "content":base64.b64encode(json.dumps(body).encode()).decode(),
            }
        return records

    def verify_history(self, get):
        armed = self.live(ARMED, 3)
        off = self.live(SIGNED_OFF, 11, armed)
        return verify_permit_history(self.run_info, self.now, armed, off, get)

    def test_exact_verified_history(self):
        self.assertEqual(self.verify_history(self.history().__getitem__), ("b"*40,"c"*40))

    def test_extra_missing_and_reordered_commits(self):
        for replacement in ([], [{"sha":"c"*40}], [{"sha":"c"*40},{"sha":"b"*40},{"sha":"d"*40}],
                            [{"sha":"b"*40},{"sha":"c"*40}]):
            records = self.history()
            history_key = next(key for key in records if key.startswith("commits?"))
            records[history_key] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.verify_history(records.__getitem__)

    def test_unverified_author_signature_and_chronology(self):
        for field, value in (("author", {"login":"other"}), ("date", self.stamp(11)),
                             ("date", self.stamp(2)), ("verified", False)):
            records = self.history()
            commit = records["commits/"+"c"*40]
            target = {"author":commit, "date":commit["commit"]["committer"],
                      "verified":commit["commit"]["verification"]}[field]
            target[field] = value
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                self.verify_history(records.__getitem__)

    def test_other_field_change_and_unstable_control_tip(self):
        records = self.history()
        file = records["contents/monitor-control/42.json?ref="+"c"*40]
        body = json.loads(base64.b64decode(file["content"]))
        body["expires"] = body["expires"].replace("+00:00", "Z")
        file["content"] = base64.b64encode(json.dumps(body).encode()).decode()
        with self.assertRaises(ValueError):
            self.verify_history(records.__getitem__)
        records = self.history()
        reads = iter(("c"*40,"d"*40))
        def get(path):
            if path == "branches/top10-monitor-control":
                return {**records[path], "commit":{"sha":next(reads)}}
            return records[path]
        with self.assertRaises(ValueError):
            self.verify_history(get)

    def stamp(self, seconds):
        return (self.now+timedelta(seconds=seconds)).isoformat()

    def step(self, name, number, seconds):
        return dict(name=name, number=number, status="completed", conclusion="success",
                    started_at=self.stamp(seconds-1), completed_at=self.stamp(seconds))

    def live(self, name, seconds, previous=None):
        return live_step(self.run_info, self.meta, self.jobs, name, self.now,
                         self.now+timedelta(seconds=seconds), previous=previous)

    def test_live_then_terminal_zero_artifacts(self):
        armed = self.live(ARMED, 3)
        off = self.live(SIGNED_OFF, 11, armed)
        self.meta.update(status="completed", conclusion="cancelled")
        self.job.update(status="completed", conclusion="cancelled", completed_at=self.stamp(13))
        self.assertEqual(verify_terminal(self.run_info, 9, self.meta, self.jobs, {"total_count":0,"artifacts":[]},
                                         off, self.now+timedelta(seconds=12), opens=self.now, armed=armed,
                                         get=self.history().__getitem__), 1)
        with self.assertRaises(ValueError):
            verify_terminal(self.run_info, 9, self.meta, self.jobs, {"total_count":1,"artifacts":[{}]},
                            off, self.now+timedelta(seconds=12), opens=self.now, armed=armed,
                                         get=self.history().__getitem__)

    def test_reject_retrospective_and_late(self):
        with self.assertRaises(ValueError):
            self.live(ARMED, 20)
        self.meta.update(status="completed", conclusion="cancelled")
        with self.assertRaises(ValueError):
            self.live(ARMED, 3)

    def test_off_requires_prior_same_job(self):
        for previous in (None, (8,self.now,self.now,0), (9,self.now,self.now+timedelta(seconds=12),12)):
            with self.subTest(previous=previous), self.assertRaises(ValueError):
                self.live(SIGNED_OFF, 13, previous)

    def test_mismatched_attempt_release_and_step(self):
        for field, bad_value in (("run_attempt",2),("head_sha","f"*40)):
            original=self.meta[field]
            self.meta[field]=bad_value
            with self.assertRaises(ValueError):
                self.live(ARMED, 3)
            self.meta[field]=original
        self.job["steps"][0]["conclusion"]="skipped"
        with self.assertRaises(ValueError):
            self.live(ARMED, 3)
